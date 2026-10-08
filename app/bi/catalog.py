"""Public analytics descriptors contain semantic names, never storage mappings."""
from __future__ import annotations

import hashlib

from app.bi.engine import QueryEngine, _canonical
from app.bi.security import PERMISSIONS, require_permission
from app.bi.visualizations import VISUALIZATIONS

# Keys are the stable public DSL; labels describe business entities. Backend
# mappings and physical fields remain in administrative configuration.
DIMENSIONS = {
    'date': ('Дата', 'Час'), 'category': ('Кафедра', 'Контекст місії'),
    'department': ('ББАК / підрозділ', 'Організація'),
    'group': ('Рота', 'Організація'), 'team': ('Екіпаж', 'Організація'),
    'direction': ('АК', 'Контекст місії'), 'purpose': ('Мета', 'Контекст місії'),
    'device': ('Засіб', 'Контекст місії'), 'target_type': ('Тип цілі', 'Результати'),
    'zone': ('Зона відповідальності', 'Контекст місії'),
}
FILTERS = {**DIMENSIONS, 'result': ('Результат події', 'Результати'), 'main_result': ('Основний результат місії', 'Результати'), 'is_effective': ('Результативний виліт', 'Результативність')}
METRIC_CATEGORIES = {
    'flights': 'Активність', 'avg_flights_per_position': 'Активність',
    'effective': 'Результативність', 'efficiency': 'Результативність',
    'weighted_efficiency': 'Результативність', 'detected': 'Результати',
    'affected': 'Результати', 'destroyed': 'Результати', 'target_results': 'Результати',
    'target_affected': 'Результати', 'target_destroyed': 'Результати',
    'target_equivalent': 'Результати', 'losses': 'Втрати', 'survivability': 'Втрати',
    'ammunition': 'Ресурси',
}


def dimension_labels():
    return {key: value[0] for key, value in FILTERS.items()}


def catalog(service: QueryEngine, principal) -> dict:
    require_permission(principal, 'metric.view')
    metadata = service.metadata(principal)
    metrics = []
    for metric in metadata['metrics']:
        category = METRIC_CATEGORIES.get(metric['key'], 'KPI' if metric.get('kind') == 'kpi' else 'Інші показники')
        metrics.append({'key': metric['key'], 'label': metric['title'], 'title': metric['title'], 'description': metric.get('description', ''), 'category': category, 'format': metric.get('format', {}), 'direction': metric.get('direction', 'NEUTRAL'), 'kind': 'kpi' if metric.get('kind') == 'kpi' else 'metric'})
    for key, title in [('avg_flights_per_position', 'Середні вильоти на позицію за добу'), ('weighted_efficiency', 'Зважений KPI місій')]:
        if key == 'weighted_efficiency' and not principal.has('kpi.view'):
            continue
        if not any(item['key'] == key for item in metrics):
            metrics.append({'key': key, 'label': title, 'title': title, 'description': 'Деталізований системний показник', 'category': METRIC_CATEGORIES[key], 'format': {'type': 'percent' if key == 'weighted_efficiency' else 'number', 'decimals': 1}, 'direction': 'HIGHER_IS_BETTER', 'kind': 'builtin'})
    available = {item['field'] for item in metadata['dimensions']}
    dimensions = [{'key': key, 'field': key, 'label': label, 'title': label, 'category': category, 'granularities': ['day', 'week', 'month', 'quarter', 'year'] if key == 'date' else []} for key, (label, category) in DIMENSIONS.items() if key in available]
    filters = [{'key': key, 'field': key, 'label': label, 'title': label, 'category': category, 'operators': ['eq', 'neq', 'in', 'not_in', 'is_null', 'is_not_null'] if key != 'date' else ['eq']} for key, (label, category) in FILTERS.items() if key in metadata['filter_fields']]
    result = {'metrics': metrics, 'dimensions': dimensions, 'filters': filters, 'filter_fields': filters, 'visualizations': list(VISUALIZATIONS), 'permissions': [permission for permission in PERMISSIONS if principal.has(permission)], 'revision': getattr(service.store, 'configuration_revision', lambda: 0)(), 'data_scope': principal.scope.model_dump(mode='json'), 'sections': [{'key': key, 'label': label} for key, label in {'overview': 'Огляд', 'effectiveness': 'Результативність', 'targets': 'Результати за цілями', 'losses': 'Втрати', 'resources': 'Ресурси'}.items()]}
    result['fingerprint'] = hashlib.sha256(_canonical(result).encode()).hexdigest()
    return result


def filter_options(service: QueryEngine, principal, date_range) -> dict:
    require_permission(principal, 'metric.view')
    if (date_range.date_to - date_range.date_from).days > 3660:
        raise ValueError('Filter option range cannot exceed ten years')
    request = {'date_range': date_range.model_dump(mode='json', by_alias=True), 'data_scope': 'USER_SCOPE'}
    dataset = service._load(request, principal)
    from app.bi.engine import _row_value
    results = {}
    for key in FILTERS:
        if key == 'date':
            continue
        values = {}
        for rows in dataset.values():
            for row in rows:
                current = _row_value(row, key)
                for value in current if isinstance(current, list) else [current]:
                    if isinstance(value, (str, int, bool)) and value != '' and len(values) < 2000:
                        label = row.get({'department': 'bbak_title', 'group': 'rota_title'}.get(key, '')) or str(value)
                        values.setdefault(str(value), {'value': value, 'label': str(label)})
        results[key] = [value for _, value in sorted(values.items())]
    return results
