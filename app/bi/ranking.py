"""Crew ranking: each crew's place in its company, battalion and the brigade.

Places are computed over every crew of the brigade per кафедра (device category),
but a company commander or battalion staff receives only their own crews: the
value and the places, never other crews' names or values. Users who may see other
units' own values (comparison.view_peer_raw) receive the whole list.
"""
from __future__ import annotations

from typing import Any

from app.bi.engine import QueryError, QueryPermissionError
from app.bi.models import DataScope

# Ratio metrics are ranked only for crews with enough flights to mean something.
DEFAULT_MIN_FLIGHTS = 5
ORGANIZATION_FIELDS = {"department", "department_id", "bbak_id", "bbak", "bbak_title", "group", "group_id", "rota_id", "rota", "rota_title", "team", "team_id", "crew", "battalion", "unit", "units"}


def _places(rows: list[dict[str, Any]], higher_is_better: bool) -> dict[tuple, int]:
    """Competition ranking (1, 2, 2, 4) by value."""
    ordered = sorted(rows, key=lambda row: row["value"], reverse=higher_is_better)
    places, previous, place = {}, object(), 0
    for index, row in enumerate(ordered, start=1):
        if row["value"] != previous:
            place, previous = index, row["value"]
        places[row["key"]] = place
    return places


def crew_ranking(service, principal, body: dict[str, Any]) -> dict[str, Any]:
    if not principal.has("ranking.view"):
        raise QueryPermissionError("Crew ranking is not permitted")
    metric = str(body.get("metric") or "efficiency")
    definitions, _ = service._definitions()
    if metric not in definitions:
        raise QueryError(f"Unknown metric: {metric}")
    filters = [item for item in body.get("filters", []) if isinstance(item, dict) and item.get("field") not in ORGANIZATION_FIELDS]
    request = {"metrics": [{"key": metric}, {"key": "flights"}], "dimensions": [{"field": "category"}, {"field": "department"}, {"field": "group"}, {"field": "team"}],
               "date_range": body["date_range"], "filters": filters, "data_scope": "USER_SCOPE"}
    # The user must be allowed to see the metric; the brigade-wide numbers are then
    # computed once for everyone and cut down to what this user may see.
    service._validated(request, principal, definitions)
    from app.bi.security import Principal
    system = Principal("ranking", "administrator", frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")
    result = service.execute(request, system)
    definition = definitions[metric]
    higher = definition.get("direction", "HIGHER_IS_BETTER") != "LOWER_IS_BETTER"
    ratio = definition.get("aggregation") in {"RATIO", "FORMULA"} or definition.get("format", {}).get("type") == "percent"
    minimum = int(body.get("min_flights", DEFAULT_MIN_FLIGHTS if ratio else 1) or 1)
    crews = []
    for row in result["rows"]:
        if row.get("team") in (None, "") or row.get(metric) is None or (row.get("flights") or 0) < minimum:
            continue
        key = (str(row.get("category")), str(row.get("department")), str(row.get("team")))
        crews.append({"key": key, "category": str(row.get("category")), "department": str(row.get("department")), "group": str(row.get("group")), "team": str(row.get("team")), "value": row[metric], "flights": row.get("flights")})
    by_category: dict[str, list[dict]] = {}
    for crew in crews:
        by_category.setdefault(crew["category"], []).append(crew)
    scope: DataScope = principal.scope
    see_all = principal.is_administrator or (scope.scope_type == "ALL" and principal.has("comparison.view_peer_raw"))
    if not see_all and scope.scope_type not in {"DEPARTMENT", "GROUP"}:
        raise QueryPermissionError("Crew ranking needs a battalion or company scope")
    own_ids = set(scope.scope_ids)
    output = []
    for category, members in by_category.items():
        brigade = _places(members, higher)
        battalions: dict[str, list] = {}
        companies: dict[tuple, list] = {}
        for crew in members:
            battalions.setdefault(crew["department"], []).append(crew)
            companies.setdefault((crew["department"], crew["group"]), []).append(crew)
        battalion_places = {department: _places(rows, higher) for department, rows in battalions.items()}
        company_places = {key: _places(rows, higher) for key, rows in companies.items()}
        for crew in members:
            mine = see_all or (crew["department"] in own_ids if scope.scope_type == "DEPARTMENT" else crew["group"] in own_ids)
            if not mine:
                continue
            company = (crew["department"], crew["group"])
            output.append({
                "category": category, "battalion_id": crew["department"], "company_id": crew["group"], "crew": crew["team"],
                "value": crew["value"], "flights": crew["flights"],
                "company_place": company_places[company][crew["key"]], "company_total": len(companies[company]),
                "battalion_place": battalion_places[crew["department"]][crew["key"]], "battalion_total": len(battalions[crew["department"]]),
                "brigade_place": brigade[crew["key"]], "brigade_total": len(members),
            })
    output.sort(key=lambda row: (row["category"], row["brigade_place"], row["crew"]))
    return {"metric": metric, "title": definition.get("title", metric), "format": definition.get("format", {}), "higher_is_better": higher,
            "min_flights": minimum, "full_list": see_all, "rows": output}
