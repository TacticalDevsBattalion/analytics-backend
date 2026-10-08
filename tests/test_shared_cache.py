from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import date, datetime, time as dt_time
from types import SimpleNamespace

from pydantic import BaseModel, ValidationError

from app.api.models import Metric
from app.bi.models import QueryResult
from app.core import cache_codec
from app.core.config import Settings
from app.core.shared_cache import SharedCache, SharedCacheUnavailable, _RELEASE


class FakeRedis:
    """Thread-safe Redis command semantics used by the lease/revision contract."""
    def __init__(self):
        self.values = {}
        self.expiry = {}
        self.lock = threading.RLock()
        self.available = True

    def _check(self):
        if not self.available:
            raise ConnectionError("Redis unavailable")
        now = time.monotonic()
        for key, deadline in tuple(self.expiry.items()):
            if now >= deadline:
                self.values.pop(key, None)
                self.expiry.pop(key, None)

    def get(self, key):
        with self.lock:
            self._check()
            return self.values.get(key)

    def mget(self, keys):
        with self.lock:
            self._check()
            return [self.values.get(key) for key in keys]

    def set(self, key, value, nx=False, px=None):
        with self.lock:
            self._check()
            if nx and key in self.values:
                return None
            self.values[key] = value.encode() if isinstance(value, str) else value
            if px is not None:
                self.expiry[key] = time.monotonic() + int(px) / 1000
            return True

    def incr(self, key):
        with self.lock:
            self._check()
            self.values[key] = str(int(self.values.get(key) or 0) + 1).encode()
            return int(self.values[key])

    def eval(self, script, count, *arguments):
        with self.lock:
            self._check()
            keys = arguments[:count]
            args = arguments[count:]
            if "cache invalidate" in script:
                return [self.incr(key) for key in keys]
            if "cache publish" in script:
                n = int(args[0])
                if any(int(self.values.get(keys[i]) or 0) != int(args[i + 1]) for i in range(n)):
                    return 0
                owner = args[n + 1]
                if owner and self.values.get(keys[n]) != owner.encode():
                    return 0
                self.set(keys[n + 1], args[n + 2], px=int(args[n + 3]))
                return 1
            owner = args[0].encode()
            if self.values.get(keys[0]) != owner:
                return 0
            if "cache release" in script:
                self.values.pop(keys[0], None)
                self.expiry.pop(keys[0], None)
                return 1
            if "cache renew" in script:
                self.expiry[keys[0]] = time.monotonic() + int(args[1]) / 1000
                return 1
            raise AssertionError("Unknown Lua script")

    def info(self, section):
        with self.lock:
            self._check()
            return {"used_memory": sum(len(value) for value in self.values.values())}


class SharedCacheTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.settings = Settings(_env_file=None, cache_enabled=True, cache_retry_seconds=0, cache_lease_seconds=1, cache_lock_wait_seconds=2, cache_l1_ttl_seconds=1)
        self.cache = self.new_cache()

    def new_cache(self, remote=True):
        cache = SharedCache(self.redis if remote else None, settings_getter=lambda: self.settings)
        cache._config = lambda: SimpleNamespace(enabled=True, stale_if_error_seconds=0, stale_while_revalidate=False)
        return cache

    def load(self, cache, loader, **kwargs):
        return cache.get_or_load("bi.query", {"user": "alice"}, loader, ttl_seconds=30, **kwargs)

    def test_safe_json_preserves_registered_models_dates_and_containers(self):
        value = {"result": QueryResult(rows=[{"value": 2}]), "metric": Metric(key="flights", label="Flights", value=2), "dates": (date(2026, 1, 1), datetime(2026, 1, 1, 12), dt_time(12, 30)), "sets": {1, 2}, "literal": {"t": "model", "name": "os.system"}}
        raw = cache_codec.dumps(value)
        decoded = cache_codec.loads(raw, 100000)
        self.assertIsInstance(decoded["result"], QueryResult)
        self.assertIsInstance(decoded["metric"], Metric)
        self.assertEqual(decoded, value)

    def test_unknown_type_and_malformed_json_cannot_construct_classes(self):
        with self.assertRaises(ValueError):
            cache_codec.loads(json.dumps({"version": 1, "value": {"t": "model", "name": "os.system", "v": "anything"}}), 1000)
        for raw in ('[]', '{"version":3}', '{"version":1,"value":NaN}', '{"version":1,"value":1e999}'):
            with self.assertRaises(ValueError):
                cache_codec.loads(raw, 1000)
        class Unknown(BaseModel):
            value: int
        with self.assertRaises(ValueError):
            cache_codec.dumps(Unknown(value=1))

    def test_ordered_dimensions_and_scope_identities_have_distinct_keys(self):
        self.assertNotEqual(self.cache.key("bi.query", {"dimensions": ["crew", "date"]}), self.cache.key("bi.query", {"dimensions": ["date", "crew"]}))
        self.assertNotEqual(self.cache.key("bi.query", {"scope": "team-a"}), self.cache.key("bi.query", {"scope": "team-b"}))
        self.assertEqual(self.cache.key("bi.query", {"a": 1, "b": 2}), self.cache.key("bi.query", {"b": 2, "a": 1}))

    def test_cross_worker_reuse_and_mutation_isolation(self):
        self.load(self.cache, lambda: QueryResult(rows=[{"value": 2}]))
        other = self.new_cache()
        value = self.load(other, lambda: self.fail("Shared value was not reused"))
        self.assertIsInstance(value, QueryResult)
        value.rows[0]["value"] = 99
        hit, result = other.probe("bi.query", {"user": "alice"})
        self.assertTrue(hit)
        self.assertEqual(result.rows[0]["value"], 2)

    def test_tag_invalidation_is_fresh_across_workers_and_selective(self):
        other = self.new_cache()
        for cache in (self.cache, other):
            cache.put("bi.period", "day-a", [1], ttl_seconds=30, dependency_tags=["data:2026-01-01"])
            cache.put("bi.period", "day-b", [2], ttl_seconds=30, dependency_tags=["data:2026-01-02"])
        other.invalidate_tags(["data:2026-01-01"])
        self.assertFalse(self.cache.probe("bi.period", "day-a", dependency_tags=["data:2026-01-01"])[0])
        self.assertEqual(self.cache.probe("bi.period", "day-b", dependency_tags=["data:2026-01-02"]), (True, [2]))

    def test_data_epoch_fences_reads_without_eviction_of_other_days(self):
        self.cache.put("bi.period", "day-b", [2], ttl_seconds=30, dependency_tags=["data:2026-01-02"])
        before = self.cache.generations(["data:epoch"])
        updates = self.new_cache().invalidate_tags(["data:2026-01-01"])
        self.assertEqual(updates["data:epoch"], 1)
        self.assertNotEqual(before, self.cache.generations(["data:epoch"]))
        self.assertEqual(self.cache.probe("bi.period", "day-b", dependency_tags=["data:2026-01-02"]), (True, [2]))

    def test_namespace_prefix_clear_leaves_unrelated_namespaces(self):
        other = self.new_cache()
        self.cache.put("bi.period", 1, "period", ttl_seconds=30)
        self.cache.put("bi:query", 2, "query", ttl_seconds=30)
        self.cache.put("api.overview", 3, "overview", ttl_seconds=30)
        other.clear("bi")
        self.assertFalse(self.cache.probe("bi.period", 1)[0])
        self.assertFalse(self.cache.probe("bi:query", 2)[0])
        self.assertEqual(self.cache.probe("api.overview", 3), (True, "overview"))

    def test_local_namespace_invalidation_preserves_other_entries(self):
        cache = self.new_cache(remote=False)
        cache.put("bi.period", 1, 10, ttl_seconds=30)
        cache.put("api.overview", 1, 20, ttl_seconds=30)
        cache.clear("bi")
        self.assertFalse(cache.probe("bi.period", 1)[0])
        self.assertEqual(cache.probe("api.overview", 1), (True, 20))

    def test_invalidation_during_read_fences_partition_publication(self):
        before = self.cache.generations(["data:2026-01-01", "data:2026-01-02"], namespace="bi.period")
        self.new_cache().invalidate_tags(["data:2026-01-01"])
        self.cache.put("bi.period", "day-a", "old", ttl_seconds=30, dependency_tags=["data:2026-01-01"], expected_generations=before)
        self.cache.put("bi.period", "day-b", "valid", ttl_seconds=30, dependency_tags=["data:2026-01-02"], expected_generations=before)
        self.assertFalse(self.cache.probe("bi.period", "day-a", dependency_tags=["data:2026-01-01"])[0])
        self.assertEqual(self.cache.probe("bi.period", "day-b", dependency_tags=["data:2026-01-02"]), (True, "valid"))

    def test_invalidation_inside_loader_does_not_publish_under_new_generation(self):
        def loader():
            self.new_cache().invalidate_tags(["metric:flights"])
            return "old source result"
        self.load(self.cache, loader, dependency_tags=["metric:flights"])
        self.assertFalse(self.cache.probe("bi.query", {"user": "alice"}, dependency_tags=["metric:flights"])[0])

    def _parallel(self, calls):
        results, errors = [], []
        def run(call):
            try:
                results.append(call())
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(call,), daemon=True) for call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
        self.assertFalse(any(thread.is_alive() for thread in threads), "Singleflight callers did not complete")
        self.assertEqual(errors, [])
        return results

    def test_distributed_singleflight_loads_once_across_workers(self):
        count = 0
        count_lock = threading.Lock()
        def loader():
            nonlocal count
            with count_lock:
                count += 1
            time.sleep(0.06)
            return [42]
        caches = [self.new_cache() for _ in range(4)]
        self.assertEqual(self._parallel([lambda cache=cache: self.load(cache, loader) for cache in caches]), [[42]] * 4)
        self.assertEqual(count, 1)
        self.assertFalse(any(key.endswith(":lease") for key in self.redis.values))

    def test_zero_ttl_period_fill_serializes_without_storing_bundle(self):
        source_reads = 0
        caches = [self.new_cache() for _ in range(3)]
        def call(cache):
            def loader():
                nonlocal source_reads
                hit, value = cache.probe("bi.period", "day")
                if hit:
                    return value
                source_reads += 1
                time.sleep(0.05)
                cache.put("bi.period", "day", [5], ttl_seconds=30)
                return [5]
            return cache.get_or_load("bi.fill", "day", loader, ttl_seconds=0)
        self.assertEqual(self._parallel([lambda cache=cache: call(cache) for cache in caches]), [[5]] * 3)
        self.assertEqual(source_reads, 1)
        self.assertFalse(self.cache.probe("bi.fill", "day")[0])

    def test_long_loader_renews_lease_before_original_expiry(self):
        started = threading.Event()
        calls = 0
        def loader():
            nonlocal calls
            calls += 1
            started.set()
            time.sleep(1.1)
            return "loaded"
        other = self.new_cache()
        def second():
            self.assertTrue(started.wait(1))
            time.sleep(1.02)
            return self.load(other, loader)
        self.assertEqual(self._parallel([lambda: self.load(self.cache, loader), second]), ["loaded", "loaded"])
        self.assertEqual(calls, 1)

    def test_old_lease_owner_cannot_delete_or_publish_new_owners_value(self):
        generations, remote = self.cache._snapshot([], "bi.query")
        key = self.cache._value_key("bi.query", {"user": "alice"}, generations, remote)
        self.redis.set(key + ":lease", "new-owner", px=1000)
        self.cache._publish("bi.query", {"user": "alice"}, "bad", 30, 0, [], generations, remote, owner="old-owner")
        self.assertFalse(self.cache.probe("bi.query", {"user": "alice"})[0])
        self.redis.eval(_RELEASE, 1, key + ":lease", "old-owner")
        self.assertEqual(self.redis.get(key + ":lease"), b"new-owner")

    def test_lock_timeout_computes_uncached_without_stealing_owner(self):
        self.settings.cache_lock_wait_seconds = 0
        generations, remote = self.cache._snapshot([], "bi.query")
        key = self.cache._value_key("bi.query", {"user": "alice"}, generations, remote)
        self.redis.set(key + ":lease", "other-owner", px=1000)
        self.assertEqual(self.load(self.cache, lambda: "direct"), "direct")
        self.assertIsNone(self.redis.get(key))
        self.assertEqual(self.redis.get(key + ":lease"), b"other-owner")
        self.assertEqual(self.cache.stats()["lease_timeouts"], 1)

    def test_outage_fails_open_recovers_and_invalidation_is_not_false_success(self):
        self.load(self.cache, lambda: "before")
        self.redis.available = False
        self.assertEqual(self.load(self.cache, lambda: "during"), "during")
        self.assertEqual(self.load(self.cache, lambda: self.fail("Short local fallback should be reused")), "during")
        with self.assertRaises(SharedCacheUnavailable):
            self.cache.invalidate_tags(["data:2026-01-01"])
        self.assertTrue(self.cache.stats()["degraded"])
        self.redis.available = True
        self.assertEqual(self.load(self.cache, lambda: self.fail("Redis must recover automatically")), "before")
        self.assertTrue(self.cache.stats()["redis_available"])

    def test_large_values_and_l1_ram_are_bounded(self):
        self.settings.cache_max_value_bytes = 1024
        self.assertEqual(self.load(self.cache, lambda: "x" * 2000), "x" * 2000)
        self.assertFalse(self.cache.probe("bi.query", {"user": "alice"})[0])
        self.assertEqual(self.cache.stats()["skipped_large"], 1)
        self.settings.cache_l1_max_bytes = 2000
        for index in range(10):
            self.cache.put("bi.period", index, "x" * 400, ttl_seconds=30)
        self.assertLessEqual(self.cache.stats()["l1_bytes"], 2000)
        self.assertLess(self.cache.stats()["entries"], 10)

    def test_expired_stale_value_is_not_served_after_slow_failed_loader(self):
        self.cache.get_or_load("bi.query", "stale", lambda: 42, ttl_seconds=0.02, stale_if_error_seconds=0.05)
        time.sleep(0.03)
        def fail():
            time.sleep(0.06)
            raise ValueError("source failed")
        with self.assertRaises(ValueError):
            self.cache.get_or_load("bi.query", "stale", fail, ttl_seconds=0.02, stale_if_error_seconds=0.05)

    def test_current_caller_can_forbid_stale_from_an_older_entry(self):
        self.cache.get_or_load("bi.query", "stale", lambda: 42, ttl_seconds=0.02, stale_if_error_seconds=1)
        time.sleep(0.03)
        def fail():
            raise ValueError("source failed")
        with self.assertRaises(ValueError):
            self.cache.get_or_load("bi.query", "stale", fail, ttl_seconds=0.02, stale_if_error_seconds=0)

    def test_recursive_load_fails_and_does_not_leak_local_locks(self):
        with self.assertRaisesRegex(RuntimeError, "Recursive"):
            self.load(self.cache, lambda: self.load(self.cache, lambda: 1))
        self.assertEqual(self.cache._key_locks, {})

    def test_disabled_cache_avoids_redis_and_settings_override_legacy(self):
        self.settings.cache_enabled = False
        self.redis.available = False
        self.assertEqual(self.load(self.cache, lambda: 10), 10)
        self.assertEqual(self.cache._redis_errors, 0)
        self.settings.cache_enabled = True
        self.cache._config = lambda: SimpleNamespace(enabled=False, stale_if_error_seconds=0, stale_while_revalidate=False)
        self.redis.available = True
        self.load(self.cache, lambda: 20)
        self.assertTrue(self.cache.probe("bi.query", {"user": "alice"})[0])

    def test_api_prefix_validation_and_stats_do_not_expose_credentials(self):
        for prefix in ("api", "/api/", "/api//v1", "/api?secret=1"):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None, api_prefix=prefix)
        self.assertEqual(Settings(_env_file=None, api_prefix="/api/v2").api_prefix, "/api/v2")
        self.settings.redis_url = "redis://secret:password@redis:6379/0"
        self.assertNotIn("password", json.dumps(self.cache.stats()))


if __name__ == "__main__":
    unittest.main()
