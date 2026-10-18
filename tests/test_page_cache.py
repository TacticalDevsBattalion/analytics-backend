import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api.analytics_pages import PageQuery
from app.bi import page_cache, prewarm
from app.bi.cache_policy import CachePolicy
from app.core.config import Settings
from app.core.shared_cache import SharedCache
from app.core.user_accounts import CreateUserRequest, UserAccountStore

TODAY = date(2026, 10, 18)


class Definition:
    def __init__(self, widgets):
        self.widgets = widgets

    def model_dump(self, mode='json'):
        return {'widgets': self.widgets}


class Service:
    cache_policy = CachePolicy()

    def today(self):
        return TODAY

    def _definitions(self):
        return {'flights': {'key': 'flights'}, 'score': {'key': 'score', 'kind': 'kpi'}}, []

    def semantic_signature(self):
        return 'source-1'


class User:
    def __init__(self, user_id='u1'):
        self.user_id = user_id

    def cache_key(self):
        return {'user_id': self.user_id}


def body(start=TODAY, end=TODAY, **times):
    return PageQuery.model_validate({'date_range': {'from': start, 'to': end}, **times})


class PageCacheTests(unittest.TestCase):
    def setUp(self):
        settings = Settings(_env_file=None, cache_enabled=True, redis_url='')
        self.cache = SharedCache(None, settings_getter=lambda: settings)
        self.cache._config = lambda: SimpleNamespace(enabled=True, stale_if_error_seconds=0, stale_while_revalidate=False, l1_max_entries=64, l1_max_bytes=10**8, l1_ttl_seconds=60, max_entries=64)
        patcher = patch.object(page_cache, 'cache', self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def fake_query(self, status='success'):
        def run(definition, query, user, service):
            self.calls.append((query.date_range.date_from, user.user_id))
            return {'dashboard_id': 'd', 'revision': 1, 'results': [{'widget_id': 'w', 'status': status, 'data': {'value': 'Знищено', 'day': date(2026, 10, 1)}}]}
        return run

    def test_a_ready_page_is_returned_as_json_without_recomputing(self):
        with patch.object(page_cache, 'query_page', self.fake_query()), patch('app.bi.catalog.dimension_labels', return_value={}):
            first = page_cache.page_json(Definition(['w']), body(), User(), Service())
            second = page_cache.page_json(Definition(['w']), body(), User(), Service())
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)['results'][0]['data'], {'value': 'Знищено', 'day': '2026-10-01'})
        self.assertEqual(len(self.calls), 1)

    def test_users_periods_times_and_widgets_have_their_own_pages(self):
        with patch.object(page_cache, 'query_page', self.fake_query()), patch('app.bi.catalog.dimension_labels', return_value={}):
            page_cache.page_json(Definition(['w']), body(), User(), Service())
            page_cache.page_json(Definition(['w']), body(), User('u2'), Service())
            page_cache.page_json(Definition(['w']), body(date(2026, 10, 17)), User(), Service())
            page_cache.page_json(Definition(['w']), body(time_from='15:00', time_to='15:00', start=date(2026, 10, 17)), User(), Service())
            page_cache.page_json(Definition(['w', 'x']), body(), User(), Service())
        self.assertEqual(len(self.calls), 5)

    def test_failed_widgets_are_not_kept(self):
        with patch.object(page_cache, 'query_page', self.fake_query('error')), patch('app.bi.catalog.dimension_labels', return_value={}):
            page_cache.page_json(Definition(['w']), body(), User(), Service())
            page_cache.page_json(Definition(['w']), body(), User(), Service())
        self.assertEqual(len(self.calls), 2)

    def test_invalidating_a_day_or_a_metric_drops_the_page(self):
        with patch.object(page_cache, 'query_page', self.fake_query()), patch('app.bi.catalog.dimension_labels', return_value={}):
            page_cache.page_json(Definition(['w']), body(date(2026, 10, 10), TODAY), User(), Service())
            self.cache.invalidate_tags(['data:2026-10-12'])
            page_cache.page_json(Definition(['w']), body(date(2026, 10, 10), TODAY), User(), Service())
            self.cache.invalidate_tags(['kpi:score'])
            page_cache.page_json(Definition(['w']), body(date(2026, 10, 10), TODAY), User(), Service())
            self.cache.invalidate_tags(['data:2026-09-01'])
            page_cache.page_json(Definition(['w']), body(date(2026, 10, 10), TODAY), User(), Service())
        # data:epoch moves with any day, so even an unrelated day refreshes the page.
        self.assertEqual(len(self.calls), 4)

    def test_periods_past_the_cache_horizon_are_computed_each_time(self):
        with patch.object(page_cache, 'query_page', self.fake_query()):
            page_cache.page_json(Definition(['w']), body(date(2025, 1, 1), date(2025, 1, 2)), User(), Service())
            page_cache.page_json(Definition(['w']), body(date(2025, 1, 1), date(2025, 1, 2)), User(), Service())
        self.assertEqual(len(self.calls), 2)


class QuickPeriodTests(unittest.TestCase):
    def test_periods_match_the_filter_bar(self):
        settings = prewarm.PrewarmSettings(days=(7,), reporting_cutoffs=('15:00',))
        periods = [(p['date_range']['from'].isoformat(), p['date_range']['to'].isoformat(), p.get('time_from')) for p in prewarm.quick_periods(TODAY, settings)]
        self.assertEqual(periods, [
            ('2026-10-18', '2026-10-18', None), ('2026-10-17', '2026-10-18', '15:00'),
            ('2026-10-17', '2026-10-17', None), ('2026-10-16', '2026-10-17', '15:00'),
            ('2026-10-12', '2026-10-18', None), ('2026-10-11', '2026-10-18', '15:00'),
            ('2026-10-01', '2026-10-18', None), ('2026-09-30', '2026-10-18', '15:00'),
        ])
        for period in prewarm.quick_periods(TODAY, settings):
            PageQuery.model_validate(period).bounded_filters()

    def test_settings_come_from_the_environment(self):
        with patch.dict('os.environ', {'CACHE_PREWARM_DAYS': '7,30', 'CACHE_PREWARM_REPORTING_CUTOFFS': '16:30', 'CACHE_PREWARM_INTERVAL_SECONDS': '10'}):
            settings = prewarm.PrewarmSettings.from_env()
        self.assertEqual((settings.days, settings.reporting_cutoffs, settings.interval_seconds), ((7, 30), ('16:30',), 60))

    def test_a_pass_computes_every_period_for_every_user_and_page(self):
        seen = []
        users = [SimpleNamespace(user_id='a'), SimpleNamespace(user_id='b')]
        with patch.object(prewarm, 'principals', return_value=users), patch.object(prewarm, 'pages_for', return_value=[SimpleNamespace(id='dashboard'), SimpleNamespace(id='statistics')]), \
                patch('app.api.bi_routes.engine', return_value=Service()), patch('app.bi.page_cache.page_json', side_effect=lambda d, b, u, s, serve_stale: seen.append((d.id, u.user_id, serve_stale))):
            result = prewarm.run_once(prewarm.PrewarmSettings(days=(7,), reporting_cutoffs=('15:00',)))
        self.assertEqual(result, {'pages': 32, 'failed': 0, 'complete': True})
        self.assertEqual(len(seen), 32)
        self.assertFalse(any(stale for *_, stale in seen))

    def test_a_pass_stops_on_request(self):
        with patch.object(prewarm, 'principals', return_value=[SimpleNamespace(user_id='a')]), patch.object(prewarm, 'pages_for', return_value=[SimpleNamespace(id='dashboard')]), \
                patch('app.api.bi_routes.engine', return_value=Service()), patch('app.bi.page_cache.page_json'):
            self.assertFalse(prewarm.run_once(should_stop=lambda: True)['complete'])


class ActiveUserTests(unittest.TestCase):
    def test_only_users_with_a_valid_session_are_active(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        now = [1_750_000_000.0]
        store = UserAccountStore(Path(directory.name) / 'users.sqlite3', lambda: now[0])
        password = 'private-test-password-42'
        for name in ('admin', 'viewer', 'idle'):
            store.create_user(CreateUserRequest(username=name, display_name=name, password=password, role='administrator' if name == 'admin' else 'viewer'))
        store.login('admin', password, 'client')
        store.login('viewer', password, 'client')
        self.assertEqual([user.username for user in store.active_users()], ['admin', 'viewer'])
        now[0] += 10 ** 8
        self.assertEqual(store.active_users(), [])


class LeaseTests(unittest.TestCase):
    def test_one_worker_holds_the_lease_and_renews_it(self):
        class Redis:
            def __init__(self):
                self.values = {}
            def set(self, key, value, nx=False, px=None):
                if nx and key in self.values:
                    return None
                self.values[key] = value.encode()
                return True
            def get(self, key):
                return self.values.get(key)
            def pexpire(self, key, px):
                return True
        settings = Settings(_env_file=None, cache_enabled=True)
        cache = SharedCache(Redis(), settings_getter=lambda: settings)
        self.assertTrue(cache.lease('bi.prewarm', 'one', 60))
        self.assertFalse(cache.lease('bi.prewarm', 'two', 60))
        self.assertTrue(cache.lease('bi.prewarm', 'one', 60))
        self.assertTrue(SharedCache(None, settings_getter=lambda: Settings(_env_file=None, redis_url='')).lease('bi.prewarm', 'x', 60))


if __name__ == '__main__':
    unittest.main()
