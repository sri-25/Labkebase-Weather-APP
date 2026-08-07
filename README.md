# Weather Intelligence — Unstructured Data → Lakebase Vector Search → REST API

Bootcamp homework: harvest unstructured weather text from the National Weather Service, embed it into vectors, store those vectors in Lakebase (Postgres + pgvector), and expose a semantic search REST API over it.

Full write-up (data source rationale, schema decisions, chunking/embedding parameters, known limitations) is in **[README_WEATHER.md](README_WEATHER.md)**. A complete, dated log of every decision, bug, and live-verification along the way is in **[DECISIONS.md](DECISIONS.md)**.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/setup_secrets.py     # one-time: store your Lakebase connection URL
python scripts/create_tables.py     # one-time: create weather_documents + weather_embeddings

python app.py                       # starts the Flask API on :8000
```

In a second terminal:

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'

curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "flash flood risk this weekend", "top_k": 5}'
```

## What's implemented

**Core deliverables:**
- `weather_client.py` — NWS API client (harvest alerts + forecasts, geocoding)
- `app.py` — `POST /weather/sync`, `POST /weather/search`
- `lakebase.py` + `sql/` — Postgres/pgvector schema (`weather_documents`, `weather_embeddings`)
- `notebooks/ingest_weather_embeddings.py` — psycopg3 chunk → embed → upsert pipeline
- `tests/` — 107 passing unit tests (fully mocked, no live infra needed)

**Stretch goals:**
- `llm_summary.py` — LLM-generated natural-language summary over search results (`"summarize": true`), via Databricks Foundation Model APIs — live-verified
- `notebooks/sync_weather_job.py` + `resources/sync_weather_job.json` — scheduled re-sync as a Databricks Job, live-verified running hourly on serverless compute
- `notebooks/hnsw_benchmark.py` — HNSW vs. sequential-scan query latency benchmark (built, not yet run against live data)
- Upsert/dedup on `id`, `source_type` filtering (alert vs. forecast) — both folded into the base `/weather/search` contract

`templates/index.html` is a search/sync UI with live stats and trend charts — not a required deliverable, just a convenience for demoing without curl. Can also be deployed as a persistent Databricks App (`app.yaml`).

## Project layout

```
weather_client.py, lakebase.py, app.py, chunking.py,        Core app modules
embedding.py, embed_pipeline.py, llm_summary.py
notebooks/                                                   Ingestion, scheduled job, HNSW benchmark
sql/                                                          Table DDL (documentation)
scripts/                                                       One-off/manual dev scripts (setup, verification, debugging)
tests/                                                        Automated pytest suite
templates/                                                    Optional search UI
```
