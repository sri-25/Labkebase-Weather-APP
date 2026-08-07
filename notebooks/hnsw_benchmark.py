"""
Benchmark: query latency WITH vs WITHOUT the HNSW index on
weather_embeddings.embedding.

Demonstrates the actual effect of the `USING hnsw (embedding
vector_cosine_ops)` index built in app.py's ensure_weather_embeddings_table()
- drops it, times N sample cosine-similarity queries (sequential scan),
recreates it, times N more (index scan), and reports the difference.

HONESTY NOTE (read before trusting the numbers): HNSW's advantage grows
with row count and dimensionality. On a small corpus (the kind this
homework's V1 produces - tens to low hundreds of documents), a sequential
scan can be just as fast, or even faster once you account for the index's
own overhead - that's a real, well-known characteristic of ANN indexes,
not a bug in this script. Don't be surprised or worried if WITH/WITHOUT
look similar at small scale; report what the numbers actually say rather
than assuming the index "must" win.

WARNING: this drops and recreates a real index against your Lakebase
instance. Safe (no data loss - only the index, not the table, is
dropped), but don't run it against a shared/production instance while
others are querying, since query latency will be worse than normal for
the sequential-scan phase.

Usage:
    python notebooks/hnsw_benchmark.py
    python notebooks/hnsw_benchmark.py --queries 50
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lakebase
from embedding import embed_texts

# A handful of realistic weather queries, cycled to fill out --queries
# runs - real query text (not random vectors) so the benchmark reflects
# actual usage, not a synthetic worst/best case.
SAMPLE_QUERIES = [
    "flash flood warning this weekend",
    "heat advisory dangerous temperatures",
    "winter storm heavy snow accumulation",
    "high wind gusts damaging property",
    "air quality alert unhealthy conditions",
]


def _time_one_query(vector_literal: str) -> float:
    start = time.perf_counter()
    lakebase.run_query(
        """
        SELECT id FROM weather_embeddings
        ORDER BY embedding <=> %s::vector
        LIMIT 5
        """,
        (vector_literal,),
    )
    return time.perf_counter() - start


def _run_phase(vector_literals: list[str], n: int) -> list[float]:
    cycled = (vector_literals * ((n // len(vector_literals)) + 1))[:n]
    return [_time_one_query(lit) for lit in cycled]


def _report(label: str, durations: list[float]) -> None:
    ms = [d * 1000 for d in durations]
    print(
        f"{label}: mean={statistics.mean(ms):.2f}ms  median={statistics.median(ms):.2f}ms  "
        f"min={min(ms):.2f}ms  max={max(ms):.2f}ms  (n={len(ms)})"
    )


def run(queries_per_phase: int = 20) -> dict:
    vectors = embed_texts(SAMPLE_QUERIES)
    vector_literals = ["[" + ",".join(str(x) for x in v) + "]" for v in vectors]

    row_count_result = lakebase.run_query("SELECT COUNT(*) AS n FROM weather_embeddings")
    row_count = row_count_result[0]["n"] if row_count_result else 0
    print(f"Benchmarking against {row_count} embedding rows, {queries_per_phase} queries per phase.\n")

    print("Dropping HNSW index...")
    lakebase.run_write("DROP INDEX IF EXISTS idx_weather_embeddings_embedding")

    print(f"Running {queries_per_phase} queries WITHOUT index (sequential scan)...")
    without_index = _run_phase(vector_literals, queries_per_phase)

    print("Recreating HNSW index...")
    lakebase.run_write(
        "CREATE INDEX idx_weather_embeddings_embedding "
        "ON weather_embeddings USING hnsw (embedding vector_cosine_ops)"
    )

    print(f"Running {queries_per_phase} queries WITH index...")
    with_index = _run_phase(vector_literals, queries_per_phase)

    print()
    _report("WITHOUT HNSW index", without_index)
    _report("WITH HNSW index   ", with_index)

    mean_without = statistics.mean(without_index) * 1000
    mean_with = statistics.mean(with_index) * 1000
    if mean_with < mean_without:
        print(f"\nIndex is faster by {mean_without - mean_with:.2f}ms on average "
              f"({(1 - mean_with / mean_without) * 100:.1f}% reduction).")
    else:
        print(f"\nIndex was NOT faster on this run ({mean_with - mean_without:.2f}ms slower on "
              f"average) - expected at small row counts, see this script's docstring.")

    return {"row_count": row_count, "without_index_ms": without_index, "with_index_ms": with_index}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=20, help="Queries per phase (default: 20)")
    args = parser.parse_args()
    run(queries_per_phase=args.queries)
