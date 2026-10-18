"""Map layers: struck and destroyed targets as points, FLET and front line as lines.

Coordinates of results are stored as MGRS strings ("37U DQ 06502 11859"); they
are converted here to WGS84 longitude/latitude for GeoJSON.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

# --- MGRS -> latitude/longitude (WGS84) ---------------------------------------

_MGRS = re.compile(r"^(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z])([A-HJ-NP-V])(\d*)$")
_COLUMN_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_ROWS = "ABCDEFGHJKLMNPQRSTUV"
# Lowest northing (m) of each latitude band; resolves the 2000 km row cycle.
_BAND_MIN_NORTHING = {
    "C": 1_100_000, "D": 2_000_000, "E": 2_800_000, "F": 3_700_000, "G": 4_600_000, "H": 5_500_000,
    "J": 6_400_000, "K": 7_300_000, "L": 8_200_000, "M": 9_100_000, "N": 0, "P": 800_000,
    "Q": 1_700_000, "R": 2_600_000, "S": 3_500_000, "T": 4_400_000, "U": 5_300_000, "V": 6_200_000,
    "W": 7_000_000, "X": 7_900_000,
}
_A, _F, _K0 = 6_378_137.0, 1 / 298.257223563, 0.9996


def _utm_to_lat_lon(zone: int, northern: bool, easting: float, northing: float) -> tuple[float, float]:
    e2 = _F * (2 - _F)
    ep2 = e2 / (1 - e2)
    x = easting - 500_000.0
    y = northing if northern else northing - 10_000_000.0
    m = y / _K0
    mu = m / (_A * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    n1 = _A / math.sqrt(1 - e2 * sin1 ** 2)
    t1 = tan1 ** 2
    c1 = ep2 * cos1 ** 2
    r1 = _A * (1 - e2) / (1 - e2 * sin1 ** 2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * tan1 / r1) * (d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
                                    + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / cos1
    return math.degrees(lat), (zone - 1) * 6 - 180 + 3 + math.degrees(lon)


def mgrs_to_lon_lat(value: Any) -> tuple[float, float] | None:
    """Return (longitude, latitude) of the centre of an MGRS square, or None."""
    if value is None:
        return None
    match = _MGRS.match(re.sub(r"\s+", "", str(value)).upper())
    if not match:
        return None
    zone, band, column, row, digits = int(match[1]), match[2], match[3], match[4], match[5]
    if not 1 <= zone <= 60 or len(digits) % 2 or len(digits) > 10:
        return None
    columns = _COLUMN_SETS[(zone - 1) % 3]
    if column not in columns:
        return None
    half = len(digits) // 2
    size = 10 ** (5 - half)
    easting = (columns.index(column) + 1) * 100_000 + (int(digits[:half]) if half else 0) * size + size / 2
    row_index = (_ROWS.index(row) - (5 if zone % 2 == 0 else 0)) % 20
    northing = row_index * 100_000 + (int(digits[half:]) if half else 0) * size + size / 2
    while northing < _BAND_MIN_NORTHING[band]:
        northing += 2_000_000
    lat, lon = _utm_to_lat_lon(zone, band >= "N", easting, northing)
    return round(lon, 6), round(lat, 6)


# --- Targets -------------------------------------------------------------------

_HISTORY_LIMIT = 50


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _moment(row: dict[str, Any]) -> str:
    local = row.get("local_timestamp")
    if isinstance(local, str) and re.fullmatch(r"\d+(\.\d+)?", local.strip()):
        local = float(local)
    if isinstance(local, (int, float)) and not isinstance(local, bool):
        # BI rows carry the local time as a Unix timestamp.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from app.core.config import get_backend_config
        return datetime.fromtimestamp(local, ZoneInfo(get_backend_config().analytics.timezone)).strftime("%Y-%m-%d %H:%M")
    if local:
        return str(local).replace("T", " ")[:16]
    timestamp = _text(row.get("timestamp"))
    if timestamp:
        return timestamp.replace("T", " ")[:16]
    return " ".join(part for part in (_text(row.get("date")), _text(row.get("time"))[:5]) if part)


def target_features(events: Iterable[dict[str, Any]], *, affected: str, destroyed: str, flights: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """One point per target: every strike on it, in order, ends at its last state.

    A target is identified by its ID; records without an ID are grouped by their
    MGRS square. The point is placed where it was destroyed, else at the last strike.
    """
    flights = flights or {}
    groups: dict[str, list[tuple[dict[str, Any], str, tuple[float, float]]]] = {}
    for row in events:
        result = re.sub(r"\s+", " ", _text(row.get("result")).casefold())
        if result not in (affected, destroyed):
            continue
        grid = _text(row.get("mgrs"))
        point = mgrs_to_lon_lat(grid)
        if point is None:
            continue
        key = _text(row.get("target_id")) or f"mgrs:{re.sub(r'\s+', '', grid).upper()}"
        groups.setdefault(key, []).append((row, "destroyed" if result == destroyed else "affected", point))
    features = []
    for key, records in groups.items():
        records.sort(key=lambda item: _moment(item[0]))
        destroyed_records = [item for item in records if item[1] == "destroyed"]
        final = (destroyed_records or records)[-1]
        history = []
        for row, status, _ in records[-_HISTORY_LIMIT:]:
            parent = flights.get(_text(row.get("flight_id")), {})
            history.append({
                "time": _moment(row), "status": status, "result": _text(row.get("result")),
                "crew": _text(row.get("crew")) or _text(parent.get("crew")),
                "position": _text(row.get("position")) or _text(parent.get("position")),
                "category": _text(row.get("device_type")) or _text(parent.get("device_type")),
                "device": _text(row.get("device")), "purpose": _text(row.get("flight_purpose")),
                "grid_ref": _text(row.get("mgrs")),
            })
        row = final[0]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(final[2])},
            "properties": {
                "id": key, "target_id": _text(row.get("target_id")),
                "status": "destroyed" if destroyed_records else "affected",
                "result": "Знищено" if destroyed_records else "Уражено",
                "strikes": len(records), "affected_count": len(records) - len(destroyed_records), "destroyed_count": len(destroyed_records),
                "class_name": _text(row.get("target_class")), "unit": _text(row.get("units")),
                "category": history[-1]["category"] if history else "", "asset": _text(row.get("device")),
                "grid_ref": _text(row.get("mgrs")), "location_name": _text(row.get("location_name")),
                "purpose": _text(row.get("flight_purpose")), "timestamp": _moment(row),
                "first": _moment(records[0][0]), "history": json.dumps(history, ensure_ascii=False),
                "history_truncated": len(records) > _HISTORY_LIMIT,
            },
        })
    features.sort(key=lambda feature: feature["properties"]["timestamp"])
    return {"type": "FeatureCollection", "features": features}


# --- Lines ---------------------------------------------------------------------

LINE_LAYERS = (
    # (layer, ClickHouse view, geometry column, title)
    ("flet", "dwh.v_geo_flet_current", "geom", "FLET"),
    ("front_line", "dwh.v_geo_front_line_current", "geom", "Лінія фронту"),
)


def _wkt_coordinates(text: str) -> Any:
    tokens = re.findall(r"\(|\)|,|[-+0-9.eE]+(?:\s+[-+0-9.eE]+)+", text)
    stack: list[list[Any]] = [[]]
    for token in tokens:
        if token == "(":
            stack.append([])
        elif token == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif token != ",":
            numbers = [float(part) for part in token.split()]
            stack[-1].append(numbers[:2])
    return stack[0][0] if stack[0] else []


def wkt_to_geometry(wkt: str) -> dict[str, Any] | None:
    match = re.match(r"\s*(MULTILINESTRING|LINESTRING|MULTIPOLYGON|POLYGON)\s*(\(.*\))\s*$", wkt or "", re.I | re.S)
    if not match:
        return None
    kind = {"LINESTRING": "LineString", "MULTILINESTRING": "MultiLineString", "POLYGON": "Polygon", "MULTIPOLYGON": "MultiPolygon"}[match[1].upper()]
    coordinates = _wkt_coordinates(match[2])
    return {"type": kind, "coordinates": coordinates} if coordinates else None


def load_lines(query) -> dict[str, Any]:
    """Current FLET and front line; a missing view is skipped, not an error."""
    features = []
    for layer, view, column, title in LINE_LAYERS:
        try:
            rows = query(f"SELECT id, label, {column} AS geom, toString(valid_from) AS valid_from FROM {view}")
        except Exception:
            continue
        for row in rows:
            geometry = wkt_to_geometry(_text(row.get("geom")))
            if geometry:
                features.append({"type": "Feature", "geometry": geometry, "properties": {
                    "layer": layer, "title": _text(row.get("label")) or title, "valid_from": _text(row.get("valid_from"))[:16]}})
    return {"type": "FeatureCollection", "features": features}


def map_lines() -> dict[str, Any]:
    from app.core.cache import cache
    from app.core.config import get_settings
    from app.services.clickhouse_client import clickhouse_client

    if get_settings().active_source != "clickhouse":
        return {"type": "FeatureCollection", "features": []}
    return cache.get_or_load("api.map_lines", {"layers": [layer[:3] for layer in LINE_LAYERS]}, lambda: load_lines(clickhouse_client.query), ttl_seconds=300)
