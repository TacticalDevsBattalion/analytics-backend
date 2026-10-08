import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.configuration_routes import router
from app.core.app_configuration import (
    ApplicationConfiguration,
    ApplicationConfigurationStore,
    CardDetailsConfiguration,
    default_application_configuration,
)
from app.core.kpi_configuration import kpi_fingerprint


DEFAULT_DETAILS = {"interaction": "auto", "hover_delay_ms": 0, "width": "standard"}
CLICK_DETAILS = {"interaction": "click", "hover_delay_ms": 650, "width": "wide"}


def invalid_details():
    values = [
        {"hover_delay_ms": value} for value in (True, False, -1, 1501, 1.0, "0", None)
    ]
    values.extend({"interaction": value} for value in (True, 0, None, "hover", "AUTO"))
    values.extend({"width": value} for value in (False, 390, None, "compact"))
    values.extend([{"unrecognized": "value"}, None, [], "auto"])
    return values


class CardDetailsFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "configuration.sqlite3"
        self.seed = default_application_configuration()
        self.store = ApplicationConfigurationStore(self.path, lambda: self.seed.model_copy(deep=True))

    def configuration(self, details):
        value = self.seed.model_dump(mode="json")
        value["appearance"]["card_details"] = details
        return ApplicationConfiguration.model_validate(value)

    def persisted_rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return {
                "versions": connection.execute("SELECT revision, config_json, created_at, action FROM configuration_versions ORDER BY revision").fetchall(),
                "state": connection.execute("SELECT id, active_revision, draft_version FROM configuration_state").fetchall(),
                "draft": connection.execute("SELECT id, config_json, base_revision FROM configuration_draft").fetchall(),
            }


class CardDetailsConfigurationTests(CardDetailsFixture, unittest.TestCase):
    def test_omitted_and_partial_fields_preserve_existing_presentation_defaults(self):
        old = self.seed.model_dump(mode="json")
        old["appearance"].pop("card_details")
        first = ApplicationConfiguration.model_validate(old)
        second = ApplicationConfiguration.model_validate_json(json.dumps(old))
        self.assertEqual(first.appearance.card_details.model_dump(), DEFAULT_DETAILS)
        self.assertEqual(second.appearance.card_details.model_dump(), DEFAULT_DETAILS)
        first.appearance.card_details.width = "wide"
        self.assertEqual(second.appearance.card_details.width, "standard")
        self.assertEqual(CardDetailsConfiguration.model_validate({"interaction": "disabled"}).model_dump(), {**DEFAULT_DETAILS, "interaction": "disabled"})
        for delay in (0, 1500):
            self.assertEqual(CardDetailsConfiguration(hover_delay_ms=delay).hover_delay_ms, delay)

    def test_invalid_types_ranges_enum_values_and_extra_keys_are_rejected(self):
        for value in invalid_details():
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.configuration(value)

    def test_old_published_draft_and_history_receive_defaults_without_rewriting_database_rows(self):
        historical = self.seed.model_dump(mode="json")
        historical["appearance"].pop("card_details")
        published = json.loads(json.dumps(historical))
        published["appearance"]["title"] = "Published legacy appearance"
        draft = json.loads(json.dumps(historical))
        draft["appearance"]["title"] = "Unpublished legacy appearance"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE configuration_versions (revision INTEGER PRIMARY KEY, config_json TEXT NOT NULL, created_at TEXT NOT NULL, action TEXT NOT NULL)")
            connection.execute("CREATE TABLE configuration_state (id INTEGER PRIMARY KEY CHECK(id=1), active_revision INTEGER NOT NULL, draft_version INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE TABLE configuration_draft (id INTEGER PRIMARY KEY CHECK(id=1), config_json TEXT NOT NULL, base_revision INTEGER NOT NULL)")
            connection.execute("INSERT INTO configuration_versions VALUES (3, ?, '2026-09-01T00:00:00+00:00', 'initial')", (json.dumps(historical, ensure_ascii=False, indent=2),))
            connection.execute("INSERT INTO configuration_versions VALUES (7, ?, '2026-09-02T00:00:00+00:00', 'publish')", (json.dumps(published, ensure_ascii=False, indent=2),))
            connection.execute("INSERT INTO configuration_state VALUES (1, 7, 11)")
            connection.execute("INSERT INTO configuration_draft VALUES (1, ?, 7)", (json.dumps(draft, ensure_ascii=False, indent=2),))
            connection.commit()
        before = self.persisted_rows()
        for _ in range(2):
            snapshot = self.store.snapshot()
            self.assertEqual(snapshot.revision, 7)
            self.assertEqual(snapshot.draft_version, 11)
            self.assertEqual(snapshot.draft_base_revision, 7)
            self.assertEqual(snapshot.config.appearance.card_details.model_dump(), DEFAULT_DETAILS)
            self.assertEqual(snapshot.draft.appearance.card_details.model_dump(), DEFAULT_DETAILS)
            self.assertEqual(snapshot.config.appearance.title, "Published legacy appearance")
            self.assertEqual(snapshot.draft.appearance.title, "Unpublished legacy appearance")
            self.assertEqual([row.revision for row in snapshot.history], [7, 3])
        for row in before["versions"]:
            self.assertEqual(ApplicationConfiguration.model_validate_json(row[1]).appearance.card_details.model_dump(), DEFAULT_DETAILS)
        self.assertEqual(self.persisted_rows(), before)

    def test_explicit_presentation_survives_restart_publication_and_rollback_without_changing_kpi(self):
        initial = self.store.snapshot()
        initial_rows = self.persisted_rows()["versions"]
        explicit = self.configuration(CLICK_DETAILS)
        saved = self.store.save_draft(explicit, initial.revision, initial.draft_version)
        self.assertEqual(saved.config.appearance.card_details.model_dump(), DEFAULT_DETAILS)
        self.assertEqual(saved.draft.appearance.card_details.model_dump(), CLICK_DETAILS)
        published = self.store.publish(saved.revision, saved.draft_version)
        self.assertEqual(published.config.appearance.card_details.model_dump(), CLICK_DETAILS)
        self.assertEqual(published.config.kpi, initial.config.kpi)
        self.assertEqual(kpi_fingerprint(published.config.kpi), kpi_fingerprint(initial.config.kpi))
        restarted = ApplicationConfigurationStore(self.path).snapshot()
        self.assertEqual(restarted.config.appearance.card_details.model_dump(), CLICK_DETAILS)
        self.assertEqual(restarted.revision, published.revision)
        self.assertEqual(self.persisted_rows()["versions"][:1], initial_rows)
        changed = self.configuration({"interaction": "disabled", "hover_delay_ms": 1500, "width": "standard"})
        saved = self.store.save_draft(changed, restarted.revision, restarted.draft_version)
        changed_publication = self.store.publish(saved.revision, saved.draft_version)
        rolled_back = self.store.rollback(published.revision, changed_publication.revision)
        self.assertEqual(rolled_back.config.appearance.card_details.model_dump(), CLICK_DETAILS)
        self.assertEqual(rolled_back.config.kpi, initial.config.kpi)
        self.assertEqual([row.action for row in rolled_back.history], ["rollback", "publish", "publish", "initial"])
        self.assertEqual(self.persisted_rows()["versions"][:1], initial_rows)

    def test_mutated_invalid_presentation_is_revalidated_before_persistence(self):
        self.store.snapshot()
        before = self.persisted_rows()
        invalid = self.seed.model_copy(deep=True)
        invalid.appearance.card_details.hover_delay_ms = True
        with self.assertRaises(ValidationError):
            self.store.save_draft(invalid, 1, 0)
        self.assertEqual(self.persisted_rows(), before)


class CardDetailsApiTests(CardDetailsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"APP_ADMIN_TOKEN": "card-presentation-test-key", "APP_USERS_DB": str(self.directory / "users.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        store = patch("app.api.configuration_routes.get_configuration_store", return_value=self.store)
        store.start()
        self.addCleanup(store.stop)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"X-App-Admin-Token": "card-presentation-test-key"}

    def test_old_json_request_is_accepted_and_response_adds_defaults_without_publication(self):
        old = self.seed.model_dump(mode="json")
        old["appearance"].pop("card_details")
        self.store.snapshot()
        before = self.persisted_rows()
        response = self.client.post("/api/admin/app-configuration/preview", headers=self.headers, json={"config": old, "base_revision": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["config"]["appearance"]["card_details"], DEFAULT_DETAILS)
        self.assertEqual(self.persisted_rows(), before)
        draft = self.client.put("/api/admin/app-configuration/draft", headers=self.headers, json={"config": old, "base_revision": 1, "expected_draft_version": 0})
        self.assertEqual(draft.status_code, 200, draft.text)
        self.assertEqual(draft.json()["revision"], 1)
        self.assertEqual(draft.json()["draft"]["appearance"]["card_details"], DEFAULT_DETAILS)

    def test_api_rejects_malformed_presentation_without_changing_draft_or_revision(self):
        self.store.snapshot()
        before = self.persisted_rows()
        for value in invalid_details():
            config = self.seed.model_dump(mode="json")
            config["appearance"]["card_details"] = value
            with self.subTest(value=value):
                response = self.client.put("/api/admin/app-configuration/draft", headers=self.headers, json={"config": config, "base_revision": 1, "expected_draft_version": 0})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.persisted_rows(), before)


if __name__ == "__main__":
    unittest.main()
