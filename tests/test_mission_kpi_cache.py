import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.bi.engine import QueryEngine
from app.bi.mission_kpi import MissionKpiRequest, calculate, rule_snapshot
from app.bi.models import DataScope
from app.bi.security import Principal
from app.core.app_configuration import ApplicationConfigurationStore, ConfigurationRevisionNotFound, default_application_configuration
from app.core.cache import cache
from app.core.kpi_configuration import KpiConfiguration, KpiSnapshot, kpi_fingerprint


class EmptyStore:
    def list(self, entity):
        return []


class MissionKpiCacheTests(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.config = KpiConfiguration.model_validate({'purpose_rules': [{'purpose': 'Strike', 'usefulness_percent': 50.0, 'coefficient': 2.0}]})
        self.snapshot = KpiSnapshot(self.config, kpi_fingerprint(self.config), 7)
        replacement = patch('app.bi.mission_kpi.current_kpi_snapshot', side_effect=lambda: self.snapshot)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.user = Principal('one', 'viewer', frozenset({'metric.view', 'kpi.view'}), DataScope(scope_type='DEPARTMENT', scope_ids=[1]))
        self.rows = {'flights': [{'flight_id': 'one', 'date': '2026-09-17', 'bbak_id': 1, 'main_purpose': 'strike', 'is_effective': True}, {'flight_id': 'two', 'date': '2026-09-18', 'bbak_id': 1, 'main_purpose': 'Strike', 'is_effective': False}, {'flight_id': 'private', 'date': '2026-09-17', 'bbak_id': 2, 'main_purpose': 'Strike', 'is_effective': True}], 'events': [], 'ammunition': [], 'lost_devices': []}
        self.calls = []
        def loader(start, end):
            self.calls.append((start, end))
            return copy.deepcopy(self.rows)
        self.service = QueryEngine(EmptyStore(), loader, today=lambda: date(2026, 10, 5))
        self.body = MissionKpiRequest.model_validate({'date_range': {'from': '2026-09-17', 'to': '2026-09-18'}})

    def test_scoped_components_and_paging_share_daily_calculation(self):
        first = calculate(self.body.model_copy(update={'limit': 1}), self.user, self.service)
        second = calculate(self.body.model_copy(update={'offset': 1, 'limit': 1}), self.user, self.service)
        self.assertEqual(first['total_missions'], 2)
        self.assertEqual(first['weighted_efficiency'], 25)
        self.assertEqual(len(first['purposes']), 1)
        self.assertEqual(first['missions'][0]['numerator'], 1)
        self.assertEqual(second['missions'][0]['mission_id'], 'two')
        self.assertEqual(len(self.calls), 1)
        other = calculate(self.body, replace(self.user, scope=DataScope(scope_type='DEPARTMENT', scope_ids=[2])), self.service)
        self.assertEqual(other['total_missions'], 1)
        self.assertEqual(other['missions'][0]['mission_id'], 'private')

    def test_rule_revision_and_late_day_correction_invalidate_semantics(self):
        calculate(self.body, self.user, self.service)
        config = KpiConfiguration.model_validate({'purpose_rules': [{'purpose': 'Strike', 'usefulness_percent': 100.0, 'coefficient': 2.0}]})
        self.snapshot = KpiSnapshot(config, kpi_fingerprint(config), 8)
        changed = calculate(self.body, self.user, self.service)
        self.assertEqual(changed['weighted_efficiency'], 50)
        self.rows['flights'][1]['is_effective'] = True
        cache.invalidate_tags(['data:2026-09-18'])
        corrected = calculate(self.body, self.user, self.service)
        self.assertEqual(corrected['weighted_efficiency'], 100)
        self.assertEqual(self.calls[-1], (date(2026, 9, 18), date(2026, 9, 18)))

    def test_historical_rules_are_explicit_immutable_publications(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ApplicationConfigurationStore(Path(directory) / 'config.sqlite3', default_application_configuration)
            initial = store.snapshot()
            changed = initial.config.model_copy(deep=True)
            changed.kpi = self.config
            draft = store.save_draft(changed, initial.revision)
            current = store.publish(initial.revision, draft.draft_version)
            old = self.body.model_copy(update={'rule_mode': 'HISTORICAL_RULE', 'rule_revision': initial.revision})
            with patch('app.bi.mission_kpi.get_configuration_store', return_value=store):
                self.assertEqual(rule_snapshot(old).config.purpose_rules, [])
                self.assertEqual(rule_snapshot(old.model_copy(update={'rule_revision': current.revision})).config, self.config)
                with self.assertRaises(ConfigurationRevisionNotFound):
                    rule_snapshot(old.model_copy(update={'rule_revision': 999}))
            self.assertEqual(store.snapshot().revision, current.revision)

    def test_missing_revision_or_permission_cannot_execute(self):
        historical = MissionKpiRequest.model_validate({'date_range': self.body.date_range.model_dump(by_alias=True), 'rule_mode': 'HISTORICAL_RULE'})
        self.assertIsNone(historical.rule_revision)
        with self.assertRaises(ValidationError):
            MissionKpiRequest.model_validate({'date_range': self.body.date_range.model_dump(by_alias=True), 'rule_mode': 'CURRENT_RULE', 'rule_revision': 7})
        with self.assertRaises(HTTPException):
            calculate(self.body, replace(self.user, permissions=frozenset({'metric.view'})), self.service)
