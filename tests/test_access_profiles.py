import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import access_routes, bi_routes
from app.bi import access_profiles
from app.bi.models import DataScope
from app.bi.security import Principal, principal_for_user
from app.bi.store import BiStore
from app.core.user_accounts import CreateUserRequest, UserAccountStore

HIERARCHY = {
    "departments": [{"id": "1", "title": "1 ББАК"}, {"id": "2", "title": "2 ББАК"}],
    "groups": [{"id": "1", "title": "1 РБАК", "department_id": "1"}, {"id": "5", "title": "1 РБАК", "department_id": "2"}],
}
PASSWORD = "private-test-password-42"


class ProfileTests(unittest.TestCase):
    def test_profiles_expand_to_permissions_and_scope(self):
        company = access_profiles.permissions_for("company", False)
        self.assertIn("comparison.view_difference", company)
        self.assertNotIn("comparison.view_peer_raw", company)  # never the other unit's own values
        self.assertIn("ranking.view", company)
        self.assertIn("personal_dashboard.widget.create", company)
        self.assertNotIn("map.coordinates", company)
        self.assertIn("map.coordinates", access_profiles.permissions_for("battalion", True))
        command = access_profiles.permissions_for("command", False)
        self.assertIn("comparison.view_peer_raw", command)
        self.assertFalse({"dashboard.manage", "metric.manage", "analytics.admin", "kpi.manage"} & command)
        self.assertEqual(access_profiles.scope_for("company", "1", "5").model_dump()["scope_ids"], ["5"])
        self.assertEqual(access_profiles.scope_for("battalion", "2", None).scope_type, "DEPARTMENT")
        self.assertEqual(access_profiles.scope_for("command", None, None).scope_type, "ALL")

    def test_company_must_belong_to_the_battalion(self):
        self.assertEqual(access_profiles.validate("company", "2", "5", HIERARCHY), ("2", "5"))
        with self.assertRaises(access_profiles.AccessError):
            access_profiles.validate("company", "1", "5", HIERARCHY)  # same title, other battalion
        with self.assertRaises(access_profiles.AccessError):
            access_profiles.validate("battalion", "9", None, HIERARCHY)
        self.assertEqual(access_profiles.validate("command", "1", "1", HIERARCHY), (None, None))


class AccessApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.accounts = UserAccountStore(root / "users.sqlite3")
        self.store = BiStore(root / "bi.sqlite3")
        self.admin = self.accounts.create_user(CreateUserRequest(username="admin", display_name="Адмін", password=PASSWORD, role="administrator"))
        self.viewer = self.accounts.create_user(CreateUserRequest(username="ivan", display_name="Іван", password=PASSWORD, role="viewer"))
        self.user = Principal(self.admin.id, "administrator", frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")
        app = FastAPI()
        app.include_router(access_routes.router, prefix="/api")
        app.dependency_overrides[bi_routes.principal] = lambda: self.user
        self.client = TestClient(app, headers={"Origin": "http://testserver"})
        self.addCleanup(self.client.close)
        for target, name in ((access_routes, "get_bi_store"), (access_routes, "get_user_account_store"), (access_routes, "_hierarchy")):
            value = {"get_bi_store": self.store, "get_user_account_store": self.accounts, "_hierarchy": None}[name]
            replacement = patch.object(target, name, return_value=value) if name != "_hierarchy" else patch.object(target, name, return_value=HIERARCHY)
            replacement.start()
            self.addCleanup(replacement.stop)
        for name in ("app.core.user_accounts.get_user_account_store", "app.bi.security.get_user_account_store", "app.bi.security.get_bi_store"):
            replacement = patch(name, return_value=self.accounts if "user" in name else self.store)
            replacement.start()
            self.addCleanup(replacement.stop)

    def row(self, user_id):
        return next(item for item in self.client.get("/api/v1/admin/access").json()["users"] if item["id"] == user_id)

    def test_new_users_wait_for_access_and_a_profile_decides_everything(self):
        self.assertEqual(self.row(self.viewer.id)["status"], "pending")
        self.assertEqual(principal_for_user(self.viewer, self.store).scope.scope_type, "NONE")
        saved = self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "company", "battalion_id": "2", "company_id": "5"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["status"], saved.json()["battalion"], saved.json()["company"]), ("assigned", "2 ББАК", "1 РБАК"))
        self.assertIn("роти 1 РБАК (2 ББАК)", saved.json()["summary"])
        principal = principal_for_user(self.viewer, self.store)
        self.assertEqual((principal.scope.scope_type, principal.scope.scope_ids, principal.precision), ("GROUP", ["5"], "EXACT"))
        self.assertTrue(principal.has("ranking.view"))
        self.assertFalse(principal.has("comparison.view_peer_raw"))
        stale = self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "battalion", "battalion_id": "1", "expected_revision": 0})
        self.assertEqual(stale.status_code, 409)
        moved = self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "battalion", "battalion_id": "1", "coordinates": True, "expected_revision": saved.json()["revision"]})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertTrue(principal_for_user(self.viewer, self.store).has("map.coordinates"))
        audit = self.client.get(f"/api/v1/admin/access/{self.viewer.id}/audit").json()
        self.assertEqual(len(audit), 2)
        self.assertEqual((audit[0]["before"]["profile"], audit[0]["after"]["profile"], audit[0]["actor"]), ("company", "battalion", "Адмін"))

    def test_invalid_units_and_non_administrators_are_rejected(self):
        self.assertEqual(self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "company", "battalion_id": "1", "company_id": "5"}).status_code, 422)
        self.assertEqual(self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "battalion"}).status_code, 422)
        self.user = Principal(self.viewer.id, "viewer", frozenset({"dashboard.view"}), DataScope(scope_type="ALL"))
        self.assertEqual(self.client.get("/api/v1/admin/access").status_code, 403)
        self.assertEqual(self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "command"}).status_code, 403)

    def test_admin_profile_is_the_account_role_and_the_last_administrator_stays(self):
        promoted = self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "admin"})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual({item.id: item.role for item in self.accounts.users()}[self.viewer.id], "administrator")
        demoted = self.client.put(f"/api/v1/admin/access/{self.admin.id}", json={"profile": "command"})
        self.assertEqual(demoted.status_code, 200, demoted.text)
        last = self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "command", "expected_revision": promoted.json()["revision"]})
        self.assertEqual(last.status_code, 409)
        self.assertEqual({item.id: item.role for item in self.accounts.users()}[self.viewer.id], "administrator")

    def test_bulk_assignment_reports_each_user(self):
        result = self.client.put("/api/v1/admin/access", json={"user_ids": [self.viewer.id, "missing"], "profile": "battalion", "battalion_id": "2"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([item["id"] for item in result.json()["users"]], [self.viewer.id])
        self.assertEqual(result.json()["errors"][0]["user_id"], "missing")


if __name__ == "__main__":
    unittest.main()


class ViewAsTests(AccessApiTests):
    def request(self, method="GET", path="/api/v1/analytics/pages/dashboard/query", target=None):
        from starlette.requests import Request
        headers = [(b"x-analytics-view-as", target.encode())] if target else []
        return Request({"type": "http", "method": method, "path": path, "headers": headers, "query_string": b""})

    def resolve(self, signed, **kwargs):
        from app.bi import security
        with patch.object(security, "require_signed_user", return_value=signed), patch("app.core.keycloak.enabled", return_value=True):
            return security.resolve_principal(self.request(**kwargs), self.store)

    def test_administrator_sees_another_user_read_only(self):
        from fastapi import HTTPException
        self.client.put(f"/api/v1/admin/access/{self.viewer.id}", json={"profile": "battalion", "battalion_id": "2"})
        seen = self.resolve(self.admin, method="POST", target=self.viewer.id)
        self.assertEqual((seen.user_id, seen.scope.scope_ids), (self.viewer.id, ["2"]))
        for method, path in (("PUT", "/api/v1/personal-dashboard"), ("POST", "/api/v1/personal-dashboard/widgets"), ("DELETE", "/api/v1/personal-dashboard/widgets/x")):
            with self.assertRaises(HTTPException) as denied:
                self.resolve(self.admin, method=method, path=path, target=self.viewer.id)
            self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as denied:
            self.resolve(self.viewer, target=self.admin.id)
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as missing:
            self.resolve(self.admin, target="nobody")
        self.assertEqual(missing.exception.status_code, 404)


class LegacySummaryTests(unittest.TestCase):
    def test_old_style_access_is_described_as_still_working(self):
        self.assertIn("усі дані", access_profiles.legacy_summary("ALL", 3))
        self.assertIn("даних не бачить", access_profiles.legacy_summary("NONE", 0))
