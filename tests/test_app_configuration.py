import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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
    ConfigurationConflict,
    ConfigurationRevisionNotFound,
    default_application_configuration,
)


class ConfigurationFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data" / "configuration.sqlite3"
        self.seed = default_application_configuration()
        self.store = ApplicationConfigurationStore(self.path, lambda: self.seed.model_copy(deep=True))

    def changed(self, title="Changed application"):
        config = self.seed.model_copy(deep=True)
        config.appearance.title = title
        return config


class ConfigurationStoreTests(ConfigurationFixture, unittest.TestCase):
    def test_seed_has_existing_metrics_compact_filters_and_dashboard_defaults(self):
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(snapshot.draft_version, 0)
        self.assertEqual(snapshot.config.appearance.density, "compact")
        self.assertEqual(snapshot.config.filters.layout, "compact")
        self.assertEqual([field.key for field in snapshot.config.filters.fields if field.placement == "main"], ["organization", "category", "purpose"])
        self.assertEqual({field.key: field.label for field in snapshot.config.filters.fields if field.key in {"organization", "category", "asset", "class_name"}}, {"organization": "Підрозділ", "category": "Кафедра", "asset": "Конкретний засіб", "class_name": "Клас цілі"})
        self.assertEqual(snapshot.config.dashboard.blocks, ["map", "timeline", "departments", "lost_devices"])
        self.assertEqual(snapshot.config.tables.page_size, 18)
        self.assertEqual(len(snapshot.config.tables.columns), 10)
        self.assertEqual(snapshot.config.defaults.comparison_mode, "units_over_time")
        self.assertEqual(snapshot.config.defaults.comparison_granularity, "week")
        self.assertIsNone(snapshot.draft)
        self.assertEqual([(row.revision, row.action) for row in snapshot.history], [(1, "initial")])

    def test_saving_and_previewing_do_not_change_publication_or_history(self):
        draft = self.changed()
        preview = self.store.preview(draft, 1)
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["config"]["appearance"]["title"], "Changed application")
        self.assertIsNone(self.store.snapshot().draft)
        saved = self.store.save_draft(draft, 1)
        self.assertEqual(saved.revision, 1)
        self.assertEqual(saved.config, self.seed)
        self.assertEqual(saved.draft_base_revision, 1)
        self.assertEqual(saved.draft, draft)
        self.assertEqual(saved.draft_version, 1)
        self.assertEqual(len(saved.history), 1)

    def test_configuration_and_draft_survive_new_store_instance_without_reseeding(self):
        self.store.save_draft(self.changed("Published"), 1)
        published = self.store.publish(1)
        self.store.save_draft(self.changed("Unpublished draft"), 2)

        def must_not_reseed():
            raise AssertionError("A persisted configuration must not be reseeded")

        restarted = ApplicationConfigurationStore(self.path, must_not_reseed).snapshot()
        self.assertEqual(restarted.revision, 2)
        self.assertEqual(restarted.config, published.config)
        self.assertEqual(restarted.draft.appearance.title, "Unpublished draft")
        self.assertEqual(restarted.draft_base_revision, 2)
        self.assertEqual(restarted.draft_version, 3)
        self.assertEqual(len(restarted.history), 2)

    def test_snapshots_and_input_models_do_not_share_mutable_configuration(self):
        input_model = self.changed()
        saved = self.store.save_draft(input_model, 1)
        input_model.tables.columns.reverse()
        input_model.appearance.title = "Changed after saving"
        saved.draft.dashboard.metric_keys.clear()
        saved.config.appearance.menu.clear()
        fresh = self.store.snapshot()
        self.assertEqual(fresh.draft.appearance.title, "Changed application")
        self.assertEqual(fresh.draft.tables.columns, self.seed.tables.columns)
        self.assertEqual(fresh.config, self.seed)
        self.assertEqual(fresh.draft.dashboard.metric_keys, self.seed.dashboard.metric_keys)

    def test_publish_and_rollback_create_new_versions_and_discard_drafts(self):
        self.store.save_draft(self.changed(), 1)
        published = self.store.publish(1)
        self.assertEqual(published.revision, 2)
        self.assertEqual(published.draft_version, 2)
        self.assertEqual(published.config.appearance.title, "Changed application")
        self.assertIsNone(published.draft)
        self.store.save_draft(self.changed("Another draft"), 2)
        rolled_back = self.store.rollback(1, 2)
        self.assertEqual(rolled_back.revision, 3)
        self.assertEqual(rolled_back.draft_version, 4)
        self.assertEqual(rolled_back.config, self.seed)
        self.assertIsNone(rolled_back.draft)
        self.assertEqual([(row.revision, row.action) for row in rolled_back.history], [(3, "rollback"), (2, "publish"), (1, "initial")])
        self.assertTrue(all(row.created_at.endswith("+00:00") for row in rolled_back.history))

    def test_stale_base_revisions_cannot_save_preview_publish_or_rollback(self):
        self.store.save_draft(self.changed(), 1)
        self.store.publish(1)
        calls = [
            lambda: self.store.save_draft(self.changed("Stale"), 1),
            lambda: self.store.preview(self.changed("Stale"), 1),
            lambda: self.store.publish(1),
            lambda: self.store.rollback(1, 1),
        ]
        for call in calls:
            with self.subTest(call=call), self.assertRaises(ConfigurationConflict):
                call()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.revision, 2)
        self.assertEqual(snapshot.config.appearance.title, "Changed application")
        self.assertIsNone(snapshot.draft)
        self.assertEqual(len(snapshot.history), 2)

    def test_missing_draft_and_revision_leave_active_version_unchanged(self):
        with self.assertRaises(ConfigurationConflict):
            self.store.publish(1)
        with self.assertRaises(ConfigurationRevisionNotFound):
            self.store.rollback(999, 1)
        self.assertEqual(self.store.snapshot().revision, 1)

    def test_concurrent_publications_allow_exactly_one_expected_revision(self):
        self.store.save_draft(self.changed(), 1)
        ready = threading.Barrier(2)

        def publish():
            ready.wait(timeout=3)
            try:
                return self.store.publish(1).revision
            except ConfigurationConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(publish) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]
        self.assertCountEqual(results, [2, "conflict"])
        self.assertEqual(len(self.store.snapshot().history), 2)

    def test_same_published_revision_cannot_overwrite_an_unseen_draft(self):
        saved = self.store.save_draft(self.changed("Draft A"), 1, 0)
        self.assertEqual(saved.draft_version, 1)
        with self.assertRaises(ConfigurationConflict):
            self.store.save_draft(self.changed("Draft B"), 1, 0)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(snapshot.draft_version, 1)
        self.assertEqual(snapshot.draft.appearance.title, "Draft A")
        self.assertEqual(len(snapshot.history), 1)

    def test_publish_cannot_activate_a_draft_replaced_since_review(self):
        self.store.save_draft(self.changed("Draft A"), 1, 0)
        replaced = self.store.save_draft(self.changed("Draft B"), 1, 1)
        with self.assertRaises(ConfigurationConflict):
            self.store.publish(1, 1)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(snapshot.draft_version, 2)
        self.assertEqual(snapshot.draft, replaced.draft)
        self.assertEqual(len(snapshot.history), 1)
        published = self.store.publish(1, 2)
        self.assertEqual(published.revision, 2)
        self.assertEqual(published.draft_version, 3)
        self.assertEqual(published.config.appearance.title, "Draft B")

    def test_concurrent_draft_saves_allow_exactly_one_expected_generation(self):
        self.store.snapshot()
        ready = threading.Barrier(2)

        def save(title):
            ready.wait(timeout=3)
            try:
                return self.store.save_draft(self.changed(title), 1, 0).draft_version
            except ConfigurationConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(save, title) for title in ("Draft A", "Draft B")]
            results = [future.result(timeout=5) for future in futures]
        self.assertCountEqual(results, [1, "conflict"])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.revision, 1)
        self.assertEqual(snapshot.draft_version, 1)
        self.assertIn(snapshot.draft.appearance.title, ("Draft A", "Draft B"))
        self.assertEqual(len(snapshot.history), 1)

    def test_existing_database_migrates_draft_generation_and_preserves_versions(self):
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE configuration_versions (revision INTEGER PRIMARY KEY, config_json TEXT NOT NULL, created_at TEXT NOT NULL, action TEXT NOT NULL)")
            connection.execute("CREATE TABLE configuration_state (id INTEGER PRIMARY KEY CHECK (id = 1), active_revision INTEGER NOT NULL)")
            connection.execute("CREATE TABLE configuration_draft (id INTEGER PRIMARY KEY CHECK (id = 1), config_json TEXT NOT NULL, base_revision INTEGER NOT NULL)")
            connection.execute("INSERT INTO configuration_versions VALUES (1, ?, '2026-10-02T00:00:00+00:00', 'initial')", (self.seed.model_dump_json(),))
            connection.execute("INSERT INTO configuration_state VALUES (1, 1)")
            connection.execute("INSERT INTO configuration_draft VALUES (1, ?, 1)", (self.changed("Existing draft").model_dump_json(),))
            connection.commit()
        migrated = self.store.snapshot()
        self.assertEqual(migrated.revision, 1)
        self.assertEqual(migrated.draft_version, 0)
        self.assertEqual(migrated.draft.appearance.title, "Existing draft")
        self.store.save_draft(self.changed("Replacement draft"), 1, 0)
        self.store.publish(1, 1)
        restarted = ApplicationConfigurationStore(self.path).snapshot()
        self.assertEqual(restarted.revision, 2)
        self.assertEqual(restarted.draft_version, 2)
        self.assertEqual(restarted.config.appearance.title, "Replacement draft")

    def test_ordered_fields_metrics_menu_and_columns_survive_round_trip(self):
        ordered = self.changed()
        ordered.filters.fields.reverse()
        ordered.dashboard.metric_keys.reverse()
        ordered.tables.columns.reverse()
        ordered.appearance.menu.reverse()
        self.store.save_draft(ordered, 1)
        self.assertEqual(self.store.publish(1).config, ordered)

    def test_revalidation_rejects_mutated_invalid_models_before_persistence(self):
        invalid = self.changed()
        invalid.tables.page_size = -1
        with self.assertRaises(ValidationError):
            self.store.save_draft(invalid, 1)
        self.assertIsNone(self.store.snapshot().draft)


class ConfigurationApiTests(ConfigurationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"APP_ADMIN_TOKEN": "application-admin-test-token", "CACHE_ADMIN_TOKEN": "cache-test-token"})
        environment.start()
        self.addCleanup(environment.stop)
        store = patch("app.api.configuration_routes.get_configuration_store", return_value=self.store)
        store.start()
        self.addCleanup(store.stop)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        self.client = TestClient(app)
        self.headers = {"X-App-Admin-Token": "application-admin-test-token"}

    def test_public_contract_has_no_draft_history_secret_or_internal_path(self):
        self.store.save_draft(self.changed("Unpublished private title"), 1)
        response = self.client.get("/api/app-configuration")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"revision", "config", "administration_enabled"})
        self.assertTrue(response.json()["administration_enabled"])
        self.assertNotIn("Unpublished private title", response.text)
        self.assertNotIn("application-admin-test-token", response.text)
        self.assertNotIn(str(self.path), response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_admin_reads_and_writes_require_the_separate_application_token(self):
        body = {"config": self.changed().model_dump(), "base_revision": 1, "expected_draft_version": 0}
        for headers in ({}, {"X-App-Admin-Token": "wrong"}, {"X-Cache-Admin-Token": "cache-test-token"}, {"X-App-Admin-Token": "cache-test-token"}):
            with self.subTest(headers=headers):
                read = self.client.get("/api/admin/app-configuration", headers=headers)
                write = self.client.put("/api/admin/app-configuration/draft", json=body, headers=headers)
                self.assertEqual(read.status_code, 403)
                self.assertEqual(write.status_code, 403)
                self.assertNotIn("config", read.json())
                self.assertNotIn("application-admin-test-token", read.text)
        self.assertIsNone(self.store.snapshot().draft)
        self.assertEqual(self.client.get("/api/admin/app-configuration", headers=self.headers).status_code, 200)

    def test_absent_token_disables_all_admin_endpoints_without_cache_token_fallback(self):
        with patch.dict(os.environ, {"APP_ADMIN_TOKEN": ""}):
            public = self.client.get("/api/app-configuration")
            self.assertFalse(public.json()["administration_enabled"])
            for method, path, body in [
                ("get", "", None),
                ("put", "/draft", {"config": self.seed.model_dump(), "base_revision": 1, "expected_draft_version": 0}),
                ("post", "/preview", {"config": self.seed.model_dump(), "base_revision": 1}),
                ("post", "/publish", {"base_revision": 1, "expected_draft_version": 0}),
                ("post", "/rollback", {"base_revision": 1, "target_revision": 1}),
            ]:
                response = getattr(self.client, method)(f"/api/admin/app-configuration{path}", headers=self.headers, **({"json": body} if body else {}))
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("config", response.json())

    def test_preview_draft_publish_and_public_read_follow_one_revision(self):
        body = {"config": self.changed().model_dump(), "base_revision": 1, "expected_draft_version": 0}
        preview = self.client.post("/api/admin/app-configuration/preview", json={"config": body["config"], "base_revision": 1}, headers=self.headers)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["revision"], 1)
        self.assertTrue(preview.json()["valid"])
        self.assertIsNone(self.store.snapshot().draft)
        saved = self.client.put("/api/admin/app-configuration/draft", json=body, headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["draft_base_revision"], 1)
        self.assertEqual(saved.json()["draft_version"], 1)
        self.assertEqual(self.client.get("/api/app-configuration").json()["config"]["appearance"]["title"], self.seed.appearance.title)
        published = self.client.post("/api/admin/app-configuration/publish", json={"base_revision": 1, "expected_draft_version": 1}, headers=self.headers)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["revision"], 2)
        self.assertEqual(published.json()["draft_version"], 2)
        self.assertIsNone(published.json()["draft"])
        self.assertEqual(self.client.get("/api/app-configuration").json()["config"]["appearance"]["title"], "Changed application")

    def test_stale_admin_mutations_are_409_and_missing_rollback_is_404(self):
        self.store.save_draft(self.changed(), 1)
        self.store.publish(1)
        for path, body in [
            ("/draft", {"base_revision": 1, "config": self.changed("Stale").model_dump(), "expected_draft_version": 1}),
            ("/preview", {"base_revision": 1, "config": self.changed().model_dump()}),
            ("/publish", {"base_revision": 1, "expected_draft_version": 1}),
            ("/rollback", {"base_revision": 1, "target_revision": 1}),
        ]:
            response = self.client.post(f"/api/admin/app-configuration{path}", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 409)
        missing = self.client.post("/api/admin/app-configuration/rollback", json={"base_revision": 2, "target_revision": 999}, headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.store.snapshot().revision, 2)

    def test_custom_filters_round_trip_through_draft_and_publication(self):
        custom = [
            {"id": "ammo", "label": "Тип БК", "field": "bc_name", "type": "select", "placement": "main", "options": [{"value": "a", "label": "A"}]},
            {"id": "count_range", "label": "Кількість БК", "field": "bc_count", "type": "number_range", "placement": "extra", "options": []},
            {"id": "note", "label": "Текст", "field": "result", "type": "text", "placement": "hidden", "options": []},
            {"id": "effective_flag", "label": "Ефективні", "field": "is_effective", "type": "boolean", "placement": "extra", "options": []},
        ]
        config = self.seed.model_dump()
        config["filters"]["custom"] = custom
        response = self.client.put("/api/admin/app-configuration/draft", json={"config": config, "base_revision": 1, "expected_draft_version": 0}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.snapshot().draft.filters.custom[0].id, "ammo")
        self.assertEqual(ApplicationConfiguration.model_validate(self.seed.model_dump()).filters.custom, [])

    def test_unknown_keys_unrecognized_values_and_invalid_ranges_are_rejected(self):
        invalid_configs = []

        def candidate(change):
            config = self.seed.model_dump()
            change(config)
            invalid_configs.append(config)

        candidate(lambda config: config.update(clickhouse_password="must-not-be-configurable"))
        candidate(lambda config: config["filters"].update(raw_sql="SELECT secret"))
        candidate(lambda config: config["filters"]["fields"].pop())
        candidate(lambda config: config["filters"]["fields"][0].update(key="unknown"))
        candidate(lambda config: config["filters"]["fields"][0].update(label=" "))
        candidate(lambda config: config["filters"]["fields"][1].update(key="organization"))
        candidate(lambda config: config["dashboard"].update(metric_keys=["arbitrary_formula"]))
        candidate(lambda config: config["dashboard"].update(metric_keys=[]))
        candidate(lambda config: config["dashboard"].update(blocks=["map", "map"]))
        candidate(lambda config: config["tables"].update(page_size=-1))
        candidate(lambda config: config["tables"].update(page_size=4))
        candidate(lambda config: config["tables"].update(page_size=True))
        candidate(lambda config: config["tables"].update(columns=["secret_column"]))
        candidate(lambda config: config["appearance"].update(menu=["settings"]))
        candidate(lambda config: config["defaults"].update(date_range_days=0))
        candidate(lambda config: config["defaults"].update(date_range_days=366))
        candidate(lambda config: config["dictionaries"].update(labels={"unknown": {"id": "label"}}))
        candidate(lambda config: config["dictionaries"].update(labels={"bbak": {str(index): "label" for index in range(501)}}))
        good = {"id": "ammo", "label": "Тип БК", "field": "bc_name", "type": "select", "placement": "extra", "options": [{"value": "a", "label": "A"}]}
        candidate(lambda config: config["filters"].update(custom=[{**good, "id": "organization"}]))
        candidate(lambda config: config["filters"].update(custom=[good, good]))
        candidate(lambda config: config["filters"].update(custom=[{**good, "id": "Bad Id"}]))
        candidate(lambda config: config["filters"].update(custom=[{**good, "field": "no_such_field"}]))
        candidate(lambda config: config["filters"].update(custom=[{**good, "options": []}]))
        candidate(lambda config: config["filters"].update(custom=[{**good, "type": "text"}]))
        candidate(lambda config: config["filters"].update(custom=[{**good, "options": [good["options"][0], good["options"][0]]}]))
        candidate(lambda config: config["filters"].update(custom=[{**good, "label": " "}]))
        for config in invalid_configs:
            with self.subTest(config=config):
                response = self.client.put("/api/admin/app-configuration/draft", json={"config": config, "base_revision": 1, "expected_draft_version": 0}, headers=self.headers)
                self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.store.snapshot().draft)

    def test_dictionary_aliases_are_public_configuration_without_changing_stable_ids(self):
        config = self.changed()
        config.dictionaries.labels = {"bbak": {"42": "Organization label"}, "category": {"source-type": "Displayed department"}}
        saved = self.client.post("/api/admin/app-configuration/draft", json={"config": config.model_dump(), "base_revision": 1, "expected_draft_version": 0}, headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        published = self.client.post("/api/admin/app-configuration/publish", json={"base_revision": 1, "expected_draft_version": 1}, headers=self.headers)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(self.client.get("/api/app-configuration").json()["config"]["dictionaries"]["labels"], config.dictionaries.labels)

    def test_draft_and_publish_require_a_strict_expected_draft_version(self):
        for version in (None, -1, "0", True):
            for path in ("draft", "publish"):
                with self.subTest(version=version, path=path):
                    body = {"base_revision": 1}
                    if path == "draft":
                        body["config"] = self.changed().model_dump()
                    if version is not None:
                        body["expected_draft_version"] = version
                    response = self.client.post(f"/api/admin/app-configuration/{path}", json=body, headers=self.headers)
                    self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.store.snapshot().draft)

    def test_replaced_draft_rejects_stale_save_and_publish_at_same_live_revision(self):
        def save(title, version):
            return self.client.put("/api/admin/app-configuration/draft", json={"config": self.changed(title).model_dump(), "base_revision": 1, "expected_draft_version": version}, headers=self.headers)

        initial = self.client.get("/api/admin/app-configuration", headers=self.headers).json()
        self.assertEqual(initial["draft_version"], 0)
        first = save("Draft A", 0)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["draft_version"], 1)
        stale_save = save("Draft B", 0)
        self.assertEqual(stale_save.status_code, 409)
        replacement = save("Draft B", 1)
        self.assertEqual(replacement.status_code, 200)
        self.assertEqual(replacement.json()["draft_version"], 2)
        stale_publish = self.client.post("/api/admin/app-configuration/publish", json={"base_revision": 1, "expected_draft_version": 1}, headers=self.headers)
        self.assertEqual(stale_publish.status_code, 409)
        self.assertEqual(self.client.get("/api/app-configuration").json()["revision"], 1)
        latest = self.client.get("/api/admin/app-configuration", headers=self.headers).json()
        self.assertEqual(latest["draft"]["appearance"]["title"], "Draft B")
        self.assertEqual(latest["draft_version"], 2)
        published = self.client.post("/api/admin/app-configuration/publish", json={"base_revision": 1, "expected_draft_version": 2}, headers=self.headers)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["draft_version"], 3)
        self.assertEqual(published.json()["config"]["appearance"]["title"], "Draft B")

    def test_storage_failure_returns_generic_error_without_database_details(self):
        with patch.object(self.store, "snapshot", side_effect=sqlite3.OperationalError("private database path")), patch("app.api.configuration_routes.logger.exception"):
            response = self.client.get("/api/app-configuration")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)


if __name__ == "__main__":
    unittest.main()


def test_custom_filter_accepts_new_clickhouse_column_field():
    from app.core.app_configuration import FilterConfiguration, default_application_configuration

    config = default_application_configuration().model_dump(mode="json")
    config["filters"]["custom"] = [{"id": "new_col", "label": "Нова колонка", "field": "x_region_code", "type": "text"}]
    assert FilterConfiguration.model_validate(config["filters"]).custom[0].field == "x_region_code"
    config["filters"]["custom"][0]["field"] = "no_such_field"
    import pytest
    with pytest.raises(ValueError):
        FilterConfiguration.model_validate(config["filters"])
