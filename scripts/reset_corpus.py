"""
Reset the weather corpus - wipes weather_documents (weather_embeddings
cascades automatically) and the old weather_watchlist table if it's
still around.

Named reset_, not test_, so plain `pytest` never collects it. Destructive
- requires --yes to actually run; prints row counts before/after either way.

Usage (from repo root):
    python scripts/reset_corpus.py            # dry run - shows counts, changes nothing
    python scripts/reset_corpus.py --yes      # actually wipes the tables
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lakebase


def _count(table: str) -> int:
    rows = lakebase.run_query(f"SELECT COUNT(*) AS n FROM {table}")
    return rows[0]["n"] if rows else 0


def run(confirm: bool = False) -> None:
    doc_count = _count("weather_documents")
    embed_count = _count("weather_embeddings")
    print(f"Current state: {doc_count} weather_documents, {embed_count} weather_embeddings rows.")

    try:
        watchlist_count = _count("weather_watchlist")
        print(f"(weather_watchlist still exists with {watchlist_count} row(s) - no longer used by the app.)")
    except Exception:
        watchlist_count = 0
        print("(weather_watchlist table not found - nothing to clean up there.)")

    if not confirm:
        print("\nDry run - nothing changed. Re-run with --yes to actually wipe these tables.")
        return

    print("\nWiping weather_documents (weather_embeddings cascades automatically)...")
    lakebase.run_write("TRUNCATE weather_documents CASCADE")

    if watchlist_count or watchlist_count == 0:
        try:
            print("Wiping weather_watchlist...")
            lakebase.run_write("TRUNCATE weather_watchlist")
        except Exception as e:
            print(f"(Skipped weather_watchlist: {e})")

    print(f"\nDone. Re-sync a deliberate set of cities via:")
    print('  python notebooks/sync_weather_job.py --locations "Denver, CO;Seattle, WA"')
    print("or:")
    print('  curl -X POST http://localhost:8000/weather/sync -H "Content-Type: application/json" '
          '-d \'{"locations": ["Denver, CO", "Seattle, WA"]}\'')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", dest="confirm",
        help="Actually perform the wipe (default: dry run, shows counts only)",
    )
    args = parser.parse_args()
    run(confirm=args.confirm)
