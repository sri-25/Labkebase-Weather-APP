"""
Manual smoke test for embedding.py - NOT part of the automated pytest
suite, since it downloads a real ~80MB model file the first time it runs.

Named verify_ rather than test_ specifically so plain `pytest` (run from
the project root, with no path argument) never tries to collect this file
- it isn't a pytest test at all, it's a manual sanity check. See
DECISIONS.md Phase 8 for the naming-collision bug this fixes.

Run this once to confirm the model loads and produces 384-number vectors,
and that semantically similar sentences end up numerically close together
(a basic sanity check that the embeddings actually mean something).

Usage (from repo root):
    python scripts/verify_embedding_model.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from embedding import EMBEDDING_DIM, embed_texts

sentences = [
    "Flash Flood Warning issued for Cook County",
    "Heavy rain expected to cause rapid flooding in low-lying areas",
    "Sunny skies with a high near 78 degrees",
]

print(f"Embedding {len(sentences)} test sentences...")
vectors = embed_texts(sentences)

print(f"Got {len(vectors)} vectors, each with {len(vectors[0])} numbers "
      f"(expected {EMBEDDING_DIM}).")
assert all(len(v) == EMBEDDING_DIM for v in vectors), "dimension mismatch!"

# Cosine similarity, computed by hand (no numpy needed for a quick check)
def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b)

sim_flood_flood = cosine_similarity(vectors[0], vectors[1])
sim_flood_sunny = cosine_similarity(vectors[0], vectors[2])

print(f"\nSimilarity('Flash Flood Warning' vs 'Heavy rain...flooding'): {sim_flood_flood:.3f}")
print(f"Similarity('Flash Flood Warning' vs 'Sunny skies...'):          {sim_flood_sunny:.3f}")
print("\nExpect the first number to be noticeably higher than the second -")
print("that's the model correctly recognizing the two flood sentences are")
print("about the same thing, while the sunny-weather sentence is not.")
