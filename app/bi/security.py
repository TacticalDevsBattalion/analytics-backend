"""Feature permissions and independent data scopes, resolved only on the server."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from fastapi import HTTPException, Request

from app.bi.models import DashboardDefinition, DataScope, QueryFilter, WidgetDefinition
from app.bi.store import BiNotFound, BiStore, get_bi_store
from app.core.user_accounts import (
    account_operation, check_request_origin, get_user_account_store,
    legacy_administrator, require_signed_user,
)

PERMISSIONS = (
    "dashboard.view", "dashboard.manage", "dashboard.layout.edit",
    "widget.create", "widget.edit", "widget.delete",
    "metric.view", "metric.manage", "kpi.view", "kpi.manage",
    "statistics.view", "comparison.view", "comparison.manage",
    "comparison.department", "comparison.group", "comparison.team",
    "comparison.select_peer", "comparison.view_peer_raw",
    "comparison.view_difference", "comparison.view_breakdown",
    "analytics.admin", "analytics.global", "analytics.cache.manage",
    'dashboard.admin_edit', 'statistics.admin_edit',
    'personal_dashboard.view', 'personal_dashboard.edit',
    'personal_dashboard.widget.create', 'personal_dashboard.widget.edit',
    'personal_dashboard.widget.delete', 'personal_dashboard.appearance.edit',
    'dashboard.system_global_widget.view', 'analytics.global_query',
    'comparison.category', 'comparison.unit', 'comparison.crew',
)
PERMISSION_ALIASES = {
    'dashboard.admin_edit': 'dashboard.manage', 'statistics.admin_edit': 'dashboard.manage',
    'analytics.global_query': 'analytics.global',
    'comparison.crew': 'comparison.team', 'comparison.unit': 'comparison.department',
}
DEFAULT_VIEWER_PERMISSIONS = frozenset({"dashboard.view", "statistics.view", "metric.view", "kpi.view", "comparison.view"})

# Every public scope/filter field resolves to canonical, source-derived row keys.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "department": ("department_id", "bbak_id"),
    "department_id": ("department_id", "bbak_id"),
    "bbak_id": ("bbak_id", "department_id"),
    "group": ("group_id", "rota_id"),
    "group_id": ("group_id", "rota_id"),
    "rota_id": ("rota_id", "group_id"),
    "team": ("team_id", "crew"),
    "team_id": ("team_id", "crew"),
    "crew": ("crew", "team_id"),
    "self": ("owner_id", "user_id"),
    "owner_id": ("owner_id", "user_id"),
    "user_id": ("user_id", "owner_id"),
    "category": ("category", "device_type"),
    "status": ("status", "main_result", "result"),
    "purpose": ("purpose", "main_purpose", "flight_purpose"),
    "device": ("device",), "position": ("position",),
    "direction": ("direction",), "date": ("date",),
    "flight_id": ("flight_id",), "is_effective": ("is_effective",),
    "result": ("result",), "main_result": ("main_result",),
    "main_purpose": ("main_purpose",), "flight_purpose": ("flight_purpose",),
    "device_type": ("device_type",), "sub_department": ("sub_department",),
    "target_class": ("target_class",), "target_id": ("target_id",),
    "field_200": ("field_200",), "field_300": ("field_300",),
    "bc_name": ("bc_name",), "bc_count": ("bc_count",),
    "unit_id": ("unit_id",), "unit_title": ("unit_title",),
    "units": ("units",), "device_type_id": ("device_type_id",),
    "zone": ("units",), "target_type": ("target_class",),
    "type": ("type", "device_type", "category"),
    "asset": ("asset", "device"), "class_name": ("class_name", "target_class"),
    "unit": ("unit", "units", "unit_title"),
    "bbak_title": ("bbak_title",), "rota_title": ("rota_title",),
    "timestamp": ("timestamp",), "time": ("time",), "time_range": ("time_range",),
    "flight_number": ("flight_number",), "id": ("id",), "row_hash": ("row_hash",),
}


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    role: str
    permissions: frozenset[str]
    scope: DataScope
    precision: str = "ROUND_5"
    role_ids: frozenset[str] = field(default_factory=frozenset)
    role_keys: frozenset[str] = field(default_factory=frozenset)
    access_revision: int = 0
    denied_permissions: frozenset[str] = field(default_factory=frozenset)

    def has(self, permission: str) -> bool:
        if permission in self.denied_permissions or "*" in self.denied_permissions:
            return False
        if permission in {'analytics.global', 'analytics.global_query'}:
            if self.denied_permissions.intersection({'analytics.global', 'analytics.global_query'}):
                return False
            return bool(self.permissions.intersection({'*', 'analytics.global', 'analytics.global_query'}))
        alias = PERMISSION_ALIASES.get(permission)
        return ("*" in self.permissions or permission in self.permissions
                or (alias is not None and alias in self.permissions and alias not in self.denied_permissions)
                or ("analytics.admin" in self.permissions and "analytics.admin" not in self.denied_permissions and permission in PERMISSIONS and permission not in {'analytics.global', 'analytics.global_query'}))

    @property
    def effective_permissions(self) -> frozenset[str]:
        return frozenset({*(permission for permission in self.permissions if self.has(permission)), *(permission for permission in PERMISSIONS if self.has(permission))})

    @property
    def is_administrator(self):
        return self.has("analytics.admin")

    def cache_key(self):
        # Include SELF identity, permissions, access revision and precision: cached
        # metadata/comparison output must not survive revoked access.
        return {"user_id": self.user_id, "scope": self.scope.model_dump(mode="json"), "permissions": sorted(self.permissions), "denied_permissions": sorted(self.denied_permissions), "access_revision": self.access_revision, "precision": self.precision}


def principal_for_user(user, store: BiStore | None = None) -> Principal:
    store = store or get_bi_store()
    if user.role == "administrator":
        return Principal(user.id, user.role, frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")
    access = store.user_access(user.id)
    permissions = set(access.permissions)
    keys = set()
    for role_id in access.role_ids:
        try:
            role = store.get("roles", role_id)
        except BiNotFound:
            continue
        permissions.update(role.permissions)
        keys.add(role.key)
    # Existing viewers retain read-only feature navigation while new BI data
    # remains unavailable until an administrator assigns an explicit scope.
    if access.revision == 0:
        permissions.update(DEFAULT_VIEWER_PERMISSIONS)
    permissions.difference_update(access.denied_permissions)
    return Principal(user.id, user.role, frozenset(permissions), access.data_scope, access.comparison_precision, frozenset(access.role_ids), frozenset(keys), access.revision, frozenset(access.denied_permissions))


def resolve_principal(request: Request, store: BiStore | None = None) -> Principal:
    if legacy_administrator(request):
        return Principal(None, "administrator", frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")
    from app.core.keycloak import enabled as keycloak_enabled
    if not keycloak_enabled() and not account_operation(lambda: get_user_account_store().enabled()):
        return Principal(None, "viewer", DEFAULT_VIEWER_PERMISSIONS, DataScope(scope_type="ALL"))
    user = require_signed_user(request)
    return principal_for_user(user, store)


def require_permission(principal: Principal, permission: str) -> None:
    if not principal.has(permission):
        raise HTTPException(status_code=403, detail="This analytics feature is not permitted")


def field_value(row: dict[str, Any], name: str):
    aliases = FIELD_ALIASES.get(name)
    if aliases is None:
        raise ValueError("Unknown data-scope field")
    for candidate in aliases:
        if candidate in row and row[candidate] is not None:
            return row[candidate]
    return None


def _equivalent(left, right):
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    return str(left) == str(right)


def matches_filter(row: dict[str, Any], condition: QueryFilter) -> bool:
    value = field_value(row, condition.field)
    expected = condition.value
    operator = condition.operator
    if operator == "is_null":
        return value is None
    if operator == "is_not_null":
        return value is not None
    # Missing fields must never satisfy a restrictive custom scope by accident.
    if value is None:
        return False
    if operator == "eq":
        return _equivalent(value, expected)
    if operator == "neq":
        return not _equivalent(value, expected)
    if operator == "in":
        return any(_equivalent(value, item) for item in expected)
    if operator == "not_in":
        return not any(_equivalent(value, item) for item in expected)
    if operator == "contains":
        return str(expected).casefold() in str(value).casefold()
    if operator == "not_contains":
        return str(expected).casefold() not in str(value).casefold()
    try:
        if operator == "between":
            a, b = expected
            return a <= value <= b
        if operator == "gt":
            return value > expected
        if operator == "gte":
            return value >= expected
        if operator == "lt":
            return value < expected
        if operator == "lte":
            return value <= expected
    except (TypeError, ValueError):
        return False
    return False


def row_in_scope(row: dict[str, Any], scope: DataScope, user_id: str | None = None) -> bool:
    if scope.scope_type == "NONE":
        return False
    if scope.scope_type in {"ALL", "CUSTOM"}:
        base = True
    elif scope.scope_type == "SELF":
        value = field_value(row, "self")
        base = user_id is not None and value is not None and str(value) == user_id
    else:
        field_name = {"DEPARTMENT": "department", "GROUP": "group", "TEAM": "team"}[scope.scope_type]
        value = field_value(row, field_name)
        base = value is not None and str(value) in scope.scope_ids
    if not base or any(not any(alias in row for alias in FIELD_ALIASES.get(condition.field, ())) for condition in scope.filters):
        return False
    return all(matches_filter(row, condition) for condition in scope.filters)


def apply_user_data_scope(rows: Iterable[dict[str, Any]], principal: Principal) -> list[dict[str, Any]]:
    return [row for row in rows if row_in_scope(row, principal.scope, principal.user_id)]


def dashboard_visible(dashboard: DashboardDefinition, principal: Principal) -> bool:
    permission = {"DASHBOARD": "dashboard.view", "STATISTICS": "statistics.view", "COMPARISON": "comparison.view"}[dashboard.type]
    if not principal.has(permission):
        return False
    if principal.is_administrator or not dashboard.assignments:
        return True
    roles = {principal.role, *principal.role_ids, *principal.role_keys}
    context_ids = {"DEPARTMENT": None, "GROUP": None}
    if principal.scope.scope_type in context_ids:
        context_ids[principal.scope.scope_type] = set(principal.scope.scope_ids)
    for condition in principal.scope.filters:
        scope_type = "DEPARTMENT" if condition.field in {"department", "department_id", "bbak_id"} else "GROUP" if condition.field in {"group", "group_id", "rota_id"} else None
        if scope_type and condition.operator in {"eq", "in"}:
            values = condition.value if condition.operator == "in" else [condition.value]
            restriction = {str(value) for value in values}
            context_ids[scope_type] = restriction if context_ids[scope_type] is None else context_ids[scope_type].intersection(restriction)
    for assignment in dashboard.assignments:
        if assignment.assignment_type == "user" and assignment.assignment_id == principal.user_id:
            return True
        if assignment.assignment_type == "role" and assignment.assignment_id in roles:
            return True
        scope_type = {"department": "DEPARTMENT", "group": "GROUP"}.get(assignment.assignment_type)
        if scope_type and ((principal.scope.scope_type == "ALL" and context_ids[scope_type] is None) or assignment.assignment_id in (context_ids[scope_type] or set())):
            return True
    return False


def widget_visible(widget: WidgetDefinition, principal: Principal) -> bool:
    if any(permission in principal.denied_permissions for permission in widget.permissions.required_permissions):
        return False
    if principal.is_administrator:
        return True
    if any(not principal.has(permission) for permission in widget.permissions.required_permissions):
        return False
    roles = {principal.role, *principal.role_ids, *principal.role_keys}
    if widget.permissions.roles and not roles.intersection(widget.permissions.roles):
        return False
    if widget.permissions.users and principal.user_id not in widget.permissions.users:
        return False
    if widget.data_scope == "GLOBAL" and not (principal.has("analytics.global_query") or (widget.widget_kind == 'SYSTEM_WIDGET' and principal.has('dashboard.system_global_widget.view'))):
        return False
    return True


def visible_dashboard(dashboard: DashboardDefinition, principal: Principal, store: BiStore | None = None) -> DashboardDefinition:
    if not dashboard_visible(dashboard, principal):
        raise HTTPException(status_code=404, detail="Dashboard was not found")
    result = dashboard.model_copy(deep=True)
    if store is not None and principal.user_id and result.type == "DASHBOARD" and result.layout_mode in {"CUSTOMIZABLE", "FREE"}:
        result.widgets.extend(store.personal_widgets(dashboard.id, principal.user_id))
    result.widgets = [widget for widget in result.widgets if widget_visible(widget, principal)]
    if store is not None and principal.user_id and result.layout_mode != "LOCKED":
        overrides = {item.widget_id: item for item in store.overrides(dashboard.id, principal.user_id)}
        for widget in result.widgets:
            if widget.id in overrides and not widget.is_locked:
                widget.layout = overrides[widget.id].layout
    return result


def apply_scope_to_filter_request(filters, principal: Principal):
    """Narrow legacy ClickHouse requests with the same independently assigned scope.

    Unsupported SELF/custom legacy sources are rejected rather than approximated.
    A disjoint client filter must not become an empty list (which means all rows).
    """
    scope = principal.scope
    if scope.scope_type == "NONE":
        raise HTTPException(status_code=403, detail="An administrator must assign a data scope")
    field_name = {"DEPARTMENT": "bbak", "GROUP": "rota", "TEAM": "group"}.get(scope.scope_type)
    restrictions = []
    if field_name:
        restrictions.append((field_name, scope.scope_ids))
    elif scope.scope_type not in {"ALL", "CUSTOM"}:
        raise HTTPException(status_code=403, detail="This scope requires an owner-aware analytics source")
    if scope.filters:
        aliases = {"department": "bbak", "department_id": "bbak", "bbak_id": "bbak", "group": "rota", "group_id": "rota", "rota_id": "rota", "team": "group", "team_id": "group", "crew": "group", "category": "category", "device_type": "category", "purpose": "purpose", "direction": "direction", "device": "asset", "result": "result", "target_class": "class_name"}
        for condition in scope.filters:
            target = aliases.get(condition.field)
            if target is None or condition.operator not in {"eq", "in"}:
                raise HTTPException(status_code=403, detail="This scope requires the generic analytics query")
            values = condition.value if condition.operator == "in" else [condition.value]
            restrictions.append((target, [str(value) for value in values]))
    narrowed = filters.model_copy(deep=True)
    for target, allowed in restrictions:
        requested = getattr(narrowed, target)
        chosen = [value for value in requested if str(value) in allowed] if requested else list(allowed)
        if not chosen:
            raise HTTPException(status_code=403, detail="Requested data is outside your assigned scope")
        setattr(narrowed, target, chosen)
    return narrowed
