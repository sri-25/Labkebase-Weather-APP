"""
Manual smoke test for lakebase.py - NOT part of the automated pytest suite,
since it needs your real Databricks secret + live Lakebase instance to
succeed (the tests/ folder is all mocked and needs neither).

Named verify_ rather than test_ specifically so plain `pytest` (run from
the project root, with no path argument) never tries to collect this file
- it isn't a pytest test at all, it's a live-credential smoke check, and
importing it without a live connection just crashes. See DECISIONS.md
Phase 8 for the naming-collision bug this fixes.

Run this once, right after setup_secrets.py, to prove the connection
actually works end-to-end - before we build anything on top of it.

Usage (from repo root):
    python scripts/verify_connection.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lakebase

print("Connecting to Lakebase...")
rows = lakebase.run_query("SELECT version() AS pg_version, now() AS server_time")

print("Connected successfully. Postgres says:")
for row in rows:
    print(f"  version:     {row['pg_version']}")
    print(f"  server_time: {row['server_time']}")
