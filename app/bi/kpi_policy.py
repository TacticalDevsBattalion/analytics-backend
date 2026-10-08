"""Deterministic mission policy resolution and the shared simulator arithmetic."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Callable

from app.bi.formula import FormulaError, evaluate_formula, validate_formula
from app.bi.kpi_policy_models import KpiPolicyRule, SimulationContext
from app.bi.kpi_policy_store import specificity
from app.bi.engine import QueryError
from app.core.kpi_configuration import normalize_kpi_text

BUILTIN_METRICS = {
    'flight_count': 'Вильоти', 'successful': 'Результативність місії',
    'event_count': 'Результати місії', 'detected_count': 'Виявлено',
    'damaged_count': 'Уражено', 'destroyed_count': 'Знищено',
    'additional_result_count': 'Додаткові результати',
    'device_loss_count': 'Втрата засобу', 'ammunition': 'Витрати БК',
    'zone_count': 'Кількість зон',
}


class PolicyAmbiguity(QueryError):
    pass


def _normalized(value):
    return normalize_kpi_text(value) if value is not None else None


def _text(value):
    return str(value).strip() if value is not None else ''


def _matches(rule, context):
    for field in type(rule.context).model_fields:
        expected = getattr(rule.context, field)
        if expected is None:
            continue
        if field == 'zone':
            if _normalized(expected) not in {_normalized(zone) for zone in context.zones}:
                return False
        elif _normalized(expected) != _normalized(getattr(context, field)):
            return False
    return True


def resolve_rule(rules: list[KpiPolicyRule], context: SimulationContext, mode='CURRENT_RULE', *, today: date | None = None):
    if mode not in {'CURRENT_RULE', 'HISTORICAL_RULE'}:
        raise QueryError('Unsupported policy calculation mode')
    if today is None:
        from app.bi.cache_policy import reporting_today
        today = reporting_today()
    applicable_date = context.date if mode == 'HISTORICAL_RULE' else today
    versions = {}
    # Select the effective version first. An inactive/expired newer publication
    # must not silently reactivate a superseded older publication.
    for rule in rules:
        if rule.valid_from > applicable_date:
            continue
        previous = versions.get(rule.id)
        if previous is None or (rule.valid_from, rule.version) > (previous.valid_from, previous.version):
            versions[rule.id] = rule
    matches = [rule for rule in versions.values() if rule.active and (rule.valid_to is None or applicable_date <= rule.valid_to) and _matches(rule, context)]
    if not matches:
        return None
    priority = max(specificity(rule) for rule in matches)
    winners = [rule for rule in matches if specificity(rule) == priority]
    if len(winners) != 1:
        raise PolicyAmbiguity('Mission matches multiple equally specific zone/context rules; use a common rule with explicit component zones')
    return winners[0]


def metric_references(rule):
    references = set()
    for component in rule.components:
        references.update(validate_formula(component.formula) if component.formula is not None else {component.metric_key})
    return references


def validate_rule_metrics(rule, service, principal, day):
    external = sorted(metric_references(rule) - BUILTIN_METRICS.keys())
    definitions, _ = service._definitions()
    for offset in range(0, len(external), 30):
        service._validated({'metrics': [{'key': key} for key in external[offset:offset + 30]], 'date_range': {'from': day.isoformat(), 'to': day.isoformat()}}, principal, definitions)


def evaluate_policy(rule: KpiPolicyRule, metrics: dict[str, float | None], *, zone_metrics=None, resolver: Callable | None = None):
    """Simulator and production both use this exact component calculation."""
    zone_metrics = zone_metrics or {}
    components, contributions = [], {}
    for component in rule.components:
        selected = metrics if component.zone is None else next((values for zone, values in zone_metrics.items() if _normalized(zone) == _normalized(component.zone)), {})
        def value(key):
            return selected[key] if key in selected else resolver(key, component.zone) if resolver is not None else None
        raw = evaluate_formula(component.formula, value) if component.formula is not None else value(component.metric_key)
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or abs(raw) > 1e15):
            raise FormulaError('Policy metric must be a bounded finite number')
        normalized = raw
        if raw is not None and component.threshold is not None:
            threshold = component.threshold
            passed = {'gt': raw > threshold.value, 'gte': raw >= threshold.value, 'lt': raw < threshold.value, 'lte': raw <= threshold.value, 'eq': raw == threshold.value}[threshold.operator]
            if not passed:
                normalized = threshold.else_value
        if normalized is not None and component.normalization is not None:
            normalization = component.normalization
            normalized = max(0.0, min(1.0, (normalized - normalization.minimum) / (normalization.maximum - normalization.minimum)))
            if normalization.direction == 'LOWER_IS_BETTER':
                normalized = 1 - normalized
        contribution = None if normalized is None else normalized * component.weight * component.scale
        if contribution is not None:
            contribution = -abs(contribution) if component.direction == 'negative' else contribution
            if not math.isfinite(contribution) or abs(contribution) > 1e15:
                raise FormulaError('Policy contribution exceeds supported numeric bounds')
        contributions[component.key] = contribution
        components.append({'key': component.key, 'label': component.label, 'metric_key': component.metric_key, 'raw_value': raw, 'normalized_value': normalized, 'weight': component.weight, 'scale': component.scale, 'contribution': contribution, 'direction': component.direction, 'zone': component.zone})
    # A missing component is explicitly represented as NULL, never successful zero.
    score = evaluate_formula(rule.score_formula, contributions.get) if rule.score_formula is not None else None if any(value is None for value in contributions.values()) else sum(contributions.values())
    if score is not None and (not math.isfinite(score) or abs(score) > 1e15):
        raise FormulaError('Policy score exceeds supported numeric bounds')
    return {'score': score, 'rule_set_id': rule.id, 'rule_version': rule.version, 'calculation_mode': 'POLICY', 'components': components}


def zones_for_event(row):
    value = row.get('units')
    if value is None:
        return []
    if isinstance(value, str) and value.lstrip().startswith('['):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            pass
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return sorted({str(item).strip() for item in values if item is not None and str(item).strip()})


def mission_context(row, events):
    return SimulationContext(category=_text(row.get('category', row.get('device_type'))) or None, purpose=_text(row.get('main_purpose')) or None, zones=sorted({zone for event in events for zone in zones_for_event(event)}), device_type=_text(row.get('device_type')) or None, bbak_id=_text(row.get('bbak_id', row.get('department_id'))) or None, device=_text(row.get('device')) or None, date=date.fromisoformat(str(row['date'])[:10]))


def builtin_values(row, events, losses, ammunition):
    names = {'detected_count': {'detected', 'виявлено'}, 'damaged_count': {'damaged', 'уражено'}, 'destroyed_count': {'destroyed', 'знищено'}}
    values = {'flight_count': 1.0, 'successful': 1.0 if row.get('is_effective') is True else 0.0, 'event_count': float(len(events)), 'additional_result_count': float(len(events)), 'device_loss_count': float(len(losses)), 'zone_count': float(len({zone for event in events for zone in zones_for_event(event)})), 'ammunition': sum(float(item.get('bc_count') or 0) for item in ammunition)}
    for key, labels in names.items():
        values[key] = float(sum(_normalized(event.get('result')) in labels for event in events))
    return values


def score_missions(dataset, principal, service, *, mode='CURRENT_RULE', rules=None, legacy_snapshot=None, start=None, end=None):
    """Score each owning flight, joining children only by stable flight_id.

    Scope has already been enforced on the normalized dataset by QueryEngine.
    A missing flight ID never joins anonymous children by date or crew.
    """
    if rules is None:
        from app.bi.kpi_policy_store import PolicyStore
        rules = PolicyStore(service.store).all_versions() if hasattr(service.store, '_connection') else []
    parents = {}
    for flight in dataset.get('flights', []):
        identifier = _text(flight.get('flight_id'))
        if not identifier:
            continue
        identity = tuple(_text(flight.get(field)) for field in ('bbak_id', 'rota_id', 'crew', 'device_type', 'main_purpose', 'device'))
        if identifier in parents and parents[identifier] != identity:
            raise QueryError('Mission identifier has ambiguous organizational or policy context')
        parents[identifier] = identity
    children = {source: defaultdict(list) for source in ('events', 'lost_devices', 'ammunition')}
    for source, indexed in children.items():
        for row in dataset.get(source, []):
            identifier = _text(row.get('flight_id'))
            if identifier:
                indexed[identifier].append(row)
    definitions, weights = service._definitions()
    legacy = None
    seen, missions = set(), []
    for row in dataset.get('flights', []):
        if row.get('_bi_parent_context'):
            continue
        mission_date = date.fromisoformat(str(row['date'])[:10])
        if (start and mission_date < start) or (end and mission_date > end):
            continue
        identifier = _text(row.get('flight_id'))
        if identifier and identifier in seen:
            continue
        if identifier:
            seen.add(identifier)
        events, losses, ammo = (children[source].get(identifier, []) if identifier else [] for source in ('events', 'lost_devices', 'ammunition'))
        context = mission_context(row, events)
        rule = resolve_rule(rules, context, mode, today=service.today())
        successful = row.get('is_effective') is True
        if rule is not None:
            validate_rule_metrics(rule, service, principal, mission_date)
            zone_values = {}
            for zone in context.zones:
                zoned_events = [event for event in events if zone in zones_for_event(event)]
                zone_values[zone] = builtin_values(row, zoned_events, [], [])
            memo = {}
            def resolve(key, zone):
                identity = (key, zone)
                if identity not in memo:
                    population = {'flights': [row], 'events': events, 'lost_devices': losses, 'ammunition': ammo}
                    if zone is not None:
                        population = {**population, 'events': [event for event in events if _normalized(zone) in {_normalized(item) for item in zones_for_event(event)}], 'lost_devices': [], 'ammunition': []}
                    request = service._validated({'metrics': [{'key': key}], 'date_range': {'from': (start or mission_date).isoformat(), 'to': (end or mission_date).isoformat()}}, principal, definitions)
                    result = service._evaluate(request, principal, definitions, weights, population, apply_scope=False)
                    memo[identity] = result['rows'][0].get(key) if result['rows'] else None
                return memo[identity]
            result = evaluate_policy(rule, builtin_values(row, events, losses, ammo), zone_metrics=zone_values, resolver=resolve)
            numerator, denominator = (result['score'] / 100 if result['score'] is not None else None), 1.0
            details = {}
        else:
            if legacy is None:
                from app.core.kpi_configuration import current_kpi_snapshot
                from app.services.purpose_kpi import PurposeKpiCalculator
                configuration = (legacy_snapshot or current_kpi_snapshot()).config
                if service.production_loader:
                    from app.services.clickhouse_analytics import clickhouse_analytics
                    clickhouse_analytics._require_kpi_fields(configuration)
                legacy = PurposeKpiCalculator(configuration)
            numerator, denominator = legacy.weights(row)
            successful = legacy.successful(row)
            legacy_rule = legacy.rule(row)
            result = {'score': numerator / denominator * 100 if denominator > 0 else None, 'rule_set_id': None, 'rule_version': (legacy_snapshot.revision if legacy_snapshot else None), 'calculation_mode': 'LEGACY', 'components': []}
            details = {'success_mode': legacy_rule.success_mode, 'usefulness_percent': legacy_rule.usefulness_percent, 'coefficient': legacy_rule.coefficient}
        missions.append({'mission_id': identifier or None, 'date': context.date.isoformat(), 'department': row.get('department_id', row.get('bbak_id')), 'group': row.get('group_id', row.get('rota_id')), 'team': row.get('team_id', row.get('crew')), 'category': context.category, 'purpose': context.purpose or 'Без мети', 'zones': context.zones, 'successful': successful, 'numerator': numerator, 'denominator': denominator, **result, **details})
    policy_count = sum(mission['calculation_mode'] == 'POLICY' for mission in missions)
    numerator = None if any(mission['numerator'] is None for mission in missions) else sum(mission['numerator'] for mission in missions)
    denominator = sum(mission['denominator'] for mission in missions)
    return {'missions': missions, 'score': None if numerator is None or denominator <= 0 else numerator / denominator * 100, 'policy_missions': policy_count, 'legacy_missions': len(missions) - policy_count, 'calculation_mode': 'MIXED' if 0 < policy_count < len(missions) else 'POLICY' if policy_count else 'LEGACY', 'usefulness_points': numerator, 'weighted_flights': denominator}
