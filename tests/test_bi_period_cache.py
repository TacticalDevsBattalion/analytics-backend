import copy
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import patch

from app.bi.cache_policy import CachePolicy, date_tags
from app.bi.clickhouse_aggregate import aggregate_metric
from app.bi.engine import QueryEngine, QueryError
from app.bi.models import DataScope, MetricDefinition, QueryRequest
from app.bi.security import Principal
from app.core.cache import cache


class Store:
    def __init__(self, metrics):
        self.metrics = metrics

    def list(self, entity):
        return self.metrics if entity == "metrics" else []


def query(keys=("flights",), start="2026-09-01", end="2026-09-03", **values):
    return QueryRequest.model_validate({"metrics": [{"key": key} for key in keys], "date_range": {"from": start, "to": end}, **values})


class CachePolicyTests(unittest.TestCase):
    def test_temporal_bands_and_horizon_do_not_limit_query_dates(self):
        policy, today = CachePolicy(), date(2026, 10, 5)
        for age, ttl in [(0, 600), (2, 600), (3, 21600), (14, 21600), (15, 172800), (120, 172800), (121, 0)]:
            self.assertEqual(policy.ttl_for_day(today - timedelta(days=age), today), ttl)
        self.assertEqual(policy.ttl_for_range(today - timedelta(days=30), today, today), 600)
        self.assertEqual(date_tags(date(2026, 9, 1), date(2026, 9, 2)), ["data:2026-09-01", "data:2026-09-02"])

    def test_policy_env_is_validated(self):
        with patch.dict(os.environ, {"ANALYTICS_CACHE_HORIZON_DAYS": "90", "ANALYTICS_CACHE_HOT_TTL_SECONDS": "900"}):
            self.assertEqual(CachePolicy.from_env().horizon_days, 90)
            self.assertEqual(CachePolicy.from_env().hot_ttl_seconds, 900)
        with patch.dict(os.environ, {"ANALYTICS_CACHE_HOT_DAYS": "30", "ANALYTICS_CACHE_WARM_DAYS": "14"}), self.assertRaises(ValueError):
            CachePolicy.from_env()


class PeriodCacheTests(unittest.TestCase):
    def setUp(self):
        cache.clear("bi")
        self.principal = Principal("one", "viewer", frozenset({"*"}), DataScope(scope_type="DEPARTMENT", scope_ids=["1"]), "EXACT")
        self.data = {"flights": [
            {"flight_id": "a", "date": "2026-09-01", "bbak_id": 1, "rota_id": 11, "crew": "red", "device_type": "A", "is_effective": True},
            {"flight_id": "b", "date": "2026-09-02", "bbak_id": 1, "rota_id": 11, "crew": "red", "device_type": "B", "is_effective": False},
            {"flight_id": "c", "date": "2026-09-03", "bbak_id": 1, "rota_id": 12, "crew": "blue", "device_type": "A", "is_effective": True},
            {"flight_id": "p", "date": "2026-09-03", "bbak_id": 2, "rota_id": 21, "crew": "peer", "is_effective": True},
        ], "events": [], "ammunition": [], "lost_devices": []}
        self.calls = []

        def loader(start, end):
            self.calls.append((start, end))
            return {source: [copy.deepcopy(row) for row in rows if start <= date.fromisoformat(row["date"]) <= end] for source, rows in self.data.items()}

        self.loader = loader
        self.store = Store([
            MetricDefinition(key="flights", title="flights", aggregation="COUNT_DISTINCT", field="flight_id"),
            MetricDefinition(key="effective", title="effective", aggregation="COUNT", filters=[{"field": "is_effective", "operator": "eq", "value": True}]),
            MetricDefinition(key="efficiency", title="efficiency", aggregation="RATIO", numerator="effective", denominator="flights", format={"type": "percent"}),
        ])
        self.engine = QueryEngine(self.store, loader, today=lambda: date(2026, 10, 5))

    def test_cold_range_is_one_read_and_overlap_uses_daily_partitions(self):
        self.assertEqual(self.engine.execute(query(), self.principal)["rows"][0]["flights"], 3)
        self.assertEqual(self.engine.execute(query(start="2026-09-02"), self.principal)["rows"][0]["flights"], 2)
        self.assertEqual(self.calls, [(date(2026, 9, 1), date(2026, 9, 3))])

    def test_day_invalidation_reloads_only_that_day_and_dependent_ratio(self):
        self.engine.execute(query(("flights", "efficiency")), self.principal)
        self.data["flights"][1]["is_effective"] = True
        cache.invalidate_tags(["data:2026-09-02"])
        result = self.engine.execute(query(("flights", "efficiency")), self.principal)
        self.assertEqual(result["rows"][0]["efficiency"], 100)
        self.assertEqual(self.calls[-1], (date(2026, 9, 2), date(2026, 9, 2)))
        self.assertEqual(len(self.calls), 2)

    def test_metric_aggregate_reused_across_widget_order_sort_and_topn(self):
        with patch.object(self.engine, "_evaluate", wraps=self.engine._evaluate) as evaluate:
            self.engine.execute(query(("flights", "effective"), dimensions=[{"field": "category"}]), self.principal)
            before = evaluate.call_count
            result = self.engine.execute(query(("effective", "flights"), dimensions=[{"field": "category"}], sort=[{"field": "effective", "direction": "desc"}], top_n=1), self.principal)
            self.assertEqual(evaluate.call_count, before)
        self.assertEqual(result["rows"], [{"category": "A", "effective": 2, "flights": 2}])
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(len(self.calls), 1)

    def test_transitive_metric_invalidation_recomputes_formula_without_db(self):
        self.engine.execute(query(("efficiency",)), self.principal)
        cache.invalidate_tags(["metric:effective"])
        with patch.object(self.engine, "_evaluate", wraps=self.engine._evaluate) as evaluate:
            self.engine.execute(query(("efficiency",)), self.principal)
            self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(len(self.calls), 1)

    def test_period_cache_stores_only_assigned_rows_and_separates_peers(self):
        validated = self.engine._validated(query(), self.principal, self.engine._definitions()[0])
        own = self.engine._load(validated, self.principal)
        self.assertEqual({row["bbak_id"] for row in own["flights"]}, {1})
        peer = replace(self.principal, user_id="two", scope=DataScope(scope_type="DEPARTMENT", scope_ids=["2"]))
        self.assertEqual(self.engine.execute(query(), peer)["rows"][0]["flights"], 1)
        self.assertEqual(len(self.calls), 2)

    def test_beyond_horizon_executes_direct_and_does_not_return_cached_history(self):
        old = query(start="2025-01-01", end="2025-01-02")
        self.engine.execute(old, self.principal)
        self.engine.execute(old, self.principal)
        self.assertEqual(len(self.calls), 2)

    def test_oversized_daily_raw_payload_is_not_stored(self):
        engine = QueryEngine(self.store, self.loader, cache_policy=CachePolicy(raw_max_rows=1), today=lambda: date(2026, 10, 5))
        validated = engine._validated(query(start="2026-09-03"), self.principal, engine._definitions()[0])
        all_principal = replace(self.principal, scope=DataScope(scope_type="ALL"))
        engine._load(validated, all_principal)
        engine._load(validated, all_principal)
        self.assertEqual(len(self.calls), 2)

    def test_distinct_average_and_ratio_merge_exact_records_across_days(self):
        self.data["flights"][1]["flight_id"] = "a"
        self.data["events"] = [{"flight_id": "a", "date": "2026-09-01", "field_200": 0} for _ in range(3)] + [{"flight_id": "c", "date": "2026-09-03", "field_200": 100}]
        self.store.metrics.append(MetricDefinition(key="average", title="average", source="events", aggregation="AVG", field="field_200"))
        result = self.engine.execute(query(("flights", "average", "efficiency")), self.principal)
        self.assertEqual(result["rows"][0], {"flights": 2, "average": 25, "efficiency": 100})

    def test_comparison_uses_one_period_read_and_keeps_raw_permission(self):
        request = {"level": "DEPARTMENT", "own_id": "1", "peer_id": "2", "query": query(("flights", "effective")).model_dump(mode="json", by_alias=True)}
        viewer = replace(self.principal, permissions=frozenset({"metric.view", "comparison.view", "comparison.view_difference", "comparison.department", "comparison.select_peer"}))
        result = self.engine.comparison(request, viewer)
        self.engine.comparison(request, viewer)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(result["raw_visible"])
        self.assertTrue(all("peer" not in row and "own" not in row for row in result["rows"]))

    def test_same_day_child_final_cache_hits_without_another_sql_read(self):
        self.data["events"] = [{"flight_id": "a", "date": "2026-09-01"}]
        self.store.metrics.append(MetricDefinition(key="events", title="events", source="events", aggregation="COUNT"))
        with patch.object(self.engine, "_evaluate", wraps=self.engine._evaluate) as evaluate:
            self.engine.execute(query(("events",)), self.principal)
            self.engine.execute(query(("events",)), self.principal)
            self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(len(self.calls), 1)

    def test_oversized_raw_child_data_still_reuses_aggregate_without_sql(self):
        self.data["events"] = [{"flight_id": "a", "date": "2026-09-01"} for _ in range(3)]
        self.store.metrics.append(MetricDefinition(key="events", title="events", source="events", aggregation="COUNT"))
        engine = QueryEngine(self.store, self.loader, cache_policy=CachePolicy(raw_max_rows=1), today=lambda: date(2026, 10, 5))
        req = query(("events",), start="2026-09-01", end="2026-09-01")
        self.assertEqual(engine.execute(req, self.principal)["rows"], [{"events": 3}])
        self.assertEqual(engine.execute(req, self.principal)["rows"], [{"events": 3}])
        self.assertEqual(len(self.calls), 1)

    def test_old_parent_day_invalidation_updates_current_child_scope(self):
        self.store.metrics.append(MetricDefinition(key="losses", title="losses", source="lost_devices", aggregation="COUNT"))
        parent = {"flight_id": "old", "date": "2026-08-01", "bbak_id": 1, "rota_id": 11, "crew": "red", "_bi_parent_context": True}
        calls = []

        def loader(start, end):
            calls.append(1)
            return {"flights": [dict(parent)], "lost_devices": [{"flight_id": "old", "date": "2026-09-03"}]}

        engine = QueryEngine(self.store, loader, today=lambda: date(2026, 10, 5))
        req = query(("losses",), start="2026-09-03", end="2026-09-03")
        with patch.object(cache, "get_or_load", wraps=cache.get_or_load) as caches:
            self.assertEqual(engine.execute(req, self.principal)["rows"], [{"losses": 1}])
            aggregate_call = next(call for call in caches.call_args_list if call.args[0] == "bi.metric")
            self.assertIn("data:2026-08-01", aggregate_call.kwargs["dependency_tags"])
        parent["bbak_id"] = 2
        cache.invalidate_tags(["data:2026-08-01"])
        self.assertEqual(engine.execute(req, self.principal)["status"], "empty")
        self.assertEqual(len(calls), 2)  # Cross-day hierarchy is never frozen raw.

    def test_invalidation_while_preparing_old_parent_does_not_cache_stale_child(self):
        self.store.metrics.append(MetricDefinition(key="losses", title="losses", source="lost_devices", aggregation="COUNT"))
        calls = []

        def loader(start, end):
            calls.append(1)
            if len(calls) == 1:
                cache.invalidate_tags(["data:2026-08-01"])
            return {"flights": [{"flight_id": "old", "date": "2026-08-01", "bbak_id": 1, "rota_id": 11, "crew": "red", "_bi_parent_context": True}], "lost_devices": [{"flight_id": "old", "date": "2026-09-03"}]}

        engine = QueryEngine(self.store, loader, today=lambda: date(2026, 10, 5))
        req = query(("losses",), start="2026-09-03", end="2026-09-03")
        with patch.object(engine, "_evaluate", wraps=engine._evaluate) as evaluate:
            engine.execute(req, self.principal)
            engine.execute(req, self.principal)
            self.assertEqual(evaluate.call_count, 2)

    def test_unrelated_day_invalidation_preserves_other_period_and_aggregate(self):
        req = query(("efficiency",), start="2026-09-01", end="2026-09-01")
        self.engine.execute(req, self.principal)
        cache.invalidate_tags(["data:2026-09-03"])
        with patch.object(self.engine, "_evaluate", wraps=self.engine._evaluate) as evaluate:
            self.engine.execute(req, self.principal)
            self.assertEqual(evaluate.call_count, 0)
        self.assertEqual(len(self.calls), 1)

    def test_source_invalidation_during_read_does_not_publish_stale_partitions(self):
        calls = []

        def loader(start, end):
            calls.append(1)
            value = self.loader(start, end)
            if len(calls) == 1:
                cache.invalidate_tags(["data:2026-09-01"])
            return value

        engine = QueryEngine(self.store, loader, today=lambda: date(2026, 10, 5))
        engine.execute(query(start="2026-09-01", end="2026-09-01"), self.principal)
        engine.execute(query(start="2026-09-01", end="2026-09-01"), self.principal)
        self.assertEqual(len(calls), 2)

    def test_simultaneous_different_widgets_share_period_fill(self):
        calls, gate = [], threading.Lock()

        def loader(start, end):
            with gate:
                calls.append(1)
            time.sleep(.05)
            return self.loader(start, end)

        engine = QueryEngine(self.store, loader, today=lambda: date(2026, 10, 5))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda key: engine.execute(query((key,)), self.principal), ["flights", "effective"]))
        self.assertEqual(len(calls), 1)
        self.assertEqual([result["status"] for result in results], ["success", "success"])


class ClickHouseAggregateTests(unittest.TestCase):
    def setUp(self):
        cache.clear("bi")
        self.principal = Principal("one", "viewer", frozenset({"*"}), DataScope(scope_type="DEPARTMENT", scope_ids=["1"]), "EXACT")
        self.metric = MetricDefinition(key="flights", title="flights", aggregation="COUNT_DISTINCT", field="flight_id").model_dump(mode="json")

    def patches(self, rows):
        return (
            patch("app.services.clickhouse_analytics.clickhouse_analytics._physical", side_effect=lambda entity, key, **kwargs: key),
            patch("app.services.clickhouse_client.clickhouse_client.query", return_value=rows),
        )

    def test_exact_distinct_sql_is_bound_and_scope_intersects_filters(self):
        physical, sql_query = self.patches([{"_population": 3, "_value": 2}])
        dangerous = "x' OR 1=1 --"
        req = query(filters=[{"field": "device", "operator": "eq", "value": dangerous}]).model_dump(mode="json", by_alias=True)
        with physical, sql_query as sql:
            result = aggregate_metric(req, self.principal, self.metric)
        statement, params = sql.call_args.args[0], sql.call_args.kwargs["parameters"]
        self.assertIn("uniqExactIf", statement)
        self.assertIn("bbak_id", statement)
        self.assertNotIn(dangerous, statement)
        self.assertIn(dangerous, params.values())
        self.assertEqual(result["rows"], [{"flights": 2}])

    def test_unsupported_join_and_self_scope_fall_back_without_sql(self):
        physical, sql_query = self.patches([])
        with physical, sql_query as sql:
            event_filter = query(filters=[{"field": "result", "operator": "eq", "value": "positive"}]).model_dump(mode="json", by_alias=True)
            self.assertIsNone(aggregate_metric(event_filter, self.principal, self.metric))
            own = replace(self.principal, scope=DataScope(scope_type="SELF"))
            self.assertIsNone(aggregate_metric(query().model_dump(mode="json", by_alias=True), own, self.metric))
            self.assertFalse(sql.called)

    def test_count_zero_and_empty_population_are_different(self):
        self.metric.update(aggregation="COUNT", field=None, filters=[{"field": "is_effective", "operator": "eq", "value": True}])
        physical, sql_query = self.patches([{"_population": 3, "_value": 0}])
        with physical, sql_query as sql:
            result = aggregate_metric(query().model_dump(mode="json", by_alias=True), self.principal, self.metric)
            self.assertEqual(result["rows"][0]["flights"], 0)
            self.assertIn("countIf", sql.call_args.args[0])
            sql.return_value = [{"_population": 0, "_value": 0}]
            self.assertIsNone(aggregate_metric(query().model_dump(mode="json", by_alias=True), self.principal, self.metric)["rows"][0]["flights"])

    def test_additive_daily_counts_reuse_overlap_without_summing_distinct(self):
        count = {**self.metric, "aggregation": "COUNT", "field": None}
        engine = QueryEngine(Store([count]), lambda start, end: {}, today=lambda: date(2026, 10, 5))
        physical, sql_query = self.patches([
            {"_day": "2026-09-01", "_population": 4, "_value": 4},
            {"_day": "2026-09-02", "_population": 2, "_value": 2},
            {"_day": "2026-09-03", "_population": 1, "_value": 1},
        ])
        with physical, sql_query as sql:
            req = query().model_dump(mode="json", by_alias=True)
            self.assertEqual(engine._native_count(req, self.principal, count, [])["rows"], [{"flights": 7}])
            req["date_range"]["from"] = "2026-09-02"
            self.assertEqual(engine._native_count(req, self.principal, count, [])["rows"], [{"flights": 3}])
            self.assertEqual(sql.call_count, 1)
            sql.return_value = [{"_population": 7, "_value": 1}]
            self.assertEqual(engine._native_count(req, self.principal, self.metric, [])["rows"], [{"flights": 1}])
            self.assertIn("uniqExactIf", sql.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
