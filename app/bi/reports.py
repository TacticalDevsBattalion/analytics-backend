"""Existing rich reports rendered from the shared, scoped BI data pipeline."""
from __future__ import annotations
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
import hashlib

from app.api.models import FilterRequest
from app.bi.engine import _canonical, _global_filters, default_metric_definitions
from app.bi.catalog import dimension_labels
from app.bi.cache_policy import date_tags
from app.bi.kpi_policy import score_missions
from app.bi.kpi_policy_store import policy_fingerprint
from app.core.cache import cache
from app.core.config import get_backend_config
from app.core.kpi_configuration import current_kpi_snapshot
from app.services.analytics_engine import AnalyticsEngine, _unique_flight_rows

BUILTIN_KEYS = {'flights', 'effective', 'detected', 'affected', 'destroyed', 'efficiency', 'avg_flights_per_position', 'weighted_efficiency'}


def _calculation_fields(metric):
    keys = {'source', 'aggregation', 'field', 'filters', 'formula', 'numerator', 'denominator', 'category_field', 'category_mode', 'weight_set_id', 'normalization', 'normalization_metric', 'minimum', 'maximum', 'weights'}
    return {**{key: metric.get(key) for key in keys}, 'format_type': metric.get('format', {}).get('type'), 'normalization_direction': metric.get('direction') if metric.get('normalization') == 'MIN_MAX' else None}


def _edited_classic_keys(keys, service, definitions):
    template = {metric.key: metric.model_dump(mode='json') for metric in default_metric_definitions()}
    edited = []
    for key in keys:
        if key not in definitions or key in {'weighted_efficiency', 'avg_flights_per_position'}:
            continue
        dependencies = service._dependencies([key], definitions)
        if any(reference not in template or _calculation_fields(metric) != _calculation_fields(template[reference]) for reference, metric in dependencies.items()):
            edited.append(key)
    return edited, template


def _apply_metric_definitions(payload, edited, template, request, principal, service, definitions, weights, dataset):
    """Edited classic indicators use the same definitions as generic queries."""
    if not edited:
        return
    query = service._validated({**request, 'metrics': [{'key': key} for key in edited], 'dimensions': [], 'sort': [], 'top_n': None}, principal, definitions)
    totals = service._evaluate(query, principal, definitions, weights, dataset, apply_scope=False)
    values = totals['rows'][0] if totals['rows'] else {}
    for metric in payload:
        key = metric['key']
        if key not in edited:
            continue
        grouped = {**query, 'metrics': [{'key': key}], 'dimensions': [{'field': 'category'}, *([{'field': 'purpose'}] if key == 'flights' else [])]}
        rows = service._evaluate(grouped, principal, definitions, weights, dataset, apply_scope=False)['rows']
        unit = '%' if definitions[key].get('format', {}).get('type') == 'percent' else None
        by_category = {}
        for row in rows:
            label = str(row.get('category') or get_backend_config().analytics.unknown_device_type_label)
            if key == 'flights':
                category = by_category.setdefault(label, {'key': f'device:{label}', 'label': label, 'value': 0, 'children': []})
                value = row.get(key)
                category['children'].append({'key': str(row.get('purpose') or ''), 'label': str(row.get('purpose') or 'Без мети'), 'value': value, 'unit': unit})
                if value is not None:
                    category['value'] += value
            else:
                by_category[label] = {'key': label, 'label': label, 'value': row.get(key), 'unit': unit}
        # For a custom nonadditive flights definition, calculate category totals
        # independently instead of adding averages or distinct purpose counts.
        if key == 'flights':
            category_rows = service._evaluate({**grouped, 'dimensions': [{'field': 'category'}]}, principal, definitions, weights, dataset, apply_scope=False)['rows']
            for row in category_rows:
                label = str(row.get('category') or get_backend_config().analytics.unknown_device_type_label)
                if label in by_category:
                    by_category[label]['value'] = row.get(key)
        metric.update(value=values.get(key), unit=unit, breakdown=list(by_category.values()), primary_label=None, secondary_label=None, secondary_value=None, numerator=None, denominator=None, calculation_fingerprint=hashlib.sha256(_canonical(service._dependencies([key], definitions)).encode()).hexdigest())
        if definitions[key]['title'] != template.get(key, {}).get('title'):
            metric['label'] = definitions[key]['title']


def _report_filter(request):
    """Retain the original elapsed reporting period after shared time filtering."""
    start, end = (date.fromisoformat(request['date_range'][key]) for key in ('from', 'to'))
    zone = ZoneInfo(get_backend_config().analytics.timezone)
    lower = datetime.combine(start, time.min, zone).timestamp()
    upper = datetime.combine(end, time.max, zone).timestamp()
    timed = False
    for condition in request.get('filters', []):
        if condition['field'] != 'local_timestamp':
            continue
        operator, value = condition['operator'], condition.get('value')
        if operator in {'gte', 'gt'}:
            lower, timed = max(lower, float(value)), True
        elif operator in {'lte', 'lt'}:
            upper, timed = min(upper, float(value)), True
        elif operator == 'between':
            lower, upper, timed = max(lower, float(value[0])), min(upper, float(value[1])), True
    if not timed or lower > upper:
        return FilterRequest(date_from=start, date_to=end)
    beginning, ending = datetime.fromtimestamp(lower, zone), datetime.fromtimestamp(upper, zone)
    return FilterRequest(date_from=beginning.date(), date_to=ending.date(), time_from=beginning.time(), time_to=ending.time())


class ScopedReport(AnalyticsEngine):
    def __init__(self, dataset):
        self.dataset = dataset

    def _dataset(self, filters):
        return tuple(self.dataset.get(source, []) for source in ('flights', 'events', 'ammunition'))

    def _lost_devices(self, filters):
        return self.dataset.get('lost_devices', [])

    def _lost_flights(self, filters):
        # Child rows already inherit verified parent context, including parents
        # outside the selected loss date window.
        return [*self.dataset.get('flights', []), *self.dataset.get('lost_devices', [])]


class MissionCalculator:
    def __init__(self, dataset, scored):
        named = {mission['mission_id']: mission for mission in scored['missions'] if mission['mission_id']}
        anonymous = iter(mission for mission in scored['missions'] if not mission['mission_id'])
        self.values = {}
        for row in _unique_flight_rows(dataset.get('flights', [])):
            identifier = str(row.get('flight_id') or '').strip()
            self.values[id(row)] = named.get(identifier) if identifier else next(anonymous, None)

    def successful(self, row):
        return bool((self.values.get(id(row)) or {}).get('successful'))

    def weights(self, row):
        mission = self.values.get(id(row)) or {}
        return mission.get('numerator') or 0.0, mission.get('denominator') or 0.0


class SuccessCalculator:
    """Basic indicators resolve mission success without private KPI components."""
    def __init__(self, dataset, service):
        resolved = service._mission_success_dataset(dataset)['flights']
        self.values = {id(original): computed['mission_successful'] for original, computed in zip(dataset.get('flights', []), resolved)}

    def successful(self, row):
        return self.values.get(id(row), False)

    def weights(self, row):
        return 0.0, 0.0  # Unselected private KPI values are not calculated.


def builtin_result(widget, query, principal, service):
    from app.bi.security import require_permission
    require_permission(principal, 'metric.view')
    keys = [item.key for item in widget.query.metrics]
    if 'weighted_efficiency' in keys:
        require_permission(principal, 'kpi.view')
    request = query.model_dump(mode='json', by_alias=True)
    definitions, weights = service._definitions()
    dependencies = service._dependencies([key for key in keys if key in definitions], definitions)
    edited, template = _edited_classic_keys(keys, service, definitions)
    service._check_source()
    dataset = None
    def supply():
        nonlocal dataset
        if dataset is None:
            dataset = _global_filters(service._load(request, principal), [condition for condition in request['filters'] if condition['field'] != 'mission_successful'])
            success_filters = [condition for condition in request['filters'] if condition['field'] == 'mission_successful']
            if success_filters:
                dataset = _global_filters(service._mission_success_dataset(dataset), success_filters)
        return dataset
    # Detailed reports can use all child sources, independent of selected card
    # keys. Safe daily link markers let warm reports avoid loading large raw
    # partitions; cross-day links use the same bounded fallback as BI metrics.
    prepared = service._with_parent_tags(
        {**request, 'metrics': [{'key': '_report_context'}]},
        {'_report_context': {'source': 'events', 'aggregation': 'COUNT'}},
        supply, principal,
    )
    snapshot = current_kpi_snapshot()
    start, end = query.date_range.date_from, query.date_range.date_to
    tags = [*date_tags(start, end), 'dictionary', 'kpi:mission', 'kpi:policy']
    tags += [f"metric:{key}" for key in dependencies]
    tags += prepared.get('_cache_parent_tags', [])
    vector = cache.generations(tags)
    stable = not prepared.get('_cache_uncacheable')
    signature = {'source': service.semantic_signature(), 'principal': principal.cache_key(), 'query': request, 'builtin': widget.builtin, 'keys': keys, 'definitions': dependencies, 'weights': weights, 'kpi': snapshot.fingerprint, 'policy': policy_fingerprint(service.store), 'mode': widget.visualization.options.get('rule_mode', 'CURRENT_RULE'), 'prepared_generations': prepared.get('_cache_read_generations'), 'report_generations': vector}

    def compute():
        dataset = supply()
        report = ScopedReport(dataset)
        filters = _report_filter(request)
        calculator = None
        scored = None
        if 'weighted_efficiency' in keys and widget.builtin in {'summary', 'timeline', 'average', 'category', 'purpose'}:
            mode = signature['mode']
            if mode not in {'CURRENT_RULE', 'HISTORICAL_RULE'}:
                raise ValueError('Unknown KPI rule mode')
            scored = score_missions(dataset, principal, service, mode=mode, legacy_snapshot=snapshot, start=start, end=end)
            calculator = MissionCalculator(dataset, scored)
        elif widget.builtin in {'summary', 'timeline', 'average', 'category', 'purpose'}:
            calculator = SuccessCalculator(dataset, service)
        if widget.builtin in {'summary', 'average', 'category', 'purpose'}:
            values = report.overview(filters, snapshot.config, calculator=calculator)
            payload = [metric.model_dump(mode='json') for metric in values if not keys or metric.key in keys]
            for metric in payload:
                if metric['key'] == 'weighted_efficiency' and scored is not None:
                    metric.update(value=scored['score'], numerator=scored['usefulness_points'], denominator=scored['weighted_flights'], calculation_fingerprint=signature['policy'])
                    metric['mission_details'] = scored
            _apply_metric_definitions(payload, edited, template, request, principal, service, definitions, weights, dataset)
            rows = [{metric['key']: metric['value'] for metric in payload}]
            metadata = [{'key': metric['key'], 'title': metric['label'], 'format': definitions[metric['key']].get('format', {}) if metric['key'] in definitions else {'type': 'percent' if metric.get('unit') == '%' else 'number', 'decimals': 1}} for metric in payload]
        elif widget.builtin == 'timeline':
            payload = [point.model_dump(mode='json') for point in report.timeline(filters, snapshot.config, calculator=calculator)]
            rows, metadata = [], []
        elif widget.builtin in {'records', 'losses', 'map'}:
            records = report._lost_device_rows(filters) if widget.builtin == 'losses' else report.events(filters)
            records = [row.model_dump(mode='json') for row in records]
            payload = {'geojson': report.geojson(filters), 'rows': records} if widget.builtin == 'map' else records
            rows, metadata = [], []
        else:
            raise ValueError('Unknown system report')
        return {'status': 'success', 'rows': rows, 'metrics': metadata, 'dimensions': [], 'dimension_labels': dimension_labels(), 'payload': payload}

    return cache.get_or_load('bi.report', _canonical(signature), compute, ttl_seconds=service.cache_policy.ttl_for_range(start, end, service.today()) if stable else 0, dependency_tags=tags, stale_if_error_seconds=0, stale_while_revalidate=False)
