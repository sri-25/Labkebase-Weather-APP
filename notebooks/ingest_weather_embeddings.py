"""
Ingest weather_documents -> weather_embeddings (Lakebase).

Plain psycopg3 script, not Spark - Spark JDBC can't reliably write
pgvector VECTOR columns or run ON CONFLICT upserts here. Runs fine from a
terminal, cron, or a Databricks Job "python script" task.

Finds documents with no embeddings yet (LEFT JOIN against
weather_embeddings, no built-in change-tracking in Postgres so this is
the stand-in), then chunks/embeds/upserts them via embed_pipeline.py -
the same module app.py uses inline after every sync. This script is the
safety net for anything that slips through that path.

Usage:
    python notebooks/ingest_weather_embeddings.py
    python notebooks/ingest_weather_embeddings.py --limit 100
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly (python notebooks/ingest_weather_embeddings.py)
# without needing the project root on PYTHONPATH already.
try:
    _this_file = Path(__file__).resolve()
except NameError:
    # Databricks' spark_python_task runs the file via exec(), not a normal
    # `python file.py` invocation, so __file__ is never defined there -
    # sys.argv[0] (the python_file path from the job spec) is the reliable
    # fallback in that context. See notebooks/sync_weather_job.py for where
    # this was first found live.
    _this_file = Path(sys.argv[0]).resolve()
sys.path.insert(0, str(_this_file.parent.parent))

import lakebase
from embed_pipeline import build_chunk_rows, upsert_chunk_rows
from embedding import EMBEDDING_DIM, EMBEDDING_MODEL_NAME


def find_unembedded_documents(limit: int | None = None) -> list[dict]:
    """Documents with no matching weather_embeddings rows yet. Selects
    location + headline too, not just id/narrative_text - build_chunk_rows()
    bakes those into the embedded text, so skipping them here would mean
    picked-up documents lose that context."""
    sql = """
        SELECT d.id, d.narrative_text, d.location, d.headline
        FROM weather_documents d
        LEFT JOIN weather_embeddings e ON e.document_id = d.id
        WHERE e.id IS NULL
          AND d.narrative_text IS NOT NULL
          AND d.narrative_text != ''
        ORDER BY d.synced_at DESC
    """
    if limit:
        sql += " LIMIT %s"
        return lakebase.run_query(sql, (limit,))
    return lakebase.run_query(sql)


def find_all_documents(limit: int | None = None) -> list[dict]:
    """Every document with narrative text, embedded or not. Used by --all
    to force a full re-embed - e.g. after changing what gets embedded, or
    switching models, where existing rows are stale rather than missing."""
    sql = """
        SELECT d.id, d.narrative_text, d.location, d.headline
        FROM weather_documents d
        WHERE d.narrative_text IS NOT NULL
          AND d.narrative_text != ''
        ORDER BY d.synced_at DESC
    """
    if limit:
        sql += " LIMIT %s"
        return lakebase.run_query(sql, (limit,))
    return lakebase.run_query(sql)


def run(limit: int | None = None, reembed_all: bool = False) -> None:
    print(f"Model: {EMBEDDING_MODEL_NAME} ({EMBEDDING_DIM} dims)")
    if reembed_all:
        print("Re-embedding ALL documents (--all: ignoring existing embeddings)...")
        documents = find_all_documents(limit=limit)
        print(f"Found {len(documents)} documents.")
    else:
        print("Finding documents without embeddings yet...")
        documents = find_unembedded_documents(limit=limit)
        print(f"Found {len(documents)} unembedded documents.")

    if not documents:
        print("Nothing to do.")
        return

    rows = build_chunk_rows(documents)
    written = upsert_chunk_rows(rows)
    print(f"Wrote {written} chunk embeddings to weather_embeddings.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of documents to process this run (default: all)",
    )
    parser.add_argument(
        "--all", action="store_true", dest="reembed_all",
        help="Re-embed every document, not just unembedded ones - use "
             "after a change to what gets embedded so existing rows pick "
             "up the new behavior too.",
    )
    args = parser.parse_args()
    run(limit=args.limit, reembed_all=args.reembed_all)
