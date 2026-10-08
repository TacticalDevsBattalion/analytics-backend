import math
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.models import ComparisonRequest, FilterRequest
from app.api.routes import router
from app.core.app_configuration import ApplicationConfigurationStore, default_application_configuration
from app.core.cache import MemoryCache
from app.core.kpi_configuration import KpiConfiguration, KpiDataUnavailable, KpiSnapshot, PurposeKpiRule, current_kpi_snapshot, kpi_fingerprint, pin_kpi_snapshot
from app.services import analytics, comparison
from app.services.analytics_engine import AnalyticsEngine
from app.services.clickhouse_analytics import ClickHouseAnalytics


def filters(**updates):
    return FilterRequest.model_validate({"date_from": "2026-09-01", "date_to": "2026-09-07", **updates})


def rule(purpose="Розвідка", **updates):
    return PurposeKpiRule(purpose=purpose, **updates)


def flight(identifier, purpose="Розвідка", result="Виявлено", success=True, device="Коптер", **updates):
    return {
        "flight_id": identifier, "main_purpose": purpose, "main_result": result,
        "is_effective": success, "device_type": device, "date": "2026-09-01",
        "time_range": "2026-09-01T12:00:00", "position": "Позиція", **updates,
    }


class SampleEngine(AnalyticsEngine):
    def __init__(self, rows, events=None):
        self.rows = rows
        self.event_rows = events or []

    def _dataset(self, request):
        return self.rows, self.event_rows, []


def metrics(engine, config=None):
    return {item.key: item for item in engine.overview(filters(), config)}


class PurposeKpiValidationTests(unittest.TestCase):
    def test_empty_rules_preserve_defaults_and_valid_coefficients_include_zero(self):
        self.assertEqual(KpiConfiguration().purpose_rules, [])
        self.assertEqual(rule().usefulness_percent, 100)
        self.assertEqual(rule().coefficient, 1)
        self.assertEqual(rule(coefficient=0).coefficient, 0)

    def test_invalid_ranges_types_nonfinite_and_code_fields_are_rejected(self):
        for updates in (
            {"usefulness_percent": -1}, {"usefulness_percent": 101},
            {"coefficient": -1}, {"coefficient": 101},
            {"coefficient": math.inf}, {"usefulness_percent": math.nan},
            {"coefficient": "1.5"}, {"coefficient": True},
            {"expression": "eval(code)"}, {"purpose": "   "},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                PurposeKpiRule.model_validate({"purpose": "Розвідка", **updates})

    def test_individual_purposes_and_results_unique_after_normalization(self):
        with self.assertRaises(ValidationError):
            KpiConfiguration(purpose_rules=[rule("  Повторна   розвідка "), rule("повторна розвідка")])
        for updates in (
            {"success_mode": "results", "successful_results": []},
            {"success_mode": "results", "successful_results": ["Виявлено", " вИяВлЕнО "]},
            {"successful_results": ["Виявлено"]},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                rule(**updates)

    def test_fingerprint_is_semantic_and_changes_for_weights_or_criteria(self):
        first = KpiConfiguration(purpose_rules=[rule("А", success_mode="results", successful_results=["X", " Y "]), rule("Б")])
        second = KpiConfiguration(purpose_rules=[rule("б"), rule(" а ", success_mode="results", successful_results=["y", "x"])])
        self.assertEqual(kpi_fingerprint(first), kpi_fingerprint(second))
        changed = second.model_copy(deep=True)
        changed.purpose_rules[0].coefficient = 2
        self.assertNotEqual(kpi_fingerprint(first), kpi_fingerprint(changed))
        default = KpiConfiguration(purpose_rules=[rule()])
        explicit = KpiConfiguration(purpose_rules=[rule(usefulness_percent=100, coefficient=1)])
        self.assertEqual(kpi_fingerprint(default), kpi_fingerprint(explicit))


class PurposeKpiArithmeticTests(unittest.TestCase):
    def test_legacy_defaults_and_duplicate_flights(self):
        rows = [flight("1"), flight("1"), flight("2", success=False)]
        engine = SampleEngine(rows)
        result = metrics(engine)
        self.assertEqual(result["flights"].value, 2)
        self.assertEqual(result["effective"].value, 1)
        self.assertEqual(result["efficiency"].value, 50)
        self.assertEqual(result["weighted_efficiency"].value, 50)
        preview = engine.kpi_preview(filters(), KpiConfiguration())
        self.assertEqual(preview.total_flights, 2)
        self.assertEqual(preview.weighted_flights, 2)

    def test_distinct_purposes_use_weighted_denominator_and_full_aggregate(self):
        rows = [flight("1", "Розвідка"), flight("2", "Розвідка", success=False), flight("3", "Логістика")]
        config = KpiConfiguration(purpose_rules=[rule(usefulness_percent=50, coefficient=2), rule("Логістика", usefulness_percent=100, coefficient=4)])
        engine = SampleEngine(rows)
        preview = engine.kpi_preview(filters(), config)
        self.assertEqual((preview.usefulness_points, preview.weighted_flights), (5, 8))
        self.assertEqual(preview.weighted_efficiency, 62.5)
        result = metrics(engine, config)["weighted_efficiency"]
        self.assertEqual(result.value, 62.5)
        self.assertEqual((result.breakdown[0].numerator, result.breakdown[0].denominator), (5, 8))
        self.assertEqual((result.numerator, result.denominator), (5, 8))
        self.assertEqual(sorted((row.numerator, row.denominator) for row in result.breakdown[0].children), [(1, 4), (4, 4)])

    def test_results_rule_uses_main_result_not_event_rows_or_source_success_flag(self):
        rows = [flight("1", result=" ДОСТАВЛЕНО ", success=False), flight("2", result="Не доставлено", success=True)]
        events = [{"flight_id": "2", "result": "Доставлено"}] * 20000
        config = KpiConfiguration(purpose_rules=[rule(success_mode="results", successful_results=["доставлено"])])
        result = metrics(SampleEngine(rows, events), config)
        self.assertEqual(result["effective"].value, 1)
        self.assertEqual(result["weighted_efficiency"].value, 50)
        effective_rows = SampleEngine(rows).timeline(filters(), config)
        self.assertEqual(effective_rows[0].effective, 1)

    def test_normalization_matches_individual_purpose_and_unknown_falls_back(self):
        config = KpiConfiguration(purpose_rules=[rule("Повторна   розвідка", usefulness_percent=20)])
        engine = SampleEngine([flight("1", " ПОВТОРНА розвідка "), flight("2", "Невідома мета")])
        self.assertEqual(metrics(engine, config)["weighted_efficiency"].value, 60)

    def test_coefficient_zero_retains_raw_counts_and_nullable_weighted_rows(self):
        config = KpiConfiguration(purpose_rules=[rule(coefficient=0)])
        engine = SampleEngine([flight("1")])
        result = metrics(engine, config)
        self.assertEqual(result["flights"].value, 1)
        self.assertEqual(result["effective"].value, 1)
        self.assertIsNone(result["weighted_efficiency"].value)
        self.assertIsNone(result["weighted_efficiency"].breakdown[0].children[0].value)
        preview = engine.kpi_preview(filters(), config)
        self.assertEqual(preview.weighted_flights, 0)
        self.assertIsNone(preview.weighted_efficiency)

    def test_zero_usefulness_is_visible_and_not_excluded_from_denominator(self):
        config = KpiConfiguration(purpose_rules=[rule(usefulness_percent=0)])
        result = metrics(SampleEngine([flight("1")]), config)["weighted_efficiency"]
        self.assertEqual(result.value, 0)
        self.assertEqual(result.breakdown[0].children[0].value, 0)
        self.assertEqual(result.breakdown[0].denominator, 1)

    def test_empty_window_keeps_configured_purpose_without_invented_zero_ratio(self):
        preview = SampleEngine([]).kpi_preview(filters(), KpiConfiguration(purpose_rules=[rule()]))
        self.assertEqual(preview.total_flights, 0)
        self.assertIsNone(preview.weighted_efficiency)
        self.assertEqual(preview.purposes[0].purpose, "Розвідка")
        self.assertEqual(preview.purposes[0].flights, 0)
        self.assertIsNone(preview.purposes[0].weighted_efficiency)

    def test_day_and_night_success_use_same_configured_criteria(self):
        rows = [
            flight("1", result="Успіх", success=False),
            flight("2", result="Невдача", success=True, time_range="2026-09-01T01:00:00"),
        ]
        config = KpiConfiguration(purpose_rules=[rule(success_mode="results", successful_results=["Успіх"])])
        point = SampleEngine(rows).timeline(filters(), config)[0]
        self.assertEqual((point.total, point.effective), (2, 1))
        self.assertEqual((point.day_effective, point.night_effective), (1, 0))


class PurposeKpiStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "configuration.sqlite3"
        self.store = ApplicationConfigurationStore(self.path)

    def test_old_published_draft_and_history_json_load_safe_defaults(self):
        seed = self.store.snapshot().config.model_dump()
        seed.pop("kpi")
        import json
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE configuration_versions SET config_json=?", (json.dumps(seed),))
            connection.execute("INSERT INTO configuration_draft VALUES(1,?,1)", (json.dumps(seed),))
            connection.commit()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.config.kpi.purpose_rules, [])
        self.assertEqual(snapshot.draft.kpi.purpose_rules, [])
        published = self.store.publish(1, 0)
        self.assertEqual(published.config.kpi.purpose_rules, [])
        restored = self.store.rollback(1, 2)
        self.assertEqual(restored.config.kpi.purpose_rules, [])

    def test_draft_preview_does_not_change_published_snapshot_publish_and_rollback_do(self):
        config = self.store.snapshot().config
        config.kpi = KpiConfiguration(purpose_rules=[rule(usefulness_percent=30)])
        with patch("app.core.app_configuration.get_configuration_store", return_value=self.store):
            before = current_kpi_snapshot()
            self.store.preview(config, 1)
            saved = self.store.save_draft(config, 1, 0)
            self.assertEqual(current_kpi_snapshot().fingerprint, before.fingerprint)
            self.store.publish(1, saved.draft_version)
            self.assertNotEqual(current_kpi_snapshot().fingerprint, before.fingerprint)
            self.store.rollback(1, 2)
            self.assertEqual(current_kpi_snapshot().fingerprint, before.fingerprint)


class PurposeKpiCacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = MemoryCache()
        cfg = SimpleNamespace(enabled=True, max_entries=128, stale_if_error_seconds=900, stale_while_revalidate=True, copy_on_read=False, copy_on_write=False)
        self.cache._config = lambda: cfg
        for target in ("app.services.analytics.cache", "app.services.comparison.cache"):
            patched = patch(target, self.cache)
            patched.start()
            self.addCleanup(patched.stop)
        self.first = KpiSnapshot(KpiConfiguration(), kpi_fingerprint(KpiConfiguration()), 1)
        changed = KpiConfiguration(purpose_rules=[rule(usefulness_percent=20)])
        self.second = KpiSnapshot(changed, kpi_fingerprint(changed), 2)
        patched = patch("app.services.analytics._source", return_value="clickhouse")
        patched.start()
        self.addCleanup(patched.stop)

    def test_overview_and_timeline_cache_change_immediately_with_rule_fingerprint(self):
        engine = SampleEngine([flight("1")])
        with patch.object(analytics.clickhouse_analytics, "overview", side_effect=engine.overview) as overview, patch.object(analytics.clickhouse_analytics, "timeline", side_effect=engine.timeline) as timeline:
            with pin_kpi_snapshot(self.first):
                first = analytics.overview(filters())
                analytics.overview(filters())
                analytics.timeline(filters())
                analytics.timeline(filters())
            with pin_kpi_snapshot(self.second):
                second = analytics.overview(filters())
                analytics.timeline(filters())
            self.assertEqual(overview.call_count, 2)
            self.assertEqual(timeline.call_count, 2)
        self.assertEqual(first[-1].value, 100)
        self.assertEqual(second[-1].value, 20)

    def test_comparison_pins_rules_across_concurrent_publication_and_caches_revision(self):
        selected_snapshot = [self.first]
        observed = []

        def load_overview(request):
            observed.append(current_kpi_snapshot().fingerprint)
            selected_snapshot[0] = self.second
            return SampleEngine([flight("1")]).overview(request, current_kpi_snapshot().config)

        with patch("app.services.comparison.current_kpi_snapshot", side_effect=lambda: selected_snapshot[0]), patch("app.services.comparison.analytics.overview", side_effect=load_overview):
            request = ComparisonRequest(mode="units", filters=filters(), units=["А", "Б"])
            first = comparison.compare(request)
            second = comparison.compare(request)
            third = comparison.compare(request)
        self.assertEqual(observed, [self.first.fingerprint, self.first.fingerprint, self.second.fingerprint, self.second.fingerprint])
        self.assertEqual(first.kpi_fingerprint, self.first.fingerprint)
        self.assertEqual(first.configuration_revision, 1)
        self.assertIn(first.source, {"mock", "clickhouse"})
        self.assertEqual(first.source, second.source)
        self.assertEqual(first.kpi_configuration, self.first.config)
        self.assertEqual(second.kpi_configuration, self.second.config)
        self.assertEqual(second.kpi_fingerprint, self.second.fingerprint)
        self.assertEqual(second, third)
        # An exported result must retain the applied rules after another edit.
        self.second.config.purpose_rules[0].usefulness_percent = 90
        self.assertEqual(second.kpi_configuration.purpose_rules[0].usefulness_percent, 20)

    def test_raw_dataset_cache_reuses_same_rows_when_rules_change(self):
        adapter = ClickHouseAnalytics()
        with patch("app.services.clickhouse_analytics.cache", self.cache), patch.object(adapter, "_dataset_uncached", return_value=([flight("1")], [], [])) as loader:
            self.assertEqual(adapter.overview(filters(), self.first.config)[-1].value, 100)
            self.assertEqual(adapter.overview(filters(), self.second.config)[-1].value, 20)
            self.assertEqual(loader.call_count, 1)


class PurposeKpiRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(router, prefix="/api")
        cls.client = TestClient(app)

    def test_readonly_preview_contract_with_real_engine_has_no_configuration_write(self):
        engine = SampleEngine([flight("1"), flight("1")])
        with patch("app.services.analytics._source", return_value="clickhouse"), patch.object(analytics.clickhouse_analytics, "kpi_preview", side_effect=engine.kpi_preview), patch("app.core.app_configuration.get_configuration_store") as store:
            response = self.client.post("/api/analytics/kpi-preview", json={"filters": filters().model_dump(mode="json"), "kpi": {"purpose_rules": [{"purpose": "Розвідка", "usefulness_percent": 40, "coefficient": 2}]}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["weighted_efficiency"], 40)
            self.assertEqual(response.json()["total_flights"], 1)
            self.assertEqual(response.json()["purposes"][0]["success_mode"], "source")
            store.assert_not_called()

    def test_unsupported_sources_are_rejected_for_preview_and_options(self):
        with patch("app.services.analytics._source", return_value="mock"):
            response = self.client.post("/api/analytics/kpi-preview", json={"filters": filters().model_dump(mode="json"), "kpi": {"purpose_rules": []}})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(self.client.get("/api/analytics/kpi-options").status_code, 422)

    def test_invalid_preview_rule_returns_validation_error(self):
        response = self.client.post("/api/analytics/kpi-preview", json={"filters": filters().model_dump(mode="json"), "kpi": {"purpose_rules": [{"purpose": "Розвідка", "coefficient": 1000000}]}})
        self.assertEqual(response.status_code, 422)

    def test_kpi_options_read_main_flight_results_not_event_results(self):
        adapter = ClickHouseAnalytics()
        with patch.object(adapter, "_distinct_strings", side_effect=lambda entity, key: [f"{entity}.{key}"]) as distinct:
            values = adapter.kpi_options()
        self.assertEqual(values["purposes"], ["flight_list.main_purpose"])
        self.assertEqual(values["results"], ["flight_list.main_result"])
        self.assertEqual([call.args for call in distinct.call_args_list], [("flight_list", "main_purpose"), ("flight_list", "main_result")])

    def test_missing_flight_result_field_cannot_be_reported_as_zero_success(self):
        adapter = ClickHouseAnalytics()
        config = KpiConfiguration(purpose_rules=[rule(success_mode="results", successful_results=["Успіх"])])
        with patch.object(adapter, "_physical", return_value=None), patch.object(adapter, "_dataset", return_value=([flight("1")], [], [])) as dataset:
            for calculate in (
                lambda: adapter.overview(filters(), config),
                lambda: adapter.timeline(filters(), config),
                lambda: adapter.kpi_preview(filters(), config),
            ):
                with self.assertRaisesRegex(KpiDataUnavailable, "main_result"):
                    calculate()
            dataset.assert_not_called()
            self.assertEqual(adapter.overview(filters(), KpiConfiguration())[-1].value, 100)

    def test_missing_result_field_preview_returns_explicit_validation_error(self):
        with patch("app.services.analytics._source", return_value="clickhouse"), patch.object(analytics.clickhouse_analytics, "kpi_preview", side_effect=KpiDataUnavailable("Flight main_result is unavailable")):
            response = self.client.post("/api/analytics/kpi-preview", json={"filters": filters().model_dump(mode="json"), "kpi": {"purpose_rules": []}})
        self.assertEqual(response.status_code, 422)
        self.assertIn("main_result", response.json()["detail"])

    def test_compatibility_timeline_loader_includes_per_purpose_source_fields(self):
        adapter = ClickHouseAnalytics()
        with patch.object(adapter, "_query_rows", return_value=[]) as query, patch.object(adapter, "_flight_where"):
            adapter._timeline_flights(filters())
        selected = query.call_args.args[1]
        self.assertIn("main_purpose", selected)
        self.assertIn("main_result", selected)


if __name__ == "__main__":
    unittest.main()
