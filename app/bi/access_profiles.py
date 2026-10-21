"""Four access profiles instead of hand-picked permissions.

An administrator answers three questions per user: the profile, the unit
(battalion, and the company for a company commander) and whether exact target
coordinates are shown. The profile expands into the same permissions and data
scope the engine already enforces, so checks elsewhere do not change.

- company:   company commander; sees the crews of one company.
- battalion: battalion commander or analyst; sees every crew of one battalion.
  Both compare with other units only as exact differences by кафедра (never
  the other unit's own values) and see their crews' places in the brigade.
- command:   sees and compares everything, edits only their own dashboard.
- admin:     everything, including administration.
"""
from __future__ import annotations

from typing import Literal

from app.bi.models import DataScope

Profile = Literal["company", "battalion", "command", "admin"]

TITLES: dict[str, str] = {
    "company": "Ротний",
    "battalion": "Батальйонний / аналітик батальйону",
    "command": "Командування",
    "admin": "Адміністратор",
}

VIEW = {"dashboard.view", "statistics.view", "metric.view", "kpi.view"}
# Everyone keeps a personal dashboard limited to their own data scope.
PERSONAL = {
    "personal_dashboard.view", "personal_dashboard.edit", "personal_dashboard.widget.create",
    "personal_dashboard.widget.edit", "personal_dashboard.widget.delete", "personal_dashboard.appearance.edit",
}
# Other units are seen only as differences: no comparison.view_peer_raw.
COMPARE_DIFFERENCE = {
    "comparison.view", "comparison.view_difference", "comparison.select_peer", "comparison.view_breakdown",
    "comparison.department", "comparison.unit", "comparison.group", "comparison.category",
}
COMPARE_ALL = COMPARE_DIFFERENCE | {"comparison.view_peer_raw", "comparison.team", "comparison.crew"}
RANKING = {"ranking.view"}

PERMISSIONS: dict[str, frozenset[str]] = {
    "company": frozenset(VIEW | PERSONAL | COMPARE_DIFFERENCE | RANKING),
    "battalion": frozenset(VIEW | PERSONAL | COMPARE_DIFFERENCE | RANKING),
    "command": frozenset(VIEW | PERSONAL | COMPARE_ALL | RANKING | {
        "analytics.global", "analytics.global_query", "dashboard.system_global_widget.view", "map.coordinates",
    }),
    "admin": frozenset({"*"}),
}


class AccessError(ValueError):
    pass


def permissions_for(profile: str, coordinates: bool) -> frozenset[str]:
    permissions = set(PERMISSIONS[profile])
    if coordinates:
        permissions.add("map.coordinates")
    return frozenset(permissions)


def scope_for(profile: str, battalion_id: str | None, company_id: str | None) -> DataScope:
    if profile == "company":
        return DataScope(scope_type="GROUP", scope_ids=[str(company_id)])
    if profile == "battalion":
        return DataScope(scope_type="DEPARTMENT", scope_ids=[str(battalion_id)])
    return DataScope(scope_type="ALL")


def validate(profile: str, battalion_id: str | None, company_id: str | None, hierarchy: dict) -> tuple[str | None, str | None]:
    """Check the unit against the brigade's battalions and companies."""
    if profile not in TITLES:
        raise AccessError("Unknown access profile")
    if profile in {"command", "admin"}:
        return None, None
    battalions = {item["id"]: item for item in hierarchy.get("departments", [])}
    if not battalion_id or str(battalion_id) not in battalions:
        raise AccessError("Choose the user's battalion")
    if profile == "battalion":
        return str(battalion_id), None
    companies = {item["id"]: item for item in hierarchy.get("groups", []) if item.get("department_id") == str(battalion_id)}
    if not company_id or str(company_id) not in companies:
        raise AccessError("Choose a company of the selected battalion")
    return str(battalion_id), str(company_id)


def legacy_summary(scope_type: str, grants: int) -> str:
    """Access set up the old way keeps working until a profile is chosen."""
    scope = {"ALL": "усі дані", "DEPARTMENT": "дані своїх батальйонів", "GROUP": "дані своїх рот", "TEAM": "дані своїх екіпажів"}.get(scope_type)
    if scope is None:
        return "Старі права без доступу до даних: даних не бачить."
    return f"Старі права (ролі й окремі дозволи, {grants}): бачить {scope}. Працюють як раніше; оберіть профіль, щоб перейти на простий."


def summary(profile: str | None, battalion: str | None, company: str | None, coordinates: bool) -> str:
    """What the user can do, in the words an administrator uses."""
    if profile is None:
        return "Доступ не призначено: даних не бачить."
    unit = f"{company} ({battalion})" if profile == "company" else battalion
    lines = {
        "company": f"Бачить екіпажі роти {unit}. Порівнює роту з іншими лише за різницею по кафедрах; бачить рейтинг своїх екіпажів і їхнє місце в батальйоні та бригаді.",
        "battalion": f"Бачить усі екіпажі батальйону {unit}. Порівнює батальйон з іншими лише за різницею по кафедрах; бачить рейтинг своїх екіпажів і їхнє місце в бригаді.",
        "command": "Бачить і порівнює все: усі батальйони, статистику, рейтинги. Нічого не змінює, крім власного дашборда.",
        "admin": "Повний доступ, зокрема адміністрування.",
    }
    text = lines[profile]
    if profile != "admin":
        text += " Має особистий дашборд із даними свого рівня."
        if profile in {"company", "battalion"}:
            text += " Точні координати на мапі: " + ("так." if coordinates else "ні.")
    return text
