"""
One-time setup script: creates the Databricks secret scope for this
project and stores the Lakebase connection URL in it.

No API key needed here (unlike the ticker-news reference app's
setup_secrets.py) - the NWS weather API requires no auth at all.

Run this locally (with `databricks auth login` already done) or from a
Databricks notebook - never commit the resulting secret value anywhere.

Usage (from repo root):
    python scripts/setup_secrets.py
"""
import getpass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace

w = WorkspaceClient()

SCOPE = "weather"
KEY = "lakebase-url"

w.secrets.create_scope(scope=SCOPE)

lakebase_url = getpass.getpass(
    "Paste your Lakebase connection URL "
    "(postgresql://role:password@host:5432/databricks_postgres?sslmode=require): "
)

w.secrets.put_secret(scope=SCOPE, key=KEY, string_value=lakebase_url)

w.secrets.put_acl(
    scope=SCOPE,
    principal="users",
    permission=workspace.AclPermission.READ,
)

print(f"Stored Lakebase URL as secret '{SCOPE}/{KEY}'.")
