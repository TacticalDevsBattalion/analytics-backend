import unittest

from app.bi import access_profiles
from app.bi.engine import QueryPermissionError
from app.bi.ranking import crew_ranking
from app.bi.security import Principal


class FakeService:
    """Rows as the engine returns them for category × battalion × company × crew."""

    def __init__(self, rows):
        self.rows = rows
        self.requests = []

    def _definitions(self):
        return {"efficiency": {"title": "Результативність, %", "aggregation": "RATIO", "format": {"type": "percent"}},
                "flights": {"title": "Вильоти", "aggregation": "COUNT_DISTINCT"},
                "flights_per_result": {"title": "Вильотів на ціль", "aggregation": "RATIO", "direction": "LOWER_IS_BETTER"}}, []

    def _validated(self, request, principal, definitions):
        return request

    def execute(self, request, principal):
        self.requests.append((request, principal))
        return {"rows": self.rows}


def row(category, battalion, company, crew, value, flights=10, metric="efficiency"):
    return {"category": category, "department": battalion, "group": company, "team": crew, metric: value, "flights": flights}


ROWS = [
    row("FPV", "1", "11", "A", 60), row("FPV", "1", "11", "B", 50), row("FPV", "1", "12", "C", 50),
    row("FPV", "2", "21", "D", 70), row("FPV", "2", "21", "E", 90, flights=2),  # too few flights for a ratio
    row("Mavic", "1", "11", "F", 30), row("Mavic", "2", "21", "G", 40),
]
BODY = {"date_range": {"from": "2026-10-01", "to": "2026-10-20"}, "metric": "efficiency", "filters": [{"field": "bbak", "operator": "in", "value": ["1"]}, {"field": "purpose", "operator": "eq", "value": "Удар"}]}


def principal(profile, battalion=None, company=None):
    return Principal("u", "viewer", access_profiles.permissions_for(profile, False), access_profiles.scope_for(profile, battalion, company), "EXACT")


class RankingTests(unittest.TestCase):
    def test_company_sees_only_its_crews_with_places_across_the_brigade(self):
        service = FakeService(ROWS)
        result = crew_ranking(service, principal("company", "1", "11"), BODY)
        self.assertFalse(result["full_list"])
        fpv = {item["crew"]: item for item in result["rows"] if item["category"] == "FPV"}
        self.assertEqual(set(fpv), {"A", "B"})  # never C (other company) or D (other battalion)
        self.assertEqual((fpv["A"]["company_place"], fpv["A"]["battalion_place"], fpv["A"]["brigade_place"], fpv["A"]["brigade_total"]), (1, 1, 2, 4))
        # B and C share 50 %: competition ranking gives both the same place.
        self.assertEqual((fpv["B"]["battalion_place"], fpv["B"]["battalion_total"], fpv["B"]["brigade_place"]), (2, 3, 3))
        self.assertNotIn("D", str(result["rows"]))
        # Organizational filters would narrow the brigade; other filters stay.
        request = service.requests[0][0]
        self.assertEqual([item["field"] for item in request["filters"]], ["purpose"])
        self.assertEqual(service.requests[0][1].scope.scope_type, "ALL")

    def test_battalion_and_command_views(self):
        battalion = crew_ranking(FakeService(ROWS), principal("battalion", "2"), BODY)
        self.assertEqual({item["crew"] for item in battalion["rows"]}, {"D", "G"})
        command = crew_ranking(FakeService(ROWS), principal("command"), BODY)
        self.assertTrue(command["full_list"])
        self.assertEqual({item["crew"] for item in command["rows"]}, {"A", "B", "C", "D", "F", "G"})

    def test_lower_is_better_and_permissions(self):
        rows = [row("FPV", "1", "11", "A", 3, metric="flights_per_result"), row("FPV", "1", "11", "B", 8, metric="flights_per_result")]
        result = crew_ranking(FakeService(rows), principal("battalion", "1"), {**BODY, "metric": "flights_per_result"})
        self.assertEqual({item["crew"]: item["brigade_place"] for item in result["rows"]}, {"A": 1, "B": 2})
        with self.assertRaises(QueryPermissionError):
            crew_ranking(FakeService(ROWS), Principal("u", "viewer", frozenset({"metric.view"}), principal("command").scope), BODY)


if __name__ == "__main__":
    unittest.main()
