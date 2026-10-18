"""Success rules by flight records, and the lost-drone "minus" on effectiveness."""
import copy
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.bi.engine import QueryEngine, seed_default_metrics
from app.bi.models import DataScope
from app.bi.security import Principal
from app.bi.store import BiStore
from app.core.cache import cache
from app.core.kpi_configuration import KpiConfiguration, KpiSnapshot, PurposeKpiRule, kpi_fingerprint, pin_kpi_snapshot
from app.services.purpose_kpi import PurposeKpiCalculator

RECON = PurposeKpiRule(purpose="Розвідка", success_mode="events", successful_results=["Виявлено", "Фіксація", "Підтверджено"], loss_rule="penalty")


def flight(identifier, purpose="Розвідка", **values):
    return {"flight_id": identifier, "date": "2026-10-01", "main_purpose": purpose, "main_result": "Розвідка", "is_effective": True, "bbak_id": 1, "crew": "C1", "device_type": "Mavic", **values}


def event(identifier, result):
    return {"flight_id": identifier, "date": "2026-10-01", "result": result}


# found: detection · quiet: nothing found · lost: nothing found, drone lost ·
# found_lost: detection, then lost · strike: another purpose, source flag.
FLIGHTS = [flight("found"), flight("quiet"), flight("lost"), flight("found_lost"), flight("strike", "Динамічне ураження", main_result="Уражено")]
EVENTS = [event("found", "Розвідка"), event("found", "виявлено "), event("quiet", "Розвідка"), event("lost", "Розвідка"), event("lost", "Втрата борту"), event("found_lost", "Виявлено")]
LOSSES = [{"flight_id": "found_lost", "date": "2026-10-01"}]


class CalculatorTests(unittest.TestCase):
    def calculator(self, **rule):
        return PurposeKpiCalculator(KpiConfiguration(purpose_rules=[RECON.model_copy(update=rule)]), events=EVENTS, losses=LOSSES)

    def test_reconnaissance_counts_only_when_something_was_found(self):
        calculator = self.calculator()
        self.assertEqual({row["flight_id"]: calculator.successful(row) for row in FLIGHTS}, {"found": True, "quiet": False, "lost": False, "found_lost": True, "strike": True})

    def test_losses_come_from_loss_records_and_loss_results(self):
        calculator = self.calculator()
        self.assertEqual({row["flight_id"] for row in FLIGHTS if calculator.lost(row)}, {"lost", "found_lost"})
        self.assertTrue(calculator.lost(flight("x", main_result="Втрата борту")))

    def test_a_penalty_rule_takes_a_minus_only_for_an_unsuccessful_lost_flight(self):
        calculator = self.calculator()
        self.assertEqual([row["flight_id"] for row in FLIGHTS if calculator.penalized(row)], ["lost"])
        self.assertEqual(calculator.weights(FLIGHTS[2]), (-1.0, 1.0))
        self.assertEqual(calculator.weights(FLIGHTS[1]), (0.0, 1.0))

    def test_an_unsuccessful_rule_drops_lost_flights_without_a_minus(self):
        calculator = self.calculator(loss_rule="unsuccessful")
        self.assertEqual({row["flight_id"] for row in FLIGHTS if calculator.successful(row)}, {"found", "strike"})
        self.assertFalse(any(calculator.penalized(row) for row in FLIGHTS))

    def test_records_can_be_given_per_flight(self):
        calculator = PurposeKpiCalculator(KpiConfiguration(purpose_rules=[RECON]))
        self.assertTrue(calculator.successful(flight("x"), [event("x", "Фіксація")], []))
        self.assertTrue(calculator.penalized(flight("x"), [event("x", "Розвідка")], [{"flight_id": "x"}]))

    def test_preview_shows_the_minus(self):
        recon = next(item for item in self.calculator().preview(FLIGHTS, "clickhouse").purposes if item.purpose == "Розвідка")
        self.assertEqual((recon.flights, recon.successful_flights, recon.penalized_flights, recon.success_rate, recon.loss_rule), (4, 2, 1, 25.0, "penalty"))

    def test_an_events_rule_needs_results_and_the_fingerprint_follows_the_loss_rule(self):
        with self.assertRaises(ValidationError):
            PurposeKpiRule(purpose="Розвідка", success_mode="events")
        plain = KpiConfiguration(purpose_rules=[RECON.model_copy(update={"loss_rule": "ignore"})])
        self.assertNotEqual(kpi_fingerprint(plain), kpi_fingerprint(KpiConfiguration(purpose_rules=[RECON])))


class EffectivenessMetricTests(unittest.TestCase):
    def setUp(self):
        cache.clear("bi")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = BiStore(Path(temporary.name) / "bi.sqlite3")
        seed_default_metrics(self.store)
        dataset = {"flights": FLIGHTS, "events": EVENTS, "ammunition": [], "lost_devices": LOSSES}
        self.engine = QueryEngine(self.store, lambda start, end: copy.deepcopy(dataset))
        self.principal = Principal("all", "viewer", frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")

    def run_with(self, rules, dimensions=()):
        config = KpiConfiguration(purpose_rules=rules)
        with pin_kpi_snapshot(KpiSnapshot(config, kpi_fingerprint(config), 1)):
            return self.engine.execute({"metrics": [{"key": key} for key in ("flights", "effective", "penalized", "efficiency")], "dimensions": [{"field": field} for field in dimensions], "date_range": {"from": "2026-10-01", "to": "2026-10-01"}}, self.principal)["rows"]

    def test_without_rules_effectiveness_is_unchanged(self):
        row = self.run_with([])[0]
        self.assertEqual((row["flights"], row["effective"], row["penalized"], row["efficiency"]), (5, 5, 0, 100))

    def test_reconnaissance_rule_and_minus_lower_effectiveness(self):
        row = self.run_with([RECON])[0]
        # 3 successful (found, found_lost, strike) − 1 minus (lost) of 5 flights.
        self.assertEqual((row["effective"], row["penalized"]), (3, 1))
        self.assertAlmostEqual(row["efficiency"], 40)

    def test_effectiveness_can_go_below_zero(self):
        rows = {row["purpose"]: row for row in self.run_with([RECON.model_copy(update={"successful_results": ["Підтверджено"]})], dimensions=("purpose",))}
        # Nothing counts as found, so both lost flights are a minus: (0 − 2) ÷ 4.
        self.assertAlmostEqual(rows["Розвідка"]["efficiency"], -50)


if __name__ == "__main__":
    unittest.main()
