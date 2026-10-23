"""Crew ranking inputs from the "ranking.groups" SQL dataset instead of every record.

The engine loads every flight, event and loss of the period into Python (half a
million records for four months) only to count them per crew and day. The dataset,
kept in the BI store and run on ClickHouse (app.bi.sql_datasets), groups the flights
by everything their success depends on and returns a few thousand groups; the same
KPI rules as the engine then decide each group's success, so the numbers are the
engine's. Only the system flight, success and loss metrics and the filters the
dataset takes as parameters are read this way; anything else uses the engine.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

DATASET = "ranking.groups"
NAMESPACE = "bi.ranking"
VERSION = 2
SYSTEM_METRICS = ("flights", "effective", "penalized", "effective_net", "lost_devices")
DEFINITION_FIELDS = ("aggregation", "field", "filters", "formula", "source", "category_mode", "normalization", "normalization_metric", "weights", "kind")
# Filter field → the dataset's parameter prefix (<prefix>_in, <prefix>_out).
LIST_FILTERS = {"category": "category", "type": "category", "device_type": "category", "purpose": "purpose", "main_purpose": "purpose",
                "asset": "device", "device": "device", "direction": "direction"}
CLOCK_FILTERS = {"gte": "time_from", "lte": "time_to"}


def supported(service, definitions: dict[str, dict[str, Any]], filters: list[dict[str, Any]]) -> bool:
    from app.bi.engine import _plain, default_metric_definitions
    if not getattr(service, "production_loader", False):
        return False
    defaults = {item.key: _plain(item) for item in default_metric_definitions() if item.key in SYSTEM_METRICS}
    if any(key not in definitions or any(definitions[key].get(name) != defaults[key].get(name) for name in DEFINITION_FIELDS) for key in SYSTEM_METRICS):
        return False
    if _filter_values(filters) is None:
        return False
    from app.bi.sql_datasets import DatasetNotFound, get_dataset_store
    try:
        get_dataset_store().published(DATASET)
    except DatasetNotFound:
        return False  # An archived dataset hands the ranking back to the engine.
    return True


def _filter_values(filters: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The dataset's filter parameters, or None when a filter cannot be expressed by them."""
    values: dict[str, Any] = {f"{prefix}_{side}": [] for prefix in set(LIST_FILTERS.values()) for side in ("in", "out")}
    values.update(time_from=None, time_to=None)
    allowed: dict[str, set[str]] = {}
    for condition in filters:
        field, operator, value = condition.get("field"), condition.get("operator"), condition.get("value")
        if field == "local_timestamp":
            if operator not in CLOCK_FILTERS or isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            name = CLOCK_FILTERS[operator]
            current = values[name]
            values[name] = float(value) if current is None else (max if operator == "gte" else min)(current, float(value))
            continue
        if field not in LIST_FILTERS:
            return None
        items = value if operator in {"in", "not_in"} else [value]
        if operator not in {"eq", "neq", "in", "not_in"} or not isinstance(items, list) or any(item is None for item in items):
            return None  # NULL matching has no parameter.
        prefix, texts = LIST_FILTERS[field], {str(item) for item in items}
        if operator in {"eq", "in"}:
            allowed[prefix] = texts if prefix not in allowed else allowed[prefix] & texts
            if not allowed[prefix]:
                return None  # Nothing can match; the engine returns its empty result.
        else:
            values[f"{prefix}_out"] = sorted(set(values[f"{prefix}_out"]) | texts)
    for prefix, texts in allowed.items():
        values[f"{prefix}_in"] = sorted(texts)
    return values


def _groups(filters: list[dict[str, Any]], start: date, end: date, detail: bool) -> list[dict[str, Any]]:
    from app.bi.sql_datasets import get_dataset_store
    from app.core.config import get_backend_config

    config = get_backend_config().analytics
    started = time.perf_counter()
    rows = get_dataset_store().run(DATASET, {"date_from": start.isoformat(), "date_to": end.isoformat(), "tz": config.timezone,
                                             "excluded_purposes": list(config.excluded_analytics_purposes), "detail": int(detail), **_filter_values(filters)})
    logger.info("ranking_groups days=%s rows=%s db_ms=%.2f", (end - start).days + 1, len(rows), (time.perf_counter() - started) * 1000)
    return rows


def cached(service, body: dict[str, Any], filters: list[dict[str, Any]], settings, compute):
    """The brigade-wide ranking rows, kept until a day of the period or the settings change.

    The rows hold no viewer's scope, so one entry serves everyone. The source signature
    covers the KPI rules and source settings; the dataset version and the ranking
    settings are part of the key.
    """
    from app.bi.cache_policy import date_tags
    from app.bi.engine import _canonical
    from app.bi.sql_datasets import get_dataset_store
    from app.core.cache import cache

    start, end = (date.fromisoformat(str(body["date_range"][key])[:10]) for key in ("from", "to"))
    today = service.today()
    # Rules apply by their validity on the reporting day, so a new day is a new entry.
    payload = _canonical({"version": VERSION, "dataset": get_dataset_store().published(DATASET)["version"], "source": service.semantic_signature(),
                          "from": start.isoformat(), "to": end.isoformat(), "filters": filters, "settings": settings.model_dump(mode="json"), "today": today.isoformat()})
    ttl = service.cache_policy.ttl_for_range(start, end, today)
    return cache.get_or_load(NAMESPACE, payload, compute, ttl_seconds=ttl, dependency_tags=[*date_tags(start, end), "dictionary"])


def read(service, body: dict[str, Any], filters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Daily rows (category × battalion × crew × purpose × day) and flights per company.

    The rows carry the engine's flights, effective_net and lost_devices for the ranking.
    """
    from app.bi.kpi_policy import mission_context, resolve_rule
    from app.bi.kpi_policy_store import PolicyStore
    from app.core.kpi_configuration import current_kpi_snapshot
    from app.services.purpose_kpi import PurposeKpiCalculator

    start, end = (date.fromisoformat(str(body["date_range"][key])[:10]) for key in ("from", "to"))
    rules = PolicyStore(service.store).all_versions() if hasattr(service.store, "_connection") else []
    groups = _groups(filters, start, end, bool(rules))
    legacy = PurposeKpiCalculator(current_kpi_snapshot().config)
    today = service.today()
    daily: dict[tuple, dict[str, Any]] = {}
    companies: dict[tuple, int] = defaultdict(int)

    def row(key: tuple) -> dict[str, Any]:
        if key not in daily:
            category, department, team, purpose, day = key
            daily[key] = {"category": category, "department": department, "team": team, "purpose": purpose, "date": day, "flights": 0, "effective_net": 0.0, "lost_devices": 0}
        return daily[key]

    outcomes: dict[tuple, int] = {}

    def outcome(group: dict[str, Any]) -> int:
        """+1 for a successful flight, -1 for a penalized one, 0 otherwise."""
        flight = {"main_purpose": group["purpose"], "is_effective": group["is_effective"], "main_result": group["main_result"], "device_type": group["category"],
                  "bbak_id": group["department"], "device": group.get("device"), "date": group["day"]}
        events = [{"result": result} for result in group["results"]] + [{"units": units} for units in group.get("units", [])]
        losses = [{}] if group["lost"] else []
        rule = resolve_rule(rules, mission_context(flight, events), today=today) if rules else None
        if rule is not None:
            return int(group["is_effective"] is True)
        return int(legacy.successful(flight, events, losses)) - int(legacy.penalized(flight, events, losses))

    for group in groups:
        key = (group["category"], group["department"], group["team"], group["purpose"], group["day"])
        if group["kind"] == "losses":
            row(key)["lost_devices"] += int(group["lost_devices"])
            continue
        flights = int(group["flights"])
        # Without zone or device rules the outcome depends only on what the KPI rules read.
        signature = (group["purpose"], group["is_effective"], group["main_result"], bool(group["lost"]), tuple(group["results"]))
        if rules:
            signature += (group["category"], group["department"], group["day"], group.get("device"), tuple(group.get("units", ())))
        if signature not in outcomes:
            outcomes[signature] = outcome(group)
        target = row(key)
        target["flights"] += flights
        target["effective_net"] += flights * outcomes[signature]
        companies[(group["department"], group["company"], group["team"])] += flights
    return list(daily.values()), [{"department": department, "group": company, "team": team, "flights": flights} for (department, company, team), flights in companies.items()]
