"""Crew ranking: one score per crew and its place in the company, battalion and brigade.

Crews are compared per кафедра (device category) and per mission group (strike,
logistics, mining, …; set by an administrator), so a crew that only flies safe
deliveries is never ranked against strike crews. Within a group three criteria count:
  * average flights per day the crew flew such missions (more is better);
  * effectiveness, effective flights net of losses per flight (more is better);
  * devices lost per flight (fewer is better; left out where devices do not come back).
Each criterion becomes a percentile among the crews of the same кафедра and group
(0 = worst, 100 = best) and the group score is their weighted mean. The overall score
is the mean of the crew's group scores weighted by its flights in each group, so a
crew that flew strikes, deliveries and mining is judged on all three and a crew that
only delivered and mined on those two. Equal scores are ordered by flights.

A company commander or battalion staff receives only their own crews: the values and
the places, never other crews' names or values. Users who may see other units' own
values (comparison.view_peer_raw) receive the whole list.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.bi.engine import QueryError, QueryPermissionError
from app.bi.models import DataScope
from app.core.kpi_configuration import normalize_kpi_text
from app.core.ranking_configuration import OTHER_GROUP, RankingConfiguration

OVERALL = ("all", "Загальний")
CRITERIA = (
    # key, title, higher is better
    ("avg_flights", "Середня кількість вильотів за добу", True),
    ("efficiency", "Результативність, %", True),
    ("loss_rate", "Втрати бортів на виліт", False),
)
SOURCE_METRICS = ("flights", "effective_net", "lost_devices")
ORGANIZATION_FIELDS = {"department", "department_id", "bbak_id", "bbak", "bbak_title", "group", "group_id", "rota_id", "rota", "rota_title", "team", "team_id", "crew", "battalion", "unit", "units"}


CHUNK_DAYS = 14


def _chunks(date_range: dict[str, Any]) -> list[tuple[str, str]]:
    start, end = (date.fromisoformat(str(date_range[key])[:10]) for key in ("from", "to"))
    parts = []
    while start <= end:
        stop = min(start + timedelta(days=CHUNK_DAYS - 1), end)
        parts.append((start.isoformat(), stop.isoformat()))
        start = stop + timedelta(days=1)
    return parts


def _settings() -> RankingConfiguration:
    from app.core.app_configuration import get_configuration_store
    return get_configuration_store().snapshot().config.ranking


def _percentiles(crews: list[dict[str, Any]], key: str, higher_is_better: bool) -> dict[tuple, float]:
    """0 for the worst crew, 1 for the best; equal values share the mean of their positions."""
    if len(crews) == 1:
        return {crews[0]["key"]: 1.0}
    ordered = sorted(crews, key=lambda crew: crew[key] if higher_is_better else -crew[key])
    result, index = {}, 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][key] == ordered[index][key]:
            end += 1
        share = (index + end) / 2 / (len(ordered) - 1)
        for crew in ordered[index:end + 1]:
            result[crew["key"]] = share
        index = end + 1
    return result


def _places(rows: list[dict[str, Any]]) -> dict[tuple, int]:
    """Competition ranking (1, 2, 2, 4) by score, then by flights."""
    rank = lambda row: (-row["score"], -row["flights"])
    places, previous, place = {}, None, 0
    for index, row in enumerate(sorted(rows, key=rank), start=1):
        if rank(row) != previous:
            place, previous = index, rank(row)
        places[row["key"]] = place
    return places


def _empty(key: tuple, **extra) -> dict[str, Any]:
    return {"key": key, "flights": 0, "effective": 0.0, "lost": 0, "days": set(), **extra}


def _finish(crew: dict[str, Any]) -> dict[str, Any]:
    crew["active_days"] = len(crew["days"])
    crew["avg_flights"] = crew["flights"] / max(crew["active_days"], 1)
    crew["efficiency"] = 100 * crew["effective"] / crew["flights"] if crew["flights"] else 0.0
    crew["loss_rate"] = crew["lost"] / crew["flights"] if crew["flights"] else 0.0
    return crew


def _score(members: list[dict[str, Any]], settings: RankingConfiguration, ignore_losses: bool) -> None:
    weights = {"avg_flights": settings.weights.avg_flights, "efficiency": settings.weights.efficiency, "loss_rate": 0 if ignore_losses else settings.weights.loss_rate}
    if not any(weights.values()):
        weights["avg_flights"] = 1
    total = sum(weights.values())
    shares = {key: _percentiles(members, key, higher) for key, _, higher in CRITERIA if weights[key]}
    for crew in members:
        crew["parts"] = {key: round(100 * shares[key][crew["key"]], 1) for key in shares}
        crew["score"] = round(sum(weights[key] * crew["parts"][key] for key in shares) / total, 1)


def crew_ranking(service, principal, body: dict[str, Any], settings: RankingConfiguration | None = None) -> dict[str, Any]:
    if not principal.has("ranking.view"):
        raise QueryPermissionError("Crew ranking is not permitted")
    settings = settings or _settings()
    definitions, _ = service._definitions()
    missing = [key for key in SOURCE_METRICS if key not in definitions]
    if missing:
        raise QueryError(f"Unknown metric: {', '.join(missing)}")
    filters = [item for item in body.get("filters", []) if isinstance(item, dict) and item.get("field") not in ORGANIZATION_FIELDS]
    metrics = [{"key": key} for key in SOURCE_METRICS]
    # The user must be allowed to see the metrics; the brigade-wide numbers are then
    # computed once for everyone and cut down to what this user may see.
    service._validated({"metrics": metrics, "dimensions": [], "date_range": body["date_range"], "filters": filters, "data_scope": "USER_SCOPE"}, principal, definitions)
    scope: DataScope = principal.scope
    see_all = principal.is_administrator or (scope.scope_type == "ALL" and principal.has("comparison.view_peer_raw"))
    if not see_all and scope.scope_type not in {"DEPARTMENT", "GROUP"}:
        raise QueryPermissionError("Crew ranking needs a battalion or company scope")
    from app.bi.security import Principal
    system = Principal("ranking", "administrator", frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")
    base = {"date_range": body["date_range"], "filters": filters, "data_scope": "USER_SCOPE"}
    # Crew × purpose × day makes many groups over months: read the days in parts. Each row
    # is one day, so the parts never overlap and only one part's flights are held at once.
    daily = []
    for start, end in _chunks(body["date_range"]):
        daily += service.execute({**base, "date_range": {"from": start, "to": end}, "metrics": metrics,
                                  "dimensions": [{"field": "category"}, {"field": "department"}, {"field": "team"}, {"field": "purpose"}, {"field": "date", "granularity": "day"}]}, system)["rows"]
    # The company a crew flies for (the one with most of its flights, should it ever change).
    companies: dict[tuple, tuple[str, int]] = {}
    for row in service.execute({**base, "metrics": [{"key": "flights"}], "dimensions": [{"field": "department"}, {"field": "group"}, {"field": "team"}]}, system)["rows"]:
        crew, flights = (str(row.get("department")), str(row.get("team"))), int(row.get("flights") or 0)
        if flights > companies.get(crew, ("", -1))[1]:
            companies[crew] = (str(row.get("group")), flights)

    group_of = settings.group_of()
    titles = {group.id: group.title for group in settings.mission_groups} | {OTHER_GROUP[0]: OTHER_GROUP[1]}
    ignored = {group.id: {normalize_kpi_text(item) for item in group.ignore_losses_for} for group in settings.mission_groups}
    # (кафедра, group) → crew → totals for that kind of mission.
    buckets: dict[tuple[str, str], dict[tuple, dict]] = {}
    for row in daily:
        if row.get("team") in (None, ""):
            continue
        category, department, team = str(row.get("category")), str(row.get("department")), str(row.get("team"))
        mission = group_of.get(normalize_kpi_text(row.get("purpose")), OTHER_GROUP)[0]
        key = (category, department, team)
        crew = buckets.setdefault((category, mission), {}).setdefault(key, _empty(key, category=category, department=department, team=team))
        flights = int(row.get("flights") or 0)
        crew["flights"] += flights
        crew["effective"] += float(row.get("effective_net") or 0)
        crew["lost"] += int(row.get("lost_devices") or 0)
        if flights:
            crew["days"].add(row.get("date"))

    # Every group a crew flew enough of is scored among the crews of that кафедра and group.
    ranked: dict[tuple[str, str], list[dict]] = {}
    overall: dict[tuple, dict] = {}
    for (category, mission), crews in buckets.items():
        members = [_finish(crew) for crew in crews.values() if crew["flights"] >= settings.min_flights]
        if not members:
            continue
        _score(members, settings, normalize_kpi_text(category) in ignored.get(mission, set()))
        ranked[(category, mission)] = members
        for crew in members:
            total = overall.setdefault(crew["key"], _empty(crew["key"], category=category, department=crew["department"], team=crew["team"], weighted=0.0, missions={}))
            total["flights"] += crew["flights"]
            total["effective"] += crew["effective"]
            total["lost"] += crew["lost"]
            total["weighted"] += crew["score"] * crew["flights"]
            total["missions"][mission] = {"score": crew["score"], "flights": crew["flights"]}
    # Active days across groups: a day counts once even if the crew flew two kinds of missions.
    for (category, mission), crews in buckets.items():
        for key, crew in crews.items():
            if key in overall and mission in overall[key]["missions"]:
                overall[key]["days"].update(crew.get("days") or ())
    by_category: dict[str, list[dict]] = {}
    for crew in overall.values():
        crew["score"] = round(crew.pop("weighted") / crew["flights"], 1)
        by_category.setdefault(crew["category"], []).append(crew)
    for category, members in by_category.items():
        ranked[(category, OVERALL[0])] = members

    own_ids = set(scope.scope_ids)
    output = []
    for (category, mission), members in ranked.items():
        for crew in members:
            crew["group"] = companies.get((crew["department"], crew["team"]), ("", 0))[0]
            if "active_days" not in crew:
                _finish(crew)
        brigade = _places(members)
        battalions: dict[str, list] = {}
        units: dict[tuple, list] = {}
        for crew in members:
            battalions.setdefault(crew["department"], []).append(crew)
            units.setdefault((crew["department"], crew["group"]), []).append(crew)
        battalion_places = {department: _places(rows) for department, rows in battalions.items()}
        company_places = {key: _places(rows) for key, rows in units.items()}
        for crew in members:
            mine = see_all or (crew["department"] in own_ids if scope.scope_type == "DEPARTMENT" else crew["group"] in own_ids)
            if not mine:
                continue
            company = (crew["department"], crew["group"])
            row = {
                "category": category, "mission": mission, "battalion_id": crew["department"], "company_id": crew["group"], "crew": crew["team"],
                "score": crew["score"], "flights": crew["flights"], "active_days": crew["active_days"], "avg_flights": round(crew["avg_flights"], 2),
                "efficiency": round(crew["efficiency"], 1), "lost": crew["lost"],
                "company_place": company_places[company][crew["key"]], "company_total": len(units[company]),
                "battalion_place": battalion_places[crew["department"]][crew["key"]], "battalion_total": len(battalions[crew["department"]]),
                "brigade_place": brigade[crew["key"]], "brigade_total": len(members),
            }
            if mission == OVERALL[0]:
                row["missions"] = crew["missions"]
            else:
                row["parts"] = crew["parts"]
            output.append(row)
    order = [OVERALL[0], *(group.id for group in settings.mission_groups), OTHER_GROUP[0]]
    output.sort(key=lambda row: (order.index(row["mission"]), row["category"], row["brigade_place"], row["crew"]))
    present = {row["mission"] for row in output}
    weights = settings.weights.model_dump()
    return {
        "missions": [{"id": key, "title": OVERALL[1] if key == OVERALL[0] else titles[key]} for key in order if key in present],
        "criteria": [{"key": key, "title": title, "weight": weights[key], "higher_is_better": higher} for key, title, higher in CRITERIA],
        "ignore_losses": {group.id: group.ignore_losses_for for group in settings.mission_groups if group.ignore_losses_for},
        "min_flights": settings.min_flights, "full_list": see_all, "rows": output,
    }
