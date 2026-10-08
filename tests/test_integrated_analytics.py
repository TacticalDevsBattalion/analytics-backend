"""Canonical reports, trusted globals and isolated personal persistence."""
import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from app.api import analytics_pages as pages, personal_dashboard as personal
from app.bi.engine import QueryEngine, QueryPermissionError, seed_default_metrics
from app.bi.cache_policy import CachePolicy
from app.bi.kpi_policy_models import KpiPolicyRule
from app.bi.kpi_policy_store import PolicyStore
from app.bi.models import DashboardDefinition, DataScope, QueryFilter, WidgetDefinition
from app.bi.security import Principal
from app.bi.store import BiStore
from app.bi.widgets import query_page
from app.core.cache import cache
from app.core.kpi_configuration import KpiConfiguration, KpiSnapshot, kpi_fingerprint


class IntegratedAnalyticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = BiStore(Path(temporary.name) / 'bi.sqlite3')
        seed_default_metrics(self.store)
        self.user = Principal('one', 'viewer', frozenset({'metric.view', 'kpi.view', 'personal_dashboard.view', 'personal_dashboard.edit', 'personal_dashboard.widget.create', 'personal_dashboard.widget.edit', 'personal_dashboard.widget.delete', 'personal_dashboard.appearance.edit'}), DataScope(scope_type='DEPARTMENT', scope_ids=['1']))
        self.rows = {'flights': [{'flight_id': 'one', 'date': '2026-09-17', 'time_range': '10:00 - 10:30', 'bbak_id': 1, 'rota_id': 2, 'crew': 'First', 'device_type': 'FPV', 'main_purpose': 'strike', 'is_effective': True}, {'flight_id': 'other', 'date': '2026-09-17', 'bbak_id': 2, 'rota_id': 3, 'crew': 'Other', 'device_type': 'FPV', 'is_effective': True}], 'events': [{'flight_id': 'one', 'date': '2026-09-17', 'result': 'уражено', 'target_class': 'ОС', 'field_300': 5}, {'flight_id': 'other', 'date': '2026-09-17', 'result': 'уражено', 'target_class': 'ОС', 'field_300': 99}], 'ammunition': [], 'lost_devices': []}
        self.loads = []
        def loader(start, end):
            self.loads.append((start, end))
            return copy.deepcopy(self.rows)
        self.service = QueryEngine(self.store, loader, today=lambda: date(2026, 10, 5))
        self.body = pages.PageQuery.model_validate({'date_range': {'from': '2026-09-17', 'to': '2026-09-17'}})
        self.request = Request({'type': 'http', 'method': 'POST', 'headers': [], 'path': '/', 'server': ('localhost', 80), 'scheme': 'http', 'query_string': b''})
        configuration = KpiConfiguration()
        snapshot = KpiSnapshot(configuration, kpi_fingerprint(configuration), 1)
        replacements = [patch.object(pages, 'get_bi_store', return_value=self.store), patch.object(personal, 'get_bi_store', return_value=self.store), patch.object(pages, 'engine', return_value=self.service), patch.object(personal, 'engine', return_value=self.service), patch('app.bi.reports.current_kpi_snapshot', return_value=snapshot), patch('app.core.kpi_configuration.current_kpi_snapshot', return_value=snapshot)]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        cache.clear()
        self.addCleanup(cache.clear)

    def dashboard(self, widgets=None):
        return self.store.save('dashboards', DashboardDefinition(id='system-dashboard', name='Dashboard', slug='central-dashboard', layout_mode='CUSTOMIZABLE', widgets=widgets or [self.widget()]))

    def widget(self, key='flights', **updates):
        return WidgetDefinition(id='summary-' + key, title=key, builtin='summary', widget_type='kpi', query={'metrics': [{'key': key}]}, visualization={'type': 'kpi'}, **updates)

    def test_original_personnel_primary_and_target_secondary_are_scoped(self):
        output = query_page(self.dashboard([self.widget('affected')]), self.body, self.user, self.service)
        value = output['results'][0]['data']['payload'][0]
        self.assertEqual((value['value'], value['secondary_value']), (5, 1))
        self.assertEqual(value['primary_label'], 'ОС')
        self.assertNotIn('99', str(value))

    def test_many_builtin_widgets_share_one_source_snapshot(self):
        query_page(self.dashboard([self.widget('flights'), self.widget('effective')]), self.body, self.user, self.service)
        self.assertEqual(len(self.loads), 1)

    def test_timed_average_preserves_original_elapsed_reporting_period(self):
        self.rows['flights'] = [{'flight_id': 'one', 'date': '2026-09-17', 'time_range': '10:00 - 10:30', 'bbak_id': 1, 'device_type': 'FPV', 'position': 'First'}, {'flight_id': 'two', 'date': '2026-09-18', 'time_range': '10:00 - 10:30', 'bbak_id': 1, 'device_type': 'FPV', 'position': 'First'}]
        body = pages.PageQuery.model_validate({'date_range': {'from': '2026-09-17', 'to': '2026-09-18'}, 'time_from': '09:00', 'time_to': '11:00'})
        result = query_page(self.dashboard([self.widget('avg_flights_per_position')]), body, self.user, self.service)
        # Two flights at one position over 26 hours, rather than two whole days.
        self.assertEqual(result['results'][0]['data']['payload'][0]['value'], 1.8)

    def test_warm_report_skips_source_even_when_raw_dataset_is_too_large_to_cache(self):
        self.service.cache_policy = CachePolicy(raw_max_rows=1)
        definition = self.dashboard([self.widget('flights')])
        first = query_page(definition, self.body, self.user, self.service)
        second = query_page(definition, self.body, self.user, self.service)
        self.assertEqual(first, second)
        self.assertEqual(len(self.loads), 1)

    def test_admin_edited_classic_metric_and_dependency_match_generic_queries_after_warm_cache(self):
        self.rows['flights'].append({'flight_id': 'bomber', 'date': '2026-09-17', 'bbak_id': 1, 'device_type': 'Бомбери', 'main_purpose': 'recon', 'is_effective': True})
        definition = self.dashboard([self.widget('flights'), self.widget('efficiency')])
        first = query_page(definition, self.body, self.user, self.service)
        self.assertEqual(first['results'][0]['data']['payload'][0]['value'], 2)
        metric = self.store.find_by_key('metrics', 'flights')
        self.store.save('metrics', metric.model_copy(update={'filters': [QueryFilter(field='device_type', operator='eq', value='FPV')]}))
        updated = query_page(definition, self.body, self.user, self.service)
        generic = self.service.execute({'metrics': [{'key': 'flights'}, {'key': 'efficiency'}], 'date_range': {'from': '2026-09-17', 'to': '2026-09-17'}}, self.user)['rows'][0]
        flights, efficiency = (result['data']['payload'][0] for result in updated['results'])
        self.assertEqual(flights['value'], generic['flights'])
        self.assertEqual(efficiency['value'], generic['efficiency'])
        self.assertEqual([item['label'] for item in flights['breakdown'] if item['value']], ['FPV'])
        self.assertEqual(len(self.loads), 1)

    def test_cross_day_children_keep_verified_parent_category_without_counting_parent_flight(self):
        self.rows['flights'][0]['date'] = '2026-09-16'
        self.rows['flights'][0]['_bi_parent_context'] = True
        self.rows['ammunition'] = [{'flight_id': 'one', 'date': '2026-09-17', 'bc_name': 'виявлено', 'bc_count': 3}]
        definition = self.dashboard([self.widget('flights'), self.widget('detected'), self.widget('affected'), WidgetDefinition(id='records', title='Records', builtin='records', query={'metrics': []}, visualization={'type': 'table'})])
        output = query_page(definition, self.body, self.user, self.service)
        flights, detected, affected = (item['data']['payload'][0] for item in output['results'][:3])
        self.assertEqual(flights['value'], 0)
        self.assertEqual(detected['breakdown'][0]['label'], 'FPV')
        self.assertEqual(affected['breakdown'][0]['label'], 'FPV')
        self.assertEqual(output['results'][3]['data']['payload'][0]['category'], 'FPV')

    def test_policy_is_used_in_rich_summary_without_legacy_constructor(self):
        PolicyStore(self.store).publish(KpiPolicyRule(name='Policy', valid_from='2026-09-01', context={'category': 'FPV'}, components=[{'key': 'main', 'label': 'Main', 'metric_key': 'successful', 'weight': .65}]))
        with patch('app.services.analytics_engine.PurposeKpiCalculator', side_effect=AssertionError('Legacy calculator invoked')):
            output = query_page(self.dashboard([self.widget('weighted_efficiency')]), self.body, self.user, self.service)
        value = output['results'][0]['data']['payload'][0]
        self.assertEqual(value['value'], 65)
        self.assertEqual(value['mission_details']['calculation_mode'], 'POLICY')

    def test_configured_result_success_matches_generic_and_rich_indicators(self):
        configuration = KpiConfiguration(purpose_rules=[{'purpose': 'strike', 'success_mode': 'results', 'successful_results': ['miss']}])
        snapshot = KpiSnapshot(configuration, kpi_fingerprint(configuration), 2)
        self.rows['flights'][0]['main_result'] = 'miss'
        self.rows['flights'][0]['is_effective'] = False
        with patch('app.core.kpi_configuration.current_kpi_snapshot', return_value=snapshot), patch('app.bi.reports.current_kpi_snapshot', return_value=snapshot):
            generic = self.service.execute({'metrics': [{'key': 'effective'}, {'key': 'efficiency'}], 'date_range': {'from': '2026-09-17', 'to': '2026-09-17'}}, self.user)['rows'][0]
            rich = query_page(self.dashboard([self.widget('effective'), self.widget('efficiency')]), self.body, self.user, self.service)
        self.assertEqual(generic, {'effective': 1, 'efficiency': 100})
        self.assertEqual([item['data']['payload'][0]['value'] for item in rich['results']], [generic['effective'], generic['efficiency']])

    def test_policy_success_virtual_never_evaluates_private_components_or_constructs_legacy(self):
        PolicyStore(self.store).publish(KpiPolicyRule(name='Policy', valid_from='2026-09-01', context={'category': 'FPV'}, components=[{'key': 'main', 'label': 'Main', 'metric_key': 'weighted_efficiency', 'weight': .65}]))
        user = replace(self.user, permissions=self.user.permissions - {'kpi.view'})
        with patch('app.services.purpose_kpi.PurposeKpiCalculator', side_effect=AssertionError('Legacy calculator invoked')):
            result = self.service.execute({'metrics': [{'key': 'effective'}], 'date_range': {'from': '2026-09-17', 'to': '2026-09-17'}}, user)
            with patch('app.bi.reports.score_missions', side_effect=AssertionError('Private policy components invoked')):
                rich = query_page(self.dashboard([self.widget('effective')]), self.body, user, self.service)
        self.assertEqual(result['rows'], [{'effective': 1}])
        self.assertEqual(rich['results'][0]['data']['payload'][0]['value'], 1)

    def test_trusted_stored_global_widget_does_not_authorize_arbitrary_query(self):
        user = replace(self.user, permissions=self.user.permissions | {'dashboard.system_global_widget.view'})
        definition = self.dashboard([self.widget(data_scope='GLOBAL')])
        self.assertEqual(query_page(definition, self.body, user, self.service)['results'][0]['data']['payload'][0]['value'], 2)
        with self.assertRaises(QueryPermissionError):
            self.service.execute({'metrics': [{'key': 'flights'}], 'date_range': {'from': '2026-09-17', 'to': '2026-09-17'}, 'data_scope': 'GLOBAL'}, user)
        with self.assertRaises(HTTPException) as raised:
            pages.preview_widget(pages.WidgetPreview(widget=definition.widgets[0], **self.body.model_dump()), user)
        self.assertEqual(raised.exception.status_code, 403)

    def test_personal_permission_is_independent_of_main_dashboard_navigation(self):
        self.dashboard()
        self.assertFalse(self.user.has('dashboard.view'))
        self.assertEqual(personal.snapshot(self.user)['dashboard'].widgets[0].query.metrics[0].key, 'flights')
        with self.assertRaises(HTTPException):
            pages.load_page('dashboard', self.user)

    def test_personal_revision_and_locked_widget_are_enforced(self):
        self.dashboard([self.widget(is_locked=True)])
        updated = personal.update_personal(personal.PersonalUpdate(expected_revision=0, appearance=personal.PersonalAppearance(background='navy')), self.request, self.user)
        self.assertEqual(updated['revision'], 1)
        self.assertEqual(personal.snapshot(self.user)['appearance']['background'], 'navy')
        with self.assertRaises(HTTPException) as conflict:
            personal.update_personal(personal.PersonalUpdate(expected_revision=0), self.request, self.user)
        self.assertEqual(conflict.exception.status_code, 409)
        with self.assertRaises(HTTPException) as protected:
            personal.update_personal(personal.PersonalUpdate(expected_revision=1, hidden_widget_ids=['summary-flights']), self.request, self.user)
        self.assertEqual(protected.exception.status_code, 403)

    def test_personal_widget_cannot_elevate_scope(self):
        self.dashboard()
        widget = self.widget('effective', data_scope='GLOBAL')
        with self.assertRaises(HTTPException) as raised:
            personal.save_widget(widget, self.request, self.user)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(self.store.personal_widgets('system-dashboard', self.user.user_id), [])

    def test_migration_is_atomic_idempotent_and_keeps_saved_time_bounds(self):
        self.dashboard()
        view = {'id': 'view', 'name': 'Saved', 'target': 'dashboard', 'filters': {'date_from': '2026-09-17', 'date_to': '2026-09-17', 'time_from': '09:00', 'time_to': '11:00'}}
        body = personal.PersonalMigration.model_validate({'saved_views': [view], 'legacy_preferences': {'version': 1, 'metric_keys': ['flights'], 'blocks': [], 'background': 'forest', 'density': 'compact', 'chart_styles': {'timeline': 'line', 'category': 'donut', 'purpose': 'bars'}}})
        first = personal.migrate(body, self.request, self.user)
        second = personal.migrate(body, self.request, self.user)
        self.assertEqual(first['revision'], second['revision'])
        self.assertTrue(second['migrated'])
        imported = self.store.personal_widgets('system-dashboard', 'one')
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].data_scope, 'USER_SCOPE')
        self.assertEqual({condition.field for condition in imported[0].query.filters}, {'local_timestamp'})
        self.assertEqual(second['appearance']['background'], 'forest')
        with self.store._connection() as connection:
            archive = connection.execute('SELECT definition_json FROM personal_dashboard_preferences WHERE user_id=?', ('one',)).fetchone()[0]
        self.assertIn('09:00', archive)

    def test_asset_record_is_owner_scoped(self):
        self.dashboard()
        with self.store._connection() as connection:
            connection.execute('INSERT INTO dashboard_assets VALUES (?,?,?,?,?)', ('a' * 32, 'other', 'image/png', 10, self.store._now()))
        with self.assertRaises(HTTPException) as raised:
            personal.get_asset('a' * 32, self.user)
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == '__main__':
    unittest.main()
