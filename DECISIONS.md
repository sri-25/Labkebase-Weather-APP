# Weather Intelligence — Decisions & Learnings Log

Running log of what we decided, why, and what we learned along the way.
Updated as we build, one phase at a time. Treat this as the "teacher's notes"
companion to README_WEATHER.md (which is the polished, submission-facing doc).

---

## Phase 0 — Planning

### Decision: Data source = National Weather Service API (api.weather.gov)
**Why:** It's the assignment's own recommended source, and for good reason:
no API key/auth (so effort goes into the actual pipeline, not auth
plumbing), generous rate limits, and genuinely rich free-text fields
(`description`/`instruction` on alerts, `detailedForecast` on forecast
periods) that are exactly the kind of unstructured text embeddings are
built for.
**Rejected alternatives:**
- OpenWeatherMap — alerts are gated behind paid tiers in some plans.
- NOAA CPC discussion products — HTML scraping, not a clean JSON API.

### Decision: DB driver = pg8000, not psycopg2 — SUPERSEDED, see correction below
**Why this matters:** The homework spec's wording says "psycopg2" and
"RealDictCursor." But the reference app (`ticker-news-lakebase-app`) we're
mirroring *used to* try psycopg2 and hit a real SIGABRT crash — psycopg2's
binary wheel bundles its own libssl, which collides with the SSL library
already loaded in the Databricks runtime. Its own `sql/README.md` documents
this explicitly. Given the spec's intent is clearly "use a standard
Python Postgres driver with this connection pattern," not "psycopg2
specifically at all costs," we're using pg8000 to match the reference
app's actual (working) driver.
**What's different in practice:** pg8000 has no `RealDictCursor` — we
zip `cursor.description` column names onto each row manually (see
`lakebase.py`'s `_rows_as_dicts()` helper, copied from the reference).
pg8000 cursors also don't support the `with conn.cursor() as cur:` context
manager protocol — must be `cur = conn.cursor()` + manual `cur.close()` in
a `finally` block.
**Documented in:** README_WEATHER.md, with the same justification.

> **CORRECTION (see Phase 1.5 below):** the repo above turned out to be an
> outdated fork. The instructor's actual canonical repo
> (`EcZachly/databricks-lakebase-app-day-2`) uses plain `psycopg2` +
> `RealDictCursor` throughout, including the ingestion notebook. Reverted
> to psycopg2. Left this entry in place rather than deleting it — the
> reasoning was sound given the information available at the time, and
> it's a useful reminder to verify against the canonical source before
> making an architecture call based on a single fork.

---

## Phase 1.5 — Correction: switched back to psycopg2

**What happened:** the instructor posted an updated video + pointed at the
canonical repo (`github.com/EcZachly/databricks-lakebase-app-day-2`), which
is different from the fork (`sri-25/ticker-news-lakebase-app`) originally
cloned for research. Diffing the two:
- `lakebase.py`: canonical uses `psycopg2.connect(url, cursor_factory=RealDictCursor)`
  + a SQLAlchemy `get_engine()` helper — not pg8000.
- `requirements.txt`: `psycopg2-binary` + `sqlalchemy`, no pg8000.
- `notebooks/ingest_ticker_news_embeddings.py`: now uses `psycopg2` +
  `psycopg2.extras.execute_values` directly for all writes (not Spark JDBC,
  not pg8000) — matching the homework spec's wording exactly.
- The notebook's first cell runs `%pip uninstall -y psycopg2 psycopg2-binary`
  before reinstalling — this is the actual fix for the crash the other fork
  hit: having *both* `psycopg2` and `psycopg2-binary` installed
  simultaneously (or a stale/conflicting one) causes the libssl collision,
  not psycopg2 itself. A clean single install doesn't have this problem.

**Decision:** use `psycopg2-binary` + `RealDictCursor` + SQLAlchemy engine,
matching the canonical repo exactly. `requirements.txt` updated
accordingly. No code needed to be un-built since `lakebase.py` hadn't been
written yet when this correction landed — caught before it cost anything.

**Lesson for next time:** verify against the instructor's canonical
source early, not just whatever fork/example turns up first in a search
or was linked earlier in the course. A fork's documented crash workaround
can itself be outdated.

### Decision: Location resolution — two-layer geocoding
**Why:** NWS's API takes lat/lon, not city names — there's no geocoding
endpoint. A fully hardcoded city→lat/lon dict doesn't scale ("how do we
add more cities?").
**Design:**
1. Small static dict for the 3 V1 seed cities (fast, deterministic, no
   network — good for unit tests that shouldn't depend on external
   services).
2. Fallback to the US Census Geocoder API (`geocoding.geo.census.gov`,
   free, no key, US addresses/places) for any city/state not in the dict.
3. Raw `"lat,lon"` strings bypass geocoding entirely (spec explicitly
   allows this location format).
**Expansion story:** adding a new city is just passing its name in the
`/weather/sync` request body — no code change required.

### Decision: V1 seed locations
Chicago, IL / Austin, TX / Miami FL — chosen for weather variety (severe
storms, heat, hurricane/flood risk), which maximizes the odds of having
real active alerts to test retrieval against, not just forecast text.

### Decision: Testing strategy — local Docker pgvector + real Lakebase
**Why:** Provisioning a fresh Databricks workspace/Lakebase instance is
itself a phase of work and shouldn't block iteration on the pipeline
logic. We stand up `docker-compose.yml` with Postgres + pgvector locally
so schema/ingest/retrieval can be tested end-to-end in minutes, then do a
final real-Lakebase run once infra is up, deployed via the Databricks CLI.

### Decision: source_type filtering folded into V1 (not deferred to stretch)
The spec's base requirement already has us harvesting both `"alert"` and
`"forecast"` source types. Since the schema already has a `source_type`
column, adding an optional filter param to `/weather/search` is nearly
free — so it's in V1 rather than held back as a stretch goal.

### Deliverables checklist (from homework spec)
- [ ] `weather_client.py` — NWS client + geocoding
- [ ] `app.py` — `POST /weather/sync`, `POST /weather/search`
- [ ] `lakebase.py` + `sql/*.sql` — DDL for `weather_documents`, `weather_embeddings`
- [ ] `notebooks/ingest_weather_embeddings.py` — psycopg2/pg8000-based embedding pipeline
- [ ] `README_WEATHER.md`
- [ ] Stretch: GET variant with LLM summary (basic RAG)
- [ ] Stretch: upsert dedup (base requirement via `ON CONFLICT`, not just stretch)
- [ ] Stretch: scheduled Databricks Job re-sync
- [ ] Stretch: source_type filter (folded into V1, see above)
- [ ] Stretch: HNSW benchmark (with vs. without index)

---

## Phase 1 — Infra provisioning (in progress)

### Learning: Lakebase provisioning model changed since the reference app was written
The reference app's README walks through creating a Lakebase "instance" via
the workspace UI (Catalog tab → Create Lakebase instance → Roles &
Databases tab). That flow is now outdated: since March 2026, Lakebase uses
a **projects → branches → endpoints** hierarchy (branches work like git
branches for your Postgres database — you can fork a branch for
dev/testing) and is fully CLI-driven via the `databricks postgres` command
group. We used the current model for this project instead of the older
one described in the reference README.

### Decision: dedicated secret scope, not shared with the ticker-news app
Scope `weather` / key `lakebase-url` (vs. the reference app's `database`/
`lakebase-url`) — keeps this project's credentials isolated so we can't
accidentally clobber the ticker-news app's secret if both live in the same
workspace. `lakebase.py` keeps the same `LAKEBASE_SECRET_SCOPE` /
`LAKEBASE_SECRET_KEY` env var override pattern as the reference, just with
different defaults.

### Provisioning steps (CLI)
```
databricks auth login --host https://<workspace>.cloud.databricks.com
databricks postgres create-project weather-intel --json '{"spec": {"display_name": "Weather Intelligence"}}'
databricks postgres list-branches projects/weather-intel
databricks postgres list-endpoints projects/weather-intel/branches/production
databricks postgres get-endpoint projects/weather-intel/branches/production/endpoints/primary
databricks postgres create-role projects/weather-intel/branches/production --role-id weather-app --json '{"spec": {"postgres_role": "weather_app"}}'
databricks secrets create-scope weather
databricks secrets put-secret weather lakebase-url --string-value "postgresql://weather_app:<password>@<host>:5432/databricks_postgres?sslmode=require"
```

### Learning: pg8000 script (not Spark JDBC) can run DDL directly
The reference notebook's `sql/README.md` explains it needed *manual* SQL
setup scripts because Spark JDBC can't run `CREATE EXTENSION`, arbitrary
DDL, or `ON CONFLICT` upserts. Since our ingestion script talks to Postgres
directly via pg8000 (no Spark), it doesn't have that limitation — it can
run `CREATE EXTENSION IF NOT EXISTS vector` and create tables/indexes
itself, same as `ensure_*_table()` does in `app.py`. We still keep
`sql/*.sql` files for documentation/manual reference, but they're not a
required manual step like they were for the reference notebook.

### Provisioning result
- Project: `weather-intel`, branch: `production`
- Endpoint host: `ep-broad-mud-d8l3cd74.database.us-east-2.cloud.databricks.com`
- Password role: `weatherApp` (native password auth, not OAuth)
- Secret scope/key: `weather` / `lakebase-url` (password itself intentionally
  NOT recorded here — see note below)

### Decision: never store the raw LAKEBASE_URL in a file, even locally
The connection string briefly existed in plaintext (pasted mid-chat to
finish provisioning). Once it's stored as a Databricks secret, `lakebase.py`
fetches it via `WorkspaceClient().secrets.get_secret(...)` every time —
same as the reference app — rather than reading a local `.env`. That means
local runs of `python app.py` work purely off `databricks auth login`
credentials (which were already set up for provisioning), and the real
secret value never needs to touch disk in this repo, committed or not.
`.gitignore` still excludes `.env`/`.env.local` as defense in depth in case
a future contributor adds one for overriding non-secret config (table
names, model name, etc.).

_(status: DONE — secret `weather/lakebase-url` stored and confirmed)_

### Learning: local environment — use a project venv, not Anaconda `(base)`
Running `setup_secrets.py` directly in the Anaconda `(base)` env failed with
a `cryptography` native-extension symbol error
(`symbol not found in flat namespace '_ERR_get_error_all'`) — a classic
conda-vs-pip OpenSSL version mismatch, unrelated to our code. Fixed by
using a project-local virtual environment built from Homebrew's Python
(`/opt/homebrew/bin/python3`, 3.9.10) instead of conda's:
```
python3 -m venv .venv
source .venv/bin/activate
pip install databricks-sdk
```
`.venv/` is already in `.gitignore`. This is the environment convention
for the rest of the project — every "how to run" instruction in
README_WEATHER.md assumes an activated `.venv`, not system/conda Python.

### Verification: weather_embeddings table + pgvector extension
`python create_tables.py` (extended) created `weather_embeddings`
successfully, including `CREATE EXTENSION IF NOT EXISTS vector` — no
permission issues on the `weatherApp` role. `embedding` column confirmed
as type `vector`. FK `document_id -> weather_documents(id) ON DELETE
CASCADE` in place per the spec's schema requirement.

### Verification: chunking.py
Pure-logic sliding-window chunker (CHUNK_SIZE=800, CHUNK_OVERLAP=100,
matching the spec's suggested values and the ticker-news pipeline's
convention). 12 unit tests, all passing, checking exact character
positions (not just chunk counts) — overlap region genuinely identical
between consecutive chunks, no characters dropped at boundaries, no chunk
ever exceeds the requested size, invalid overlap>=size rejected. No DB or
network involved, so this suite runs anywhere instantly. Full project test
suite now 30/30.

### Verification: embedding.py (sentence-transformers/all-MiniLM-L6-v2)
`python test_embedding_model.py` on the user's machine: 3 sentences ->
3 vectors, each exactly 384 numbers (matches EMBEDDING_DIM, matches the
weather_embeddings.embedding column). Cosine similarity check:
"Flash Flood Warning" vs. a flooding-related sentence scored 0.437;
same sentence vs. an unrelated sunny-weather sentence scored 0.074 - a
clean, meaningful gap confirming the model captures actual semantic
meaning, not just keyword overlap. This is the real proof the whole
retrieval concept (Part 3) will work once wired up.
Could not pre-verify this one in the build sandbox: `sentence-transformers`
pulls in `torch` as a dependency (large download), which exceeds the
sandbox's 45-second per-command limit, and background processes don't
survive between separate tool calls there - so this was verified entirely
on the user's machine, same as the NWS-dependent pieces.

### Decision: notebooks/ingest_weather_embeddings.py - psycopg2, not Spark
Plain Python script (argparse, no dbutils/widgets) so it runs identically
via `python notebooks/ingest_weather_embeddings.py` locally or as a
Databricks Job "python script" task - matching the homework spec's
explicit instruction not to use `spark.write.jdbc` (which the reference
notebook's own `sql/README.md` documents as unable to run DDL, write
VECTOR columns, or do ON CONFLICT upserts against this Lakebase instance).
"What's new" detection = a LEFT JOIN against weather_embeddings, filtered
to unmatched rows - our hand-rolled equivalent of Delta's Change Data
Feed, since Postgres has no built-in change-tracking feature the way
Delta tables do. Batches ALL chunks from ALL pending documents into one
embed_texts() call (not one call per document) for throughput, then
writes via a single execute_values() upsert keyed on
`{document_id}_{chunk_index}`.

### Gotcha/learning: lakebase.py requires live credentials just to import
`import lakebase` executes `WorkspaceClient()` immediately at module load
time (not lazily on first use) - so any code that imports it, even
indirectly, can't be unit-tested without live Databricks credentials
configured. This blocked testing `ingest_weather_embeddings.py`'s logic
in the sandbox. Fixed at the TEST level, not by changing `lakebase.py`
(which would diverge from the canonical reference pattern): inject a fake
module into `sys.modules["lakebase"]` before importing anything that
imports the real one. Standard Python testing technique, zero production
code changes. See `tests/test_ingest_weather_embeddings.py`.

### Verification: ingestion script logic (mocked lakebase + embedding model)
8 unit tests, all passing: LEFT JOIN query shape (incl. LIMIT handling),
chunk-row assembly for both short (1 chunk) and long (2 chunks, matching
chunking.py's own tests) documents, empty-input short-circuits (no wasted
embed_texts() call), whitespace-only documents produce zero rows, empty
upsert list never touches the DB, and the real upsert path is verified to
use `ON CONFLICT (id) DO UPDATE` and cast via `%s::vector` before
committing. Full suite now 38/38.

### Verification: ingestion script end-to-end against real Lakebase
`python notebooks/ingest_weather_embeddings.py` then `python
peek_embeddings.py` against the 10 real Chicago forecast documents synced
earlier: 10 chunks across 10 documents (1:1 - every forecast period's text
was short enough to stay in a single chunk, exactly as expected).
Confirmed via JOIN back to weather_documents that chunk text matches the
correct location/headline. Part 2 (vectorize) fully done and proven
against live infrastructure.

### Decision: POST /weather/search design
Model loaded once at module level (`get_model()` called at import time,
not inside the route) per the spec's explicit instruction - avoids paying
model-load cost on every request. `source_type` filter (alert/forecast)
folded in as an optional body param, not held back as a stretch goal,
since the schema already supports it cheaply. Edge cases handled
explicitly rather than left to fail organically: missing/non-string/empty
query -> 400 with a clear message; non-integer top_k -> 400; top_k clamped
to [1, 20] rather than rejected outright (out-of-range is a minor user
error, not a malformed request); invalid source_type -> 400; empty
weather_embeddings table -> 200 with `results: []`, not an error (querying
zero rows isn't a failure state). Both `ensure_*_table()` calls run first
so a fresh/never-synced deployment fails gracefully instead of a raw
Postgres "relation does not exist" error.

### Verification: /weather/search unit tests (mocked lakebase + embedding)
Same sys.modules mocking technique as the ingestion script tests, extended
to also mock `embedding` (app.py eagerly loads the real model at import
time - same reason it can't be imported bare in the sandbox/CI). 13 tests
via Flask's test client, all passing: every validation edge case above,
empty-table -> empty-results (not an error), source_type present vs.
absent correctly changes the generated SQL (WHERE clause + param), the
query uses `<=>` and `::vector` as required, and both ensure-table calls
happen before querying. Full suite now 51/51.

### Verification: POST /weather/search end-to-end against real Lakebase
`curl -X POST /weather/search -d '{"query": "chance of thunderstorms this
week", "top_k": 3}'` returned the 3 correct thunderstorm-related forecast
periods (Sunday, Saturday Night, Monday Night) out of all 10 synced
documents, ranked by similarity (0.665, 0.644, 0.638) - meaningfully
higher than unrelated periods would score. Full V1 (Parts 1-3) now proven
end to end against live NWS data and a live Lakebase instance, not just
mocks. **V1 is functionally complete.**

### Scope expansion: full app (UI + all stretch goals)
After V1 was proven, user asked for: a real UI, a watchlist (cities being
tracked), a lightweight "new alerts" feed, and all remaining stretch
goals (LLM summary, scheduled job, HNSW benchmark). Decision: LLM summary
uses Databricks Foundation Model APIs (already-authenticated databricks-sdk,
no new secret) over OpenAI/Anthropic. The "flashed news feed" idea was
scoped down deliberately: a true real-time push system needs a background
worker + websockets (disproportionate infra for this project); a
30-second-polling "what's new" endpoint gets the same felt experience for
a fraction of the complexity - explained this tradeoff and got agreement
before building.

### Bug found + fixed: Census geocoder doesn't do city-level lookups
Live test: adding "Denver, CO" to the watchlist failed with "Nominatim
found no match" (well, "Census" at the time) - a real US city failing to
resolve was suspicious, so checked the live API directly rather than
assuming Denver was just an edge case. Confirmed via a direct call to
`geocoding.geo.census.gov/geocoder/locations/onelineaddress?address=Denver,+CO`:
`"addressMatches": []` - genuinely empty. Root cause: that Census endpoint
matches STREET ADDRESSES against TIGER address ranges
("1600 Pennsylvania Ave, Washington, DC" works); it was never designed
for bare "City, ST" queries, no matter how well-known the city. Wrong
tool for this job from the start - this code path had never been tested
against the live API (only against a mocked response shape invented from
memory), so the mismatch went undetected until real usage surfaced it.
**Fix:** switched the fallback geocoder to OpenStreetMap's Nominatim
(`nominatim.openstreetmap.org/search`), which is built for place/city-level
queries. Free, no key, subject to a ~1 req/sec usage policy (fine for this
project's interactive one-city-at-a-time use). Response shape is a JSON
array of matches (not the Census's nested dict), with lat/lon as strings
needing `float()` conversion - updated `_geocode_nominatim()` and its
tests accordingly. All 57 tests still passing after the swap.
**Lesson:** "free geocoder found no match" was worth treating as
suspicious rather than assumed-correct, given how common the input city
was - verified against the live API directly instead of trusting the
mocked test's assumptions.

### Verification: GET /weather/alerts/recent
7 new tests (64/64 full suite): default minutes=60, custom minutes,
invalid (non-integer) -> 400, clamping both above 1440 and below 1, SQL
correctly filters `source_type = 'alert'` and uses `make_interval(mins =>
%s)` (chosen over `(%s || ' minutes')::interval` string concatenation -
cleaner, no text-cast ambiguity with a parameterized integer), and row
shape passthrough.

### Verification: GET /weather/alerts/recent against real Lakebase
Real Denver alerts returned: "Air Quality Alert" (x4, different issue
times) and "Heat Advisory," full real NWS narrative text, correctly
filtered to source_type='alert' only (no forecast periods mixed in).
Confirms the alert path end to end, not just the forecast path Chicago
happened to exercise earlier.

### Decision: UI = server-rendered template + vanilla JS, no build step
`templates/index.html` - single file, inline CSS/JS, fetch() calls to the
existing JSON endpoints. No React/npm/bundler, matching the reference
app's simplicity and keeping `python app.py` the only thing needed to run
it (nothing to `npm install` or build first). Three panels: search (with
a summary slot ready for the LLM stretch goal, currently unused until that
endpoint exists), watchlist (add/remove, calls the endpoints built above),
recent-alerts feed (polls every 30s, highlights newly-seen alert IDs via
an in-memory JS Set - no localStorage needed, nothing has to survive a
page reload).

### Product scope clarification: tracked-cities corpus, not live-any-city
Live UI test ("flood alert this weekend in Vermont") exposed that search
silently returned Chicago forecasts at ~44% similarity for a query about
an untracked state - correct mechanically (pgvector always returns the
closest K rows) but misleading. Talked through the actual product shape:
this is Model A, a tracked-cities corpus with semantic search *within*
it (matching the ticker-news reference pattern exactly - track tickers,
sync their news, search that news), not Model B, a live "ask about any US
city" agent (which wouldn't really need Lakebase/pgvector the way it's
used here). Decision: keep building Model A, but make it stop behaving
like it silently promises Model B.

### Decision: cascade delete on watchlist removal
Removing a city from `weather_watchlist` now also deletes its
`weather_documents` rows (embeddings cascade automatically via the
existing FK `ON DELETE CASCADE`). Rationale: forecast text specifically
decays fast - a forecast synced last week isn't stale, it's simply wrong
now - so keeping an untracked city's old text searchable indefinitely
would make results actively misleading, not just less fresh. Cities
synced via a one-off POST /weather/sync (never watchlisted) are
unaffected by this - a separate, lower-priority reconciliation noted as a
future improvement.

### Decision: MIN_SIMILARITY floor + low_confidence flag on /weather/search
0.5 cosine similarity, chosen empirically from exactly two real data
points so far: genuine matches scored ~0.63-0.67 (the "thunderstorms"
query against Chicago forecasts), a known-irrelevant query scored
~0.41-0.45 (Vermont flood query against Chicago forecasts). Below that
floor, the API still returns `results` (stays transparent/debuggable) but
sets `"low_confidence": true`; the UI treats that as "nothing relevant is
tracked yet" and shows a message + points at the watchlist panel, instead
of rendering weak matches as if they were real results. Explicitly noted
this threshold is a heuristic, not a guarantee - short, formulaic weather
text doesn't necessarily separate cleanly by topic in embedding space,
and 0.5 may need retuning with more real examples (env var override:
WEATHER_MIN_SIMILARITY).

### Verification: weather_watchlist + POST/GET/DELETE /weather/watchlist
19/19 tests in test_app.py (57/57 full suite), incl.: empty/missing
location -> 400, unresolvable location -> 400 *before* it's ever written
to the table (geocode validated first - fail fast on typos), successful
add both inserts (ON CONFLICT DO NOTHING) AND triggers an immediate sync
via a new shared `_sync_one_location()` helper (extracted from
/weather/sync's loop body - same logic, no duplication), remove missing
-> 404, remove existing -> 200. DELETE takes a JSON body rather than a
URL path segment, since location strings like "Chicago, IL" are awkward
to URL-encode from a plain fetch() call.

### Verification: POST /weather/sync end-to-end against real NWS + real Lakebase
`curl -X POST /weather/sync -d '{"locations": ["Chicago, IL"], "limit": 10}'`
returned `{"synced": 10}`, confirmed by querying the table directly
(`peek_documents.py`): 10 real forecast periods with actual NWS narrative
text ("A slight chance of rain showers before 9pm...", etc.), correctly
capped at the requested limit. All 10 were `source_type: forecast` because
Chicago had zero active alerts at test time — expected behavior (alerts
are ordered first when present; see `sync_location()`), not a bug.
Part 1 (harvest) is fully done and proven against live infrastructure, not
just mocked tests.

### Gotcha: target Python is 3.9, but the build sandbox is 3.10
`lakebase.py` used `tuple | dict | None`-style union type hints (PEP 604),
which only work at runtime on Python 3.10+. The build sandbox runs 3.10, so
`pytest` there didn't catch it — but the user's real local `.venv`
(Homebrew Python 3.9.10) hit `TypeError: unsupported operand type(s) for
|: 'type' and 'type'` immediately on `import lakebase`. Same latent bug
existed in `weather_client.py`, untested locally until this surfaced it.
**Fix:** added `from __future__ import annotations` (PEP 563) as the first
line of both files — defers annotation evaluation so they're never
actually executed as code, just stored as text. Works identically on 3.9
and 3.10+, zero behavior change otherwise.
**Going forward:** every new `.py` file in this project gets this import
as a standing habit, since the target environment is confirmed 3.9.

### Verification: weather_documents table created and confirmed
`python create_tables.py` ran successfully against the real Lakebase
instance — all 9 expected columns present with correct types (`id`/
`location`/`source_type`/`headline`/`narrative_text` as `text`,
`issued_at`/`effective_at`/`synced_at` as `timestamp with time zone`,
`payload` as `jsonb`). `ensure_weather_documents_table()` is ready to be
folded into `app.py` as-is.

### Verification: live connection confirmed
`python test_connection.py` succeeded against the real Lakebase instance:
`PostgreSQL 17.10` on Ubuntu 24.04. Secret fetch → psycopg2 connect →
query → result, fully working end to end. `lakebase.py` is done; safe to
build on top of it now.

### Gotcha: `pip install databricks-sdk` needs the hyphen
`pip install databricks sdk` (space instead of hyphen) silently installs
two unrelated packages (`databricks` and `sdk`) instead of the one we
need. Pip package names are single tokens; a space means "install two
separate things." Worth remembering for `psycopg2-binary` and
`sentence-transformers` later too.

---

## Phase 2 — Part 1: `weather_client.py` (harvest)

### Learning: sandbox network can't reach api.weather.gov
The build sandbox routes all traffic through an allowlist proxy that
blocks `api.weather.gov` (`403 blocked-by-allowlist`) and even
`WebFetch` returned empty for it. So `weather_client.py` was built and
unit-tested entirely against NWS's *documented* schema (cross-checked via
`weather-gov.github.io/api` and NWS API discussions), not live responses.
**Action for you:** run `pytest tests/ -v` locally (works everywhere,
fully mocked) but also do one live smoke-test call once you have this
checked out locally, e.g.:
```python
from weather_client import WeatherClient
c = WeatherClient()
print(c.sync_location("Chicago, IL", limit=5))
```
to confirm the live schema still matches what's coded. If NWS has changed
a field name since this was written, this is where it'd surface.

### Decision: alert narrative_text = description + instruction combined
The `instruction` field ("Turn around, don't drown...") often carries the
actionable guidance separately from `description`. Combining both into one
`narrative_text` (blank-line separated) gives richer embeddings than
either alone — and this is exactly the case the homework spec calls out
as "where chunking may actually matter" for otherwise-short NWS text.

### Decision: alert `id` = NWS's own alert URN; forecast `id` = synthesized
Alerts already carry a globally stable identifier
(`properties.id`, a URN like `urn:oid:2.49.0.1.840.0...`) — used directly,
no synthesis needed. Forecast periods have no such ID, so we build one from
`{gridId}-{gridX}-{gridY}-fc-{period_number}-{startTime}`.
**Known limitation:** this ID is stable across re-syncs *within* the same
forecast cycle (so re-running `/weather/sync` upserts correctly instead of
duplicating), but changes when NWS regenerates the forecast package
(~twice daily) — old period rows aren't deleted, just no longer the
"current" one for that slot. A follow-up cleanup job (delete forecast docs
older than N hours) would be the fix; documented in README_WEATHER.md
rather than built for V1.

### Decision: geocoding tested for both dict-hit and Census-fallback paths
Static dict lookups run with zero network calls (deterministic, fast unit
tests). The Census geocoder fallback path is tested with a mocked HTTP
response rather than skipped, so the fallback logic itself is verified
even though we can't hit the real Census endpoint from this sandbox either.

### Verification
`pytest tests/test_weather_client.py -v` — **18/18 passed**, covering:
geocoding (raw lat/lon, static dict, Census fallback, no-match error,
empty-string error), `/points` parsing, `/alerts/active` parsing (incl.
empty-alerts case), `/gridpoints/.../forecast` parsing, alert/forecast
normalization (incl. missing-instruction and missing-properties-id edge
cases), and `sync_location()` end-to-end (ordering, limit clamping, and
the no-gridId edge case not crashing).

---

## Phase 4 — Auto-embed-on-sync, national alerts sync, and the "location isn't embedded" gap

### Bug: newly-synced cities weren't searchable until a manual script ran
Root cause: `/weather/watchlist` (and `/weather/sync`) wrote raw documents
into `weather_documents`, but nothing ever turned them into vectors in
`weather_embeddings` — that only happened when `notebooks/ingest_weather_embeddings.py`
was run by hand in a second terminal. Live-verified via a Seattle search
that silently returned Chicago results.
**Fix:** extracted the chunk→embed→upsert logic (previously only living
inside the ingestion script) into a new shared module, `embed_pipeline.py`
(`build_chunk_rows`, `upsert_chunk_rows`, `embed_documents_now`). Both the
ingestion script and `app.py` now import from it — no duplicate logic.
`app.py` calls `embed_documents_now()` immediately after every sync
(`_embed_now_best_effort()`, best-effort so a slow/flaky embed call
doesn't fail the sync request itself — the batch script still catches
anything missed on its next run). Verified via 78/78 passing tests
including new coverage in `tests/test_embed_pipeline.py` and
`tests/test_app.py`.

### Feature: bulk "sync all active US alerts" (`POST /weather/sync/national-alerts`)
NWS's `/alerts/active` with no location filter returns every currently
active alert nationwide in one call — unlike forecasts, which are
inherently per-gridpoint and have no bulk equivalent. Added
`WeatherClient.get_all_active_alerts()` / `sync_national_alerts()` (uses
each alert's own `areaDesc` as the document's location, e.g.
"Cook County, IL", since a nationwide sync has no single watchlist city to
attribute it to) plus the Flask route and a UI button.

### Bug found live: national alerts sync failing with a bare "400 Client Error"
`resp.raise_for_status()` only reports the HTTP status code, not NWS's
actual reason. Fixed `get_all_active_alerts()` to read NWS's JSON `detail`
field (or raw body) and include it in the raised error, so the next
failure is actually debuggable instead of a dead-end "400 Bad Request."
Root cause not yet confirmed — this sandbox can't reach `api.weather.gov`
directly (network allowlist), so the fix is instrumentation-first:
re-trigger from the real app and read the improved error message.

### Discovered gap (not yet fixed): search has no location awareness
Live-verified: after the auto-embed fix, Seattle's alert documents *were*
in `weather_embeddings` (confirmed via the Recent Alerts panel showing
them), but searching "how is the weather in seattle?" still returned
Chicago forecast results. Root cause is different from the bug above:
`build_chunk_rows()` only embeds a document's `narrative_text` — the
`location` field is stored as separate SQL metadata and never enters the
embedding at all. So a query naming a city has literally nothing to match
against for that city's name; ranking is driven purely by topical content.
Chicago's plain "Mostly sunny, high near 79..." forecast text reads more
generically "weather-like" than Seattle's two alert documents (Air Quality
Alert, Heat Advisory), so it out-scored them for a generic query — and at
60.6% similarity it cleared the `MIN_SIMILARITY` floor, so the app called
it "confident" while being confidently wrong about the city.
**Not yet fixed** — candidate approaches (to decide with a human): (a)
prefix the location into the embedded text so city names become part of
the vector's semantic content, (b) pre-filter by watchlist location when
the query text contains a tracked city name, (c) fold into the planned
LLM-summary feature, having the LLM read retrieved chunks against the
original query and say plainly when nothing retrieved is actually
relevant.

### Fix: bake location + headline into the embedded text (option a, chosen)
`embed_pipeline.build_chunk_rows()` now embeds `"{location} — {headline}: {chunk}"`
instead of the raw chunk alone — e.g. `"Seattle, WA — Air Quality Alert:
Unhealthy air quality expected today."` The STORED `chunk_text` column is
unchanged (still the clean, raw chunk) — only what gets fed into
`embed_texts()` changes, so the UI still shows clean text while the vector
itself now encodes "this is about Seattle." Falls back to embedding the
raw chunk unprefixed when a document has no location/headline (keeps this
backward-compatible rather than crashing on unexpected shapes).

**Important operational note:** this only affects NEWLY embedded chunks.
Existing rows in `weather_embeddings` (Denver, Seattle, Chicago, etc. from
before this fix) were embedded without the location prefix and won't
benefit until they're re-embedded. Added `--all` to
`notebooks/ingest_weather_embeddings.py` (`find_all_documents()`, no
`LEFT JOIN` filter) specifically for this: re-embeds every document
regardless of whether it already has an embedding, overwriting the old
vector via the existing `ON CONFLICT (id) DO UPDATE`. Run once after this
change:
```
python notebooks/ingest_weather_embeddings.py --all
```
Verified via 5 new tests (`test_embed_pipeline.py`,
`test_ingest_weather_embeddings.py`) — 83/83 passing.

**Live-verified** (2026-08-06): ran `--all` against the real Lakebase
instance, restarted the app, searched "how is the weather in seattle??" —
now returns Seattle forecast periods (Saturday/Friday/Saturday Night/...)
at 65.6-68.8% similarity, all correctly attributed to Seattle, WA. Fixed.
Also confirmed the *ranking* itself is sound: at `top_k=5` only Seattle
content appears; widening to `top_k=20` backfills with Denver's forecasts,
but at visibly lower similarity (48-51% vs. Seattle's 54-68%) - exactly
the expected "best matches first, weaker backfill only once you ask for
more than exist" behavior, not a regression.

### Bug found + fixed: national alerts sync 400 - root cause confirmed
Live-diagnosed by asking the human to curl `/alerts/active?limit=200`
directly, bypassing the app entirely. NWS's real response:
```json
{"parameterErrors": [{"parameter": "query.limit", "message": "Query parameter \"limit\" is not recognized"}], "detail": "Bad Request", ...}
```
So `/alerts/active` genuinely does not accept a `limit` query param at
all (unlike `/alerts`, the general endpoint, which supports
`limit`/`cursor` pagination) - not a value-out-of-range issue, not a rate
limit, an outright unsupported parameter. This is exactly why the earlier
"surface NWS's error detail" fix mattered: the bare "400 Client Error"
gave no way to distinguish this from a dozen other possible causes, but
`parameterErrors` pinpointed it immediately.
**Fix:** `get_all_active_alerts()` no longer sends `limit` to NWS at all -
fetches every active alert nationwide (no server-side pagination on this
endpoint) and slices the returned list client-side (`features[:limit]`)
to still honor our own `limit` parameter/API contract. Also improved the
error-detail extraction to include `parameterErrors` when present, not
just `detail` (which was too generic - literally "Bad Request" - to be
useful on its own here). Verified via 3 new tests (no `limit` sent,
client-side slicing, error message includes `parameterErrors`) - 86/86
passing.

**Live-verified** (2026-08-06): re-clicked "Sync all active US alerts" -
now succeeds ("Synced 200 alert document(s) nationwide") instead of
erroring. Fixed.

### Bug found live: bulk sync flooded the Recent Alerts feed
Immediately after the national sync succeeded, the Recent Alerts sidebar
- meant to be a short, glanceable "what's new" feed - filled with all 200
just-synced alerts at once, since `GET /weather/alerts/recent` had no
row limit, just a time-window filter (`synced_at >= now() - N minutes`).
A sync that makes 200 documents "recent" simultaneously will always blow
past what a sidebar feed should show.
**Fix:** added a `limit` query param (default 20, clamped [1, 100],
mirroring the existing `minutes` param's shape) and a SQL `LIMIT %s`
clause. The UI doesn't need to change - it doesn't pass `limit` today, so
it now gets the sane default automatically. Verified via 4 new tests
(custom limit, invalid limit, clamped above/below) - 90/90 passing.

---

## Phase 5 — Product scope tightening: tracked-only feed, alert lifecycle

Prompted by a live product discussion after the national-alerts feature
landed: the bulk sync quietly turned "Recent Alerts" from "what's new for
cities I track" into "what's new anywhere in the US," and the human
correctly flagged this as scope drift rather than a bug to just patch
around. Two decisions made together:

### Decision: Recent Alerts defaults to scope=tracked, not everything
`GET /weather/alerts/recent` now takes `scope=tracked|all` (default
`tracked`) and `offset` (for pagination), on top of the existing `minutes`
and `limit`. `tracked` adds `AND location IN (SELECT location FROM
weather_watchlist)`. Default `limit` dropped from 20 to 10 to match "10
at a time" - the UI's "Show all US alerts" toggle switches scope to
`all` and reveals a "Load more" button (paginates via `offset`, 10 per
click) instead of ever rendering all ~200 nationwide alerts in one shot.
Switching scope resets pagination and clears the "new" highlight
tracking (`seenAlertIds`) so switching views doesn't misleadingly
pulse-highlight everything as new.

### Decision: expired alerts get deleted automatically, not archived forever
Added `cleanup_expired_alerts()` - `DELETE FROM weather_documents WHERE
source_type = 'alert' AND effective_at < now()` (embeddings cascade-delete
automatically via the existing FK). Runs as a best-effort step
(`_cleanup_expired_alerts_best_effort`, same non-fatal shape as the
inline-embed step) right after every sync - per-city, watchlist add, and
national - since there's no scheduler/cron infra yet (that's task #6,
still pending) to run it on its own timer. Scoped to `source_type =
'alert'` only: forecast periods' `effective_at` is their *start* time,
not an expiration, so applying this predicate to them would delete
current, not-yet-happened forecasts.

### Clarification (not a bug): per-city vs. national alert labeling
The human questioned whether earlier Chicago/Denver/Seattle alert entries
were "bad" given national-sync alerts use NWS's own county-level
`areaDesc` as their location label. They're not bad - just a different
granularity. Per-city sync queries NWS with `point={lat},{lon}`; NWS
resolves server-side which alerts genuinely cover that point, and we
label the result with the tracked city name (more useful to the user than
NWS's raw zone/county name). National sync has no city to attribute to,
so it uses NWS's own `areaDesc` instead. Same underlying accuracy, two
label schemes for two different jobs (personal watchlist vs.
browse-everything).

Verified via 10 new tests (`test_app.py`: scope filtering, offset
handling, cleanup SQL shape, cleanup wired into both sync paths, cleanup
failure doesn't fail the request) - 100/100 passing. Not yet
live-verified (pending the human restarting the app and checking the
toggle/pagination/cleanup behavior against real Lakebase).

---

## Phase 6 — Course correction: cut national alerts sync entirely

After Phase 5 shipped, the human pushed back directly: "I am losing faith
in these two features [watchlist + national sync]... it feels like an
overhead at this point." Worth taking at face value rather than patching
around it again - the pattern across Phases 3-5 (Seattle mislabeling, the
`limit` 400, the sidebar flood, the scope-drift discussion) was five
consecutive rounds of firefighting on a feature that was never actually
part of the assignment. The homework's three required deliverables
(harvest -> vectorize -> search API) have been stable since they were
built; watchlist/UI/national-sync were self-added scope chasing "stretch
on all the points."

**Decision: cut national alerts sync entirely, keep the watchlist.**
The watchlist itself has been stable (no bugs since the cascade-delete
decision) and is genuinely useful as the input to the still-pending
scheduled sync job (task #6). National sync was the actual source of
every recent bug and had no tie-in to the watchlist at all (orphaned
data, its own cleanup story, its own labeling scheme) - it didn't serve
the core "search what I'm tracking" product, it competed with it.

Removed:
- `WeatherClient.get_all_active_alerts()` / `sync_national_alerts()`
  (weather_client.py)
- `POST /weather/sync/national-alerts` route (app.py)
- The `scope`/`offset` params and watchlist-membership SQL filter on
  `GET /weather/alerts/recent` - with national sync gone, every alert in
  the corpus is watchlist-tied by construction, so the distinction is
  moot. Reverted to the simple `minutes`/`limit` shape (limit default
  back to 20).
- "Sync all active US alerts" button, scope toggle, "Load more"
  pagination, and their JS (templates/index.html) - reverted to the
  original simple polling feed.
- All associated tests (weather_client + app).

Kept: `cleanup_expired_alerts()` (still valuable independent of national
sync - watchlist alerts go stale too) and its best-effort wiring into
`_sync_one_location`.

Verified via 85/85 passing tests (down from 100 - the removed count is
expected and correct, not a coverage regression, since the tested surface
area itself shrank). Not yet live-verified (pending the human restarting
the app and confirming the sidebar/watchlist still work as the simpler
version).

**Where effort goes next:** the assignment's actual stretch goals - a
scheduled sync job (task #6, syncing watchlist cities on a timer instead
of only on manual add/search) and an LLM-generated summary over retrieved
chunks via Databricks Foundation Model APIs, plus an HNSW benchmark
(task #7).

---

## Phase 7 — The actual homework stretch goals: scheduled sync, LLM summary, HNSW benchmark

### Feature: LLM-generated summary over search results (`llm_summary.py`)
The most-requested feature this session - after watching search
confidently return the wrong city (the Seattle/Chicago saga), the human
explicitly asked for an LLM to read the retrieved text and answer
honestly rather than leaving raw similarity-ranked cards to be
interpreted by eye.

Uses Databricks Foundation Model APIs (`WorkspaceClient().serving_endpoints.query()`)
over calling OpenAI/Anthropic directly - reuses the already-authenticated
`WorkspaceClient` (same auth path as `lakebase.py`), no new secret to
manage. `WorkspaceClient()` is instantiated INSIDE `summarize()`, not at
module import time (unlike `lakebase.py`) - so importing `llm_summary.py`
doesn't require live credentials, only calling it does.

The `low_confidence` flag (already computed by `/weather/search` via
`MIN_SIMILARITY`) is passed into the prompt explicitly rather than left
for the model to infer - when nothing scored as a strong match, the model
is told so directly, so it says "nothing relevant tracked" instead of
stretching a weak match into a confident-sounding answer. The system
prompt also explicitly forbids inventing weather conditions not present
in the retrieved text - same honesty principle as the low_confidence
flag itself, just applied at the generation step instead of the
retrieval step.

Wired into `POST /weather/search` as an opt-in `"summarize": true` body
flag (default false) - keeps the base search contract and its existing
tests unchanged, and no caller pays LLM latency/cost unless they ask for
it. Best-effort: a failed/misconfigured FM endpoint returns
`summary_error` instead of failing the whole search request (same
best-effort shape as the embed/cleanup steps elsewhere). The UI's
search box already had a summary display scaffolded in from earlier -
just needed `summarize: true` added to its request body and a
`summary_error` display path added.

**Model endpoint name is a placeholder** (`databricks-meta-llama-3-3-70b-instruct`,
overridable via `WEATHER_FM_ENDPOINT` env var) - Databricks periodically
changes which pay-per-token Foundation Model endpoints are offered per
workspace/region. Verify via `databricks serving-endpoints list` or the
Serving UI before relying on this in your own workspace.

Verified via 8 new tests (`test_llm_summary.py`: context formatting,
endpoint call shape, low-confidence note inclusion/omission, system
prompt honesty guard) + 3 new tests (`test_app.py`: opt-in flag,
best-effort failure handling) - 96/96 passing at that point. NOT yet
live-verified against a real Foundation Model endpoint (needs the human
to confirm `WEATHER_FM_ENDPOINT` matches something actually available in
their workspace, then test live from the UI).

### Feature: scheduled watchlist re-sync (`notebooks/sync_watchlist_job.py` + `resources/sync_weather_job.json`)
Closes the gap where the corpus only refreshes when a human manually adds
a city or hits sync - the job reads every location off `weather_watchlist`
and re-syncs + re-embeds each one, reusing `app.py`'s already-tested
`_sync_one_location()` (harvest -> upsert -> embed -> expired-alert
cleanup) rather than duplicating that logic.

**Known wart, deliberately not fixed here:** this imports a
"private" (underscore-prefixed) function directly from `app.py`, which is
unusual. The clean fix is extracting a shared `sync_pipeline` module both
`app.py` and this script import from (matching the `embed_pipeline.py`
precedent) - deferred to the upcoming codebase cleanup pass specifically
so this feature's diff stays focused and low-risk, per the "tighten up
the homework first" instruction. Noted here so it isn't forgotten.

`resources/sync_weather_job.json` is a Databricks Jobs API job
definition (hourly cron, `spark_python_task` running the script above) -
**not yet deployed or live-verified**, since this sandbox has no
Databricks CLI/credentials. `node_type_id` is a placeholder the human
needs to replace with something available in their workspace (via
`databricks clusters list-node-types`). Deploy via:
```
databricks jobs create --json @resources/sync_weather_job.json
```
then verify once manually with `databricks jobs run-now` before trusting
the schedule.

Verified via 6 new tests (`test_sync_watchlist_job.py`: watchlist query
shape, per-city orchestration, partial-failure handling, limit
pass-through) - 102/102 passing at that point.

### Feature: HNSW benchmark (`notebooks/hnsw_benchmark.py`)
Drops the `idx_weather_embeddings_embedding` HNSW index, times N sample
cosine-similarity queries (sequential scan), recreates the index, times N
more (index scan), reports mean/median/min/max for both and the
difference.

**Documented honestly, not just built to "prove HNSW wins":** HNSW's
advantage grows with row count and dimensionality; on this project's
small V1 corpus (tens to low hundreds of documents), a sequential scan
can be just as fast or faster once index overhead is accounted for -
that's a genuine, well-known property of ANN indexes, not a bug. The
script's docstring says this explicitly so the benchmark's real output is
interpreted correctly rather than assumed to always favor the index.

Verified via 5 new tests (`test_hnsw_benchmark.py`: drop-before-time
ordering, index recreated with correct SQL, requested query count
honored, row count reporting) - 107/107 passing. Not yet live-run (needs
real Lakebase - can't produce meaningful timing numbers against a mocked
DB).

---

## Phase 8 — Fixing the "too many test files" bug

After Phase 7 landed, the human asked to tighten up the working folder,
specifically citing "too many test files." Investigated rather than
guessed at what that meant - ran `pytest` (no path argument) from the
project root instead of `pytest tests/`, which is what a grader or
teammate unfamiliar with this repo's layout would naturally try first.

**Found a real bug, not just a feeling:** `test_connection.py` and
`test_embedding_model.py` sit in the project ROOT (not `tests/`) and are
plain manual smoke scripts requiring live Databricks credentials / a real
model download - not pytest tests at all, just named like ones. Because
pytest's default discovery matches any `test_*.py` file it finds,
running bare `pytest` from root crashed immediately trying to collect
both:
```
ERROR test_connection.py - ValueError: default auth: cannot configure default...
ERROR test_embedding_model.py - ModuleNotFoundError: No module named 'sentence_transformers'
```
This confirms the "too many test files" feeling had a concrete, fixable
cause - not just too much code, but a genuine naming collision.

**Fix:** renamed both to `verify_connection.py` / `verify_embedding_model.py`
(content unchanged) - `verify_` doesn't match pytest's `test_*`/`*_test`
discovery pattern, so they're never collected regardless of what
directory `pytest` is invoked from. Updated the two references in
README_WEATHER.md. Old files deleted via `allow_cowork_file_delete`
(required since files in the user's selected folder can't be removed
without explicit permission).

**Deliberately NOT done** (per the human's explicit choice - offered
three options, picked the minimal one): did not consolidate the other
root-level scripts (`setup_secrets.py`, `create_tables.py`,
`peek_documents.py`, `peek_embeddings.py`) into a `scripts/` folder, and
did not refresh the rest of `README_WEATHER.md` (still doesn't document
watchlist/UI/LLM-summary/scheduled-job/HNSW-benchmark) - both remain
legitimate future cleanup candidates if revisited.

Verified: `pytest` (no arguments, run from repo root) now collects and
passes all 107 tests with zero collection errors - the exact broken
command is now the fix's own regression test.

---

## Phase 9 — Removing the watchlist entirely; feed becomes an unscoped news feed

### Bug found live: the Phase 6 "watchlist-tied by construction" assumption was wrong for existing data
After a restart, the human reported the Recent Alerts sidebar showing
alerts from Trinity/Gulf-of-America/Cape-Mendocino/Green-Bay/etc. -
exactly the county/region-labeled clutter from the national-sync
experiment that was supposedly removed in Phase 6. Root cause: Phase 6
removed the CODE that creates new national-sync documents, and (wrongly)
concluded the tracked-cities filter on Recent Alerts was now redundant
since "every alert in the corpus is watchlist-tied by construction" -
true only for alerts synced AFTER the Phase 6 rollback, not the rows
that already existed in the database from before it. Data survives code
changes; that assumption didn't account for it. Caught live, not
theoretically - a direct consequence of this project's "verify against
real data, not just tests" discipline.

### Decision: remove the watchlist/"Cities Tracked" concept entirely, not just clean up its data
The human's read, after seeing this: "i dont want to have that track
city feature at all." Given the accumulated pattern across Phases 3-9
(Seattle mislabeling, the `limit` 400, the sidebar flood, the scope-drift
discussion, now stale orphaned data resurfacing) - the watchlist
abstraction kept generating edge cases disproportionate to what it added
over the assignment's actual primitive: `POST /weather/sync` already does
everything the watchlist did (harvest -> upsert -> embed a list of
cities), just without a persistent "which cities am I tracking" table
and its own CRUD surface to keep consistent.

**Removed entirely:**
- `weather_watchlist` table usage, `ensure_weather_watchlist_table()`
- `GET`/`POST`/`DELETE /weather/watchlist` routes
- "Cities Tracked" UI panel (add/remove city input, list, remove buttons)
- `notebooks/sync_watchlist_job.py` (read locations from the watchlist table)

**What replaced it:** `POST /weather/sync` (already existed, already
exactly what the homework spec asked for) is now the only way data
enters the corpus - via a manual call, a script, or the scheduled job.
This is a deliberate UX tradeoff: the UI can no longer add a new city by
itself. Confirmed explicitly with the human before removing the feature
rather than assumed.

### Decision: news feed becomes unscoped, shows alerts AND forecasts, capped at 10
The human's own words: "the side pane should be just a news feed of
pulled forecasts / alerts... only top 5-10 alerts that is it." Rebuilt
`GET /weather/alerts/recent` as `GET /weather/feed/recent`:
- No location/watchlist filter (no watchlist to filter by anymore)
- No `source_type = 'alert'` filter - shows forecasts too now (the old
  endpoint was alert-only, which no longer matches "pulled
  forecasts/alerts" both)
- `MAX_RECENT_LIMIT` dropped from 100 to 10 - a glanceable feed, not a
  paginated archive (matches "that is it" - deliberately no "load more")
- No time-window (`minutes`) filter anymore either - simplified to "top N
  most recently synced, full stop," since a small corpus with an active
  time window could otherwise go empty even when perfectly good recent
  content exists just outside an arbitrary window
- Added `last_synced_at` to the response (the first/newest row's
  `synced_at`, since results are already `ORDER BY synced_at DESC`) so
  the UI can show "last synced HH:MM" reflecting actual data, not just
  "when the browser polled" - directly requested ("show the last synced
  time (based on our scheduled job)").
- UI badge now switches between the alert/forecast styles (the
  `.badge.forecast` CSS class existed from early UI work but was never
  actually reachable until now, since the old feed was alert-only).

### Feature: `reset_corpus.py` - one-time DB cleanup
Wipes `weather_documents` (`TRUNCATE ... CASCADE`, taking
`weather_embeddings` with it) and `weather_watchlist`. Dry-run by default
(prints row counts, changes nothing) - requires `--yes` to actually
execute, since this is destructive and touches the human's real Lakebase
data. Named `reset_` (not `test_`) per the Phase 8 naming convention.
Cannot be run from this sandbox (no live Lakebase access) - the human
needs to run it themselves, then re-sync a deliberate set of cities via
`POST /weather/sync` or `notebooks/sync_weather_job.py`.

### Feature: `notebooks/sync_weather_job.py` (replaces sync_watchlist_job.py)
Same shape as the file it replaces - reuses `app.py`'s tested
`_sync_one_location` - but syncs a fixed, configured location list
(`app.py`'s `DEFAULT_LOCATIONS` / `WEATHER_DEFAULT_LOCATIONS` env var by
default, or `--locations "City, ST;City2, ST2"` for a one-off override)
instead of reading a watchlist table that no longer exists. Same "known
wart" caveat as before: imports a private function from `app.py` rather
than a shared pipeline module - still deferred.
`resources/sync_weather_job.json` updated to reference the renamed
script.

Verified via test rewrites: `test_sync_weather_job.py` (replaces
`test_sync_watchlist_job.py` - fixed-location-list orchestration,
per-location failure handling, limit pass-through), `test_app.py`'s
watchlist tests replaced with `/weather/feed/recent` tests (unscoped,
alerts+forecasts, capped at 10, `last_synced_at` shape) and the
cleanup/embed best-effort tests re-pointed at `POST /weather/sync`
instead of the now-removed `/weather/watchlist` - 100/100 passing.

Not yet live-verified: the human needs to (1) run `reset_corpus.py --yes`
against real Lakebase, (2) re-sync a fresh set of cities, (3) restart the
app and confirm the News Feed panel shows a clean, capped, alerts+forecasts
feed with an accurate "last synced" time.

---

## Phase 10 — Re-grounding in the actual assignment text

After Phase 9, the human asked to stop specifying explicit cities entirely
("i just wanna pull from API and get answers"). Rather than design another
tradeoff in isolation, the human said: "i think we are going a little
wrong. Please deeply read the homework instructions." Correct call - the
last several phases (watchlist, national sync, News Feed, this
no-locations question) were all product decisions made without
re-checking them against the literal spec text.

### Finding: re-read the verbatim assignment - the UI was never a deliverable
The five required deliverables are `weather_client.py`, `app.py` with
`POST /weather/sync` + `POST /weather/search`, Lakebase DDL, a psycopg2
ingestion script, and `README_WEATHER.md`. No UI is mentioned anywhere.
`POST /weather/sync`'s own spec'd example (`{"locations": ["Chicago, IL",
"Austin, TX"], "limit": 50}`) already answers the "don't want to track
cities" question: locations are supplied per-call by the caller, not
persisted/managed by the app - there was never a contradiction to resolve.
The watchlist/Cities-Tracked/News-Feed churn across Phases 3-9 was scope I
added on top of the assignment, not part of it - the real source of the
"losing faith in these features" fatigue.

**Decision: keep the UI (human's call - "unable to understand what is the
harm with a nice basic UI"), but stop treating it as something to keep
redesigning.** It stays as a thin reflection of the actual API contract
(search + sync trigger), not a product with its own state. Priority
shifts to proving the five actual deliverables work, live, before
touching UI/stretch polish further.

### Verification: Parts 1-3 end-to-end against real NWS + real Lakebase (post-reset)
After the human's `reset_corpus.py --yes` wiped the corpus clean:
```
curl -X POST /weather/sync -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'
  -> {"synced": 28, "locations": ["Chicago, IL", "Austin, TX"]}
curl -X POST /weather/search -d '{"query": "flash flood risk this weekend", "top_k": 5}'
  -> low_confidence: true, top similarity 0.36 (Chicago forecast text only)
curl -X POST /weather/search -d '{"query": "chance of rain and thunderstorms this week", "top_k": 3}'
  -> low_confidence: false, similarities 0.675/0.674/0.673 (Chicago forecast text)
```
The first query's low_confidence was correct, not a bug - neither city had
an active flood alert at sync time, so nothing in the corpus was
genuinely about flooding, and 0.36 correctly reflects "closest available,
not actually relevant." The second query, run against the same corpus,
confirms the ranking itself is sound: a query matching what's actually
present scores 0.67+ and clears the confidence floor cleanly. This is the
clean control/experiment pair needed to trust the number, not just one
data point in isolation. **Parts 1-3 (harvest -> vectorize -> retrieve)
are proven live end-to-end, not just via mocked tests.**

### Verification: LLM summary against a real Foundation Model endpoint
`databricks serving-endpoints list` confirmed `databricks-meta-llama-3-3-70b-instruct`
(the existing placeholder) is genuinely deployed and `READY` in the human's
workspace - no code change needed. Live call: `curl .../weather/search -d
'{"query": "chance of rain and thunderstorms this week", "top_k": 3,
"summarize": true}'` returned a real generated summary, correctly grounded
in only the 3 retrieved chunks (explicitly noted it couldn't speak to the
rest of the week beyond what was retrieved) - confirms the honesty-guard
system prompt is working as designed, not just returning plausible-sounding
text. **Stretch goal #1 (LLM summary) done and live-verified.**

### Decision: `node_type_id` = m5.large
`databricks clusters list-node-types` returned dozens of options; picked
the smallest General Purpose instance (2 cores, 8GB) since this job is
just HTTP calls + small DB writes, no heavy compute. Filled into
`resources/sync_weather_job.json`.

### Decision: root-level utility scripts moved into `scripts/`
Raised again by the human while prepping to push to GitHub/Databricks
Repos - same complaint as Phase 8 ("too many test files"), but this time
traced to a more precise cause: the ROOT folder mixes real app modules
(`app.py`, `lakebase.py`, `weather_client.py`, etc.) with one-off manual
dev scripts (`create_tables.py`, `setup_secrets.py`, `peek_documents.py`,
`peek_embeddings.py`, `reset_corpus.py`, `verify_connection.py`,
`verify_embedding_model.py`) - 7 of 14 root `.py` files were dev
utilities, not app code. `tests/` itself was checked again and found
correctly sized (8 files, one per source module, all properly under
`tests/`) - explicitly did NOT touch it, since removing any of those
would mean losing real coverage right before submission for no actual
benefit.

**Fix:** moved the 7 utility scripts into `scripts/`, added the same
`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` pattern
`notebooks/*.py` already used (so `import lakebase` / `from embedding
import ...` still resolves when run as `python scripts/whatever.py` from
repo root) - no new pattern invented, just applied the one already
established. Updated `README_WEATHER.md`'s file map and "how to run"
commands accordingly. 100/100 tests still passing (this only touched
manual scripts, none of which the automated suite imports), and all 7
moved files verified to still parse/import cleanly.

### Remaining before this is fully closed out
- Stretch: scheduled job - `node_type_id` filled in, not yet deployed
  (repo isn't in GitHub/Databricks Repos yet - that's the current
  blocker, see below).
- Stretch: HNSW benchmark - built (`notebooks/hnsw_benchmark.py`), not yet
  run against live data (needs real row counts/timings, meaningless
  against a mock).
- ~~Repo needs to exist on GitHub~~ - DONE. Pushed to
  `github.com/sri-25/Labkebase-Weather-APP` (main branch, 2 commits) from
  the human's own terminal (this sandbox has no `gh` CLI/GitHub
  credentials, so `git init` + local commits happened here, `git push`
  happened on their machine). Also added a root `README.md` (GitHub only
  auto-renders a file named exactly `README.md` on the repo landing page -
  `README_WEATHER.md` alone wouldn't show up there, even though it
  satisfies the homework spec's own deliverable wording).

### Learning: Databricks Repos land under `/Workspace/Users/<email>/`, not `/Workspace/Repos/`
Same "provisioning model changed since the reference docs were written"
pattern as the Lakebase `postgres` CLI note in Phase 1 - added via
Workspace -> Repos -> Add Repo, the human's repo landed at
`/Workspace/Users/srijan2554@gmail.com/Labkebase-Weather-APP`, not the
older `/Workspace/Repos/<username>/<repo>` convention this file's
placeholder assumed. Updated `resources/sync_weather_job.json`'s
`python_file` path to match the real location instead of guessing.

### Bug found live: `databricks jobs create` rejected the classic-cluster job spec
`Error: Only serverless compute is supported in the workspace.` - not a
typo or bad value, a genuine workspace-level constraint: this workspace
doesn't offer classic all-purpose/job clusters at all, only serverless.
The `node_type_id` decision above (m5.large) was correct reasoning for a
classic cluster but moot here - there's no cluster to size.

**Fix:** rewrote `resources/sync_weather_job.json` for serverless job
compute - removed `new_cluster` and `libraries` (classic-cluster-only
concepts) entirely. Serverless tasks instead reference an
`environment_key` pointing at a top-level `environments` entry (`spec.client:
"1"`, `spec.dependencies: [...]` - same five pip packages as before, just
declared differently). No cluster sizing decision needed at all for
serverless - one less thing to get wrong. Validated as syntactically
correct JSON; not yet confirmed to deploy (pending the human re-running
`databricks jobs create`).

### Bug found live: `NameError: name '__file__' is not defined` on the deployed job
After fixing the environment version, the job got further (environment
installed successfully, per the driver logs) but the task itself failed.
`databricks jobs get-run-output <task_run_id>` gave the real traceback
(the parent run's generic "Workload failed, see run output for details"
message was useless on its own - had to drill into the task-level run_id,
not the job-run id, to get this):
```
NameError: name '__file__' is not defined
  File "/Workspace/Users/.../notebooks/sync_weather_job.py", line 42
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```
Root cause: Databricks' `spark_python_task` runner executes the script via
`exec(compile(f.read(), filename, 'exec'))` rather than a normal `python
file.py` process invocation - so `__file__` is simply never defined in
that execution context. This is invisible when testing locally
(`python notebooks/sync_weather_job.py` always has `__file__` set, same
for pytest) - only surfaces when actually run as a Databricks Job, which
is exactly why this had to be caught live rather than by any amount of
local testing.

**Fix:** wrapped the `Path(__file__)` lookup in try/except NameError,
falling back to `Path(sys.argv[0])` - `spark_python_task` sets `sys.argv[0]`
to the `python_file` path from the job spec, so this resolves correctly in
both contexts. Applied to all three `notebooks/*.py` scripts that use this
pattern (`sync_weather_job.py`, `ingest_weather_embeddings.py`,
`hnsw_benchmark.py` - the ones that could plausibly run as a Databricks
Job task); left `scripts/*.py` alone since those are local-only manual
utilities where `__file__` is always defined. 100/100 tests still passing.

### Bug found live: `ModuleNotFoundError: No module named 'flask'`
Got past the `__file__` fix and the actual import chain, then died on
`from app import ... ` -> `app.py`'s own `from flask import Flask, ...`.
Root cause: `sync_weather_job.py` imports from `app.py` to reuse
`_sync_one_location` (the documented "known wart" - see Phase 7), and
`app.py` is the Flask app file, so it imports Flask at module level even
though the job itself never touches Flask/HTTP anything. `flask` was
simply never added to `resources/sync_weather_job.json`'s dependency list,
since nobody was thinking of the job as "needing a web framework."
**Fix:** added `flask` to the `environments[].spec.dependencies` list.
Cross-checked every other import across `app.py` and everything it
transitively pulls in (`lakebase`, `embed_pipeline`, `embedding`,
`llm_summary`, `weather_client`) against the dependency list to catch any
other gaps in one pass rather than one-at-a-time - confirmed
`sentence-transformers` (lazily imported inside `embedding.get_model()`,
so it wouldn't show up in a naive top-of-file import grep) and everything
else was already covered.

### Bug found live: `Invalid platform channel Client-1` - serverless environment version too old for this workspace
Deployed the serverless rewrite above (`spec.client: "1"`), deleted+recreated
the job, ran it - failed immediately at cluster launch, before any Python
even ran: `Cannot launch the cluster. Cause: Invalid platform channel
Client-1. Workspace doesn't support Client-1 channel for REPL.
INVALID_PARAMETER_VALUE.` Root cause: serverless environment "client"
versions are Databricks-side release channels, not something this project
controls the meaning of - version `"1"` has been deprecated in this
workspace. Researched via Databricks' serverless release notes
(`docs.databricks.com/aws/en/release-notes/serverless/`) and the serverless
dependencies doc - version `"5"` is the current channel (available since
Feb 2026).
**Fix:** bumped `resources/sync_weather_job.json`'s `environments[].spec.client`
from `"1"` to `"5"`. Job deleted+recreated again (environment spec changes
aren't picked up by a plain re-run), ran again - got past cluster launch
this time and into the `__file__` bug documented above.

### Bug found live: `psycopg2-binary` crashes with SIGABRT on import under Databricks serverless
Got past the flask fix, further than any previous run - then died with
`exit code 134` (SIGABRT) partway through `import psycopg2`, before any of
this project's own code ran. `databricks jobs get-run-output` on the
task-level run_id (not the parent job-run id - see the diagnostic pattern
established above) gave the actual crash trace: abort inside
`psycopg2/__init__.py:51`, triggered by `lakebase.py:20`'s `import psycopg2`,
itself triggered by `sync_weather_job.py`'s `from app import ...` (`app.py`
imports `lakebase` at module level).

Root-caused as a genuine platform-level conflict, not an app bug: `psycopg2-binary`'s
wheel bundles its own OpenSSL; the Databricks serverless Python 3.12 process
already has `grpc`'s own bundled OpenSSL loaded before any application code
runs; the two collide at the C-extension level and abort the process. This
is a different failure than the one already documented and fixed in Phase
0/1.5 (having both `psycopg2` and `psycopg2-binary` installed
simultaneously) - confirmed by testing a completely clean, single-package
`psycopg2-binary` install, which still crashed identically. Cross-referenced
against a similar unresolved GitHub issue (`docling-project/docling#3201`)
showing the same SIGABRT class of crash for a different native-extension
library on the identical Databricks Standard v5 / Python 3.12 serverless
runtime - this looks like a genuine platform-level gap, not something
specific to this project's code.

**A competing diagnosis was raised and rejected:** another tool suggested the
real cause was `app.py` eagerly loading the sentence-transformers/torch
embedding model at import time (`get_model()`, lines 40-42), causing an
OOM that only coincidentally surfaced during the psycopg2 import. Checked
against the actual trace and ruled out directly: the crash occurs at
`app.py:26` (`import lakebase`), which executes before line 40-42 in
top-to-bottom module execution - torch/sentence_transformers are never even
reached. The crash dump's "Extension modules" list also didn't include
torch or sentence_transformers, confirming they were never loaded. Not
applied.

**Fix: migrated from `psycopg2-binary` to `psycopg[binary]` (psycopg3).**
psycopg3's binary wheel doesn't bundle OpenSSL the same conflicting way, and
this project's `%s`-style SQL parameter placeholders needed no changes - a
driver swap, not a query rewrite. Changes:
- `lakebase.py` - `psycopg.connect(url, row_factory=psycopg.rows.dict_row)`
  instead of `psycopg2.connect(url, cursor_factory=RealDictCursor)`;
  `get_engine()` now rewrites the URL to `postgresql+psycopg://` so
  SQLAlchemy picks the psycopg3 dialect instead of defaulting to the
  no-longer-installed psycopg2 one.
- `embed_pipeline.py` - psycopg3 has no `execute_values()` equivalent
  (that was a psycopg2-only extra), so `upsert_chunk_rows()` now hand-builds
  one `INSERT ... VALUES (%s,...,%s::vector), (%s,...,%s::vector), ...` with
  a flattened parameter list instead.
- `requirements.txt` / `resources/sync_weather_job.json` - `psycopg2-binary` ->
  `psycopg[binary]`.
- `tests/test_embed_pipeline.py` - rewrote the upsert test to assert on the
  single hand-built multi-row `execute()` call instead of mocking
  `execute_values`.
- `README.md` / `README_WEATHER.md` / notebook docstrings - updated stale
  `psycopg2` references; `README_WEATHER.md`'s driver section now honestly
  documents the full pg8000 -> psycopg2 -> psycopg3 history and why each
  switch happened, instead of just naming the current driver.

Verified locally: `psycopg[binary]` installs and imports cleanly
(`psycopg 3.3.4`), both rewritten modules parse, full suite 100/100 passing.
**Not yet redeployed or live-tested against Databricks** - that's the next
step.

### Verification: psycopg3 fix confirmed live - scheduled job runs clean end to end
Pushed, pulled into the Databricks Repo, job deleted+recreated with the
updated JSON, run once more. Full run output: `import lakebase` (psycopg3)
succeeded silently, the embedding model loaded normally
(`sentence-transformers/all-MiniLM-L6-v2` downloaded from HF, no crash),
and the sync itself completed cleanly:
```
Syncing 3 location(s): ['Chicago, IL', 'Austin, TX', 'Miami, FL']
  Chicago, IL: 14 document(s)
  Austin, TX: 14 document(s)
  Miami, FL: 15 document(s)
Done. Synced 43 document(s) across 3 location(s).
```
No traceback, no SIGABRT, no `error`/`error_trace` in the run output - the
exact shape a successful run should have. **SIGABRT bug fully resolved;
scheduled job (stretch goal) confirmed working end to end on real
Databricks serverless infra, cron-scheduled hourly going forward.**

---

## Phase 11 — UI overhaul: stat strip, sync-from-UI, distinct visual identity

The human shared two reference-app screenshots for inspiration (neither was
this project's own UI - confirmed by re-reading the actual
`templates/index.html`, which turned out to be much simpler than either
reference: no stat cards, no sync-from-UI panel, just a search box and a
feed list) and asked for a UI that's "different, very thoughtful, well
designed, clean and sleek" - inspired by, not copied from, the references.

### Decision: reintroduce a sync trigger in the UI, but stay stateless
Both references prominently featured a "sync weather data" panel with
per-city toggles. Phase 9 deliberately removed the *watchlist* (a
persisted "tracked cities" table + its own CRUD surface) because it kept
generating edge cases disproportionate to its value. Bringing back a
sync panel doesn't reverse that decision - the new panel is just a nicer
front end over the existing `POST /weather/sync` call (client-side chip
toggles + a free-text "add a city" field, no new table, no persisted
state). Functionally identical to running the equivalent curl command by
hand; only the ergonomics changed.

### Feature: `GET /weather/stats`
New read-only endpoint - three cheap `COUNT(*)`/`COUNT(DISTINCT ...)`
queries (documents, embeddings, distinct locations) plus the embedding
model name/dimension - powers the UI's stat strip. Verified via 2 new
tests (`test_stats_returns_counts_and_model_info`,
`test_stats_ensures_tables_before_querying`) - 102/102 passing.

### Fix (caught before it shipped): don't guess source_type from headline text
First draft of the new result cards inferred an alert-vs-forecast badge
from a regex against the headline (`/advisory|warning|watch|alert/i`) -
`POST /weather/search`'s response never actually included `source_type`,
so this would have been a fabricated-looking signal, the exact thing this
project has deliberately avoided elsewhere (the `MIN_SIMILARITY`
low-confidence floor, the LLM summary's honesty guard). Caught it on
review rather than shipping a guess dressed up as data.
**Fix:** added `d.source_type` to the search SQL's `SELECT` and to the
response dict; the badge now reads the real field. Updated the 4 affected
tests that mock `run_query` return rows accordingly. Still 102/102 after
the fix.

### Rewrite: `templates/index.html`
Full rewrite, still a single self-contained file (inline CSS/JS, no build
step - same convention as before). Distinct dark visual identity (not a
copy of either reference): near-black background with a subtle two-tone
sky-blue/indigo radial glow, glass-panel cards, a signature gradient used
consistently for primary actions/accents/similarity bars, inline SVG
icons (no external icon font dependency - keeps the file offline-safe,
relevant since it may run inside a sandboxed Databricks Apps environment
with restricted egress). Structure: stat strip (documents / embeddings /
locations / vector model, animated count-up on load) -> two-column sync +
search panels -> a horizontally-scrolling "live feed" strip (replaces the
old fixed sidebar) -> a one-line factual footer. Search results now show
a real similarity bar (not just a number) and the corrected source_type
badge; sync results show a per-location success/fail breakdown.
HTML tag balance and JS syntax verified locally (Python `html.parser` +
Node `Function()` parse check - can't fully exercise fetch()-driven
behavior without live Lakebase, same limitation as always). Not yet
seen running against real data - next step is the human running
`python app.py` locally and syncing/searching for real.

### Remaining
- Stretch: HNSW benchmark - built (`notebooks/hnsw_benchmark.py`), not yet
  run against live data.
- New UI not yet live-verified against real Lakebase data (built and
  locally sanity-checked only).

---

## Phase 12 — Deploying as a Databricks App

Researched Databricks Apps (a separate feature from the scheduled Job -
this hosts `app.py` itself persistently with a real URL, vs. the Job
which just runs `sync_weather_job.py` on a timer with no HTTP surface).
Read the current (2026) `deploy`, `app-runtime`, `system-env`, and
`resources` docs directly rather than relying on training knowledge,
since this is a fast-evolving platform feature.

### Decision: `app.yaml` = just `command: ['python', 'app.py']`
Databricks Apps auto-detects Flask (via `requirements.txt`) and injects
`FLASK_RUN_PORT` / `FLASK_RUN_HOST=0.0.0.0` into the runtime automatically
- and `app.py`'s existing `__main__` block already reads exactly those two
env vars (with the same defaults used for local runs), so no `env:`
section is needed in `app.yaml` at all. Deliberately did NOT follow
Databricks' own documented Gunicorn example for Flask apps
(`command: [gunicorn, app:app, -w, 4]`) - that example doesn't show how
Gunicorn would bind to the app's assigned port, and Gunicorn doesn't read
`FLASK_RUN_PORT` the way Flask's own dev server does. Pinning the command
to `python app.py` removes the ambiguity around Databricks' documented
default ("runs the first `.py` file it finds") and reuses the exact code
path already proven working locally and in the scheduled Job, rather than
introducing a new WSGI server + port-binding config this late.

### Fix: `app.run(..., threaded=True)`
The new UI polls `/weather/stats` (60s) and `/weather/feed/recent` (30s)
on independent timers, on top of whatever a user is doing (search, sync).
Flask's dev server handles one request at a time by default - without
`threaded=True`, those background polls would queue up behind a slow
in-flight request (a sync call does real NWS + Lakebase + embedding work,
not instant). Cheap, one-line fix, no behavior change for local dev.

### Key gotcha to solve at deploy time: the app runs as its OWN service principal, not as you
The scheduled Job runs as the human's own identity ("Run as:
srijan2554@gmail.com" - visible on the Jobs UI page), so it inherits
whatever access their personal account already has. Databricks Apps are
different: each app gets its own dedicated service principal that starts
with ZERO permissions - it does not inherit anything from the person who
created the app. Two things this app's service principal needs explicit
grants for before it will work, done via the "App resources" step in the
Apps UI (add-resource, not code):
1. **Secret resource** - `weather` scope, `lakebase-url` key, "Can read"
   permission. Without this, `lakebase.py`'s
   `WorkspaceClient().secrets.get_secret(...)` call fails at connection
   time (this code path is unchanged - no new secret-fetching mechanism
   was introduced for Apps; only the permission grant is new).
2. **Model serving endpoint resource** -
   `databricks-meta-llama-3-3-70b-instruct`, "Can query" permission.
   Without this, the LLM summary feature (`llm_summary.py`) fails - search
   itself still works, summarize just returns `summary_error`
   (best-effort, matches its existing failure-handling design).

### Decision: deploy from the workspace folder (existing Databricks Repo), not a fresh Git integration
Databricks Apps supports deploying either from an uploaded/synced
workspace folder or directly from a Git repository reference. This
project already has a Databricks Repo connected
(`/Workspace/Users/srijan2554@gmail.com/Labkebase-Weather-APP`, set up in
Phase 10) and pulling the latest commits. Deploying from that existing
folder avoids the extra step Git-source deployment requires for private
repos (configuring a Git credential/personal access token specifically
for the app's service principal) - one less moving part this late in the
project, and consistent with how the scheduled Job is already deployed
(also reads from that same synced folder).

### Remaining
- Not yet deployed live - next step is the human creating the app via
  the Databricks Apps UI, granting the two resource permissions above,
  and deploying from the existing workspace folder.
