import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import bi_routes, preferences_routes
from app.bi.models import DataScope
from app.bi.security import Principal
from app.bi.store import BiStore


class UserPreferencesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BiStore(Path(directory.name) / "bi.sqlite3")
        self.user = Principal("user-1", "viewer", frozenset({"dashboard.view"}), DataScope(scope_type="ALL"))
        app = FastAPI()
        app.include_router(preferences_routes.router, prefix="/api")
        app.dependency_overrides[bi_routes.principal] = lambda: self.user
        self.client = TestClient(app, headers={"Origin": "http://testserver"})
        self.addCleanup(self.client.close)
        replacement = patch.object(preferences_routes, "get_bi_store", return_value=self.store)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_settings_follow_the_user_and_stay_private(self):
        self.assertEqual(self.client.get("/api/v1/me/preferences").json(), {})
        filters = {"date": {"mode": "relative", "end_offset": 0, "length": 7}, "values": {"bbak": ["4"]}}
        first = self.client.put("/api/v1/me/preferences/filters", json={"value": filters})
        self.assertEqual(first.json()["revision"], 1, first.text)
        self.assertEqual(self.client.put("/api/v1/me/preferences/filters", json={"value": {**filters, "values": {}}}).json()["revision"], 2)
        self.client.put("/api/v1/me/preferences/ui", json={"value": {"density": "compact", "language": "uk"}})
        saved = self.client.get("/api/v1/me/preferences").json()
        self.assertEqual((saved["filters"]["value"]["values"], saved["ui"]["value"]["density"]), ({}, "compact"))
        self.user = Principal("user-2", "viewer", frozenset(), DataScope(scope_type="ALL"))
        self.assertEqual(self.client.get("/api/v1/me/preferences").json(), {})

    def test_unknown_sections_large_values_and_anonymous_users_are_rejected(self):
        self.assertEqual(self.client.put("/api/v1/me/preferences/secrets", json={"value": 1}).status_code, 422)
        self.assertEqual(self.client.put("/api/v1/me/preferences/ui", json={"value": "x" * 70000}).status_code, 413)
        self.assertEqual(self.client.put("/api/v1/me/preferences/ui", json={"value": 1}, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.user = Principal(None, "viewer", frozenset(), DataScope(scope_type="ALL"))
        self.assertEqual(self.client.get("/api/v1/me/preferences").json(), {})
        self.assertEqual(self.client.put("/api/v1/me/preferences/ui", json={"value": 1}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
