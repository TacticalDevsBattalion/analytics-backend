"""Pure SQLite/thread tests: no HTTP sockets or external data source required."""
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.bi.cache_admin import CacheWarmRequest, start_warming, warm_queries, warming_limits
from app.bi.engine import QueryError
from app.bi.models import DataScope
from app.bi.security import Principal
from app.bi.store import BiConflict, BiStore


class CacheJobLeaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'bi.sqlite3'
        self.store = BiStore(self.path)
        self.store.ensure_seed()
        self.user = Principal('admin', 'viewer', frozenset({'metric.view', 'analytics.cache.manage'}), DataScope(scope_type='ALL'))

    def create(self, store=None, owner='worker-a', **kwargs):
        return (store or self.store).create_cache_job({'periods': ['CURRENT_MONTH']}, 'admin', 1, lease_owner=owner, **kwargs)

    def test_atomic_global_bound_across_independent_store_instances(self):
        workers = [BiStore(self.path) for _ in range(5)]
        def reserve(index):
            try:
                return self.create(workers[index], owner=f'worker-{index}')['id']
            except BiConflict:
                return None
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(reserve, range(5)))
        self.assertEqual(sum(value is not None for value in results), 2)
        first = next(value for value in results if value is not None)
        self.store.update_cache_job(first, 'complete', 1)
        self.assertEqual(self.create(owner='next')['status'], 'queued')

    def test_heartbeat_extends_live_lease_but_cannot_revive_expired_owner(self):
        instant = time.time()
        with patch('app.bi.store.time.time', return_value=instant):
            job = self.create(lease_seconds=1)
        with patch('app.bi.store.time.time', return_value=instant + 0.8):
            self.assertTrue(self.store.renew_cache_job(job['id'], 'worker-a', 1))
            self.assertFalse(self.store.renew_cache_job(job['id'], 'wrong-owner', 1))
        with patch('app.bi.store.time.time', return_value=instant + 1.2):
            self.assertEqual(BiStore(self.path).cache_job(job['id'])['status'], 'queued')
        with patch('app.bi.store.time.time', return_value=instant + 2):
            self.assertFalse(self.store.renew_cache_job(job['id'], 'worker-a', 1))
            expired = self.store.cache_job(job['id'])
            self.assertEqual(expired['status'], 'failed')
            self.assertEqual(expired['error_code'], 'interrupted')
            with self.assertRaises(BiConflict):
                self.store.update_cache_job(job['id'], 'complete', 1, lease_owner='worker-a')

    def test_stale_jobs_are_cleaned_on_new_worker_start_and_free_reservations(self):
        jobs = [self.create(owner=f'owner-{index}') for index in range(2)]
        with self.store._connection() as connection:
            connection.execute('UPDATE analytics_cache_jobs SET lease_expires_at=0')
        next_worker = BiStore(self.path)
        next_worker.ensure_seed()
        self.assertTrue(all(next_worker.cache_job(job['id'])['status'] == 'failed' for job in jobs))
        self.assertEqual(self.create(next_worker, owner='replacement')['status'], 'queued')

    def test_lease_owner_is_private_and_job_progress_is_monotonic(self):
        job = self.create()
        self.assertFalse({'lease_owner', 'lease_expires_at', 'deadline_at'} & job.keys())
        with self.assertRaises(BiConflict):
            self.store.update_cache_job(job['id'], 'running', 0, lease_owner='other')
        with self.assertRaises(ValueError):
            self.store.update_cache_job(job['id'], 'complete', 0, lease_owner='worker-a')
        self.store.update_cache_job(job['id'], 'complete', 1, lease_owner='worker-a')
        with self.assertRaises(BiConflict):
            self.store.update_cache_job(job['id'], 'running', 0, lease_owner='worker-a')

    def test_upgrade_marks_unleased_old_active_jobs_interrupted(self):
        old_path = self.path.with_name('legacy.sqlite3')
        connection = sqlite3.connect(old_path)
        connection.execute('CREATE TABLE bi_schema_migrations (version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        migrations = Path(__file__).parents[1] / 'app' / 'bi' / 'migrations'
        for migration in sorted(migrations.glob('*.sql')):
            version = int(migration.name.split('_', 1)[0])
            if version > 5:
                break
            connection.executescript(migration.read_text(encoding='utf-8'))
            connection.execute('INSERT INTO bi_schema_migrations VALUES (?,?)', (version, 'legacy'))
            connection.commit()
        connection.execute("INSERT INTO analytics_cache_jobs VALUES ('old',NULL,'{}','running',0,1,NULL,'old','old')")
        connection.commit()
        connection.close()
        upgraded = BiStore(old_path)
        self.assertEqual(upgraded.cache_job('old')['error_code'], 'interrupted')

    def test_warming_dates_are_horizon_bounded_and_future_requests_rejected(self):
        for start, end in [('2020-01-01', '2026-10-05'), ('2026-10-01', '2026-10-06')]:
            body = CacheWarmRequest.model_validate({'periods': ['CUSTOM'], 'date_range': {'from': start, 'to': end}, 'metric_keys': ['flights']})
            with patch('app.bi.cache_admin.today', return_value=date(2026, 10, 5)):
                with self.assertRaises(QueryError):
                    warm_queries(body)
        with self.assertRaises(ValidationError):
            CacheWarmRequest(metric_keys=['x' * 129])
        with patch.dict('os.environ', {'CACHE_WARM_MAX_SECONDS': '9000'}):
            with self.assertRaises(QueryError):
                warming_limits()

    def _wait_final(self, job_id):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            value = self.store.cache_job(job_id)
            if value['status'] in {'complete', 'failed'}:
                for thread in threading.enumerate():
                    if thread.name.endswith(job_id[:8]):
                        thread.join(2)
                        self.assertFalse(thread.is_alive(), 'Warm worker/heartbeat did not stop')
                return value
            time.sleep(0.01)
        self.fail('Warm job did not complete')

    def _start_fake(self, execute, duration, lease, periods=None):
        service = SimpleNamespace(_definitions=lambda: ({}, []), _validated=lambda *args: None, execute=execute)
        with patch('app.bi.cache_admin.QueryEngine', return_value=service), patch('app.bi.cache_admin.get_bi_store', return_value=self.store), patch('app.bi.cache_admin.warming_limits', return_value=(duration, lease)):
            return start_warming(CacheWarmRequest(metric_keys=['flights'], periods=periods or ['CURRENT_MONTH']), self.user)

    def test_background_heartbeat_keeps_long_source_query_reserved(self):
        job = self._start_fake(lambda *args: time.sleep(1.1), 3, 1)
        time.sleep(1.02)
        self.assertEqual(self.store.cache_job(job['id'])['status'], 'running')
        extra = self.create(owner='second')
        with self.assertRaises(BiConflict):
            self.create(owner='third')
        final = self._wait_final(job['id'])
        self.assertEqual(final['status'], 'complete')
        self.store.update_cache_job(extra['id'], 'complete', 1, lease_owner='second')

    def test_runtime_budget_stops_new_queries_after_current_bounded_call(self):
        calls = []
        def execute(*args):
            calls.append(1)
            time.sleep(1.02)
        job = self._start_fake(execute, 1, 1, ['CURRENT_MONTH', 'PREVIOUS_MONTH'])
        final = self._wait_final(job['id'])
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['error_code'], 'timeout')
        self.assertEqual(calls, [1])


if __name__ == '__main__':
    unittest.main()
