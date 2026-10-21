"""Copy the account, configuration and BI stores from SQLite files into PostgreSQL.

    DATABASE_URL=postgresql://... python -m app.tools.sqlite_to_postgres [--replace] [--dry-run]

The SQLite files are read from APP_USERS_DB, APP_CONFIGURATION_DB and APP_BI_DB
(as configured for the backend) and upgraded to their latest SQLite schema first.
The PostgreSQL schema must be empty of user data unless --replace is given, which
first empties every store table. Everything is copied in one transaction and the
row counts are compared before it commits; background images kept as files are
copied into the database too.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from app.core import database

SOURCES = {
    "users": ("APP_USERS_DB", "/app/data/app_users.sqlite3"),
    "configuration": ("APP_CONFIGURATION_DB", "/app/data/app_configuration.sqlite3"),
    "bi": ("APP_BI_DB", "/app/data/app_bi.sqlite3"),
}
# Rows the PostgreSQL schema seeds itself; they are replaced by the source rows.
SEEDED = {"account_state", "account_schema_migrations", "bi_schema_migrations", "bi_state", "weight_sets"}
USER_DATA = ("users", "configuration_versions", "dashboards", "metric_definitions", "user_access")


def _paths() -> dict[str, Path]:
    return {store: Path(os.environ.get(name, default)).expanduser().resolve() for store, (name, default) in SOURCES.items()}


def _upgrade_sqlite(paths: dict[str, Path]) -> None:
    """Bring old files to the latest SQLite schema with the stores' own migrations."""
    url = os.environ.pop("DATABASE_URL", None)
    try:
        from app.bi.store import BiStore
        from app.core.app_configuration import ApplicationConfigurationStore
        from app.core.user_accounts import UserAccountStore
        from app.core import keycloak
        if paths["users"].exists():
            store = UserAccountStore(paths["users"])
            with store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                keycloak._migrate(connection)
        if paths["configuration"].exists():
            ApplicationConfigurationStore(paths["configuration"]).snapshot()
        if paths["bi"].exists():
            BiStore(paths["bi"]).ensure_seed()
    finally:
        if url is not None:
            os.environ["DATABASE_URL"] = url


def _source_tables(paths: dict[str, Path]) -> dict[str, tuple[Path, list[str]]]:
    found = {}
    for path in paths.values():
        if not path.exists():
            continue
        with sqlite3.connect(path) as connection:
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                found[table] = (path, columns)
    return found


def migrate(replace: bool = False, dry_run: bool = False, out=sys.stdout) -> dict[str, int]:
    if not database.postgres_enabled():
        raise SystemExit("DATABASE_URL must point to PostgreSQL")
    paths = _paths()
    missing = [str(path) for path in paths.values() if not path.exists()]
    if len(missing) == len(paths):
        raise SystemExit("No SQLite store files found: " + ", ".join(missing))
    _upgrade_sqlite(paths)
    sources = _source_tables(paths)
    tables = [table for table in database.iter_tables() if table in sources]
    target = database.connect(paths["bi"], "bi")
    copied: dict[str, int] = {}
    try:
        target.execute("BEGIN IMMEDIATE")
        target_columns = {table: [row[0] for row in target.execute("PRAGMA table_info(%s)" % table)] for table in database.iter_tables()}
        busy = [table for table in USER_DATA if target.execute(f"SELECT count(*) FROM {table}").fetchone()[0]]
        if busy and not replace:
            raise SystemExit(f"PostgreSQL already has data in {', '.join(busy)}; run with --replace to overwrite it")
        # Children first, so foreign keys never block the delete.
        for table in reversed(list(database.iter_tables())):
            if replace or table in SEEDED or table in sources:
                target.execute(f"DELETE FROM {table}")
        for table in tables:
            path, columns = sources[table]
            shared = [column for column in columns if column in target_columns[table]]
            with sqlite3.connect(path) as source:
                rows = source.execute(f'SELECT {",".join(shared)} FROM "{table}"').fetchall()
            for row in rows:
                target.execute(f"INSERT INTO {table} ({','.join(shared)}) VALUES ({','.join('?' for _ in shared)})", row)
            copied[table] = len(rows)
            counted = target.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if counted != len(rows):
                raise RuntimeError(f"{table}: copied {len(rows)} rows but PostgreSQL has {counted}")
            print(f"{table:34} {len(rows):6}", file=out)
        # Background images uploaded before they were kept in the database.
        directory = paths["bi"].parent / "dashboard-assets"
        images = 0
        for (identifier,) in target.execute("SELECT id FROM dashboard_assets WHERE id NOT IN (SELECT id FROM dashboard_asset_content)").fetchall():
            file = directory / identifier
            if file.is_file():
                target.execute("INSERT INTO dashboard_asset_content VALUES (?,?)", (identifier, file.read_bytes()))
                images += 1
        copied["dashboard_asset_files"] = images
        print(f"{'dashboard image files':34} {images:6}", file=out)
        if dry_run:
            target.rollback()
            print("Dry run: nothing was written.", file=out)
        else:
            target.commit()
            print("Copied into PostgreSQL schema " + database.schema_for(paths["bi"]) + ".", file=out)
    finally:
        target.close()
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replace", action="store_true", help="empty the PostgreSQL store tables first")
    parser.add_argument("--dry-run", action="store_true", help="copy and check inside a transaction, then roll back")
    arguments = parser.parse_args()
    try:
        migrate(arguments.replace, arguments.dry_run)
    finally:
        database.close_pools()


if __name__ == "__main__":
    main()
