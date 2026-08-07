"""
Weather Intelligence Flask API.
- Serves POST /weather/sync - harvest NWS alerts + forecasts into Lakebase
- Serves POST /weather/search - semantic search over synced weather docs,
  optionally with an LLM-generated summary (see llm_summary.py)
- Serves GET /weather/feed/recent - small "what's new" news feed
- Reads/writes to Lakebase (Databricks-managed Postgres) via lakebase.py
- Pulls unstructured weather text from the NWS API via weather_client.py

Run locally (needs two terminal windows - app.py stays running in one):
    python app.py

Then, in a SECOND terminal:
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
from embedding import EMBEDDING_MODEL_NAME, embed_texts, get_model
from llm_summary import summarize as llm_summarize
from weather_client import GeocodeError, WeatherClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-app")

app = Flask(__name__)

# Load the embedding model once, here at module level, NOT inside the
# /weather/search route - loading it per-request would make every search
# pay the model-load cost (slow) instead of just the one-time startup cost.
logger.info("Loading embedding model %s ...", EMBEDDING_MODEL_NAME)
get_model()
logger.info("Embedding model loaded.")

WEATHER_DOCUMENTS_TABLE = os.environ.get("WEATHER_DOCUMENTS_TABLE", "weather_documents")
WEATHER_EMBEDDINGS_TABLE = os.environ.get("WEATHER_EMBEDDINGS_TABLE", "weather_embeddings")

MIN_TOP_K = 1
MAX_TOP_K = 20
DEFAULT_TOP_K = 5

# Below this cosine similarity, a result is "the least-bad option we have"
# rather than a genuine match - pgvector's <=> always returns the closest
# K rows even if none are actually relevant, so without a floor, search
# for a completely untracked city/topic quietly returns misleading
# results instead of admitting "nothing relevant here." 0.5 is an
# empirically-chosen heuristic (see DECISIONS.md) based on real matches
# scoring ~0.63-0.67 vs. a real known-irrelevant query scoring ~0.41-0.45
# - not a universal constant, tune via env var if it misfires in practice.
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
    """Search + news feed UI. Populating the corpus (POST /weather/sync)
    is an API/script/scheduled-job operation, not a UI action - see
    DECISIONS.md Phase 9 for why the watchlist add/remove UI was removed
    rather than kept as the only way to get data in."""
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
    """Sync one location end to end: harvest -> upsert -> embed. Returns
    (documents_synced, error_message_or_None). Shared by POST /weather/sync
    and notebooks/sync_weather_job.py (the scheduled re-sync).

    Embeds inline (not just upserts raw text) so whatever was just synced
    is searchable immediately - without this, a freshly-added city would
    say "synced: N" while /weather/search silently found nothing for it
    until someone remembered to run the batch ingestion script by hand."""
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
    return written, None


def _embed_now_best_effort(documents: list[dict], context: str) -> None:
    """Embed just-synced documents immediately. Failures here are logged,
    not raised - the raw documents are already safely stored either way,
    and the batch ingestion script will pick up anything missed on its
    next scheduled run, so a slow/flaky embedding call shouldn't fail the
    whole sync request."""
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


@app.route("/weather/feed/recent", methods=["GET"])
def recent_feed():
    """
    A small, glanceable "news feed" of whatever's actually in the corpus -
    the most recently synced documents, ALERTS AND FORECASTS BOTH, newest
    first. Polled by the UI every ~30s.

    Deliberately NOT scoped by location/watchlist (there is no watchlist
    concept anymore, see DECISIONS.md Phase 9) and deliberately capped
    small (top 10, not paginated) - this is meant to answer "what's new,"
    not double as a full document browser. Use /weather/search for that.

    Query params: ?limit=10 (default 10, clamped [1, 10])

    Response includes last_synced_at - the most recent synced_at across
    the whole corpus (i.e. when data was actually last pulled, whether by
    a manual /weather/sync call or the scheduled job) - NOT the same as
    "when the UI last polled this endpoint," which is what a naive
    client-side timestamp would otherwise show.
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
    """
    Delete alert documents whose NWS-reported expiration
    (effective_at = expires/ends, see WeatherClient.normalize_alert) has
    already passed. weather_embeddings rows go with them automatically
    via the existing ON DELETE CASCADE foreign key, so search stops
    surfacing expired alerts too, not just the Recent Alerts feed.

    Only applies to source_type='alert' - forecast periods' effective_at
    is their *start* time, not an expiration, so this predicate would be
    wrong for them (it's alert-specific by design, not a blanket "delete
    old rows" job).

    Run opportunistically after every sync (not on a schedule - there's
    no scheduler/cron infra yet) rather than at read-time, since this is
    a write, and the news feed is polled every ~30s - running a DELETE on
    every read would be wasteful.
    """
    return lakebase.run_write(
        f"""
        DELETE FROM {WEATHER_DOCUMENTS_TABLE}
        WHERE source_type = 'alert'
          AND effective_at IS NOT NULL
          AND effective_at < now()
        """
    )


@app.route("/weather/search", methods=["POST"])
def search_weather():
    """
    Semantic search over synced weather documents.

    Body: {"query": "flash flood risk this weekend", "top_k": 5,
           "source_type": "alert", "summarize": true}
    "source_type" is optional (filters to "alert" or "forecast" only).
    "summarize" is optional (default false) - when true, an LLM (Databricks
    Foundation Model API, see llm_summary.py) reads the retrieved chunks
    and answers the question in plain language, instead of leaving the
    user to read through raw similarity-ranked cards themselves. Off by
    default so the base search contract (and its tests) stay unchanged
    and every caller doesn't pay LLM latency/cost unless they ask for it.

    Returns the top_k most semantically similar chunks (pgvector cosine
    distance via the <=> operator), each with its parent document's
    location/headline attached.
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
        SELECT d.location, d.headline, d.narrative_text, e.chunk_text,
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
            "chunk_text": r["chunk_text"],
            "similarity": float(r["similarity"]),
        }
        for r in rows
    ]

    # "Low confidence" = nothing relevant enough exists in the tracked
    # corpus, not "the database is broken" - the UI treats these two
    # cases very differently.
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
            # Best-effort, same shape as the embed/cleanup best-effort
            # steps elsewhere - a flaky/misconfigured LLM endpoint
            # shouldn't take down search results that already work fine
            # without it.
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
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", 8000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"Flask app running on http://{host}:{port}")
    app.run(debug=debug, host=host, port=port)
