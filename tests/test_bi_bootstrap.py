"""Default BI installation is atomic and never overwrites administrator decisions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bi.bootstrap import bootstrap, _v1_pages, _V1_METRICS
from app.bi.engine import QueryEngine, seed_default_metrics
from app.bi.models import DashboardAssignment, DataScope, MetricDefinition, QueryFilter, WidgetDefinition
from app.core.app_configuration import DashboardConfiguration
from app.bi.security import Principal
from app.bi.store import BiStore


class BiBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "bi.sqlite3"
        self.store = BiStore(self.path)
        self.admin = Principal("admin", "administrator", frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")
        self.configuration = DashboardConfiguration(metric_keys=['flights', 'effective', 'detected', 'affected', 'destroyed', 'efficiency'], blocks=['map', 'timeline', 'departments', 'lost_devices'])
        configuration_patch = patch('app.bi.bootstrap.dashboard_configuration', side_effect=lambda: self.configuration)
        configuration_patch.start()
        self.addCleanup(configuration_patch.stop)

    def install_v1(self):
        class Definitions:
            def seed_definitions(self, metrics=(), **kwargs):
                self.metrics = metrics
        definitions = Definitions()
        seed_default_metrics(definitions)
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for metric in definitions.metrics:
                if metric.key in _V1_METRICS:
                    self.store._save_record(connection, 'metrics', metric, 0)
            for dashboard in _v1_pages():
                self.store._save_record(connection, 'dashboards', dashboard, 0)
                for widget in dashboard.widgets:
                    self.store._save_record(connection, 'widgets', widget.model_copy(update={'dashboard_id': dashboard.id}), 0)
            connection.execute('INSERT INTO bi_bootstrap_versions VALUES (1,?)', (self.store._now(),))

    def assert_layouts_do_not_overlap(self, widgets):
        for index, widget in enumerate(widgets):
            a = widget.layout
            for other in widgets[index + 1:]:
                b = other.layout
                self.assertFalse(a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h, (widget.id, other.id))

    def test_defaults_use_valid_shared_metrics_and_normalized_widgets(self):
        bootstrap(self.store)
        metrics = {item.key: item for item in self.store.list("metrics")}
        self.assertIn("flights", metrics)
        self.assertEqual(metrics["efficiency"].numerator, "effective")
        self.assertEqual(metrics["efficiency"].denominator, "flights")
        dashboards = self.store.list("dashboards")
        self.assertEqual({item.type for item in dashboards}, {"DASHBOARD", "STATISTICS"})
        self.assertEqual(len(dashboards), 2)
        self.assertEqual(len(self.store.list("widgets")), 25)
        self.assertEqual(self.store.get('dashboards', 'system-dashboard').layout_mode, 'CUSTOMIZABLE')
        self.assertEqual(self.store.get('dashboards', 'system-statistics').layout_mode, 'LAYOUT_EDITABLE')
        for dashboard in dashboards:
            self.assertTrue(dashboard.widgets)
            self.assert_layouts_do_not_overlap(dashboard.widgets)
            for widget in dashboard.widgets:
                self.assertEqual(widget.dashboard_id, dashboard.id)
                self.assertGreaterEqual(len(widget.query.metrics), 1)
                self.assertTrue(all(reference.key in metrics or (widget.builtin and reference.key in {'avg_flights_per_position', 'weighted_efficiency'}) for reference in widget.query.metrics))
                self.assertIsNone(widget.owner_id)
        cards = [widget for widget in self.store.get('dashboards', 'system-dashboard').widgets if widget.builtin == 'summary']
        self.assertEqual({widget.id for widget in cards}, {f'main-kpi-{key}' for key in self.configuration.metric_keys})
        self.assertTrue(all(widget.layout.w == widget.layout.h == 2 and len(widget.query.metrics) == 1 for widget in cards))
        targets = self.store.get('widgets', 'target-total')
        self.assertEqual(targets.visualization.type, 'total_with_highlight')
        self.assertEqual([item.key for item in targets.query.metrics], ['target_results', 'target_destroyed'])
        self.assertEqual({item.section for item in self.store.get('dashboards', 'system-statistics').widgets}, {'overview', 'effectiveness', 'targets', 'losses', 'resources', 'positions'})
        with self.store._connection() as connection:
            payloads = [json.loads(row[0]) for row in connection.execute("SELECT definition_json FROM dashboards")]
            self.assertTrue(all(payload["widgets"] == [] for payload in payloads))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bi_bootstrap_versions WHERE version=1").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bi_bootstrap_versions WHERE version=2").fetchone()[0], 1)
            self.assertTrue(connection.execute("SELECT 1 FROM bi_schema_migrations WHERE version=4").fetchone())
        rows = {"flights": [{"flight_id": "a", "date": "2026-09-05", "bbak_id": 1, "rota_id": 11, "crew": "one", "is_effective": True}, {"flight_id": "b", "date": "2026-09-05", "bbak_id": 1, "rota_id": 11, "crew": "one", "is_effective": False}], "events": [], "ammunition": [], "lost_devices": []}
        engine = QueryEngine(self.store, lambda start, end: rows)
        result = engine.execute({"metrics": [{"key": "flights"}, {"key": "effective"}, {"key": "efficiency"}], "date_range": {"from": "2026-09-01", "to": "2026-09-30"}}, self.admin)
        self.assertEqual(result["rows"][0], {"flights": 2, "effective": 1, "efficiency": 50})

    def test_restart_preserves_admin_edits_and_never_reseeds_deleted_entities(self):
        bootstrap(self.store)
        metric = self.store.find_by_key("metrics", "flights")
        metric.title = "Administrator edited title"
        saved = self.store.save("metrics", metric)
        removed_metric = self.store.find_by_key("metrics", "ammunition")
        self.store.delete("metrics", removed_metric.id, removed_metric.revision)
        dashboard = self.store.find_by_key("dashboards", "central-dashboard")
        removed_widget = dashboard.widgets[0]
        self.store.delete("widgets", removed_widget.id, removed_widget.revision)
        statistics = self.store.find_by_key("dashboards", "statistics")
        self.store.delete("dashboards", statistics.id, statistics.revision)
        revision = self.store.revision
        restarted = BiStore(self.path)
        bootstrap(restarted)
        preserved = restarted.find_by_key("metrics", "flights")
        self.assertEqual((preserved.title, preserved.revision), ("Administrator edited title", saved.revision))
        self.assertIsNone(restarted.find_by_key("metrics", "ammunition"))
        self.assertIsNone(restarted.find_by_key("dashboards", "statistics"))
        self.assertNotIn(removed_widget.id, {item.id for item in restarted.list("widgets")})
        self.assertEqual(restarted.revision, revision)

    def install_old_success_calculation(self, **changes):
        bootstrap(self.store)
        current = self.store.find_by_key('metrics', 'effective')
        saved = self.store.save('metrics', current.model_copy(update={'filters': [QueryFilter(field='is_effective', operator='eq', value=True)], **changes}))
        with self.store._connection() as connection:
            connection.execute('DELETE FROM bi_bootstrap_versions WHERE version=3')
        return saved

    def test_success_upgrade_preserves_descriptions_and_runs_after_completed_page_bootstrap(self):
        previous = self.install_old_success_calculation(title='Renamed', description='Administrator description')
        bootstrap(self.store)
        upgraded = self.store.find_by_key('metrics', 'effective')
        self.assertEqual(upgraded.filters[0].field, 'mission_successful')
        self.assertEqual((upgraded.title, upgraded.description), (previous.title, previous.description))
        self.assertEqual(upgraded.revision, previous.revision + 1)
        bootstrap(self.store)
        self.assertEqual(self.store.find_by_key('metrics', 'effective').revision, upgraded.revision)

    def test_success_upgrade_retains_custom_calculation_and_deleted_default(self):
        previous = self.install_old_success_calculation(aggregation='COUNT', field=None)
        bootstrap(self.store)
        self.assertEqual(self.store.find_by_key('metrics', 'effective').model_dump(), previous.model_dump())
        with self.store._connection() as connection:
            connection.execute('DELETE FROM bi_bootstrap_versions WHERE version=3')
        self.store.delete('metrics', previous.id, previous.revision)
        bootstrap(self.store)
        self.assertIsNone(self.store.find_by_key('metrics', 'effective'))

    def test_success_upgrade_interruption_rolls_back_metric_and_marker(self):
        previous = self.install_old_success_calculation()
        with patch.object(self.store, '_save_record', side_effect=RuntimeError('Interrupted')), self.assertRaises(RuntimeError):
            bootstrap(self.store)
        self.assertEqual(self.store.find_by_key('metrics', 'effective').model_dump(), previous.model_dump())
        with self.store._connection() as connection:
            self.assertIsNone(connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=3').fetchone())

    def test_bootstrap_failure_rolls_back_definitions_dashboards_and_version_marker(self):
        self.store.ensure_seed()
        original = self.store._save_record

        def fail_after_first_metric(connection, entity, definition, expected_revision):
            if entity == "metrics" and definition.key == "effective":
                raise RuntimeError("Injected interruption")
            return original(connection, entity, definition, expected_revision)

        with patch.object(self.store, "_save_record", side_effect=fail_after_first_metric), self.assertRaises(RuntimeError):
            bootstrap(self.store)
        self.assertEqual(self.store.list("metrics"), [])
        self.assertEqual(self.store.list("dashboards"), [])
        self.assertEqual(self.store.list("widgets"), [])
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bi_bootstrap_versions").fetchone()[0], 0)
        bootstrap(self.store)
        self.assertEqual(len(self.store.list("dashboards")), 2)

    def test_existing_custom_metric_with_the_default_key_is_retained(self):
        custom = self.store.save("metrics", MetricDefinition(id="custom-flights", key="flights", title="Custom existing flights", aggregation="COUNT"))
        bootstrap(self.store)
        preserved = self.store.find_by_key("metrics", "flights")
        self.assertEqual((preserved.id, preserved.title, preserved.revision), (custom.id, custom.title, custom.revision))

    def test_published_original_configuration_controls_main_cards_and_blocks(self):
        self.configuration = DashboardConfiguration(metric_keys=['weighted_efficiency', 'avg_flights_per_position', 'flights'], blocks=['events', 'purposes'])
        bootstrap(self.store)
        widgets = self.store.get('dashboards', 'system-dashboard').widgets
        self.assertEqual({widget.id for widget in widgets}, {'main-kpi-weighted_efficiency', 'main-kpi-avg_flights_per_position', 'main-kpi-flights', 'main-records', 'main-purpose'})
        self.assertEqual(self.store.get('widgets', 'main-kpi-weighted_efficiency').permissions.required_permissions, ['kpi.view'])
        self.assertEqual(self.store.get('widgets', 'main-records').builtin, 'records')
        self.assertEqual(self.store.get('widgets', 'main-records').visualization.options['legacy_block'], 'events')
        self.assertEqual(self.store.get('widgets', 'main-purpose').builtin, 'purpose')
        self.assertNotIn('weighted_efficiency', {item.key for item in self.store.list('metrics')})

    def test_v1_stock_upgrade_replaces_only_old_defaults_and_imports_new_metrics(self):
        self.install_v1()
        flights = self.store.find_by_key('metrics', 'flights')
        flights.title = 'Custom flights title'
        saved = self.store.save('metrics', flights)
        ammunition = self.store.find_by_key('metrics', 'ammunition')
        self.store.delete('metrics', ammunition.id, ammunition.revision)
        override = self.store.save_override('system-dashboard', 'alice', 'system-widget-trend', {'x': 0, 'y': 10, 'w': 12, 'h': 6})
        bootstrap(self.store)
        self.assertEqual(self.store.find_by_key('metrics', 'flights').model_dump(), saved.model_dump())
        self.assertIsNone(self.store.find_by_key('metrics', 'ammunition'))
        self.assertIsNotNone(self.store.find_by_key('metrics', 'target_results'))
        self.assertFalse({item.id for item in self.store.list('widgets')} & {'system-widget-overview', 'system-widget-trend', 'system-widget-statistics'})
        self.assertNotIn(override.id, {item.id for item in self.store.list('overrides')})
        self.assertEqual(self.store.get('dashboards', 'system-dashboard').layout_mode, 'CUSTOMIZABLE')
        self.assertEqual(self.store.get('dashboards', 'system-statistics').layout_mode, 'LAYOUT_EDITABLE')
        self.assertEqual(len(self.store.list('widgets')), 25)

    def test_upgrade_retains_edited_widgets_layouts_parent_assignments_and_locked_policy(self):
        self.install_v1()
        overview = self.store.get('widgets', 'system-widget-overview')
        overview.title = 'Edited overview'
        edited = self.store.save('widgets', overview)
        custom = self.store.save('widgets', WidgetDefinition(id='custom-overview', dashboard_id='system-dashboard', title='Custom', layout={'x': 0, 'y': 30, 'w': 12, 'h': 4}))
        dashboard = self.store.get('dashboards', 'system-dashboard')
        dashboard.name = 'My locked shared dashboard'
        dashboard.layout_mode = 'LOCKED'
        dashboard.assignments = [DashboardAssignment(assignment_type='role', assignment_id='viewer')]
        saved_dashboard = self.store.save('dashboards', dashboard)
        before = self.store.get('widgets', edited.id)
        custom_before = self.store.get('widgets', custom.id)
        bootstrap(self.store)
        page = self.store.get('dashboards', 'system-dashboard')
        self.assertEqual((page.name, page.layout_mode, page.assignments, page.revision), (saved_dashboard.name, 'LOCKED', saved_dashboard.assignments, saved_dashboard.revision))
        self.assertEqual(self.store.get('widgets', edited.id).model_dump(), before.model_dump())
        self.assertEqual(self.store.get('widgets', custom.id).model_dump(), custom_before.model_dump())
        new_widgets = [item for item in page.widgets if item.id.startswith('main-')]
        self.assertTrue(new_widgets)
        self.assertTrue(all(item.layout.y >= 34 for item in new_widgets))
        self.assert_layouts_do_not_overlap(page.widgets)

    def test_v1_deleted_content_and_dashboard_are_not_restored(self):
        self.install_v1()
        for identifier in ('system-widget-overview', 'system-widget-trend'):
            widget = self.store.get('widgets', identifier)
            self.store.delete('widgets', identifier, widget.revision)
        statistics = self.store.get('dashboards', 'system-statistics')
        self.store.delete('dashboards', statistics.id, statistics.revision)
        bootstrap(self.store)
        ids = {item.id for item in self.store.list('widgets')}
        self.assertFalse(any(identifier.startswith('main-kpi-') for identifier in ids))
        self.assertNotIn('main-timeline', ids)
        self.assertIsNone(self.store.find_by_key('dashboards', 'statistics'))
        self.assertIn('main-map', ids)

    def test_v1_deleted_statistics_widget_keeps_other_custom_content(self):
        self.install_v1()
        original = self.store.get('widgets', 'system-widget-statistics')
        self.store.delete('widgets', original.id, original.revision)
        custom = self.store.save('widgets', WidgetDefinition(id='custom-statistics', dashboard_id='system-statistics', title='Custom stats'))
        bootstrap(self.store)
        self.assertEqual([item.id for item in self.store.get('dashboards', 'system-statistics').widgets], [custom.id])

    def test_v1_widget_with_changed_payload_at_revision_one_is_retained(self):
        self.install_v1()
        with self.store._connection() as connection:
            original = json.loads(connection.execute("SELECT definition_json FROM dashboard_widgets WHERE id='system-widget-trend'").fetchone()[0])
            original['title'] = 'Imported custom title'
            connection.execute("UPDATE dashboard_widgets SET definition_json=? WHERE id='system-widget-trend'", (json.dumps(original),))
        bootstrap(self.store)
        preserved = self.store.get('widgets', 'system-widget-trend')
        self.assertEqual((preserved.title, preserved.revision), ('Imported custom title', 1))
        self.assertGreaterEqual(self.store.get('widgets', 'main-timeline').layout.y, preserved.layout.y + preserved.layout.h)

    def test_v1_upgrade_failure_rolls_back_old_stock_deletion_and_new_version(self):
        self.install_v1()
        before = {item.id: item.model_dump() for item in self.store.list('widgets')}
        revision = self.store.revision
        original = self.store._save_record
        def interrupt(connection, entity, definition, expected_revision):
            if entity == 'widgets' and definition.id == 'main-map':
                raise RuntimeError('Interrupted upgrade')
            return original(connection, entity, definition, expected_revision)
        with patch.object(self.store, '_save_record', side_effect=interrupt), self.assertRaises(RuntimeError):
            bootstrap(self.store)
        self.assertEqual({item.id: item.model_dump() for item in self.store.list('widgets')}, before)
        self.assertEqual(self.store.revision, revision)
        self.assertIsNone(self.store.find_by_key('metrics', 'target_results'))
        with self.store._connection() as connection:
            self.assertIsNone(connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=2').fetchone())
        bootstrap(self.store)
        self.assertEqual(len(self.store.list('widgets')), 25)


    def test_old_builtin_metric_texts_are_replaced_once_and_edits_are_kept(self):
        from app.bi.bootstrap import bootstrap_metric_texts
        from app.bi.metric_texts import LEGACY_METRIC_TEXTS, METRIC_TEXTS
        # An existing installation still carrying the texts shipped before.
        for metric in seed_default_metrics_with_legacy_texts():
            self.store.save("metrics", metric)
        edited = self.store.find_by_key("metrics", "flights")
        self.store.save("metrics", edited.model_copy(update={"title": "Мої вильоти"}), expected_revision=edited.revision)
        bootstrap_metric_texts(self.store)
        efficiency = self.store.find_by_key("metrics", "efficiency")
        self.assertEqual((efficiency.title, efficiency.description), METRIC_TEXTS["efficiency"])
        # Built-ins whose stable id differs from the key are updated too.
        self.assertEqual(self.store.find_by_key("metrics", "lost_devices").title, METRIC_TEXTS["lost_devices"][0])
        self.assertEqual(self.store.find_by_key("metrics", "weighted_result").description, METRIC_TEXTS["weighted_result"][1])
        flights = self.store.find_by_key("metrics", "flights")
        self.assertEqual(flights.title, "Мої вильоти")  # administrator edit kept
        self.assertEqual(flights.description, METRIC_TEXTS["flights"][1])  # untouched empty description filled
        self.assertEqual(efficiency.numerator, "effective")  # calculation unchanged
        revision = efficiency.revision
        bootstrap_metric_texts(self.store)
        self.assertEqual(self.store.find_by_key("metrics", "efficiency").revision, revision)
        # Metrics added after the old texts have no legacy entry.
        self.assertLessEqual(set(LEGACY_METRIC_TEXTS), set(METRIC_TEXTS))


def seed_default_metrics_with_legacy_texts():
    from app.bi.engine import default_metric_definitions
    from app.bi.metric_texts import LEGACY_METRIC_TEXTS
    return [metric.model_copy(update=dict(zip(("title", "description"), LEGACY_METRIC_TEXTS[metric.key]))) if metric.key in LEGACY_METRIC_TEXTS else metric for metric in default_metric_definitions()]

if __name__ == "__main__":
    unittest.main()


class OverviewWidgetsTests(unittest.TestCase):
    def test_overview_widgets_are_added_once_and_deleted_ones_stay_deleted(self):
        import tempfile, os
        from app.bi.store import BiStore
        from app.bi.bootstrap import bootstrap
        with tempfile.TemporaryDirectory() as directory:
            store = BiStore(os.path.join(directory, "bi.sqlite3"))
            bootstrap(store)
            widgets = {item.id: item for item in store.get("dashboards", "system-statistics").widgets}
            for identifier in ("stats-overview-flights-direction", "stats-overview-flights-zone", "stats-overview-results-direction", "stats-overview-results-zone"):
                self.assertEqual(widgets[identifier].section, "overview")
            self.assertEqual([item.key for item in widgets["stats-overview-results-zone"].query.metrics], ["affected", "destroyed", "target_results", "target_detected"])
            self.assertEqual(widgets["stats-overview-flights-zone"].query.metrics[0].key, "zone_flights")
            self.assertIsNotNone(store.find_by_key("metrics", "target_detected"))
            store.delete("widgets", "stats-overview-flights-zone", widgets["stats-overview-flights-zone"].revision)
            bootstrap(store)
            self.assertNotIn("stats-overview-flights-zone", {item.id for item in store.get("dashboards", "system-statistics").widgets})


class AmmunitionSpendingTests(unittest.TestCase):
    def install(self, directory, **changes):
        import os
        store = BiStore(os.path.join(directory, "bi.sqlite3"))
        bootstrap(store)
        metric = store.find_by_key("metrics", "ammunition")
        # The state before version 7: every record of the table, old texts.
        old = metric.model_copy(update={"filters": [], "title": "Сума за таблицею БК", "description": "Сума поля «кількість» по всіх записах таблиці боєприпасів. Увага: таблиця містить не лише боєприпаси, а й записи «відстань», «вага», «виявлено», тому сума не відповідає реальній витраті БК.", **changes})
        store.save("metrics", old, metric.revision)
        with store._connection() as connection:
            connection.execute("DELETE FROM bi_bootstrap_versions WHERE version=7")
        bootstrap(store)
        return store.find_by_key("metrics", "ammunition")

    def test_stock_calculation_is_limited_to_strike_and_mining_once(self):
        with tempfile.TemporaryDirectory() as directory:
            metric = self.install(directory)
            self.assertEqual([item.field for item in metric.filters], ["main_purpose", "norm_bc_name"])
            self.assertIn("Мінування", metric.filters[0].value)
            self.assertEqual(metric.title, "Витрати боєприпасів")

    def test_edited_calculation_and_texts_are_kept(self):
        edited = [QueryFilter(field="norm_bc_name", operator="neq", value="вага")]
        with tempfile.TemporaryDirectory() as directory:
            metric = self.install(directory, filters=edited, title="Мій БК")
            self.assertEqual(metric.filters, edited)
            self.assertEqual(metric.title, "Мій БК")


class PositionsTests(unittest.TestCase):
    def test_positions_section_is_added_once_and_timeline_opens_it(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            store = BiStore(os.path.join(directory, "bi.sqlite3"))
            bootstrap(store)
            widgets = {item.id: item for item in store.get("dashboards", "system-statistics").widgets}
            table = widgets["stats-positions-table"]
            self.assertEqual(table.section, "positions")
            self.assertEqual([item.field for item in table.query.dimensions], ["category", "bbak_title", "position", "team"])
            self.assertEqual([item.key for item in table.query.metrics], ["flights", "day_flights", "night_flights", "effective", "efficiency"])
            self.assertEqual(table.visualization.type, "position_board")
            self.assertIsNotNone(store.find_by_key("metrics", "night_flights"))
            self.assertEqual(store.get("widgets", "main-timeline").interaction.target_section, "positions")
            # A deleted widget is not restored.
            store.delete("widgets", table.id, table.revision)
            bootstrap(store)
            self.assertNotIn("stats-positions-table", {item.id for item in store.get("dashboards", "system-statistics").widgets})

    def test_v8_positions_table_becomes_the_board_unless_edited(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            store = BiStore(os.path.join(directory, "bi.sqlite3"))
            bootstrap(store)

            def reset(metrics):
                table = store.get("widgets", "stats-positions-table")
                query = table.query.model_copy(update={"metrics": [type(table.query.metrics[0])(key=key) for key in metrics]})
                store.save("widgets", table.model_copy(update={"query": query, "layout": table.layout.model_copy(update={"h": 14}), "visualization": table.visualization.model_copy(update={"type": "table"})}), table.revision)
                with store._connection() as connection:
                    connection.execute("DELETE FROM bi_bootstrap_versions WHERE version=9")
                bootstrap(store)
                return store.get("widgets", "stats-positions-table")

            board = reset(["flights", "day_flights", "night_flights", "efficiency"])
            self.assertEqual(board.visualization.type, "position_board")
            self.assertEqual([item.key for item in board.query.metrics], ["flights", "day_flights", "night_flights", "effective", "efficiency"])
            self.assertEqual(board.layout.h, 16)
            edited = reset(["flights", "night_flights"])
            self.assertEqual(edited.visualization.type, "table")

    def test_edited_timeline_click_is_kept(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            store = BiStore(os.path.join(directory, "bi.sqlite3"))
            bootstrap(store)
            timeline = store.get("widgets", "main-timeline")
            store.save("widgets", timeline.model_copy(update={"interaction": timeline.interaction.model_copy(update={"target_section": "targets"})}), timeline.revision)
            with store._connection() as connection:
                connection.execute("DELETE FROM bi_bootstrap_versions WHERE version=8")
            bootstrap(store)
            self.assertEqual(store.get("widgets", "main-timeline").interaction.target_section, "targets")
