import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.bi.engine import QueryEngine, QueryPermissionError
from app.bi.kpi_policy import PolicyAmbiguity, evaluate_policy, resolve_rule, score_missions
from app.bi.kpi_policy_models import KpiPolicyRule, PolicySimulation, SimulationContext
from app.bi.kpi_policy_store import PolicyStore, policy_fingerprint
from app.bi.mission_kpi import MissionKpiRequest, calculate
from app.bi.models import DataScope, MetricDefinition
from app.bi.security import Principal
from app.bi.store import BiConflict, BiStore
from app.core.cache import cache
from app.core.kpi_configuration import KpiConfiguration, KpiSnapshot, kpi_fingerprint


class KpiPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BiStore(Path(directory.name) / 'bi.sqlite3')
        self.repository = PolicyStore(self.store)
        self.user = Principal('one', 'viewer', frozenset({'metric.view', 'kpi.view', 'kpi.manage'}), DataScope(scope_type='DEPARTMENT', scope_ids=[1]))
        self.context = SimulationContext(category='FPV', purpose='strike', zones=['North'], device_type='FPV', bbak_id='1', date=date(2026, 9, 17))
        self.rows = {'flights': [{'flight_id': 'one', 'date': '2026-09-17', 'bbak_id': 1, 'rota_id': 2, 'crew': 'Crew', 'device_type': 'FPV', 'main_purpose': 'strike', 'is_effective': True}], 'events': [{'flight_id': 'one', 'date': '2026-09-17', 'units': ['North'], 'result': 'уражено'}], 'lost_devices': [{'flight_id': 'one', 'date': '2026-09-18', 'row_hash': 'loss-one'}], 'ammunition': []}
        self.calls = []
        def loader(start, end):
            self.calls.append((start, end))
            return copy.deepcopy(self.rows)
        self.service = QueryEngine(self.store, loader, today=lambda: date(2026, 10, 5))
        configuration = KpiConfiguration.model_validate({'purpose_rules': [{'purpose': 'strike', 'usefulness_percent': 50.0, 'coefficient': 2.0}]})
        self.snapshot = KpiSnapshot(configuration, kpi_fingerprint(configuration), 7)
        cache.clear()
        self.addCleanup(cache.clear)

    def rule(self, **updates):
        payload = {'name': 'Mission policy', 'valid_from': '2026-09-01', 'context': {'category': 'FPV'}, 'components': [{'key': 'main', 'label': 'Основний результат', 'metric_key': 'successful', 'weight': 0.7}, {'key': 'additional', 'label': 'Додатковий результат', 'metric_key': 'event_count', 'weight': 0.15}, {'key': 'zone', 'label': 'Зона', 'metric_key': 'zone_count', 'weight': 0.1}, {'key': 'loss', 'label': 'Втрата засобу', 'metric_key': 'device_loss_count', 'direction': 'negative', 'weight': 0.3}]}
        payload.update(updates)
        return KpiPolicyRule.model_validate(payload)

    def dataset(self):
        return self.service._load({'date_range': {'from': '2026-09-17', 'to': '2026-09-18'}, 'data_scope': 'USER_SCOPE'}, self.user)

    def score(self, **kwargs):
        return score_missions(self.dataset(), self.user, self.service, start=date(2026, 9, 17), end=date(2026, 9, 18), legacy_snapshot=self.snapshot, **kwargs)

    def test_versions_are_immutable_conflicted_and_fingerprint_targeted(self):
        first = self.repository.publish(self.rule())
        initial_hash = policy_fingerprint(self.store)
        self.store.save('metrics', MetricDefinition(key='extra', title='Extra', aggregation='COUNT'))
        self.assertEqual(initial_hash, policy_fingerprint(self.store))
        draft = first.model_copy(deep=True, update={'valid_from': date(2026, 10, 1)})
        draft.components[0].weight = 0.8
        second = self.repository.publish(draft)
        self.assertEqual([rule.version for rule in self.repository.all_versions()], [1, 2])
        self.assertEqual(self.repository.all_versions()[0].components[0].weight, 0.7)
        self.assertNotEqual(initial_hash, policy_fingerprint(self.store))
        self.assertEqual(PolicyStore(BiStore(self.store.path)).list_rules()[0].version, 2)
        with self.assertRaises(BiConflict):
            self.repository.publish(first)
        with self.assertRaises(BiConflict):
            self.repository.publish(second.model_copy(update={'valid_from': date(2026, 8, 1)}))

    def test_context_specificity_is_deterministic_and_overlap_rejected(self):
        generic = self.repository.publish(self.rule(context={}))
        category = self.repository.publish(self.rule())
        specific = self.repository.publish(self.rule(context={'category': ' FPV ', 'purpose': ' STRIKE ', 'zone': 'north'}))
        self.assertEqual(resolve_rule(self.repository.all_versions(), self.context, today=date(2026, 10, 5)).id, specific.id)
        self.assertEqual(resolve_rule([generic, category], self.context, today=date(2026, 10, 5)).id, category.id)
        with self.assertRaises(BiConflict):
            self.repository.publish(self.rule())

    def test_current_and_historical_versions_never_invent_before_validity(self):
        first = self.repository.publish(self.rule())
        second = self.repository.publish(first.model_copy(update={'valid_from': date(2026, 10, 1)}))
        rules = self.repository.all_versions()
        self.assertEqual(resolve_rule(rules, self.context, today=date(2026, 10, 5)).version, second.version)
        self.assertEqual(resolve_rule(rules, self.context, 'HISTORICAL_RULE', today=date(2026, 10, 5)).version, first.version)
        self.assertIsNone(resolve_rule(rules, self.context.model_copy(update={'date': date(2026, 8, 31)}), 'HISTORICAL_RULE'))
        disabled = self.repository.publish(second.model_copy(update={'active': False, 'valid_from': date(2026, 10, 3)}))
        self.assertIsNone(resolve_rule(self.repository.all_versions(), self.context, today=date(2026, 10, 5)))
        self.assertEqual(resolve_rule(self.repository.all_versions(), self.context, 'HISTORICAL_RULE').id, first.id)
        self.assertFalse(disabled.active)

    def test_positive_and_negative_components_join_by_id_without_legacy(self):
        self.repository.publish(self.rule())
        self.rows['lost_devices'].append({'flight_id': 'other-flight', 'date': '2026-09-17'})
        with patch('app.services.purpose_kpi.PurposeKpiCalculator', side_effect=AssertionError('Legacy calculator must not run')):
            result = self.score()
        self.assertEqual(result['calculation_mode'], 'POLICY')
        self.assertAlmostEqual(result['score'], 65)
        components = result['missions'][0]['components']
        self.assertEqual([value['contribution'] for value in components], [70, 15, 10, -30])
        self.assertEqual(result['missions'][0]['rule_version'], 1)

    def test_multi_zone_components_are_explicit_and_loss_is_applied_once(self):
        self.rows['events'][0]['units'] = ['North', 'South']
        rule = self.rule(components=[{'key': 'north', 'label': 'North', 'metric_key': 'event_count', 'zone': 'North', 'weight': 0.2}, {'key': 'south', 'label': 'South', 'metric_key': 'event_count', 'zone': 'South', 'weight': 0.3}, {'key': 'loss', 'label': 'Loss', 'metric_key': 'device_loss_count', 'direction': 'negative', 'weight': 0.1}])
        self.repository.publish(rule)
        result = self.score()
        self.assertEqual(result['score'], 40)
        self.assertEqual(result['missions'][0]['zones'], ['North', 'South'])

    def test_multi_zone_equal_specificity_never_chooses_arbitrary_first(self):
        north = self.repository.publish(self.rule(context={'category': 'FPV', 'zone': 'North'}))
        south = self.repository.publish(self.rule(context={'category': 'FPV', 'zone': 'South'}))
        context = self.context.model_copy(update={'zones': ['North', 'South']})
        for rules in ([north, south], [south, north]):
            with self.assertRaises(PolicyAmbiguity):
                resolve_rule(rules, context, today=date(2026, 10, 5))

    def test_same_simulator_arithmetic_normalizes_thresholds_ratios_and_null(self):
        rule = self.rule(components=[{'key': 'effect', 'label': 'Effect', 'formula': {'op': 'div', 'args': [{'metric': 'event_count'}, {'metric': 'flight_count'}]}, 'normalization': {'min': 0, 'max': 10}, 'weight': 0.5}, {'key': 'loss', 'label': 'Loss', 'metric_key': 'device_loss_count', 'threshold': {'operator': 'gte', 'value': 1}, 'direction': 'negative', 'scale': 10}])
        result = evaluate_policy(rule, {'event_count': 5, 'flight_count': 1, 'device_loss_count': 1})
        self.assertEqual(result['score'], 15)
        self.assertEqual(result['components'][0]['normalized_value'], 0.5)
        null = evaluate_policy(rule, {'event_count': 5, 'flight_count': 0, 'device_loss_count': 0})
        self.assertIsNone(null['score'])
        self.assertIsNone(null['components'][0]['raw_value'])

    def test_external_metric_permissions_and_formula_code_are_enforced(self):
        self.store.save('metrics', MetricDefinition(key='private', title='Private', aggregation='COUNT', permissions=['secret']))
        self.repository.publish(self.rule(components=[{'key': 'x', 'label': 'Private', 'metric_key': 'private'}]))
        with self.assertRaises(QueryPermissionError):
            self.score()
        with self.assertRaises(ValueError):
            self.rule(components=[{'key': 'x', 'label': 'Bad', 'formula': {'op': 'eval', 'args': ['anything']}}])
        with self.assertRaises(ValidationError):
            PolicySimulation.model_validate({'context': self.context.model_dump(mode='json'), 'metrics': {'successful': float('nan')}})

    def test_absent_policy_explicitly_falls_back_and_historical_auto_date_supported(self):
        self.repository.publish(self.rule(valid_from='2026-10-01'))
        historical = self.score(mode='HISTORICAL_RULE')
        self.assertEqual(historical['calculation_mode'], 'LEGACY')
        self.assertEqual(historical['score'], 50)
        self.assertEqual(self.score()['calculation_mode'], 'POLICY')
        body = MissionKpiRequest.model_validate({'date_range': {'from': '2026-09-17', 'to': '2026-09-18'}, 'rule_mode': 'HISTORICAL_RULE'})
        self.assertIsNone(body.rule_revision)

    def test_child_date_correction_invalidates_owning_mission_day(self):
        self.repository.publish(self.rule())
        body = MissionKpiRequest.model_validate({'date_range': {'from': '2026-09-17', 'to': '2026-09-18'}})
        with patch('app.bi.mission_kpi.current_kpi_snapshot', return_value=self.snapshot):
            first = calculate(body, self.user, self.service)
            self.assertEqual(first['score'], 65)
            self.rows['lost_devices'] = []
            cache.invalidate_tags(['data:2026-09-18'])
            second = calculate(body, self.user, self.service)
        self.assertEqual(second['score'], 95)
        self.assertEqual(second['missions'][0]['components'][-1]['raw_value'], 0)

    def test_ambiguous_stable_mission_identifier_fails_closed(self):
        self.repository.publish(self.rule())
        dataset = self.dataset()
        dataset['flights'].append({**dataset['flights'][0], 'bbak_id': 99})
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            score_missions(dataset, self.user, self.service, legacy_snapshot=self.snapshot)


if __name__ == '__main__':
    unittest.main()
