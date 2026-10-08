"""Real HTTP boundaries for canonical pages, personal widgets and binary assets."""
import base64
import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import analytics_pages as pages, bi_routes, personal_dashboard as personal
from app.bi.engine import QueryEngine, seed_default_metrics
from app.bi.models import DashboardDefinition, DataScope, WidgetDefinition
from app.bi.security import Principal
from app.bi.store import BiStore
from app.core.cache import cache
from app.core.kpi_configuration import KpiConfiguration, KpiSnapshot, kpi_fingerprint


BASE = '/api/v1/analytics'
PERIOD = {'from': '2026-09-17', 'to': '2026-09-17'}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6I90AAAAASUVORK5CYII=')


class IntegratedAnalyticsApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = BiStore(self.directory / 'bi.sqlite3')
        seed_default_metrics(self.store)
        self.user = Principal('one', 'viewer', frozenset({
            'metric.view', 'kpi.view', 'personal_dashboard.view', 'personal_dashboard.edit',
            'personal_dashboard.widget.create', 'personal_dashboard.widget.edit', 'personal_dashboard.widget.delete',
            'personal_dashboard.appearance.edit',
        }), DataScope(scope_type='DEPARTMENT', scope_ids=['1']))
        self.rows = {
            'flights': [
                {'flight_id': 'own', 'date': '2026-09-17', 'bbak_id': 1, 'rota_id': 11, 'crew': 'Own', 'device_type': 'FPV', 'main_purpose': 'strike', 'is_effective': True},
                {'flight_id': 'foreign-a', 'date': '2026-09-17', 'bbak_id': 2, 'rota_id': 21, 'crew': 'Foreign', 'device_type': 'FPV', 'is_effective': True},
                {'flight_id': 'foreign-b', 'date': '2026-09-17', 'bbak_id': 2, 'rota_id': 21, 'crew': 'Foreign', 'device_type': 'FPV', 'is_effective': False},
            ], 'events': [], 'ammunition': [], 'lost_devices': [],
        }
        self.service = QueryEngine(self.store, lambda start, end: copy.deepcopy(self.rows), today=lambda: date(2026, 10, 5))
        summary = WidgetDefinition(id='main-kpi-flights', title='Вильоти', builtin='summary', widget_type='kpi', query={'metrics': [{'key': 'flights'}]}, visualization={'type': 'kpi'})
        self.store.save('dashboards', DashboardDefinition(id='system-dashboard', name='Dashboard', slug='central-dashboard', layout_mode='CUSTOMIZABLE', widgets=[summary]))
        self.store.save('dashboards', DashboardDefinition(id='system-statistics', name='Statistics', slug='statistics', type='STATISTICS', layout_mode='LAYOUT_EDITABLE', widgets=[summary.model_copy(update={'id': 'stats-flights'})]))
        configuration = KpiConfiguration()
        snapshot = KpiSnapshot(configuration, kpi_fingerprint(configuration), 1)
        for replacement in [
            patch.object(pages, 'get_bi_store', return_value=self.store),
            patch.object(personal, 'get_bi_store', return_value=self.store),
            patch.object(bi_routes, 'get_bi_store', return_value=self.store),
            patch.object(pages, 'engine', return_value=self.service),
            patch.object(personal, 'engine', return_value=self.service),
            patch.object(bi_routes, 'engine', return_value=self.service),
            patch('app.bi.reports.current_kpi_snapshot', return_value=snapshot),
        ]:
            replacement.start()
            self.addCleanup(replacement.stop)
        cache.clear()
        self.addCleanup(cache.clear)
        app = FastAPI()
        app.include_router(bi_routes.router, prefix='/api')
        app.include_router(pages.router, prefix='/api')
        app.include_router(personal.router, prefix='/api')
        app.dependency_overrides[bi_routes.principal] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def widget(self, **updates):
        return {'id': 'personal-flight-count', 'title': 'My count', 'query': {'metrics': [{'key': 'flights'}]}, 'visualization': {'type': 'kpi'}, **updates}

    def test_personal_navigation_is_separate_from_main_and_statistics_permissions(self):
        self.assertFalse(self.user.has('dashboard.view'))
        self.assertFalse(self.user.has('statistics.view'))
        for page in ('dashboard', 'statistics'):
            self.assertEqual(self.client.get(f'{BASE}/pages/{page}').status_code, 403)
            self.assertEqual(self.client.post(f'{BASE}/pages/{page}/query', json={'date_range': PERIOD}).status_code, 403)
        response = self.client.get(f'{BASE}/personal-dashboard')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['dashboard']['name'], 'Мій дашборд')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.user = replace(self.user, denied_permissions=frozenset({'personal_dashboard.view'}))
        self.assertEqual(self.client.get(f'{BASE}/personal-dashboard').status_code, 403)

    def test_personal_queries_and_new_widgets_keep_server_scope_and_owner_isolation(self):
        saved = self.client.post(f'{BASE}/personal-dashboard/widgets', json=self.widget())
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['owner_id'], 'one')
        self.assertEqual(saved.json()['widget_kind'], 'USER_WIDGET')
        response = self.client.post(f'{BASE}/personal-dashboard/query', json={'date_range': PERIOD})
        self.assertEqual(response.status_code, 200, response.text)
        results = {item['widget_id']: item for item in response.json()['results']}
        self.assertEqual(results['main-kpi-flights']['data']['payload'][0]['value'], 1)
        self.assertEqual(results['personal-flight-count']['data']['rows'][0]['flights'], 1)
        tampered = self.client.post(f'{BASE}/personal-dashboard/query', json={'date_range': PERIOD, 'filters': [{'field': 'department', 'operator': 'eq', 'value': 2}]})
        personal_result = next(item for item in tampered.json()['results'] if item['widget_id'] == 'personal-flight-count')
        self.assertEqual(personal_result['data']['status'], 'empty')
        self.assertIsNone(personal_result['data']['rows'][0]['flights'])
        self.user = replace(self.user, user_id='other', scope=DataScope(scope_type='DEPARTMENT', scope_ids=['2']))
        other = self.client.get(f'{BASE}/personal-dashboard')
        self.assertNotIn('personal-flight-count', {item['id'] for item in other.json()['dashboard']['widgets']})
        denied = self.client.delete(f'{BASE}/personal-dashboard/widgets/personal-flight-count', params={'expected_revision': 1})
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(len(self.store.personal_widgets('system-dashboard', 'one')), 1)

    def test_personal_widget_global_and_ownership_tampering_are_rejected(self):
        for body in [
            self.widget(data_scope='GLOBAL'), self.widget(owner_id='other'),
            self.widget(query={'metrics': [{'key': 'flights'}], 'data_scope': 'GLOBAL'}),
            self.widget(query={'metrics': [{'key': 'flights'}], 'fixed_scope': {'scope_type': 'ALL'}}),
        ]:
            response = self.client.post(f'{BASE}/personal-dashboard/widgets', json=body)
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.personal_widgets('system-dashboard', 'one'), [])
        self.user = replace(self.user, permissions=self.user.permissions | {'dashboard.system_global_widget.view'})
        preview = self.client.post(f'{BASE}/widgets/preview', json={'date_range': PERIOD, 'widget': self.widget(data_scope='GLOBAL')})
        self.assertEqual(preview.status_code, 403, preview.text)

    def test_personal_write_origin_and_revisions_enforce_atomic_updates(self):
        url = f'{BASE}/personal-dashboard'
        body = {'expected_revision': 0, 'appearance': {'background': 'navy'}}
        foreign = self.client.put(url, json=body, headers={'Origin': 'https://foreign.invalid'})
        self.assertEqual(foreign.status_code, 403, foreign.text)
        self.assertEqual(self.client.get(url).json()['revision'], 0)
        saved = self.client.put(url, json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['revision'], 1)
        self.assertEqual(saved.json()['appearance']['background'], 'navy')
        stale = self.client.put(url, json={'expected_revision': 0, 'appearance': {'background': 'forest'}})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.client.get(url).json()['appearance']['background'], 'navy')
        widget = self.client.post(f'{BASE}/personal-dashboard/widgets', json=self.widget())
        self.assertEqual(widget.status_code, 200, widget.text)
        self.assertEqual(self.client.post(f'{BASE}/personal-dashboard/widgets', json=self.widget()).status_code, 409)

    def test_appearance_only_permission_does_not_allow_layout_or_hidden_widget_changes(self):
        self.user = replace(self.user, permissions=frozenset({'metric.view', 'personal_dashboard.view', 'personal_dashboard.appearance.edit'}))
        url = f'{BASE}/personal-dashboard'
        updated = self.client.put(url, json={'expected_revision': 0, 'appearance': {'background': 'navy'}})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()['revision'], 1)
        self.assertEqual(updated.json()['appearance']['background'], 'navy')
        for mutation in [{'overrides': []}, {'hidden_widget_ids': []}]:
            response = self.client.put(url, json={'expected_revision': 1, 'appearance': {'background': 'forest'}, **mutation})
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.client.put(url, json={'expected_revision': 1}).status_code, 403)
        self.assertEqual(self.client.put(f'{BASE}/dashboards/system-dashboard/overrides', json={'expected_revision': 1, 'overrides': []}).status_code, 403)
        snapshot = self.client.get(url).json()
        self.assertEqual((snapshot['revision'], snapshot['appearance']['background']), (1, 'navy'))

    def test_canonical_page_writes_require_feature_origin_and_revision(self):
        body = self.store.get('dashboards', 'system-dashboard').model_dump(mode='json')
        self.assertEqual(self.client.put(f'{BASE}/pages/dashboard', json=body).status_code, 403)
        self.user = replace(self.user, permissions=self.user.permissions | {'dashboard.admin_edit', 'dashboard.view'})
        self.assertEqual(self.client.put(f'{BASE}/pages/dashboard', json=body, headers={'Origin': 'https://foreign.invalid'}).status_code, 403)
        body['name'] = 'Administrator updated'
        saved = self.client.put(f'{BASE}/pages/dashboard', json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['name'], 'Administrator updated')
        self.assertEqual(self.client.put(f'{BASE}/pages/dashboard', json=body).status_code, 409)

    def test_tiny_png_upload_readback_and_cross_owner_access(self):
        uploaded = self.client.post(f'{BASE}/personal-dashboard/assets', content=PNG, headers={'Content-Type': 'image/png'})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        identifier = uploaded.json()['asset_id']
        response = self.client.get(uploaded.json()['url'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertIn('no-store', response.headers['cache-control'])
        appearance = self.client.put(f'{BASE}/personal-dashboard', json={'expected_revision': 0, 'appearance': {'background': 'image', 'background_asset_id': identifier}})
        self.assertEqual(appearance.status_code, 200, appearance.text)
        self.assertEqual(appearance.json()['appearance']['background_url'], uploaded.json()['url'])
        self.user = replace(self.user, user_id='other')
        self.assertEqual(self.client.get(uploaded.json()['url']).status_code, 404)
        spoof = self.client.put(f'{BASE}/personal-dashboard', json={'expected_revision': 0, 'appearance': {'background': 'image', 'background_asset_id': identifier}})
        self.assertEqual(spoof.status_code, 404, spoof.text)
        self.assertEqual(self.client.get(f'{BASE}/personal-dashboard').json()['revision'], 0)

    def test_asset_binary_size_content_type_and_origin_limits_precede_persistence(self):
        url = f'{BASE}/personal-dashboard/assets'
        self.assertEqual(self.client.post(url, content=PNG, headers={'Content-Type': 'image/png', 'Origin': 'https://foreign.invalid'}).status_code, 403)
        self.assertEqual(self.client.post(url, content=b'<svg/>', headers={'Content-Type': 'image/svg+xml'}).status_code, 422)
        self.assertEqual(self.client.post(url, content=b'not a png', headers={'Content-Type': 'image/png'}).status_code, 422)
        oversized = self.client.post(url, content=b'\x89PNG\r\n\x1a\n' + b'0' * (5 * 1024 * 1024), headers={'Content-Type': 'image/png'})
        self.assertEqual(oversized.status_code, 413, oversized.text)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM dashboard_assets').fetchone()[0], 0)
        self.assertFalse((self.directory / 'dashboard-assets').exists())

    def test_read_only_personal_permission_cannot_create_widgets_through_legacy_migration(self):
        self.user = replace(self.user, denied_permissions=frozenset({'personal_dashboard.widget.create'}))
        body = {'saved_views': [{'id': 'spoofed-view', 'name': 'Saved', 'target': 'dashboard', 'filters': {'date_from': '2026-09-17', 'date_to': '2026-09-17'}}]}
        response = self.client.post(f'{BASE}/personal-dashboard/migrate', json=body)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.personal_widgets('system-dashboard', 'one'), [])

    def test_legacy_preferences_cannot_hide_locked_system_widgets(self):
        widget = self.store.get('widgets', 'main-kpi-flights')
        widget.is_locked = True
        self.store.save('widgets', widget)
        response = self.client.post(f'{BASE}/personal-dashboard/migrate', json={'legacy_preferences': {'version': 1, 'metric_keys': ['effective'], 'blocks': [], 'background': 'default', 'density': 'comfortable', 'chart_styles': {'timeline': 'line', 'category': 'donut', 'purpose': 'bars'}}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('main-kpi-flights', {item['id'] for item in response.json()['dashboard']['widgets']})
        self.assertNotIn('main-kpi-flights', response.json()['hidden_widget_ids'])

    def test_old_canonical_reads_share_rich_reports_and_do_not_append_personal_widgets(self):
        self.user = replace(self.user, permissions=self.user.permissions | {'dashboard.view'})
        definition = self.store.get('dashboards', 'system-dashboard')
        for key in ('avg_flights_per_position', 'weighted_efficiency'):
            definition.widgets.append(WidgetDefinition(id='summary-' + key, title=key, builtin='summary', query={'metrics': [{'key': key}]}, visualization={'type': 'kpi'}))
        self.store.save('dashboards', definition)
        created = self.client.post(f'{BASE}/personal-dashboard/widgets', json=self.widget())
        self.assertEqual(created.status_code, 200, created.text)
        old = self.client.get(f'{BASE}/dashboards/system-dashboard')
        new = self.client.get(f'{BASE}/pages/dashboard')
        self.assertEqual(old.status_code, 200, old.text)
        self.assertEqual(old.json(), new.json())
        ids = {widget['id'] for widget in old.json()['widgets']}
        self.assertNotIn('personal-flight-count', ids)
        self.assertIn('summary-avg_flights_per_position', ids)
        self.assertIn('summary-weighted_efficiency', ids)
        query = self.client.post(f'{BASE}/dashboards/system-dashboard/query', json={'date_range': PERIOD})
        self.assertEqual(query.status_code, 200, query.text)
        results = {item['widget_id']: item for item in query.json()['results']}
        self.assertNotIn('personal-flight-count', results)
        for key in ('avg_flights_per_position', 'weighted_efficiency'):
            result = results['summary-' + key]
            self.assertEqual(result['status'], 'success', result)
            self.assertEqual(result['data']['payload'][0]['key'], key)

    def test_old_canonical_personal_mutations_honor_explicit_new_denies(self):
        self.user = replace(self.user, permissions=self.user.permissions | {'dashboard.view', 'widget.create', 'widget.edit', 'widget.delete', 'dashboard.layout.edit'})
        old = f'{BASE}/dashboards/system-dashboard'
        baseline = self.user
        self.user = replace(baseline, denied_permissions=frozenset({'personal_dashboard.widget.create'}))
        self.assertEqual(self.client.post(old + '/widgets', json=self.widget()).status_code, 403)
        self.assertEqual(self.store.personal_widgets('system-dashboard', 'one'), [])
        self.user = baseline
        saved = self.client.post(old + '/widgets', json=self.widget())
        self.assertEqual(saved.status_code, 200, saved.text)
        self.user = replace(baseline, denied_permissions=frozenset({'personal_dashboard.widget.edit'}))
        self.assertEqual(self.client.post(old + '/widgets', json={**saved.json(), 'title': 'Changed'}).status_code, 403)
        self.user = replace(baseline, denied_permissions=frozenset({'personal_dashboard.widget.delete'}))
        self.assertEqual(self.client.delete(old + '/widgets/personal-flight-count', params={'expected_revision': 1}).status_code, 403)
        self.user = replace(baseline, denied_permissions=frozenset({'personal_dashboard.edit'}))
        self.assertEqual(self.client.put(old + '/overrides', json={'expected_revision': 0, 'overrides': []}).status_code, 403)
        self.user = replace(baseline, denied_permissions=frozenset({'personal_dashboard.view'}))
        self.assertEqual(self.client.get(old + '/overrides').status_code, 403)
        self.assertEqual(self.store.get_personal_widget('system-dashboard', 'one', 'personal-flight-count').title, 'My count')

    def test_old_canonical_overrides_use_the_same_personal_revision(self):
        old = f'{BASE}/dashboards/system-dashboard/overrides'
        appearance = self.client.put(f'{BASE}/personal-dashboard', json={'expected_revision': 0, 'appearance': {'background': 'navy'}})
        self.assertEqual(appearance.status_code, 200, appearance.text)
        self.assertEqual(self.client.get(old).json()['revision'], 1)
        layout = {'widget_id': 'main-kpi-flights', 'layout': {'x': 0, 'y': 7, 'w': 6, 'h': 4}}
        stale = self.client.put(old, json={'expected_revision': 0, 'overrides': [layout]})
        self.assertEqual(stale.status_code, 409, stale.text)
        updated = self.client.put(old, json={'expected_revision': 1, 'overrides': [layout]})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()['revision'], 2)
        personal = self.client.get(f'{BASE}/personal-dashboard').json()
        self.assertEqual(personal['revision'], 2)
        self.assertEqual(personal['dashboard']['widgets'][0]['layout']['y'], 7)
        self.assertEqual(personal['appearance']['background'], 'navy')

    def test_old_admin_endpoints_do_not_bypass_new_canonical_edit_denies(self):
        self.user = replace(self.user, permissions=self.user.permissions | {'dashboard.manage', 'widget.create', 'widget.edit', 'widget.delete'}, denied_permissions=frozenset({'dashboard.admin_edit', 'statistics.admin_edit'}))
        revision = self.store.revision
        for identifier, widget_id in [('system-dashboard', 'main-kpi-flights'), ('system-statistics', 'stats-flights')]:
            body = self.store.get('dashboards', identifier).model_dump(mode='json')
            self.assertEqual(self.client.post('/api/v1/admin/bi/dashboards', json=body).status_code, 403)
            widget = self.store.get('widgets', widget_id).model_dump(mode='json')
            widget['title'] = 'Bypass'
            self.assertEqual(self.client.post('/api/v1/admin/bi/widgets', json=widget).status_code, 403)
            self.assertEqual(self.client.post('/api/v1/admin/bi/widgets', json=self.widget(id='new-' + identifier, dashboard_id=identifier)).status_code, 403)
            self.assertEqual(self.client.delete('/api/v1/admin/bi/widgets/' + widget_id, params={'expected_revision': 1}).status_code, 403)
            self.assertEqual(self.client.delete('/api/v1/admin/bi/dashboards/' + identifier, params={'expected_revision': 1}).status_code, 403)
        self.assertEqual(self.client.get('/api/v1/admin/bi/dashboards').json(), [])
        self.assertEqual(self.client.get('/api/v1/admin/bi/widgets').json(), [])
        self.assertEqual(self.store.revision, revision)

    def test_old_admin_canonical_widgets_apply_native_protection_even_for_administrators(self):
        self.user = Principal('admin', 'administrator', frozenset({'*'}), DataScope(scope_type='ALL'))
        url = '/api/v1/admin/bi/widgets'
        identifier = 'main-kpi-flights'
        for flags, mutation in [
            ({'is_locked': True}, {'title': 'Changed'}),
            ({'movable': False}, {'layout': {'x': 1, 'y': 0, 'w': 6, 'h': 4}}),
            ({'resizable': False}, {'layout': {'x': 0, 'y': 0, 'w': 5, 'h': 4}}),
        ]:
            widget = self.store.get('widgets', identifier)
            widget.is_locked = False
            widget.movable = widget.resizable = True
            for field, value in flags.items():
                setattr(widget, field, value)
            widget = self.store.save('widgets', widget)
            response = self.client.post(url, json={**widget.model_dump(mode='json'), **mutation})
            self.assertEqual(response.status_code, 403, response.text)
        for field in ('is_locked', 'mandatory', 'removable'):
            widget = self.store.get('widgets', identifier)
            widget.is_locked = widget.mandatory = False
            widget.removable = True
            setattr(widget, field, field != 'removable')
            widget = self.store.save('widgets', widget)
            deleted = self.client.delete(url + '/' + identifier, params={'expected_revision': widget.revision})
            self.assertEqual(deleted.status_code, 403, deleted.text)
            body = self.store.get('dashboards', 'system-dashboard').model_dump(mode='json')
            body['widgets'] = []
            self.assertEqual(self.client.post('/api/v1/admin/bi/dashboards', json=body).status_code, 403)

    def test_old_statistics_overrides_apply_new_edit_denies_and_layout_guards(self):
        self.user = replace(self.user, permissions=self.user.permissions | {'statistics.view', 'dashboard.manage', 'dashboard.layout.edit'}, denied_permissions=frozenset({'statistics.admin_edit'}))
        url = f'{BASE}/dashboards/system-statistics/overrides'
        layout = {'widget_id': 'stats-flights', 'layout': {'x': 0, 'y': 2, 'w': 6, 'h': 4}}
        self.assertEqual(self.client.put(url, json={'expected_revision': 0, 'overrides': [layout]}).status_code, 403)
        self.user = replace(self.user, denied_permissions=frozenset())
        widget = self.store.get('widgets', 'stats-flights')
        widget.movable = False
        self.store.save('widgets', widget)
        self.assertEqual(self.client.put(url, json={'expected_revision': 0, 'overrides': [layout]}).status_code, 403)
        widget = self.store.get('widgets', 'stats-flights')
        widget.movable = True
        widget.resizable = False
        self.store.save('widgets', widget)
        layout['layout'] = {'x': 0, 'y': 0, 'w': 5, 'h': 4}
        self.assertEqual(self.client.put(url, json={'expected_revision': 0, 'overrides': [layout]}).status_code, 403)
        self.assertEqual(self.store.overrides_snapshot('system-statistics', 'one')['revision'], 0)


if __name__ == '__main__':
    unittest.main()
