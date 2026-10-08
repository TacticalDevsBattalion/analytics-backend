"""Versioned, scoped mission semantics, stored in bounded daily partitions."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from app.bi.cache_policy import date_tags, days
from app.bi.engine import QueryEngine, _canonical, _date, _global_filters, validate_filters
from app.bi.models import BiModel, DateRange, QueryFilter
from app.bi.security import require_permission
from app.core.app_configuration import get_configuration_store
from app.core.cache import cache
from app.core.kpi_configuration import KpiSnapshot, current_kpi_snapshot, kpi_fingerprint, normalize_kpi_text
from app.bi.kpi_policy import score_missions
from app.bi.kpi_policy_store import PolicyStore, policy_fingerprint
from app.services.purpose_kpi import weighted_percentage


class MissionKpiRequest(BiModel):
    date_range: DateRange
    filters: list[QueryFilter] = Field(default_factory=list, max_length=100)
    rule_mode: Literal['CURRENT_RULE', 'HISTORICAL_RULE'] = 'CURRENT_RULE'
    rule_revision: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode='after')
    def historical_revision(self):
        if self.rule_mode == 'CURRENT_RULE' and self.rule_revision is not None:
            raise ValueError('CURRENT_RULE uses the active revision; an explicit legacy revision belongs to HISTORICAL_RULE')
        if (self.date_range.date_to - self.date_range.date_from).days > 3660:
            raise ValueError('Mission KPI range cannot exceed ten years')
        return self


def rule_snapshot(body: MissionKpiRequest) -> KpiSnapshot:
    if body.rule_mode == 'CURRENT_RULE' or body.rule_revision is None:
        return current_kpi_snapshot()
    config = get_configuration_store().published_revision(body.rule_revision).kpi
    return KpiSnapshot(config, kpi_fingerprint(config), body.rule_revision)


def calculate(body: MissionKpiRequest, principal, service: QueryEngine) -> dict:
    require_permission(principal, 'metric.view')
    require_permission(principal, 'kpi.view')
    filters = validate_filters([condition.model_dump(mode='json') for condition in body.filters])
    snapshot = rule_snapshot(body)
    service._check_source()
    rules = PolicyStore(service.store).all_versions() if hasattr(service.store, '_connection') else []
    start, end = body.date_range.date_from, body.date_range.date_to
    signature = {'source': service.semantic_signature(), 'principal': principal.cache_key(), 'filters': filters, 'rule_set': 'mission', 'rule_mode': body.rule_mode, 'rule_revision': snapshot.revision, 'rule_fingerprint': snapshot.fingerprint, 'policy_fingerprint': policy_fingerprint(service.store) if hasattr(service.store, '_connection') else 'no-policy'}
    # Mission policies join events/losses whose dates can differ from the flight.
    # The observation window is part of every derived partition's identity.
    context = body.date_range.model_dump(mode='json', by_alias=True)
    signature['join_window'] = context
    tags = [*date_tags(start, end), 'dictionary', 'kpi:mission', 'kpi:policy']

    def load():
        partitions = {}
        missing = []
        for day in days(start, end):
            hit, value = cache.probe('bi.mission.period', _canonical({**signature, 'day': day}), dependency_tags=tags)
            if hit:
                partitions[day] = value
            else:
                missing.append(day)
        if missing:
            generations = cache.generations(tags, namespace='bi.mission.period')
            query = {'date_range': {'from': start.isoformat(), 'to': end.isoformat()}, 'data_scope': 'USER_SCOPE'}
            dataset = _global_filters(service._load(query, principal), filters)
            scored = score_missions(dataset, principal, service, mode=body.rule_mode, rules=rules, legacy_snapshot=snapshot, start=start, end=end)
            derived = {day: [] for day in missing}
            for mission in scored['missions']:
                day = _date(mission['date'])
                if day not in derived:
                    continue
                derived[day].append({**mission, 'rule_revision': mission['rule_version'] if mission['calculation_mode'] == 'POLICY' else snapshot.revision, 'rule_fingerprint': signature['policy_fingerprint'] if mission['calculation_mode'] == 'POLICY' else snapshot.fingerprint})
            for day, value in derived.items():
                partitions[day] = value
                ttl = service.cache_policy.ttl_for_day(day, service.today())
                if ttl:
                    cache.put('bi.mission.period', _canonical({**signature, 'day': day}), value, ttl_seconds=ttl, dependency_tags=tags, expected_generations=generations)
        # IDs can occur on more than one day. The reference calculator counts
        # each named flight once; unnamed records remain independent missions.
        missions, seen = [], set()
        for day in days(start, end):
            for value in partitions[day]:
                identifier = str(value.get('mission_id') or '').strip()
                if identifier and identifier in seen:
                    continue
                if identifier:
                    seen.add(identifier)
                missions.append(value)
        purpose_totals = defaultdict(lambda: {'flights': 0, 'successful_flights': 0, 'usefulness_points': 0.0, 'weighted_flights': 0.0})
        labels = {}
        for mission in missions:
            key = normalize_kpi_text(mission['purpose'])
            labels.setdefault(key, mission['purpose'])
            purpose = purpose_totals[key]
            purpose['flights'] += 1
            purpose['successful_flights'] += int(mission['successful'])
            purpose['usefulness_points'] = None if purpose['usefulness_points'] is None or mission['numerator'] is None else purpose['usefulness_points'] + mission['numerator']
            purpose['weighted_flights'] += mission['denominator']
        for rule in snapshot.config.purpose_rules:
            key = normalize_kpi_text(rule.purpose)
            labels.setdefault(key, rule.purpose)
            purpose_totals[key]
        numerator = None if any(mission['numerator'] is None for mission in missions) else sum(mission['numerator'] for mission in missions)
        denominator = sum(mission['denominator'] for mission in missions)
        policy_count = sum(mission['calculation_mode'] == 'POLICY' for mission in missions)
        return {'total_missions': len(missions), 'successful_missions': sum(int(mission['successful']) for mission in missions), 'usefulness_points': numerator, 'weighted_flights': denominator, 'weighted_efficiency': weighted_percentage(numerator, denominator) if numerator is not None else None, 'score': numerator / denominator * 100 if numerator is not None and denominator > 0 else None, 'calculation_mode': 'MIXED' if 0 < policy_count < len(missions) else 'POLICY' if policy_count else 'LEGACY', 'policy_missions': policy_count, 'legacy_missions': len(missions) - policy_count, 'purposes': [{'purpose': labels[key], **totals, 'weighted_efficiency': weighted_percentage(totals['usefulness_points'], totals['weighted_flights']) if totals['usefulness_points'] is not None else None} for key, totals in sorted(purpose_totals.items())], 'missions': missions}

    result = cache.get_or_load('bi.mission.query', _canonical({**signature, 'range': body.date_range}), load, ttl_seconds=service.cache_policy.ttl_for_range(start, end, service.today()), dependency_tags=tags, stale_if_error_seconds=0, stale_while_revalidate=False)
    return {**result, 'missions': result['missions'][body.offset:body.offset + body.limit], 'offset': body.offset, 'limit': body.limit, 'rule_mode': body.rule_mode, 'rule_revision': snapshot.revision, 'rule_fingerprint': snapshot.fingerprint}
