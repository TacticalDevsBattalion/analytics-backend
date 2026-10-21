import unittest

from app.bi import access_profiles
from app.bi.engine import QueryPermissionError
from app.bi.ranking import crew_ranking
from app.bi.security import Principal
from app.core.ranking_configuration import RankingConfiguration


class FakeService:
    """Daily rows (category × battalion × crew × purpose × day) and the crews' companies."""

    def __init__(self, rows):
        self.rows = rows
        self.requests = []

    def _definitions(self):
        return {"flights": {"aggregation": "COUNT_DISTINCT"}, "effective_net": {"aggregation": "FORMULA"}, "lost_devices": {"aggregation": "COUNT"}}, []

    def _validated(self, request, principal, definitions):
        return request

    def execute(self, request, principal):
        self.requests.append((request, principal))
        if any(item["field"] == "date" for item in request["dimensions"]):
            return {"rows": self.rows}
        companies = {}
        for row in self.rows:
            key = (row["department"], row["_company"], row["team"])
            companies[key] = companies.get(key, 0) + row["flights"]
        return {"rows": [{"department": d, "group": g, "team": t, "flights": f} for (d, g, t), f in companies.items()]}


def day(category, battalion, company, crew, date, flights, effective, lost=0, purpose="Динамічне ураження"):
    return {"category": category, "department": battalion, "_company": company, "team": crew, "purpose": purpose, "date": date, "flights": flights, "effective_net": effective, "lost_devices": lost}


ROWS = [
    # A: 10 strikes a day over 2 days, 80 % effective, no losses — best on every criterion.
    day("FPV", "1", "11", "A", "2026-10-01", 10, 8), day("FPV", "1", "11", "A", "2026-10-02", 10, 8),
    # B: 5 a day, 60 %, one loss.
    day("FPV", "1", "11", "B", "2026-10-01", 5, 3, 1), day("FPV", "1", "11", "B", "2026-10-02", 5, 3),
    # C: 10 flights in one day, 50 %, two losses.
    day("FPV", "1", "12", "C", "2026-10-01", 10, 5, 2),
    # D: other battalion, 6 a day, 100 %, no losses.
    day("FPV", "2", "21", "D", "2026-10-01", 6, 6), day("FPV", "2", "21", "D", "2026-10-03", 6, 6),
    # E: too few flights to be ranked.
    day("FPV", "2", "21", "E", "2026-10-01", 2, 2),
    day("Mavic", "1", "11", "F", "2026-10-01", 6, 3, purpose="Розвідка"), day("Mavic", "2", "21", "G", "2026-10-01", 8, 8, purpose="Розвідка"),
]
BODY = {"date_range": {"from": "2026-10-01", "to": "2026-10-20"}, "filters": [{"field": "bbak", "operator": "in", "value": ["1"]}, {"field": "purpose", "operator": "eq", "value": "Удар"}]}
SETTINGS = RankingConfiguration.model_validate({"mission_groups": [
    {"id": "strike", "title": "Ударні", "purposes": ["Динамічне ураження"]},
    {"id": "logistics", "title": "Логістика", "purposes": ["Доставка"]},
    {"id": "mining", "title": "Мінування", "purposes": ["Мінування"]},
    {"id": "recon", "title": "Розвідка", "purposes": ["Розвідка"]},
]})


def principal(profile, battalion=None, company=None):
    return Principal("u", "viewer", access_profiles.permissions_for(profile, False), access_profiles.scope_for(profile, battalion, company), "EXACT")


def rank(rows, who, settings=SETTINGS):
    return crew_ranking(FakeService(rows), who, BODY, settings)


def table(result, mission="all", category=None):
    return {item["crew"]: item for item in result["rows"] if item["mission"] == mission and (category is None or item["category"] == category)}


class RankingTests(unittest.TestCase):
    def test_one_score_combines_flights_per_day_effectiveness_and_losses(self):
        result = rank(ROWS, principal("command"))
        fpv = table(result, "strike", "FPV")
        self.assertEqual(set(fpv), {"A", "B", "C", "D"})  # E has 2 flights
        self.assertEqual((fpv["A"]["avg_flights"], fpv["A"]["efficiency"], fpv["A"]["lost"], fpv["A"]["active_days"]), (10, 80, 0, 2))
        self.assertEqual(fpv["C"]["avg_flights"], 10)  # per day it flew, not per day of the period
        # A: avg 10 (shared best with C), 80 % (2nd), no losses (shared best with D).
        self.assertEqual(fpv["A"]["parts"], {"avg_flights": 83.3, "efficiency": 66.7, "loss_rate": 83.3})
        self.assertEqual([item["crew"] for item in sorted(fpv.values(), key=lambda item: item["brigade_place"])], ["A", "D", "C", "B"])
        # One kind of mission: the overall score is that group's score.
        self.assertEqual(table(result, "all", "FPV")["A"]["score"], fpv["A"]["score"])
        self.assertEqual([item["id"] for item in result["missions"]], ["all", "strike", "recon"])

    def test_safe_missions_are_ranked_apart_and_mixed_crews_get_a_flight_weighted_score(self):
        rows = [
            # S: strikes only, 50 %. L: deliveries only, 100 % — safe missions, never compared with S.
            day("Бомбери", "1", "11", "S", "2026-10-01", 10, 5, 1), day("Бомбери", "1", "11", "S2", "2026-10-01", 10, 3, 3),
            day("Бомбери", "1", "11", "L", "2026-10-01", 10, 10, purpose="Доставка"), day("Бомбери", "1", "11", "L2", "2026-10-01", 10, 9, purpose="Доставка"),
            # M: 10 strikes (worse than S) and 30 deliveries (best): scored on both, deliveries weigh 3×.
            day("Бомбери", "1", "12", "M", "2026-10-01", 10, 2, 4), day("Бомбери", "1", "12", "M", "2026-10-02", 30, 30, purpose="Доставка"),
            # Too few mining flights for that group; they still count nowhere else.
            day("Бомбери", "1", "12", "M", "2026-10-03", 2, 2, purpose="Мінування"),
        ]
        result = rank(rows, principal("command"))
        strike, logistics, overall = table(result, "strike"), table(result, "logistics"), table(result, "all")
        self.assertEqual(set(strike), {"S", "S2", "M"})
        self.assertEqual(set(logistics), {"L", "L2", "M"})
        self.assertNotIn("mining", {item["mission"] for item in result["rows"]})
        self.assertEqual(overall["M"]["missions"], {"strike": {"score": strike["M"]["score"], "flights": 10}, "logistics": {"score": logistics["M"]["score"], "flights": 30}})
        self.assertAlmostEqual(overall["M"]["score"], round((strike["M"]["score"] * 10 + logistics["M"]["score"] * 30) / 40, 1))
        self.assertEqual(overall["M"]["flights"], 40)
        self.assertEqual(overall["M"]["active_days"], 2)
        # The delivery crew's 100 % does not beat the strike crew by default: each is scored in its own group.
        self.assertEqual(overall["L"]["score"], logistics["L"]["score"])

    def test_losses_can_be_left_out_for_a_category(self):
        rows = [day("FPV", "1", "11", "A", "2026-10-01", 10, 5, 10), day("FPV", "1", "11", "B", "2026-10-01", 10, 5, 2)]
        counted = table(rank(rows, principal("command")), "strike")
        self.assertGreater(counted["B"]["score"], counted["A"]["score"])
        settings = SETTINGS.model_copy(deep=True)
        settings.mission_groups[0].ignore_losses_for = ["fpv "]
        ignored = table(rank(rows, principal("command"), settings), "strike")
        self.assertEqual(ignored["A"]["score"], ignored["B"]["score"])
        self.assertNotIn("loss_rate", ignored["A"]["parts"])

    def test_company_sees_only_its_crews_with_places_across_the_brigade(self):
        service = FakeService(ROWS)
        result = crew_ranking(service, principal("company", "1", "11"), BODY, SETTINGS)
        self.assertFalse(result["full_list"])
        fpv = table(result, "strike", "FPV")
        self.assertEqual(set(fpv), {"A", "B"})  # never C (other company) or D (other battalion)
        self.assertEqual((fpv["A"]["company_place"], fpv["A"]["battalion_place"], fpv["A"]["brigade_place"], fpv["A"]["brigade_total"]), (1, 1, 1, 4))
        self.assertEqual((fpv["B"]["company_place"], fpv["B"]["battalion_place"], fpv["B"]["battalion_total"], fpv["B"]["brigade_place"]), (2, 3, 3, 4))
        self.assertNotIn("'D'", str(result["rows"]))
        # Organizational filters would narrow the brigade; other filters stay. Days and purposes are dimensions.
        request = service.requests[0][0]
        self.assertEqual([item["field"] for item in request["filters"]], ["purpose"])
        self.assertIn({"field": "date", "granularity": "day"}, request["dimensions"])
        self.assertIn({"field": "purpose"}, request["dimensions"])
        self.assertEqual(service.requests[0][1].scope.scope_type, "ALL")

    def test_battalion_and_command_views(self):
        battalion = rank(ROWS, principal("battalion", "2"))
        self.assertEqual({item["crew"] for item in battalion["rows"]}, {"D", "G"})
        command = rank(ROWS, principal("command"))
        self.assertTrue(command["full_list"])
        self.assertEqual(set(table(command)), {"A", "B", "C", "D", "F", "G"})

    def test_equal_scores_are_ordered_by_flights_and_permissions(self):
        rows = [day("Mavic", "1", "11", "A", "2026-10-01", 6, 6), day("Mavic", "1", "11", "B", "2026-10-01", 6, 6),
                day("Mavic", "1", "11", "B", "2026-10-02", 6, 6)]
        result = rank(rows, principal("command"))
        self.assertEqual({item["crew"]: item["brigade_place"] for item in result["rows"] if item["mission"] == "all"}, {"B": 1, "A": 2})
        with self.assertRaises(QueryPermissionError):
            rank(ROWS, Principal("u", "viewer", frozenset({"metric.view"}), principal("command").scope))

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            RankingConfiguration.model_validate({"mission_groups": [{"id": "a", "title": "A", "purposes": ["Доставка"]}, {"id": "b", "title": "B", "purposes": [" доставка"]}]})
        with self.assertRaises(ValueError):
            RankingConfiguration.model_validate({"weights": {"avg_flights": 0, "efficiency": 0, "loss_rate": 0}})
        self.assertEqual([group.id for group in RankingConfiguration().mission_groups], ["strike", "logistics", "mining", "recon"])


if __name__ == "__main__":
    unittest.main()
