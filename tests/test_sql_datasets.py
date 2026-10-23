import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.bi import activity
from app.bi.models import WidgetDefinition
from app.bi.sql_datasets import SEED, DatasetError, DatasetNotFound, DatasetStore, encode, parameters, validate
from app.bi.store import BiStore

SQL = "SELECT x FROM t WHERE d BETWEEN {date_from:Date32} AND {date_to:Date32} AND has({names:Array(String)}, x) -- {ignored:String}\n AND y = 'a{b:String}'"


class ValidationTests(unittest.TestCase):
    def test_parameters_come_from_the_sql_outside_comments_and_strings(self):
        self.assertEqual(parameters(SQL), {"date_from": "Date32", "date_to": "Date32", "names": "Array(String)"})

    def test_only_one_select_without_backend_settings(self):
        for sql in ("DROP TABLE t", "SELECT 1; SELECT 2", "SELECT 1 FORMAT JSON", "SELECT 1 SETTINGS readonly=0", "SELECT {a:Map(String,String)}", "SELECT {a:String}, {a:Int64}"):
            with self.assertRaises(DatasetError, msg=sql):
                validate(sql)
        self.assertEqual(validate("WITH a AS (SELECT 1) SELECT formatDateTime(now(), '%F; x') FROM a;"), {})

    def test_values_are_sent_in_clickhouse_text_form(self):
        self.assertEqual(encode(["a'b", "c\\d"], "Array(String)"), "['a\\'b','c\\\\d']")
        self.assertEqual(encode(None, "Nullable(Float64)"), "\\N")
        self.assertEqual(encode(1.5, "Nullable(Float64)"), "1.5")
        self.assertEqual(encode("2026-10-01", "Date32"), "2026-10-01")
        self.assertEqual(encode(True, "UInt8"), "1")
        with self.assertRaises(DatasetError):
            encode(None, "String")
        with self.assertRaises(DatasetError):
            encode("1; DROP", "Int64")

    def test_the_seeded_ranking_dataset_is_valid(self):
        declared = validate((SEED / "ranking.groups.sql").read_text(encoding="utf-8"))
        self.assertEqual(declared["date_from"], "Date32")
        self.assertEqual(declared["excluded_purposes"], "Array(String)")
        self.assertEqual(declared["time_from"], "Nullable(Float64)")


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.dict("os.environ", {"DATABASE_URL": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = BiStore(Path(directory.name) / "bi.sqlite3")
        self.datasets = DatasetStore(self.store)

    def test_seed_is_published_once_and_new_versions_wait_for_publishing(self):
        published = self.datasets.published("ranking.groups")
        self.assertEqual(published["version"], 1)
        saved = self.datasets.save_version("ranking.groups", "SELECT {date_from:Date32} AS d", actor="admin-1", comment="simpler")
        self.assertEqual(saved["version"], 2)
        self.assertEqual(self.datasets.published("ranking.groups")["version"], 1)
        self.datasets.publish("ranking.groups", 2, actor="admin-1")
        self.assertEqual(self.datasets.published("ranking.groups")["sql"], "SELECT {date_from:Date32} AS d")
        # A second store over the same database does not seed again.
        self.assertEqual(len(DatasetStore(self.store).get("ranking.groups")["versions"]), 2)
        actions = [(item["action"], item["actor_id"], item["revision"]) for item in activity.history(self.store, "dataset", "ranking.groups")]
        self.assertEqual(actions, [("publish", "admin-1", 2), ("version", "admin-1", 2), ("create", "system", 1)])

    def test_archived_and_unknown_datasets_are_not_run(self):
        self.datasets.save_version("crews.daily", "SELECT 1 AS x", actor="admin-1", title="Crews")
        with self.assertRaises(DatasetNotFound):
            self.datasets.published("crews.daily")  # a draft until published
        self.datasets.publish("crews.daily", 1, actor="admin-1")
        self.datasets.archive("crews.daily", actor="admin-1")
        with self.assertRaises(DatasetNotFound):
            self.datasets.published("crews.daily")
        with self.assertRaises(DatasetError):
            self.datasets.save_version("Bad Key", "SELECT 1", actor="admin-1")

    def test_runs_are_readonly_with_every_declared_parameter(self):
        self.datasets.save_version("crews.daily", "SELECT {day:Date32} AS d, has({names:Array(String)}, 'a') AS h", actor="admin-1")
        self.datasets.publish("crews.daily", 1, actor="admin-1")
        with mock.patch("app.services.clickhouse_client.clickhouse_client.query", return_value=[{"d": "2026-10-01"}]) as query:
            self.assertEqual(self.datasets.run("crews.daily", {"day": "2026-10-01", "names": ["a"]}), [{"d": "2026-10-01"}])
            with self.assertRaises(DatasetError):
                self.datasets.run("crews.daily", {"day": "2026-10-01"})
        self.assertEqual(query.call_args.kwargs, {"parameters": {"day": "2026-10-01", "names": "['a']"}, "settings": {"readonly": 1}})
        report = activity.usage(self.store, 7)
        self.assertEqual(next(item for item in report["items"] if item["entity_id"] == "crews.daily")["runs"], 1)
        self.assertIn("ranking.groups", {item["entity_id"] for item in report["unused"]})


class HistoryAndUsageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.dict("os.environ", {"DATABASE_URL": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = BiStore(Path(directory.name) / "bi.sqlite3")

    def widget(self, **changes):
        return WidgetDefinition.model_validate({"id": "w1", "dashboard_id": "", "title": "Польоти", "query": {"metrics": [{"key": "flights"}], "date_range": {"from": "2026-10-01", "to": "2026-10-02"}}, **changes})

    def test_personal_widget_changes_are_recorded_with_their_author(self):
        from app.bi.models import DashboardDefinition
        self.store.save("dashboards", DashboardDefinition(id="d1", name="Мій", slug="mine", type="DASHBOARD", layout_mode="CUSTOMIZABLE"), actor="admin-1")
        saved = self.store.save_personal_widget("d1", "user-7", self.widget())
        self.store.save_personal_widget("d1", "user-7", saved.model_copy(update={"title": "Вильоти"}))
        self.store.delete_personal_widget("d1", "user-7", "w1", 2)
        entries = activity.history(self.store, "widget", "w1")
        self.assertEqual([(item["action"], item["actor_id"]) for item in entries], [("delete", "user-7"), ("update", "user-7"), ("create", "user-7")])
        self.assertEqual(entries[1]["before"]["title"], "Польоти")
        self.assertEqual(entries[1]["after"]["title"], "Вильоти")
        self.assertEqual(activity.history(self.store, "dashboard", "d1")[0]["actor_id"], "admin-1")

    def test_usage_is_counted_per_day_and_lists_what_nobody_opened(self):
        from app.bi.models import DashboardDefinition
        self.store.save("dashboards", DashboardDefinition(id="d1", name="Мій", slug="mine", type="DASHBOARD", layout_mode="CUSTOMIZABLE"), actor="admin-1")
        self.store.save_personal_widget("d1", "user-7", self.widget())
        self.store.save_personal_widget("d1", "user-7", self.widget(id="w2", revision=0))
        for _ in range(3):
            activity.touch(self.store, [("dashboard", "d1"), ("widget", "w1")], total_ms=30)
        activity.touch(self.store, [("widget", "crew-ranking")], total_ms=10)
        report = activity.usage(self.store, 30)
        runs = {(item["entity_type"], item["entity_id"]): item["runs"] for item in report["items"]}
        self.assertEqual((runs[("dashboard", "d1")], runs[("widget", "w1")], runs[("widget", "w2")], runs[("widget", "crew-ranking")]), (3, 3, 0, 1))
        self.assertEqual([item["entity_id"] for item in report["unused"] if item["entity_type"] == "widget"], ["w2"])
        self.assertEqual(next(item for item in report["items"] if item["entity_id"] == "w1")["avg_ms"], 30.0)


if __name__ == "__main__":
    unittest.main()
