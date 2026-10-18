import unittest
from datetime import date

from app.api.models import FilterRequest
from app.bi.reports import ScopedReport


def flight(flight_id, device_type, main_result):
    return {"flight_id": flight_id, "date": "2026-10-10", "device_type": device_type, "main_result": main_result, "position": "ІО", "main_purpose": "Ураження"}


def loss(flight_id, device_type, result, device="Борт"):
    return {"flight_id": flight_id, "date": "2026-10-10", "time": "10:00", "device_type": device_type, "result": result, "device": device, "row_hash": f"h-{flight_id}"}


class LostDevicesRuleTests(unittest.TestCase):
    def test_single_use_hits_are_not_losses_and_reusable_count_by_loss_record(self):
        dataset = {
            "flights": [flight("1", "FPV ППО", "Знищено"), flight("2", "FPV ППО", "Технічні несправності"), flight("3", "FPV ППО", "Знищено"),
                        flight("4", "Бомбери", "Знищено"), flight("5", "Бомбери", "Доставлено")],
            "lost_devices": [
                loss("1", "FPV ППО", "Знищено"),               # spent on a kill
                loss("2", "FPV ППО", "Технічні несправності"),  # lost
                loss("3", "FPV ППО", "Детонація в повітрі"),    # flight destroyed the target: spent
                loss("4", "Бомбери", "Втрата борту"),           # reusable: lost despite the kill
                loss("5", "Бомбери", "Втрата борту"),
            ],
        }
        rows = ScopedReport(dataset)._lost_device_rows(FilterRequest(date_from=date(2026, 10, 1), date_to=date(2026, 10, 18)))
        self.assertEqual(sorted((row.category, row.result) for row in rows), [("FPV ППО", "Технічні несправності"), ("Бомбери", "Втрата борту"), ("Бомбери", "Втрата борту")])

    def test_losses_report_also_lists_spent_drones(self):
        dataset = {"flights": [flight("1", "FPV", "Уражено"), flight("2", "FPV", "Впав")], "lost_devices": [loss("1", "FPV", "Уражено"), loss("2", "FPV", "Впав")]}
        rows = ScopedReport(dataset)._lost_device_rows(FilterRequest(date_from=date(2026, 10, 1), date_to=date(2026, 10, 18)), include_spent=True)
        self.assertEqual(sorted((row.category, row.result, row.spent) for row in rows), [("FPV", "Впав", False), ("FPV", "Уражено", True)])
