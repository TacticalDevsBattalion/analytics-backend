"""Administrator-selected ClickHouse columns exposed as extra filter fields.

A custom filter may point at a real column of the flights table that no built-in
filter covers. The logical name is ``x_<column>``; the column must exist in the
live table schema, so no identifier ever comes straight from an HTTP request.
Child tables (events, ammunition, losses) inherit these fields through the flight.
"""
from __future__ import annotations

import re
import threading
import time

PREFIX = "x_"
ENTITY = "flight_list"
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TTL_SECONDS = 5.0

_lock = threading.Lock()
_applied: dict[str, str] = {}
_checked_at = 0.0


def is_extra_field(field: object) -> bool:
    return isinstance(field, str) and field.startswith(PREFIX) and bool(_COLUMN.fullmatch(field[len(PREFIX):]))


def _configured_fields() -> set[str]:
    from app.core.app_configuration import get_configuration_store

    custom = get_configuration_store().snapshot().config.filters.custom
    return {item.field for item in custom if is_extra_field(item.field)}


def _existing(fields: set[str]) -> dict[str, str]:
    from app.services.clickhouse_client import clickhouse_client
    from app.core.config import get_backend_config

    table = get_backend_config().clickhouse.tables[ENTITY]
    columns = clickhouse_client.table_columns(table)
    return {field: field[len(PREFIX):] for field in sorted(fields) if field[len(PREFIX):] in columns}


def _apply(mapping: dict[str, str]) -> None:
    from app.bi import engine
    from app.core.config import get_backend_config

    stale = set(_applied) - set(mapping)
    fresh = set(mapping)
    flights = engine.SOURCE_FIELDS["flights"]
    for target in (flights, engine.INHERITED_FLIGHT_FIELDS, engine.ENRICHED_FIELDS, engine.FILTER_FIELDS):
        target.difference_update(stale)
        target.update(fresh)
    get_backend_config().clickhouse_fields.set_extra(ENTITY, mapping)
    _applied.clear()
    _applied.update(mapping)


def sync(force: bool = False) -> None:
    """Align engine field registries with the published configuration."""
    global _checked_at
    from app.core.config import get_settings

    if get_settings().active_source != "clickhouse":
        return
    now = time.monotonic()
    with _lock:
        if not force and now - _checked_at < _TTL_SECONDS:
            return
        try:
            mapping = _existing(_configured_fields())
        except Exception:
            # Keep the last known registry when storage or ClickHouse is unavailable.
            _checked_at = now
            return
        _checked_at = now
        if mapping != _applied:
            _apply(mapping)


def signature() -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(_applied.items()))


def available_columns() -> list[dict[str, str]]:
    """Columns of the flights table that no existing field mapping already uses."""
    from app.core.config import get_backend_config
    from app.services.clickhouse_client import clickhouse_client

    config = get_backend_config()
    columns = clickhouse_client.table_columns(config.clickhouse.tables[ENTITY])
    mapped: set[str] = set()
    for key, value in config.clickhouse_fields.entities.get(ENTITY, {}).items():
        mapped.update([value] if isinstance(value, str) else value)
    return [
        {"column": name, "field": PREFIX + name}
        for name in sorted(columns)
        if _COLUMN.fullmatch(name) and name not in mapped
    ]
