from __future__ import annotations

from collections import Counter, defaultdict
import re
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.api.models import EventRow, FilterRequest, Metric, MetricBreakdownItem, TimelinePoint
from app.core.config import get_backend_config
from app.core.kpi_configuration import KpiConfiguration, kpi_fingerprint
from .purpose_kpi import PurposeKpiCalculator, weighted_percentage

CONFIG = get_backend_config()
ANALYTICS_CONFIG = CONFIG.analytics
CH_CONFIG = CONFIG.clickhouse

KYIV_TZ = ZoneInfo(ANALYTICS_CONFIG.timezone)
DAY_START = time.fromisoformat(ANALYTICS_CONFIG.day_period.start)
DAY_END = time.fromisoformat(ANALYTICS_CONFIG.day_period.end)

PERSONNEL_TARGET_CLASSES = {x.casefold() for x in ANALYTICS_CONFIG.personnel_target_classes}
PERSONNEL_TARGET_PREFIXES = {x.casefold() for x in ANALYTICS_CONFIG.personnel_target_prefixes}
FLIGHT_PURPOSE_GROUPS = ANALYTICS_CONFIG.flight_purpose_groups
EXCLUDED_ANALYTICS_PURPOSES = {
    x.casefold() for x in ANALYTICS_CONFIG.excluded_analytics_purposes
}
STRIKE_DEVICE_TYPES = {x.casefold() for x in ANALYTICS_CONFIG.strike_device_types}
LOST_DEVICES_SENTINEL = ANALYTICS_CONFIG.lost_devices_sentinel

# Canonical row keys used inside the analytics engine. Physical database columns
# are resolved and aliased to these keys by the ClickHouse adapter.
FLIGHT_FIELDS = {
    "flight_id": "flight_id",
    "date": "date",
    "crew": "crew",
    "device": "device",
    "direction": "direction",
    "position": "position",
    "is_effective": "is_effective",
    "device_type": "device_type",
    "main_result": "main_result",
    "main_purpose": "main_purpose",
    "sub_department": "sub_department",
    "time_range": "time_range",
}
EVENT_FIELDS = {
    "flight_id": "flight_id",
    "date": "date",
    "time": "time",
    "timestamp": "timestamp",
    "flight_number": "flight_number",
    "crew": "crew",
    "flight_purpose": "flight_purpose",
    "target_class": "target_class",
    "target_id": "target_id",
    "result": "result",
    "field_200": "field_200",
    "field_300": "field_300",
    "direction": "direction",
    "id": "id",
    "units": "units",
    "row_hash": "row_hash",
    "device": "device",
    "device_type_id": "device_type_id",
    "bc_name": "bc_name",
    "bc_count": "bc_count",
}
LOST_DEVICE_FIELDS = {
    "flight_id": "flight_id",
    "date": "date",
    "time": "time",
    "device": "device",
    "result": "result",
    "device_type": "device_type",
    "row_hash": "row_hash",
}
AMMO_FIELDS = {
    "flight_id": "flight_id",
    "date": "date",
    "bc_name": "bc_name",
    "bc_count": "bc_count",
    "result": "result",
}


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).replace(",", ".")))
        except (TypeError, ValueError):
            return 0


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _is_excluded_purpose(value: Any) -> bool:
    return _normalized_text(value) in EXCLUDED_ANALYTICS_PURPOSES


def _is_personnel_event(row: dict[str, Any]) -> bool:
    raw = str(row.get(EVENT_FIELDS["target_class"]) or "").strip().casefold()
    normalized = re.sub(r"[\s_\-/]+", " ", raw).strip()
    if normalized in PERSONNEL_TARGET_CLASSES:
        return True
    return any(
        normalized == prefix or normalized.startswith(prefix + " ")
        for prefix in PERSONNEL_TARGET_PREFIXES
    )


def _flight_purpose_group(value: Any, device_type: Any = None) -> tuple[str, str]:
    purpose = _normalized_text(value)
    normalized_device_type = _normalized_text(device_type)

    followup = ANALYTICS_CONFIG.followup_recon
    if (
        purpose == _normalized_text(followup.purpose)
        and normalized_device_type in STRIKE_DEVICE_TYPES
    ):
        return followup.key, followup.label

    groups_by_key = {group.key: group for group in FLIGHT_PURPOSE_GROUPS}
    for group in FLIGHT_PURPOSE_GROUPS:
        normalized_purposes = {_normalized_text(x) for x in group.purposes}
        if purpose in normalized_purposes:
            return group.key, group.label

    fallback = ANALYTICS_CONFIG.fallback_purpose_matching
    if fallback.recon_contains in purpose or purpose in {
        _normalized_text(x) for x in fallback.recon_exact
    }:
        group = groups_by_key[fallback.recon_group_key]
        return group.key, group.label

    if any(fragment in purpose for fragment in fallback.logistics_contains):
        group = groups_by_key[fallback.logistics_group_key]
        return group.key, group.label

    if fallback.mining_contains in purpose:
        group = groups_by_key[fallback.mining_group_key]
        return group.key, group.label

    return ANALYTICS_CONFIG.other_group.key, ANALYTICS_CONFIG.other_group.label


def _unique_flight_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    without_id: list[dict[str, Any]] = []
    for row in rows:
        flight_id = str(row.get(FLIGHT_FIELDS["flight_id"]) or "").strip()
        if flight_id:
            by_id.setdefault(flight_id, row)
        else:
            without_id.append(row)
    return [*by_id.values(), *without_id]


def _device_type_by_flight(rows: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in _unique_flight_rows(rows):
        flight_id = str(row.get(FLIGHT_FIELDS["flight_id"]) or "").strip()
        if not flight_id:
            continue
        result[flight_id] = (
            str(row.get(FLIGHT_FIELDS["device_type"]) or "").strip()
            or ANALYTICS_CONFIG.unknown_device_type_label
        )
    return result


def _breakdown_items(
    values: dict[str, float],
    labels: dict[str, str] | None = None,
    unit: str | None = None,
) -> list[MetricBreakdownItem]:
    labels = labels or {}
    rows = [
        MetricBreakdownItem(
            key=key,
            label=labels.get(key, key),
            value=round(value, 1) if unit == "%" else value,
            unit=unit,
        )
        for key, value in values.items()
        if value > 0
    ]
    rows.sort(key=lambda item: (-item.value, item.label.casefold()))
    return rows


def _has_time_filter(f: FilterRequest) -> bool:
    return f.time_from is not None or f.time_to is not None


def _requested_window(f: FilterRequest) -> tuple[datetime, datetime]:
    start = datetime.combine(f.date_from, f.time_from or time.min).replace(tzinfo=KYIV_TZ)
    end = datetime.combine(f.date_to, f.time_to or time.max).replace(tzinfo=KYIV_TZ)
    return start, end


def _parse_row_datetime(
    row: dict[str, Any],
    field: str | None,
    *,
    date_field: str | None = None,
    time_field: str | None = None,
) -> datetime | None:
    raw = row.get(field) if field else None
    if not raw and date_field:
        d = str(row.get(date_field) or "").strip()
        t = str(row.get(time_field) or "").strip() if time_field else ""
        if d:
            raw = f"{d}T{t}" if t else d
    if not raw:
        return None
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KYIV_TZ)
    return parsed.astimezone(KYIV_TZ)


def _filter_rows_by_time(
    rows: list[dict[str, Any]],
    f: FilterRequest,
    field: str | None,
    *,
    date_field: str | None = None,
    time_field: str | None = None,
) -> list[dict[str, Any]]:
    if not _has_time_filter(f):
        return rows
    start, end = _requested_window(f)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        ts = _parse_row_datetime(row, field, date_field=date_field, time_field=time_field)
        if ts is not None and start <= ts <= end:
            filtered.append(row)
    return filtered


def _average_period_days(f: FilterRequest) -> float:
    if _has_time_filter(f):
        start, end = _requested_window(f)
        duration_seconds = max(0.0, (end - start).total_seconds())
        return max(1.0, duration_seconds / 86400.0)
    calendar_days = (f.date_to - f.date_from).days + 1
    return float(max(1, calendar_days))


def _lost_device_type_label(value: Any) -> str | None:
    return ANALYTICS_CONFIG.lost_device_type_aliases.get(_normalized_text(value))


def _flight_ids(rows: list[dict[str, Any]], id_field: str = "flight_id") -> set[str]:
    return {str(x.get(id_field)) for x in rows if x.get(id_field)}


def _restrict_by_ids(
    rows: list[dict[str, Any]], ids: set[str], id_field: str = "flight_id"
) -> list[dict[str, Any]]:
    if not ids:
        return []
    return [x for x in rows if x.get(id_field) and str(x.get(id_field)) in ids]


class AnalyticsEngine:
    """Calculation layer independent from the physical analytics data source."""

    def _dataset(self, f: FilterRequest):
        raise NotImplementedError

    def _lost_devices(self, f: FilterRequest) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _lost_flights(self, f: FilterRequest) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _lost_device_rows(self, f: FilterRequest) -> list[EventRow]:
        clean_filter = f.model_copy(update={"result": []})
        losses = self._lost_devices(clean_filter)
        flights = self._lost_flights(clean_filter)

        flight_by_id: dict[str, dict[str, Any]] = {}
        for flight in _unique_flight_rows(flights):
            flight_id = str(flight.get(FLIGHT_FIELDS["flight_id"]) or "").strip()
            if flight_id:
                flight_by_id[flight_id] = flight

        out: list[EventRow] = []
        for i, row in enumerate(losses):
            flight_id = str(row.get(LOST_DEVICE_FIELDS["flight_id"]) or "").strip()
            flight = flight_by_id.get(flight_id)

            display_device_type = _lost_device_type_label(
                row.get(LOST_DEVICE_FIELDS["device_type"])
                or (flight or {}).get(FLIGHT_FIELDS["device_type"])
            )
            if display_device_type is None:
                continue

            if clean_filter.category:
                selected_categories = {_normalized_text(value) for value in clean_filter.category}
                if _normalized_text(display_device_type) not in selected_categories:
                    continue

            device = str(row.get(LOST_DEVICE_FIELDS["device"]) or "").strip()
            if clean_filter.asset:
                selected_assets = {_normalized_text(value) for value in clean_filter.asset}
                if _normalized_text(device) not in selected_assets:
                    continue

            if flight is None and (
                clean_filter.direction
                or clean_filter.group
                or clean_filter.bbak
                or clean_filter.rota
                or clean_filter.battalion
                or clean_filter.purpose
            ):
                continue

            d = str(row.get(LOST_DEVICE_FIELDS["date"]) or "").strip()
            t = str(row.get(LOST_DEVICE_FIELDS["time"]) or "").strip()
            timestamp = f"{d}T{t}" if d and t else d
            row_id = str(
                row.get(LOST_DEVICE_FIELDS["row_hash"])
                or flight_id
                or f"lost-device-{i + 1}"
            )

            out.append(
                EventRow(
                    id=row_id,
                    timestamp=timestamp,
                    direction=str((flight or {}).get(FLIGHT_FIELDS["direction"]) or ""),
                    unit="",
                    category=display_device_type,
                    asset=device or "—",
                    group=str((flight or {}).get(FLIGHT_FIELDS["position"]) or "").strip() or "—",
                    purpose=str((flight or {}).get(FLIGHT_FIELDS["main_purpose"]) or "").strip() or "—",
                    class_name="",
                    result=str(row.get(LOST_DEVICE_FIELDS["result"]) or ""),
                    grid_ref="—",
                    lat=0.0,
                    lon=0.0,
                )
            )

        loss_limit = CH_CONFIG.query.lost_devices_table_limit
        return out if loss_limit is None else out[:loss_limit]

    def overview(self, f: FilterRequest, kpi: KpiConfiguration | None = None, *, calculator=None) -> list[Metric]:
        kpi = kpi or KpiConfiguration()
        calculator = calculator or PurposeKpiCalculator(kpi)
        flights, events, ammo = self._dataset(f)
        unique_flights = _unique_flight_rows(flights)
        total_flights = len(unique_flights)

        effective_rows = [
            row for row in unique_flights if calculator.successful(row)
        ]
        effective = len(effective_rows)

        detected = sum(
            _as_int(x.get(AMMO_FIELDS["bc_count"]))
            for x in ammo
            if _normalized_text(x.get(AMMO_FIELDS["bc_name"]))
            == _normalized_text(ANALYTICS_CONFIG.detected_ammunition_name)
        )

        affected_events = [
            x for x in events
            if _normalized_text(x.get(EVENT_FIELDS["result"]))
            == _normalized_text(ANALYTICS_CONFIG.result_names["affected"])
        ]
        destroyed_events = [
            x for x in events
            if _normalized_text(x.get(EVENT_FIELDS["result"]))
            == _normalized_text(ANALYTICS_CONFIG.result_names["destroyed"])
        ]
        affected_total = len(affected_events)
        destroyed_total = len(destroyed_events)

        affected_personnel = sum(
            _as_int(x.get(EVENT_FIELDS["field_300"])) for x in events if _is_personnel_event(x)
        )
        destroyed_personnel = sum(
            _as_int(x.get(EVENT_FIELDS["field_200"])) for x in events if _is_personnel_event(x)
        )
        efficiency = (effective / total_flights * 100.0) if total_flights else 0.0

        flight_group_counts: dict[str, Counter[str]] = defaultdict(Counter)
        flight_group_labels: dict[str, str] = {}
        weighted_groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
        for row in unique_flights:
            device_type = (
                str(row.get(FLIGHT_FIELDS["device_type"]) or "").strip()
                or ANALYTICS_CONFIG.unknown_device_type_label
            )
            group_key, group_label = _flight_purpose_group(
                row.get(FLIGHT_FIELDS["main_purpose"]), device_type
            )
            flight_group_counts[device_type][group_key] += 1
            flight_group_labels[group_key] = group_label
            numerator, denominator = calculator.weights(row)
            weighted_groups[device_type][group_key][0] += numerator
            weighted_groups[device_type][group_key][1] += denominator

        flight_breakdown: list[MetricBreakdownItem] = []
        for device_type, counts in flight_group_counts.items():
            flight_breakdown.append(
                MetricBreakdownItem(
                    key=f"device:{device_type}",
                    label=device_type,
                    value=sum(counts.values()),
                    children=_breakdown_items(dict(counts), labels=flight_group_labels),
                )
            )
        flight_breakdown.sort(key=lambda item: (-item.value, item.label.casefold()))

        weighted_breakdown: list[MetricBreakdownItem] = []
        for device_type, groups in weighted_groups.items():
            children = [MetricBreakdownItem(
                key=key, label=flight_group_labels[key], unit="%",
                value=weighted_percentage(values[0], values[1]),
                numerator=values[0], denominator=values[1],
            ) for key, values in groups.items()]
            children.sort(key=lambda item: (-(item.value if item.value is not None else -1), item.label.casefold()))
            numerator = sum(values[0] for values in groups.values())
            denominator = sum(values[1] for values in groups.values())
            weighted_breakdown.append(MetricBreakdownItem(
                key=f"device:{device_type}", label=device_type, unit="%",
                value=weighted_percentage(numerator, denominator), children=children,
                numerator=numerator, denominator=denominator,
            ))
        weighted_breakdown.sort(key=lambda item: (-(item.value if item.value is not None else -1), item.label.casefold()))
        weighted_numerator = sum(item.numerator or 0.0 for item in weighted_breakdown)
        weighted_denominator = sum(item.denominator or 0.0 for item in weighted_breakdown)

        device_by_flight = _device_type_by_flight(unique_flights)
        total_by_device: Counter[str] = Counter()
        effective_by_device: Counter[str] = Counter()
        for row in unique_flights:
            device_type = (
                str(row.get(FLIGHT_FIELDS["device_type"]) or "").strip()
                or ANALYTICS_CONFIG.unknown_device_type_label
            )
            total_by_device[device_type] += 1
            if calculator.successful(row):
                effective_by_device[device_type] += 1

        effective_breakdown = _breakdown_items(dict(effective_by_device))

        positions_by_device: dict[str, set[str]] = defaultdict(set)
        for row in unique_flights:
            device_type = (
                str(row.get(FLIGHT_FIELDS["device_type"]) or "").strip()
                or ANALYTICS_CONFIG.unknown_device_type_label
            )
            position = str(row.get(FLIGHT_FIELDS["position"]) or "").strip()
            if position:
                positions_by_device[device_type].add(position)

        period_days = _average_period_days(f)
        average_flights_per_position: dict[str, float] = {}
        for device_type, total in total_by_device.items():
            position_count = len(positions_by_device.get(device_type, set()))
            if position_count > 0:
                average_flights_per_position[device_type] = round(
                    total / position_count / period_days, 1
                )
        avg_position_breakdown = _breakdown_items(average_flights_per_position)

        total_department_positions = sum(len(positions) for positions in positions_by_device.values())
        overall_avg_flights_per_position = (
            round(total_flights / total_department_positions / period_days, 1)
            if total_department_positions > 0 else 0.0
        )

        efficiency_by_device: dict[str, float] = {}
        for device_type, total in total_by_device.items():
            if total > 0:
                efficiency_by_device[device_type] = (
                    effective_by_device.get(device_type, 0) / total * 100.0
                )
        efficiency_breakdown = _breakdown_items(efficiency_by_device, unit="%")

        detected_by_device: Counter[str] = Counter()
        for row in ammo:
            if _normalized_text(row.get(AMMO_FIELDS["bc_name"])) != _normalized_text(
                ANALYTICS_CONFIG.detected_ammunition_name
            ):
                continue
            flight_id = str(row.get(AMMO_FIELDS["flight_id"]) or "").strip()
            device_type = device_by_flight.get(
                flight_id, str(row.get('device_type') or '').strip() or ANALYTICS_CONFIG.unknown_device_type_label
            )
            detected_by_device[device_type] += _as_int(row.get(AMMO_FIELDS["bc_count"]))
        detected_breakdown = _breakdown_items(dict(detected_by_device))

        affected_by_device: Counter[str] = Counter()
        destroyed_by_device: Counter[str] = Counter()
        for row in events:
            if not _is_personnel_event(row):
                continue
            flight_id = str(row.get(EVENT_FIELDS["flight_id"]) or "").strip()
            device_type = device_by_flight.get(
                flight_id, str(row.get('device_type') or '').strip() or ANALYTICS_CONFIG.unknown_device_type_label
            )
            field300 = _as_int(row.get(EVENT_FIELDS["field_300"]))
            field200 = _as_int(row.get(EVENT_FIELDS["field_200"]))
            if field300 > 0:
                affected_by_device[device_type] += field300
            if field200 > 0:
                destroyed_by_device[device_type] += field200

        return [
            Metric(
                key="flights", label="Вильоти", value=total_flights,
                breakdown_title="Вильоти за типом засобу та задачами",
                breakdown=flight_breakdown,
            ),
            Metric(
                key="effective", label="Результативні", value=effective,
                breakdown_title="Результативні за типом засобу",
                breakdown=effective_breakdown,
            ),
            Metric(
                key="detected", label="Виявлено", value=detected,
                breakdown_title="Виявлено за типом засобу",
                breakdown=detected_breakdown,
            ),
            Metric(
                key="affected", label="Уражено", value=affected_personnel,
                primary_label="ОС", secondary_label="Загалом",
                secondary_value=affected_total,
                breakdown_title="Уражено ОС за типом засобу",
                breakdown=_breakdown_items(dict(affected_by_device)),
            ),
            Metric(
                key="destroyed", label="Знищено", value=destroyed_personnel,
                primary_label="ОС", secondary_label="Загалом",
                secondary_value=destroyed_total,
                breakdown_title="Знищено ОС за типом засобу",
                breakdown=_breakdown_items(dict(destroyed_by_device)),
            ),
            Metric(
                key="efficiency", label="Ефективність", value=round(efficiency, 1), unit="%",
                breakdown_title="Ефективність за типом засобу",
                breakdown=efficiency_breakdown,
            ),
            Metric(
                key="avg_flights_per_position",
                label="Середня кількість вильотів на позицію за добу",
                value=overall_avg_flights_per_position,
                breakdown_title="Середні вильоти на позицію за добу по кафедрах",
                breakdown=avg_position_breakdown,
            ),
            Metric(
                key="weighted_efficiency", label="Зважений KPI",
                value=weighted_percentage(weighted_numerator, weighted_denominator), unit="%",
                breakdown_title="Корисна дія за кафедрами та метою вильоту",
                breakdown=weighted_breakdown, calculation_fingerprint=kpi_fingerprint(kpi),
                numerator=weighted_numerator, denominator=weighted_denominator,
            ),
        ]

    def timeline(self, f: FilterRequest, kpi: KpiConfiguration | None = None, *, calculator=None) -> list[TimelinePoint]:
        calculator = calculator or PurposeKpiCalculator(kpi or KpiConfiguration())
        flights, _, _ = self._dataset(f)
        flights = _unique_flight_rows(flights)

        day: Counter[str] = Counter()
        night: Counter[str] = Counter()
        day_effective: Counter[str] = Counter()
        night_effective: Counter[str] = Counter()
        dates: set[str] = set()

        for row in flights:
            d = str(row.get(FLIGHT_FIELDS["date"]) or "")[:10]
            if not d:
                continue
            dates.add(d)
            ts = _parse_row_datetime(row, FLIGHT_FIELDS["time_range"])
            is_day = False
            if ts is not None:
                local_time = ts.timetz().replace(tzinfo=None)
                is_day = DAY_START <= local_time < DAY_END
            if is_day:
                day[d] += 1
                if calculator.successful(row):
                    day_effective[d] += 1
            else:
                night[d] += 1
                if calculator.successful(row):
                    night_effective[d] += 1

        points: list[TimelinePoint] = []
        for d in sorted(dates):
            day_total = day[d]
            night_total = night[d]
            day_eff = day_effective[d]
            night_eff = night_effective[d]
            points.append(
                TimelinePoint(
                    date=date.fromisoformat(d),
                    total=day_total + night_total,
                    effective=day_eff + night_eff,
                    day=day_total,
                    night=night_total,
                    day_effective=day_eff,
                    night_effective=night_eff,
                    day_efficiency=round(day_eff / day_total * 100.0, 1) if day_total else 0.0,
                    night_efficiency=round(night_eff / night_total * 100.0, 1) if night_total else 0.0,
                )
            )
        return points

    def kpi_preview(self, f: FilterRequest, kpi: KpiConfiguration, source: str = "clickhouse"):
        flights, _, _ = self._dataset(f)
        return PurposeKpiCalculator(kpi).preview(_unique_flight_rows(flights), source)

    def events(self, f: FilterRequest) -> list[EventRow]:
        if LOST_DEVICES_SENTINEL in f.result:
            return self._lost_device_rows(f)

        flights, rows, _ = self._dataset(f)
        rows = rows[: CH_CONFIG.query.event_table_limit]
        device_by_flight = _device_type_by_flight(flights)

        out: list[EventRow] = []
        for i, row in enumerate(rows):
            ts = str(row.get(EVENT_FIELDS["timestamp"]) or "")
            if not ts:
                d = str(row.get(EVENT_FIELDS["date"]) or "")
                t = str(row.get(EVENT_FIELDS["time"]) or "")
                ts = f"{d}T{t}" if d and t else d
            flight_id = str(row.get(EVENT_FIELDS["flight_id"]) or "").strip()
            device_type = device_by_flight.get(
                flight_id, str(row.get('device_type') or '').strip() or ANALYTICS_CONFIG.unknown_device_type_label
            )
            row_id = str(
                row.get(EVENT_FIELDS["row_hash"])
                or row.get(EVENT_FIELDS["id"])
                or row.get(EVENT_FIELDS["flight_id"])
                or f"event-{i + 1}"
            )
            out.append(
                EventRow(
                    id=row_id,
                    timestamp=ts,
                    direction=str(row.get(EVENT_FIELDS["direction"]) or ""),
                    unit=str(row.get(EVENT_FIELDS["units"]) or ""),
                    category=device_type,
                    asset=str(row.get(EVENT_FIELDS["device"]) or ""),
                    group=str(row.get(EVENT_FIELDS["crew"]) or ""),
                    purpose=str(row.get(EVENT_FIELDS["flight_purpose"]) or ""),
                    class_name=str(row.get(EVENT_FIELDS["target_class"]) or ""),
                    result=str(row.get(EVENT_FIELDS["result"]) or ""),
                    grid_ref="—",
                    lat=0.0,
                    lon=0.0,
                )
            )
        return out

    def geojson(self, f: FilterRequest) -> dict[str, Any]:
        # Precise operational coordinates remain out of the viewer contract.
        return {"type": "FeatureCollection", "features": []}
