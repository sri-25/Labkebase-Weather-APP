"""
Unit tests for notebooks/sync_weather_job.py - the scheduled re-sync
entrypoint. Syncs a fixed, configured location list (no watchlist table
anymore - see DECISIONS.md Phase 9).

This script imports app.py (to reuse its tested _sync_one_location logic),
which imports lakebase.py (needs live Databricks credentials just to
import - WorkspaceClient() is instantiated at module level) and eagerly
loads the real embedding model at import time. Same sys.modules injection
technique as test_app.py, for the same reason - and the same defensive
sys.modules.pop() before re-faking, since app.py/lakebase/embedding are
shared modules other test files also import fresh copies of.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "notebooks"))

for _mod in ("lakebase", "embedding", "embed_pipeline", "llm_summary", "app", "sync_weather_job"):
    sys.modules.pop(_mod, None)

_fake_lakebase = MagicMock()
sys.modules["lakebase"] = _fake_lakebase

_fake_embedding = MagicMock()
_fake_embedding.EMBEDDING_MODEL_NAME = "fake-model"
_fake_embedding.get_model = MagicMock(return_value=None)
_fake_embedding.embed_texts = MagicMock(return_value=[[0.1] * 384])
sys.modules["embedding"] = _fake_embedding

import sync_weather_job as job  # noqa: E402


@pytest.fixture(autouse=True)
def reset_mocks():
    _fake_lakebase.reset_mock(return_value=True, side_effect=True)
    yield


def test_run_defaults_to_app_default_locations(monkeypatch, capsys):
    monkeypatch.setattr(job, "DEFAULT_LOCATIONS", ["Chicago, IL", "Austin, TX", "Miami, FL"])
    monkeypatch.setattr(job, "_sync_one_location", lambda client, loc, limit: (2, None))

    result = job.run()

    assert result["locations"] == ["Chicago, IL", "Austin, TX", "Miami, FL"]
    assert result["total_synced"] == 6


def test_run_empty_locations_short_circuits(capsys):
    calls = {"sync": 0}

    result = job.run(locations=[])

    assert result == {"locations": [], "total_synced": 0, "failures": []}
    assert "Nothing to sync" in capsys.readouterr().out


def test_run_syncs_every_configured_location(monkeypatch, capsys):
    calls = []

    def fake_sync(client, location, limit):
        calls.append(location)
        return (3, None)

    monkeypatch.setattr(job, "_sync_one_location", fake_sync)

    result = job.run(locations=["Denver, CO", "Seattle, WA"], limit=25)

    assert calls == ["Denver, CO", "Seattle, WA"]
    assert result["total_synced"] == 6
    assert result["failures"] == []
    out = capsys.readouterr().out
    assert "Synced 6 document(s) across 2 location(s)" in out


def test_run_collects_per_location_failures_without_stopping(monkeypatch):
    def fake_sync(client, location, limit):
        if location == "Bad, ZZ":
            return (0, "Could not resolve 'Bad, ZZ'")
        return (5, None)

    monkeypatch.setattr(job, "_sync_one_location", fake_sync)

    result = job.run(locations=["Chicago, IL", "Bad, ZZ"])

    assert result["total_synced"] == 5
    assert result["failures"] == [{"location": "Bad, ZZ", "error": "Could not resolve 'Bad, ZZ'"}]


def test_run_passes_limit_through_to_sync_one_location(monkeypatch):
    captured = {}

    def fake_sync(client, location, limit):
        captured["limit"] = limit
        return (0, None)

    monkeypatch.setattr(job, "_sync_one_location", fake_sync)

    job.run(locations=["Chicago, IL"], limit=99)

    assert captured["limit"] == 99


def test_cli_locations_arg_parses_semicolon_separated_list():
    """Smoke-test the argparse wiring shape - --locations is
    semicolon-separated to match app.py's WEATHER_DEFAULT_LOCATIONS
    convention (commas already appear inside "City, ST")."""
    raw = "Denver, CO;Seattle, WA"
    parsed = [loc.strip() for loc in raw.split(";") if loc.strip()]
    assert parsed == ["Denver, CO", "Seattle, WA"]
