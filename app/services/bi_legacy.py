"""Keep legacy reports available with the same server-enforced BI data scope."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

from fastapi import HTTPException

from app.api.models import FilterRequest
from app.bi.security import apply_scope_to_filter_request, apply_user_data_scope, require_permission, resolve_principal
from app.core.cache import cache
from app.core.config import get_backend_config, get_settings

_principal = ContextVar('legacy_bi_principal', default=None)


@contextmanager
def pin_legacy_principal(principal):
    token = _principal.set(principal)
    try:
        yield
    finally:
        _principal.reset(token)


def current_principal():
    return _principal.get()


def legacy_operation(request, callback, permissions=('dashboard.view', 'statistics.view')):
    principal = resolve_principal(request)
    if not any(principal.has(permission) for permission in permissions):
        raise HTTPException(403, 'This analytics feature is not permitted')
    with pin_legacy_principal(principal):
        return callback()


def scoped_filter(filters):
    principal = current_principal()
    if principal is None:
        return filters
    if (principal.scope.scope_type != 'ALL' or principal.scope.filters) and get_settings().active_source != 'clickhouse':
        raise HTTPException(403, 'Restricted analytics requires the configured ClickHouse source')
    return apply_scope_to_filter_request(filters, principal)


def _hierarchy_rows():
    if get_settings().active_source != 'clickhouse':
        raise ValueError('Organization hierarchy requires ClickHouse')
    from app.services.clickhouse_analytics import clickhouse_analytics, _SqlWhere
    from app.services.analytics import semantic_signature
    keys = ['bbak_id', 'bbak_title', 'rota_id', 'rota_title', 'crew']
    return cache.get_or_load('bi.hierarchy.rows', {'source': semantic_signature()},
        lambda: clickhouse_analytics._query_rows('flight_list', keys, _SqlWhere()),
        ttl_seconds=get_backend_config().cache.ttl_seconds.filter_options,
        dependency_tags=['dictionary'],
        stale_if_error_seconds=0, stale_while_revalidate=False)


def hierarchy_options(principal):
    rows = _hierarchy_rows()
    if principal.is_administrator:
        own = rows
    else:
        from app.bi.security import FIELD_ALIASES
        organization_fields = {'bbak_id', 'bbak_title', 'rota_id', 'rota_title', 'crew'}
        requires_source_rows = any(not any(alias in organization_fields for alias in FIELD_ALIASES.get(condition.field, ())) for condition in principal.scope.filters)
        own = scoped_option_rows(principal)[0] if requires_source_rows else apply_user_data_scope(rows, principal)
    departments = {str(row['bbak_id']) for row in own if row.get('bbak_id') is not None}
    groups = {(str(row['bbak_id']), str(row['rota_id'])) for row in own if row.get('bbak_id') is not None and row.get('rota_id') is not None}
    teams = {(str(row['bbak_id']), str(row['rota_id']), str(row['crew'])) for row in own if row.get('bbak_id') is not None and row.get('rota_id') is not None and row.get('crew') is not None}
    # Peer names are returned only for a separately granted comparison level.
    can_departments = principal.has('comparison.view') and principal.has('comparison.department')
    can_groups = principal.has('comparison.view') and principal.has('comparison.group')
    can_teams = principal.has('comparison.view') and principal.has('comparison.team')
    output = {'departments': {}, 'groups': {}, 'teams': {}}
    for row in rows:
        department = str(row['bbak_id']) if row.get('bbak_id') is not None else None
        group = str(row['rota_id']) if row.get('rota_id') is not None else None
        team = str(row['crew']) if row.get('crew') is not None else None
        if department is None:
            continue
        if department in departments or can_departments:
            output['departments'][department] = {'id': department, 'title': str(row.get('bbak_title') or department)}
        if group is not None and department in departments:
            if (department, group) in groups or can_groups:
                output['groups'][(department, group)] = {'id': group, 'title': str(row.get('rota_title') or group), 'department_id': department}
        if team is not None and group is not None:
            own_team = (department, group, team) in teams
            if own_team or ((department, group) in groups and can_teams):
                output['teams'][(department, group, team)] = {'id': team, 'title': team, 'department_id': department, 'group_id': group}
    return {key: sorted(values.values(), key=lambda item: (item['title'], item['id'])) for key, values in output.items()}


def scoped_option_rows(principal):
    """Share checked, joined rows between legacy filter and KPI dictionaries."""
    if get_settings().active_source != 'clickhouse':
        raise HTTPException(403, 'Scoped filter options require ClickHouse')
    from dataclasses import replace
    from app.bi.models import DataScope
    from app.bi.security import FIELD_ALIASES
    from app.services.clickhouse_analytics import clickhouse_analytics
    from app.bi.engine import SOURCE_FIELDS, _enrich
    filters = apply_scope_to_filter_request(FilterRequest(date_from=date(1970, 1, 1), date_to=date(2149, 6, 6)), principal)

    def load():
        # Event-only scope conditions are applied to the event source and joined
        # back by flight_id. Applying them to raw flights would either discard
        # valid rows or let SQL-ignored event conditions expose every purpose.
        flight_conditions = [condition for condition in principal.scope.filters if any(alias in SOURCE_FIELDS['flights'] for alias in FIELD_ALIASES.get(condition.field, ()))]
        event_conditions = [condition for condition in principal.scope.filters if condition not in flight_conditions]
        base_scope = DataScope(scope_type='ALL' if principal.scope.scope_type == 'CUSTOM' else principal.scope.scope_type, scope_ids=principal.scope.scope_ids, filters=flight_conditions)
        base_principal = replace(principal, scope=base_scope)
        flights = clickhouse_analytics._query_rows('flight_list', sorted(SOURCE_FIELDS['flights']), clickhouse_analytics._flight_where(filters))
        flights = apply_user_data_scope(flights, base_principal)
        if not flights:
            return [], []
        ids = {str(row['flight_id']) for row in flights if row.get('flight_id') is not None}
        events = [row for row in clickhouse_analytics._events(filters) if str(row.get('flight_id')) in ids]
        enriched = _enrich({'flights': flights, 'events': events})
        flights = enriched['flights']
        events = apply_user_data_scope(enriched['events'], principal)
        if event_conditions:
            matching_ids = {str(row['flight_id']) for row in events if row.get('flight_id') is not None}
            flights = [row for row in flights if str(row.get('flight_id')) in matching_ids]
        return flights, events

    # Canonical string preserves ordered custom predicates and scope identity.
    import json
    from app.services.analytics import semantic_signature
    return cache.get_or_load('bi.legacy.option_rows', json.dumps({'principal': principal.cache_key(), 'source': semantic_signature()}, sort_keys=True), load,
        ttl_seconds=get_backend_config().cache.ttl_seconds.filter_options,
        stale_if_error_seconds=0, stale_while_revalidate=False, dependency_tags=['dictionary'])


def scoped_options(principal):
    if principal.scope.scope_type == 'NONE':
        return {key: [] for key in ('direction', 'unit', 'category', 'asset', 'group', 'bbak', 'rota', 'battalion', 'purpose', 'class_name', 'result')}
    flights, events = scoped_option_rows(principal)

    def values(rows, field):
        return sorted({str(row[field]) for row in rows if row.get(field) is not None and str(row[field]).strip()})

    bbak = {int(row['bbak_id']): str(row.get('bbak_title') or row['bbak_id']) for row in flights if row.get('bbak_id') is not None}
    organization = [{'id': key, 'title': title} for key, title in sorted(bbak.items())]
    # Scoped requests are intersected against stable rota IDs, so the legacy
    # dictionary must return those IDs instead of potentially ambiguous titles.
    return {'direction': values(flights, 'direction'), 'unit': values(events, 'units'), 'category': values(flights, 'device_type'), 'asset': values(flights, 'device'), 'group': values(flights, 'crew'), 'bbak': organization, 'battalion': organization, 'rota': values(flights, 'rota_id'), 'purpose': values(flights, 'main_purpose'), 'class_name': values(events, 'target_class'), 'result': values(events, 'result')}
