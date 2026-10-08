import copy
import json
import unittest
from dataclasses import replace
from datetime import date
from unittest.mock import patch

from app.bi.engine import QueryEngine, QueryError, QueryPermissionError, load_clickhouse_rows
from app.bi.formula import FormulaError, evaluate_formula, validate_formula
from app.bi.models import DataScope, KpiDefinition, MetricDefinition, QueryRequest
from app.bi.security import Principal
from app.core.cache import cache


class DefinitionStore:
    def __init__(self, metrics=None, kpis=None, weights=None):
        self.entities = {"metrics": metrics or [], "kpis": kpis or [], "category_weights": weights or []}

    def list(self, entity):
        return self.entities.get(entity, [])


def metric(key, aggregation="COUNT_DISTINCT", **values):
    return MetricDefinition(key=key, title=key, aggregation=aggregation, **({"field": "flight_id"} if aggregation == "COUNT_DISTINCT" else {}), **values)


def request(keys=None, **values):
    return QueryRequest.model_validate({"metrics": [{"key": key} for key in keys or ["flights"]], "date_range": {"from": "2026-09-01", "to": "2026-09-30"}, **values})


class FormulaTests(unittest.TestCase):
    def test_json_arithmetic_null_and_zero_are_safe(self):
        expression = {"op": "div", "args": [{"op": "add", "args": [{"metric": "a"}, 3]}, {"metric": "b"}]}
        self.assertEqual(evaluate_formula(expression, {"a": 1, "b": 2}.get), 2)
        self.assertIsNone(evaluate_formula(expression, {"a": 1, "b": 0}.get))
        self.assertIsNone(evaluate_formula(expression, {"a": None, "b": 2}.get))

    def test_coalesce_and_lower_is_better_normalization(self):
        self.assertEqual(evaluate_formula({"op": "coalesce", "args": [{"metric": "missing"}, 0]}, {}.get), 0)
        expression = {"op": "normalize", "args": [25, 0, 100], "direction": "LOWER_IS_BETTER"}
        self.assertEqual(evaluate_formula(expression, {}.get), .75)

    def test_rejects_code_extra_properties_nonfinite_and_unbounded_trees(self):
        for expression in ["__import__('os').system('x')", {"op": "pow", "args": [2, 10000]}, {"op": "add", "args": [1, 2], "python": "x"}, float("nan"), {"value": float("inf")}, True]:
            with self.subTest(expression=expression), self.assertRaises(FormulaError):
                validate_formula(expression)
        nested = {"metric": "flights"}
        for _ in range(18):
            nested = {"op": "abs", "args": [nested]}
        with self.assertRaises(FormulaError):
            validate_formula(nested)


class QueryEngineTests(unittest.TestCase):
    def setUp(self):
        cache.clear("bi.query")
        self.principal = Principal("one", "viewer", frozenset({"*"}), DataScope(scope_type="DEPARTMENT", scope_ids=["1"]), "ROUND_5")
        self.dataset = {
            "flights": [
                {"flight_id": "a", "date": "2026-09-01", "bbak_id": 1, "rota_id": 11, "crew": "red", "device_type": "A", "is_effective": True},
                {"flight_id": "b", "date": "2026-09-08", "bbak_id": 1, "rota_id": 11, "crew": "red", "device_type": "special", "is_effective": False},
                {"flight_id": "c", "date": "2026-09-09", "bbak_id": 1, "rota_id": 12, "crew": "blue", "device_type": "B", "is_effective": True},
                {"flight_id": "d", "date": "2026-09-09", "bbak_id": 2, "rota_id": 21, "crew": "yellow", "device_type": "A", "is_effective": True},
            ],
            "events": [
                {"flight_id": "a", "date": "2026-09-01", "result": "positive", "target_class": "A", "field_200": 2},
                {"flight_id": "b", "date": "2026-09-08", "result": "negative", "target_class": "special", "field_200": 1},
                {"flight_id": "c", "date": "2026-09-09", "result": "positive", "target_class": "B", "field_200": 4},
                {"flight_id": "d", "date": "2026-09-09", "result": "positive", "target_class": "A", "field_200": 200},
            ],
            "ammunition": [{"flight_id": "a", "date": "2026-09-01", "bc_count": 5}, {"flight_id": "d", "date": "2026-09-09", "bc_count": 100}],
            "lost_devices": [],
        }
        self.calls = []

        def loader(start, end):
            self.calls.append((start, end))
            return copy.deepcopy(self.dataset)

        self.store = DefinitionStore([
            metric("flights"),
            metric("effective", "COUNT", filters=[{"field": "is_effective", "operator": "eq", "value": True}]),
            metric("efficiency", "RATIO", numerator="effective", denominator="flights", format={"type": "percent"}, percentage_difference="percentage_points"),
            metric("events", "COUNT", source="events"),
            metric("ammunition", "SUM", field="bc_count", source="ammunition"),
        ])
        self.engine = QueryEngine(self.store, loader)

    def test_shared_scoped_populations_and_multiple_metrics(self):
        result = self.engine.execute(request(["flights", "effective", "efficiency", "events", "ammunition"]), self.principal)
        self.assertEqual({key: value for key, value in result["rows"][0].items() if key != "efficiency"}, {"flights": 3, "effective": 2, "events": 3, "ammunition": 5})
        self.assertAlmostEqual(result["rows"][0]["efficiency"], 200 / 3)
        self.assertEqual(len(self.calls), 1)

    def test_client_filter_cannot_widen_and_fixed_scope_intersects(self):
        forged = request(filters=[{"field": "department", "operator": "eq", "value": 2}])
        self.assertIsNone(self.engine.execute(forged, self.principal)["rows"][0]["flights"])
        fixed = request(data_scope="FIXED_SCOPE", fixed_scope={"scope_type": "DEPARTMENT", "scope_ids": ["2"]})
        self.assertEqual(self.engine.execute(fixed, self.principal)["status"], "empty")
        no_global = replace(self.principal, permissions=frozenset({"metric.view"}))
        with self.assertRaises(QueryPermissionError):
            self.engine.execute(request(data_scope="GLOBAL"), no_global)
        self.assertEqual(self.engine.execute(request(data_scope="GLOBAL"), self.principal)["rows"][0]["flights"], 4)

    def test_child_records_cannot_override_the_parent_scope(self):
        self.dataset["events"][-1].update(bbak_id=1, department_id=1, rota_id=11, crew="red")
        result = self.engine.execute(request(["events"]), self.principal)
        self.assertEqual(result["rows"][0]["events"], 3)

    def test_scope_and_definition_revision_are_in_cache_key(self):
        self.assertEqual(self.engine.execute(request(), self.principal)["rows"][0]["flights"], 3)
        peer = replace(self.principal, user_id="two", scope=DataScope(scope_type="DEPARTMENT", scope_ids=["2"]))
        self.assertEqual(self.engine.execute(request(), peer)["rows"][0]["flights"], 1)
        self.store.entities["metrics"][0] = metric("flights", "COUNT", filters=[{"field": "is_effective", "operator": "eq", "value": True}], revision=2)
        self.assertEqual(self.engine.execute(request(), self.principal)["rows"][0]["flights"], 2)
        self.assertEqual(len(self.calls), 2)  # Revised metric reuses scoped periods.

    def test_ordered_dimensions_and_sort_priority_do_not_collide_in_cache(self):
        a = request(dimensions=[{"field": "category"}, {"field": "team"}])
        b = request(dimensions=[{"field": "team"}, {"field": "category"}])
        self.assertEqual(self.engine.execute(a, self.principal)["dimensions"], ["category", "team"])
        self.assertEqual(self.engine.execute(b, self.principal)["dimensions"], ["team", "category"])
        self.assertEqual(len(self.calls), 1)  # Ordered outputs reuse the same data.

    def test_granularity_topn_and_stable_nulls(self):
        grouped = request(dimensions=[{"field": "date", "granularity": "week"}], sort=[{"field": "flights", "direction": "desc"}], top_n=1)
        result = self.engine.execute(grouped, self.principal)
        self.assertEqual(result["rows"], [{"date": "2026-09-07", "flights": 2}])
        self.assertEqual(result["total_rows"], 2)
        self.dataset["flights"][0]["device_type"] = None
        cache.invalidate_tags(["data:2026-09-01"])
        nulls = self.engine.execute(request(dimensions=[{"field": "category"}], sort=[{"field": "category", "direction": "desc"}]), self.principal)
        self.assertIsNone(nulls["rows"][-1]["category"])

    def test_count_known_zero_distinct_from_empty_population(self):
        self.store.entities["metrics"].append(metric("missing_status", "COUNT", source="events", filters=[{"field": "result", "operator": "eq", "value": "missing"}]))
        self.assertEqual(self.engine.execute(request(["missing_status"]), self.principal)["rows"][0]["missing_status"], 0)
        nobody = replace(self.principal, scope=DataScope(scope_type="NONE"))
        self.assertIsNone(self.engine.execute(request(["missing_status"]), nobody)["rows"][0]["missing_status"])

    def test_batch_reuses_data_and_isolates_bad_widgets(self):
        result = self.engine.batch([request(), request(["events"]), request(["unknown"])], self.principal)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([item["status"] for item in result["results"]], ["success", "success", "error"])

    def test_event_global_filters_restrict_flights_and_ammunition_via_flight_ids(self):
        result = self.engine.execute(request(["flights", "events", "ammunition"], filters=[{"field": "result", "operator": "eq", "value": "positive"}]), self.principal)
        self.assertEqual(result["rows"][0], {"flights": 2, "events": 2, "ammunition": 5})
        target = self.engine.execute(request(["flights", "events"], filters=[{"field": "target_class", "operator": "eq", "value": "B"}]), self.principal)
        self.assertEqual(target["rows"][0], {"flights": 1, "events": 1})

    def test_source_specific_filter_intersections_and_full_operator_whitelist(self):
        result = self.engine.execute(request(["flights", "events"], filters=[{"field": "field_200", "operator": "between", "value": [2, 4]}, {"field": "result", "operator": "not_contains", "value": "negative"}]), self.principal)
        self.assertEqual(result["rows"][0], {"flights": 2, "events": 2})
        empty = self.engine.execute(request(filters=[{"field": "result", "operator": "neq", "value": "negative"}, {"field": "bc_count", "operator": "gt", "value": 10}]), self.principal)
        self.assertEqual(empty["status"], "empty")
        result = self.engine.execute(request(filters=[{"field": "is_effective", "operator": "is_not_null"}]), self.principal)
        self.assertEqual(result["rows"][0]["flights"], 3)

    def test_unavailable_cross_source_dimension_is_explicitly_rejected(self):
        with self.assertRaisesRegex(QueryError, "unavailable for metric source flights"):
            self.engine.execute(request(["flights", "events"], dimensions=[{"field": "target_class"}]), self.principal)
        valid = self.engine.execute(request(["events"], dimensions=[{"field": "target_class"}]), self.principal)
        self.assertEqual(len(valid["rows"]), 3)

    def test_derived_metric_filters_apply_to_dependencies_without_affecting_other_metrics(self):
        self.store.entities["metrics"].extend([
            metric("efficiency_a", "RATIO", numerator="effective", denominator="flights", filters=[{"field": "category", "operator": "eq", "value": "A"}], format={"type": "percent"}),
            metric("positive_formula", "FORMULA", formula={"op": "add", "args": [{"metric": "flights"}, {"metric": "events"}]}, filters=[{"field": "result", "operator": "eq", "value": "positive"}]),
        ])
        result = self.engine.execute(request(["flights", "efficiency_a", "positive_formula"]), self.principal)
        self.assertEqual(result["rows"][0], {"flights": 3, "efficiency_a": 100, "positive_formula": 4})
        grouped = self.engine.execute(request(["positive_formula"], dimensions=[{"field": "category"}]), self.principal)
        self.assertEqual({row["category"]: row["positive_formula"] for row in grouped["rows"]}, {"A": 2, "B": 2, "special": None})

    def test_loss_metric_uses_historical_parent_scope_without_counting_old_flights(self):
        self.dataset["flights"].append({"flight_id": "old", "date": "2026-08-01", "bbak_id": 1, "rota_id": 11, "crew": "red"})
        self.dataset["lost_devices"] = [{"flight_id": "old", "date": "2026-09-15", "result": "lost"}]
        self.store.entities["metrics"].append(metric("losses", "COUNT", source="lost_devices"))
        result = self.engine.execute(request(["flights", "losses"]), self.principal)
        self.assertEqual(result["rows"][0], {"flights": 3, "losses": 1})

    def test_loss_parent_lookup_does_not_reintroduce_excluded_flights(self):
        self.dataset["flights"].append({"flight_id": "context", "date": "2026-09-15", "bbak_id": 1, "rota_id": 11, "crew": "red", "_bi_parent_context": True})
        self.dataset["lost_devices"] = [{"flight_id": "context", "date": "2026-09-15"}]
        self.store.entities["metrics"].append(metric("losses", "COUNT", source="lost_devices"))
        self.assertEqual(self.engine.execute(request(["flights", "losses"]), self.principal)["rows"][0], {"flights": 3, "losses": 1})

    def test_formula_cycle_raw_sql_and_permission_ref_rejected(self):
        self.store.entities["metrics"].extend([
            metric("a", "FORMULA", formula={"metric": "b"}),
            metric("b", "FORMULA", formula={"metric": "a"}),
        ])
        with self.assertRaises(QueryError):
            self.engine.execute(request(["a"]), self.principal)
        with self.assertRaises(QueryError):
            self.engine.execute(request(dimensions=[{"field": "date; DROP TABLE pk_events"}]), self.principal)
        self.store.entities["metrics"] = [metric("secret", "COUNT", permissions=["secret.view"]), metric("public", "FORMULA", formula={"metric": "secret"})]
        viewer = replace(self.principal, permissions=frozenset({"metric.view"}))
        with self.assertRaises(QueryPermissionError):
            self.engine.execute(request(["public"]), viewer)
        self.assertEqual(self.engine.metadata(viewer)["metrics"], [])

    def test_kpi_weights_normalization_and_direction(self):
        self.store.entities["kpis"] = [KpiDefinition(key="score", title="score", metrics=[{"key": "effective"}, {"key": "events"}], weights={"effective": 2}, formula={"op": "add", "args": [{"metric": "effective"}, {"metric": "events"}]}, normalization="MIN_MAX", minimum=0, maximum=10, direction="LOWER_IS_BETTER", format={"type": "percent"})]
        result = self.engine.execute(request(["score"]), self.principal)
        self.assertAlmostEqual(result["rows"][0]["score"], 30)
        self.store.entities["kpis"][0] = self.store.entities["kpis"][0].model_copy(update={"normalization": "PER_100_OPERATIONS", "normalization_metric": "flights", "revision": 2})
        self.assertAlmostEqual(self.engine.execute(request(["score"]), self.principal)["rows"][0]["score"], 700 / 3)

    def test_weighted_categories_are_configured_and_specials_excluded(self):
        self.store.entities["metrics"].append({"key": "weighted", "title": "weighted", "aggregation": "WEIGHTED_SUM", "source": "events", "field": "field_200", "category_field": "target_class", "filters": []})
        self.store.entities["category_weights"] = [
            {"category": "A", "weight": 1.5, "include_in_weighted_total": True},
            {"category": "B", "weight": 2, "include_in_weighted_total": True},
            {"category": "special", "weight": 1000, "display_separately": True, "include_in_weighted_total": False},
        ]
        self.assertEqual(self.engine.execute(request(["weighted"]), self.principal)["rows"][0]["weighted"], 11)

    def test_draft_preview_uses_unsaved_definition_and_never_changes_store(self):
        draft = metric("draft", "FORMULA", formula={"op": "add", "args": [{"metric": "flights"}, {"metric": "effective"}]})
        before = [item.model_dump() for item in self.store.entities["metrics"]]
        result = self.engine.preview("metrics", draft, request(), self.principal)
        self.assertEqual(result["rows"][0]["draft"], 5)
        self.assertEqual(before, [item.model_dump() for item in self.store.entities["metrics"]])
        viewer = replace(self.principal, permissions=frozenset({"metric.view"}))
        with self.assertRaises(QueryPermissionError):
            self.engine.preview("metrics", draft, request(), viewer)

    def comparison_request(self, **values):
        return {"level": "DEPARTMENT", "own_id": "1", "peer_id": "2", "query": request().model_dump(mode="json", by_alias=True), **values}

    def test_percent_only_response_contains_no_peer_absolute_values(self):
        permissions = {"metric.view", "comparison.view", "comparison.department", "comparison.view_difference", "comparison.select_peer"}
        viewer = replace(self.principal, permissions=frozenset(permissions))
        result = self.engine.comparison(self.comparison_request(), viewer)
        self.assertEqual(result["rows"][0]["difference_percent"], 200)
        self.assertFalse(result["raw_visible"])
        self.assertNotIn('"peer":', json.dumps(result))
        self.assertNotIn('"own":', json.dumps(result))
        raw = self.engine.comparison(self.comparison_request(), self.principal)
        self.assertEqual((raw["rows"][0]["own"], raw["rows"][0]["peer"]), (3, 1))

    def test_forged_own_id_and_peer_context_cannot_widen(self):
        self.assertEqual(self.engine.comparison(self.comparison_request(own_id="2", peer_id="1"), self.principal)["rows"], [])
        bad = self.comparison_request(level="GROUP", own_id="11", peer_id="21", context_department_id="1")
        with self.assertRaises(QueryError):
            self.engine.comparison(bad, self.principal)
        with self.assertRaises(QueryError):
            self.engine.comparison(self.comparison_request(level="TEAM", own_id="red", peer_id="blue", context_department_id="1"), self.principal)

    def test_percentage_points_rounding_and_range(self):
        value = self.comparison_request(query=request(["efficiency"]).model_dump(mode="json", by_alias=True))
        exact = self.engine.comparison(value, replace(self.principal, precision="EXACT"))["rows"][0]
        self.assertAlmostEqual(exact["difference_pp"], -100 / 3)
        self.assertNotIn("difference_percent", exact)
        rounded = self.engine.comparison(value, self.principal)["rows"][0]
        self.assertEqual(rounded["difference_pp"], -35)
        range_ = self.engine.comparison(value, replace(self.principal, precision="RANGE"))["rows"][0]
        self.assertEqual(range_["difference_range"], {"from": -35, "to": -30})
        self.assertNotIn("difference_pp", range_)

    def test_zero_and_negative_baselines_safe(self):
        self.store.entities["metrics"] = [metric("balance", "SUM", field="field_200", source="events")]
        self.dataset["events"][-1]["field_200"] = 0
        zero = self.engine.comparison(self.comparison_request(query=request(["balance"]).model_dump(mode="json", by_alias=True)), self.principal)["rows"][0]
        self.assertEqual(zero["status"], "zero_baseline")
        self.assertIsNone(zero["difference_percent"])
        self.dataset["events"][-1]["field_200"] = -2
        cache.invalidate_tags(["data:2026-09-09"])
        negative = self.engine.comparison(self.comparison_request(query=request(["balance"]).model_dump(mode="json", by_alias=True)), self.principal)["rows"][0]
        self.assertEqual(negative["difference_percent"], -450)
        self.assertEqual(negative["direction"], "higher")

    def test_clickhouse_loader_uses_bound_dates_and_requires_real_source(self):
        with patch("app.bi.engine.get_settings") as settings:
            settings.return_value.active_source = "mock"
            with self.assertRaises(QueryError):
                load_clickhouse_rows(date(2026, 9, 1), date(2026, 9, 30))
        with patch("app.bi.engine.get_settings") as settings, patch("app.services.clickhouse_analytics.clickhouse_analytics._physical", return_value="date"), patch("app.services.clickhouse_analytics.clickhouse_analytics._query_rows", return_value=[]) as query:
            settings.return_value.active_source = "clickhouse"
            load_clickhouse_rows(date(2026, 9, 1), date(2026, 9, 30))
            self.assertEqual(query.call_count, 4)
            where = query.call_args_list[0].args[2]
            self.assertIn("{d1:Date}", where.sql())
            self.assertEqual(where.params, {"d1": "2026-09-01", "d2": "2026-09-30"})

    def test_clickhouse_loss_backreference_is_bounded_parameterized_and_never_scans(self):
        calls = []

        def query(entity, keys, where):
            calls.append((entity, where))
            if entity == "lost_devices":
                return [{"flight_id": "old-uuid", "date": "2026-09-15"}]
            if entity == "flight_list" and "p1" in where.params:
                return [{"flight_id": "old-uuid", "date": "2026-08-01", "bbak_id": 1, "rota_id": 11, "crew": "red"}]
            return []

        with patch("app.bi.engine.get_settings") as settings, patch("app.services.clickhouse_analytics.clickhouse_analytics._physical", side_effect=lambda entity, field, **kwargs: field), patch("app.services.clickhouse_analytics.clickhouse_analytics._query_rows", side_effect=query):
            settings.return_value.active_source = "clickhouse"
            rows = load_clickhouse_rows(date(2026, 9, 1), date(2026, 9, 30))
            self.assertEqual(len(calls), 5)
            self.assertEqual(rows["flights"][0]["date"], "2026-08-01")
            self.assertEqual(calls[-1][1].params, {"p1": "old-uuid"})
            self.assertIn("{p1:String}", calls[-1][1].sql())
            self.assertNotIn("old-uuid", calls[-1][1].sql())


if __name__ == "__main__":
    unittest.main()
