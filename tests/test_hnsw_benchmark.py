"""
Unit tests for notebooks/hnsw_benchmark.py.

This can only meaningfully smoke-test the ORCHESTRATION (drop index ->
time queries -> recreate index -> time queries -> report) with a mocked
lakebase - the actual benchmark VALUE (real latency numbers) requires a
real Lakebase instance with real data and can't be usefully asserted on
in a hermetic unit test. Live-running this script against real Lakebase
is a separate, manual verification step (see its docstring).

Same sys.modules injection technique as the other DB-touching test files.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "notebooks"))

for _mod in ("lakebase", "embedding", "hnsw_benchmark"):
    sys.modules.pop(_mod, None)

_fake_lakebase = MagicMock()
sys.modules["lakebase"] = _fake_lakebase

_fake_embedding = MagicMock()
_fake_embedding.embed_texts = MagicMock(return_value=[[0.1, 0.2, 0.3] for _ in range(5)])
sys.modules["embedding"] = _fake_embedding

import hnsw_benchmark as bench  # noqa: E402


@pytest.fixture(autouse=True)
def reset_mocks():
    _fake_lakebase.reset_mock(return_value=True, side_effect=True)
    # Used for BOTH the one-off COUNT(*) query (needs an "n" key) and the
    # repeated similarity queries (whose result is never inspected, only
    # timed) - one shape that satisfies both call sites.
    _fake_lakebase.run_query.return_value = [{"n": 0, "id": "doc1_0"}]
    yield


def test_run_drops_index_before_timing_without_index(capsys):
    bench.run(queries_per_phase=2)
    write_calls = [c[0][0] for c in _fake_lakebase.run_write.call_args_list]
    assert any("DROP INDEX IF EXISTS idx_weather_embeddings_embedding" in c for c in write_calls)


def test_run_recreates_index_after_without_index_phase(capsys):
    bench.run(queries_per_phase=2)
    write_calls = [c[0][0] for c in _fake_lakebase.run_write.call_args_list]
    drop_idx = next(i for i, c in enumerate(write_calls) if "DROP INDEX" in c)
    create_idx = next(i for i, c in enumerate(write_calls) if "CREATE INDEX" in c)
    assert drop_idx < create_idx
    assert "USING hnsw (embedding vector_cosine_ops)" in write_calls[create_idx]


def test_run_times_the_requested_number_of_queries_per_phase(capsys):
    result = bench.run(queries_per_phase=3)
    assert len(result["without_index_ms"]) == 3
    assert len(result["with_index_ms"]) == 3


def test_run_reports_row_count_from_query(capsys):
    _fake_lakebase.run_query.side_effect = [
        [{"n": 42}],  # COUNT(*) query
        *([[{"id": "d"}]] * 10),  # the actual similarity queries
    ]
    bench.run(queries_per_phase=2)
    out = capsys.readouterr().out
    assert "42 embedding rows" in out


def test_run_prints_both_summary_reports(capsys):
    bench.run(queries_per_phase=2)
    out = capsys.readouterr().out
    assert "WITHOUT HNSW index" in out
    assert "WITH HNSW index" in out
