import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import replace
from pathlib import Path

from app.bi.engine import QueryEngine, QueryError, QueryPermissionError, seed_default_metrics
from app.bi.models import BiComparisonRequest, CategoryWeight, DataScope, MetricDefinition, WeightSet
from app.bi.security import Principal
from app.bi.store import BiStore, BiConflict
from app.core.cache import cache


class SemanticMetricTests(unittest.TestCase):
    def setUp(self):
        cache.clear("bi")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = BiStore(Path(temporary.name) / "bi.sqlite3")
        seed_default_metrics(self.store)
        self.principal = Principal("one", "viewer", frozenset({"*"}), DataScope(scope_type="DEPARTMENT", scope_ids=["1"]), "EXACT")
        self.dataset = {"flights": [
            {"flight_id": "a", "date": "2026-09-01", "bbak_id": 1, "rota_id": 11, "crew": "B01", "device_type": "FPV", "direction": "АК 1"},
            {"flight_id": "b", "date": "2026-09-01", "bbak_id": 1, "rota_id": 12, "crew": "B12", "device_type": "FPV", "direction": "АК 2"},
            {"flight_id": "c", "date": "2026-09-01", "bbak_id": 1, "rota_id": 12, "crew": "Bomber", "device_type": "Бомбери", "direction": "АК 2"},
            {"flight_id": "unrelated", "date": "2026-09-01", "bbak_id": 1, "rota_id": 12, "crew": "B12", "device_type": "FPV"},
            {"flight_id": "peer", "date": "2026-09-01", "bbak_id": 2, "rota_id": 21, "crew": "Peer", "device_type": "FPV"},
        ], "events": [
            {"flight_id": "a", "date": "2026-09-01", "target_class": "Танк", "target_id": "t1", "result": " УРАЖЕНО ", "units": "Зона 1"},
            {"flight_id": "a", "date": "2026-09-01", "target_class": "Танк", "target_id": "t2", "result": "Уражено", "units": "Зона 1"},
            {"flight_id": "b", "date": "2026-09-01", "target_class": "Танк", "target_id": "t3", "result": "ЗНИЩЕНО", "units": "Зона 2"},
            {"flight_id": "c", "date": "2026-09-01", "target_class": "Танк", "target_id": "t3", "result": "знищено", "units": "Зона 2"},
            {"flight_id": "a", "date": "2026-09-01", "target_class": "ОС", "result": "уражено", "field_200": 2, "field_300": 5, "units": "Зона 1"},
            {"flight_id": "peer", "date": "2026-09-01", "target_class": "Танк", "target_id": "private", "result": "уражено", "units": "Зона 1"},
        ], "ammunition": [{"flight_id": "a", "date": "2026-09-01", "bc_name": " ВИЯВЛЕНО ", "bc_count": 7}], "lost_devices": []}
        self.engine = QueryEngine(self.store, lambda start, end: copy.deepcopy(self.dataset))

    def query(self, keys, **values):
        return {"metrics": [{"key": key} for key in keys], "date_range": {"from": "2026-09-01", "to": "2026-09-01"}, **values}

    def special(self):
        self.store.save("category_weights", CategoryWeight(category="ОС", weight=10, display_separately=True, include_in_general_total=False, include_in_weighted_total=False))

    def test_target_flight_ratios_follow_actual_result_links_and_category(self):
        self.special()
        result = self.engine.execute(self.query(["target_affected", "target_destroyed", "target_results", "flights_per_affected", "flights_per_destroyed", "flights_per_result", "targets"], dimensions=[{"field": "target_type"}]), self.principal)
        tank = result["rows"][0]
        self.assertEqual(tank["target_type"], "Танк")
        self.assertEqual((tank["target_affected"], tank["target_destroyed"], tank["target_results"], tank["targets"]), (2, 2, 4, 3))
        self.assertEqual((tank["flights_per_affected"], tank["flights_per_destroyed"], tank["flights_per_result"]), (.5, 1, .75))
        fpv = self.engine.execute(self.query(["target_results", "flights_per_result"], filters=[{"field": "category", "operator": "eq", "value": "FPV"}], dimensions=[{"field": "target_type"}]), self.principal)
        self.assertEqual(fpv["rows"][0]["target_results"], 3)
        self.assertAlmostEqual(fpv["rows"][0]["flights_per_result"], 2 / 3)

    def test_legacy_personnel_cards_and_detected_remain_distinct_from_target_counts(self):
        self.special()
        result = self.engine.execute(self.query(["affected", "destroyed", "detected", "target_results"]), self.principal)
        self.assertEqual(result["rows"], [{"affected": 5, "destroyed": 2, "detected": 7, "target_results": 4}])

    def test_time_window_uses_source_clocks_and_verified_ammunition_parent(self):
        self.dataset["flights"][0]["time_range"] = "08:00 - 09:00"
        self.dataset["flights"][1]["time_range"] = "10:00 - 11:00"
        self.dataset["events"][0]["timestamp"] = "2026-09-01T09:00:00+03:00"
        self.dataset["events"][1]["time"] = "11:00"
        self.dataset["events"][2]["time"] = "09:30"
        lower = datetime(2026, 9, 1, 8, 30, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
        upper = datetime(2026, 9, 1, 10, 30, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
        result = self.engine.execute(self.query(["flights", "events", "detected"], filters=[{"field": "local_timestamp", "operator": "gte", "value": lower}, {"field": "local_timestamp", "operator": "lte", "value": upper}]), self.principal)
        self.assertEqual(result["rows"], [{"flights": 1, "events": 2, "detected": None}])

    def test_time_window_applies_first_date_start_and_last_date_end(self):
        self.dataset["flights"] = [{"flight_id": str(index), "date": day, "time_range": clock, "bbak_id": 1} for index, (day, clock) in enumerate([("2026-09-01", "08:00"), ("2026-09-01", "09:00"), ("2026-09-02", "01:00"), ("2026-09-03", "09:00"), ("2026-09-03", "10:00")])]
        lower = datetime(2026, 9, 1, 9, 0, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
        upper = datetime(2026, 9, 3, 9, 0, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp()
        result = self.engine.execute({"metrics": [{"key": "flights"}], "date_range": {"from": "2026-09-01", "to": "2026-09-03"}, "filters": [{"field": "local_timestamp", "operator": "gte", "value": lower}, {"field": "local_timestamp", "operator": "lte", "value": upper}]}, self.principal)
        self.assertEqual(result["rows"], [{"flights": 3}])

    def test_display_separately_moves_group_and_general_flag_controls_total(self):
        self.special()
        grouped = self.engine.execute(self.query(["target_results"], dimensions=[{"field": "target_type"}]), self.principal)
        self.assertEqual(grouped["rows"], [{"target_type": "Танк", "target_results": 4}])
        self.assertEqual(grouped["separate_rows"], [{"target_type": "ОС", "target_results": 1}])
        self.assertEqual(self.engine.execute(self.query(["target_results"]), self.principal)["rows"][0]["target_results"], 4)
        weight = self.store.find_by_key("category_weights", "ОС")
        self.store.save("category_weights", weight.model_copy(update={"include_in_general_total": True}))
        self.assertEqual(self.engine.execute(self.query(["target_results"]), self.principal)["rows"][0]["target_results"], 5)

    def test_weight_sets_are_independent_and_revisions_invalidate_only_calculation(self):
        self.store.save("category_weights", CategoryWeight(category="Танк", weight=2))
        self.store.save("category_weights", CategoryWeight(weight_set_id="complexity", category="Танк", weight=5))
        for key, set_id in (("equivalent", "target_equivalent"), ("complexity_score", "complexity")):
            self.store.save("metrics", MetricDefinition(key=key, title=key, source="events", aggregation="WEIGHTED_SUM", category_field="target_class", weight_set_id=set_id))
        result = self.engine.execute(self.query(["equivalent", "complexity_score"]), self.principal)
        self.assertEqual(result["rows"], [{"equivalent": 8, "complexity_score": 20}])
        weight = self.store.find_by_key("category_weights", "Танк", weight_set_id="complexity")
        self.store.save("category_weights", weight.model_copy(update={"weight": 10}))
        self.assertEqual(self.engine.execute(self.query(["equivalent", "complexity_score"]), self.principal)["rows"], [{"equivalent": 8, "complexity_score": 40}])

    def test_same_category_is_unique_within_set_but_can_exist_in_other_sets(self):
        self.store.save("category_weights", CategoryWeight(category="Танк"))
        with self.assertRaises(BiConflict):
            self.store.save("category_weights", CategoryWeight(category="Танк"))
        self.store.save("category_weights", CategoryWeight(weight_set_id="priority", category="Танк"))
        with self.assertRaises(QueryError):
            self.engine.validate_definition("category_weights", CategoryWeight(category=" танк "), self.principal)

    def test_active_weight_set_is_required_and_referenced_set_cannot_be_deleted(self):
        with self.assertRaises(BiConflict):
            configured = self.store.get("weight_sets", "target_equivalent")
            self.store.delete("weight_sets", configured.id, configured.revision)
        configured = self.store.get("weight_sets", "target_equivalent")
        self.store.save("weight_sets", configured.model_copy(update={"is_active": False}))
        with self.assertRaises(QueryError):
            self.engine.execute(self.query(["target_results"]), self.principal)

    def test_zones_are_event_units_and_ak_is_parent_direction(self):
        result = self.engine.execute(self.query(["target_results"], dimensions=[{"field": "zone"}, {"field": "direction"}], filters=[{"field": "category", "operator": "eq", "value": "FPV"}]), self.principal)
        self.assertEqual({(row["zone"], row["direction"]) for row in result["rows"]}, {("Зона 1", "АК 1"), ("Зона 2", "АК 2")})

    def test_crew_comparison_uses_category_without_requiring_rota(self):
        body = BiComparisonRequest.model_validate({"level": "CREW", "own_id": "B01", "peer_id": "B12", "context_category": "FPV", "context_department_id": "1", "query": self.query(["flights"])})
        self.assertEqual(self.engine.comparison(body, self.principal)["rows"][0]["difference_percent"], -50)
        with self.assertRaises(QueryError):
            self.engine.comparison({**body.model_dump(mode="json", by_alias=True), "peer_id": "Bomber"}, self.principal)

    def test_category_axis_is_independent_from_bbak_scope(self):
        body = {"level": "CATEGORY", "own_id": "FPV", "peer_id": "Бомбери", "context_department_id": "1", "query": self.query(["flights"])}
        self.assertEqual(self.engine.comparison(body, self.principal)["rows"][0]["difference_percent"], 200)

    def test_restricted_breakdown_whitelist_cannot_be_overridden_by_client(self):
        viewer = replace(self.principal, permissions=frozenset({"metric.view", "comparison.view", "comparison.view_difference", "comparison.unit", "comparison.select_peer", "comparison.view_breakdown"}))
        body = {"level": "BBAK", "own_id": "1", "peer_id": "2", "query": self.query(["flights"], dimensions=[{"field": "crew"}])}
        with self.assertRaises(QueryPermissionError):
            self.engine.comparison(body, viewer)
        body["query"] = self.query(["flights"], dimensions=[{"field": "category"}])
        self.assertFalse(self.engine.comparison(body, viewer)["raw_visible"])


class LegacyWeightMigrationTests(unittest.TestCase):
    def test_migration_preserves_old_weight_ids_json_and_revisions(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "bi.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE category_weights (id TEXT PRIMARY KEY,category TEXT NOT NULL UNIQUE,revision INTEGER NOT NULL,definition_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
            definition = {"id": "old", "category": "Танк", "weight": 4, "revision": 3, "display_separately": True}
            connection.execute("INSERT INTO category_weights VALUES (?,?,?,?,?,?)", ("old", "Танк", 3, json.dumps(definition), "old-time", "old-time"))
        connection.close()
        store = BiStore(path)
        migrated = store.get("category_weights", "old")
        self.assertEqual((migrated.id, migrated.revision, migrated.weight_set_id, migrated.weight, migrated.display_separately), ("old", 3, "target_equivalent", 4, True))
        self.assertEqual(len(store.list("weight_sets")), 4)
        saved = store.save("category_weights", migrated.model_copy(update={"weight": 6}))
        self.assertEqual(saved.revision, 4)
        restarted = BiStore(path)
        self.assertEqual(restarted.get("category_weights", "old").weight, 6)


if __name__ == "__main__":
    unittest.main()
