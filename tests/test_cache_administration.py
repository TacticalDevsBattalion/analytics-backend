import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import bi_routes
from app.bi.cache_admin import CacheInvalidation, CacheWarmRequest, invalidate, start_warming, warm_queries
from app.bi.models import DataScope, MetricDefinition
from app.bi.security import Principal
from app.bi.store import BiNotFound, BiStore
from app.core.cache import cache
from app.core.shared_cache import SharedCacheUnavailable


class CacheAdministrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BiStore(Path(directory.name) / 'bi.sqlite3')
        self.store.save('metrics', MetricDefinition(key='flights', title='Flights', aggregation='COUNT'))
        self.user = Principal('viewer', 'viewer', frozenset({'metric.view'}), DataScope(scope_type='ALL'))
        app = FastAPI()
        app.include_router(bi_routes.router, prefix='/api')
        app.dependency_overrides[bi_routes.principal] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        replacement = patch.object(bi_routes, 'get_bi_store', return_value=self.store)
        replacement.start()
        self.addCleanup(replacement.stop)

    def admin(self):
        self.user = replace(self.user, permissions=frozenset({'metric.view', 'analytics.cache.manage'}))

    def test_admin_permission_and_origin_are_checked(self):
        self.assertEqual(self.client.get('/api/v1/admin/cache/status').status_code, 403)
        self.assertEqual(self.client.post('/api/v1/admin/cache/invalidate', json={'target': 'ALL'}).status_code, 403)
        self.admin()
        status = self.client.get('/api/v1/admin/cache/status')
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()['policy']['horizon_days'], 120)
        denied = self.client.post('/api/v1/admin/cache/invalidate', headers={'Origin': 'https://other.example'}, json={'target': 'ALL'})
        self.assertEqual(denied.status_code, 403)

    def test_period_invalidation_updates_only_selected_days(self):
        body = CacheInvalidation.model_validate({'target': 'PERIOD', 'date_range': {'from': '2026-09-17', 'to': '2026-09-17'}})
        with patch.object(cache, 'invalidate_tags', return_value={'data:2026-09-17': 1}) as update:
            result = invalidate(body)
        update.assert_called_once_with(['data:2026-09-17'])
        self.assertEqual(result['affected_partitions'], 1)

    def test_redis_failure_is_not_a_false_invalidation_success(self):
        self.admin()
        with patch.object(cache, 'invalidate_tags', side_effect=SharedCacheUnavailable()):
            response = self.client.post('/api/v1/admin/cache/invalidate', json={'target': 'METRIC', 'key': 'flights'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('not acknowledged', response.json()['detail'])

    def test_warm_periods_and_persisted_progress(self):
        with patch('app.bi.cache_admin.today', return_value=date(2026, 10, 5)):
            queries = warm_queries(CacheWarmRequest(metric_keys=['flights']))
        self.assertEqual(queries[0].date_range.date_from, date(2026, 10, 1))
        self.assertEqual(queries[1].date_range.date_to, date(2026, 9, 30))
        job = self.store.create_cache_job({'periods': ['CURRENT_MONTH']}, 'admin', 3)
        self.assertEqual(BiStore(self.store.path).cache_job(job['id'])['status'], 'queued')
        self.store.update_cache_job(job['id'], 'running', 1)
        self.store.update_cache_job(job['id'], 'complete', 3)
        self.assertEqual(self.store.cache_job(job['id'])['completed_queries'], 3)
        with self.assertRaises(BiNotFound):
            self.store.cache_job('missing')

    def test_invalid_warming_is_rejected_before_enqueue(self):
        with patch('app.bi.cache_admin.get_bi_store', return_value=self.store), patch('app.bi.cache_admin.Thread') as thread:
            with self.assertRaises(ValueError):
                start_warming(CacheWarmRequest(metric_keys=['not_defined']), self.user)
        thread.assert_not_called()
        with self.assertRaises(ValidationError):
            CacheWarmRequest(periods=['CUSTOM'])
