"""Who created and changed what, and how often each widget and dataset is used.

History rows are written in the same transaction as the change they describe.
Usage is counted per entity and day; a failed count never fails the request.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from app.core import database

logger = logging.getLogger(__name__)

# Layout overrides change on every drag; their history would only be noise.
TRACKED_ENTITIES = {"dashboards", "widgets", "metrics", "kpis", "roles", "weight_sets", "category_weights"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def record(connection, entity_type: str, entity_id: str, action: str, *, actor: str | None, revision: int | None = None, before: Any = None, after: Any = None) -> None:
    connection.execute("INSERT INTO analytics_history VALUES (?,?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, entity_type, entity_id, action, revision, actor or "system", _now(), _json(before), _json(after)))


def history(store, entity_type: str | None = None, entity_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    conditions, values = [], []
    for column, value in (("entity_type", entity_type), ("entity_id", entity_id)):
        if value:
            conditions.append(f"{column}=?")
            values.append(value)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with store._connection() as connection:
        rows = connection.execute(f"SELECT * FROM analytics_history {where} ORDER BY changed_at DESC LIMIT ?", (*values, min(max(limit, 1), 1000))).fetchall()
    fields = ("id", "entity_type", "entity_id", "action", "revision", "actor_id", "changed_at")
    return [{field: row[field] for field in fields} | {side: json.loads(row[f"{side}_json"]) if row[f"{side}_json"] else None for side in ("before", "after")} for row in rows]


def touch(store, items: Iterable[tuple[str, str]], *, total_ms: float = 0.0, cache_hit: bool = False, error: bool = False) -> None:
    """Count one use of each (entity_type, entity_id) for today."""
    items = list(dict.fromkeys(items))
    if not items:
        return
    now, day = _now(), date.today().isoformat()
    try:
        store._initialize()
        # A plain connection: usage is not configuration and must not expire the definitions cache.
        connection = database.connect(store.path, "bi")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for entity_type, entity_id in items:
                connection.execute(
                    "INSERT INTO analytics_usage VALUES (?,?,?,1,?,?,?,?) ON CONFLICT(entity_type,entity_id,day) DO UPDATE SET "
                    "runs=analytics_usage.runs+1, cache_hits=analytics_usage.cache_hits+excluded.cache_hits, errors=analytics_usage.errors+excluded.errors, "
                    "total_ms=analytics_usage.total_ms+excluded.total_ms, last_used_at=excluded.last_used_at",
                    (entity_type, entity_id, day, int(cache_hit), int(error), float(total_ms), now))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    except Exception:
        logger.warning("analytics_usage_failed items=%s", len(items), exc_info=True)


def usage(store, days: int = 30) -> dict[str, Any]:
    """Uses per widget, dashboard and dataset over the last days, and what nobody used."""
    since = (date.today() - timedelta(days=max(days, 1) - 1)).isoformat()
    with store._connection() as connection:
        used = {(row["entity_type"], row["entity_id"]): dict(row) for row in connection.execute(
            "SELECT entity_type, entity_id, SUM(runs) AS runs, SUM(cache_hits) AS cache_hits, SUM(errors) AS errors, SUM(total_ms) AS total_ms, MAX(last_used_at) AS last_used_at "
            "FROM analytics_usage WHERE day>=? GROUP BY entity_type, entity_id", (since,))}
        last_ever = {(row["entity_type"], row["entity_id"]): row["last_used_at"] for row in connection.execute(
            "SELECT entity_type, entity_id, MAX(last_used_at) AS last_used_at FROM analytics_usage GROUP BY entity_type, entity_id")}
        known = [("widget", row["id"], row["dashboard_id"], None, row["created_at"]) for row in connection.execute("SELECT id, dashboard_id, created_at FROM dashboard_widgets")]
        known += [("widget", row["id"], row["dashboard_id"], row["user_id"], row["created_at"]) for row in connection.execute("SELECT id, dashboard_id, user_id, created_at FROM dashboard_personal_widgets")]
        known += [("dashboard", row["id"], None, None, row["created_at"]) for row in connection.execute("SELECT id, created_at FROM dashboards")]
        known += [("dataset", row["dataset_key"], None, row["created_by"], row["created_at"]) for row in connection.execute("SELECT dataset_key, created_by, created_at FROM sql_datasets WHERE archived=0")]
    items = []
    for entity_type, entity_id, dashboard_id, owner, created_at in known:
        stats = used.get((entity_type, entity_id), {})
        items.append({"entity_type": entity_type, "entity_id": entity_id, "dashboard_id": dashboard_id, "owner_id": owner, "created_at": created_at,
                      "runs": int(stats.get("runs") or 0), "cache_hits": int(stats.get("cache_hits") or 0), "errors": int(stats.get("errors") or 0),
                      "avg_ms": round(float(stats.get("total_ms") or 0) / max(int(stats.get("runs") or 0), 1), 1),
                      "last_used_at": last_ever.get((entity_type, entity_id))})
    listed = {(item["entity_type"], item["entity_id"]) for item in items}
    # Built-in parts (the crew ranking) are counted although nobody stores them.
    for (entity_type, entity_id), stats in used.items():
        if (entity_type, entity_id) not in listed:
            items.append({"entity_type": entity_type, "entity_id": entity_id, "dashboard_id": None, "owner_id": None, "created_at": None,
                          "runs": int(stats["runs"] or 0), "cache_hits": int(stats["cache_hits"] or 0), "errors": int(stats["errors"] or 0),
                          "avg_ms": round(float(stats["total_ms"] or 0) / max(int(stats["runs"] or 0), 1), 1), "last_used_at": stats["last_used_at"]})
    items.sort(key=lambda item: (item["runs"], item["last_used_at"] or ""))
    return {"days": days, "since": since, "items": items, "unused": [item for item in items if item["runs"] == 0]}
