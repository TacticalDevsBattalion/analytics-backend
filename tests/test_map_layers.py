import json
import unittest

from app.services.map_layers import load_lines, mgrs_to_lon_lat, target_features, wkt_to_geometry


class MgrsTests(unittest.TestCase):
    def test_converts_mgrs_to_wgs84(self):
        # Reference values from the `mgrs` library (corner of the 1 m square).
        for grid, lat, lon in (("37U DQ 06502 11859", 48.852650, 37.725443), ("36T VR 78108 38258", 45.497510, 32.719801), ("37UCP9233888023", 48.635996, 37.538651)):
            got_lon, got_lat = mgrs_to_lon_lat(grid)
            self.assertAlmostEqual(got_lat, lat, delta=0.00002)
            self.assertAlmostEqual(got_lon, lon, delta=0.00002)

    def test_rejects_malformed_values(self):
        for value in (None, "", "Слов’янськ", "37U DQ 06502 1185", "37I DQ 06502 11859", "61U DQ 1 1"):
            self.assertIsNone(mgrs_to_lon_lat(value))


class TargetTests(unittest.TestCase):
    def test_one_point_per_target_with_full_history(self):
        events = [
            {"target_id": "T1", "result": "Уражено", "mgrs": "37U DQ 06500 11850", "date": "2026-10-10", "time": "08:00", "crew": "A1", "flight_id": "f1"},
            {"target_id": "T1", "result": "Знищено", "mgrs": "37U DQ 06502 11859", "date": "2026-10-11", "time": "07:30", "crew": "A2", "flight_id": "f2"},
            {"target_id": "T1", "result": "уражено ", "mgrs": "37U DQ 06510 11870", "date": "2026-10-12", "time": "09:00", "crew": "A3", "flight_id": "f3"},
            {"target_id": "T2", "result": "Уражено", "mgrs": "37U DQ 10000 10000", "date": "2026-10-12", "crew": "B1", "flight_id": "f4"},
            {"target_id": "T3", "result": "Промах", "mgrs": "37U DQ 10000 10000", "date": "2026-10-12"},
            {"target_id": "T4", "result": "Знищено", "mgrs": None, "date": "2026-10-12"},
        ]
        result = target_features(events, affected="уражено", destroyed="знищено", flights={"f1": {"position": "ДЖАНКОЙ"}})
        features = {feature["properties"]["id"]: feature for feature in result["features"]}
        self.assertEqual(set(features), {"T1", "T2"})
        t1 = features["T1"]["properties"]
        self.assertEqual((t1["status"], t1["strikes"], t1["affected_count"], t1["destroyed_count"]), ("destroyed", 3, 2, 1))
        # Placed where it was destroyed, not at the later strike.
        self.assertEqual(features["T1"]["geometry"]["coordinates"], list(mgrs_to_lon_lat("37U DQ 06502 11859")))
        history = json.loads(t1["history"])
        self.assertEqual([item["crew"] for item in history], ["A1", "A2", "A3"])
        self.assertEqual(history[0]["position"], "ДЖАНКОЙ")
        self.assertEqual(features["T2"]["properties"]["status"], "affected")

    def test_unix_local_time_is_shown_in_kyiv_time(self):
        events = [{"target_id": "T", "result": "Знищено", "mgrs": "37U DQ 06502 11859", "local_timestamp": 1791665220.0}]
        properties = target_features(events, affected="уражено", destroyed="знищено")["features"][0]["properties"]
        self.assertEqual(properties["timestamp"], "2026-10-10 23:47")
        # Cached datasets return the same value as text.
        events[0]["local_timestamp"] = "1791665220.0"
        self.assertEqual(target_features(events, affected="уражено", destroyed="знищено")["features"][0]["properties"]["timestamp"], "2026-10-10 23:47")


class LineTests(unittest.TestCase):
    def test_parses_wkt_and_skips_missing_views(self):
        self.assertEqual(wkt_to_geometry("LINESTRING(31.7 52.1,31.8 52.2)"), {"type": "LineString", "coordinates": [[31.7, 52.1], [31.8, 52.2]]})
        self.assertEqual(wkt_to_geometry("MULTIPOLYGON(((1 2,3 4,5 6,1 2)),((7 8,9 10,11 12,7 8)))")["coordinates"][1][0][2], [11.0, 12.0])
        self.assertIsNone(wkt_to_geometry("POINT(1 2)"))

        def query(sql):
            if "front_line" in sql:
                raise RuntimeError("missing view")
            return [{"id": 1, "label": None, "geom": "MULTILINESTRING((1 2,3 4),(5 6,7 8))", "valid_from": "2026-10-17 09:02:48.644"}]
        lines = load_lines(query)
        self.assertEqual(len(lines["features"]), 1)
        self.assertEqual(lines["features"][0]["properties"], {"layer": "flet", "title": "FLET", "valid_from": "2026-10-17 09:02"})
        self.assertEqual(lines["features"][0]["geometry"]["type"], "MultiLineString")
