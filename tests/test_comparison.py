import unittest
import threading
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.models import ComparisonRequest, FilterRequest, Metric
from app.api.routes import router
from app.core.cache import MemoryCache
from app.services.comparison import (
    ComparisonValidationError,
    compare,
    comparison_periods,
    previous_period_filters,
)
from app.services.clickhouse_analytics import ClickHouseAnalytics


def sample_filters(**updates):
    values = {
        "date_from": "2026-09-01",
        "date_to": "2026-09-07",
        "direction": ["test-direction"],
        "unit": ["existing-unit"],
        "category": ["test-category"],
        "asset": ["test-asset"],
        "group": ["test-group"],
        "bbak": ["1"],
        "rota": ["test-rota"],
        "battalion": ["2"],
        "purpose": ["test-purpose"],
        "class_name": ["test-class"],
        "result": ["test-result"],
    }
    return FilterRequest.model_validate({**values, **updates})


def bounds(filters):
    return (
        datetime.combine(filters.date_from, filters.time_from or time.min),
        datetime.combine(filters.date_to, filters.time_to or time.max),
    )


class ComparisonCacheIsolation:
    def setUp(self):
        super().setUp()
        self.comparison_cache = MemoryCache()
        self.cache_config = SimpleNamespace(
            enabled=True, max_entries=128, stale_if_error_seconds=900,
            stale_while_revalidate=True, copy_on_read=False, copy_on_write=False,
        )
        self.comparison_cache._config = lambda: self.cache_config
        cache_patch = patch("app.services.comparison.cache", self.comparison_cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)


class PreviousPeriodTests(unittest.TestCase):
    def test_calendar_period_keeps_days_and_all_nonperiod_filters(self):
        current = sample_filters(date_from="2025-01-01", date_to="2025-01-07")
        previous = previous_period_filters(current)
        self.assertEqual(previous.date_from, date(2024, 12, 25))
        self.assertEqual(previous.date_to, date(2024, 12, 31))
        self.assertIsNone(previous.time_from)
        self.assertIsNone(previous.time_to)
        expected = current.model_dump()
        expected.update(date_from=previous.date_from, date_to=previous.date_to)
        self.assertEqual(previous.model_dump(), expected)
        previous.direction.append("another-direction")
        self.assertEqual(current.direction, ["test-direction"])

    def test_single_day_uses_leap_day(self):
        previous = previous_period_filters(
            sample_filters(date_from="2024-03-01", date_to="2024-03-01")
        )
        self.assertEqual(previous.date_from, date(2024, 2, 29))
        self.assertEqual(previous.date_to, date(2024, 2, 29))

    def test_time_period_is_adjacent_at_source_millisecond_precision(self):
        current = sample_filters(
            date_from="2026-09-01", date_to="2026-09-02",
            time_from="09:15:00.125", time_to="11:45:00.500",
        )
        previous = previous_period_filters(current)
        start, end = bounds(current)
        previous_start, previous_end = bounds(previous)
        self.assertEqual(start - previous_end, timedelta(milliseconds=1))
        self.assertEqual(end - start, previous_end - previous_start)
        self.assertEqual(previous.time_from.microsecond % 1000, 0)
        self.assertEqual(previous.time_to.microsecond % 1000, 0)

    def test_partial_time_bounds_use_midnight_and_inclusive_end_of_day(self):
        for overrides in ({"time_from": "12:00"}, {"time_to": "12:00"}):
            with self.subTest(overrides=overrides):
                current = sample_filters(date_to="2026-09-01", **overrides)
                previous = previous_period_filters(current)
                start, end = bounds(current)
                start = start.replace(microsecond=start.microsecond // 1000 * 1000)
                end = end.replace(microsecond=end.microsecond // 1000 * 1000)
                previous_start, previous_end = bounds(previous)
                self.assertEqual(start - previous_end, timedelta(milliseconds=1))
                self.assertEqual(end - start, previous_end - previous_start)

    def test_equal_timestamp_is_one_millisecond_without_overlap(self):
        current = sample_filters(
            date_to="2026-09-01", time_from="00:00", time_to="00:00",
        )
        previous = previous_period_filters(current)
        self.assertEqual(bounds(previous), (
            datetime(2026, 8, 31, 23, 59, 59, 999000),
            datetime(2026, 8, 31, 23, 59, 59, 999000),
        ))

    def test_submillisecond_bounds_follow_clickhouse_query_precision(self):
        current = sample_filters(
            date_to="2026-09-01",
            time_from="09:00:00.000100", time_to="10:00:00.000900",
        )
        previous = previous_period_filters(current)
        previous_start, previous_end = bounds(previous)
        self.assertEqual(previous_start, datetime(2026, 9, 1, 7, 59, 59, 999000))
        self.assertEqual(previous_end, datetime(2026, 9, 1, 8, 59, 59, 999000))

    def test_date_underflow_and_extreme_span_are_validation_errors(self):
        for overrides in (
            {"date_from": "0001-01-01", "date_to": "0001-01-01"},
            {"date_from": "0001-01-02", "date_to": "9999-12-31"},
            {"date_from": "0001-01-01", "date_to": "0001-01-01", "time_from": "00:00"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ComparisonValidationError, "comparison_filters"):
                    previous_period_filters(sample_filters(**overrides))


class ComparisonServiceTests(ComparisonCacheIsolation, unittest.TestCase):
    @patch("app.services.comparison.analytics.overview")
    def test_custom_period_preserves_both_filter_sets_and_complete_metrics(self, overview):
        overview.return_value = [
            Metric(key="total_flights", label="Flights", value=123456),
            Metric(key="avg_daily_flights", label="Average", value=17.5),
        ]
        current = sample_filters()
        custom = sample_filters(
            date_from="2025-08-01", date_to="2025-08-30", direction=["other-direction"],
        )
        response = compare(ComparisonRequest(
            mode="periods", filters=current, comparison_filters=custom,
        ))
        self.assertEqual(response.baseline_id, "previous")
        self.assertEqual([selection.id for selection in response.selections], ["current", "previous"])
        self.assertEqual(response.selections[0].filters, current)
        self.assertEqual(response.selections[1].filters, custom)
        self.assertEqual(response.selections[0].metrics, overview.return_value)
        self.assertEqual(response.selections[1].metrics, overview.return_value)
        self.assertEqual([call.args[0] for call in overview.call_args_list], [current, custom])

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_unit_comparison_replaces_only_unit_and_uses_first_baseline(self, overview):
        current = sample_filters(time_from="08:30", time_to="17:30")
        response = compare(ComparisonRequest(
            mode="units", filters=current, units=[" second-unit ", "first-unit"],
        ))
        self.assertEqual(response.baseline_id, "unit:second-unit")
        self.assertEqual([selection.label for selection in response.selections], ["second-unit", "first-unit"])
        self.assertEqual(len(overview.call_args_list), 2)
        for selection, unit in zip(response.selections, ["second-unit", "first-unit"]):
            expected = current.model_dump()
            expected["unit"] = [unit]
            self.assertEqual(selection.filters.model_dump(), expected)
        self.assertEqual(current.unit, ["existing-unit"])
        response.selections[0].filters.direction.append("new-direction")
        self.assertEqual(current.direction, ["test-direction"])
        self.assertEqual(response.selections[1].filters.direction, ["test-direction"])

    @patch("app.services.comparison.analytics.options")
    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_org_comparison_uses_stable_ids_and_titles_with_id_fallback(self, overview, options):
        options.return_value = {
            "bbak": [{"id": 42, "title": "Test organization"}],
            "battalion": [{"id": 42, "title": "Test battalion"}],
        }
        current = sample_filters()
        for dimension in ("bbak", "battalion"):
            with self.subTest(dimension=dimension):
                response = compare(ComparisonRequest(
                    mode="units", dimension=dimension, filters=current, units=["042", "99"],
                ))
                self.assertEqual(response.dimension, dimension)
                self.assertEqual(response.baseline_id, f"{dimension}:42")
                expected_title = options.return_value[dimension][0]["title"]
                self.assertEqual([row.label for row in response.selections], [expected_title, "99"])
                for selection, selected_id in zip(response.selections, ["42", "99"]):
                    expected = current.model_dump()
                    expected[dimension] = [selected_id]
                    self.assertEqual(selection.filters.model_dump(), expected)
                self.assertEqual(current.bbak, ["1"])
                self.assertEqual(current.battalion, ["2"])

    @patch("app.services.comparison.analytics.options")
    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_rota_comparison_keeps_other_organization_filters(self, overview, options):
        current = sample_filters()
        response = compare(ComparisonRequest(
            mode="units", dimension="rota", filters=current, units=["Rota A", "Rota B"],
        ))
        self.assertEqual(response.baseline_id, "rota:Rota A")
        for selection in response.selections:
            self.assertEqual(selection.filters.bbak, current.bbak)
            self.assertEqual(selection.filters.battalion, current.battalion)
            self.assertEqual(selection.filters.unit, current.unit)
        self.assertEqual(response.selections[0].filters.rota, ["Rota A"])
        options.assert_not_called()

    def test_invalid_modes_unit_counts_duplicates_and_combinations(self):
        invalid = [
            {"mode": "unknown"},
            {"mode": "units"},
            {"mode": "units", "units": ["only-one"]},
            {"mode": "units", "units": [str(index) for index in range(7)]},
            {"mode": "units", "units": ["duplicate", " duplicate "]},
            {"mode": "units", "units": ["a", " "]},
            {"mode": "periods", "units": ["a", "b"]},
            {"mode": "units", "units": ["a", "b"], "comparison_filters": sample_filters()},
            {"mode": "units", "dimension": "invalid", "units": ["a", "b"]},
            {"mode": "units", "dimension": "bbak", "units": ["title A", "title B"]},
            {"mode": "units", "dimension": "bbak", "units": ["1", "01"]},
            {"mode": "units", "dimension": "battalion", "units": ["1", str(2**63)]},
            {"mode": "units_over_time"},
            {"mode": "units_over_time", "units": ["a", "b"], "granularity": "year"},
            {"mode": "units_over_time", "units": ["a", "b"], "comparison_filters": sample_filters()},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    ComparisonRequest(filters=sample_filters(), **fields)


class ComparisonRouteTests(ComparisonCacheIsolation, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(router, prefix="/api")
        cls.client = TestClient(app)

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_endpoint_returns_comparison_contract(self, overview):
        response = self.client.post("/api/analytics/comparison", json={
            "mode": "periods", "filters": sample_filters().model_dump(mode="json"),
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "periods")
        self.assertEqual(body["baseline_id"], "previous")
        self.assertEqual(body["selections"][1]["filters"]["date_from"], "2026-08-25")
        self.assertEqual(overview.call_count, 2)

    @patch("app.services.comparison.analytics.overview")
    def test_underflow_returns_422_before_source_access(self, overview):
        response = self.client.post("/api/analytics/comparison", json={
            "mode": "periods",
            "filters": sample_filters(date_from="0001-01-01", date_to="0001-01-01").model_dump(mode="json"),
        })
        self.assertEqual(response.status_code, 422)
        self.assertIn("comparison_filters", response.json()["detail"])
        overview.assert_not_called()

    @patch("app.services.comparison.analytics.overview")
    def test_invalid_request_returns_422_before_source_access(self, overview):
        for fields in (
            {"mode": "units", "units": ["same", "same"]},
            {"mode": "unsupported"},
            {"mode": "periods", "filters": {"date_from": "2026-09-07", "date_to": "2026-09-01"}},
            {"mode": "units_over_time", "units": ["a", "b"], "filters": {
                "date_from": "2026-09-01", "date_to": "2026-09-07", "time_from": "08:00+03:00",
            }},
        ):
            with self.subTest(fields=fields):
                response = self.client.post("/api/analytics/comparison", json={
                    "filters": sample_filters().model_dump(mode="json"), **fields,
                })
                self.assertEqual(response.status_code, 422)
        overview.assert_not_called()

    @patch("app.api.routes.logger.exception")
    @patch("app.services.comparison.analytics.overview")
    def test_upstream_failure_is_503_without_internal_details(self, overview, log):
        for error in (RuntimeError("private SQL"), ValueError("private parse detail")):
            with self.subTest(error=error):
                overview.side_effect = error
                response = self.client.post("/api/analytics/comparison", json={
                    "mode": "periods", "filters": sample_filters().model_dump(mode="json"),
                })
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private", response.text)
        self.assertEqual(log.call_count, 2)


class CalendarPeriodTests(unittest.TestCase):
    def setUp(self):
        clock = patch("app.services.comparison._reporting_date", return_value=date(2026, 9, 30))
        clock.start()
        self.addCleanup(clock.stop)

    def test_monday_weeks_clip_at_both_ends_across_year_boundary(self):
        periods = comparison_periods(
            sample_filters(date_from="2025-12-31", date_to="2026-01-13"), "week", 2,
        )
        self.assertEqual([row.id for row in periods], [
            "week:2025-12-29", "week:2026-01-05", "week:2026-01-12",
        ])
        self.assertEqual([(row.date_from, row.date_to) for row in periods], [
            (date(2025, 12, 31), date(2026, 1, 4)),
            (date(2026, 1, 5), date(2026, 1, 11)),
            (date(2026, 1, 12), date(2026, 1, 13)),
        ])
        self.assertEqual([row.is_partial for row in periods], [True, False, True])
        shifted = comparison_periods(
            sample_filters(date_from="2026-01-01", date_to="2026-01-04"), "week", 2,
        )
        self.assertEqual(shifted[0].id, periods[0].id)
        self.assertEqual(shifted[0].label, periods[0].label)

    def test_calendar_months_include_leap_day_without_duration_assumptions(self):
        periods = comparison_periods(
            sample_filters(date_from="2024-01-31", date_to="2024-04-01"), "month", 2,
        )
        self.assertEqual([row.label for row in periods], ["2024-01", "2024-02", "2024-03", "2024-04"])
        self.assertEqual(periods[1].date_from, date(2024, 2, 1))
        self.assertEqual(periods[1].date_to, date(2024, 2, 29))
        self.assertEqual([row.is_partial for row in periods], [True, False, False, True])

    def test_quarters_follow_calendar_year_and_clip_to_requested_range(self):
        periods = comparison_periods(
            sample_filters(date_from="2024-11-12", date_to="2025-04-15"), "quarter", 2,
        )
        self.assertEqual([row.id for row in periods], [
            "quarter:2024-10-01", "quarter:2025-01-01", "quarter:2025-04-01",
        ])
        self.assertEqual([row.label for row in periods], ["Q4 2024", "Q1 2025", "Q2 2025"])
        self.assertEqual(periods[1].date_to, date(2025, 3, 31))
        self.assertEqual([row.is_partial for row in periods], [True, False, True])

    def test_explicit_times_apply_only_to_overall_endpoints(self):
        periods = comparison_periods(sample_filters(
            date_from="2026-09-01", date_to="2026-09-03", time_from="12:30", time_to="04:15",
        ), "day", 2)
        self.assertEqual([row.time_from for row in periods], [time(12, 30), None, None])
        self.assertEqual([row.time_to for row in periods], [None, None, time(4, 15)])
        self.assertEqual([row.is_partial for row in periods], [True, False, True])
        self.assertEqual([row.date_from for row in periods], [
            date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3),
        ])

    def test_full_day_source_precision_endpoints_are_not_partial(self):
        periods = comparison_periods(sample_filters(
            date_from="2026-09-01", date_to="2026-09-01",
            time_from="00:00", time_to="23:59:59.999",
        ), "day", 2)
        self.assertFalse(periods[0].is_partial)

    def test_current_and_future_flags_are_independent_of_requested_clipping(self):
        periods = comparison_periods(
            sample_filters(date_from="2026-08-01", date_to="2026-10-31"), "month", 2,
        )
        self.assertEqual([row.is_partial for row in periods], [False, False, False])
        self.assertEqual([row.is_current for row in periods], [False, True, False])
        self.assertEqual([row.is_future for row in periods], [False, False, True])

    def test_extreme_calendar_end_returns_validation_error_instead_of_overflow(self):
        with self.assertRaisesRegex(ComparisonValidationError, "supported date range"):
            comparison_periods(sample_filters(
                date_from="9999-12-31", date_to="9999-12-31",
            ), "week", 2)
        for granularity in ("day", "month", "quarter"):
            with self.subTest(granularity=granularity):
                periods = comparison_periods(sample_filters(
                    date_from="9999-12-31", date_to="9999-12-31",
                ), granularity, 2)
                self.assertEqual(periods[0].date_to, date.max)


class TemporalComparisonTests(ComparisonCacheIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        clock = patch("app.services.comparison._reporting_date", return_value=date(2026, 9, 30))
        clock.start()
        self.addCleanup(clock.stop)

    @patch("app.services.comparison.analytics.options")
    @patch("app.services.comparison.analytics.overview")
    def test_period_group_matrix_preserves_filters_and_complete_metrics(self, overview, options):
        options.return_value = {"bbak": [{"id": 42, "title": "Organization A"}]}
        overview.side_effect = lambda filters: [
            Metric(key="total_flights", label="Flights", value=int(filters.bbak[0]) + filters.date_from.month),
            Metric(key="avg_daily_flights", label="Daily", value=7.5),
        ]
        current = sample_filters(
            date_from="2026-07-02", date_to="2026-09-20", time_from="08:00", time_to="17:30",
        )
        response = compare(ComparisonRequest(
            mode="units_over_time", dimension="bbak", granularity="month", filters=current, units=["42", "99"],
        ))
        self.assertEqual(response.mode, "units_over_time")
        self.assertEqual(response.granularity, "month")
        self.assertEqual(response.baseline_id, "month:2026-07-01/bbak:42")
        self.assertEqual(len(response.selections), 6)
        self.assertEqual(overview.call_count, 6)
        options.assert_called_once_with()
        self.assertEqual([row.group_id for row in response.selections], ["bbak:42", "bbak:99"] * 3)
        for index, selection in enumerate(response.selections):
            period = response.periods[index // 2]
            unit = ["42", "99"][index % 2]
            expected = current.model_dump()
            expected.update(
                bbak=[unit], date_from=period.date_from, date_to=period.date_to,
                time_from=period.time_from, time_to=period.time_to,
            )
            self.assertEqual(selection.filters.model_dump(), expected)
            self.assertEqual(selection.period_id, period.id)
            self.assertEqual(selection.id, f"{period.id}/bbak:{unit}")
            self.assertEqual(selection.label, {"42": "Organization A", "99": "99"}[unit])
            self.assertEqual([metric.key for metric in selection.metrics], ["total_flights", "avg_daily_flights"])
        self.assertEqual(current.bbak, ["1"])
        response.selections[0].filters.asset.append("mutated")
        self.assertEqual(current.asset, ["test-asset"])
        self.assertEqual(response.selections[1].filters.asset, ["test-asset"])

    @patch("app.services.comparison.analytics.options")
    @patch("app.services.comparison.analytics.overview")
    def test_workload_limits_reject_before_options_or_source_access(self, overview, options):
        for date_to, units in (
            ("2026-09-25", ["1", "2"]),
            ("2026-09-13", [str(index) for index in range(6)]),
            ("9999-12-31", ["1", "2"]),
        ):
            with self.subTest(date_to=date_to, units=units):
                request = ComparisonRequest(
                    mode="units_over_time", dimension="bbak", granularity="day", units=units,
                    filters=sample_filters(date_to=date_to),
                )
                with self.assertRaisesRegex(ComparisonValidationError, "24 periods.*72"):
                    compare(request)
        overview.assert_not_called()
        options.assert_not_called()

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_maximum_cell_and_period_boundaries_are_accepted(self, overview):
        for date_to, units, count in (
            ("2026-09-12", [str(index) for index in range(6)], 72),
            ("2026-09-24", ["A", "B", "C"], 72),
        ):
            with self.subTest(date_to=date_to, units=units):
                response = compare(ComparisonRequest(
                    mode="units_over_time", granularity="day", units=units,
                    filters=sample_filters(date_to=date_to),
                ))
                self.assertEqual(len(response.selections), count)
                self.assertLessEqual(len(response.periods), 24)
        self.assertEqual(overview.call_count, 144)

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_temporal_api_exposes_calendar_metadata_and_validation_error(self, overview):
        app = FastAPI()
        app.include_router(router, prefix="/api")
        client = TestClient(app)
        request = {
            "mode": "units_over_time", "granularity": "month", "units": ["A", "B"],
            "filters": sample_filters(date_from="2026-08-01", date_to="2026-09-30").model_dump(mode="json"),
        }
        response = client.post("/api/analytics/comparison", json=request)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["granularity"], "month")
        self.assertEqual(body["periods"][1]["id"], "month:2026-09-01")
        self.assertTrue(body["periods"][1]["is_current"])
        self.assertFalse(body["periods"][1]["is_partial"])
        self.assertEqual(body["selections"][0]["group_id"], "unit:A")
        request.update(granularity="day")
        invalid = client.post("/api/analytics/comparison", json=request)
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("24 periods", invalid.json()["detail"])
        self.assertEqual(overview.call_count, 4)


class ComparisonResponseCacheTests(ComparisonCacheIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.reporting_date = date(2026, 9, 30)
        clock = patch("app.services.comparison._reporting_date", side_effect=lambda: self.reporting_date)
        clock.start()
        self.addCleanup(clock.stop)

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_repeated_whole_response_runs_each_selection_once(self, overview):
        for mode, extra, expected_calls in (
            ("periods", {}, 2),
            ("units", {"units": ["A", "B"]}, 2),
            ("units_over_time", {"units": ["A", "B"], "granularity": "month"}, 4),
        ):
            with self.subTest(mode=mode):
                request = ComparisonRequest(
                    mode=mode, filters=sample_filters(date_from="2026-08-01", date_to="2026-09-30"), **extra,
                )
                before = overview.call_count
                first = compare(request)
                second = compare(request)
                self.assertEqual(first, second)
                self.assertEqual(overview.call_count - before, expected_calls)

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_reference_selection_order_changes_response_cache_key(self, overview):
        first = compare(ComparisonRequest(mode="units", filters=sample_filters(), units=["A", "B"]))
        second = compare(ComparisonRequest(mode="units", filters=sample_filters(), units=["B", "A"]))
        self.assertEqual(first.baseline_id, "unit:A")
        self.assertEqual(second.baseline_id, "unit:B")
        self.assertEqual([row.id for row in second.selections], ["unit:B", "unit:A"])
        self.assertEqual(overview.call_count, 4)

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_reporting_date_changes_current_and_future_flags_at_calendar_midnight(self, overview):
        request = ComparisonRequest(
            mode="units_over_time", granularity="month", units=["A", "B"],
            filters=sample_filters(date_from="2026-09-01", date_to="2026-10-31"),
        )
        first = compare(request)
        self.reporting_date = date(2026, 10, 1)
        second = compare(request)
        self.assertEqual([row.is_current for row in first.periods], [True, False])
        self.assertEqual([row.is_current for row in second.periods], [False, True])
        self.assertEqual([row.is_future for row in second.periods], [False, False])
        self.assertEqual(overview.call_count, 8)

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_source_switch_does_not_reuse_other_source_response(self, overview):
        request = ComparisonRequest(mode="units", filters=sample_filters(), units=["A", "B"])
        with patch("app.services.comparison.get_settings", return_value=SimpleNamespace(active_source="mock")):
            compare(request)
        with patch("app.services.comparison.get_settings", return_value=SimpleNamespace(active_source="clickhouse")):
            compare(request)
        self.assertEqual(overview.call_count, 4)

    def test_whole_response_survives_constituent_cache_eviction_at_maximum_cells(self):
        request = ComparisonRequest(
            mode="units_over_time", granularity="day", units=[str(index) for index in range(6)],
            filters=sample_filters(date_from="2026-09-01", date_to="2026-09-12"),
        )
        dataset_calls = []

        def load_overview(filters):
            payload = filters.model_dump_json()

            def load_dataset():
                dataset_calls.append(payload)
                return []

            return self.comparison_cache.get_or_load(
                "api.overview", payload,
                lambda: self.comparison_cache.get_or_load("clickhouse.dataset", payload, load_dataset, ttl_seconds=60),
                ttl_seconds=60,
            )

        with patch("app.services.comparison.analytics.overview", side_effect=load_overview):
            first = compare(request)
            second = compare(request)
        self.assertEqual(len(first.selections), 72)
        self.assertEqual(first, second)
        self.assertEqual(len(dataset_calls), 72)

    def test_concurrent_identical_requests_share_one_complete_response_load(self):
        request = ComparisonRequest(mode="units", filters=sample_filters(), units=["A", "B"])
        callers_ready = threading.Barrier(4)
        loader_started = threading.Event()
        release_loader = threading.Event()
        responses = []
        errors = []

        def overview(filters):
            loader_started.set()
            if not release_loader.wait(timeout=3):
                raise TimeoutError("test loader was not released")
            return []

        def call():
            try:
                callers_ready.wait(timeout=3)
                responses.append(compare(request))
            except BaseException as exc:
                errors.append(exc)

        with patch("app.services.comparison.analytics.overview", side_effect=overview) as source:
            threads = [threading.Thread(target=call, daemon=True) for _ in range(4)]
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(loader_started.wait(timeout=3))
            finally:
                release_loader.set()
            for thread in threads:
                thread.join(timeout=3)
            self.assertFalse(any(thread.is_alive() for thread in threads), "Comparison cache callers did not finish")
            self.assertEqual(errors, [])
            self.assertEqual(source.call_count, 2)
        self.assertEqual(len(responses), 4)
        self.assertTrue(all(response == responses[0] for response in responses))
        self.assertEqual(self.comparison_cache._key_locks, {})

    @patch("app.services.comparison.analytics.overview", return_value=[])
    def test_expired_response_has_no_stale_fallback_on_source_failure(self, overview):
        request = ComparisonRequest(mode="units", filters=sample_filters(), units=["A", "B"])
        compare(request)
        with self.comparison_cache._lock:
            expires_at = max(entry.expires_at for entry in self.comparison_cache._entries.values())
        overview.side_effect = RuntimeError("source unavailable")
        with patch("app.core.cache.time.monotonic", return_value=expires_at + 1):
            with self.assertRaisesRegex(RuntimeError, "source unavailable"):
                compare(request)


class ClickHouseComparisonConsistencyTests(unittest.TestCase):
    def test_event_zone_filter_restricts_flights_events_and_ammunition_together(self):
        adapter = ClickHouseAnalytics()
        filters = sample_filters(unit=["test-zone"], class_name=[], result=[])
        flights = [{"flight_id": "matching"}, {"flight_id": "other-zone"}]
        events = [{"flight_id": "matching"}, {"flight_id": "orphan"}]
        ammunition = [
            {"flight_id": "matching"}, {"flight_id": "other-zone"}, {"flight_id": "orphan"},
        ]
        with (
            patch.object(adapter, "_flights", return_value=flights),
            patch.object(adapter, "_events", return_value=events) as event_loader,
            patch.object(adapter, "_ammunition", return_value=ammunition),
        ):
            filtered_flights, filtered_events, filtered_ammunition = adapter._dataset_uncached(filters)
        self.assertEqual(filtered_flights, [{"flight_id": "matching"}])
        self.assertEqual(filtered_events, [{"flight_id": "matching"}])
        self.assertEqual(filtered_ammunition, [{"flight_id": "matching"}])
        # The full analytics loader is used without the separate event-table cap.
        event_loader.assert_called_once_with(filters)

    def test_organizational_comparison_filters_historical_rows_by_selected_id(self):
        adapter = ClickHouseAnalytics()
        with patch.object(adapter, "_physical", side_effect=lambda entity, key, **kwargs: key):
            for dimension in ("bbak", "battalion"):
                with self.subTest(dimension=dimension):
                    filters = sample_filters(bbak=[], battalion=[], rota=[])
                    filters = filters.model_copy(update={dimension: ["42"]})
                    where = adapter._flight_where(filters)
                    self.assertIn("toInt64(`bbak_id`)", where.sql())
                    self.assertIn(42, where.params.values())
                    self.assertNotIn("bbak_title", where.sql())


if __name__ == "__main__":
    unittest.main()
