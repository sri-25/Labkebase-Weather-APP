"""
Unit tests for app.py's /weather/search route, using Flask's test client
with lakebase and embedding both mocked out via sys.modules injection -
same technique as test_ingest_weather_embeddings.py, for the same reason:
app.py imports lakebase (needs live Databricks credentials just to import)
and eagerly loads the real embedding model at import time (needs
sentence-transformers + torch installed, plus a model download on first
use). Neither should be required just to test route logic and validation.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# embed_pipeline.py is also imported (fresh, for real) by test_embed_pipeline.py -
# since Python caches modules on first import, whichever test file imports it
# first would otherwise "win" and leave the other file's fake lakebase/embedding
# silently unused inside embed_pipeline's internals. Force a fresh import here
# so THIS file's fakes are what embed_pipeline actually binds to.
for _mod in ("lakebase", "embedding", "embed_pipeline", "app"):
    sys.modules.pop(_mod, None)

_fake_lakebase = MagicMock()
sys.modules["lakebase"] = _fake_lakebase

_fake_embedding = MagicMock()
_fake_embedding.EMBEDDING_MODEL_NAME = "fake-model"
_fake_embedding.EMBEDDING_DIM = 384
_fake_embedding.get_model = MagicMock(return_value=None)
_fake_embedding.embed_texts = MagicMock(return_value=[[0.1] * 384])
sys.modules["embedding"] = _fake_embedding

import app as flask_app_module  # noqa: E402


@pytest.fixture
def client():
    flask_app_module.app.testing = True
    return flask_app_module.app.test_client()


@pytest.fixture(autouse=True)
def reset_mocks():
    # reset_mock(return_value=True) also clears any return_value/side_effect
    # a previous test set (e.g. run_write.return_value = 0), not just call
    # history - otherwise a stale return_value could silently leak into a
    # later test that forgets to set its own, regardless of file order.
    _fake_lakebase.reset_mock(return_value=True, side_effect=True)
    _fake_embedding.reset_mock(return_value=True, side_effect=True)
    _fake_embedding.EMBEDDING_MODEL_NAME = "fake-model"
    _fake_embedding.EMBEDDING_DIM = 384
    _fake_embedding.embed_texts.return_value = [[0.1] * 384]
    yield


def test_index_route_renders_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Weather Intelligence" in resp.data


def test_stats_returns_counts_and_model_info(client):
    # Three run_query calls happen in order: documents count, embeddings
    # count, distinct-location count - side_effect feeds them back in
    # that order rather than one shared return_value.
    _fake_lakebase.run_query.side_effect = [
        [{"c": 43}],
        [{"c": 51}],
        [{"c": 3}],
    ]
    resp = client.get("/weather/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "documents": 43,
        "embeddings": 51,
        "locations": 3,
        "vector_model": "fake-model",
        "vector_dim": 384,
    }


def test_stats_ensures_tables_before_querying(client):
    _fake_lakebase.run_query.side_effect = [[{"c": 0}], [{"c": 0}], [{"c": 0}]]
    client.get("/weather/stats")
    assert _fake_lakebase.run_write.call_count >= 2  # documents + embeddings table setup


def test_search_missing_query_returns_400(client):
    resp = client.post("/weather/search", json={})
    assert resp.status_code == 400
    assert "query" in resp.get_json()["error"]


def test_search_empty_query_returns_400(client):
    resp = client.post("/weather/search", json={"query": "   "})
    assert resp.status_code == 400


def test_search_non_string_query_returns_400(client):
    resp = client.post("/weather/search", json={"query": 123})
    assert resp.status_code == 400


def test_search_invalid_top_k_returns_400(client):
    resp = client.post("/weather/search", json={"query": "flooding", "top_k": "abc"})
    assert resp.status_code == 400
    assert "top_k" in resp.get_json()["error"]


def test_search_invalid_source_type_returns_400(client):
    resp = client.post("/weather/search", json={"query": "flooding", "source_type": "banana"})
    assert resp.status_code == 400


def test_search_top_k_clamped_above_max(client):
    _fake_lakebase.run_query.return_value = []
    resp = client.post("/weather/search", json={"query": "flooding", "top_k": 500})
    assert resp.status_code == 200
    assert resp.get_json()["top_k"] == 20
    params = _fake_lakebase.run_query.call_args[0][1]
    assert params[-1] == 20


def test_search_top_k_clamped_below_min(client):
    _fake_lakebase.run_query.return_value = []
    resp = client.post("/weather/search", json={"query": "flooding", "top_k": -5})
    assert resp.status_code == 200
    assert resp.get_json()["top_k"] == 1


def test_search_empty_embeddings_table_returns_empty_results_not_error(client):
    _fake_lakebase.run_query.return_value = []
    resp = client.post("/weather/search", json={"query": "flash flood risk"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["results"] == []
    assert body["query"] == "flash flood risk"
    assert body["low_confidence"] is True


def test_search_returns_expected_result_shape(client):
    _fake_lakebase.run_query.return_value = [
        {
            "location": "Chicago, IL",
            "headline": "Flash Flood Warning",
            "narrative_text": "full text...",
            "source_type": "alert",
            "chunk_text": "Turn around, don't drown.",
            "similarity": 0.87,
        }
    ]
    resp = client.post("/weather/search", json={"query": "flooding risk"})
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert len(results) == 1
    assert results[0] == {
        "location": "Chicago, IL",
        "headline": "Flash Flood Warning",
        "source_type": "alert",
        "chunk_text": "Turn around, don't drown.",
        "similarity": 0.87,
    }


def test_search_without_summarize_flag_omits_summary(client):
    """summarize defaults to False - the base search contract doesn't pay
    LLM latency/cost unless explicitly asked for it."""
    _fake_lakebase.run_query.return_value = []
    resp = client.post("/weather/search", json={"query": "flooding"})
    body = resp.get_json()
    assert "summary" not in body
    assert "summary_error" not in body


def test_search_with_summarize_flag_calls_llm_and_includes_summary(client, monkeypatch):
    _fake_lakebase.run_query.return_value = [
        {"location": "Denver, CO", "headline": "Heat Advisory", "narrative_text": "x",
         "source_type": "alert", "chunk_text": "Hot today.", "similarity": 0.9}
    ]
    captured = {}

    def fake_summarize(query, results, low_confidence):
        captured["query"] = query
        captured["results"] = results
        captured["low_confidence"] = low_confidence
        return "It's hot in Denver today."

    monkeypatch.setattr(flask_app_module, "llm_summarize", fake_summarize)

    resp = client.post("/weather/search", json={"query": "how hot is it", "summarize": True})
    body = resp.get_json()
    assert body["summary"] == "It's hot in Denver today."
    assert captured["query"] == "how hot is it"
    assert captured["low_confidence"] is False
    assert len(captured["results"]) == 1


def test_search_summarize_failure_is_non_fatal(client, monkeypatch):
    """LLM summary is best-effort, matching the embed/cleanup best-effort
    pattern elsewhere - a flaky endpoint shouldn't break search results
    that already work fine without it."""
    _fake_lakebase.run_query.return_value = []

    def failing_summarize(query, results, low_confidence):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(flask_app_module, "llm_summarize", failing_summarize)

    resp = client.post("/weather/search", json={"query": "flooding", "summarize": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert "summary" not in body
    assert body["summary_error"] == "Summary unavailable right now."


def test_search_high_similarity_is_not_low_confidence(client):
    _fake_lakebase.run_query.return_value = [
        {"location": "Chicago, IL", "headline": "Tonight", "narrative_text": "x",
         "source_type": "forecast", "chunk_text": "Thunderstorms likely.", "similarity": 0.665}
    ]
    resp = client.post("/weather/search", json={"query": "thunderstorms"})
    assert resp.get_json()["low_confidence"] is False


def test_search_low_similarity_is_flagged_low_confidence(client):
    _fake_lakebase.run_query.return_value = [
        {"location": "Chicago, IL", "headline": "Tonight", "narrative_text": "x",
         "source_type": "forecast", "chunk_text": "Mostly clear.", "similarity": 0.448}
    ]
    resp = client.post("/weather/search", json={"query": "flood alert in Vermont"})
    body = resp.get_json()
    assert body["low_confidence"] is True
    # results are still returned (transparent API), UI decides display treatment
    assert len(body["results"]) == 1


def test_search_without_source_type_has_no_where_clause(client):
    _fake_lakebase.run_query.return_value = []
    client.post("/weather/search", json={"query": "flooding"})
    sql = _fake_lakebase.run_query.call_args[0][0]
    assert "WHERE" not in sql


def test_search_with_source_type_adds_where_clause_and_param(client):
    _fake_lakebase.run_query.return_value = []
    client.post("/weather/search", json={"query": "flooding", "source_type": "alert"})
    sql, params = _fake_lakebase.run_query.call_args[0]
    assert "WHERE d.source_type = %s" in sql
    assert "alert" in params


def test_search_uses_cosine_operator_and_vector_cast(client):
    _fake_lakebase.run_query.return_value = []
    client.post("/weather/search", json={"query": "flooding"})
    sql = _fake_lakebase.run_query.call_args[0][0]
    assert "<=>" in sql
    assert "::vector" in sql


def test_search_ensures_tables_before_querying(client):
    _fake_lakebase.run_query.return_value = []
    client.post("/weather/search", json={"query": "flooding"})
    # ensure_*_table() calls go through lakebase.run_write
    assert _fake_lakebase.run_write.called


# --- GET /weather/feed/recent ---

def test_recent_feed_default_limit(client):
    _fake_lakebase.run_query.return_value = []
    resp = client.get("/weather/feed/recent")
    assert resp.status_code == 200
    assert resp.get_json() == {"limit": 10, "last_synced_at": None, "items": []}
    params = _fake_lakebase.run_query.call_args[0][1]
    assert params == (10,)


def test_recent_feed_custom_limit(client):
    _fake_lakebase.run_query.return_value = []
    resp = client.get("/weather/feed/recent?limit=3")
    assert resp.get_json()["limit"] == 3
    params = _fake_lakebase.run_query.call_args[0][1]
    assert params == (3,)


def test_recent_feed_invalid_limit_returns_400(client):
    resp = client.get("/weather/feed/recent?limit=lots")
    assert resp.status_code == 400


def test_recent_feed_limit_clamped_above_max(client):
    """Max is 10, not 100 - this is a glanceable news feed, not a
    paginated archive (see DECISIONS.md Phase 9)."""
    _fake_lakebase.run_query.return_value = []
    resp = client.get("/weather/feed/recent?limit=999999")
    assert resp.get_json()["limit"] == 10


def test_recent_feed_limit_clamped_below_min(client):
    _fake_lakebase.run_query.return_value = []
    resp = client.get("/weather/feed/recent?limit=0")
    assert resp.get_json()["limit"] == 1


def test_recent_feed_has_no_source_type_filter(client):
    """Shows alerts AND forecasts both - the old /weather/alerts/recent
    only showed alerts, this is deliberately different."""
    _fake_lakebase.run_query.return_value = []
    client.get("/weather/feed/recent")
    sql = _fake_lakebase.run_query.call_args[0][0]
    assert "source_type" not in sql or "WHERE" not in sql


def test_recent_feed_has_no_watchlist_filter(client):
    """No watchlist concept anymore - the feed is unscoped by location."""
    _fake_lakebase.run_query.return_value = []
    client.get("/weather/feed/recent")
    sql = _fake_lakebase.run_query.call_args[0][0]
    assert "weather_watchlist" not in sql


def test_recent_feed_returns_rows_from_query(client):
    _fake_lakebase.run_query.return_value = [
        {"id": "urn:1", "location": "Chicago, IL", "source_type": "alert", "headline": "Flash Flood Warning",
         "narrative_text": "...", "issued_at": "t1", "effective_at": "t2", "synced_at": "t3"}
    ]
    resp = client.get("/weather/feed/recent")
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["items"]) == 1
    assert body["items"][0]["headline"] == "Flash Flood Warning"


def test_recent_feed_last_synced_at_is_most_recent_rows_synced_at(client):
    """last_synced_at reflects the actual data, not "when the UI last
    polled" - it's the first (newest, since ORDER BY synced_at DESC)
    row's synced_at."""
    _fake_lakebase.run_query.return_value = [
        {"id": "1", "location": "A", "source_type": "alert", "headline": "H1",
         "narrative_text": "x", "issued_at": None, "effective_at": None, "synced_at": "2026-08-07T10:00:00Z"},
        {"id": "2", "location": "B", "source_type": "forecast", "headline": "H2",
         "narrative_text": "x", "issued_at": None, "effective_at": None, "synced_at": "2026-08-07T09:00:00Z"},
    ]
    resp = client.get("/weather/feed/recent")
    assert resp.get_json()["last_synced_at"] == "2026-08-07T10:00:00Z"


def test_recent_feed_last_synced_at_null_when_empty(client):
    _fake_lakebase.run_query.return_value = []
    resp = client.get("/weather/feed/recent")
    assert resp.get_json()["last_synced_at"] is None


# --- cleanup_expired_alerts ---

def test_cleanup_expired_alerts_deletes_only_expired_alert_documents():
    """Forecasts don't get swept up here - their effective_at is a start
    time, not an expiration, so this predicate would be wrong for them."""
    _fake_lakebase.run_write.return_value = 4
    deleted = flask_app_module.cleanup_expired_alerts()
    assert deleted == 4
    sql = _fake_lakebase.run_write.call_args[0][0]
    assert "source_type = 'alert'" in sql
    assert "effective_at < now()" in sql
    assert "DELETE FROM weather_documents" in sql


def test_sync_triggers_expired_alert_cleanup(client, monkeypatch):
    monkeypatch.setattr(flask_app_module.WeatherClient, "sync_location", lambda self, loc, limit=50: [])
    monkeypatch.setattr(flask_app_module, "embed_documents_now", lambda docs: 0)

    cleanup_calls = []
    monkeypatch.setattr(flask_app_module, "cleanup_expired_alerts", lambda: cleanup_calls.append(1) or 0)

    client.post("/weather/sync", json={"locations": ["Denver, CO"]})
    assert cleanup_calls == [1]


def test_cleanup_failure_does_not_fail_the_sync_request(client, monkeypatch):
    monkeypatch.setattr(flask_app_module.WeatherClient, "sync_location", lambda self, loc, limit=50: [])
    monkeypatch.setattr(flask_app_module, "embed_documents_now", lambda docs: 0)

    def failing_cleanup():
        raise RuntimeError("db exploded")

    monkeypatch.setattr(flask_app_module, "cleanup_expired_alerts", failing_cleanup)

    resp = client.post("/weather/sync", json={"locations": ["Denver, CO"]})
    assert resp.status_code == 200


def test_sync_embeds_synced_documents_immediately(client, monkeypatch):
    synced_docs = [
        {"id": "doc1", "location": "Denver, CO", "source_type": "forecast",
         "headline": "Tonight", "narrative_text": "Clear skies.",
         "issued_at": None, "effective_at": None, "payload": {}}
    ]
    monkeypatch.setattr(flask_app_module.WeatherClient, "sync_location", lambda self, loc, limit=50: synced_docs)

    embed_calls = []
    monkeypatch.setattr(flask_app_module, "embed_documents_now", lambda docs: embed_calls.append(docs) or len(docs))

    resp = client.post("/weather/sync", json={"locations": ["Denver, CO"]})
    assert resp.status_code == 200
    assert embed_calls == [synced_docs]


def test_sync_embedding_failure_does_not_fail_the_request(client, monkeypatch):
    """Embedding is best-effort - if it throws, the sync itself should
    still report success (documents are already safely stored; the batch
    ingestion script will catch anything missed)."""
    synced_docs = [
        {"id": "doc1", "location": "Denver, CO", "source_type": "forecast",
         "headline": "Tonight", "narrative_text": "Clear skies.",
         "issued_at": None, "effective_at": None, "payload": {}}
    ]
    monkeypatch.setattr(flask_app_module.WeatherClient, "sync_location", lambda self, loc, limit=50: synced_docs)

    def failing_embed(docs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(flask_app_module, "embed_documents_now", failing_embed)

    resp = client.post("/weather/sync", json={"locations": ["Denver, CO"]})
    assert resp.status_code == 200
    assert resp.get_json()["synced"] == 1
