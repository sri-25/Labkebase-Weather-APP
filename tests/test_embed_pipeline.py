"""
Unit tests for embed_pipeline.py - the chunk -> embed -> upsert logic
shared by notebooks/ingest_weather_embeddings.py (batch job) and app.py
(embeds immediately after every sync). Moved here from
test_ingest_weather_embeddings.py when the logic itself moved out of the
script and into this shared module.

Same sys.modules mocking technique as the other DB-touching test files:
lakebase.py requires live Databricks credentials just to import, so a fake
lakebase is injected into sys.modules before importing embed_pipeline.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force a fresh import - app.py also imports the real embed_pipeline, and
# whichever test file imports it first would otherwise leave the OTHER
# file's fake lakebase silently unused inside embed_pipeline's internals
# (Python caches modules after first import).
for _mod in ("lakebase", "embedding", "embed_pipeline"):
    sys.modules.pop(_mod, None)

_fake_lakebase = MagicMock()
sys.modules["lakebase"] = _fake_lakebase

_fake_embedding_mod = MagicMock()
_fake_embedding_mod.EMBEDDING_MODEL_NAME = "fake-model"
_fake_embedding_mod.embed_texts = MagicMock(return_value=[])
sys.modules["embedding"] = _fake_embedding_mod

import embed_pipeline  # noqa: E402


@pytest.fixture(autouse=True)
def reset_fake_lakebase():
    _fake_lakebase.reset_mock(return_value=True, side_effect=True)
    yield


def test_build_chunk_rows_short_document_produces_one_chunk(monkeypatch):
    monkeypatch.setattr(embed_pipeline, "embed_texts", lambda texts: [[0.1, 0.2, 0.3] for _ in texts])
    docs = [{"id": "doc1", "narrative_text": "short forecast text"}]
    rows = embed_pipeline.build_chunk_rows(docs)

    assert len(rows) == 1
    row_id, document_id, chunk_index, chunk_piece, vector_literal, model_name = rows[0]
    assert row_id == "doc1_0"
    assert document_id == "doc1"
    assert chunk_index == 0
    assert chunk_piece == "short forecast text"
    assert vector_literal == "[0.1,0.2,0.3]"
    assert model_name == embed_pipeline.EMBEDDING_MODEL_NAME


def test_build_chunk_rows_long_document_produces_multiple_chunks(monkeypatch):
    monkeypatch.setattr(embed_pipeline, "embed_texts", lambda texts: [[0.0] for _ in texts])
    long_text = "x" * 1000  # -> 2 chunks at CHUNK_SIZE=800/OVERLAP=100
    docs = [{"id": "doc1", "narrative_text": long_text}]
    rows = embed_pipeline.build_chunk_rows(docs)

    assert len(rows) == 2
    assert rows[0][0] == "doc1_0"
    assert rows[1][0] == "doc1_1"
    assert rows[0][2] == 0
    assert rows[1][2] == 1


def test_build_chunk_rows_empty_documents_list_skips_embedding_entirely(monkeypatch):
    calls = {"n": 0}

    def fake_embed(texts):
        calls["n"] += 1
        return []

    monkeypatch.setattr(embed_pipeline, "embed_texts", fake_embed)
    assert embed_pipeline.build_chunk_rows([]) == []
    assert calls["n"] == 0


def test_build_chunk_rows_whitespace_only_document_produces_no_rows(monkeypatch):
    monkeypatch.setattr(embed_pipeline, "embed_texts", lambda texts: [[0.0] for _ in texts])
    docs = [{"id": "doc1", "narrative_text": "   "}]
    rows = embed_pipeline.build_chunk_rows(docs)
    assert rows == []


def test_build_chunk_rows_prefixes_location_and_headline_before_embedding(monkeypatch):
    """Phase 4 fix: the TEXT SENT TO THE EMBEDDING MODEL should include
    location + headline (so a query naming a city can actually match), but
    the chunk_text STORED for display should stay the raw, clean text -
    see DECISIONS.md Phase 4 for why (a plain "how's the weather in
    seattle" query had nothing to match against "seattle" at all before
    this fix)."""
    captured_texts = []

    def fake_embed(texts):
        captured_texts.extend(texts)
        return [[0.1] for _ in texts]

    monkeypatch.setattr(embed_pipeline, "embed_texts", fake_embed)
    docs = [{
        "id": "doc1",
        "location": "Seattle, WA",
        "headline": "Air Quality Alert",
        "narrative_text": "Unhealthy air quality expected today.",
    }]
    rows = embed_pipeline.build_chunk_rows(docs)

    assert captured_texts == ["Seattle, WA — Air Quality Alert: Unhealthy air quality expected today."]
    # stored chunk_text (index 3 of the row tuple) stays clean, no prefix
    assert rows[0][3] == "Unhealthy air quality expected today."


def test_build_chunk_rows_missing_location_and_headline_embeds_raw_text(monkeypatch):
    """No location/headline on the document (e.g. old rows, or a caller
    that doesn't supply them) - falls back to embedding the raw chunk text
    exactly as before this fix, no crash, no stray prefix."""
    captured_texts = []

    def fake_embed(texts):
        captured_texts.extend(texts)
        return [[0.1] for _ in texts]

    monkeypatch.setattr(embed_pipeline, "embed_texts", fake_embed)
    docs = [{"id": "doc1", "narrative_text": "plain text, no metadata"}]
    embed_pipeline.build_chunk_rows(docs)

    assert captured_texts == ["plain text, no metadata"]


def test_upsert_chunk_rows_empty_list_does_not_touch_db():
    written = embed_pipeline.upsert_chunk_rows([])
    assert written == 0
    _fake_lakebase.get_connection.assert_not_called()


def test_upsert_chunk_rows_calls_execute_values_with_vector_cast_and_commits(monkeypatch):
    mock_cur = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    _fake_lakebase.get_connection.return_value.__enter__.return_value = mock_conn

    captured = {}

    def fake_execute_values(cur, sql, rows, template=None):
        captured["sql"] = sql
        captured["rows"] = rows
        captured["template"] = template

    monkeypatch.setattr(embed_pipeline, "execute_values", fake_execute_values)

    rows = [("doc1_0", "doc1", 0, "text", "[0.1,0.2]", "model-x")]
    written = embed_pipeline.upsert_chunk_rows(rows)

    assert written == 1
    assert "ON CONFLICT (id) DO UPDATE" in captured["sql"]
    assert "%s::vector" in captured["template"]
    assert captured["rows"] == rows
    mock_conn.commit.assert_called_once()


def test_embed_documents_now_chains_build_and_upsert(monkeypatch):
    monkeypatch.setattr(embed_pipeline, "embed_texts", lambda texts: [[0.1] for _ in texts])
    mock_cur = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    _fake_lakebase.get_connection.return_value.__enter__.return_value = mock_conn
    monkeypatch.setattr(embed_pipeline, "execute_values", lambda *a, **k: None)

    docs = [{"id": "doc1", "narrative_text": "short text"}]
    written = embed_pipeline.embed_documents_now(docs)
    assert written == 1


def test_embed_documents_now_empty_list_returns_zero_no_db_call():
    written = embed_pipeline.embed_documents_now([])
    assert written == 0
    _fake_lakebase.get_connection.assert_not_called()
