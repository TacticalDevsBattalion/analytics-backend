import threading
import time
import unittest
from unittest.mock import patch

from app.bi.engine import _share_strings
from app.core import memory


class MemoryGuardTests(unittest.TestCase):
    def test_equal_text_values_become_one_object(self):
        first, second = 'Кафедра ' + str(1), 'Кафедра ' + str(1)
        self.assertIsNot(first, second)
        memo = {}
        dataset = _share_strings({'flights': [{'department': first, 'n': 1}], 'events': [{'department': second, 'n': None}]}, memo)
        self.assertIs(dataset['flights'][0]['department'], dataset['events'][0]['department'])
        self.assertEqual(dataset['events'][0], {'department': 'Кафедра 1', 'n': None})

    def test_pressure_follows_the_container_limit(self):
        with patch.object(memory, 'usage', return_value=(1500, 2000)):
            self.assertTrue(memory.under_pressure())
        with patch.object(memory, 'usage', return_value=(1000, 2000)):
            self.assertFalse(memory.under_pressure())
        with patch.object(memory, 'usage', return_value=(10 ** 12, None)):
            self.assertFalse(memory.under_pressure())

    def test_configured_limit_applies_without_cgroup_limit(self):
        with patch.object(memory, '_read', side_effect=lambda path: 900 * 2 ** 20 if path.endswith('memory.current') else None), patch.dict('os.environ', {'MEMORY_LIMIT_MB': '1000'}):
            self.assertEqual(memory.usage(), (900 * 2 ** 20, 1000 * 2 ** 20))
            self.assertTrue(memory.under_pressure())

    def run_concurrently(self, periods):
        active, peak, overlaps, lock = [0], [0], [], threading.Lock()

        def work(days):
            with memory.heavy_slot(days):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                    overlaps.append((days, active[0]))
                time.sleep(0.05)
                with lock:
                    active[0] -= 1
        threads = [threading.Thread(target=work, args=(days,)) for days in periods]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return peak[0], overlaps

    def test_heavy_computations_are_limited(self):
        with patch.object(memory, '_slots', memory.Slots(2)):
            peak, _ = self.run_concurrently([7] * 6)
            self.assertEqual(peak, 2)

    def test_a_long_period_runs_alone(self):
        with patch.object(memory, '_slots', memory.Slots(2)):
            peak, overlaps = self.run_concurrently([7, 134, 7, 7, 134, 30])
            self.assertEqual(peak, 2)
            # When a four-month computation started, nothing else was running.
            self.assertTrue(all(active == 1 for days, active in overlaps if days == 134))

    def test_period_days(self):
        self.assertEqual(memory.period_days({'from': '2026-05-20', 'to': '2026-09-30'}), 134)
        self.assertIsNone(memory.period_days(None))

if __name__ == '__main__':
    unittest.main()
