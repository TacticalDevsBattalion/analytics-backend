import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.app_configuration import ApplicationConfigurationStore, default_application_configuration
from app.core.kpi_configuration import KpiConfiguration
from app.core.user_accounts import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    AccountConflict,
    AuthenticationFailed,
    CreateUserRequest,
    DashboardUpdate,
    LoginRateLimited,
    PersonalDashboardPreferences,
    UpdateUserRequest,
    UserAccountStore,
    get_user_account_store,
    verify_password,
)

TEST_PASSWORD = "private-test-password-42"


def preferences(**updates):
    value = {"version": 1, "metric_keys": ["flights", "efficiency"], "blocks": ["timeline_line", "category_chart"], "background": "forest", "density": "comfortable", "chart_styles": {"timeline": "line", "category": "donut", "purpose": "bars"}}
    value.update(updates)
    return value


class StoreFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data" / "users.sqlite3"
        self.now = [1_750_000_000.0]
        self.store = UserAccountStore(self.path, lambda: self.now[0])

    def create(self, username="admin", role="administrator"):
        return self.store.create_user(CreateUserRequest(username=username, display_name="Name " + username, password=TEST_PASSWORD, role=role))


class AccountStoreTests(StoreFixture, unittest.TestCase):
    def test_bootstrap_requires_administrator_and_activation_survives_restart(self):
        self.assertFalse(self.store.enabled())
        with self.assertRaises(AccountConflict):
            self.create("viewer", "viewer")
        self.assertFalse(self.store.enabled())
        admin = self.create()
        self.assertTrue(self.store.enabled())
        self.assertTrue(UserAccountStore(self.path).enabled())
        self.assertEqual(self.store.users()[0], admin)
        # An administrative data repair must never accidentally reopen anonymous access.
        with self.store._connection() as connection:
            connection.execute("DELETE FROM dashboard_preferences")
            connection.execute("DELETE FROM users")
        self.assertTrue(UserAccountStore(self.path).enabled())

    def test_usernames_are_case_insensitive_and_public_records_never_contain_credentials(self):
        admin = self.create("Admin")
        with self.assertRaises(AccountConflict):
            self.create(" ADMIN ")
        self.assertEqual(admin.username, "Admin")
        self.assertNotIn("password", admin.model_dump_json())
        self.assertNotIn(TEST_PASSWORD, json.dumps([row.model_dump() for row in self.store.users()]))
        _, session = self.store.login("  ADMIN ", TEST_PASSWORD, "client")
        self.assertEqual(session.id, admin.id)

    def test_passwords_have_distinct_salts_and_sessions_store_only_digests(self):
        admin = self.create()
        viewer = self.create("viewer", "viewer")
        token, user = self.store.login("admin", TEST_PASSWORD, "client")
        with self.store._connection() as connection:
            hashes = [row[0] for row in connection.execute("SELECT password_hash FROM users")]
            session = connection.execute("SELECT * FROM sessions").fetchone()
        self.assertNotEqual(hashes[0], hashes[1])
        self.assertTrue(all(row.startswith("pbkdf2_sha256$600000$") and verify_password(TEST_PASSWORD, row) for row in hashes))
        self.assertTrue(all(TEST_PASSWORD not in row for row in hashes))
        self.assertNotEqual(session["token_digest"], token)
        self.assertEqual(len(session["token_digest"]), 64)
        self.assertEqual(user.id, admin.id)
        self.assertNotEqual(user.id, viewer.id)
        self.assertNotIn(token.encode(), self.path.read_bytes())
        self.assertNotIn(TEST_PASSWORD.encode(), self.path.read_bytes())

    def test_expiry_logout_and_token_rotation_do_not_reuse_session_secrets(self):
        self.create()
        first, _ = self.store.login("admin", TEST_PASSWORD, "client")
        second, _ = self.store.login("admin", TEST_PASSWORD, "client")
        self.assertNotEqual(first, second)
        self.assertIsNotNone(self.store.session_user(first))
        self.store.logout(first)
        self.assertIsNone(self.store.session_user(first))
        self.assertIsNotNone(self.store.session_user(second))
        self.now[0] += SESSION_SECONDS
        self.assertIsNone(self.store.session_user(second))

    def test_password_role_and_disable_changes_revoke_sessions(self):
        self.create()
        viewer = self.create("viewer", "viewer")
        token, _ = self.store.login("viewer", TEST_PASSWORD, "client")
        self.store.update_user(viewer.id, UpdateUserRequest(display_name="Updated name"))
        self.assertEqual(self.store.session_user(token).display_name, "Updated name")
        self.store.update_user(viewer.id, UpdateUserRequest(role="administrator"))
        self.assertIsNone(self.store.session_user(token))
        token, _ = self.store.login("viewer", TEST_PASSWORD, "client")
        self.store.update_user(viewer.id, UpdateUserRequest(password="a-new-private-password"))
        self.assertIsNone(self.store.session_user(token))
        with self.assertRaises(AuthenticationFailed):
            self.store.login("viewer", TEST_PASSWORD, "client")
        token, _ = self.store.login("viewer", "a-new-private-password", "client")
        self.store.update_user(viewer.id, UpdateUserRequest(active=False))
        self.assertIsNone(self.store.session_user(token))
        self.assertTrue(self.store.enabled())
        with self.assertRaises(AuthenticationFailed):
            self.store.login("viewer", "a-new-private-password", "client")

    def test_last_active_administrator_cannot_be_disabled_or_demoted(self):
        admin = self.create()
        self.create("viewer", "viewer")
        for change in ({"active": False}, {"role": "viewer"}, {"active": False, "role": "viewer"}):
            with self.subTest(change=change), self.assertRaises(AccountConflict):
                self.store.update_user(admin.id, UpdateUserRequest(**change))
        self.assertEqual(self.store.users()[0].role, "administrator")
        self.assertTrue(self.store.users()[0].active)

    def test_concurrent_admin_demotions_preserve_one_active_administrator(self):
        first = self.create("first")
        second = self.create("second")
        barrier = threading.Barrier(2)

        def demote(user):
            barrier.wait()
            try:
                self.store.update_user(user.id, UpdateUserRequest(role="viewer"))
                return "saved"
            except AccountConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(demote, [first, second]))
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        self.assertEqual(sum(row.active and row.role == "administrator" for row in self.store.users()), 1)

    def test_dashboard_preferences_are_isolated_persistent_and_reset_to_null(self):
        admin = self.create()
        viewer = self.create("viewer", "viewer")
        self.assertEqual(self.store.dashboard(admin.id).revision, 0)
        model = PersonalDashboardPreferences.model_validate(preferences())
        saved = self.store.save_dashboard(admin.id, DashboardUpdate(expected_revision=0, preferences=model))
        self.assertEqual(saved.revision, 1)
        self.assertIsNone(self.store.dashboard(viewer.id).preferences)
        self.assertEqual(UserAccountStore(self.path).dashboard(admin.id).preferences, model)
        with self.assertRaises(AccountConflict):
            self.store.save_dashboard(admin.id, DashboardUpdate(expected_revision=0, preferences=None))
        self.assertEqual(self.store.dashboard(admin.id).revision, 1)
        reset = self.store.save_dashboard(admin.id, DashboardUpdate(expected_revision=1, preferences=None))
        self.assertEqual(reset.revision, 2)
        self.assertIsNone(UserAccountStore(self.path).dashboard(admin.id).preferences)
        model.blocks.clear()
        self.assertIsNone(self.store.dashboard(viewer.id).preferences)

    def test_simultaneous_preference_updates_detect_one_conflict(self):
        admin = self.create()
        barrier = threading.Barrier(2)

        def save(background):
            barrier.wait()
            try:
                self.store.save_dashboard(admin.id, DashboardUpdate(expected_revision=0, preferences=preferences(background=background)))
                return "saved"
            except AccountConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, ["forest", "navy"]))
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        self.assertEqual(self.store.dashboard(admin.id).revision, 1)

    def test_login_rate_limits_are_persisted_and_do_not_disclose_user_existence(self):
        self.create()
        for username in ("admin", "unknown"):
            with self.subTest(username=username), patch("app.core.user_accounts.LOGIN_ACCOUNT_LIMIT", 2):
                for _ in range(2):
                    with self.assertRaisesRegex(AuthenticationFailed, "Username or password is incorrect"):
                        self.store.login(username, "incorrect-password", "client")
                with self.assertRaises(LoginRateLimited):
                    UserAccountStore(self.path, lambda: self.now[0]).login(username, TEST_PASSWORD, "client")
        self.now[0] += 901
        self.assertIsNotNone(self.store.login("admin", TEST_PASSWORD, "client")[1])

    def test_malformed_or_unauthorized_preferences_and_passwords_are_rejected(self):
        valid = preferences(metric_keys=[], blocks=[], density=None)
        self.assertEqual(PersonalDashboardPreferences.model_validate(valid).blocks, [])
        self.assertIsNone(PersonalDashboardPreferences.model_validate(preferences(metric_keys=None, blocks=None)).blocks)
        invalid = [preferences(version=True), preferences(version="1"), preferences(version=2), preferences(metric_keys=["flights", "flights"]), preferences(blocks=["timeline", "timeline"]), preferences(metric_keys=["untrusted"]), preferences(blocks=["unknown"]), preferences(background="https://untrusted.example/img"), preferences(density="large"), preferences(kpi={"formula": "eval"}), preferences(chart_styles={"timeline": "pie", "category": "bars", "purpose": "bars"})]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                PersonalDashboardPreferences.model_validate(candidate)
        for revision in (True, "0", -1):
            with self.subTest(revision=revision), self.assertRaises(ValidationError):
                DashboardUpdate(expected_revision=revision, preferences=None)
        for password in ("short", "x" * 129, "\ud800" * 12):
            with self.subTest(password_length=len(password)), self.assertRaises(ValidationError):
                CreateUserRequest(username="admin", display_name="Name", password=password, role="administrator")


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        environment = patch.dict(os.environ, {"APP_USERS_DB": str(self.path / "users.sqlite3"), "APP_ADMIN_TOKEN": "legacy-test-administrator", "APP_SESSION_COOKIE_SECURE": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = get_user_account_store()
        self.configuration = ApplicationConfigurationStore(self.path / "configuration.sqlite3", default_application_configuration)
        configuration_patch = patch("app.api.configuration_routes.get_configuration_store", return_value=self.configuration)
        configuration_patch.start()
        self.addCleanup(configuration_patch.stop)
        from app.main import app
        warmup_patch = patch("app.main.analytics.warm_cache", return_value=None)
        warmup_patch.start()
        self.addCleanup(warmup_patch.stop)
        self.app = app
        self.client = TestClient(app, headers={"Origin": "http://testserver"})
        self.addCleanup(self.client.close)
        self.key = {"X-App-Admin-Token": "legacy-test-administrator"}

    def create(self, username="admin", role="administrator"):
        result = self.client.post("/api/admin/users", headers=self.key, json={"username": username, "display_name": "Name " + username, "password": TEST_PASSWORD, "role": role})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def login(self, client=None, username="admin", password=TEST_PASSWORD):
        target = client or self.client
        result = target.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(result.status_code, 200, result.text)
        return result

    def test_bootstrap_does_not_reopen_or_require_anonymous_analytics_login(self):
        before = self.client.get("/api/auth/session")
        self.assertEqual(before.json(), {"enabled": False, "user": None})
        self.assertEqual(before.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        unauthorized = self.client.post("/api/admin/users", json={"username": "admin", "display_name": "Name", "password": TEST_PASSWORD, "role": "administrator"})
        self.assertEqual(unauthorized.status_code, 403)
        invalid_first = self.client.post("/api/admin/users", headers=self.key, json={"username": "viewer", "display_name": "Viewer", "password": TEST_PASSWORD, "role": "viewer"})
        self.assertEqual(invalid_first.status_code, 409)
        self.assertFalse(self.store.enabled())
        self.create()
        self.assertEqual(self.client.get("/api/auth/session").json(), {"enabled": True, "user": None})
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_all_analytics_options_source_and_cache_reads_require_sign_in(self):
        self.create()
        for method, path, body in [("get", "/api/source/status", None), ("get", "/api/source/schema", None), ("get", "/api/filters/options", None), ("get", "/api/analytics/kpi-options", None), ("get", "/api/cache/status", None), ("post", "/api/analytics/overview", {}), ("post", "/api/analytics/timeline", {}), ("post", "/api/analytics/events", {}), ("post", "/api/analytics/map", {}), ("post", "/api/analytics/comparison", {}), ("post", "/api/analytics/kpi-preview", {})]:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, **({"json": body} if body is not None else {}))
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
        with patch("app.api.routes.analytics.cache_stats", return_value={"ok": True}):
            self.assertEqual(self.client.get("/api/cache/status", headers=self.key).status_code, 200)
            self.login()
            self.assertEqual(self.client.get("/api/cache/status").status_code, 200)

    def test_anonymous_configuration_contains_branding_but_no_private_rules_or_aliases(self):
        configuration = default_application_configuration()
        configuration.appearance.title = "Public title"
        configuration.appearance.subtitle = "Public subtitle"
        configuration.dictionaries.labels = {"unit": {"private-unit": "Private unit name"}}
        configuration.kpi = KpiConfiguration.model_validate({"purpose_rules": [{"purpose": "Private rule", "success_mode": "source", "successful_results": [], "usefulness_percent": 45, "coefficient": 2}]})
        # Round-trip validation before publication is the same path used by shared administration.
        self.configuration.save_draft(configuration, 1, 0)
        self.configuration.publish(1, 1)
        self.create()
        anonymous = self.client.get("/api/app-configuration")
        self.assertEqual(anonymous.status_code, 200)
        self.assertEqual(anonymous.json()["revision"], 2)
        self.assertEqual(anonymous.json()["config"]["appearance"]["title"], "Public title")
        self.assertEqual(anonymous.json()["config"]["appearance"]["subtitle"], "Public subtitle")
        self.assertEqual(anonymous.json()["config"]["kpi"]["purpose_rules"], [])
        self.assertEqual(anonymous.json()["config"]["dictionaries"]["labels"], {})
        self.assertNotIn("Private", anonymous.text)
        legacy = self.client.get("/api/app-configuration", headers=self.key)
        self.assertEqual(legacy.json()["config"]["dictionaries"]["labels"], configuration.dictionaries.labels)
        self.login()
        signed = self.client.get("/api/app-configuration")
        self.assertEqual(signed.json()["config"], legacy.json()["config"])
        self.assertEqual(signed.json()["revision"], 2)
        self.assertEqual(self.configuration.snapshot().revision, 2)

    def test_session_cookie_attributes_and_logout_revocation(self):
        admin = self.create()
        response = self.login()
        self.assertEqual(response.json()["user"], {key: admin[key] for key in ("id", "username", "display_name", "role")})
        cookie = response.headers["set-cookie"]
        for expected in ("HttpOnly", "Max-Age=28800", "Path=/", "SameSite=strict"):
            self.assertIn(expected, cookie)
        self.assertNotIn("Secure", cookie)
        token = self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(self.client.get("/api/auth/session").json()["user"]["id"], admin["id"])
        logout = self.client.post("/api/auth/logout")
        self.assertEqual(logout.json(), {"enabled": True, "user": None})
        self.assertIsNone(self.store.session_user(token))
        self.assertEqual(self.client.get("/api/me/dashboard").status_code, 401)

    def test_reauthentication_rotates_and_revokes_the_old_browser_cookie(self):
        self.create()
        self.login()
        previous = self.client.cookies.get(SESSION_COOKIE)
        self.login()
        current = self.client.cookies.get(SESSION_COOKIE)
        self.assertNotEqual(previous, current)
        self.assertIsNone(self.store.session_user(previous))
        self.assertIsNotNone(self.store.session_user(current))

    def test_https_and_configured_secure_cookie_are_respected(self):
        self.create()
        with TestClient(self.app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as https_client:
            self.assertIn("Secure", self.login(https_client).headers["set-cookie"])
        with patch.dict(os.environ, {"APP_SESSION_COOKIE_SECURE": "true"}):
            self.assertIn("Secure", self.login().headers["set-cookie"])

    def test_viewers_have_private_settings_and_cannot_manage_users_or_shared_configuration(self):
        admin = self.create()
        viewer = self.create("viewer", "viewer")
        with TestClient(self.app, headers={"Origin": "http://testserver"}) as viewer_client:
            self.login(viewer_client, "viewer")
            self.assertEqual(viewer_client.get("/api/admin/users").status_code, 403)
            self.assertEqual(viewer_client.get("/api/admin/app-configuration").status_code, 403)
            saved = viewer_client.put("/api/me/dashboard", json={"expected_revision": 0, "preferences": preferences()})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["revision"], 1)
            self.assertIsNone(self.store.dashboard(admin["id"]).preferences)
            self.assertEqual(self.store.dashboard(viewer["id"]).preferences.background, "forest")
            self.assertEqual(viewer_client.get("/api/me/dashboard?user_id=" + admin["id"]).json()["preferences"]["background"], "forest")
        self.assertEqual(self.configuration.snapshot().revision, 1)
        self.assertIsNone(self.configuration.snapshot().draft)

    def test_logged_in_administrator_can_manage_users_and_shared_drafts_without_legacy_key(self):
        self.create()
        self.login()
        self.assertEqual(self.client.get("/api/admin/users").status_code, 200)
        created = self.client.post("/api/admin/users", json={"username": "newuser", "display_name": "New viewer", "password": TEST_PASSWORD, "role": "viewer"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(self.client.patch("/api/admin/users/" + created.json()["id"], json={"display_name": "Updated viewer"}).status_code, 200)
        current = self.client.get("/api/admin/app-configuration")
        self.assertEqual(current.status_code, 200)
        candidate = current.json()["config"]
        candidate["appearance"]["title"] = "Shared title"
        draft = self.client.put("/api/admin/app-configuration/draft", json={"config": candidate, "base_revision": 1, "expected_draft_version": 0})
        self.assertEqual(draft.status_code, 200)
        publish = self.client.post("/api/admin/app-configuration/publish", json={"base_revision": 1, "expected_draft_version": 1})
        self.assertEqual(publish.status_code, 200)
        self.assertEqual(self.configuration.snapshot().config.appearance.title, "Shared title")

    def test_personal_preferences_detect_stale_revision_and_reset_independently(self):
        self.create()
        self.login()
        self.assertEqual(self.client.get("/api/me/dashboard").json(), {"revision": 0, "preferences": None})
        saved = self.client.put("/api/me/dashboard", json={"expected_revision": 0, "preferences": preferences()})
        self.assertEqual(saved.status_code, 200)
        conflict = self.client.put("/api/me/dashboard", json={"expected_revision": 0, "preferences": None})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.client.get("/api/me/dashboard").json()["preferences"]["background"], "forest")
        reset = self.client.put("/api/me/dashboard", json={"expected_revision": 1, "preferences": None})
        self.assertEqual(reset.json(), {"revision": 2, "preferences": None})
        self.assertEqual(self.configuration.snapshot().revision, 1)

    def test_expected_identity_header_cannot_read_or_overwrite_another_tab_user(self):
        admin = self.create()
        viewer = self.create("viewer", "viewer")
        self.login()
        admin_header = {"X-Analytics-User-Id": admin["id"]}
        saved_admin = self.client.put("/api/me/dashboard", headers=admin_header, json={"expected_revision": 0, "preferences": preferences(background="navy")})
        self.assertEqual(saved_admin.status_code, 200)
        # Another tab signs in, replacing the shared browser cookie with viewer B.
        self.login(username="viewer")
        viewer_header = {"X-Analytics-User-Id": viewer["id"]}
        saved_viewer = self.client.put("/api/me/dashboard", headers=viewer_header, json={"expected_revision": 0, "preferences": preferences(background="forest")})
        self.assertEqual(saved_viewer.status_code, 200)
        self.assertEqual(saved_admin.json()["revision"], saved_viewer.json()["revision"])
        self.assertEqual(self.client.get("/api/me/dashboard", headers=admin_header).status_code, 401)
        stale_tab = self.client.put("/api/me/dashboard", headers=admin_header, json={"expected_revision": 1, "preferences": preferences(background="graphite")})
        self.assertEqual(stale_tab.status_code, 401)
        unchanged = self.client.get("/api/me/dashboard", headers=viewer_header)
        self.assertEqual(unchanged.status_code, 200)
        self.assertEqual(unchanged.json()["preferences"]["background"], "forest")
        self.assertEqual(unchanged.json()["revision"], 1)
        # Direct API callers remain compatible; cookies continue to choose identity.
        self.assertEqual(self.client.get("/api/me/dashboard").json(), unchanged.json())
        self.assertEqual(self.store.dashboard(admin["id"]).preferences.background, "navy")

    def test_role_change_password_reset_and_disabling_invalidate_existing_cookie(self):
        self.create()
        viewer = self.create("viewer", "viewer")
        with TestClient(self.app, headers={"Origin": "http://testserver"}) as viewer_client:
            self.login(viewer_client, "viewer")
            role = self.client.patch("/api/admin/users/" + viewer["id"], headers=self.key, json={"role": "administrator"})
            self.assertEqual(role.status_code, 200)
            self.assertIsNone(viewer_client.get("/api/auth/session").json()["user"])
            self.login(viewer_client, "viewer")
            password = self.client.patch("/api/admin/users/" + viewer["id"], headers=self.key, json={"password": "new-password-for-viewer"})
            self.assertEqual(password.status_code, 200)
            self.assertIsNone(viewer_client.get("/api/auth/session").json()["user"])
            self.login(viewer_client, "viewer", "new-password-for-viewer")
            self.assertEqual(self.client.patch("/api/admin/users/" + viewer["id"], headers=self.key, json={"active": False}).status_code, 200)
            self.assertEqual(viewer_client.get("/api/me/dashboard").status_code, 401)
        self.assertTrue(self.client.get("/api/auth/session").json()["enabled"])

    def test_passwords_tokens_and_private_database_details_never_echo_in_responses(self):
        self.create()
        for path, body in [("/api/auth/login", {"username": "admin", "password": "too-short"}), ("/api/admin/users", {"username": "admin", "display_name": "Name", "role": "administrator", "password": "short-test"}), ("/api/admin/users", {"username": "bad username", "display_name": "Name", "role": "administrator", "password": TEST_PASSWORD})]:
            response = self.client.post(path, headers=self.key, json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(body["password"], response.text)
            self.assertNotIn('"input"', response.text)
        for username in ("admin", "unknown"):
            response = self.client.post("/api/auth/login", json={"username": username, "password": "wrong-test-password"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"], "Username or password is incorrect")
        self.login()
        self.assertNotIn(TEST_PASSWORD, self.client.get("/api/admin/users").text)
        self.assertNotIn(self.client.cookies.get(SESSION_COOKIE), self.client.get("/api/auth/session").text)
        with patch.object(self.store, "enabled", side_effect=sqlite3.OperationalError("private-path.sql")):
            response = self.client.get("/api/auth/session")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private-path", response.text)
            self.assertEqual(self.client.get("/api/filters/options").status_code, 503)

    def test_cross_origin_login_logout_preferences_and_admin_mutations_are_rejected(self):
        admin = self.create()
        evil = {"Origin": "https://untrusted.example"}
        blocked = self.client.post("/api/auth/login", headers=evil, json={"username": "admin", "password": TEST_PASSWORD})
        self.assertEqual(blocked.status_code, 403)
        self.login()
        self.assertEqual(self.client.post("/api/auth/logout", headers=evil).status_code, 403)
        self.assertEqual(self.client.put("/api/me/dashboard", headers=evil, json={"expected_revision": 0, "preferences": preferences()}).status_code, 403)
        self.assertEqual(self.client.patch("/api/admin/users/" + admin["id"], headers=evil, json={"display_name": "Blocked"}).status_code, 403)
        allowed = self.client.put("/api/me/dashboard", headers={"Origin": "http://testserver"}, json={"expected_revision": 0, "preferences": preferences()})
        self.assertEqual(allowed.status_code, 200)
        self.assertIsNotNone(self.client.get("/api/auth/session").json()["user"])

    def test_referer_and_configured_cors_origin_checks_do_not_trust_external_sites(self):
        self.create()
        self.login()
        # Remove the browser Origin to exercise its Referer fallback explicitly.
        del self.client.headers["Origin"]
        bad = self.client.put("/api/me/dashboard", headers={"Referer": "https://untrusted.example/path"}, json={"expected_revision": 0, "preferences": None})
        self.assertEqual(bad.status_code, 403)
        good = self.client.put("/api/me/dashboard", headers={"Referer": "http://testserver/settings"}, json={"expected_revision": 0, "preferences": None})
        self.assertEqual(good.status_code, 200)
        missing = self.client.post("/api/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(missing.status_code, 403)
        from app.core.config import get_settings
        settings = get_settings().model_copy(update={"api_cors_origins": "https://allowed.example"})
        with patch("app.core.user_accounts.get_settings", return_value=settings):
            allowed = self.client.put("/api/me/dashboard", headers={"Origin": "https://allowed.example"}, json={"expected_revision": 1, "preferences": None})
            self.assertEqual(allowed.status_code, 200)

    def test_cookie_mutations_reject_absent_null_and_malformed_origins(self):
        self.create()
        self.login()
        for origin in ("null", "not-an-origin", "http://testserver/invalid/path", "http://user@testserver", "http://testserver:0"):
            with self.subTest(origin=origin):
                response = self.client.put("/api/me/dashboard", headers={"Origin": origin}, json={"expected_revision": 0, "preferences": None})
                self.assertEqual(response.status_code, 403)
        del self.client.headers["Origin"]
        response = self.client.put("/api/me/dashboard", json={"expected_revision": 0, "preferences": None})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 403)
        self.assertEqual(self.client.get("/api/me/dashboard").json()["revision"], 0)

    def test_cache_clearing_requires_administrator_and_accepts_admin_sessions(self):
        self.create()
        self.create("viewer", "viewer")
        from app.core.config import get_backend_config
        config = get_backend_config().model_copy(deep=True)
        config.cache.admin.enabled = True
        with patch("app.api.routes.get_backend_config", return_value=config), patch("app.api.routes.analytics.clear_cache", return_value={"cleared": 1}), patch("app.api.routes.analytics.cache_stats", return_value={}):
            self.assertEqual(self.client.post("/api/cache/clear").status_code, 401)
            self.login(username="viewer")
            self.assertEqual(self.client.post("/api/cache/clear").status_code, 403)
            self.login()
            self.assertEqual(self.client.post("/api/cache/clear").status_code, 200)
            self.assertEqual(self.client.post("/api/cache/clear", headers={"Origin": "https://untrusted.example"}).status_code, 403)
            config.cache.admin.enabled = False
            self.assertEqual(self.client.post("/api/cache/clear").status_code, 404)

    def test_account_validation_rejects_unsafe_preference_fields_and_strict_boolean_coercion(self):
        admin = self.create()
        self.login()
        for candidate in (preferences(blocks=["timeline", "timeline"]), preferences(metric_keys=["unknown"]), preferences(kpi={"formula": "eval"}), preferences(version=True)):
            response = self.client.put("/api/me/dashboard", json={"expected_revision": 0, "preferences": candidate})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.patch("/api/admin/users/" + admin["id"], json={"active": "false"}).status_code, 422)
        self.assertEqual(self.client.patch("/api/admin/users/" + admin["id"], json={"active": False}).status_code, 409)
        self.assertEqual(self.client.patch("/api/admin/users/" + admin["id"], json={"role": "viewer"}).status_code, 409)
        self.assertEqual(self.client.get("/api/me/dashboard").json()["revision"], 0)


if __name__ == "__main__":
    unittest.main()
