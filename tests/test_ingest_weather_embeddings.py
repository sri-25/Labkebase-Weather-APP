"""
Unit tests for notebooks/ingest_weather_embeddings.py - specifically the
"what changed" detection (find_unembedded_documents), which is the only
logic still specific to this script. The chunk/embed/upsert logic itself
moved to embed_pipeline.py (shared with app.py) - see
test_embed_pipeline.py for those tests.

lakebase.py calls WorkspaceClient() at import time, which requires live
Databricks credentials and fails immediately without them. So before
importing anything that transitively imports lakebase, we inject a fake
lakebase module into sys.modules. This keeps the real lakebase.py
untouched (matches the canonical reference pattern exactly) while still
letting this script's own logic be tested in isolation, hermetically.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "notebooks"))

_fake_lakebase = MagicMock()
sys.modules["lakebase"] = _fake_lakebase

import ingest_weather_embeddings as ingest  # noqa: E402


@pytest.fixture(autouse=True)
def reset_fake_lakebase():
    _fake_lakebase.reset_mock()
    yield


def test_find_unembedded_documents_queries_left_join():
    _fake_lakebase.run_query.return_value = [{"id": "doc1", "narrative_text": "hello"}]
    result = ingest.find_unembedded_documents()
    assert result == [{"id": "doc1", "narrative_text": "hello"}]
    sql_arg = _fake_lakebase.run_query.call_args[0][0]
    assert "LEFT JOIN weather_embeddings" in sql_arg
    assert "e.id IS NULL" in sql_arg


def test_find_unembedded_documents_with_limit_appends_limit_clause():
    _fake_lakebase.run_query.return_value = []
    ingest.find_unembedded_documents(limit=5)
    args, _ = _fake_lakebase.run_query.call_args
    sql_arg, params = args
    assert "LIMIT %s" in sql_arg
    assert params == (5,)


def test_run_orchestrates_find_build_upsert(monkeypatch, capsys):
    """run() should call find -> build -> upsert in order and report
    counts - the chunk/embed/upsert internals themselves are covered in
    test_embed_pipeline.py, this just checks the wiring."""
    _fake_lakebase.run_query.return_value = [{"id": "doc1", "narrative_text": "hi"}]
    monkeypatch.setattr(ingest, "build_chunk_rows", lambda docs: [("doc1_0", "doc1", 0, "hi", "[0.1]", "m")])
    monkeypatch.setattr(ingest, "upsert_chunk_rows", lambda rows: len(rows))

    ingest.run(limit=10)

    out = capsys.readouterr().out
    assert "Found 1 unembedded documents" in out
    assert "Wrote 1 chunk embeddings" in out


def test_run_no_unembedded_documents_short_circuits(monkeypatch, capsys):
    _fake_lakebase.run_query.return_value = []
    calls = {"build": 0}
    monkeypatch.setattr(ingest, "build_chunk_rows", lambda docs: calls.__setitem__("build", calls["build"] + 1) or [])

    ingest.run()

    assert calls["build"] == 0  # never called - nothing to embed
    assert "Nothing to do" in capsys.readouterr().out


def test_find_all_documents_has_no_left_join():
    """--all bypasses the "already embedded" check entirely - no
    weather_embeddings JOIN at all, unlike find_unembedded_documents."""
    _fake_lakebase.run_query.return_value = [{"id": "doc1", "narrative_text": "hello"}]
    result = ingest.find_all_documents()
    assert result == [{"id": "doc1", "narrative_text": "hello"}]
    sql_arg = _fake_lakebase.run_query.call_args[0][0]
    assert "LEFT JOIN" not in sql_arg
    assert "weather_embeddings" not in sql_arg


def test_find_all_documents_with_limit_appends_limit_clause():
    _fake_lakebase.run_query.return_value = []
    ingest.find_all_documents(limit=5)
    args, _ = _fake_lakebase.run_query.call_args
    sql_arg, params = args
    assert "LIMIT %s" in sql_arg
    assert params == (5,)


def test_run_with_reembed_all_calls_find_all_documents(monkeypatch, capsys):
    _fake_lakebase.run_query.return_value = [{"id": "doc1", "narrative_text": "hi"}]
    monkeypatch.setattr(ingest, "build_chunk_rows", lambda docs: [("doc1_0", "doc1", 0, "hi", "[0.1]", "m")])
    monkeypatch.setattr(ingest, "upsert_chunk_rows", lambda rows: len(rows))

    ingest.run(reembed_all=True)

    sql_arg = _fake_lakebase.run_query.call_args[0][0]
    assert "LEFT JOIN" not in sql_arg  # confirms find_all_documents' query ran, not find_unembedded_documents'
    out = capsys.readouterr().out
    assert "Re-embedding ALL documents" in out
    assert "Wrote 1 chunk embeddings" in out
