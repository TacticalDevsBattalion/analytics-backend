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

    def test_heavy_computations_are_limited(self):
        with patch.object(memory, '_slots', threading.BoundedSemaphore(2)):
            active, peak, lock = [0], [0], threading.Lock()

            def work():
                with memory.heavy_slot():
                    with lock:
                        active[0] += 1
                        peak[0] = max(peak[0], active[0])
                    time.sleep(0.05)
                    with lock:
                        active[0] -= 1
            threads = [threading.Thread(target=work) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(peak[0], 2)


if __name__ == '__main__':
    unittest.main()
