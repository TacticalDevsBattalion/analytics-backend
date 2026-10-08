"""Shared server-side query, metric, KPI and restricted comparison engine."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import time
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import cmp_to_key
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from app.core.cache import cache
from app.core.config import get_backend_config, get_settings
from app.bi.formula import FormulaError, evaluate_formula, validate_formula
from app.bi.security import apply_user_data_scope
from app.bi.cache_policy import CachePolicy, date_tags, days, reporting_today

logger = logging.getLogger(__name__)


class QueryError(ValueError):
    pass


class QueryPermissionError(PermissionError):
    pass


SOURCES = {"flights": "flight_list", "events": "events", "ammunition": "ammunition_expenditures", "lost_devices": "lost_devices"}
SOURCE_FIELDS = {
    "flights": {"flight_id", "date", "crew", "device", "direction", "position", "is_effective", "device_type", "main_result", "main_purpose", "sub_department", "time_range", "bbak_id", "bbak_title", "rota_id", "rota_title", "unit_id", "unit_title"},
    "events": {"flight_id", "date", "time", "timestamp", "flight_number", "crew", "flight_purpose", "target_class", "target_id", "result", "field_200", "field_300", "direction", "id", "units", "row_hash", "device", "device_type_id", "bc_name", "bc_count"},
    "ammunition": {"flight_id", "date", "bc_name", "bc_count", "result"},
    "lost_devices": {"flight_id", "date", "time", "device", "result", "device_type", "row_hash"},
}
ALIASES = {
    "department": "department_id", "group": "group_id", "team": "team_id",
    "department_id": "bbak_id", "group_id": "rota_id", "team_id": "crew",
    "category": "device_type", "type": "device_type", "purpose": "main_purpose",
    "asset": "device", "class_name": "target_class", "unit": "units",
    "status": "result",
    "zone": "units", "target_type": "target_class", "department_category": "device_type",
}
VIRTUAL_FIELDS = {"flights": {"norm_main_result", "local_timestamp", "mission_successful"}, "events": {"norm_result", "norm_target_class", "is_personnel", "norm_bc_name", "local_timestamp"}, "ammunition": {"norm_result", "norm_bc_name", "local_timestamp"}, "lost_devices": {"norm_result", "local_timestamp"}}
INHERITED_FLIGHT_FIELDS = SOURCE_FIELDS["flights"] - {"flight_id", "date"}
ENRICHED_FIELDS = {"department_id", "group_id", "team_id", *INHERITED_FLIGHT_FIELDS, *ALIASES}
FILTER_FIELDS = set().union(*SOURCE_FIELDS.values(), *VIRTUAL_FIELDS.values(), ENRICHED_FIELDS)
DIMENSION_FIELDS = FILTER_FIELDS - {"field_200", "field_300", "bc_count", "timestamp", "time_range", "time", "local_timestamp", "mission_successful"}
NUMERIC_FIELDS = {"bc_count", "field_200", "field_300", "is_effective"}
FILTER_OPERATORS = {"eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "not_contains", "is_null", "is_not_null", "between"}
AGGREGATIONS = {"COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX", "RATIO", "FORMULA", "WEIGHTED_SUM"}
MAX_METRICS = 30
LOAD_WINDOW_DAYS = 14
MAX_DIMENSIONS = 5
MAX_GROUPS = 10000
MAX_DEPENDENCIES = 100
PRIMARY_FILTER_SOURCE = {
    **{field: "events" for field in ("result", "status", "target_class", "class_name", "target_id", "field_200", "field_300", "units", "unit", "flight_purpose", "flight_number", "id", "device_type_id", "row_hash", "time", "timestamp")},
    "bc_name": "ammunition", "bc_count": "ammunition",
    "norm_result": "events", "norm_target_class": "events", "is_personnel": "events", "norm_bc_name": "ammunition", "zone": "events", "target_type": "events",
    "mission_successful": "flights",
}
RESTRICTED_BREAKDOWN_DIMENSIONS = {"purpose", "main_purpose", "flight_purpose", "category", "device_type", "department_category", "target_type", "target_class", "class_name", "date", "direction", "zone", "units"}


def _normalized_text(value: Any) -> str | None:
    return re.sub(r"\s+", " ", str(value).strip().casefold()) if value is not None else None


def _supports_field(source: str, field: str) -> bool:
    if field in SOURCE_FIELDS[source] | VIRTUAL_FIELDS[source] | INHERITED_FLIGHT_FIELDS | {"department_id", "group_id", "team_id"}:
        return True
    alias = ALIASES.get(field)
    return _supports_field(source, alias) if alias else False


def _global_filters(dataset: dict[str, list[dict[str, Any]]], filters: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Filter the owning source, then join restrictions across common flight IDs."""
    shared, specific = [], defaultdict(list)
    for condition in filters:
        field = condition["field"]
        owner = PRIMARY_FILTER_SOURCE.get(field)
        if owner:
            specific[owner].append(condition)
        elif all(_supports_field(source, field) for source in SOURCES):
            shared.append(condition)
        else:
            supported = [source for source in SOURCES if _supports_field(source, field)]
            if len(supported) != 1:
                raise QueryError(f"Filter field has no unambiguous source: {field}")
            specific[supported[0]].append(condition)
    filtered = {source: [row for row in rows if matches_filters(row, shared)] for source, rows in dataset.items()}
    joined_ids: set[str] | None = None
    for source, conditions in specific.items():
        filtered[source] = [row for row in filtered.get(source, []) if matches_filters(row, conditions)]
        ids = {str(row["flight_id"]) for row in filtered[source] if row.get("flight_id") is not None}
        joined_ids = ids if joined_ids is None else joined_ids & ids
    if joined_ids is not None:
        filtered = {source: [row for row in rows if row.get("flight_id") is not None and str(row["flight_id"]) in joined_ids] for source, rows in filtered.items()}
    return filtered


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    return value


def _canonical(value: Any) -> str:
    # MemoryCache intentionally treats arrays as sets for legacy filters. Encode
    # the BI payload first: dimension and sort priority arrays are ordered.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=lambda item: _plain(item) if hasattr(item, "model_dump") else str(item))


def _row_value(row: Mapping[str, Any], field: str) -> Any:
    if field in row:
        return row[field]
    alias = ALIASES.get(field)
    if alias:
        return _row_value(row, alias)
    return None


def _local_timestamp(row: Mapping[str, Any], source: str) -> float | None:
    """Convert a verified source clock to epoch seconds in the reporting zone."""
    raw = row.get("time_range") if source == "flights" else row.get("timestamp")
    if isinstance(raw, datetime):
        parsed = raw
    else:
        value = str(raw or "").strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        except ValueError:
            parsed = None
        if parsed is None:
            clock = value if source == "flights" else str(row.get("time") or "").strip()
            match = re.search(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?!\d)", clock)
            row_date = _date(row.get("date"))
            if not match or row_date is None:
                return None
            try:
                hour, remainder = match[1].split(":", 1)
                parsed = datetime.fromisoformat(f"{row_date.isoformat()}T{hour.zfill(2)}:{remainder}")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(get_backend_config().analytics.timezone))
    return parsed.timestamp()


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _numeric(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def validate_filters(filters: list[Any]) -> list[dict[str, Any]]:
    from app.bi import extra_fields
    extra_fields.sync()
    if len(filters) > 100:
        raise QueryError("Too many filters")
    result = []
    for item in filters:
        item = _plain(item)
        if not isinstance(item, dict) or item.get("field") not in FILTER_FIELDS:
            raise QueryError("Unsupported filter field")
        operator = str(item.get("operator", "eq")).lower()
        if operator not in FILTER_OPERATORS:
            raise QueryError("Unsupported filter operator")
        value = item.get("value")
        if operator in {"in", "not_in", "between"}:
            if not isinstance(value, list) or len(value) > 1000 or any(isinstance(v, (dict, list)) for v in value):
                raise QueryError("Set filters require a bounded list of scalar values")
            if operator == "between" and (len(value) != 2 or any(_numeric(v) is None for v in value)):
                raise QueryError("Between requires two finite numeric bounds")
        elif isinstance(value, (dict, list)):
            raise QueryError("Filter values must be scalar")
        if operator in {"gt", "gte", "lt", "lte"} and _numeric(value) is None:
            raise QueryError("Numeric filter requires a finite number")
        result.append({"field": item["field"], "operator": operator, "value": value})
    return result


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    # Organizational IDs arrive as both Int64 values and JSON strings.
    return a == b or str(a) == str(b)


def matches_filters(row: Mapping[str, Any], filters: list[dict[str, Any]]) -> bool:
    for item in filters:
        current = _row_value(row, item["field"])
        value, operator = item["value"], item["operator"]
        if operator == "eq":
            matches = _same(current, value)
        elif operator == "neq":
            matches = current is not None and not _same(current, value)
        elif operator == "in":
            matches = any(_same(current, candidate) for candidate in value)
        elif operator == "not_in":
            matches = current is not None and not any(_same(current, candidate) for candidate in value)
        elif operator == "is_null":
            matches = current is None
        elif operator == "is_not_null":
            matches = current is not None
        elif operator in {"contains", "not_contains"}:
            matches = current is not None and (str(value).casefold() in str(current).casefold()) == (operator == "contains")
        elif operator == "between":
            current_number = _numeric(current)
            matches = current_number is not None and float(value[0]) <= current_number <= float(value[1])
        else:
            a, b = _numeric(current), _numeric(value)
            if a is None or b is None:
                matches = False
            elif operator == "gt":
                matches = a > b
            elif operator == "gte":
                matches = a >= b
            elif operator == "lt":
                matches = a < b
            else:
                matches = a <= b
        if not matches:
            return False
    return True


def _period(value: Any, granularity: str | None) -> Any:
    if not granularity:
        return value
    day = _date(value)
    if day is None:
        return None
    if granularity == "week":
        day -= timedelta(days=day.weekday())
    elif granularity == "month":
        day = day.replace(day=1)
    elif granularity == "quarter":
        day = day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)
    elif granularity == "year":
        day = day.replace(month=1, day=1)
    return day.isoformat()


def _enrich(dataset: Mapping[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Attach canonical hierarchy to all records through their stable flight ID."""
    flights = [dict(row) for row in dataset.get("flights", [])]
    index: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    inherited = sorted(INHERITED_FLIGHT_FIELDS)
    for row in flights:
        row.update(department_id=row.get("bbak_id"), group_id=row.get("rota_id"), team_id=row.get("crew"))
        row["norm_main_result"] = _normalized_text(row.get("main_result"))
        row["local_timestamp"] = _local_timestamp(row, "flights")
        flight_id = row.get("flight_id")
        if flight_id is not None:
            key = str(flight_id)
            previous = index.get(key)
            if previous and any(not _same(previous.get(field), row.get(field)) for field in ("bbak_id", "rota_id", "crew")):
                ambiguous.add(key)
            index[key] = row
    result = {"flights": flights}
    for source in SOURCES.keys() - {"flights"}:
        records = []
        for original in dataset.get(source, []):
            row = dict(original)
            row["local_timestamp"] = _local_timestamp(row, source) if source != "ammunition" else None
            row["norm_result"] = _normalized_text(row.get("result"))
            if source in {"events", "ammunition"}:
                row["norm_bc_name"] = _normalized_text(row.get("bc_name"))
            if source == "events":
                row["norm_target_class"] = _normalized_text(row.get("target_class"))
                from app.services.analytics_engine import _is_personnel_event
                row["is_personnel"] = _is_personnel_event(row)
            key = str(row.get("flight_id"))
            parent = index.get(key) if key not in ambiguous else None
            if parent:
                if source == "ammunition":
                    row["local_timestamp"] = parent.get("local_timestamp")
                row["_bi_parent_date"] = _date(parent.get("date")).isoformat() if _date(parent.get("date")) else None
                # A record cannot override its flight's organizational scope.
                for field in inherited:
                    if field in {"bbak_id", "bbak_title", "rota_id", "rota_title", "crew"} or row.get(field) is None:
                        row[field] = parent.get(field)
            else:
                row["_bi_parent_date"] = None
                for field in ("bbak_id", "rota_id", "crew"):
                    row[field] = None
            if source == "events" and row.get("main_purpose") is None:
                row["main_purpose"] = row.get("flight_purpose")
            row.update(department_id=row.get("bbak_id"), group_id=row.get("rota_id"), team_id=row.get("crew"))
            records.append(row)
        result[source] = records
    return result


def load_clickhouse_rows(start: date, end: date) -> dict[str, list[dict[str, Any]]]:
    """Use the existing transport and mapping; identifiers never come from HTTP."""
    if get_settings().active_source != "clickhouse":
        raise QueryError("BI queries require the configured ClickHouse source")
    from app.bi import extra_fields
    extra_fields.sync()
    from app.services.clickhouse_analytics import clickhouse_analytics, _SqlWhere

    dataset = {}
    for source, entity in SOURCES.items():
        keys = sorted(SOURCE_FIELDS[source])
        date_field = clickhouse_analytics._physical(entity, "date", required=True)
        rows: list[dict[str, Any]] = []
        # Long periods are read in bounded windows so a single ClickHouse query or
        # HTTP response stays well inside the execution-time and memory limits.
        window_start = start
        while window_start <= end:
            window_end = min(end, window_start + timedelta(days=LOAD_WINDOW_DAYS - 1))
            where = _SqlWhere()
            where.dates(date_field, window_start, window_end)
            rows.extend(clickhouse_analytics._query_rows(entity, keys, where))
            window_start = window_end + timedelta(days=1)
        dataset[source] = rows
    excluded = set(get_backend_config().analytics.excluded_analytics_purposes)
    dataset["flights"] = [row for row in dataset["flights"] if row.get("main_purpose") not in excluded]
    valid_ids = {str(row["flight_id"]) for row in dataset["flights"] if row.get("flight_id") is not None}
    # Child records have their own date, so their parent can be outside a daily
    # partition. Resolve all bounded links before applying hierarchy or purpose.
    # Unmatched losses remain visible to ALL, with NULL organization for viewers.
    missing_ids = sorted({str(row["flight_id"]) for source in SOURCES.keys() - {"flights"} for row in dataset[source] if row.get("flight_id") is not None and str(row["flight_id"]) not in valid_ids})
    if len(missing_ids) > 10000:
        raise QueryError("Too many unmatched child flight links; narrow the date range")
    if missing_ids:
        flight_field = clickhouse_analytics._physical("flight_list", "flight_id", required=True)
        for offset in range(0, len(missing_ids), 500):
            where = _SqlWhere()
            where.strings(flight_field, missing_ids[offset:offset + 500])
            # This bounded lookup uses only child-linked IDs, never an unfiltered
            # historical flight scan. Outside-period rows supply parent context;
            # _evaluate still filters every metric by its own source row date.
            parents = clickhouse_analytics._query_rows("flight_list", sorted(SOURCE_FIELDS["flights"]), where)
            dataset["flights"].extend({**row, "_bi_parent_context": True} for row in parents)
    included_ids = {str(row["flight_id"]) for row in dataset["flights"] if row.get("flight_id") is not None and row.get("main_purpose") not in excluded}
    for source in {"events", "ammunition"}:
        dataset[source] = [row for row in dataset[source] if str(row.get("flight_id")) in included_ids]
    return dataset


class QueryEngine:
    def __init__(self, store: Any, row_loader: Callable[[date, date], Mapping[str, list[dict[str, Any]]]] | None = None, *, cache_identity: str | None = None, cache_policy: CachePolicy | None = None, today: Callable[[], date] | None = None) -> None:
        self.store = store
        self.row_loader = row_loader or load_clickhouse_rows
        self.production_loader = row_loader is None or row_loader is load_clickhouse_rows
        # Injected data providers are isolated unless a test explicitly declares
        # a shared source identity; production uses the actual source fingerprint.
        self.cache_identity = cache_identity or ("clickhouse" if self.production_loader else uuid.uuid4().hex)
        self.cache_policy = cache_policy or CachePolicy.from_env()
        self.today = today or reporting_today

    def semantic_signature(self) -> str:
        if not self.production_loader:
            identity = {"version": 5, "source": self.cache_identity}
            if hasattr(self.store, "_connection"):
                from app.bi.kpi_policy_store import policy_fingerprint
                from app.core.kpi_configuration import current_kpi_snapshot
                snapshot = current_kpi_snapshot()
                identity.update(policy=policy_fingerprint(self.store), kpi=snapshot.fingerprint, kpi_revision=snapshot.revision)
            return hashlib.sha256(_canonical(identity).encode()).hexdigest()
        self._check_source()
        from app.bi import extra_fields
        extra_fields.sync()
        config, settings = get_backend_config(), get_settings()
        from app.core.kpi_configuration import current_kpi_snapshot
        snapshot = current_kpi_snapshot()
        identity = {name: getattr(settings, name, None) for name in ("active_source", "clickhouse_url", "clickhouse_host", "clickhouse_port", "clickhouse_database", "clickhouse_user")}
        from app.bi.kpi_policy_store import policy_fingerprint
        payload = {"version": 5, "source": identity, "clickhouse": _plain(config.clickhouse), "mappings": _plain(config.clickhouse_fields), "analytics": _plain(config.analytics), "kpi_revision": snapshot.revision, "kpi_fingerprint": snapshot.fingerprint, "policy": policy_fingerprint(self.store), "extra_fields": extra_fields.signature()}
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()

    def _check_source(self) -> None:
        if self.production_loader and get_settings().active_source != "clickhouse":
            raise QueryError("BI queries require the configured ClickHouse source")

    def validate_definition(self, entity: str, definition: Any, principal: Any) -> None:
        """Validate a proposed definition and dependency graph before persistence."""
        if entity == "category_weights":
            item = _plain(definition)
            for existing in self.store.list("category_weights"):
                existing = _plain(existing)
                if existing.get("id") != item.get("id") and existing.get("weight_set_id", "target_equivalent") == item.get("weight_set_id", "target_equivalent") and _normalized_text(existing["category"]) == _normalized_text(item["category"]):
                    raise QueryError("A category has only one weight in each weight set")
            return
        if entity not in {"metrics", "kpis"}:
            return
        definitions, _ = self._definitions()
        item = dict(_plain(definition))
        existing = definitions.get(item["key"])
        if existing and existing.get("id") != item.get("id"):
            raise QueryError("Metric and KPI keys must be globally unique")
        if entity == "kpis":
            item.update(kind="kpi", aggregation="FORMULA", source="flights")
        definitions[item["key"]] = item
        self._validated({"metrics": [{"key": item["key"]}], "date_range": {"from": "2026-01-01", "to": "2026-01-01"}}, principal, definitions)

    def preview(self, entity: str, definition: Any, query: Any, principal: Any) -> dict[str, Any]:
        if entity not in {"metrics", "kpis"}:
            raise QueryError("Only metric and KPI definitions can be previewed")
        self._require(principal, "metric.manage" if entity == "metrics" else "kpi.manage")
        from app.bi.models import ENTITY_MODELS

        draft = ENTITY_MODELS[entity].model_validate(_plain(definition))
        self.validate_definition(entity, draft, principal)
        definitions, weights = self._definitions()
        item = dict(_plain(draft))
        if entity == "kpis":
            item.update(kind="kpi", aggregation="FORMULA", source="flights")
        definitions[item["key"]] = item
        query = dict(_plain(query))
        query["metrics"] = [{"key": item["key"]}]
        validated = self._validated(query, principal, definitions)
        return self._evaluate(validated, principal, definitions, weights, self._load(validated, principal), apply_scope=False)

    def _definitions(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        definitions = {_plain(item)["key"]: dict(_plain(item)) for item in self.store.list("metrics") if _plain(item).get("is_active", True)}
        for item in self.store.list("kpis"):
            item = dict(_plain(item))
            if not item.get("is_active", True):
                continue
            item.setdefault("aggregation", "FORMULA")
            item.setdefault("source", "flights")
            item["kind"] = "kpi"
            if item["key"] in definitions:
                raise QueryError("Metric and KPI keys must be globally unique")
            definitions[item["key"]] = item
        sets = {item["id"]: item for model in self.store.list("weight_sets") if (item := dict(_plain(model)))}
        weights = []
        seen = set()
        for model in self.store.list("category_weights"):
            item = dict(_plain(model))
            item.setdefault("weight_set_id", "target_equivalent")
            identity = (item["weight_set_id"], _normalized_text(item["category"]))
            if identity in seen:
                raise QueryError("Duplicate normalized category in a weight set")
            seen.add(identity)
            if sets:
                config = sets.get(item["weight_set_id"])
                if not config or not config.get("is_active", True):
                    continue
                item["_weight_set_revision"] = config.get("revision")
            weights.append(item)
        return definitions, weights

    @staticmethod
    def _require(principal: Any, permission: str) -> None:
        if not principal.has(permission):
            raise QueryPermissionError(f"Missing permission: {permission}")

    def _validated(self, request: Any, principal: Any, definitions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        self._require(principal, "metric.view")
        request = dict(_plain(request))
        refs = request.get("metrics", [])
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_METRICS:
            raise QueryError("Query requires between 1 and 30 metrics")
        keys = [item.get("key") if isinstance(item, dict) else str(item) for item in refs]
        if len(keys) != len(set(keys)):
            raise QueryError("Metric references must be unique")
        checked: set[str] = set()
        visiting: set[str] = set()
        metric_sources: set[str] = set()

        def visit(key: str, depth: int = 0) -> None:
            if depth > 16 or len(checked | visiting) > MAX_DEPENDENCIES:
                raise QueryError("Metric dependency graph exceeds supported limits")
            if key in visiting:
                raise QueryError("Metric dependencies contain a cycle")
            if key in checked:
                return
            metric = definitions.get(key)
            if not metric:
                raise QueryError(f"Unknown metric: {key}")
            for permission in metric.get("permissions", []):
                self._require(principal, permission)
            if metric.get("kind") == "kpi":
                self._require(principal, "kpi.view")
            if metric.get("aggregation") not in AGGREGATIONS:
                raise QueryError(f"Unsupported metric aggregation: {key}")
            source = metric.get("source", "flights")
            if source not in SOURCES:
                raise QueryError("Unsupported metric source")
            if metric["aggregation"] not in {"FORMULA", "RATIO"}:
                metric_sources.add(source)
            field = metric.get("field")
            if field and not _supports_field(source, field):
                raise QueryError(f"Unsupported metric field: {field}")
            if metric["aggregation"] in {"SUM", "AVG", "MIN", "MAX"} and field not in NUMERIC_FIELDS:
                raise QueryError("Numeric aggregation requires a numeric field")
            if metric["aggregation"] == "WEIGHTED_SUM" and field is not None and field not in NUMERIC_FIELDS:
                raise QueryError("Weighted sum requires a numeric field or row count")
            if metric.get("category_mode", "ALL") not in {"ALL", "GENERAL", "SEPARATE"}:
                raise QueryError("Unsupported category mode")
            if metric["aggregation"] == "WEIGHTED_SUM" or metric.get("category_mode", "ALL") != "ALL":
                weight_sets = [_plain(item) for item in self.store.list("weight_sets")]
                if weight_sets and not any(item["id"] == metric.get("weight_set_id", "target_equivalent") and item.get("is_active", True) for item in weight_sets):
                    raise QueryError("Metric requires an active weight set")
            if not _supports_field(source, metric.get("category_field", "category")):
                raise QueryError("Unsupported weight category field")
            if metric["aggregation"] == "COUNT_DISTINCT" and not field:
                raise QueryError("COUNT_DISTINCT requires a field")
            definition_filters = validate_filters(metric.get("filters", []))
            if metric["aggregation"] not in {"FORMULA", "RATIO"} and any(not _supports_field(source, condition["field"]) for condition in definition_filters):
                raise QueryError("Metric filter field is unavailable for its source")
            visiting.add(key)
            dependencies = []
            if metric["aggregation"] == "FORMULA":
                dependencies = set(validate_formula(metric.get("formula")))
                if metric.get("kind") == "kpi":
                    declared = {item["key"] for item in metric.get("metrics", [])}
                    if declared and not dependencies.issubset(declared):
                        raise QueryError("KPI formula refers to an undeclared metric")
                    dependencies |= declared
                    metric_weights = metric.get("weights", {})
                    if not set(metric_weights).issubset(dependencies) or any(_numeric(value) is None or abs(float(value)) > 1e6 for value in metric_weights.values()):
                        raise QueryError("Invalid KPI weights")
                    normalization = metric.get("normalization", "NONE")
                    if normalization in {"PER_OPERATION", "PER_100_OPERATIONS", "PERCENT_OF_TOTAL", "RATIO"}:
                        if not metric.get("normalization_metric"):
                            raise QueryError("KPI normalization requires a denominator metric")
                        dependencies.add(metric["normalization_metric"])
                    elif normalization == "MIN_MAX":
                        minimum, maximum = _numeric(metric.get("minimum")), _numeric(metric.get("maximum"))
                        if minimum is None or maximum is None or maximum <= minimum:
                            raise QueryError("KPI min/max normalization requires an increasing finite range")
                    elif normalization != "NONE":
                        raise QueryError("Unsupported KPI normalization")
            elif metric["aggregation"] == "RATIO":
                dependencies = [metric.get("numerator"), metric.get("denominator")]
                if not all(isinstance(item, str) for item in dependencies):
                    raise QueryError("RATIO requires numerator and denominator metric keys")
            for dependency in sorted(dependencies):
                visit(dependency, depth + 1)
            visiting.remove(key)
            checked.add(key)

        for key in keys:
            visit(key)
        dimensions = request.get("dimensions", [])
        if not isinstance(dimensions, list) or len(dimensions) > MAX_DIMENSIONS:
            raise QueryError("Too many dimensions")
        fields = []
        for dimension in dimensions:
            field, granularity = dimension.get("field"), dimension.get("granularity")
            if field not in DIMENSION_FIELDS:
                raise QueryError("Unsupported dimension")
            for source in metric_sources or {"flights"}:
                if not _supports_field(source, field):
                    raise QueryError(f"Dimension {field} is unavailable for metric source {source}")
            if granularity is not None and (field != "date" or granularity not in {"day", "week", "month", "quarter", "year"}):
                raise QueryError("Unsupported date granularity")
            fields.append(field)
        if len(fields) != len(set(fields)):
            raise QueryError("Dimensions must be unique")
        if set(fields) & set(keys):
            raise QueryError("Metric keys cannot collide with dimension fields")
        request["filters"] = validate_filters(request.get("filters", []))
        range_ = request.get("date_range") or {}
        start, end = _date(range_.get("from", range_.get("date_from"))), _date(range_.get("to", range_.get("date_to")))
        if start is None or end is None or start > end or (end - start).days > 3660:
            raise QueryError("Invalid date range; maximum supported range is ten years")
        for sort in request.get("sort", []):
            if sort.get("field") not in set(fields) | set(keys) or str(sort.get("direction", "asc")).lower() not in {"asc", "desc"}:
                raise QueryError("Unsupported sort")
        top_n = request.get("top_n")
        if top_n is not None and (isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 10000):
            raise QueryError("Top N must be between 1 and 10000")
        scope_mode = request.get("data_scope", "USER_SCOPE")
        if scope_mode == "GLOBAL":
            self._require(principal, "analytics.global")
        elif scope_mode not in {"USER_SCOPE", "FIXED_SCOPE"}:
            raise QueryError("Unsupported query scope mode")
        if scope_mode == "FIXED_SCOPE" and not request.get("fixed_scope"):
            raise QueryError("Fixed scope configuration is required")
        request["metrics"] = [{"key": key} for key in keys]
        request["date_range"] = {"from": start.isoformat(), "to": end.isoformat()}
        return request

    def _load(self, request: dict[str, Any], principal: Any = None, *, comparison_context: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
        """Read only missing daily partitions, merging exact normalized records.

        Contiguous misses are read together. Parent enrichment happens before
        partitioning, so a loss retains its historical flight context on hits.
        Scope validation is done by the caller before reaching any cache layer.
        """
        self._check_source()
        start, end = (date.fromisoformat(request["date_range"][key]) for key in ("from", "to"))
        current = self.today()
        base = self._period_base(request, principal, comparison_context)
        scope = base["scope"]
        partitions: dict[date, dict[str, list[dict[str, Any]]]] = {}
        missing = []
        for day in days(start, end):
            if self.cache_policy.ttl_for_day(day, current):
                hit, value = cache.probe("bi.period", _canonical({**base, "day": day.isoformat()}), dependency_tags=[f"data:{day}", "dictionary"])
                if hit:
                    partitions[day] = value
                    continue
            missing.append(day)

        def read_missing():
            pending = []
            # Recheck after acquiring the single-flight fill lock.
            for day in missing:
                hit = False
                if self.cache_policy.ttl_for_day(day, current):
                    hit, value = cache.probe("bi.period", _canonical({**base, "day": day.isoformat()}), dependency_tags=[f"data:{day}", "dictionary"])
                    if hit:
                        partitions[day] = value
                if not hit:
                    pending.append(day)
            intervals: list[list[date]] = []
            for day in pending:
                if intervals and day == intervals[-1][-1] + timedelta(days=1):
                    intervals[-1].append(day)
                else:
                    intervals.append([day])
            for interval in intervals:
                tags = [f"data:{day}" for day in interval] + ["dictionary"]
                snapshot = cache.generations(tags, namespace="bi.period")
                links_snapshot = cache.generations(tags, namespace="bi.period.links")
                started = time.perf_counter()
                normalized = _enrich(self.row_loader(interval[0], interval[-1]))
                logger.info("bi_source_read days=%s db_ms=%.2f scope_hash=%s", len(interval), (time.perf_counter() - started) * 1000, hashlib.sha256(_canonical(scope).encode()).hexdigest()[:16])
                # Cross-day links depend on a parent date outside this partition.
                # Avoid freezing hierarchy under a child-only generation. These
                # bounded records are reloaded, and parent tags protect L1/L2.
                unsafe_days = {_date(row.get("date")) for source, rows in normalized.items() if source != "flights" for row in rows if row.get("flight_id") is not None and _date(row.get("_bi_parent_date")) != _date(row.get("date"))}
                if principal is not None and comparison_context is None:
                    normalized = self._apply_scope(normalized, principal, request)
                elif comparison_context is not None:
                    normalized = {source: [row for row in rows if matches_filters(row, comparison_context)] for source, rows in normalized.items()}
                split = {day: {source: [] for source in SOURCES} for day in interval}
                for source, rows in normalized.items():
                    for row in rows:
                        day = _date(row.get("date"))
                        if day in split and not (source == "flights" and row.get("_bi_parent_context")):
                            split[day][source].append(row)
                for day, value in split.items():
                    partitions[day] = value
                    ttl = self.cache_policy.ttl_for_day(day, current)
                    if ttl and day not in unsafe_days:
                        tag = f"data:{day}"
                        expected = {key: revision for key, revision in links_snapshot.items() if key.startswith("__") or key in {tag, "dictionary"}}
                        parents = sorted({row["_bi_parent_date"] for source, rows in value.items() if source != "flights" for row in rows if row.get("_bi_parent_date")})
                        cache.put("bi.period.links", _canonical({**base, "day": day.isoformat()}), {"parent_dates": parents}, ttl_seconds=ttl, dependency_tags=[tag, "dictionary"], expected_generations=expected)
                    if ttl and day not in unsafe_days and sum(len(rows) for rows in value.values()) <= self.cache_policy.raw_max_rows:
                        tag = f"data:{day}"
                        expected = {key: revision for key, revision in snapshot.items() if key.startswith("__") or key in {tag, "dictionary"}}
                        cache.put("bi.period", _canonical({**base, "day": day.isoformat()}), value, ttl_seconds=ttl, dependency_tags=[tag, "dictionary"], expected_generations=expected)
            return {day.isoformat(): partitions[day] for day in missing}

        if missing:
            # This lock is local/shared single flight, but the merged raw bundle
            # is never kept in Redis; only bounded daily partitions are stored.
            filled = cache.get_or_load("bi.period.fill", _canonical({**base, "missing": [day.isoformat() for day in missing]}), read_missing, ttl_seconds=0, stale_if_error_seconds=0, stale_while_revalidate=False, dependency_tags=[f"data:{day}" for day in missing] + ["dictionary"], shared=True)
            for day in missing:
                partitions[day] = filled[day.isoformat()]
        return {source: [row for day in days(start, end) for row in partitions[day].get(source, [])] for source in SOURCES}

    def _period_base(self, request, principal, comparison_context=None):
        scope = {"principal": principal.cache_key() if principal else None, "mode": request.get("data_scope", "USER_SCOPE"), "fixed": request.get("fixed_scope"), "comparison_context": comparison_context}
        return {"source": self.semantic_signature(), "scope": scope, "policy": asdict(self.cache_policy)}

    @staticmethod
    def _apply_scope(dataset: dict[str, list[dict[str, Any]]], principal: Any, request: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        from dataclasses import replace
        from app.bi.models import DataScope

        effective_principal = principal
        if request.get("data_scope") == "GLOBAL":
            if not principal.has("analytics.global"):
                raise QueryPermissionError("Missing permission: analytics.global")
            effective_principal = replace(principal, scope=DataScope(scope_type="ALL"))
        scoped = {source: apply_user_data_scope(rows, effective_principal) for source, rows in dataset.items()}
        fixed = request.get("fixed_scope") if request.get("data_scope") == "FIXED_SCOPE" else None
        if fixed:
            fixed_principal = replace(principal, scope=DataScope.model_validate(fixed))
            scoped = {source: apply_user_data_scope(rows, fixed_principal) for source, rows in scoped.items()}
        return scoped

    def _evaluate(self, request: dict[str, Any], principal: Any, definitions: dict[str, dict[str, Any]], weights: list[dict[str, Any]], dataset: dict[str, list[dict[str, Any]]], *, apply_scope: bool = True) -> dict[str, Any]:
        if apply_scope:
            dataset = self._apply_scope(dataset, principal, request)
        start, end = (_date(request["date_range"][key]) for key in ("from", "to"))
        dataset = {source: [row for row in rows if not (source == "flights" and row.get("_bi_parent_context")) and (day := _date(row.get("date"))) is not None and start <= day <= end] for source, rows in dataset.items()}
        dataset = _global_filters(dataset, [condition for condition in request["filters"] if condition["field"] != "mission_successful"])
        dimensions = request.get("dimensions", [])
        keys = [item["key"] for item in request["metrics"]]
        dependencies = self._dependencies(keys, definitions)
        if any(condition["field"] == "mission_successful" for condition in request["filters"] + [condition for metric in dependencies.values() for condition in metric.get("filters", [])]) or any(metric.get("field") == "mission_successful" for metric in dependencies.values()):
            dataset = self._mission_success_dataset(dataset)
        success_filters = [condition for condition in request["filters"] if condition["field"] == "mission_successful"]
        if success_filters:
            dataset = _global_filters(dataset, success_filters)
        used_sources: set[str] = set()
        collected: set[str] = set()

        def collect(key: str) -> None:
            if key in collected:
                return
            collected.add(key)
            metric = definitions[key]
            aggregation = metric["aggregation"]
            if aggregation in {"FORMULA", "RATIO"}:
                for condition in metric.get("filters", []):
                    owner = PRIMARY_FILTER_SOURCE.get(condition["field"])
                    if owner:
                        used_sources.add(owner)
            if aggregation == "FORMULA":
                for dependency in sorted(validate_formula(metric["formula"])):
                    collect(dependency)
                if metric.get("normalization_metric"):
                    collect(metric["normalization_metric"])
            elif aggregation == "RATIO":
                collect(metric["numerator"])
                collect(metric["denominator"])
            else:
                used_sources.add(metric.get("source", "flights"))

        for key in keys:
            collect(key)
        if not used_sources:
            used_sources.add("flights")
        grouped: dict[tuple[Any, ...], dict[str, list[dict[str, Any]]]] = {}
        if not dimensions:
            grouped[()] = dataset
        else:
            for source in sorted(used_sources):
                for row in dataset.get(source, []):
                    group = tuple(_period(_row_value(row, dim["field"]), dim.get("granularity")) for dim in dimensions)
                    # Grouping keys are scalar; unsupported data is represented as NULL.
                    group = tuple(value if isinstance(value, (str, int, float, bool, type(None))) else str(value) for value in group)
                    grouped.setdefault(group, defaultdict(list))[source].append(row)
                    if len(grouped) > MAX_GROUPS:
                        raise QueryError("Query creates too many groups; narrow filters or dates")
        rows, separate_rows = [], []
        weight_lookup = {(item.get("weight_set_id", "target_equivalent"), _normalized_text(item.get("category"))): item for item in weights}

        def canonical_field(field):
            while field in ALIASES:
                field = ALIASES[field]
            return field

        def category_config(metric, row):
            return weight_lookup.get((metric.get("weight_set_id", "target_equivalent"), _normalized_text(_row_value(row, metric.get("category_field", "category")))))

        def category_dimension(metric):
            return next((dim["field"] for dim in dimensions if canonical_field(dim["field"]) == canonical_field(metric.get("category_field", "category"))), None)
        for group, populations in grouped.items():
            resolved: dict[tuple[str, str], float | None] = {}
            contexts: dict[str, dict[str, list[dict[str, Any]]]] = {}

            def calculate(key: str, inherited_filters: tuple[dict[str, Any], ...] = ()) -> float | None:
                context_key = _canonical(inherited_filters)
                cache_key = (key, context_key)
                if cache_key in resolved:
                    return resolved[cache_key]
                if len(resolved) + len(contexts) > 512:
                    raise QueryError("Metric calculation exceeds supported context limits")
                metric = definitions[key]
                aggregation, field = metric["aggregation"], metric.get("field")
                metric_filters = validate_filters(metric.get("filters", []))
                derived_filters = (*inherited_filters, *metric_filters)
                if len(derived_filters) > 100:
                    raise QueryError("Too many combined metric filters")
                if aggregation == "FORMULA":
                    def weighted_metric(reference: str) -> float | None:
                        value = calculate(reference, derived_filters)
                        return None if value is None else value * metric.get("weights", {}).get(reference, 1)

                    result = evaluate_formula(metric["formula"], weighted_metric)
                    normalization = metric.get("normalization", "NONE")
                    if normalization in {"PER_OPERATION", "PER_100_OPERATIONS", "PERCENT_OF_TOTAL", "RATIO"}:
                        denominator = calculate(metric["normalization_metric"], derived_filters)
                        result = None if result is None or denominator in {None, 0} else result / denominator
                        if result is not None and normalization in {"PER_100_OPERATIONS", "PERCENT_OF_TOTAL"}:
                            result *= 100
                    elif normalization == "MIN_MAX" and result is not None:
                        result = min(1.0, max(0.0, (result - metric["minimum"]) / (metric["maximum"] - metric["minimum"])))
                        if metric.get("direction") == "LOWER_IS_BETTER":
                            result = 1 - result
                        if metric.get("format", {}).get("type") == "percent":
                            result *= 100
                elif aggregation == "RATIO":
                    numerator, denominator = calculate(metric["numerator"], derived_filters), calculate(metric["denominator"], derived_filters)
                    result = None if numerator is None or denominator in {None, 0} else numerator / denominator
                    if result is not None and metric.get("format", {}).get("type") == "percent":
                        result *= 100
                else:
                    if context_key not in contexts:
                        contexts[context_key] = _global_filters(populations, list(inherited_filters)) if inherited_filters else populations
                    population = contexts[context_key].get(metric.get("source", "flights"), [])
                    mode = metric.get("category_mode", "ALL")
                    if mode != "ALL":
                        def included(row):
                            config = category_config(metric, row)
                            if mode == "SEPARATE":
                                return bool(config and config.get("display_separately"))
                            if not config:
                                return True
                            if category_dimension(metric) and config.get("display_separately"):
                                return True  # This group is returned in separate_rows.
                            return config.get("include_in_general_total", True) and not config.get("excluded_from_general_total", False)

                        population = [row for row in population if included(row)]
                    records = [row for row in population if matches_filters(row, metric_filters)]
                    # Empty populations are different from a known count of zero.
                    if not population or (not records and aggregation not in {"COUNT", "COUNT_DISTINCT"}):
                        result = None
                    elif aggregation == "COUNT":
                        result = float(len(records)) if not field else float(sum(_row_value(row, field) is not None for row in records))
                    elif aggregation == "COUNT_DISTINCT":
                        result = float(len({_canonical(_row_value(row, field)) for row in records if _row_value(row, field) is not None}))
                    elif aggregation == "WEIGHTED_SUM":
                        values = []
                        for row in records:
                            config = category_config(metric, row)
                            if not config or not config.get("include_in_weighted_total", True):
                                continue
                            value = _numeric(_row_value(row, field)) if field else 1.0
                            weight = _numeric(config.get("weight"))
                            if value is not None and weight is not None:
                                values.append(value * weight)
                        result = sum(values) if values else None
                    else:
                        values = [value for row in records if (value := _numeric(_row_value(row, field))) is not None]
                        if not values:
                            result = None
                        elif aggregation == "SUM":
                            result = sum(values)
                        elif aggregation == "AVG":
                            result = sum(values) / len(values)
                        elif aggregation == "MIN":
                            result = min(values)
                        else:
                            result = max(values)
                resolved[cache_key] = _numeric(result)
                return resolved[cache_key]

            output = {dim["field"]: value for dim, value in zip(dimensions, group)}
            output.update({key: calculate(key) for key in keys})
            separate_keys = set()
            for key in keys:
                metric = definitions[key]
                category_field = category_dimension(metric)
                config = category_config(metric, {metric.get("category_field", "category"): output.get(category_field)}) if category_field else None
                if metric.get("category_mode", "ALL") in {"GENERAL", "SEPARATE"} and config and config.get("display_separately"):
                    separate_keys.add(key)
            if separate_keys:
                separate_rows.append({name: value if name not in keys or name in separate_keys else None for name, value in output.items()})
            main_keys = set(keys) - separate_keys
            if main_keys and not all(definitions[key].get("category_mode") == "SEPARATE" and output[key] is None for key in main_keys):
                rows.append({name: value if name not in keys or name in main_keys else None for name, value in output.items()})
        sorts = request.get("sort", [])
        dimension_names = [item["field"] for item in dimensions]

        def compare(a: dict[str, Any], b: dict[str, Any]) -> int:
            for sort in sorts:
                av, bv = a.get(sort["field"]), b.get(sort["field"])
                if av is None or bv is None:
                    difference = (av is None) - (bv is None)  # NULL last in both directions
                else:
                    if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
                        difference = (av > bv) - (av < bv)
                    else:
                        difference = (str(av) > str(bv)) - (str(av) < str(bv))
                    if str(sort.get("direction", "asc")).lower() == "desc":
                        difference = -difference
                if difference:
                    return difference
            aa, bb = _canonical([a.get(key) for key in dimension_names]), _canonical([b.get(key) for key in dimension_names])
            return (aa > bb) - (aa < bb)

        rows.sort(key=cmp_to_key(compare))
        total = len(rows)
        if request.get("top_n"):
            rows = rows[:request["top_n"]]
        status = "success" if any(row.get(key) is not None for row in rows + separate_rows for key in keys) else "empty"
        return {"rows": rows, "separate_rows": separate_rows, "metrics": [{name: definitions[key].get(name) for name in ("key", "title", "format", "direction")} for key in keys], "dimensions": dimension_names, "total_rows": total, "source": "clickhouse", "status": status}

    def _mission_success_dataset(self, dataset):
        """Resolve success only; private policy component metrics are unnecessary."""
        from app.bi.kpi_policy import mission_context, resolve_rule
        from app.bi.kpi_policy_store import PolicyStore
        rules = PolicyStore(self.store).all_versions() if hasattr(self.store, "_connection") else []
        events_by_flight = defaultdict(list)
        for event in dataset.get("events", []):
            if event.get("flight_id") is not None:
                events_by_flight[str(event["flight_id"])].append(event)
        legacy = None
        flights = []
        for row in dataset.get("flights", []):
            events = events_by_flight.get(str(row["flight_id"]), []) if row.get("flight_id") is not None else []
            rule = resolve_rule(rules, mission_context(row, events), today=self.today())
            if rule is not None:
                successful = row.get("is_effective") is True
            else:
                if legacy is None:
                    from app.core.kpi_configuration import current_kpi_snapshot
                    from app.services.purpose_kpi import PurposeKpiCalculator
                    legacy = PurposeKpiCalculator(current_kpi_snapshot().config)
                successful = legacy.successful(row)
            flights.append({**row, "mission_successful": successful})
        return {**dataset, "flights": flights}

    @staticmethod
    def _dependencies(keys: list[str], definitions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        selected = {}

        def collect(key):
            if key in selected:
                return
            metric = definitions[key]
            selected[key] = metric
            refs = []
            if metric["aggregation"] == "RATIO":
                refs = [metric["numerator"], metric["denominator"]]
            elif metric["aggregation"] == "FORMULA":
                refs = list(validate_formula(metric["formula"])) + [item["key"] for item in metric.get("metrics", [])]
                if metric.get("normalization_metric"):
                    refs.append(metric["normalization_metric"])
            for ref in refs:
                collect(ref)

        for key in keys:
            collect(key)
        return selected

    def _cache_context(self, request, principal, definitions, weights, cohort=None):
        keys = [ref["key"] for ref in request["metrics"]]
        dependencies = self._dependencies(keys, definitions)
        start, end = (date.fromisoformat(request["date_range"][key]) for key in ("from", "to"))
        tags = date_tags(start, end) + request.get("_cache_parent_tags", []) + ["dictionary"] + [f"{'kpi' if metric.get('kind') == 'kpi' else 'metric'}:{key}" for key, metric in dependencies.items()]
        if any(condition['field'] == 'mission_successful' for condition in request.get('filters', []) + [condition for metric in dependencies.values() for condition in metric.get('filters', [])]) or any(metric.get('field') == 'mission_successful' for metric in dependencies.values()):
            tags += ['kpi:mission', 'kpi:policy']
        relevant_weights = weights if any(metric["aggregation"] == "WEIGHTED_SUM" or metric.get("category_mode", "ALL") != "ALL" for metric in dependencies.values()) else []
        payload = {"request": request, "principal": principal.cache_key(), "definitions": dependencies, "weights": relevant_weights, "semantic": self.semantic_signature(), "cohort": cohort, "policy": asdict(self.cache_policy)}
        ttl = 0 if request.get("_cache_uncacheable") else self.cache_policy.ttl_for_range(start, end, self.today())
        return _canonical(payload), tags, ttl

    @staticmethod
    def _finish(request, definitions, rows, separate_rows=None):
        separate_rows = separate_rows or []
        keys = [ref["key"] for ref in request["metrics"]]
        dimensions = [dim["field"] for dim in request.get("dimensions", [])]

        def compare(a, b):
            for sort in request.get("sort", []):
                av, bv = a.get(sort["field"]), b.get(sort["field"])
                if av is None or bv is None:
                    difference = (av is None) - (bv is None)
                else:
                    difference = ((av > bv) - (av < bv)) if isinstance(av, (int, float)) and isinstance(bv, (int, float)) else ((str(av) > str(bv)) - (str(av) < str(bv)))
                    if sort.get("direction", "asc").lower() == "desc":
                        difference = -difference
                if difference:
                    return difference
            aa, bb = _canonical([a.get(key) for key in dimensions]), _canonical([b.get(key) for key in dimensions])
            return (aa > bb) - (aa < bb)

        rows.sort(key=cmp_to_key(compare))
        separate_rows.sort(key=cmp_to_key(compare))
        total = len(rows)
        if request.get("top_n"):
            rows = rows[:request["top_n"]]
        return {"rows": rows, "separate_rows": separate_rows, "metrics": [{name: definitions[key].get(name) for name in ("key", "title", "format", "direction")} for key in keys], "dimensions": dimensions, "total_rows": total, "source": "clickhouse", "status": "success" if any(row.get(key) is not None for row in rows + separate_rows for key in keys) else "empty"}

    def _native_count(self, request, principal, metric, weights):
        """Persist additive daily count states; never sum distinct counts/ratios."""
        from app.bi.clickhouse_aggregate import aggregate_metric
        if metric.get("aggregation") != "COUNT":
            return aggregate_metric(request, principal, metric)
        start, end = (date.fromisoformat(request["date_range"][key]) for key in ("from", "to"))
        current, partitions, missing = self.today(), {}, []
        namespace = "bi.period.aggregate"
        definitions = {metric["key"]: metric}

        def context(day):
            one_day = {**request, "date_range": {"from": day.isoformat(), "to": day.isoformat()}}
            return self._cache_context(one_day, principal, definitions, weights)

        for day in days(start, end):
            payload, tags, ttl = context(day)
            hit, rows = cache.probe(namespace, payload, dependency_tags=tags) if ttl else (False, None)
            if hit:
                partitions[day] = rows
            else:
                missing.append(day)

        def fill():
            pending = []
            for day in missing:
                payload, tags, ttl = context(day)
                hit, rows = cache.probe(namespace, payload, dependency_tags=tags) if ttl else (False, None)
                if hit:
                    partitions[day] = rows
                else:
                    pending.append(day)
            intervals = []
            for day in pending:
                if intervals and day == intervals[-1][-1] + timedelta(days=1):
                    intervals[-1].append(day)
                else:
                    intervals.append([day])
            for interval in intervals:
                tags = date_tags(interval[0], interval[-1]) + ["dictionary", f"metric:{metric['key']}"]
                snapshot = cache.generations(tags, namespace=namespace)
                interval_request = {**request, "date_range": {"from": interval[0].isoformat(), "to": interval[-1].isoformat()}}
                started = time.perf_counter()
                result = aggregate_metric(interval_request, principal, metric, daily=True)
                if result is None:
                    return None
                logger.info("bi_source_aggregate metric=%s days=%s db_ms=%.2f", metric["key"], len(interval), (time.perf_counter() - started) * 1000)
                split = {day: [] for day in interval}
                for row in result["rows"]:
                    day = _date(row.get("_day"))
                    if day in split:
                        split[day].append({key: value for key, value in row.items() if key != "_day"})
                for day, rows in split.items():
                    partitions[day] = rows
                    payload, day_tags, ttl = context(day)
                    if ttl:
                        expected = {key: value for key, value in snapshot.items() if key.startswith("__") or key in day_tags}
                        cache.put(namespace, payload, rows, ttl_seconds=ttl, dependency_tags=day_tags, expected_generations=expected)
            return {day.isoformat(): partitions[day] for day in missing}

        if missing:
            payload, tags, _ = self._cache_context(request, principal, definitions, weights)
            filled = cache.get_or_load(f"{namespace}.fill", payload, fill, ttl_seconds=0, dependency_tags=tags, stale_if_error_seconds=0, stale_while_revalidate=False)
            if filled is None:
                return None
            for day in missing:
                partitions[day] = filled[day.isoformat()]
        dimensions = [dim["field"] for dim in request.get("dimensions", [])]
        grouped = {}
        key = metric["key"]
        for day in days(start, end):
            for row in partitions[day]:
                group = _canonical([row.get(field) for field in dimensions])
                output = grouped.setdefault(group, {**row, key: None})
                if row.get(key) is not None:
                    output[key] = (output[key] or 0) + row[key]
                if len(grouped) > MAX_GROUPS:
                    raise QueryError("Query creates too many groups; narrow filters or dates")
        if not grouped and not dimensions:
            return {"rows": [{key: None}]}
        return {"rows": list(grouped.values())}

    def _aggregates(self, request, principal, definitions, weights, dataset_supplier, *, cohort=None, native=True):
        request = self._with_parent_tags(request, definitions, dataset_supplier, principal)
        dimensions = [dim["field"] for dim in request.get("dimensions", [])]
        keys = [ref["key"] for ref in request["metrics"]]
        combined, separated = {}, {}
        for key in keys:
            individual = {**request, "metrics": [{"key": key}], "sort": [], "top_n": None}
            if not self._needs_parent_context(individual, definitions):
                individual = {name: value for name, value in individual.items() if not name.startswith("_cache_")}
            payload, tags, ttl = self._cache_context(individual, principal, definitions, weights, cohort)

            def calculate():
                started = time.perf_counter()
                result = None
                if native and self.production_loader:
                    result = self._native_count(individual, principal, definitions[key], weights)
                if result is None:
                    result = self._evaluate(individual, principal, definitions, weights, dataset_supplier(), apply_scope=False)
                logger.info("bi_aggregate metric=%s aggregate_ms=%.2f", key, (time.perf_counter() - started) * 1000)
                return result

            hit, result = cache.probe("bi.metric", payload, dependency_tags=tags)
            if not hit:
                result = cache.get_or_load("bi.metric", payload, calculate, ttl_seconds=ttl, dependency_tags=tags, stale_if_error_seconds=0, stale_while_revalidate=False)
            logger.info("bi_metric_cache metric=%s cache=%s", key, "hit" if hit else "miss")
            for row in result["rows"]:
                group = _canonical([row.get(field) for field in dimensions])
                combined.setdefault(group, {field: row.get(field) for field in dimensions})[key] = row.get(key)
            for row in result.get("separate_rows", []):
                group = _canonical([row.get(field) for field in dimensions])
                separated.setdefault(group, {field: row.get(field) for field in dimensions})[key] = row.get(key)
        rows = [{**row, **{key: row.get(key) for key in keys}} for row in combined.values()]
        separate = [{**row, **{key: row.get(key) for key in keys}} for row in separated.values()]
        return self._finish(request, definitions, rows, separate)

    def _needs_parent_context(self, request, definitions):
        dependencies = self._dependencies([ref["key"] for ref in request["metrics"]], definitions)
        return any(metric.get("source", "flights") != "flights" or metric.get("field") == "mission_successful" for metric in dependencies.values()) or any(condition["field"] in PRIMARY_FILTER_SOURCE or condition["field"] == "mission_successful" for condition in request.get("filters", []) + [condition for metric in dependencies.values() for condition in metric.get("filters", [])])

    def _with_parent_tags(self, request, definitions, dataset_supplier, principal=None):
        if "_cache_parent_tags" in request:
            return request
        if not self._needs_parent_context(request, definitions):
            return {key: value for key, value in request.items() if key != "_cache_read_epoch"}
        epoch_before = request["_cache_read_epoch"] if "_cache_read_epoch" in request else cache.generations(["data:epoch"]).get("data:epoch", 0)
        start, end = (date.fromisoformat(request["date_range"][key]) for key in ("from", "to"))
        known_tags = date_tags(start, end) + ["dictionary"]
        known_before = cache.generations(known_tags)
        parent_dates = None
        if principal is not None and "_cache_read_epoch" not in request:
            base, found = self._period_base(request, principal), set()
            for day in days(start, end):
                hit, links = cache.probe("bi.period.links", _canonical({**base, "day": day.isoformat()}), dependency_tags=[f"data:{day}", "dictionary"])
                if not hit:
                    break
                found.update(links["parent_dates"])
            else:
                parent_dates = sorted(found)
        if parent_dates is None:
            dataset = dataset_supplier()
            parent_dates = sorted({row["_bi_parent_date"] for source, rows in dataset.items() if source != "flights" for row in rows if row.get("_bi_parent_date")})
        parent_tags = [f"data:{day}" for day in parent_dates]
        read_generations = cache.generations(known_tags + parent_tags)
        epoch_after = cache.generations(["data:epoch"]).get("data:epoch", 0)
        changed = epoch_before != epoch_after or any(read_generations.get(tag) != version for tag, version in known_before.items())
        # Keep the revision of prepared rows in the payload. Invalidation after
        # preparation cannot publish those rows under a future revision's key.
        prepared = {key: value for key, value in request.items() if key != "_cache_read_epoch"}
        return {**prepared, "_cache_parent_tags": parent_tags, "_cache_read_generations": read_generations, "_cache_uncacheable": changed}

    def _execute_validated(self, validated, principal, definitions, weights, dataset_supplier):
        validated = self._with_parent_tags(validated, definitions, dataset_supplier, principal)
        payload, tags, ttl = self._cache_context(validated, principal, definitions, weights)
        hit, value = cache.probe("bi.query", payload, dependency_tags=tags)
        if hit:
            return value, True
        result = cache.get_or_load("bi.query", payload, lambda: self._aggregates(validated, principal, definitions, weights, dataset_supplier), ttl_seconds=min(60, ttl), dependency_tags=tags, stale_if_error_seconds=0, stale_while_revalidate=False)
        return result, False

    def execute(self, request: Any, principal: Any) -> dict[str, Any]:
        started = time.perf_counter()
        definitions, weights = self._definitions()
        validated = self._validated(request, principal, definitions)
        self._check_source()
        dataset = None

        def supply():
            nonlocal dataset
            if dataset is None:
                dataset = self._load(validated, principal)
            return dataset

        result, hit = self._execute_validated(validated, principal, definitions, weights, supply)
        try:
            from app.core.observability import request_id
            correlation = request_id.get()
        except ImportError:
            correlation = "-"
        logger.info("bi_query request_id=%s metrics=%s date_from=%s date_to=%s scope_hash=%s cache=%s total_ms=%.2f", correlation, ",".join(ref["key"] for ref in validated["metrics"]), validated["date_range"]["from"], validated["date_range"]["to"], hashlib.sha256(_canonical(principal.cache_key()).encode()).hexdigest()[:16], "hit" if hit else "miss", (time.perf_counter() - started) * 1000)
        return result

    def batch(self, requests: list[Any], principal: Any) -> dict[str, Any]:
        if not 1 <= len(requests) <= 100:
            raise QueryError("Batch requires between 1 and 100 queries")
        definitions, weights = self._definitions()
        datasets: dict[str, dict[str, list[dict[str, Any]]]] = {}
        results = []
        for index, request in enumerate(requests):
            try:
                validated = self._validated(request, principal, definitions)
                self._check_source()
                range_key = _canonical({"date": validated["date_range"], "scope": validated.get("data_scope"), "fixed": validated.get("fixed_scope")})

                def supply():
                    if range_key not in datasets:
                        datasets[range_key] = self._load(validated, principal)
                    return datasets[range_key]

                result, _ = self._execute_validated(validated, principal, definitions, weights, supply)
                results.append({"index": index, "status": result["status"], "data": result})
            except (QueryError, FormulaError, QueryPermissionError) as exc:
                results.append({"index": index, "status": "error", "error": {"code": "forbidden" if isinstance(exc, QueryPermissionError) else "invalid_query", "message": str(exc)}})
            except Exception:
                # Transport failures are local to a widget and do not expose SQL,
                # credentials, upstream response bodies or another widget's data.
                results.append({"index": index, "status": "error", "error": {"code": "source_unavailable", "message": "ClickHouse data is unavailable"}})
        return {"results": results}

    def metadata(self, principal: Any) -> dict[str, Any]:
        self._require(principal, "metric.view")
        definitions, _ = self._definitions()
        visible = [metric for metric in definitions.values() if all(principal.has(permission) for permission in metric.get("permissions", []))]
        # Check dependencies too: a public formula must not expose a protected ref.
        accessible = []
        for metric in visible:
            try:
                self._validated({"metrics": [{"key": metric["key"]}], "date_range": {"from": "2026-01-01", "to": "2026-01-01"}}, principal, definitions)
                accessible.append(metric)
            except (QueryError, QueryPermissionError, FormulaError):
                continue
        return {"metrics": accessible, "dimensions": [{"field": field, "granularities": ["day", "week", "month", "quarter", "year"] if field == "date" else []} for field in sorted(DIMENSION_FIELDS)], "filter_fields": sorted(FILTER_FIELDS), "filter_operators": sorted(FILTER_OPERATORS), "aggregations": sorted(AGGREGATIONS), "visualizations": ["number", "line", "bar", "stacked_bar", "pie", "table", "comparison", "text"], "hierarchy": [{"level": "department", "field": "bbak_id"}, {"level": "group", "field": "rota_id"}, {"level": "team", "field": "crew"}], "formula": {"representation": "json_ast", "operations": ["add", "sub", "mul", "div", "min", "max", "abs", "neg", "coalesce", "clamp", "normalize"]}}

    def comparison(self, request: Any, principal: Any) -> dict[str, Any]:
        self._require(principal, "comparison.view")
        self._require(principal, "comparison.view_difference")
        request = dict(_plain(request))
        level = str(request.get("level", "")).lower()
        if level not in {"department", "group", "team", "category", "bbak", "crew"}:
            raise QueryError("Unsupported comparison level")
        permission_level = {"bbak": "unit", "crew": "crew"}.get(level, level)
        compatible = {"bbak": "department", "crew": "team"}.get(level)
        if not principal.has(f"comparison.{permission_level}") and not (compatible and principal.has(f"comparison.{compatible}")):
            self._require(principal, f"comparison.{permission_level}")
        context = {key: value for key, value in {"department_id": request.get("context_department_id"), "group_id": request.get("context_group_id")}.items() if value is not None}
        if request.get("context_category") is not None:
            context["category"] = request["context_category"]
        if request.get("context"):
            context.update(request["context"])
        if not isinstance(context, dict) or not set(context).issubset({"department_id", "group_id", "category"}):
            raise QueryError("Invalid comparison context")
        if level in {"group", "team"} and not context.get("department_id"):
            raise QueryError("Group and team comparisons require department context")
        if level == "team" and not context.get("group_id"):
            raise QueryError("Team comparison requires group context")
        if level == "crew" and not context.get("category"):
            raise QueryError("Crew comparison requires department category context")
        definitions, weights = self._definitions()
        validated = self._validated(request.get("query", {}), principal, definitions)
        if validated.get("dimensions"):
            self._require(principal, "comparison.view_breakdown")
            if not principal.has("comparison.view_peer_raw") and any(dim["field"] not in RESTRICTED_BREAKDOWN_DIMENSIONS for dim in validated["dimensions"]):
                raise QueryPermissionError("This breakdown dimension is unavailable for restricted comparison")
        if validated.get("data_scope", "USER_SCOPE") != "USER_SCOPE":
            raise QueryError("Comparison must inherit the user's scope")
        context_filters = validate_filters([{"field": field, "operator": "eq", "value": value} for field, value in context.items()])
        read_epoch = cache.generations(["data:epoch"]).get("data:epoch", 0)
        dataset = self._load(validated, principal, comparison_context=context_filters)
        field = {"category": "category", "bbak": "department_id", "crew": "team_id"}.get(level, f"{level}_id")
        own_id = request.get("own_id")
        if own_id is None or str(own_id).strip() == "":
            raise QueryError("Own organizational ID is required")
        own_scope = self._apply_scope(dataset, principal, validated)
        own_dataset = {source: [row for row in rows if _same(_row_value(row, field), own_id)] for source, rows in own_scope.items()}
        # The requested own ID must be in the assigned scope in this context;
        # never treat arbitrary own_id as permission to read its data.
        if not any(own_dataset.values()):
            return {"level": level.upper(), "own_id": str(own_id), "peer_id": None, "precision": str(getattr(principal, "precision", "ROUND_5")), "rows": [], "status": "empty", "raw_visible": principal.has("comparison.view_peer_raw")}
        peer_id = request.get("peer_id")
        candidates = sorted({str(_row_value(row, field)) for rows in dataset.values() for row in rows if _row_value(row, field) is not None and not _same(_row_value(row, field), own_id)})
        if peer_id is not None:
            if _same(peer_id, own_id):
                raise QueryError("Choose two different comparison areas")
            self._require(principal, "comparison.select_peer")
            if str(peer_id) not in candidates:
                raise QueryError("Selected peer is unavailable in the comparison context")
        else:
            peer_id = candidates[0] if candidates else None
        peer_dataset = {source: [row for row in rows if peer_id is not None and _same(_row_value(row, field), peer_id)] for source, rows in dataset.items()}
        comparison_query = {**validated, "top_n": None, "_cache_read_epoch": read_epoch}
        own_result = self._aggregates(comparison_query, principal, definitions, weights, lambda: own_dataset, cohort={"level": level, "id": str(own_id), "context": context, "own": True}, native=False)
        peer_result = self._aggregates(comparison_query, principal, definitions, weights, lambda: peer_dataset, cohort={"level": level, "id": str(peer_id), "context": context, "own": False}, native=False)
        precision = str(getattr(principal, "precision", "ROUND_5"))
        if precision not in {"EXACT", "ROUND_1", "ROUND_5", "RANGE"}:
            raise QueryError("Invalid server comparison precision")
        raw = principal.has("comparison.view_peer_raw")
        dimension_names = [dimension["field"] for dimension in validated.get("dimensions", [])]
        own_index = {_canonical([row.get(field) for field in dimension_names]): row for row in own_result["rows"] + own_result.get("separate_rows", [])}
        peer_index = {_canonical([row.get(field) for field in dimension_names]): row for row in peer_result["rows"] + peer_result.get("separate_rows", [])}
        rows = []
        groups = list(own_index) + [key for key in peer_index if key not in own_index]
        if validated.get("top_n"):
            groups = groups[:validated["top_n"]]
        for group in groups:
            own_row, peer_row = own_index.get(group, {}), peer_index.get(group, {})
            dims = {field: (own_row if field in own_row else peer_row).get(field) for field in dimension_names}
            for reference in validated["metrics"]:
                key = reference["key"]
                metric = definitions[key]
                own, peer = own_row.get(key), peer_row.get(key)
                percent_metric = metric.get("format", {}).get("type") == "percent"
                points = percent_metric and metric.get("percentage_difference", "relative_percent") == "percentage_points"
                difference = None
                if own is None or peer is None:
                    status, direction = "no_data", "unavailable"
                else:
                    direction = "higher" if own > peer else "lower" if own < peer else "equal"
                    if points:
                        difference, status = own - peer, "ok"
                    elif peer == 0:
                        difference, status = (0.0, "ok") if own == 0 else (None, "zero_baseline")
                    else:
                        difference, status = (own - peer) / peer * 100, "ok"
                    if difference is not None and (not math.isfinite(difference) or abs(difference) > 1e15):
                        difference, status = None, "out_of_range"
                output = {**dims, "metric": key, "direction": direction, "status": status, "difference_type": "percentage_points" if points else "relative_percent"}
                name = "difference_pp" if points else "difference_percent"
                if precision == "RANGE":
                    if difference is None:
                        output["difference_range"] = None
                    elif difference == 0:
                        output["difference_range"] = {"from": 0, "to": 0}
                    else:
                        lower = math.floor(difference / 5) * 5
                        output["difference_range"] = {"from": lower, "to": lower + 5}
                elif difference is None:
                    output[name] = None
                elif precision == "EXACT":
                    output[name] = difference
                else:
                    step = Decimal(1 if precision == "ROUND_1" else 5)
                    output[name] = float((Decimal(str(difference)) / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step)
                if raw:
                    output.update(own=own, peer=peer)
                rows.append(output)
        return {"level": level.upper(), "own_id": str(own_id), "peer_id": str(peer_id) if peer_id is not None else None, "precision": precision, "rows": rows, "status": "success" if rows else "empty", "raw_visible": raw}


def default_metric_definitions() -> list[Any]:
    """Return the deterministic metric template used for imports and parity."""
    from app.bi.models import MetricDefinition

    definitions = [
        {"id": "system-metric-flights", "key": "flights", "title": "Польоти", "aggregation": "COUNT_DISTINCT", "field": "flight_id", "direction": "HIGHER_IS_BETTER"},
        {"id": "system-metric-effective", "key": "effective", "title": "Результативні польоти", "aggregation": "COUNT_DISTINCT", "field": "flight_id", "filters": [{"field": "mission_successful", "operator": "eq", "value": True}], "direction": "HIGHER_IS_BETTER"},
        {"id": "system-metric-efficiency", "key": "efficiency", "title": "Результативність", "aggregation": "RATIO", "numerator": "effective", "denominator": "flights", "format": {"type": "percent", "decimals": 1}, "direction": "HIGHER_IS_BETTER", "percentage_difference": "percentage_points"},
        {"id": "system-metric-events", "key": "events", "title": "Події", "source": "events", "aggregation": "COUNT"},
        {"id": "system-metric-ammunition", "key": "ammunition", "title": "Витрати боєприпасів", "source": "ammunition", "aggregation": "SUM", "field": "bc_count", "direction": "LOWER_IS_BETTER"},
        {"id": "system-metric-lost", "key": "lost_devices", "title": "Втрати засобів", "source": "lost_devices", "aggregation": "COUNT", "direction": "LOWER_IS_BETTER"},
        {"id": "system-metric-weighted", "key": "weighted_result", "title": "Зважений результат", "source": "events", "aggregation": "WEIGHTED_SUM", "category_field": "target_class", "direction": "HIGHER_IS_BETTER", "description": "Сума налаштованих ваг категорій подій. Категорії без ваги та виключені категорії не входять у загальний результат."},
    ]
    config = get_backend_config().analytics
    affected, destroyed = (_normalized_text(config.result_names[key]) for key in ("affected", "destroyed"))
    target = {"source": "events", "category_field": "target_class", "category_mode": "GENERAL", "weight_set_id": "target_equivalent", "direction": "HIGHER_IS_BETTER"}
    semantics = [
        {"key": "detected", "title": "Виявлено", "source": "ammunition", "aggregation": "SUM", "field": "bc_count", "filters": [{"field": "norm_bc_name", "operator": "eq", "value": _normalized_text(config.detected_ammunition_name)}], "direction": "HIGHER_IS_BETTER"},
        {"key": "affected", "title": "Уражено ОС", "source": "events", "aggregation": "SUM", "field": "field_300", "filters": [{"field": "is_personnel", "operator": "eq", "value": True}], "direction": "HIGHER_IS_BETTER", "description": "Кількість ураженого особового складу за окремим числовим полем подій, як у поточних KPI-картках."},
        {"key": "destroyed", "title": "Знищено ОС", "source": "events", "aggregation": "SUM", "field": "field_200", "filters": [{"field": "is_personnel", "operator": "eq", "value": True}], "direction": "HIGHER_IS_BETTER"},
        {**target, "key": "target_affected", "title": "Уражені цілі", "aggregation": "COUNT", "filters": [{"field": "norm_result", "operator": "eq", "value": affected}]},
        {**target, "key": "target_destroyed", "title": "Знищені цілі", "aggregation": "COUNT", "filters": [{"field": "norm_result", "operator": "eq", "value": destroyed}]},
        {**target, "key": "target_results", "title": "Уражено + знищено", "aggregation": "FORMULA", "formula": {"op": "add", "args": [{"metric": "target_affected"}, {"metric": "target_destroyed"}]}},
        {**target, "key": "targets", "title": "Унікальні цілі", "aggregation": "COUNT_DISTINCT", "field": "target_id", "description": "Кількість різних наявних ідентифікаторів цілей у подіях. Події без ідентифікатора не створюють вигаданих цілей."},
        {**target, "key": "flights_for_affected", "title": "Вильоти з ураженням", "aggregation": "COUNT_DISTINCT", "field": "flight_id", "filters": [{"field": "norm_result", "operator": "eq", "value": affected}]},
        {**target, "key": "flights_for_destroyed", "title": "Вильоти зі знищенням", "aggregation": "COUNT_DISTINCT", "field": "flight_id", "filters": [{"field": "norm_result", "operator": "eq", "value": destroyed}]},
        {**target, "key": "flights_for_result", "title": "Вильоти з результатом", "aggregation": "COUNT_DISTINCT", "field": "flight_id", "filters": [{"field": "norm_result", "operator": "in", "value": [affected, destroyed]}]},
    ]
    for suffix, label, denominator in (("affected", "ураження", "target_affected"), ("destroyed", "знищення", "target_destroyed"), ("result", "результат", "target_results")):
        semantics.append({**target, "key": f"flights_per_{suffix}", "title": f"Середні вильоти на {label}", "aggregation": "RATIO", "numerator": f"flights_for_{suffix}", "denominator": denominator, "format": {"type": "number", "decimals": 2}, "direction": "LOWER_IS_BETTER", "description": "Унікальні вильоти, пов’язані через стабільний ідентифікатор із відповідними записами результату, поділені на кількість цих записів. Не включає непов’язані вильоти і не оцінює невідомі спроби до успіху."})
    definitions.extend({"id": f"system-metric-{item['key']}", **item} for item in semantics)
    return [MetricDefinition.model_validate(item) for item in definitions]


def seed_default_metrics(store: Any) -> None:
    """Import editable defaults without overwriting existing definitions."""
    store.seed_definitions(metrics=default_metric_definitions())
