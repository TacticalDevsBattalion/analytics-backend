"""Authorization and persistence tests using only isolated temporary BI databases."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from app.bi.models import (
    DashboardAssignment, DashboardDefinition, DataScope, MetricDefinition,
    QueryFilter, RoleDefinition, UserAccessDefinition, WidgetDefinition,
)
from app.bi.security import (
    Principal, apply_user_data_scope, dashboard_visible, principal_for_user,
    require_permission, visible_dashboard, widget_visible,
)
from app.bi.store import BiConflict, BiNotFound, BiStore


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"bbak_id": 1, "rota_id": 11, "crew": "crew-a", "owner_id": "u1", "category": "a"},
            {"bbak_id": 1, "rota_id": 12, "crew": "crew-b", "owner_id": "u2", "category": "b"},
            {"bbak_id": 2, "rota_id": 21, "crew": "crew-c", "owner_id": "u1", "category": "a"},
        ]

    def principal(self, scope, **changes):
        if scope.get("scope_type") == "TEAM" and not scope.get("filters"):
            scope = {**scope, "filters": [{"field": "department", "operator": "eq", "value": 1}, {"field": "group", "operator": "eq", "value": 11}]}
        return Principal("u1", "viewer", frozenset({"statistics.view"}), DataScope.model_validate(scope), **changes)

    def test_hierarchy_scope_filters_rows_and_never_uses_client_selected_area(self):
        for scope_type, ids, expected in (("DEPARTMENT", [1], 2), ("GROUP", [11], 1), ("TEAM", ["crew-a"], 1), ("SELF", [], 2), ("NONE", [], 0), ("ALL", [], 3)):
            principal = self.principal({"scope_type": scope_type, "scope_ids": ids})
            self.assertEqual(len(apply_user_data_scope(self.rows, principal)), expected)
        # Requesting another department cannot change the authenticated principal.
        principal = self.principal({"scope_type": "DEPARTMENT", "scope_ids": [1]})
        requested_rows = [row for row in self.rows if row["bbak_id"] == 2]
        self.assertEqual(apply_user_data_scope(requested_rows, principal), [])

    def test_custom_scope_combines_every_filter_and_missing_fields_fail_closed(self):
        scope = {"scope_type": "CUSTOM", "filters": [{"field": "department", "operator": "eq", "value": 1}, {"field": "category", "operator": "in", "value": ["a"]}]}
        self.assertEqual(apply_user_data_scope(self.rows, self.principal(scope)), [self.rows[0]])
        for scope_type, ids in (("DEPARTMENT", [1]), ("GROUP", [11]), ("TEAM", ["crew-a"]), ("SELF", [])):
            self.assertEqual(apply_user_data_scope([{}], self.principal({"scope_type": scope_type, "scope_ids": ids})), [])
        null_scope = {"scope_type": "CUSTOM", "filters": [{"field": "category", "operator": "is_null"}]}
        self.assertEqual(apply_user_data_scope([{}], self.principal(null_scope)), [])

    def test_feature_permission_and_data_scope_are_independent(self):
        principal = self.principal({"scope_type": "NONE"})
        require_permission(principal, "statistics.view")
        self.assertEqual(apply_user_data_scope(self.rows, principal), [])
        with self.assertRaises(HTTPException):
            require_permission(principal, "metric.manage")
        other = Principal("u1", "viewer", frozenset(), DataScope(scope_type="ALL"))
        with self.assertRaises(HTTPException):
            require_permission(other, "statistics.view")

    def test_analytics_administration_implies_features_but_keeps_data_assignment(self):
        principal = Principal("u1", "viewer", frozenset({"analytics.admin"}), DataScope(scope_type="GROUP", scope_ids=[11]))
        self.assertTrue(principal.has("metric.manage"))
        self.assertTrue(principal.has("metric.view"))
        self.assertTrue(principal.has("comparison.view_peer_raw"))
        self.assertFalse(principal.has("analytics.global"))
        self.assertEqual(apply_user_data_scope(self.rows, principal), [self.rows[0]])
        self.assertIn("metric.manage", principal.effective_permissions)

    def test_explicit_denies_override_roles_and_administration_implication(self):
        principal = Principal("u1", "viewer", frozenset({"analytics.admin", "dashboard.view"}), DataScope(scope_type="ALL"), denied_permissions=frozenset({"widget.create", "dashboard.view"}))
        self.assertTrue(principal.has("metric.manage"))
        self.assertFalse(principal.has("widget.create"))
        self.assertFalse(principal.has("dashboard.view"))
        self.assertNotIn("widget.create", principal.effective_permissions)
        self.assertNotIn("dashboard.view", principal.effective_permissions)
        other = Principal("u1", "viewer", principal.permissions, principal.scope)
        self.assertNotEqual(principal.cache_key(), other.cache_key())

    def test_scope_is_part_of_cache_identity(self):
        a = self.principal({"scope_type": "GROUP", "scope_ids": [11]})
        b = self.principal({"scope_type": "GROUP", "scope_ids": [12]})
        self.assertNotEqual(a.cache_key(), b.cache_key())

    def test_team_name_repeated_in_other_department_is_constrained_by_context(self):
        rows = [self.rows[0], {"bbak_id": 2, "rota_id": 21, "crew": "crew-a"}]
        principal = self.principal({"scope_type": "TEAM", "scope_ids": ["crew-a"], "filters": [{"field": "department", "operator": "eq", "value": 1}, {"field": "group", "operator": "eq", "value": 11}]})
        self.assertEqual(apply_user_data_scope(rows, principal), [rows[0]])

    def test_extra_query_or_scope_code_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            DataScope(scope_type="ALL", sql="select *")
        with self.assertRaises(ValidationError):
            QueryFilter(field="department", operator="in", value="1 OR 1=1")
        with self.assertRaises(ValidationError):
            DataScope(scope_type="TEAM", scope_ids=["ambiguous-crew-name"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bi.sqlite3"
        self.store = BiStore(self.path)

    def metric(self):
        return MetricDefinition(id="metric-total", key="total_operations", title="Operations", source="flights", aggregation="COUNT_DISTINCT", field="flight_id")

    def dashboard(self):
        return self.store.save("dashboards", DashboardDefinition(id="dashboard-one", name="Dashboard", slug="dashboard", layout_mode="LAYOUT_EDITABLE", widgets=[WidgetDefinition(id="widget-one", title="Flights")]))

    def test_migrations_are_idempotent_and_entities_persist_separately(self):
        saved = self.dashboard()
        self.assertEqual(saved.version, 2)
        restarted = BiStore(self.path)
        self.assertEqual(restarted.get("dashboards", saved.id).widgets[0].id, "widget-one")
        connection = sqlite3.connect(self.path)
        try:
            versions = {row[0] for row in connection.execute("SELECT version FROM bi_schema_migrations")}
            self.assertTrue({1, 2, 3}.issubset(versions))
            raw = connection.execute("SELECT definition_json FROM dashboards").fetchone()[0]
            self.assertIn('"widgets":[]', raw)
            self.assertEqual(connection.execute("SELECT count(*) FROM dashboard_widgets").fetchone()[0], 1)
        finally:
            connection.close()

    def test_optimistic_update_delete_and_global_revision(self):
        initial = self.store.revision
        saved = self.store.save("metrics", self.metric())
        self.assertGreater(self.store.revision, initial)
        with self.assertRaises(BiConflict):
            self.store.save("metrics", self.metric())
        changed = self.store.save("metrics", saved.model_copy(update={"title": "Updated"}))
        self.assertEqual(changed.revision, 2)
        with self.assertRaises(BiConflict):
            self.store.delete("metrics", changed.id, 1)
        self.store.delete("metrics", changed.id, 2)
        with self.assertRaises(BiNotFound):
            self.store.get("metrics", changed.id)

    def test_assignments_and_role_permissions_resolve_independently(self):
        user = SimpleNamespace(id="u1", role="viewer")
        unassigned = principal_for_user(user, self.store)
        self.assertEqual(unassigned.scope.scope_type, "NONE")
        self.assertTrue(unassigned.has("statistics.view"))
        self.assertFalse(unassigned.has("analytics.global"))
        role = self.store.save("roles", RoleDefinition(key="manager", title="Manager", permissions=["statistics.view", "comparison.view_peer_raw"]))
        access = self.store.save("user_access", UserAccessDefinition(user_id="u1", role_ids=[role.id], permissions=["dashboard.view"], denied_permissions=["comparison.view_peer_raw"], data_scope=DataScope(scope_type="GROUP", scope_ids=[11])))
        principal = principal_for_user(user, self.store)
        self.assertTrue(principal.has("dashboard.view"))
        self.assertTrue(principal.has("statistics.view"))
        self.assertFalse(principal.has("comparison.view_peer_raw"))
        self.assertEqual(principal.scope.scope_ids, ["11"])
        self.assertEqual(principal.access_revision, access.revision)

    def test_dashboard_widget_access_and_personal_overrides(self):
        dashboard = self.dashboard()
        principal = Principal("u1", "viewer", frozenset({"dashboard.view"}), DataScope(scope_type="TEAM", scope_ids=["crew-a"], filters=[QueryFilter(field="department", operator="eq", value=1), QueryFilter(field="group", operator="eq", value=11)]))
        self.assertTrue(dashboard_visible(dashboard, principal))
        hidden = dashboard.model_copy(update={"assignments": [DashboardAssignment(assignment_type="user", assignment_id="u2")]})
        self.assertFalse(dashboard_visible(hidden, principal))
        self.assertFalse(widget_visible(WidgetDefinition(title="Global", data_scope="GLOBAL"), principal))
        self.store.save_overrides(dashboard.id, "u1", [{"widget_id": "widget-one", "layout": {"x": 1, "y": 2, "w": 5, "h": 4}}], 0)
        effective = visible_dashboard(self.store.get("dashboards", dashboard.id), principal, self.store)
        self.assertEqual(effective.widgets[0].layout.x, 1)
        self.assertEqual(self.store.get("dashboards", dashboard.id).widgets[0].layout.x, 0)

    def test_override_batch_is_atomic_revisioned_and_resettable(self):
        dashboard = self.dashboard()
        layout = {"x": 1, "y": 1, "w": 5, "h": 4}
        self.assertEqual(self.store.overrides_snapshot(dashboard.id, "u1")["revision"], 0)
        with self.assertRaises(BiNotFound):
            self.store.save_overrides(dashboard.id, "u1", [{"widget_id": "widget-one", "layout": layout}, {"widget_id": "missing", "layout": layout}], 0)
        self.assertEqual(self.store.overrides_snapshot(dashboard.id, "u1")["revision"], 0)
        saved = self.store.save_overrides(dashboard.id, "u1", [{"widget_id": "widget-one", "layout": layout}], 0)
        self.assertEqual(saved["revision"], 1)
        with self.assertRaises(BiConflict):
            self.store.save_overrides(dashboard.id, "u1", [], 0)
        reset = self.store.save_overrides(dashboard.id, "u1", [], 1)
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(reset["overrides"], [])

    def test_new_system_widget_survives_existing_layout_override(self):
        dashboard = self.dashboard()
        self.store.save_overrides(dashboard.id, "u1", [{"widget_id": "widget-one", "layout": {"x": 0, "y": 1, "w": 6, "h": 4}}], 0)
        dashboard.widgets.append(WidgetDefinition(id="widget-two", title="New metric"))
        saved = self.store.save("dashboards", dashboard)
        self.assertEqual({item.id for item in saved.widgets}, {"widget-one", "widget-two"})
        self.assertEqual(len(self.store.overrides_snapshot(dashboard.id, "u1")["overrides"]), 1)

    def test_personal_widgets_are_owned_and_system_updates_never_copy_or_delete_them(self):
        dashboard = self.dashboard()
        dashboard.layout_mode = "CUSTOMIZABLE"
        dashboard = self.store.save("dashboards", dashboard)
        owned = self.store.save_personal_widget(dashboard.id, "u1", WidgetDefinition(id="personal-one", title="My metric"))
        self.assertEqual(owned.owner_id, "u1")
        self.assertEqual(self.store.personal_widgets(dashboard.id, "u2"), [])
        viewer = Principal("u1", "viewer", frozenset({"dashboard.view"}), DataScope(scope_type="ALL"))
        self.assertEqual({widget.id for widget in visible_dashboard(dashboard, viewer, self.store).widgets}, {"widget-one", "personal-one"})
        other = Principal("u2", "administrator", frozenset({"*"}), DataScope(scope_type="ALL"))
        self.assertEqual({widget.id for widget in visible_dashboard(dashboard, other, self.store).widgets}, {"widget-one"})
        dashboard.widgets = []
        dashboard = self.store.save("dashboards", dashboard)
        self.assertEqual(self.store.get("dashboards", dashboard.id).widgets, [])
        self.assertEqual(len(self.store.personal_widgets(dashboard.id, "u1")), 1)
        with self.assertRaises(BiNotFound):
            self.store.save_personal_widget(dashboard.id, "u2", owned)
        with self.assertRaises(BiNotFound):
            self.store.delete_personal_widget(dashboard.id, "u2", owned.id, owned.revision)

    def test_personal_widget_mode_scope_revision_and_override_security(self):
        dashboard = self.dashboard()
        with self.assertRaises(BiConflict):
            self.store.save_personal_widget(dashboard.id, "u1", WidgetDefinition(id="p", title="My metric"))
        dashboard.layout_mode = "FREE"
        dashboard = self.store.save("dashboards", dashboard)
        with self.assertRaises(BiConflict):
            self.store.save_personal_widget(dashboard.id, "u1", WidgetDefinition(id="p", title="Global", data_scope="GLOBAL"))
        owned = self.store.save_personal_widget(dashboard.id, "u1", WidgetDefinition(id="p", title="Mine"))
        with self.assertRaises(BiConflict):
            self.store.save_personal_widget(dashboard.id, "u1", owned.model_copy(update={"revision": 0}))
        self.store.save_overrides(dashboard.id, "u1", [{"widget_id": owned.id, "layout": {"x": 0, "y": 2, "w": 6, "h": 4}}], 0)
        self.assertEqual(self.store.overrides_snapshot(dashboard.id, "u1")["overrides"][0].widget_id, "p")
        with self.assertRaises(BiNotFound):
            self.store.save_overrides(dashboard.id, "u2", [{"widget_id": owned.id, "layout": {"x": 0, "y": 2, "w": 6, "h": 4}}], 0)
        self.store.delete_personal_widget(dashboard.id, "u1", owned.id, 1)
        self.assertEqual(self.store.overrides_snapshot(dashboard.id, "u1")["overrides"], [])

    def test_system_and_personal_widget_ids_cannot_collide(self):
        dashboard = self.dashboard()
        dashboard.layout_mode = "FREE"
        dashboard = self.store.save("dashboards", dashboard)
        with self.assertRaises(BiConflict):
            self.store.save_personal_widget(dashboard.id, "u1", WidgetDefinition(id="widget-one", title="Collision"))
        self.store.save_personal_widget(dashboard.id, "u1", WidgetDefinition(id="personal-one", title="My metric"))
        dashboard.widgets.append(WidgetDefinition(id="personal-one", title="Collision"))
        with self.assertRaises(BiConflict):
            self.store.save("dashboards", dashboard)

    def test_parent_assignments_apply_to_all_and_team_scope(self):
        dashboard = self.dashboard()
        dashboard.assignments = [DashboardAssignment(assignment_type="department", assignment_id="1")]
        all_scope = Principal("u1", "viewer", frozenset({"dashboard.view"}), DataScope(scope_type="ALL"))
        self.assertTrue(dashboard_visible(dashboard, all_scope))
        team = Principal("u1", "viewer", frozenset({"dashboard.view"}), DataScope(scope_type="TEAM", scope_ids=["crew-a"], filters=[QueryFilter(field="department", operator="eq", value=1), QueryFilter(field="group", operator="eq", value=11)]))
        self.assertTrue(dashboard_visible(dashboard, team))
        dashboard.assignments = [DashboardAssignment(assignment_type="group", assignment_id="11")]
        self.assertTrue(dashboard_visible(dashboard, team))
        dashboard.assignments = [DashboardAssignment(assignment_type="department", assignment_id="2")]
        self.assertFalse(dashboard_visible(dashboard, team))


if __name__ == "__main__":
    unittest.main()
