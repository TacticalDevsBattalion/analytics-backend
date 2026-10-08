from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time
from typing import Any

from app.api.models import FilterRequest
from app.core.cache import cache
from app.core.config import get_backend_config
from app.core.kpi_configuration import KpiConfiguration, KpiDataUnavailable
from .analytics_engine import (
    AnalyticsEngine,
    ANALYTICS_CONFIG,
    AMMO_FIELDS,
    EVENT_FIELDS,
    FLIGHT_FIELDS,
    LOST_DEVICE_FIELDS,
    _filter_rows_by_time,
    _flight_ids,
    _has_time_filter,
    _is_excluded_purpose,
    _restrict_by_ids,
)
from .clickhouse_client import clickhouse_client, quote_identifier

CONFIG = get_backend_config()
CH_CONFIG = CONFIG.clickhouse
CH_FIELDS = CONFIG.clickhouse_fields
CACHE_CONFIG = CONFIG.cache


class _SqlWhere:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.params: dict[str, Any] = {}
        self._seq = 0

    def _name(self, prefix: str = "p") -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def raw(self, expression: str | None) -> None:
        if expression:
            self.parts.append(expression)

    def scalar(self, field: str, value: Any, ch_type: str = "String") -> None:
        name = self._name()
        self.parts.append(f"{quote_identifier(field)} = {{{name}:{ch_type}}}")
        self.params[name] = value

    def strings(self, field: str | None, values: Iterable[str]) -> None:
        clean = [str(v).strip() for v in values if str(v).strip()]
        if not clean:
            return
        if not field:
            raise RuntimeError("Selected filter is unavailable in the ClickHouse schema")
        expressions: list[str] = []
        for value in clean:
            name = self._name()
            expressions.append(f"{quote_identifier(field)} = {{{name}:String}}")
            self.params[name] = value
        self.parts.append("(" + " OR ".join(expressions) + ")")

    def ints(self, field: str | None, values: Iterable[int]) -> None:
        clean = list(values)
        if not clean:
            return
        if not field:
            raise RuntimeError("Selected ID filter is unavailable in the ClickHouse schema")
        expressions: list[str] = []
        for value in clean:
            name = self._name()
            expressions.append(f"toInt64({quote_identifier(field)}) = {{{name}:Int64}}")
            self.params[name] = int(value)
        self.parts.append("(" + " OR ".join(expressions) + ")")

    def not_strings(self, field: str, values: Iterable[str]) -> None:
        clean = [str(v).strip() for v in values if str(v).strip()]
        for value in clean:
            name = self._name()
            self.parts.append(f"{quote_identifier(field)} != {{{name}:String}}")
            self.params[name] = value

    def dates(self, field: str, start: date, end: date) -> None:
        a = self._name("d")
        b = self._name("d")
        q = quote_identifier(field)
        self.parts.extend([f"{q} >= {{{a}:Date}}", f"{q} <= {{{b}:Date}}"])
        self.params[a] = start.isoformat()
        self.params[b] = end.isoformat()

    def datetimes(self, field: str, start: datetime, end: datetime) -> None:
        a = self._name("ts")
        b = self._name("ts")
        q = quote_identifier(field)
        timezone = ANALYTICS_CONFIG.timezone.replace("'", "")
        self.parts.extend(
            [
                f"{q} >= toDateTime64({{{a}:String}}, 3, '{timezone}')",
                f"{q} <= toDateTime64({{{b}:String}}, 3, '{timezone}')",
            ]
        )
        self.params[a] = start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.params[b] = end.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def sql(self) -> str:
        return " AND ".join(self.parts) if self.parts else "1"


class ClickHouseAnalytics(AnalyticsEngine):
    """ClickHouse-backed adapter preserving the existing dashboard calculations.

    The first migration step intentionally keeps the established Python KPI rules
    and replaces only the transport/data-source layer. Physical ClickHouse column
    names are resolved from config/backend/clickhouse_fields.json and are aliased
    to the canonical keys already used by the analytics code.
    """

    def __init__(self) -> None:
        self._resolved: dict[tuple[str, str], str | None] = {}

    def _require_kpi_fields(self, kpi: KpiConfiguration | None) -> None:
        if kpi and any(rule.success_mode == "results" for rule in kpi.purpose_rules):
            if not self._physical("flight_list", "main_result"):
                raise KpiDataUnavailable("Flight main_result is unavailable in this ClickHouse schema; success by selected results cannot be calculated")

    def overview(self, f: FilterRequest, kpi: KpiConfiguration | None = None):
        self._require_kpi_fields(kpi)
        return super().overview(f, kpi)

    def timeline(self, f: FilterRequest, kpi: KpiConfiguration | None = None):
        self._require_kpi_fields(kpi)
        return super().timeline(f, kpi)

    def kpi_preview(self, f: FilterRequest, kpi: KpiConfiguration, source: str = "clickhouse"):
        self._require_kpi_fields(kpi)
        return super().kpi_preview(f, kpi, source)

    def _table(self, entity: str) -> str:
        try:
            return CH_CONFIG.tables[entity]
        except KeyError as exc:
            raise RuntimeError(f"ClickHouse table is not configured: {entity}") from exc

    def _physical(self, entity: str, key: str, *, required: bool = False) -> str | None:
        cache_key = (entity, key)
        if cache_key in self._resolved:
            value = self._resolved[cache_key]
            if required and not value:
                raise RuntimeError(f"Required ClickHouse field is missing: {entity}.{key}")
            return value

        table = self._table(entity)
        columns = clickhouse_client.table_columns(table)
        value = next((candidate for candidate in CH_FIELDS.candidates(entity, key) if candidate in columns), None)
        self._resolved[cache_key] = value
        if required and not value:
            candidates = ", ".join(CH_FIELDS.candidates(entity, key))
            raise RuntimeError(
                f"Required ClickHouse field is missing: {entity}.{key}; tried [{candidates}] in {table}"
            )
        return value

    def _validate_required(self) -> None:
        for entity, keys in CH_CONFIG.required_fields.items():
            for key in keys:
                self._physical(entity, key, required=True)

    def _canonical_alias(self, entity: str, key: str) -> str:
        # Internal analytics rows use logical snake_case names.
        return key

    def _select(self, entity: str, keys: list[str]) -> str:
        required = set(CH_CONFIG.required_fields.get(entity, []))
        columns: list[str] = []
        for key in keys:
            physical = self._physical(entity, key, required=key in required)
            alias = self._canonical_alias(entity, key)
            if physical:
                columns.append(f"{quote_identifier(physical)} AS {quote_identifier(alias)}")
            else:
                columns.append(f"NULL AS {quote_identifier(alias)}")
        return ",\n  ".join(columns)

    def _query_rows(
        self,
        entity: str,
        keys: list[str],
        where: _SqlWhere,
        *,
        order_by: list[tuple[str, str]] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        table = self._table(entity)
        sql = f"SELECT\n  {self._select(entity, keys)}\nFROM {quote_identifier(table)}\nWHERE {where.sql()}"
        if order_by:
            clauses: list[str] = []
            for key, direction in order_by:
                field = self._physical(entity, key, required=True)
                clauses.append(f"{quote_identifier(field)} {'DESC' if direction.lower() == 'desc' else 'ASC'}")
            sql += "\nORDER BY " + ", ".join(clauses)
        if limit is not None:
            sql += f"\nLIMIT {max(0, int(limit))}"
        rows = clickhouse_client.query(sql, parameters=where.params)

        # JSONEachRow can serialize UInt8/Bool differently across ClickHouse versions.
        if entity == "flight_list":
            effective_key = FLIGHT_FIELDS["is_effective"]
            for row in rows:
                value = row.get(effective_key)
                if value is not None:
                    row[effective_key] = value is True or value == 1 or str(value).lower() in {"1", "true"}
        return rows

    @staticmethod
    def _id_or_title_values(values: Iterable[str]) -> tuple[list[int], list[str]]:
        ids: list[int] = []
        titles: list[str] = []
        for raw in values:
            value = str(raw).strip()
            if not value:
                continue
            try:
                ids.append(int(value))
            except ValueError:
                titles.append(value)
        return ids, titles

    def _organization_filter(
        self,
        where: _SqlWhere,
        entity: str,
        id_key: str,
        title_key: str,
        values: Iterable[str],
    ) -> None:
        ids, titles = self._id_or_title_values(values)
        if not ids and not titles:
            return
        id_field = self._physical(entity, id_key)
        title_field = self._physical(entity, title_key)
        groups: list[str] = []
        if ids:
            if not id_field:
                raise RuntimeError(f"Selected {id_key} filter is unavailable in the ClickHouse schema")
            parts: list[str] = []
            for value in ids:
                name = where._name()
                parts.append(f"toInt64({quote_identifier(id_field)}) = {{{name}:Int64}}")
                where.params[name] = int(value)
            groups.append("(" + " OR ".join(parts) + ")")
        if titles:
            if not title_field:
                raise RuntimeError(f"Selected {title_key} filter is unavailable in the ClickHouse schema")
            parts = []
            for value in titles:
                name = where._name()
                parts.append(f"{quote_identifier(title_field)} = {{{name}:String}}")
                where.params[name] = value
            groups.append("(" + " OR ".join(parts) + ")")
        if groups:
            where.raw("(" + " OR ".join(groups) + ")")

    def _flight_where(self, f: FilterRequest, *, include_excluded: bool = False) -> _SqlWhere:
        where = _SqlWhere()
        date_field = self._physical("flight_list", "date", required=True)
        time_field = self._physical("flight_list", "time_range", required=True)
        if _has_time_filter(f):
            start = datetime.combine(f.date_from, f.time_from or time.min)
            end = datetime.combine(f.date_to, f.time_to or time.max)
            where.datetimes(time_field, start, end)
        else:
            where.dates(date_field, f.date_from, f.date_to)

        purpose_field = self._physical("flight_list", "main_purpose", required=True)
        if not include_excluded:
            where.not_strings(purpose_field, ANALYTICS_CONFIG.excluded_analytics_purposes)

        where.strings(self._physical("flight_list", "direction"), f.direction)
        where.strings(self._physical("flight_list", "device_type"), f.category)
        where.strings(self._physical("flight_list", "device"), f.asset)
        where.strings(self._physical("flight_list", "crew"), f.group)
        where.strings(purpose_field, f.purpose)
        self._organization_filter(where, "flight_list", "bbak_id", "bbak_title", f.bbak)
        self._organization_filter(where, "flight_list", "bbak_id", "bbak_title", f.battalion)
        if f.rota:
            rota_id, rota_titles = self._id_or_title_values(f.rota)
            if rota_id:
                where.ints(self._physical("flight_list", "rota_id"), rota_id)
            if rota_titles:
                where.strings(self._physical("flight_list", "rota_title"), rota_titles)
        return where

    def _event_where(self, f: FilterRequest) -> _SqlWhere:
        where = _SqlWhere()
        where.dates(self._physical("events", "date", required=True), f.date_from, f.date_to)
        purpose = self._physical("events", "flight_purpose")
        if purpose:
            where.not_strings(purpose, ANALYTICS_CONFIG.excluded_analytics_purposes)
        where.strings(self._physical("events", "direction"), f.direction)
        where.strings(self._physical("events", "units"), f.unit)
        where.strings(self._physical("events", "device"), f.asset)
        where.strings(self._physical("events", "crew"), f.group)
        where.strings(purpose, f.purpose)
        where.strings(self._physical("events", "target_class"), f.class_name)
        where.strings(self._physical("events", "result"), f.result)
        return where

    def _date_where(self, entity: str, f: FilterRequest) -> _SqlWhere:
        where = _SqlWhere()
        where.dates(self._physical(entity, "date", required=True), f.date_from, f.date_to)
        return where

    def _flights(self, f: FilterRequest) -> list[dict[str, Any]]:
        keys = list(dict.fromkeys([*CH_CONFIG.selects["flights"], "main_purpose", "main_result"]))
        rows = self._query_rows(
            "flight_list",
            keys,
            self._flight_where(f),
            order_by=[("date", "asc")],
        )
        return _filter_rows_by_time(rows, f, FLIGHT_FIELDS["time_range"])

    def _timeline_flights(self, f: FilterRequest) -> list[dict[str, Any]]:
        # Kept for compatibility; timeline normally reuses the shared dataset.
        keys = list(dict.fromkeys([*CH_CONFIG.selects["timeline_flights"], "main_purpose", "main_result"]))
        rows = self._query_rows(
            "flight_list",
            keys,
            self._flight_where(f),
            order_by=[("date", "asc")],
        )
        return _filter_rows_by_time(rows, f, FLIGHT_FIELDS["time_range"])

    def _events(self, f: FilterRequest, max_rows: int | None = None) -> list[dict[str, Any]]:
        keys = CH_CONFIG.selects["events"]
        rows = self._query_rows(
            "events",
            keys,
            self._event_where(f),
            order_by=[("date", "desc"), ("timestamp", "desc")]
            if self._physical("events", "timestamp")
            else [("date", "desc")],
            limit=None if _has_time_filter(f) else max_rows,
        )
        rows = _filter_rows_by_time(
            rows,
            f,
            EVENT_FIELDS["timestamp"],
            date_field=EVENT_FIELDS["date"],
            time_field=EVENT_FIELDS["time"],
        )
        return rows[:max_rows] if max_rows is not None else rows

    def _ammunition(self, f: FilterRequest) -> list[dict[str, Any]]:
        return self._query_rows(
            "ammunition_expenditures",
            CH_CONFIG.selects["ammunition_expenditures"],
            self._date_where("ammunition_expenditures", f),
            order_by=[("date", "asc")],
        )

    def _lost_devices(self, f: FilterRequest) -> list[dict[str, Any]]:
        rows = self._query_rows(
            "lost_devices",
            CH_CONFIG.selects["lost_devices"],
            self._date_where("lost_devices", f),
            order_by=[("date", "desc"), ("time", "desc")]
            if self._physical("lost_devices", "time")
            else [("date", "desc")],
        )
        return _filter_rows_by_time(
            rows,
            f,
            None,
            date_field=LOST_DEVICE_FIELDS["date"],
            time_field=LOST_DEVICE_FIELDS["time"],
        )

    def _lost_flights(self, f: FilterRequest) -> list[dict[str, Any]]:
        rows = self._query_rows(
            "flight_list",
            CH_CONFIG.selects["lost_flights"],
            self._flight_where(f, include_excluded=True),
            order_by=[("date", "desc")],
        )
        return _filter_rows_by_time(rows, f, FLIGHT_FIELDS["time_range"])

    def _dataset_uncached(self, f: FilterRequest):
        flights = self._flights(f)
        events = self._events(f)
        ammo = self._ammunition(f)
        flight_ids = _flight_ids(flights, FLIGHT_FIELDS["flight_id"])

        if f.unit or f.class_name or f.result:
            matching_event_ids = _flight_ids(events, EVENT_FIELDS["flight_id"])
            flight_ids &= matching_event_ids
            flights = _restrict_by_ids(flights, flight_ids, FLIGHT_FIELDS["flight_id"])

        # Always keep event/ammunition populations aligned with the filtered flights.
        events = _restrict_by_ids(events, flight_ids, EVENT_FIELDS["flight_id"]) if flight_ids else []
        ammo = _restrict_by_ids(ammo, flight_ids, AMMO_FIELDS["flight_id"]) if flight_ids else []
        return flights, events, ammo

    def _dataset(self, f: FilterRequest):
        from app.services.analytics import cache_dependencies, cache_ttl, semantic_signature
        return cache.get_or_load(
            "clickhouse.dataset",
            {'filters': f, 'source': semantic_signature(include_kpi=False)},
            lambda: self._dataset_uncached(f),
            ttl_seconds=cache_ttl(f, CACHE_CONFIG.ttl_seconds.clickhouse_dataset),
            stale_if_error_seconds=0,
            stale_while_revalidate=False,
            dependency_tags=cache_dependencies(f),
        )

    def _distinct_strings(self, entity: str, key: str) -> list[str]:
        field = self._physical(entity, key)
        if not field:
            return []
        q = quote_identifier(field)
        table = quote_identifier(self._table(entity))
        rows = clickhouse_client.query(
            f"SELECT DISTINCT toString({q}) AS value FROM {table} "
            f"WHERE {q} IS NOT NULL AND notEmpty(trimBoth(toString({q}))) ORDER BY value"
        )
        return [str(row.get("value") or "").strip() for row in rows if str(row.get("value") or "").strip()]

    def _id_title_options(self, entity: str, id_key: str, title_key: str) -> list[dict[str, Any]]:
        id_field = self._physical(entity, id_key)
        title_field = self._physical(entity, title_key)
        time_field = self._physical(entity, "time_range")
        if not id_field or not title_field:
            return []
        qid = quote_identifier(id_field)
        qtitle = quote_identifier(title_field)
        table = quote_identifier(self._table(entity))
        title_expr = f"argMax(toString({qtitle}), {quote_identifier(time_field)})" if time_field else f"any(toString({qtitle}))"
        rows = clickhouse_client.query(
            f"SELECT toInt64({qid}) AS id, {title_expr} AS title "
            f"FROM {table} WHERE {qid} IS NOT NULL GROUP BY id ORDER BY id"
        )
        return [
            {"id": int(row["id"]), "title": str(row.get("title") or row["id"])}
            for row in rows
            if row.get("id") is not None
        ]

    def filter_options(self) -> dict[str, Any]:
        bbak = self._id_title_options("flight_list", "bbak_id", "bbak_title")
        purposes = [x for x in self._distinct_strings("flight_list", "main_purpose") if not _is_excluded_purpose(x)]
        rota = self._distinct_strings("flight_list", "rota_title")
        return {
            "direction": self._distinct_strings("flight_list", "direction"),
            "unit": self._distinct_strings("events", "units"),
            "category": self._distinct_strings("flight_list", "device_type"),
            "asset": self._distinct_strings("flight_list", "device"),
            "group": self._distinct_strings("flight_list", "crew"),
            "bbak": bbak,
            "rota": rota,
            "battalion": bbak,
            "purpose": purposes,
            "class_name": self._distinct_strings("events", "target_class"),
            "result": self._distinct_strings("events", "result"),
        }

    def kpi_options(self) -> dict[str, Any]:
        return {
            "purposes": [value for value in self._distinct_strings("flight_list", "main_purpose") if not _is_excluded_purpose(value)],
            "results": self._distinct_strings("flight_list", "main_result"),
        }

    def schema_status(self) -> dict[str, Any]:
        entities: dict[str, Any] = {}
        for entity, table in CH_CONFIG.tables.items():
            columns = clickhouse_client.table_columns(table)
            mappings: dict[str, Any] = {}
            for key in CH_FIELDS.entities.get(entity, {}):
                resolved = self._physical(entity, key)
                mappings[key] = {
                    "resolved": resolved,
                    "candidates": CH_FIELDS.candidates(entity, key),
                    "required": key in CH_CONFIG.required_fields.get(entity, []),
                }
            entities[entity] = {
                "table": table,
                "columns": sorted(columns),
                "mappings": mappings,
            }
        return {"source": "clickhouse", "entities": entities}

    def status(self) -> dict[str, Any]:
        base = clickhouse_client.status()
        if not base["connected"]:
            return base
        try:
            self._validate_required()
        except Exception as exc:
            base["connected"] = False
            base["message"] = str(exc)
        return base


clickhouse_analytics = ClickHouseAnalytics()
