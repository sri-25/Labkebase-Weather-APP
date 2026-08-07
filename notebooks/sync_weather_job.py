"""
Scheduled re-sync entrypoint - re-syncs + re-embeds a configured list of
cities on a timer, without needing anyone to run /weather/sync by hand.
Meant to run as a Databricks Job (see resources/sync_weather_job.json) so
the corpus stays fresh automatically.

Which cities: no watchlist table anymore (see DECISIONS.md Phase 9 - the
per-city "Cities Tracked" UI/table was removed entirely). This job syncs
a fixed, configured location list instead - reuses app.py's
DEFAULT_LOCATIONS by default (same "WEATHER_DEFAULT_LOCATIONS" env var
used by POST /weather/sync when no locations are given, so the app and
this job agree on what "the corpus" means unless you override one of
them), or pass --locations to sync something else for this run only.

Deliberately thin: it reuses app.py's already-tested per-city sync logic
(_sync_one_location - harvest -> upsert -> embed -> expire-cleanup, see
DECISIONS.md Phases 3-5) rather than duplicating that logic here. Yes,
this means importing a "private" (underscore-prefixed) function from
app.py, which is a known wart - the cleaner fix is extracting a shared
sync_pipeline module both app.py and this script import from, still
deferred to a future codebase cleanup pass.

Plain psycopg3 + requests script (NOT Spark) - matches the homework
spec's explicit note that Spark JDBC can't reliably write pgvector VECTOR
columns or run ON CONFLICT upserts against Lakebase. Runs fine as a
Databricks Job "Python script" task, a local terminal, or cron - no
dbutils/notebook-only APIs used.

Usage:
    python notebooks/sync_weather_job.py
    python notebooks/sync_weather_job.py --limit 100
    python notebooks/sync_weather_job.py --locations "Denver, CO;Seattle, WA"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly (python notebooks/sync_weather_job.py)
# without needing the project root on PYTHONPATH already.
try:
    _this_file = Path(__file__).resolve()
except NameError:
    # Databricks' spark_python_task runs the file via exec(), not a normal
    # `python file.py` invocation, so __file__ is never defined there -
    # sys.argv[0] (the python_file path from the job spec) is the reliable
    # fallback in that context. Found live: a scheduled-job run failed with
    # "NameError: name '__file__' is not defined" until this was added.
    _this_file = Path(sys.argv[0]).resolve()
sys.path.insert(0, str(_this_file.parent.parent))

from app import DEFAULT_LOCATIONS, WeatherClient, _sync_one_location


def run(locations: list[str] | None = None, limit: int = 50) -> dict:
    """Sync + embed every configured location. Returns a summary dict
    (also printed) rather than just printing, so this is easy to unit
    test and so a caller can inspect the outcome without scraping
    stdout."""
    locations = locations if locations is not None else DEFAULT_LOCATIONS
    print(f"Syncing {len(locations)} location(s): {locations}")

    if not locations:
        print("Nothing to sync - no locations configured.")
        return {"locations": [], "total_synced": 0, "failures": []}

    client = WeatherClient()
    total_synced = 0
    failures = []
    for location in locations:
        count, error = _sync_one_location(client, location, limit)
        total_synced += count
        status = f"error: {error}" if error else f"{count} document(s)"
        print(f"  {location}: {status}")
        if error:
            failures.append({"location": location, "error": error})

    print(f"Done. Synced {total_synced} document(s) across {len(locations)} location(s).")
    if failures:
        print(f"{len(failures)} location(s) had errors: {failures}")

    return {"locations": locations, "total_synced": total_synced, "failures": failures}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max documents to sync per city, per run (default: 50)",
    )
    parser.add_argument(
        "--locations", type=str, default=None,
        help='Semicolon-separated locations to sync instead of the default '
             'list, e.g. "Denver, CO;Seattle, WA" (default: DEFAULT_LOCATIONS '
             'from app.py / WEATHER_DEFAULT_LOCATIONS env var)',
    )
    args = parser.parse_args()

    locations_arg = None
    if args.locations:
        locations_arg = [loc.strip() for loc in args.locations.split(";") if loc.strip()]

    run(locations=locations_arg, limit=args.limit)
