-- Setup script for weather_embeddings table.
-- Requires the pgvector extension (adds the VECTOR column type + <=>
-- cosine-distance operator + HNSW index support to Postgres).
-- 384 dimensions matches sentence-transformers/all-MiniLM-L6-v2, the
-- embedding model used by ingest_weather_embeddings.py. If that model
-- ever changes, this dimension must change to match exactly.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS weather_embeddings (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding VECTOR(384) NOT NULL,
    model_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW index for fast cosine-similarity search (used by pgvector's <=> operator)
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding
ON weather_embeddings
USING hnsw (embedding vector_cosine_ops);

-- Speeds up "all chunks for this document" lookups (e.g. re-embedding, cleanup)
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id
ON weather_embeddings (document_id);

-- Verify the table was created
SELECT table_name, column_name, data_type, udt_name
FROM information_schema.columns
WHERE table_name = 'weather_embeddings'
ORDER BY ordinal_position;
