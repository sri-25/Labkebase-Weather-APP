"""
Quick manual peek at what's in weather_embeddings - run any time to sanity
check what the ingestion script has embedded so far. Not part of the app,
just a debugging convenience (same idea as peek_documents.py).

Usage (from repo root):
    python scripts/peek_embeddings.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lakebase

rows = lakebase.run_query(
    """
    SELECT e.document_id, e.chunk_index, d.location, d.headline,
           LEFT(e.chunk_text, 70) AS chunk_preview, e.model_name
    FROM weather_embeddings e
    JOIN weather_documents d ON d.id = e.document_id
    ORDER BY e.created_at DESC
    LIMIT 20
    """
)

print(f"{len(rows)} embedding rows (showing up to 20, newest first):\n")
for row in rows:
    print(f"[{row['location']:<15}] {row['headline']} (chunk {row['chunk_index']})")
    print(f"    {row['chunk_preview']}...")
    print()

counts = lakebase.run_query(
    """
    SELECT COUNT(*) AS total_chunks, COUNT(DISTINCT document_id) AS total_documents
    FROM weather_embeddings
    """
)
print(f"Totals: {counts[0]['total_chunks']} chunks across {counts[0]['total_documents']} documents")
