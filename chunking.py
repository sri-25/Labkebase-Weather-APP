"""
Text chunking for the embedding pipeline.

Splits long narrative_text into overlapping windows so a long piece of
text doesn't get truncated when embedded, and a search match retains some
surrounding context instead of stopping mid-sentence at a hard boundary.

Defaults (CHUNK_SIZE=800, CHUNK_OVERLAP=100) match the values suggested by
the homework spec and the existing ticker-news pipeline
(notebooks/ingest_ticker_news_embeddings.py) - kept the same rather than
inventing new ones. Most NWS text (a single forecast period, or most
alerts on their own) is well under 800 characters and won't be split at
all; this mainly kicks in for the longer combined alert
description+instruction text.
"""
from __future__ import annotations

import os

CHUNK_SIZE = int(os.environ.get("WEATHER_CHUNK_SIZE", 800))
CHUNK_OVERLAP = int(os.environ.get("WEATHER_CHUNK_OVERLAP", 100))


def chunk_text(
    text: str, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """
    Split `text` into a list of overlapping chunks, each up to `chunk_size`
    characters long, with `chunk_overlap` characters shared between
    consecutive chunks.

    Returns [] for empty/whitespace-only input, and a single one-item list
    if `text` already fits within `chunk_size` (the common case for NWS
    text - no splitting needed).
    """
    text = (text or "").strip()
    if not text:
        return []
    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap ({chunk_overlap}) must be smaller than chunk_size ({chunk_size})"
        )
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    step = chunk_size - chunk_overlap
    start = 0
    while True:
        piece = text[start : start + chunk_size].strip()
        if piece:
            chunks.append(piece)
        if start + chunk_size >= len(text):
            break
        start += step
    return chunks
