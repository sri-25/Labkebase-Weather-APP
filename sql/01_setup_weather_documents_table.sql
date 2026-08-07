-- Setup script for weather_documents table.
-- This table is auto-created by ensure_weather_documents_table() in app.py
-- on first request (same pattern as the reference app), so running this
-- manually is optional - it exists for documentation/manual reference.

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
);

CREATE INDEX IF NOT EXISTS idx_weather_documents_location ON weather_documents (location);
CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type ON weather_documents (source_type);

-- Verify the table was created
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name = 'weather_documents'
ORDER BY ordinal_position;
