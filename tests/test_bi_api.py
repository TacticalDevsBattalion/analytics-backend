"""Integration tests of the BI HTTP boundary and isolated permission contexts."""
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import bi_routes
from app.bi.engine import QueryEngine
from app.bi.models import DashboardDefinition, DataScope, MetricDefinition, WidgetDefinition
from app.bi.security import PERMISSIONS, Principal
from app.bi.store import BiStore
from app.core.cache import cache
from app.core.user_accounts import CreateUserRequest, UserAccountStore


DATE_RANGE = {"from": "2026-09-01", "to": "2026-09-30"}


class BiApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = BiStore(self.directory / "bi.sqlite3")
        self.accounts = UserAccountStore(self.directory / "users.sqlite3")
        self.accounts.create_user(CreateUserRequest(username="test-admin", display_name="Admin", password="test-password-admin-42", role="administrator"))
        self.viewer = self.accounts.create_user(CreateUserRequest(username="test-viewer", display_name="Viewer", password="test-password-viewer-42", role="viewer"))
        self.principal = Principal(self.viewer.id, "viewer", frozenset({"dashboard.view", "statistics.view", "metric.view", "kpi.view", "comparison.view", "comparison.department", "comparison.group", "comparison.team", "comparison.select_peer", "comparison.view_difference", "comparison.view_breakdown", "dashboard.layout.edit"}), DataScope(scope_type="DEPARTMENT", scope_ids=[1]), "ROUND_5")
        self.store.save("metrics", MetricDefinition(id="flights", key="flights", title="Flights", aggregation="COUNT_DISTINCT", field="flight_id"))
        self.store.save("metrics", MetricDefinition(id="private", key="private_count", title="Confidential count", aggregation="COUNT_DISTINCT", field="flight_id", permissions=["metrics.confidential"]))
        self.store.save("metrics", MetricDefinition(id="composite", key="composite_private", title="Confidential formula", aggregation="FORMULA", formula={"op": "mul", "args": [{"metric": "private_count"}, 2]}))
        self.rows = {"flights": [
            {"flight_id": "f1", "date": "2026-09-05", "bbak_id": 1, "rota_id": 11, "crew": "crew-a", "device_type": "a", "main_purpose": "strike"},
            {"flight_id": "f2", "date": "2026-09-05", "bbak_id": 1, "rota_id": 12, "crew": "crew-b", "device_type": "b", "main_purpose": "strike"},
            {"flight_id": "f3", "date": "2026-09-05", "bbak_id": 2, "rota_id": 21, "crew": "crew-c", "device_type": "a", "main_purpose": "strike"},
            {"flight_id": "f4", "date": "2026-09-05", "bbak_id": 2, "rota_id": 21, "crew": "crew-c", "device_type": "a", "main_purpose": "strike"},
            {"flight_id": "f5", "date": "2026-09-05", "bbak_id": 2, "rota_id": 22, "crew": "crew-d", "device_type": "b", "main_purpose": "strike"},
        ], "events": [], "ammunition": [], "lost_devices": []}
        cache.clear()
        self.addCleanup(cache.clear)
        for replacement in (patch.object(bi_routes, "get_bi_store", return_value=self.store), patch.object(bi_routes, "get_user_account_store", return_value=self.accounts), patch.object(bi_routes, "engine", side_effect=lambda: QueryEngine(self.store, row_loader=lambda start, end: self.rows))):
            replacement.start()
            self.addCleanup(replacement.stop)
        app = FastAPI()
        app.include_router(bi_routes.router, prefix="/api")
        app.dependency_overrides[bi_routes.principal] = lambda: self.principal
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def body(self, **updates):
        return {"metrics": [{"key": "flights"}], "date_range": DATE_RANGE, **updates}

    def as_admin(self):
        self.principal = Principal("admin", "administrator", frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")

    def make_dashboard(self, **updates):
        return self.store.save("dashboards", DashboardDefinition(id="dashboard", name="Dashboard", slug="dashboard", layout_mode=updates.pop("layout_mode", "LAYOUT_EDITABLE"), widgets=updates.pop("widgets", [WidgetDefinition(id="widget", title="Flights", query=self.body())]), **updates))

    def test_server_scope_survives_http_filter_and_fixed_scope_tampering(self):
        scoped = self.client.post("/api/v1/analytics/query", json=self.body())
        self.assertEqual(scoped.status_code, 200, scoped.text)
        self.assertEqual(scoped.json()["rows"][0]["flights"], 2)
        denied = self.client.post("/api/v1/analytics/query", json=self.body(filters=[{"field": "department", "operator": "eq", "value": 2}]))
        self.assertEqual(denied.status_code, 200, denied.text)
        self.assertEqual(denied.json()["status"], "empty")
        self.assertIsNone(denied.json()["rows"][0]["flights"])
        fixed = self.client.post("/api/v1/analytics/query", json=self.body(data_scope="FIXED_SCOPE", fixed_scope={"scope_type": "ALL"}))
        self.assertEqual(fixed.status_code, 200, fixed.text)
        self.assertEqual(fixed.json()["rows"][0]["flights"], 2)
        global_ = self.client.post("/api/v1/analytics/query", json=self.body(data_scope="GLOBAL"))
        self.assertEqual(global_.status_code, 403)
        self.assertEqual(self.client.post("/api/v1/analytics/query", json=self.body(sql="select * from users")).status_code, 422)

    def test_no_scope_means_no_data_and_cache_cannot_cross_assignments(self):
        self.client.post("/api/v1/analytics/query", json=self.body())
        self.principal = Principal(self.viewer.id, "viewer", frozenset({"metric.view"}), DataScope(scope_type="DEPARTMENT", scope_ids=[2]))
        peer = self.client.post("/api/v1/analytics/query", json=self.body())
        self.assertEqual(peer.json()["rows"][0]["flights"], 3)
        self.principal = Principal(self.viewer.id, "viewer", frozenset({"metric.view"}), DataScope(scope_type="NONE"))
        none = self.client.post("/api/v1/analytics/query", json=self.body())
        self.assertEqual(none.json()["status"], "empty")
        self.assertIsNone(none.json()["rows"][0]["flights"])

    def test_metadata_and_formula_dependencies_cannot_reveal_protected_metrics(self):
        metadata = self.client.get("/api/v1/analytics/metadata")
        self.assertEqual(metadata.status_code, 200, metadata.text)
        keys = {item["key"] for item in metadata.json()["metrics"]}
        self.assertIn("flights", keys)
        self.assertNotIn("private_count", keys)
        self.assertNotIn("composite_private", keys)
        for key in ("private_count", "composite_private"):
            response = self.client.post("/api/v1/analytics/query", json=self.body(metrics=[{"key": key}]))
            self.assertEqual(response.status_code, 403, response.text)

    def test_batch_errors_are_isolated_from_other_widget_results(self):
        response = self.client.post("/api/v1/analytics/query/batch", json={"queries": [self.body(), self.body(metrics=[{"key": "private_count"}])]})
        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()["results"]
        self.assertEqual([item["status"] for item in results], ["success", "error"])
        self.assertEqual(results[0]["data"]["rows"][0]["flights"], 2)
        self.assertNotIn("data", results[1])

    def test_comparison_returns_only_difference_until_explicit_raw_permission(self):
        body = {"level": "DEPARTMENT", "own_id": "1", "peer_id": "2", "query": self.body()}
        restricted = self.client.post("/api/v1/analytics/comparison", json=body)
        self.assertEqual(restricted.status_code, 200, restricted.text)
        self.assertFalse(restricted.json()["raw_visible"])
        for row in restricted.json()["rows"]:
            self.assertNotIn("own", row)
            self.assertNotIn("peer", row)
            self.assertIn("difference_percent", row)
        self.as_admin()
        raw = self.client.post("/api/v1/analytics/comparison", json=body)
        self.assertTrue(raw.json()["raw_visible"])
        self.assertEqual((raw.json()["rows"][0]["own"], raw.json()["rows"][0]["peer"]), (2, 3))

    def test_comparison_context_and_own_area_cannot_expand_scope(self):
        invalid_own = self.client.post("/api/v1/analytics/comparison", json={"level": "DEPARTMENT", "own_id": "2", "peer_id": "1", "query": self.body()})
        self.assertEqual(invalid_own.status_code, 200, invalid_own.text)
        self.assertEqual(invalid_own.json()["rows"], [])
        missing_context = self.client.post("/api/v1/analytics/comparison", json={"level": "TEAM", "own_id": "crew-a", "peer_id": "crew-c", "query": self.body()})
        self.assertEqual(missing_context.status_code, 422)
        wrong_context = self.client.post("/api/v1/analytics/comparison", json={"level": "TEAM", "own_id": "crew-a", "peer_id": "crew-c", "context_department_id": "2", "context_group_id": "21", "query": self.body()})
        self.assertEqual(wrong_context.status_code, 200, wrong_context.text)
        self.assertEqual(wrong_context.json()["rows"], [])

    def test_dashboard_query_keeps_fixed_filters_and_widget_failure_is_local(self):
        fixed = WidgetDefinition(id="fixed", title="Fixed", data_scope="FIXED_SCOPE", fixed_filters=[{"field": "category", "operator": "eq", "value": "a"}], query=self.body())
        bad = WidgetDefinition(id="bad", title="Invalid dimension", query=self.body(dimensions=[{"field": "unapproved_field"}]))
        self.make_dashboard(widgets=[fixed, bad])
        response = self.client.post("/api/v1/analytics/dashboards/dashboard/query", json={"date_range": DATE_RANGE})
        self.assertEqual(response.status_code, 200, response.text)
        values = {item["widget_id"]: item for item in response.json()["results"]}
        self.assertEqual(values["fixed"]["status"], "success", values)
        self.assertEqual(values["fixed"]["data"]["rows"][0]["flights"], 1)
        self.assertEqual(values["bad"]["status"], "error", values)

    def test_dashboard_lists_hide_protected_metric_references_and_assignments(self):
        self.make_dashboard(widgets=[WidgetDefinition(id="private-widget", title="Confidential widget", query=self.body(metrics=[{"key": "composite_private"}]))])
        for path in ("/api/v1/analytics/dashboards", "/api/v1/analytics/dashboards/dashboard"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("composite_private", response.text)
            self.assertNotIn("Confidential widget", response.text)

    def test_locked_layout_duplicate_or_unavailable_widget_cannot_be_overridden(self):
        definition = self.make_dashboard(layout_mode="LOCKED")
        body = {"expected_revision": 0, "overrides": [{"widget_id": "widget", "layout": {"x": 0, "y": 1, "w": 6, "h": 4}}]}
        self.assertEqual(self.client.put("/api/v1/analytics/dashboards/dashboard/overrides", json=body).status_code, 403)
        definition.layout_mode = "LAYOUT_EDITABLE"
        self.store.save("dashboards", definition)
        invalid = {"expected_revision": 0, "overrides": [*body["overrides"], {"widget_id": "unknown", "layout": {"x": 0, "y": 2, "w": 6, "h": 4}}]}
        self.assertEqual(self.client.put("/api/v1/analytics/dashboards/dashboard/overrides", json=invalid).status_code, 403)
        self.assertEqual(self.store.overrides_snapshot("dashboard", self.viewer.id)["revision"], 0)
        duplicate = {"expected_revision": 0, "overrides": body["overrides"] * 2}
        self.assertEqual(self.client.put("/api/v1/analytics/dashboards/dashboard/overrides", json=duplicate).status_code, 422)

    def test_override_revision_reads_and_reset_preserve_system_widgets(self):
        self.make_dashboard()
        body = {"expected_revision": 0, "overrides": [{"widget_id": "widget", "layout": {"x": 0, "y": 1, "w": 6, "h": 4}}]}
        saved = self.client.put("/api/v1/analytics/dashboards/dashboard/overrides", json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        read = self.client.get("/api/v1/analytics/dashboards/dashboard/overrides")
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()["revision"], 1)
        self.assertEqual(read.json()["overrides"][0]["widget_id"], "widget")
        self.assertEqual(self.client.put("/api/v1/analytics/dashboards/dashboard/overrides", json=body).status_code, 409)
        reset = self.client.put("/api/v1/analytics/dashboards/dashboard/overrides", json={"expected_revision": 1, "overrides": []})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(self.store.get("dashboards", "dashboard").widgets[0].layout.y, 0)

    def test_admin_users_use_actual_account_store_and_never_echo_hashes(self):
        self.assertEqual(self.client.get("/api/v1/admin/bi/users").status_code, 403)
        self.as_admin()
        response = self.client.get("/api/v1/admin/bi/users")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["users"]), 2)
        self.assertNotIn("password_hash", response.text)
        self.assertEqual(set(response.json()["permissions"]), set(PERMISSIONS))
        access = self.client.post("/api/v1/admin/bi/user_access", json={"user_id": self.viewer.id, "permissions": ["statistics.view"], "data_scope": {"scope_type": "GROUP", "scope_ids": [11]}})
        self.assertEqual(access.status_code, 200, access.text)
        self.assertEqual(self.store.user_access(self.viewer.id).data_scope.scope_ids, ["11"])

    def test_personal_widgets_are_scoped_owned_and_query_without_copying_system(self):
        self.make_dashboard(layout_mode="CUSTOMIZABLE")
        self.principal = replace(self.principal, permissions=self.principal.permissions | {"widget.create", "widget.edit", "widget.delete"})
        body = {"id": "personal", "title": "My flights", "query": self.body()}
        saved = self.client.post("/api/v1/analytics/dashboards/dashboard/widgets", json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        personal = saved.json()
        self.assertEqual(personal["owner_id"], self.viewer.id)
        self.assertEqual(len(self.store.get("dashboards", "dashboard").widgets), 1)
        query = self.client.post("/api/v1/analytics/dashboards/dashboard/query", json={"date_range": DATE_RANGE})
        self.assertEqual(query.status_code, 200, query.text)
        values = {item["widget_id"]: item for item in query.json()["results"]}
        self.assertEqual(values["personal"]["data"]["rows"][0]["flights"], 2)
        overrides = self.client.put("/api/v1/analytics/dashboards/dashboard/overrides", json={"expected_revision": 0, "overrides": [{"widget_id": "personal", "layout": {"x": 0, "y": 5, "w": 6, "h": 4}}]})
        self.assertEqual(overrides.status_code, 200, overrides.text)
        self.principal = replace(self.principal, user_id="other-user")
        other = self.client.get("/api/v1/analytics/dashboards/dashboard")
        self.assertNotIn("My flights", other.text)
        rewrite = self.client.post("/api/v1/analytics/dashboards/dashboard/widgets", json={**personal, "owner_id": "other-user"})
        self.assertEqual(rewrite.status_code, 404, rewrite.text)
        remove = self.client.delete("/api/v1/analytics/dashboards/dashboard/widgets/personal?expected_revision=1")
        self.assertEqual(remove.status_code, 404, remove.text)
        self.principal = replace(self.principal, user_id=self.viewer.id)
        stale = self.client.post("/api/v1/analytics/dashboards/dashboard/widgets", json={**personal, "revision": 0, "title": "Stale"})
        self.assertEqual(stale.status_code, 409, stale.text)
        removed = self.client.delete("/api/v1/analytics/dashboards/dashboard/widgets/personal?expected_revision=1")
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self.store.overrides_snapshot("dashboard", self.viewer.id)["overrides"], [])
        self.assertEqual(len(self.store.get("dashboards", "dashboard").widgets), 1)

    def test_personal_widget_mode_owner_and_global_scope_spoofing_is_rejected(self):
        self.make_dashboard(layout_mode="LOCKED")
        self.principal = replace(self.principal, permissions=self.principal.permissions | {"widget.create", "widget.edit", "widget.delete", "analytics.global"})
        path = "/api/v1/analytics/dashboards/dashboard/widgets"
        body = {"id": "personal", "title": "Mine", "query": self.body()}
        self.assertEqual(self.client.post(path, json=body).status_code, 403)
        definition = self.store.get("dashboards", "dashboard")
        definition.layout_mode = "FREE"
        self.store.save("dashboards", definition)
        for changes in ({"owner_id": "another-user"}, {"data_scope": "GLOBAL"}, {"query": self.body(data_scope="GLOBAL")}, {"query": self.body(fixed_scope={"scope_type": "ALL"})}):
            with self.subTest(changes=changes):
                response = self.client.post(path, json={**body, **changes})
                self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.personal_widgets("dashboard", self.viewer.id), [])
        self.as_admin()
        invalid_statistics = self.client.post("/api/v1/admin/bi/dashboards", json={"name": "Statistics", "slug": "statistics", "type": "STATISTICS", "layout_mode": "FREE"})
        self.assertEqual(invalid_statistics.status_code, 422, invalid_statistics.text)

    def test_locked_system_widget_cannot_be_deleted_or_changed_through_dashboard_save(self):
        definition = self.make_dashboard(widgets=[WidgetDefinition(id="locked", title="Locked", query=self.body(), is_locked=True)])
        self.principal = replace(self.principal, permissions=self.principal.permissions | {"widget.delete", "widget.edit", "dashboard.manage"})
        deleted = self.client.delete("/api/v1/admin/bi/widgets/locked?expected_revision=1")
        self.assertEqual(deleted.status_code, 403, deleted.text)
        changed = definition.model_dump(mode="json", by_alias=True)
        changed["widgets"][0]["title"] = "Changed via parent"
        response = self.client.post("/api/v1/admin/bi/dashboards", json=changed)
        self.assertEqual(response.status_code, 403, response.text)
        changed["widgets"] = []
        response = self.client.post("/api/v1/admin/bi/dashboards", json=changed)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.get("widgets", "locked").title, "Locked")

    def test_explicit_admin_feature_denies_are_enforced_by_admin_endpoints(self):
        self.principal = replace(self.principal, permissions=frozenset({"analytics.admin"}), denied_permissions=frozenset({"metric.manage", "widget.create"}))
        metadata = self.client.get("/api/v1/analytics/metadata")
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertNotIn("widget.create", metadata.json()["permissions"])
        self.assertNotIn("metric.manage", metadata.json()["permissions"])
        forbidden = self.client.post("/api/v1/admin/bi/metrics", json={"key": "forbidden_new", "title": "New", "aggregation": "COUNT"})
        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        self.assertIsNone(self.store.find_by_key("metrics", "forbidden_new"))

    def test_disabled_global_filter_inheritance_keeps_only_local_and_fixed_filters(self):
        local = WidgetDefinition(id="local", title="Local", query=self.body(), data_scope="FIXED_SCOPE", fixed_filters=[{"field": "category", "operator": "eq", "value": "a"}], inherit_global_filters=False)
        inherited = WidgetDefinition(id="inherited", title="Inherited", query=self.body())
        self.make_dashboard(widgets=[local, inherited], global_filters=[{"field": "category", "operator": "eq", "value": "b"}])
        response = self.client.post("/api/v1/analytics/dashboards/dashboard/query", json={"date_range": DATE_RANGE, "filters": [{"field": "department", "operator": "eq", "value": 2}]})
        self.assertEqual(response.status_code, 200, response.text)
        values = {item["widget_id"]: item for item in response.json()["results"]}
        self.assertEqual(values["local"]["status"], "success", values)
        self.assertEqual(values["local"]["data"]["rows"][0]["flights"], 1)
        self.assertEqual(values["inherited"]["status"], "empty", values)

    def test_wildcard_admin_metadata_keeps_explicit_denies_and_never_exposes_wildcard(self):
        self.make_dashboard()
        self.principal = replace(self.principal, permissions=frozenset({"*"}), denied_permissions=frozenset({"dashboard.layout.edit"}))
        metadata = self.client.get("/api/v1/analytics/metadata")
        self.assertEqual(metadata.status_code, 200, metadata.text)
        self.assertNotIn("*", metadata.json()["permissions"])
        self.assertNotIn("dashboard.layout.edit", metadata.json()["permissions"])
        self.assertEqual(set(metadata.json()["permissions"]), set(PERMISSIONS) - {"dashboard.layout.edit"})
        response = self.client.put("/api/v1/analytics/dashboards/dashboard/overrides", json={"expected_revision": 0, "overrides": [{"widget_id": "widget", "layout": {"x": 0, "y": 1, "w": 6, "h": 4}}]})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.overrides_snapshot("dashboard", self.viewer.id)["revision"], 0)
        self.as_admin()
        admin = self.client.get("/api/v1/analytics/metadata")
        self.assertEqual(set(admin.json()["permissions"]), set(PERMISSIONS))
        self.assertNotIn("*", admin.json()["permissions"])
        self.assertEqual(self.client.get("/api/v1/admin/bi/not_an_entity").status_code, 404)

    def test_weight_sets_http_crud_preserves_frontend_fields_and_revision_guards(self):
        url = '/api/v1/admin/bi/weight_sets'
        self.assertEqual(self.client.get(url).status_code, 403)
        self.principal = replace(self.principal, permissions=self.principal.permissions | {'metric.manage'})
        existing = self.client.get(url)
        self.assertEqual(existing.status_code, 200, existing.text)
        initial_ids = {item['id'] for item in existing.json()}
        body = {'id': 'frontend-weight-set', 'key': 'frontend_weight_set', 'title': 'Frontend weight set', 'description': 'Saved through the administration editor', 'is_active': True}
        created = self.client.post(url, json=body)
        self.assertEqual(created.status_code, 200, created.text)
        saved = created.json()
        for field, expected in body.items():
            self.assertEqual(saved[field], expected)
        self.assertEqual(saved['revision'], 1)
        self.assertNotIn('aggregation', saved)
        listed = self.client.get(url).json()
        self.assertEqual({item['id'] for item in listed}, initial_ids | {body['id']})
        update = {**saved, 'title': 'Edited frontend title', 'is_active': False}
        modified = self.client.post(url, json=update)
        self.assertEqual(modified.status_code, 200, modified.text)
        self.assertEqual((modified.json()['revision'], modified.json()['is_active']), (2, False))
        self.assertEqual(self.client.post(url, json=update).status_code, 409)
        self.assertEqual(self.client.delete(url + '/' + body['id'], params={'expected_revision': 1}).status_code, 409)
        removed = self.client.delete(url + '/' + body['id'], params={'expected_revision': 2})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual({item['id'] for item in self.client.get(url).json()}, initial_ids)
        self.principal = replace(self.principal, denied_permissions=frozenset({'metric.manage'}))
        self.assertEqual(self.client.post(url, json=body).status_code, 403)
        self.assertIsNone(self.store.find_by_key('weight_sets', body['key']))


if __name__ == "__main__":
    unittest.main()
