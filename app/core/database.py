"""Connections for the account, configuration and BI stores.

SQLite files remain the default. With DATABASE_URL every store lives in one
PostgreSQL schema (DATABASE_SCHEMA, default ``app``) under a single database user.
The stores keep their SQL: this module translates the few SQLite dialect parts
they use (``?`` parameters, ``BEGIN IMMEDIATE``, ``INSERT OR IGNORE``, ``PRAGMA``)
and raises ``sqlite3`` exception types, so their error handling stays the same.
``BEGIN IMMEDIATE`` takes a per-store advisory lock: writers of one store are
serialized exactly as with SQLite's single writer.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import re
import sqlite3
import threading
import weakref
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "migrations" / "postgres"

_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()
_ready: set[str] = set()
_search_paths: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()
_ready_lock = threading.Lock()


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


def postgres_enabled() -> bool:
    return database_url().startswith(("postgresql://", "postgres://"))


def schema_for(path: Path) -> str:
    """One shared schema; tests may give every store path its own schema."""
    schema = os.environ.get("DATABASE_SCHEMA", "app").strip() or "app"
    if os.environ.get("DATABASE_ISOLATE_PATHS", "").lower() in {"1", "true"}:
        schema = "t_" + hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:16]
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("DATABASE_SCHEMA must be a lowercase identifier")
    return schema


def is_postgres(connection: Any) -> bool:
    return isinstance(getattr(connection, "raw", connection), PostgresConnection)


def location(path: str | Path) -> str:
    """Where a store lives: its PostgreSQL schema or its SQLite file."""
    return "pg:" + schema_for(Path(path)) if postgres_enabled() else str(Path(path).resolve())


_WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP")


class TrackedConnection:
    """Forwards to a store connection and notes whether anything was written."""

    def __init__(self, raw):
        self.raw = raw
        self.wrote = False

    def execute(self, sql: str, parameters: Any = ()):
        if not self.wrote and sql.lstrip()[:7].upper().startswith(_WRITES):
            self.wrote = True
        return self.raw.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.raw, name)


class ReadCache:
    """Decoded store reads, valid for one store revision.

    Settings and BI definitions are read on every request but change rarely. The
    revision is re-read at most every STORE_READ_CACHE_SECONDS (1 s): writes in this
    process invalidate at once, writes from other pods are seen within that time.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.values: dict[Any, tuple[Any, Any]] = {}
        self.revision: Any = None
        self.checked = 0.0

    def current(self, fetch) -> Any:
        import time
        ttl = float(os.environ.get("STORE_READ_CACHE_SECONDS", "1"))
        with self.lock:
            now = time.monotonic()
            if self.revision is not None and now - self.checked < ttl:
                return self.revision
        revision = fetch()
        with self.lock:
            if revision != self.revision:
                self.values.clear()
            self.revision, self.checked = revision, time.monotonic()
        return revision

    def get(self, key, revision) -> tuple[bool, Any]:
        with self.lock:
            entry = self.values.get(key)
        return (True, entry[1]) if entry is not None and entry[0] == revision else (False, None)

    def put(self, key, revision, value) -> None:
        with self.lock:
            if len(self.values) > 2048:
                self.values.clear()
            self.values[key] = (revision, value)

    def invalidate(self) -> None:
        with self.lock:
            self.values.clear()
            self.revision = None
            self.checked = 0.0


_read_caches: dict[str, ReadCache] = {}


def read_cache(key: str) -> ReadCache:
    with _pools_lock:
        return _read_caches.setdefault(key, ReadCache())


def connect(path: str | Path, store: str):
    """A connection with the sqlite3 interface the stores use."""
    if postgres_enabled():
        return PostgresConnection(_pool(), schema_for(Path(path)), store)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _pool():
    url = database_url()
    with _pools_lock:
        pool = _pools.get(url)
        if pool is None:
            from psycopg_pool import ConnectionPool
            size = max(1, min(32, int(os.environ.get("DATABASE_POOL_SIZE", "8"))))
            # No per-checkout ping (it doubles every round trip): TCP keepalives and a
            # bounded lifetime drop dead connections instead.
            pool = ConnectionPool(url, min_size=min(2, size), max_size=size, timeout=15, open=True, name="analytics-stores",
                                  max_idle=300, max_lifetime=1800, reconnect_timeout=60,
                                  kwargs={"autocommit": True, "connect_timeout": 10, "application_name": "analytics-backend",
                                          "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3})
            _pools[url] = pool
        return pool


@atexit.register
def close_pools() -> None:
    with _pools_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
    with _ready_lock:
        _ready.clear()
    for cache in list(_read_caches.values()):
        cache.invalidate()


class Row(tuple):
    """Index and column-name access, like sqlite3.Row."""
    _names: tuple[str, ...] = ()

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._names.index(key))
            except ValueError:
                raise IndexError(f"No column named {key}") from None
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = tuple(column.name for column in cursor.description or ())
    row_type = type("Row", (Row,), {"_names": names, "__slots__": ()})
    return lambda values: row_type(values)


class _Empty:
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


_DDL_TYPES = [(re.compile(r"\bINTEGER\b"), "BIGINT"), (re.compile(r"\bREAL\b"), "DOUBLE PRECISION"), (re.compile(r"\bBLOB\b"), "BYTEA")]


@lru_cache(maxsize=2048)
def translate(sql: str) -> str:
    """SQLite statement -> PostgreSQL statement with %s parameters."""
    text = sql.strip()
    upper = text.upper()
    if upper.startswith(("CREATE TABLE", "ALTER TABLE")):
        for pattern, replacement in _DDL_TYPES:
            text = pattern.sub(replacement, text)
    if upper.startswith("INSERT OR IGNORE INTO"):
        text = "INSERT INTO" + text[len("INSERT OR IGNORE INTO"):].rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # Parameters: '?' outside string literals; a literal '%' must be doubled.
    out, quote = [], None
    for char in text:
        if quote:
            out.append("%%" if char == "%" else char)
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        elif char == "%":
            out.append("%%")
        else:
            out.append(char)
    return "".join(out)


class PostgresConnection:
    """The subset of sqlite3.Connection used by the stores, on a pooled connection."""

    def __init__(self, pool, schema: str, store: str):
        self._pool = pool
        self._schema = schema
        self._lock_key = int.from_bytes(hashlib.sha1(f"{schema}:{store}".encode()).digest()[:8], "big", signed=True)
        self._connection = pool.getconn()
        try:
            # Pooled connections keep their search_path; set it only when it changes.
            if _search_paths.get(self._connection) != schema:
                self._connection.execute(f'SET search_path TO "{schema}"')
                _search_paths[self._connection] = schema
            _ensure_schema(self._connection, schema)
        except BaseException:
            self._release()
            raise

    row_factory = None  # Accepted for interface compatibility.

    @property
    def in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus
        return self._connection.info.transaction_status in {TransactionStatus.INTRANS, TransactionStatus.INERROR}

    def execute(self, sql: str, parameters: Any = ()):
        import psycopg
        statement = sql.strip().rstrip(";").strip()
        upper = statement.upper()
        try:
            if upper in {"BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"}:
                self._connection.execute("BEGIN")
                self._connection.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key,))
                return _Empty()
            if upper in {"BEGIN", "BEGIN DEFERRED", "BEGIN TRANSACTION"}:
                self._connection.execute("BEGIN")
                return _Empty()
            if upper.startswith("PRAGMA"):
                match = re.fullmatch(r"PRAGMA\s+table_info\((\w+)\)", statement, re.IGNORECASE)
                if match:
                    return self._connection.cursor(row_factory=_row_factory).execute(
                        "SELECT column_name AS name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                        (self._schema, match.group(1).lower()))
                return _Empty()  # foreign_keys and other SQLite settings.
            values = tuple(int(value) if isinstance(value, bool) else value for value in (parameters or ()))
            return self._connection.cursor(row_factory=_row_factory).execute(translate(statement), values)
        except psycopg.errors.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc).splitlines()[0]) from exc
        except psycopg.Error as exc:
            raise sqlite3.OperationalError(str(exc).splitlines()[0] if str(exc) else type(exc).__name__) from exc

    def commit(self) -> None:
        if self.in_transaction:
            self._connection.execute("COMMIT")

    def rollback(self) -> None:
        if self.in_transaction:
            self._connection.execute("ROLLBACK")

    def _release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            if not connection.closed and connection.info.transaction_status.name != "IDLE":
                connection.execute("ROLLBACK")
        except Exception:
            pass
        self._pool.putconn(connection)

    def close(self) -> None:
        self._release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _ensure_schema(connection, schema: str) -> None:
    """Create the schema with every store's tables once, under a cluster-wide lock."""
    if schema in _ready:
        return
    with _ready_lock:
        if schema in _ready:
            return
        connection.execute("BEGIN")
        try:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (int.from_bytes(hashlib.sha1(f"{schema}:schema".encode()).digest()[:8], "big", signed=True),))
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            connection.execute(f'SET LOCAL search_path TO "{schema}"')
            connection.execute("CREATE TABLE IF NOT EXISTS pg_schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            applied = {row[0] for row in connection.execute("SELECT name FROM pg_schema_migrations").fetchall()}
            for migration in sorted(SCHEMA_FILE.glob("*.sql")):
                if migration.name in applied:
                    continue
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO pg_schema_migrations (name) VALUES (%s)", (migration.name,))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute(f'SET search_path TO "{schema}"')
        _ready.add(schema)


def iter_tables() -> Iterator[str]:
    """Tables owned by the stores, in an order that satisfies foreign keys."""
    yield from (
        "account_state", "users", "sessions", "dashboard_preferences", "login_attempts", "account_schema_migrations",
        "federated_identities", "oidc_login_states",
        "configuration_versions", "configuration_state", "configuration_draft",
        "bi_schema_migrations", "bi_state", "bi_bootstrap_versions", "dashboards", "dashboard_widgets", "dashboard_assignments",
        "dashboard_user_overrides", "dashboard_override_revisions", "dashboard_personal_widgets", "metric_definitions",
        "kpi_definitions", "weight_sets", "category_weights", "roles", "user_access", "analytics_cache_jobs",
        "kpi_policy_versions", "personal_dashboard_preferences", "dashboard_assets", "dashboard_asset_content",
        "access_audit", "user_preferences", "sql_datasets", "sql_dataset_versions", "analytics_history", "analytics_usage",
    )
