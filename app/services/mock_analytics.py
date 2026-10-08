from __future__ import annotations

from datetime import datetime, timedelta
from random import Random

from app.api.models import EventRow, FilterRequest, Metric, TimelinePoint
from app.core.config import get_backend_config


def _config():
    return get_backend_config().mock


def _rng(f: FilterRequest) -> Random:
    return Random(f"{f.date_from}:{f.date_to}:{f.direction}:{f.unit}:{f.category}:{f.group}")


def filter_options() -> dict[str, list[str]]:
    return _config().options


def overview(f: FilterRequest) -> list[Metric]:
    r = _rng(f)
    cfg = _config().overview
    days = max((f.date_to - f.date_from).days + 1, 1)
    total = days * r.randint(cfg.events_per_day_min, cfg.events_per_day_max)
    positive = int(total * r.uniform(cfg.positive_ratio_min, cfg.positive_ratio_max))
    detected = int(total * r.uniform(cfg.detected_ratio_min, cfg.detected_ratio_max))
    resolved = int(detected * r.uniform(cfg.resolved_ratio_min, cfg.resolved_ratio_max))
    efficiency = positive / total * 100 if total else 0
    weight = r.randint(cfg.volume_min, cfg.volume_max)
    values = [
        ("total", "Події", total, None),
        ("detected", "Виявлено", detected, None),
        ("resolved", "Опрацьовано", resolved, None),
        ("positive", "Позитивні", positive, None),
        ("efficiency", "Ефективність", efficiency, "%"),
        ("volume", "Обсяг", weight, "од."),
    ]
    return [
        Metric(
            key=k,
            label=l,
            value=v,
            unit=u,
            delta=round(r.uniform(cfg.delta_min, cfg.delta_max), 1),
        )
        for k, l, v, u in values
    ]


def timeline(f: FilterRequest) -> list[TimelinePoint]:
    r = _rng(f)
    cfg = _config().timeline
    days = max((f.date_to - f.date_from).days + 1, 1)
    days = min(days, cfg.max_days)
    out = []
    for i in range(days):
        d = f.date_from + timedelta(days=i)
        total = r.randint(cfg.events_min, cfg.events_max)
        out.append(
            TimelinePoint(
                date=d,
                total=total,
                effective=int(
                    total * r.uniform(cfg.effective_ratio_min, cfg.effective_ratio_max)
                ),
            )
        )
    return out


def _pick(r: Random, key: str, selected: list[str]) -> str:
    choices = selected or _config().options[key]
    return r.choice(choices)


def events(f: FilterRequest) -> list[EventRow]:
    r = _rng(f)
    cfg = _config().events
    base = datetime.combine(f.date_to, datetime.min.time()) + timedelta(hours=cfg.base_hour)
    rows = []
    for i in range(cfg.count):
        rows.append(
            EventRow(
                id=f"{cfg.id_prefix}{cfg.id_start + i}",
                timestamp=(
                    base
                    - timedelta(
                        minutes=i * r.randint(cfg.minute_step_min, cfg.minute_step_max)
                    )
                ).isoformat(timespec="minutes"),
                direction=_pick(r, "direction", f.direction),
                unit=_pick(r, "unit", f.unit),
                category=_pick(r, "category", f.category),
                asset=_pick(r, "asset", f.asset),
                group=_pick(r, "group", f.group),
                purpose=_pick(r, "purpose", f.purpose),
                class_name=_pick(r, "class_name", f.class_name),
                result=_pick(r, "result", f.result),
                grid_ref=f"{cfg.grid_prefix}{i + 1:03d}",
                lat=cfg.base_lat + r.uniform(-cfg.lat_spread, cfg.lat_spread),
                lon=cfg.base_lon + r.uniform(-cfg.lon_spread, cfg.lon_spread),
            )
        )
    return rows


def geojson(f: FilterRequest) -> dict:
    rows = events(f)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [x.lon, x.lat]},
                "properties": {
                    "id": x.id,
                    "result": x.result,
                    "unit": x.unit,
                    "category": x.category,
                    "asset": x.asset,
                    "grid_ref": x.grid_ref,
                    "timestamp": x.timestamp,
                    "purpose": x.purpose,
                    "class_name": x.class_name,
                },
            }
            for x in rows
        ],
    }
