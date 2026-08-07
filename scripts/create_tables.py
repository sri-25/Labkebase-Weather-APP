"""
Standalone table setup: creates weather_documents + weather_embeddings
in Lakebase and prints their columns back, for a one-time manual check
that the schema landed correctly outside of running the full app.

Usage (from repo root):
    python scripts/create_tables.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lakebase


def ensure_weather_documents_table() -> None:
    """Create the weather_documents table in Lakebase if it doesn't exist yet."""
    lakebase.run_write(
        """
        CREATE TABLE IF NOT EXISTS weather_documents (
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
        "CREATE INDEX IF NOT EXISTS idx_weather_documents_location "
        "ON weather_documents (location)"
    )
    lakebase.run_write(
        "CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type "
        "ON weather_documents (source_type)"
    )


def ensure_weather_embeddings_table() -> None:
    """Create weather_embeddings if it doesn't exist. Requires the
    pgvector extension (VECTOR type + <=> distance operator). 384
    dimensions matches sentence-transformers/all-MiniLM-L6-v2."""
    lakebase.run_write("CREATE EXTENSION IF NOT EXISTS vector")
    lakebase.run_write(
        """
        CREATE TABLE IF NOT EXISTS weather_embeddings (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
            chunk_index INT NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding VECTOR(384) NOT NULL,
            model_name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    lakebase.run_write(
        "CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding "
        "ON weather_embeddings USING hnsw (embedding vector_cosine_ops)"
    )
    lakebase.run_write(
        "CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id "
        "ON weather_embeddings (document_id)"
    )


def _print_columns(table_name: str) -> None:
    rows = lakebase.run_query(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    for row in rows:
        type_label = row["udt_name"] if row["data_type"] == "USER-DEFINED" else row["data_type"]
        print(f"  {row['column_name']:<15} {type_label}")


if __name__ == "__main__":
    print("Creating weather_documents table (if it doesn't already exist)...")
    ensure_weather_documents_table()
    print("Done. Columns now in weather_documents:")
    _print_columns("weather_documents")

    print()
    print("Creating weather_embeddings table (if it doesn't already exist)...")
    ensure_weather_embeddings_table()
    print("Done. Columns now in weather_embeddings:")
    _print_columns("weather_embeddings")
