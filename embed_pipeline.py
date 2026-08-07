"""
Shared chunk -> embed -> upsert pipeline.

Used by BOTH:
  - notebooks/ingest_weather_embeddings.py - the standalone/scheduled batch
    job that scans weather_documents for anything without embeddings yet.
  - app.py - embeds documents immediately after any sync (batch or
    watchlist add), so a user can search what they just synced right
    away, without separately running the batch script in another
    terminal. Without this, "add to watchlist" would say "synced: 19"
    while search silently returned nothing for that city until someone
    remembered to run the ingestion script by hand - a real gap for
    anything calling itself a finished app.

Extracted into its own module specifically so both call sites share one
implementation instead of drifting apart over time.
"""
from __future__ import annotations

import lakebase
from chunking import chunk_text
from embedding import EMBEDDING_MODEL_NAME, embed_texts


def build_chunk_rows(documents: list[dict]) -> list[tuple]:
    """
    Chunk + embed a list of documents (each needs at least "id" and
    "narrative_text" keys - matches both a weather_documents DB row and
    the raw dicts weather_client.py produces before they're even written
    to the DB). Returns rows ready for execute_values:
    (id, document_id, chunk_index, chunk_text, embedding, model_name).

    Embeds in one batch across ALL chunks from ALL documents (not one
    document at a time) - loading the model happens once regardless, but
    a single larger encode() call is meaningfully faster than many tiny
    ones.

    Location is prefixed onto the text that gets EMBEDDED (not what's
    stored/displayed as chunk_text). Without this, a document's city name
    lives only in a separate SQL column and never enters the vector at
    all - a query like "how's the weather in seattle" has nothing to
    match against "seattle" specifically, so ranking is driven purely by
    generic topical similarity and a plain, generic-sounding forecast in
    an untracked city can out-rank the actually-relevant city's alert
    text. Baking "{location} — {headline}: " into the embedded string (but
    NOT into the stored chunk_text, which stays clean for display) fixes
    this without needing a schema change. See DECISIONS.md Phase 4.
    """
    # (document_id, chunk_index, chunk_text_for_display, text_to_embed)
    all_chunks: list[tuple[str, int, str, str]] = []
    for doc in documents:
        pieces = chunk_text(doc["narrative_text"])
        location = (doc.get("location") or "").strip()
        headline = (doc.get("headline") or "").strip()
        prefix = " — ".join(p for p in (location, headline) if p)
        for chunk_index, piece in enumerate(pieces):
            embed_text = f"{prefix}: {piece}" if prefix else piece
            all_chunks.append((doc["id"], chunk_index, piece, embed_text))

    if not all_chunks:
        return []

    vectors = embed_texts([c[3] for c in all_chunks])

    rows = []
    for (document_id, chunk_index, chunk_piece, _embed_text), vector in zip(all_chunks, vectors):
        row_id = f"{document_id}_{chunk_index}"
        # pgvector accepts a bracketed string like "[0.1,0.2,...]" cast via
        # ::vector - psycopg2 sends it as a plain string parameter, and the
        # explicit cast in the SQL (see upsert_chunk_rows) tells Postgres
        # how to interpret it, same idiom as the ticker-news pipeline.
        vector_literal = "[" + ",".join(str(x) for x in vector) + "]"
        rows.append((row_id, document_id, chunk_index, chunk_piece, vector_literal, EMBEDDING_MODEL_NAME))
    return rows


def upsert_chunk_rows(rows: list[tuple]) -> int:
    """Batched upsert - one INSERT statement with N value-groups, one round
    trip for the whole batch instead of one INSERT per row. Built manually
    (rather than psycopg2.extras.execute_values, which psycopg3 has no
    direct equivalent for) by repeating the value-group placeholder and
    flattening the row tuples into one parameter list."""
    if not rows:
        return 0

    value_groups = ", ".join(["(%s, %s, %s, %s, %s::vector, %s, now())"] * len(rows))
    flat_params = [value for row in rows for value in row]

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO weather_embeddings
                    (id, document_id, chunk_index, chunk_text, embedding, model_name, created_at)
                VALUES {value_groups}
                ON CONFLICT (id) DO UPDATE
                    SET chunk_text = EXCLUDED.chunk_text,
                        embedding = EXCLUDED.embedding,
                        model_name = EXCLUDED.model_name,
                        created_at = now()
                """,
                flat_params,
            )
            conn.commit()
    return len(rows)


def embed_documents_now(documents: list[dict]) -> int:
    """Convenience wrapper: chunk + embed + upsert a specific list of
    documents (e.g. just-synced ones) in one call. Used by app.py right
    after a sync so results are searchable immediately."""
    rows = build_chunk_rows(documents)
    return upsert_chunk_rows(rows)
