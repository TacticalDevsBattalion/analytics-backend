"""Parent modes constrain both native and compatibility personal HTTP APIs."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import analytics_pages, bi_routes, personal_dashboard as personal
from app.bi.engine import QueryEngine
from app.bi.models import DashboardDefinition, DataScope, MetricDefinition, WidgetDefinition
from app.bi.security import Principal
from app.bi.store import BiStore


BASE = '/api/v1/analytics/personal-dashboard'
ALIAS = '/api/v1/analytics/dashboards/system-dashboard'


class PersonalDashboardModeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BiStore(Path(directory.name) / 'bi.sqlite3')
        self.store.save('metrics', MetricDefinition(id='flights', key='flights', title='Flights', aggregation='COUNT_DISTINCT', field='flight_id'))
        self.primary = self.widget('system-primary', builtin='summary')
        self.protected = self.widget('system-protected', is_locked=True, mandatory=True)
        self.store.save('dashboards', DashboardDefinition(id='system-dashboard', name='Dashboard', slug='dashboard', layout_mode='CUSTOMIZABLE', widgets=[self.primary, self.protected]))
        self.user = Principal('owner', 'viewer', frozenset({'metric.view', 'personal_dashboard.view', 'personal_dashboard.edit', 'personal_dashboard.widget.create', 'personal_dashboard.widget.edit', 'personal_dashboard.widget.delete', 'personal_dashboard.appearance.edit'}), DataScope(scope_type='ALL'))
        service = QueryEngine(self.store, row_loader=lambda start, end: {'flights': [], 'events': [], 'ammunition': [], 'lost_devices': []})
        for module in (personal, analytics_pages, bi_routes):
            for name, value in (('get_bi_store', self.store), ('engine', service)):
                replacement = patch.object(module, name, return_value=value)
                replacement.start()
                self.addCleanup(replacement.stop)
        app = FastAPI()
        app.include_router(personal.router, prefix='/api')
        app.include_router(bi_routes.router, prefix='/api')
        app.dependency_overrides[bi_routes.principal] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def widget(self, identifier, **values):
        return WidgetDefinition(id=identifier, title=identifier, query={'metrics': [{'key': 'flights'}]}, layout={'x': 0, 'y': 0, 'w': 6, 'h': 2}, **values)

    def mode(self, value):
        definition = self.store.get('dashboards', 'system-dashboard')
        definition.layout_mode = value
        self.store.save('dashboards', definition, definition.revision)

    def own_widget(self, identifier='own-widget'):
        return self.store.save_personal_widget('system-dashboard', 'owner', self.widget(identifier, widget_kind='USER_WIDGET', owner_id='owner'))

    def prefs(self):
        with self.store._connection() as connection:
            return personal.preferences(connection, 'owner')

    def layout(self, identifier='system-primary', y=8):
        return {'widget_id': identifier, 'layout': {'x': 0, 'y': y, 'w': 6, 'h': 2}}

    def migration(self, **updates):
        return {'legacy_preferences': {'version': 1, 'metric_keys': None, 'blocks': None, 'background': 'forest', 'density': 'compact', 'chart_styles': {'timeline': 'line', 'category': 'donut', 'purpose': 'bars'}}, **updates}

    def test_dormant_state_is_ignored_and_restored_when_mode_changes(self):
        own = self.own_widget()
        self.store.save_overrides('system-dashboard', 'owner', [self.layout(), self.layout(own.id, 12)], 0)
        with self.store._connection() as connection:
            values, _, _ = personal.preferences(connection, 'owner')
            values['hidden_widget_ids'] = [self.primary.id]
            personal.put_preferences(connection, 'owner', values, 1, False)
        self.mode('LOCKED')
        locked = self.client.get(BASE).json()
        self.assertEqual({widget['id'] for widget in locked['dashboard']['widgets']}, {self.primary.id, self.protected.id})
        self.assertEqual(locked['dashboard']['widgets'][0]['layout']['y'], 0)
        self.assertEqual(locked['hidden_widget_ids'], [])
        self.assertEqual(self.client.get(ALIAS + '/overrides').json()['overrides'], [])
        self.mode('LAYOUT_EDITABLE')
        editable = self.client.get(BASE).json()
        self.assertEqual(editable['dashboard']['widgets'][0]['layout']['y'], 8)
        self.assertEqual(len(editable['dashboard']['widgets']), 2)
        self.assertEqual(editable['hidden_widget_ids'], [])
        self.mode('CUSTOMIZABLE')
        restored = self.client.get(BASE).json()
        self.assertEqual({widget['id'] for widget in restored['dashboard']['widgets']}, {self.protected.id, own.id})
        self.assertEqual(restored['available_widgets'][0]['layout']['y'], 8)
        self.assertEqual(next(widget for widget in restored['dashboard']['widgets'] if widget['id'] == own.id)['layout']['y'], 12)
        self.assertEqual(len(self.store.overrides_snapshot('system-dashboard', 'owner')['overrides']), 2)

    def test_restrictive_modes_deny_native_and_compatibility_widget_mutations(self):
        own = self.own_widget()
        for mode in ('LOCKED', 'LAYOUT_EDITABLE'):
            self.mode(mode)
            for base in (BASE, ALIAS):
                with self.subTest(mode=mode, api=base):
                    created = self.client.post(base + '/widgets', json=self.widget('new-widget').model_dump(mode='json'))
                    edited = self.client.post(base + '/widgets', json=own.model_dump(mode='json'))
                    deleted = self.client.delete(base + f'/widgets/{own.id}', params={'expected_revision': own.revision})
                    self.assertEqual((created.status_code, edited.status_code, deleted.status_code), (403, 403, 403))
            self.assertEqual(self.client.put(BASE, json={'expected_revision': 0, 'hidden_widget_ids': []}).status_code, 403)
            self.assertEqual(len(self.store.personal_widgets('system-dashboard', 'owner')), 1)
            self.assertEqual(self.prefs()[1], 0)

    def test_locked_layout_rejects_both_apis_but_appearance_is_independent(self):
        self.mode('LOCKED')
        for path in (BASE, ALIAS + '/overrides'):
            self.assertEqual(self.client.put(path, json={'expected_revision': 0, 'overrides': [self.layout()]}).status_code, 403)
        self.user = replace(self.user, permissions=frozenset({'metric.view', 'personal_dashboard.view', 'personal_dashboard.appearance.edit'}))
        response = self.client.put(BASE, json={'expected_revision': 0, 'appearance': {'background': 'navy'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['appearance']['background'], 'navy')
        self.assertEqual(self.store.overrides_snapshot('system-dashboard', 'owner')['overrides'], [])

    def test_layout_editable_preserves_dormant_own_layouts(self):
        own = self.own_widget()
        self.store.save_overrides('system-dashboard', 'owner', [self.layout(own.id, 12)], 0)
        self.mode('LAYOUT_EDITABLE')
        response = self.client.put(BASE, json={'expected_revision': 0, 'overrides': [self.layout()]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['dashboard']['widgets'][0]['layout']['y'], 8)
        self.assertEqual({item.widget_id for item in self.store.overrides_snapshot('system-dashboard', 'owner')['overrides']}, {self.primary.id, own.id})
        self.assertEqual(self.client.put(BASE, json={'expected_revision': 1, 'overrides': [self.layout(own.id)]}).status_code, 403)

    def test_customizable_and_free_allow_crud_hide_and_preserve_flags(self):
        for mode in ('CUSTOMIZABLE', 'FREE'):
            self.mode(mode)
            created = self.client.post(BASE + '/widgets', json=self.widget('own-' + mode).model_dump(mode='json'))
            self.assertEqual(created.status_code, 200)
            edited = {**created.json(), 'title': 'Updated'}
            saved = self.client.post(BASE + '/widgets', json=edited)
            self.assertEqual(saved.status_code, 200)
            revision = self.prefs()[1]
            protected = self.client.put(BASE, json={'expected_revision': revision, 'hidden_widget_ids': [self.protected.id]})
            self.assertEqual(protected.status_code, 403)
            hidden = self.client.put(BASE, json={'expected_revision': revision, 'hidden_widget_ids': [self.primary.id]})
            self.assertEqual(hidden.status_code, 200)
            self.assertNotIn(self.primary.id, {widget['id'] for widget in hidden.json()['dashboard']['widgets']})
            deleted = self.client.delete(BASE + '/widgets/' + saved.json()['id'], params={'expected_revision': saved.json()['revision']})
            self.assertEqual(deleted.status_code, 200)

    def test_blocked_saved_view_migration_does_not_mark_done_and_can_be_retried(self):
        body = self.migration(saved_views=[{'id': 'view', 'name': 'Saved', 'filters': {'date_from': '2026-09-17', 'date_to': '2026-09-17'}}])
        for mode in ('LOCKED', 'LAYOUT_EDITABLE'):
            self.mode(mode)
            self.assertEqual(self.client.post(BASE + '/migrate', json=body).status_code, 403)
            self.assertEqual(self.prefs()[1:], (0, False))
            self.assertEqual(self.store.personal_widgets('system-dashboard', 'owner'), [])
        self.mode('CUSTOMIZABLE')
        first = self.client.post(BASE + '/migrate', json=body)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['migrated'])
        again = self.client.post(BASE + '/migrate', json=body)
        self.assertEqual(again.json()['revision'], first.json()['revision'])
        self.assertEqual(len(self.store.personal_widgets('system-dashboard', 'owner')), 1)
        self.assertEqual(self.prefs()[0]['migration_archive'], body | {'saved_views': [{**body['saved_views'][0], 'created_at': None, 'target': 'dashboard'}]})

    def test_hiding_migration_is_forbidden_but_appearance_only_migration_is_allowed(self):
        body = self.migration()
        body['legacy_preferences']['metric_keys'] = []
        for mode in ('LOCKED', 'LAYOUT_EDITABLE'):
            self.mode(mode)
            self.assertEqual(self.client.post(BASE + '/migrate', json=body).status_code, 403)
            self.assertFalse(self.prefs()[2])
        appearance = self.client.post(BASE + '/migrate', json=self.migration())
        self.assertEqual(appearance.status_code, 200)
        self.assertEqual(appearance.json()['appearance']['background'], 'forest')
        self.assertEqual(appearance.json()['hidden_widget_ids'], [])
        self.assertTrue(appearance.json()['migrated'])


if __name__ == '__main__':
    unittest.main()
