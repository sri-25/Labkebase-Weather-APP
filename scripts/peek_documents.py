"""
Quick manual peek at what's in weather_documents - run any time to sanity
check what /weather/sync has synced so far. Not part of the app, just a
debugging convenience.

Usage (from repo root):
    python scripts/peek_documents.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lakebase

rows = lakebase.run_query(
    """
    SELECT location, source_type, headline,
           LEFT(narrative_text, 80) AS narrative_preview, synced_at
    FROM weather_documents
    ORDER BY synced_at DESC
    LIMIT 20
    """
)

print(f"{len(rows)} rows (showing up to 20, newest first):\n")
for row in rows:
    print(f"[{row['source_type']:<8}] {row['location']:<15} {row['headline']}")
    print(f"    {row['narrative_preview']}...")
    print()

counts = lakebase.run_query(
    "SELECT source_type, COUNT(*) AS n FROM weather_documents GROUP BY source_type"
)
print("Totals by source_type:")
for row in counts:
    print(f"  {row['source_type']}: {row['n']}")
