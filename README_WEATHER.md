# Weather Intelligence — Unstructured Data → Lakebase Vector Search → REST API

A pipeline that harvests unstructured weather text from the National Weather Service, embeds it into vectors, stores those vectors in Lakebase (Postgres + pgvector), and exposes a semantic search endpoint over it — mirroring the `databricks-lakebase-app-day-2` ticker-news pipeline, applied to a new unstructured data source.

## Data source: National Weather Service API (api.weather.gov)

Chosen over OpenWeatherMap and NOAA CPC discussion products for three reasons:

- **No API key.** Zero auth plumbing, so effort goes into the actual harvest/vectorize/retrieve pipeline instead of credential management.
- **Genuinely rich free text.** Alert `description`/`instruction` fields ("A Flash Flood Warning means...", "Turn around, don't drown...") and forecast `detailedForecast` narratives ("Sunny, with a high near 78...") are real prose, not just structured numbers — exactly what embeddings are for.
- **Clean JSON API.** OpenWeatherMap gates alerts behind paid tiers on some plans; NOAA's CPC discussion products require HTML scraping, not a documented JSON contract.

Two source types are harvested per location: active **alerts** (`/alerts/active?point={lat},{lon}`) and multi-day **forecasts** (`/gridpoints/{office}/{x},{y}/forecast`).

## Architecture

```
weather_client.py          NWS API client + geocoding (harvest)
lakebase.py                 Lakebase (Postgres) connection helper
app.py                       Flask API: POST /weather/sync, POST /weather/search
chunking.py                  Sliding-window text chunker
embedding.py                  sentence-transformers wrapper (384-dim)
notebooks/ingest_weather_embeddings.py   Chunk + embed + upsert pipeline script
sql/*.sql                    Table DDL (documentation; also auto-created by the code)
scripts/                      Manual/one-off dev scripts, not part of the running app:
scripts/create_tables.py        Standalone table-creation/verification script
scripts/setup_secrets.py        One-time Lakebase secret setup
scripts/verify_connection.py    Manual Lakebase connectivity smoke test
scripts/verify_embedding_model.py  Manual embedding-model smoke test
scripts/peek_documents.py       Manual inspection of weather_documents
scripts/peek_embeddings.py      Manual inspection of weather_embeddings
scripts/reset_corpus.py         Wipes weather_documents/weather_embeddings (destructive, --yes required)
notebooks/sync_weather_job.py   Scheduled re-sync entrypoint (Databricks Job)
notebooks/hnsw_benchmark.py     HNSW vs. sequential-scan latency benchmark
llm_summary.py                LLM-generated summary over search results (RAG)
tests/                        Automated pytest suite (100 tests, fully mocked)
DECISIONS.md                  Full chronological log of decisions, gotchas, and verifications
```

**Pipeline flow:**

1. **Harvest** — `weather_client.py` resolves each location to lat/lon, calls NWS, normalizes alerts + forecast periods into a document schema, and `POST /weather/sync` upserts them into `weather_documents`.
2. **Vectorize** — `notebooks/ingest_weather_embeddings.py` finds documents with no embeddings yet, chunks their text, embeds each chunk, and upserts into `weather_embeddings`.
3. **Retrieve** — `POST /weather/search` embeds the query with the same model and ranks `weather_embeddings` by pgvector cosine distance (`<=>`), joined back to `weather_documents` for location/headline.

## Schema decisions

### Location resolution (geocoding)

NWS takes lat/lon, not city names, and has no geocoding endpoint. Three-layer resolver in `weather_client.py`:

1. Raw `"lat,lon"` strings bypass geocoding entirely.
2. A small static dict for the 3 V1 seed cities (Chicago IL, Austin TX, Miami FL — chosen for weather variety: storms, heat, hurricane/flood risk) — zero network calls, deterministic, good for tests.
3. Fallback to OpenStreetMap's Nominatim geocoder (free, no key) for any other "City, ST" string. (The US Census geocoder was tried first, since it's a well-known free/no-key option — but it only matches specific street addresses against TIGER address ranges, and never resolves a bare "City, ST" query, no matter how well-known the city. Caught via a live test adding "Denver, CO" to the watchlist; full story in `DECISIONS.md`.)

Adding a new city later requires no code change — just pass its name.

### `weather_documents`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | Alert's own NWS URN, or a synthesized `{gridId}-{gridX}-{gridY}-fc-{period}-{startTime}` for forecast periods (NWS forecasts have no natural stable ID) |
| `location` | TEXT | The location string as passed to `/weather/sync` |
| `source_type` | TEXT | `"alert"` or `"forecast"` |
| `headline` | TEXT | Alert event name, or forecast period name (e.g. "Tonight") |
| `narrative_text` | TEXT | The text that gets embedded — alert `description` + `instruction` combined, or `detailedForecast` |
| `issued_at` / `effective_at` | TIMESTAMPTZ | When the alert/forecast was issued and when it takes effect/expires |
| `payload` | JSONB | Raw NWS response, kept for provenance |
| `synced_at` | TIMESTAMPTZ | Set by the DB on insert/update |

Indexed on `location` and `source_type`.

### `weather_embeddings`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `{document_id}_{chunk_index}` |
| `document_id` | TEXT | FK → `weather_documents.id`, `ON DELETE CASCADE` |
| `chunk_index` | INT | Position within the parent document |
| `chunk_text` | TEXT | The actual chunk that was embedded |
| `embedding` | VECTOR(384) | pgvector column |
| `model_name` | TEXT | Which embedding model produced this vector |
| `created_at` | TIMESTAMPTZ | |

HNSW index on `embedding` using `vector_cosine_ops`, plus an index on `document_id`.

### Chunking

`CHUNK_SIZE=800`, `CHUNK_OVERLAP=100` — the values the homework spec suggests, kept rather than inventing new ones since they already match the existing ticker-news pipeline's convention. Most individual NWS forecast periods are well under 800 characters and end up as a single unsplit chunk; chunking mainly matters for the longer combined alert `description` + `instruction` text.

### Embedding model

`sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions — the same model the ticker-news pipeline uses, so both stay compatible with the same distance-operator conventions. Small enough to run on a laptop CPU with no GPU.

## Database driver: psycopg3 (started on psycopg2, matching the reference app)

This went through two corrections, both driven by a real crash rather than a preference:

An early draft used `pg8000` instead of `psycopg2`, based on a since-superseded fork of the reference app that had hit a SIGABRT crash from psycopg2's bundled libssl colliding with a Databricks runtime's SSL library. The instructor's actual canonical repo (`EcZachly/databricks-lakebase-app-day-2`) uses plain `psycopg2` + `RealDictCursor` throughout, so the project switched to match it exactly.

That worked everywhere it was tested locally — until the scheduled Databricks Job (running on serverless compute) hit the same class of SIGABRT on a completely clean, single-package `psycopg2-binary` install: its bundled OpenSSL collides with `grpc`'s bundled OpenSSL, both already loaded in that Python 3.12 serverless process. Not a duplicate-install issue this time — genuinely platform-specific. Migrated to `psycopg` (psycopg3), whose binary wheel doesn't bundle OpenSSL the same conflicting way. The `%s`-style parameter placeholders used everywhere in this project's SQL were unaffected by the swap; only `lakebase.py`'s connection/cursor setup and `embed_pipeline.py`'s bulk upsert (psycopg3 has no `execute_values()` equivalent, so that's now a hand-built multi-row `INSERT`) changed. Full story in `DECISIONS.md`.

## How to run end-to-end

### 1. Environment setup

Use a dedicated virtual environment — not a shared Anaconda `base` environment, which can have conflicting OpenSSL/cryptography builds:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Provision Lakebase (one-time)

Via the Databricks CLI (`postgres` command group — Lakebase's provisioning model as of 2026 is project/branch/endpoint-based, not the older "instance" UI flow):

```bash
databricks auth login --host https://<your-workspace>.cloud.databricks.com
databricks postgres create-project weather-intel --json '{"spec": {"display_name": "Weather Intelligence"}}'
databricks postgres list-branches projects/weather-intel
databricks postgres list-endpoints projects/weather-intel/branches/production
databricks postgres get-endpoint projects/weather-intel/branches/production/endpoints/primary
databricks postgres create-role projects/weather-intel/branches/production --role-id weather-app --json '{"spec": {"postgres_role": "weather_app"}}'
```

Then store the resulting connection URL as a secret:

```bash
python scripts/setup_secrets.py
```

### 3. Create the tables

```bash
python scripts/create_tables.py
```

Verifies the connection and creates `weather_documents` + `weather_embeddings` (including enabling the `pgvector` extension and the HNSW index).

### 4. Run the app and sync some weather

```bash
python app.py   # leave running in one terminal
```

In a second terminal:

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'
```

### 5. Embed the synced documents

```bash
python notebooks/ingest_weather_embeddings.py
```

Runs as a plain Python script (no Spark, no dbutils) — usable locally, via cron, or as a Databricks Job "python script" task.

### 6. Search

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "flash flood risk this weekend", "top_k": 5}'
```

Optional `"source_type": "alert"` or `""forecast"` narrows results to one type.

## Testing

- **`tests/`** — 51 automated tests (pytest), all hermetic (no live network, no live DB, no real model download). `weather_client.py` and `chunking.py` are tested directly; `app.py` and the ingestion script both import `lakebase.py`, which requires live Databricks credentials just to *import* (it connects at module load time, not lazily) — worked around at the test level by injecting a fake `lakebase` module into `sys.modules` before import, rather than changing the production code.
- **Manual smoke tests** — `scripts/verify_connection.py`, `scripts/verify_embedding_model.py` — for the two pieces that genuinely need live infrastructure (a real Lakebase connection, a real model download) and shouldn't run automatically in CI. Named `verify_` rather than `test_` specifically so plain `pytest` (run from the project root) never tries to collect them as automated tests.
- **Live end-to-end verification** — every stage of the pipeline (`/weather/sync`, the ingestion script, `/weather/search`) was run against real NWS data and a real Lakebase instance, not just mocks. Full trail in `DECISIONS.md`.

## Known limitations / future improvements

- ~~Forecast document IDs go stale.~~ **Fixed.** The synthesized forecast ID is still stable across re-syncs *within* one NWS forecast cycle but changes when NWS regenerates the package (~twice daily) — but superseded periods are now deleted automatically (`cleanup_expired_forecasts()`, runs after every sync, deletes a forecast period once its own NWS-reported `endTime` has passed). Same cascade-delete mechanism as the existing alert cleanup. This closes a real risk: without it, a stale forecast could outscore the current one in search and get handed to the LLM summary as if it were current.
- **Geocoding coverage.** Only 3 cities have static coordinates; everything else depends on the Census geocoder being reachable and returning a match. US-only.
- **No embedding staleness detection.** If a document's `narrative_text` changes (e.g. an alert gets updated) but its `id` stays the same, the ingestion script's "find unembedded documents" query won't catch it, since it only looks for documents with *zero* embedding rows, not *outdated* ones. A content hash comparison would close this gap.
- **Stretch goals not yet built:** scheduled Databricks Job for automatic re-sync, `GET /weather/search` with an LLM-generated summary (basic RAG), and an HNSW vs. no-index latency benchmark.
- **Single embedding model, no re-embedding path.** If the model ever changes, `weather_embeddings.embedding` would need a new dimension and a full re-embed — there's no migration tooling for that yet.
- **Small model, limited semantic precision on short text.** `all-MiniLM-L6-v2` is a small, general-purpose model, not fine-tuned on weather text. Live-observed example: a query for "rain" scored closer to unrelated air-quality alerts than to a "Severe Thunderstorm Watch" alert that was already fully embedded (confirmed via the ingestion script finding zero unembedded documents) — a real gap in this model's grasp of weather-domain vocabulary, not a retrieval bug. A larger or domain-tuned embedding model would likely narrow this.

## Deliverables checklist

- [x] `weather_client.py` — NWS client + geocoding
- [x] `app.py` — `POST /weather/sync`, `POST /weather/search`
- [x] `lakebase.py` + `sql/*.sql` — DDL for `weather_documents`, `weather_embeddings`
- [x] `notebooks/ingest_weather_embeddings.py` — psycopg3-based embedding pipeline
- [x] `README_WEATHER.md`
- [x] Upsert/dedup on `id` (base requirement, via `ON CONFLICT`)
- [x] `source_type` filter on retrieval
- [ ] Scheduled Databricks Job re-sync (stretch)
- [ ] `GET /weather/search` with LLM summary (stretch)
- [ ] HNSW benchmark (stretch)
