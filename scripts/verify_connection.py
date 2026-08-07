"""
Manual smoke test for lakebase.py - needs a real Lakebase connection, so
it's not part of the mocked pytest suite. Named verify_, not test_, so
plain `pytest` never tries to collect it.

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
