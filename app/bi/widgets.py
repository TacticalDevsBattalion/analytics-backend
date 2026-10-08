"""Validation and execution shared by the canonical and personal pages."""
from dataclasses import replace

from app.bi.catalog import catalog, dimension_labels
from app.bi.engine import QueryError, validate_filters
from app.bi.models import DataScope, DateRange
from app.bi.reports import BUILTIN_KEYS, builtin_result
from app.bi.security import require_permission
from app.bi.visualizations import VISUALIZATIONS


def validate_widget(widget, user, service):
    if widget.visualization.type not in VISUALIZATIONS:
        raise QueryError('Unsupported visualization type')
    validate_filters([condition.model_dump() for condition in [*widget.query.filters, *widget.fixed_filters, *widget.visibility.conditions]])
    if widget.visualization.type in {'text', 'markdown'}:
        return
    query = widget.query.model_copy(deep=True)
    query.date_range = query.date_range or DateRange.model_validate({'from': service.today(), 'to': service.today()})
    query.data_scope = widget.data_scope
    if widget.data_scope == 'FIXED_SCOPE':
        query.fixed_scope = DataScope(scope_type='CUSTOM', filters=widget.fixed_filters)
    if widget.builtin:
        if widget.builtin in {'summary', 'average', 'category', 'purpose'} and (not query.metrics or any(metric.key not in BUILTIN_KEYS for metric in query.metrics)):
            raise QueryError('The detailed report requires supported system indicators')
        allowed = {item['key'] for item in catalog(service, user)['metrics']}
        if any(metric.key not in allowed for metric in query.metrics):
            raise PermissionError('System indicator is unavailable')
        query.metrics = [reference for reference in query.metrics if reference.key not in {'avg_flights_per_position', 'weighted_efficiency'}]
        if not query.metrics:
            from app.bi.models import MetricReference
            query.metrics = [MetricReference(key='flights')]
    service._validated(query, user, service._definitions()[0])


def condition_visible(widget, filters):
    if widget.visibility.show_when_all_departments and any(condition.field in {'category', 'department_category', 'device_type'} for condition in filters):
        return False
    for expected in widget.visibility.conditions:
        matching = [condition for condition in filters if condition.field == expected.field and condition.operator in {'eq', 'in'}]
        if not matching:
            return False
        actual = {str(value) for condition in matching for value in (condition.value if condition.operator == 'in' else [condition.value])}
        wanted = {str(value) for value in (expected.value if expected.operator in {'in', 'not_in'} else [expected.value])}
        if expected.operator in {'eq', 'in'} and not actual.intersection(wanted):
            return False
        if expected.operator in {'neq', 'not_in'} and actual.intersection(wanted):
            return False
    return True


def query_page(definition, body, user, service):
    from app.bi.engine import _canonical
    original = service._load
    datasets = {}
    def shared_load(request, principal=None, *, comparison_context=None):
        key = _canonical({'date_range': request['date_range'], 'data_scope': request.get('data_scope'), 'fixed_scope': request.get('fixed_scope'), 'principal': principal.cache_key() if principal else None, 'comparison_context': comparison_context})
        if key not in datasets:
            datasets[key] = original(request, principal, comparison_context=comparison_context)
        return datasets[key]
    service._load = shared_load
    try:
        return _query_page(definition, body, user, service)
    finally:
        service._load = original


def _query_page(definition, body, user, service):
    require_permission(user, 'metric.view')
    global_filters = body.bounded_filters()
    validate_filters([condition.model_dump() for condition in global_filters])
    time_filters = global_filters[len(body.filters):]
    if (body.date_range.date_to - body.date_range.date_from).days > 3660:
        raise QueryError('Analytics range cannot exceed ten years')
    results = []
    for widget in definition.widgets:
        if not condition_visible(widget, body.filters):
            results.append({'widget_id': widget.id, 'status': 'empty', 'data': {'status': 'empty', 'rows': [], 'metrics': [], 'dimensions': []}})
            continue
        effective = user
        if widget.data_scope == 'GLOBAL' and not user.has('analytics.global_query'):
            require_permission(user, 'dashboard.system_global_widget.view')
            if widget.widget_kind != 'SYSTEM_WIDGET' or widget.owner_id is not None:
                raise PermissionError('Only stored system widgets can use trusted global access')
            effective = replace(user, permissions=user.permissions | {'analytics.global_query'}, denied_permissions=user.denied_permissions - {'analytics.global', 'analytics.global_query'})
        query = widget.query.model_copy(deep=True)
        query.data_scope = widget.data_scope
        if widget.data_scope == 'FIXED_SCOPE':
            query.fixed_scope = DataScope(scope_type='CUSTOM', filters=widget.fixed_filters)
        query.filters.extend(widget.fixed_filters)
        if widget.inherit_global_filters:
            query.filters.extend([*definition.global_filters, *body.filters])
        if widget.date_mode == 'INHERIT_GLOBAL_DATE':
            query.date_range = body.date_range
            query.filters.extend(time_filters)
        try:
            if widget.visualization.type in {'text', 'markdown'}:
                data = {'status': 'success', 'rows': [], 'metrics': [], 'dimensions': []}
            elif widget.builtin:
                validate_widget(widget, effective, service)
                data = builtin_result(widget, query, effective, service)
            else:
                data = service.execute(query, effective)
                data['dimension_labels'] = dimension_labels()
            results.append({'widget_id': widget.id, 'status': data['status'], 'data': data})
        except (PermissionError, QueryError, ValueError) as exc:
            results.append({'widget_id': widget.id, 'status': 'error', 'error': str(exc)})
    return {'dashboard_id': definition.id, 'revision': definition.revision, 'results': results}
