"""SQL datasets: versioned ClickHouse queries kept in the BI store, like Superset datasets.

Administrators write and publish the SQL; the backend runs only the published version,
always as its one service user and with readonly=1, so a dataset can never change data.
Parameters are ClickHouse's own ({name:Type}), declared by the SQL itself and sent apart
from it, never pasted into the text. Every version stays in the store with its author,
and each change and run is recorded (app.bi.activity).

The files in sql_seed/ only create a dataset that does not exist yet; from then on the
store is the source and an administrator changes it by publishing a new version.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.bi import activity

SEED = Path(__file__).parent / "sql_seed"
KEY = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
PARAMETER = re.compile(r"\{(\w+):([^{}]+)\}")
COMMENT = re.compile(r"--[^\n]*")
STRING = re.compile(r"'(?:[^'\\]|\\.|'')*'")
SCALAR_TYPES = re.compile(r"^(String|Date|Date32|DateTime|U?Int(8|16|32|64)|Float(32|64)|Bool)$")
MAX_SQL = 100_000
CACHE_SECONDS = 30
TEXT_TYPES = {"String", "Date", "Date32", "DateTime"}


class DatasetError(ValueError):
    pass


class DatasetNotFound(LookupError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _code(sql: str) -> str:
    """The SQL without comments and string contents, where parameters and statements are read."""
    return STRING.sub("''", COMMENT.sub("", sql))


def _check_type(ch_type: str) -> None:
    inner = ch_type[9:-1] if ch_type.startswith("Nullable(") and ch_type.endswith(")") else ch_type
    inner = inner[6:-1] if inner.startswith("Array(") and inner.endswith(")") else inner
    if not SCALAR_TYPES.match(inner):
        raise DatasetError(f"Unsupported parameter type: {ch_type}")


def parameters(sql: str) -> dict[str, str]:
    """Name → ClickHouse type of every {name:Type} in the SQL."""
    found: dict[str, str] = {}
    for name, ch_type in PARAMETER.findall(_code(sql)):
        ch_type = ch_type.strip()
        _check_type(ch_type)
        if found.setdefault(name, ch_type) != ch_type:
            raise DatasetError(f"Parameter {name} is used with two types")
    return found


def validate(sql: str) -> dict[str, str]:
    if not sql.strip() or len(sql) > MAX_SQL:
        raise DatasetError("The SQL is empty or too long")
    code = _code(sql).strip().rstrip(";").strip()
    if ";" in code:
        raise DatasetError("A dataset is one query")
    if not re.match(r"(?is)^(with|select)\b", code):
        raise DatasetError("A dataset is a SELECT query")
    # The backend reads JSON lines and sets the limits itself.
    if re.search(r"(?i)\b(format|settings|into\s+outfile)\b", code):
        raise DatasetError("FORMAT, SETTINGS and INTO OUTFILE are set by the backend")
    return parameters(sql)


def _literal(value: Any, ch_type: str) -> str:
    if ch_type in TEXT_TYPES:
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    if not isinstance(value, (int, float)):
        raise DatasetError(f"Expected a number for {ch_type}")
    return repr(value)


def encode(value: Any, ch_type: str) -> str:
    """A parameter value in ClickHouse's text form for its declared type."""
    nullable = ch_type.startswith("Nullable(")
    inner = ch_type[9:-1] if nullable else ch_type
    if value is None:
        if not nullable:
            raise DatasetError(f"A value is required for {ch_type}")
        return "\\N"
    if inner.startswith("Array("):
        if not isinstance(value, (list, tuple)):
            raise DatasetError(f"Expected a list for {ch_type}")
        item = inner[6:-1]
        return "[" + ",".join(_literal(element, item) for element in value) + "]"
    if inner in TEXT_TYPES:
        return str(value)
    return _literal(value, inner)


class DatasetStore:
    """Datasets in the BI store: PostgreSQL in production, SQLite in development and tests."""

    def __init__(self, store):
        self.store = store
        self._published: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._seeded = False

    def _seed(self) -> None:
        if self._seeded:
            return
        with self._lock:
            if self._seeded:
                return
            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for path in sorted(SEED.glob("*.sql")):
                    key = path.stem
                    if connection.execute("SELECT 1 FROM sql_datasets WHERE dataset_key=?", (key,)).fetchone():
                        continue
                    text = path.read_text(encoding="utf-8")
                    meta = dict(re.findall(r"(?m)^--\s*(title|description):\s*(.+)$", text))
                    self._insert(connection, key, meta.get("title", key), meta.get("description", ""), text, "Початкова версія", "system", publish=True)
            self._seeded = True

    def _insert(self, connection, key, title, description, sql, comment, actor, *, publish):
        declared = validate(sql)
        now = _now()
        connection.execute("INSERT INTO sql_datasets VALUES (?,?,?,?,?,?,?,?,?)", (key, title, description, 1 if publish else None, 0, actor, now, actor, now))
        connection.execute("INSERT INTO sql_dataset_versions VALUES (?,?,?,?,?,?,?)", (key, 1, sql, json.dumps(declared), comment, actor, now))
        activity.record(connection, "dataset", key, "create", actor=actor, revision=1, after={"title": title, "description": description, "sql": sql, "published": publish})

    def list(self) -> list[dict[str, Any]]:
        self._seed()
        with self.store._connection() as connection:
            rows = connection.execute("SELECT d.*, (SELECT MAX(version) FROM sql_dataset_versions v WHERE v.dataset_key=d.dataset_key) AS latest_version FROM sql_datasets d ORDER BY d.dataset_key").fetchall()
        return [dict(row) for row in rows]

    def get(self, key: str) -> dict[str, Any]:
        self._seed()
        with self.store._connection() as connection:
            row = connection.execute("SELECT * FROM sql_datasets WHERE dataset_key=?", (key,)).fetchone()
            if row is None:
                raise DatasetNotFound("Dataset was not found")
            versions = [{**dict(item), "parameters": json.loads(item["parameters_json"])} for item in connection.execute(
                "SELECT * FROM sql_dataset_versions WHERE dataset_key=? ORDER BY version DESC", (key,))]
        for item in versions:
            item.pop("parameters_json")
        return {**dict(row), "versions": versions}

    def save_version(self, key: str, sql: str, *, actor: str, comment: str = "", title: str | None = None, description: str | None = None) -> dict[str, Any]:
        """A new draft version; a new key creates the dataset. Nothing runs it until it is published."""
        if not KEY.match(key):
            raise DatasetError("The key is 2–100 lowercase letters, digits, '.', '_' or '-'")
        declared = validate(sql)
        self._seed()
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM sql_datasets WHERE dataset_key=?", (key,)).fetchone()
            if current is None:
                self._insert(connection, key, title or key, description or "", sql, comment, actor, publish=False)
                version = 1
            else:
                version = int(connection.execute("SELECT MAX(version) AS latest FROM sql_dataset_versions WHERE dataset_key=?", (key,)).fetchone()["latest"]) + 1
                now = _now()
                connection.execute("INSERT INTO sql_dataset_versions VALUES (?,?,?,?,?,?,?)", (key, version, sql, json.dumps(declared), comment, actor, now))
                connection.execute("UPDATE sql_datasets SET title=?, description=?, updated_by=?, updated_at=? WHERE dataset_key=?",
                                   (title or current["title"], current["description"] if description is None else description, actor, now, key))
                activity.record(connection, "dataset", key, "version", actor=actor, revision=version, after={"sql": sql, "comment": comment})
        return {"dataset_key": key, "version": version, "parameters": declared}

    def publish(self, key: str, version: int, *, actor: str) -> None:
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT published_version FROM sql_datasets WHERE dataset_key=?", (key,)).fetchone()
            if current is None or not connection.execute("SELECT 1 FROM sql_dataset_versions WHERE dataset_key=? AND version=?", (key, version)).fetchone():
                raise DatasetNotFound("Dataset version was not found")
            connection.execute("UPDATE sql_datasets SET published_version=?, archived=0, updated_by=?, updated_at=? WHERE dataset_key=?", (version, actor, _now(), key))
            activity.record(connection, "dataset", key, "publish", actor=actor, revision=version, before={"published_version": current["published_version"]}, after={"published_version": version})
        self._published.pop(key, None)

    def archive(self, key: str, *, actor: str) -> None:
        """Unused datasets are archived, not deleted: their versions and history remain."""
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM sql_datasets WHERE dataset_key=?", (key,)).fetchone():
                raise DatasetNotFound("Dataset was not found")
            connection.execute("UPDATE sql_datasets SET archived=1, updated_by=?, updated_at=? WHERE dataset_key=?", (actor, _now(), key))
            activity.record(connection, "dataset", key, "archive", actor=actor)
        self._published.pop(key, None)

    def published(self, key: str) -> dict[str, Any]:
        """The published SQL, reread at most every CACHE_SECONDS (other replicas publish too)."""
        cached = self._published.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        self._seed()
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT d.dataset_key, d.published_version AS version, v.sql_text, v.parameters_json FROM sql_datasets d "
                "JOIN sql_dataset_versions v ON v.dataset_key=d.dataset_key AND v.version=d.published_version WHERE d.dataset_key=? AND d.archived=0", (key,)).fetchone()
        if row is None:
            raise DatasetNotFound(f"Dataset {key} has no published version")
        value = {"key": key, "version": int(row["version"]), "sql": row["sql_text"], "parameters": json.loads(row["parameters_json"])}
        self._published[key] = (time.monotonic(), value)
        return value

    def run(self, key: str, values: dict[str, Any], *, version: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Rows of a dataset for these parameter values; every declared parameter is required."""
        from app.services.clickhouse_client import clickhouse_client

        if version is None:
            dataset = self.published(key)
        else:
            item = next((item for item in self.get(key)["versions"] if item["version"] == version), None)
            if item is None:
                raise DatasetNotFound("Dataset version was not found")
            dataset = {"key": key, "version": version, "sql": item["sql_text"], "parameters": item["parameters"]}
        declared = dataset["parameters"]
        if set(values) != set(declared):
            raise DatasetError(f"Parameters differ: missing {sorted(set(declared) - set(values))}, unknown {sorted(set(values) - set(declared))}")
        encoded = {name: encode(values[name], ch_type) for name, ch_type in declared.items()}
        sql = dataset["sql"] if limit is None else f"SELECT * FROM (\n{dataset['sql']}\n) LIMIT {int(limit)}"
        started, failed = time.perf_counter(), False
        try:
            return clickhouse_client.query(sql, parameters=encoded, settings={"readonly": 1})
        except Exception:
            failed = True
            raise
        finally:
            activity.touch(self.store, [("dataset", key)], total_ms=(time.perf_counter() - started) * 1000, error=failed)


_datasets: DatasetStore | None = None
_datasets_lock = threading.Lock()


def get_dataset_store() -> DatasetStore:
    from app.bi.store import get_bi_store
    global _datasets
    store = get_bi_store()
    with _datasets_lock:
        if _datasets is None or _datasets.store is not store:
            _datasets = DatasetStore(store)
        return _datasets
