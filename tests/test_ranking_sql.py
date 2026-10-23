import unittest
from unittest import mock

from app.bi import ranking_sql
from app.bi.engine import _plain, default_metric_definitions


class Service:
    production_loader = True
    store = object()  # no policy rules

    def today(self):
        from datetime import date
        return date(2026, 10, 22)


def definitions(**changes):
    result = {item.key: dict(_plain(item)) for item in default_metric_definitions()}
    for key, values in changes.items():
        result[key].update(values)
    return result


class Calculator:
    """Success from the source flag; a lost unsuccessful flight is penalized."""

    def __init__(self, config):
        pass

    def successful(self, row, events, losses):
        return row["is_effective"] is True

    def penalized(self, row, events, losses):
        return row["is_effective"] is not True and bool(losses)


def group(day, flights, effective=True, lost=False, team="A", company="11", purpose="Удар"):
    return {"kind": "flights", "category": "FPV", "department": "1", "company": company, "team": team, "purpose": purpose, "day": day,
            "is_effective": effective, "main_result": None, "lost": int(lost), "results": [], "flights": str(flights), "lost_devices": "0"}


def losses(team, day, lost):
    return {"kind": "losses", "category": "FPV", "department": "1", "company": None, "team": team, "purpose": "Удар", "day": day,
            "is_effective": None, "main_result": None, "lost": 0, "results": [], "flights": "0", "lost_devices": str(lost)}


class SupportTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("app.bi.sql_datasets.get_dataset_store")
        patcher.start().return_value.published.return_value = {"version": 1}
        self.addCleanup(patcher.stop)

    def test_reads_system_metrics_with_flight_and_clock_filters(self):
        filters = [{"field": "purpose", "operator": "in", "value": ["Удар"]}, {"field": "local_timestamp", "operator": "gte", "value": 1760000000.0}]
        self.assertTrue(ranking_sql.supported(Service(), definitions(), filters))

    def test_uses_the_engine_for_injected_sources_edited_metrics_and_event_filters(self):
        class Injected(Service):
            production_loader = False
        self.assertFalse(ranking_sql.supported(Injected(), definitions(), []))
        self.assertFalse(ranking_sql.supported(Service(), definitions(effective={"filters": []}), []))
        self.assertFalse(ranking_sql.supported(Service(), definitions(), [{"field": "result", "operator": "in", "value": ["Знищено"]}]))
        self.assertFalse(ranking_sql.supported(Service(), definitions(), [{"field": "local_timestamp", "operator": "in", "value": [1]}]))

    def test_filters_become_dataset_parameters(self):
        values = ranking_sql._filter_values([
            {"field": "purpose", "operator": "in", "value": ["Удар", "Мінування"]}, {"field": "purpose", "operator": "eq", "value": "Удар"},
            {"field": "asset", "operator": "not_in", "value": ["X"]}, {"field": "asset", "operator": "neq", "value": "Y"},
            {"field": "local_timestamp", "operator": "gte", "value": 10}, {"field": "local_timestamp", "operator": "gte", "value": 20},
            {"field": "local_timestamp", "operator": "lte", "value": 90},
        ])
        self.assertEqual((values["purpose_in"], values["device_out"], values["time_from"], values["time_to"], values["category_in"]), (["Удар"], ["X", "Y"], 20.0, 90.0, []))
        self.assertIsNone(ranking_sql._filter_values([{"field": "purpose", "operator": "in", "value": ["A"]}, {"field": "purpose", "operator": "in", "value": ["B"]}]))
        self.assertIsNone(ranking_sql._filter_values([{"field": "purpose", "operator": "eq", "value": None}]))


class ReadTests(unittest.TestCase):
    def read(self, classes, lost):
        body = {"date_range": {"from": "2026-10-01", "to": "2026-10-02"}}
        with mock.patch.object(ranking_sql, "_groups", return_value=classes + lost), \
                mock.patch("app.services.purpose_kpi.PurposeKpiCalculator", Calculator), \
                mock.patch("app.core.kpi_configuration.current_kpi_snapshot"):
            return ranking_sql.read(Service(), body, [])

    def test_groups_become_the_engines_daily_rows(self):
        daily, companies = self.read(
            [group("2026-10-01", 7), group("2026-10-01", 2, effective=False, lost=True), group("2026-10-01", 1, effective=False), group("2026-10-02", 3)],
            [losses("A", "2026-10-01", 4)],
        )
        by_day = {row["date"]: row for row in daily}
        # 7 successful, 2 lost without success (penalized), 1 plain failure.
        self.assertEqual({key: by_day["2026-10-01"][key] for key in ("flights", "effective_net", "lost_devices")}, {"flights": 10, "effective_net": 5, "lost_devices": 4})
        self.assertEqual({key: by_day["2026-10-02"][key] for key in ("flights", "effective_net", "lost_devices")}, {"flights": 3, "effective_net": 3, "lost_devices": 0})
        self.assertEqual(companies, [{"department": "1", "group": "11", "team": "A", "flights": 13}])

    def test_losses_without_flights_still_make_a_row(self):
        daily, companies = self.read([], [losses("B", "2026-10-02", 1)])
        self.assertEqual([(row["team"], row["flights"], row["lost_devices"]) for row in daily], [("B", 0, 1)])
        self.assertEqual(companies, [])


if __name__ == "__main__":
    unittest.main()
