"""
Embedding model wrapper for the weather pipeline.

Uses sentence-transformers/all-MiniLM-L6-v2 (384-dim) - the SAME model the
existing ticker-news pipeline uses, so both stay queryable with the same
distance-operator conventions and nothing else in the schema needs to
change. Loaded once and reused (loading the model is the slow part - a
few hundred ms to a couple seconds - encoding text after that is fast),
rather than re-loading it per call.
"""
from __future__ import annotations

import os

EMBEDDING_MODEL_NAME = os.environ.get(
    "WEATHER_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
EMBEDDING_DIM = 384

_model = None


def get_model():
    """Lazily load and cache the sentence-transformers model. Import is
    deferred to inside this function so anything that only needs
    chunk_text() or other lightweight pieces of this project doesn't pay
    the cost of importing torch/sentence-transformers at all."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings, returning one 384-number list per input
    string, in the same order. Empty input returns an empty list without
    loading the model at all."""
    if not texts:
        return []
    model = get_model()
    vectors = model.encode(texts, show_progress_bar=False)
    return [v.tolist() for v in vectors]
