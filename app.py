"""
Weather Intelligence Flask API.

POST /weather/sync           harvest NWS alerts + forecasts into Lakebase
POST /weather/search          semantic search over synced docs, optional LLM summary
GET  /weather/feed/recent      recently synced alerts/forecasts
GET  /weather/stats(/trends)   corpus counts + chart data for the UI

Run: python app.py, then in another terminal:
    curl -X POST http://localhost:8000/weather/sync \
      -H "Content-Type: application/json" \
      -d '{"locations": ["Chicago, IL"], "limit": 10}'
"""
from __future__ import annotations

import json
import logging
import os

from flask import Flask, jsonify, render_template, request

import lakebase
from embed_pipeline import embed_documents_now
from embedding import EMBEDDING_DIM, EMBEDDING_MODEL_NAME, embed_texts, get_model
from llm_summary import summarize as llm_summarize
from weather_client import GeocodeError, WeatherClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-app")

app = Flask(__name__)

# Load once at import time, not per-request, so search doesn't pay the
# model-load cost on every call.
logger.info("Loading embedding model %s ...", EMBEDDING_MODEL_NAME)
get_model()
logger.info("Embedding model loaded.")

WEATHER_DOCUMENTS_TABLE = os.environ.get("WEATHER_DOCUMENTS_TABLE", "weather_documents")
WEATHER_EMBEDDINGS_TABLE = os.environ.get("WEATHER_EMBEDDINGS_TABLE", "weather_embeddings")

MIN_TOP_K = 1
MAX_TOP_K = 20
DEFAULT_TOP_K = 5

# pgvector's <=> always returns the closest K rows, even if none are
# actually relevant - below this score, flag it as low_confidence instead
# of presenting it as a real match. 0.5 picked empirically (real matches
# ran ~0.63-0.67, a known-irrelevant query ran ~0.41-0.45); override via
# env var if it needs retuning.
MIN_SIMILARITY = float(os.environ.get("WEATHER_MIN_SIMILARITY", 0.5))

MIN_RECENT_LIMIT = 1
MAX_RECENT_LIMIT = 10  # a glanceable news feed, not a browsable archive
DEFAULT_RECENT_LIMIT = 10

# Locations to sync by default when the request body doesn't specify any.
# Semicolon-separated (not comma) since "City, ST" already contains a comma.
DEFAULT_LOCATIONS = [
    loc.strip()
    for loc in os.environ.get(
        "WEATHER_DEFAULT_LOCATIONS", "Chicago, IL;Austin, TX;Miami, FL"
    ).split(";")
    if loc.strip()
]


def ensure_weather_documents_table() -> None:
    """Create the weather_documents table in Lakebase if it doesn't exist yet."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {WEATHER_DOCUMENTS_TABLE} (
            id TEXT PRIMARY KEY,
            location TEXT NOT NULL,
            source_type TEXT NOT NULL,
            headline TEXT,
            narrative_text TEXT,
            issued_at TIMESTAMPTZ,
            effective_at TIMESTAMPTZ,
            payload JSONB NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    lakebase.run_write(
        f"CREATE INDEX IF NOT EXISTS idx_{WEATHER_DOCUMENTS_TABLE}_location "
        f"ON {WEATHER_DOCUMENTS_TABLE} (location)"
    )
    lakebase.run_write(
        f"CREATE INDEX IF NOT EXISTS idx_{WEATHER_DOCUMENTS_TABLE}_source_type "
        f"ON {WEATHER_DOCUMENTS_TABLE} (source_type)"
    )


def ensure_weather_embeddings_table() -> None:
    """Create the weather_embeddings table (+ pgvector extension + HNSW
    index) in Lakebase if it doesn't exist yet."""
    lakebase.run_write("CREATE EXTENSION IF NOT EXISTS vector")
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {WEATHER_EMBEDDINGS_TABLE} (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES {WEATHER_DOCUMENTS_TABLE}(id) ON DELETE CASCADE,
            chunk_index INT NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding VECTOR(384) NOT NULL,
            model_name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    lakebase.run_write(
        f"CREATE INDEX IF NOT EXISTS idx_{WEATHER_EMBEDDINGS_TABLE}_embedding "
        f"ON {WEATHER_EMBEDDINGS_TABLE} USING hnsw (embedding vector_cosine_ops)"
    )
    lakebase.run_write(
        f"CREATE INDEX IF NOT EXISTS idx_{WEATHER_EMBEDDINGS_TABLE}_document_id "
        f"ON {WEATHER_EMBEDDINGS_TABLE} (document_id)"
    )


@app.route("/")
def index():
    """Search/sync UI. Serves templates/index.html."""
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page),
    so a frontend's resp.json() call never chokes on HTML."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/weather/sync", methods=["POST"])
def sync_weather():
    """
    Harvest active alerts + forecast periods for a list of locations from
    NWS and upsert them into weather_documents.

    Body (optional JSON): {"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}
    Defaults to DEFAULT_LOCATIONS when no locations are supplied. A location
    that fails to geocode or hits an NWS API error is skipped (reported back
    in "failures") rather than failing the whole request.
    """
    ensure_weather_documents_table()
    client = WeatherClient()

    body = request.json if request.is_json else {}
    locations = body.get("locations") or DEFAULT_LOCATIONS
    locations = [loc.strip() for loc in locations if isinstance(loc, str) and loc.strip()]
    limit = int(body.get("limit", 50))

    total = 0
    failures = []
    for location in locations:
        count, error = _sync_one_location(client, location, limit)
        total += count
        if error:
            failures.append({"location": location, "error": error})

    response = {"synced": total, "locations": locations}
    if failures:
        response["failures"] = failures
    return jsonify(response)


def _sync_one_location(client: WeatherClient, location: str, limit: int) -> tuple[int, str | None]:
    """Sync one location: harvest -> upsert -> embed. Returns
    (documents_synced, error_message_or_None). Shared with
    notebooks/sync_weather_job.py so the scheduled job reuses the same
    logic. Embeds inline so newly-synced docs are searchable right away
    rather than only after the next batch ingestion run."""
    try:
        documents = client.sync_location(location, limit=limit)
    except GeocodeError as e:
        logger.warning("Skipping %r: %s", location, e)
        return 0, str(e)
    except Exception as e:  # NWS API errors, network issues, etc.
        logger.warning("Failed to sync %r: %s", location, e)
        return 0, str(e)

    written = _upsert_documents_batch(documents)
    _embed_now_best_effort(documents, location)
    _cleanup_expired_alerts_best_effort(location)
    _cleanup_expired_forecasts_best_effort(location)
    return written, None


def _embed_now_best_effort(documents: list[dict], context: str) -> None:
    """Embed just-synced documents immediately. Best-effort - a failure
    here is logged, not raised; the batch ingestion script picks up
    anything missed on its next run."""
    if not documents:
        return
    try:
        embed_documents_now(documents)
    except Exception:
        logger.exception("Inline embedding failed for %s (will retry via batch ingestion)", context)


def _cleanup_expired_alerts_best_effort(context: str) -> None:
    """Same best-effort shape as _embed_now_best_effort - a slow/flaky
    cleanup DELETE shouldn't fail the sync request that triggered it."""
    try:
        deleted = cleanup_expired_alerts()
        if deleted:
            logger.info("Cleaned up %d expired alert(s) after syncing %s", deleted, context)
    except Exception:
        logger.exception("Expired-alert cleanup failed for %s (non-fatal)", context)


def _cleanup_expired_forecasts_best_effort(context: str) -> None:
    """Same best-effort shape as _cleanup_expired_alerts_best_effort - a
    slow/flaky cleanup DELETE shouldn't fail the sync request that
    triggered it."""
    try:
        deleted = cleanup_expired_forecasts()
        if deleted:
            logger.info("Cleaned up %d expired forecast(s) after syncing %s", deleted, context)
    except Exception:
        logger.exception("Expired-forecast cleanup failed for %s (non-fatal)", context)


@app.route("/weather/stats", methods=["GET"])
def stats():
    """Corpus counts (documents, embeddings, distinct locations) plus the
    embedding model/dimension - powers the UI's stat strip. Just three
    COUNT queries, cheap enough to call on every page load."""
    ensure_weather_documents_table()
    ensure_weather_embeddings_table()

    doc_count = lakebase.run_query(f"SELECT COUNT(*) AS c FROM {WEATHER_DOCUMENTS_TABLE}")[0]["c"]
    embedding_count = lakebase.run_query(f"SELECT COUNT(*) AS c FROM {WEATHER_EMBEDDINGS_TABLE}")[0]["c"]
    location_count = lakebase.run_query(
        f"SELECT COUNT(DISTINCT location) AS c FROM {WEATHER_DOCUMENTS_TABLE}"
    )[0]["c"]

    return jsonify({
        "documents": doc_count,
        "embeddings": embedding_count,
        "locations": location_count,
        "vector_model": EMBEDDING_MODEL_NAME,
        "vector_dim": EMBEDDING_DIM,
    })


@app.route("/weather/stats/trends", methods=["GET"])
def stats_trends():
    """Chart data for the UI's Trends panel: document counts by location,
    by source_type, and by day (last 14 days). Kept separate from
    GET /weather/stats since these GROUP BYs are heavier and only need to
    run once per page load, not on every poll.

    Tracks corpus/sync activity over time, not weather parameters like
    temperature - that would need parsing NWS's raw payload JSON, which
    nothing else in this codebase does yet.
    """
    ensure_weather_documents_table()

    locations = lakebase.run_query(
        f"""
        SELECT location, COUNT(*) AS count
        FROM {WEATHER_DOCUMENTS_TABLE}
        GROUP BY location
        ORDER BY count DESC
        LIMIT 10
        """
    )
    source_types = lakebase.run_query(
        f"""
        SELECT source_type, COUNT(*) AS count
        FROM {WEATHER_DOCUMENTS_TABLE}
        GROUP BY source_type
        """
    )
    daily = lakebase.run_query(
        f"""
        SELECT DATE(synced_at) AS day, source_type, COUNT(*) AS count
        FROM {WEATHER_DOCUMENTS_TABLE}
        WHERE synced_at >= now() - interval '14 days'
        GROUP BY DATE(synced_at), source_type
        ORDER BY day
        """
    )

    return jsonify({"locations": locations, "source_types": source_types, "daily": daily})


@app.route("/weather/feed/recent", methods=["GET"])
def recent_feed():
    """Most recently synced documents (alerts + forecasts), newest first.
    Capped small (top 10, not paginated) - a glanceable feed, not a
    browser; use /weather/search for that. Polled by the UI every ~30s.

    Query params: ?limit=10 (default 10, clamped [1, 10]).
    last_synced_at is the newest synced_at in the corpus, not "when the
    UI last polled."
    """
    ensure_weather_documents_table()

    raw_limit = request.args.get("limit", DEFAULT_RECENT_LIMIT)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return jsonify({"error": f"'limit' must be an integer, got {raw_limit!r}"}), 400
    limit = max(MIN_RECENT_LIMIT, min(limit, MAX_RECENT_LIMIT))

    rows = lakebase.run_query(
        f"""
        SELECT id, location, source_type, headline, narrative_text,
               issued_at, effective_at, synced_at
        FROM {WEATHER_DOCUMENTS_TABLE}
        ORDER BY synced_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    last_synced_at = rows[0]["synced_at"] if rows else None
    return jsonify({"limit": limit, "last_synced_at": last_synced_at, "items": rows})


def cleanup_expired_alerts() -> int:
    """Delete alerts past their NWS-reported expiration (effective_at =
    expires/ends, see WeatherClient.normalize_alert). Embeddings cascade
    via the FK, so expired alerts drop out of search too, not just the
    feed. Alert-only: forecasts' effective_at is a start time, not an
    expiration - see cleanup_expired_forecasts() for those. Runs after
    every sync rather than on a schedule (no cron infra) or at read-time
    (feed is polled every ~30s, too often for a DELETE)."""
    return lakebase.run_write(
        f"""
        DELETE FROM {WEATHER_DOCUMENTS_TABLE}
        WHERE source_type = 'alert'
          AND effective_at IS NOT NULL
          AND effective_at < now()
        """
    )


def cleanup_expired_forecasts() -> int:
    """Delete forecast periods once they've actually ended, using each
    period's own NWS-reported `endTime` (from `payload`) rather than a
    fixed hours cutoff - periods range from ~6 to ~12 hours, so one fixed
    number would be wrong for some of them either way. `payload ?
    'endTime'` guards rows missing the key. Same cascade-delete and
    best-effort-after-sync shape as cleanup_expired_alerts()."""
    return lakebase.run_write(
        f"""
        DELETE FROM {WEATHER_DOCUMENTS_TABLE}
        WHERE source_type = 'forecast'
          AND payload ? 'endTime'
          AND (payload->>'endTime')::timestamptz < now()
        """
    )


@app.route("/weather/search", methods=["POST"])
def search_weather():
    """
    Semantic search over synced weather documents.

    Body: {"query": "flash flood risk this weekend", "top_k": 5,
           "source_type": "alert", "summarize": true}
    source_type is optional (alert/forecast only). summarize is optional,
    default false - when true, an LLM reads the retrieved chunks and
    answers in plain language (see llm_summary.py); off by default so
    callers don't pay LLM latency/cost unless they ask for it.

    Returns the top_k most similar chunks (pgvector cosine distance),
    each with its parent document's location/headline attached.
    """
    ensure_weather_documents_table()
    ensure_weather_embeddings_table()

    body = request.json if request.is_json else {}

    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return jsonify({"error": "'query' is required and must be a non-empty string"}), 400
    query = query.strip()

    raw_top_k = body.get("top_k", DEFAULT_TOP_K)
    try:
        top_k = int(raw_top_k)
    except (TypeError, ValueError):
        return jsonify({"error": f"'top_k' must be an integer, got {raw_top_k!r}"}), 400
    top_k = max(MIN_TOP_K, min(top_k, MAX_TOP_K))

    source_type = body.get("source_type")
    if source_type is not None and source_type not in ("alert", "forecast"):
        return jsonify({"error": "'source_type' must be 'alert' or 'forecast' if provided"}), 400

    query_vector = embed_texts([query])[0]
    vector_literal = "[" + ",".join(str(x) for x in query_vector) + "]"

    sql = f"""
        SELECT d.location, d.headline, d.narrative_text, d.source_type, e.chunk_text,
               1 - (e.embedding <=> %s::vector) AS similarity
        FROM {WEATHER_EMBEDDINGS_TABLE} e
        JOIN {WEATHER_DOCUMENTS_TABLE} d ON d.id = e.document_id
    """
    params: list = [vector_literal]
    if source_type:
        sql += " WHERE d.source_type = %s"
        params.append(source_type)
    sql += " ORDER BY e.embedding <=> %s::vector LIMIT %s"
    params.extend([vector_literal, top_k])

    rows = lakebase.run_query(sql, tuple(params))

    results = [
        {
            "location": r["location"],
            "headline": r["headline"],
            "source_type": r["source_type"],
            "chunk_text": r["chunk_text"],
            "similarity": float(r["similarity"]),
        }
        for r in rows
    ]

    low_confidence = (not results) or (results[0]["similarity"] < MIN_SIMILARITY)

    response_body = {
        "query": query,
        "top_k": top_k,
        "results": results,
        "low_confidence": low_confidence,
    }

    if bool(body.get("summarize", False)):
        try:
            response_body["summary"] = llm_summarize(query, results, low_confidence)
        except Exception:
            # Best-effort - a flaky LLM endpoint shouldn't take down
            # search results that already work fine without it.
            logger.exception("LLM summary failed (search results still returned)")
            response_body["summary_error"] = "Summary unavailable right now."

    return jsonify(response_body)


def _upsert_documents_batch(documents: list[dict]) -> int:
    """Upsert a batch of normalized weather documents into weather_documents."""
    if not documents:
        return 0

    count = 0
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            for doc in documents:
                cur.execute(
                    f"""
                    INSERT INTO {WEATHER_DOCUMENTS_TABLE} (
                        id, location, source_type, headline, narrative_text,
                        issued_at, effective_at, payload, synced_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (id) DO UPDATE
                        SET location = EXCLUDED.location,
                            source_type = EXCLUDED.source_type,
                            headline = EXCLUDED.headline,
                            narrative_text = EXCLUDED.narrative_text,
                            issued_at = EXCLUDED.issued_at,
                            effective_at = EXCLUDED.effective_at,
                            payload = EXCLUDED.payload,
                            synced_at = EXCLUDED.synced_at
                    """,
                    (
                        doc["id"],
                        doc["location"],
                        doc["source_type"],
                        doc.get("headline"),
                        doc.get("narrative_text"),
                        doc.get("issued_at"),
                        doc.get("effective_at"),
                        json.dumps(doc.get("payload") or {}),
                    ),
                )
                count += 1
            conn.commit()
    return count


if __name__ == "__main__":
    # Databricks Apps auto-injects these same two vars for detected Flask
    # apps, so this works unchanged locally or deployed.
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", 8000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"Flask app running on http://{host}:{port}")
    # threaded=True: UI polls stats/feed on their own timers alongside
    # whatever the user's doing; single-threaded dev server would queue
    # those behind a slow sync request.
    app.run(debug=debug, host=host, port=port, threaded=True)
