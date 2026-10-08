from __future__ import annotations

import hashlib
import threading
import time
import unittest
from types import SimpleNamespace

from app.core.cache import MemoryCache


class MemoryCacheConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = MemoryCache()
        self.config = SimpleNamespace(
            enabled=True,
            max_entries=8,
            stale_if_error_seconds=60,
            stale_while_revalidate=False,
            copy_on_read=False,
            copy_on_write=False,
        )
        self.cache._config = lambda: self.config

    def load(self, namespace, loader):
        return self.cache.get_or_load(namespace, None, loader, ttl_seconds=60)

    def old_stripe(self, namespace):
        key = self.cache.key(namespace, None)
        return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 64

    def namespace_on_stripe(self, prefix, stripe):
        for index in range(10000):
            namespace = f"{prefix}.{index}"
            if self.old_stripe(namespace) == stripe:
                return namespace
        self.fail("Could not find a key on the requested former lock stripe")

    def start_call(self, call, results, errors):
        def run():
            try:
                results.append(call())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def join_calls(self, threads):
        deadline = time.monotonic() + 4
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "Cache callers did not finish; possible nested-load deadlock",
        )

    def wait_for_users(self, namespace, count):
        key = self.cache.key(namespace, None)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.cache._lock:
                key_lock = self.cache._key_locks.get(key)
                if key_lock is not None and key_lock.users == count:
                    return
            threading.Event().wait(0.005)
        self.fail(f"Expected {count} callers to join the same in-flight load")

    def test_nested_keys_on_same_former_stripe_complete(self):
        outer = self.namespace_on_stripe("api.overview", 0)
        inner = self.namespace_on_stripe("clickhouse.dataset", 0)
        results, errors = [], []
        thread = self.start_call(
            lambda: self.load(outer, lambda: self.load(inner, lambda: 42)),
            results,
            errors,
        )
        self.join_calls([thread])
        self.assertEqual(errors, [])
        self.assertEqual(results, [42])
        self.assertEqual(self.cache._key_locks, {})

    def test_inverted_former_stripes_with_distinct_nested_keys_complete(self):
        outer_a = self.namespace_on_stripe("api.overview", 0)
        outer_b = self.namespace_on_stripe("api.timeline", 1)
        inner_a = self.namespace_on_stripe("clickhouse.dataset", 1)
        inner_b = self.namespace_on_stripe("clickhouse.schema", 0)
        both_loading = threading.Barrier(2)
        results, errors = [], []

        def outer_loader(inner, value):
            both_loading.wait(timeout=2)
            return self.load(inner, lambda: value)

        threads = [
            self.start_call(
                lambda: self.load(outer_a, lambda: outer_loader(inner_a, 11)),
                results,
                errors,
            ),
            self.start_call(
                lambda: self.load(outer_b, lambda: outer_loader(inner_b, 22)),
                results,
                errors,
            ),
        ]
        self.join_calls(threads)
        self.assertEqual(errors, [])
        self.assertCountEqual(results, [11, 22])
        self.assertEqual(self.cache._key_locks, {})

    def test_concurrent_callers_share_one_loader_and_release_registry(self):
        caller_count = 8
        ready = threading.Barrier(caller_count)
        loading = threading.Event()
        release = threading.Event()
        calls = []
        results, errors = [], []

        def loader():
            calls.append(1)
            loading.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Test did not release the loader")
            return 42

        def call():
            ready.wait(timeout=2)
            return self.load("api.overview", loader)

        threads = [
            self.start_call(call, results, errors) for _ in range(caller_count)
        ]
        try:
            self.assertTrue(loading.wait(timeout=2))
            self.wait_for_users("api.overview", caller_count)
        finally:
            release.set()
        self.join_calls(threads)
        self.assertEqual(errors, [])
        self.assertEqual(results, [42] * caller_count)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.cache._key_locks, {})

    def test_recursive_same_key_fails_promptly_and_releases_registry(self):
        results, errors = [], []
        thread = self.start_call(
            lambda: self.load(
                "api.overview",
                lambda: self.load(
                    "clickhouse.dataset", lambda: self.load("api.overview", lambda: 42)
                ),
            ),
            results,
            errors,
        )
        self.join_calls([thread])
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("Recursive cache load", str(errors[0]))
        self.assertEqual(self.cache._key_locks, {})

    def test_failed_and_evicted_keys_do_not_retain_locks(self):
        def fail():
            raise ValueError("upstream unavailable")

        with self.assertRaisesRegex(ValueError, "upstream unavailable"):
            self.load("failure", fail)
        self.assertEqual(self.cache._key_locks, {})

        for index in range(256):
            self.assertEqual(self.load(f"api.overview.{index}", lambda: index), index)
            self.assertEqual(self.cache._key_locks, {})
        self.assertEqual(self.cache.stats()["entries"], self.config.max_entries)

    def test_background_and_foreground_refreshes_share_one_loader(self):
        namespace = "api.overview"
        key = self.cache.key(namespace, None)
        self.cache._store(key, "old", 60, 60)
        with self.cache._lock:
            self.cache._entries[key].expires_at = time.monotonic() - 1

        loading = threading.Event()
        release = threading.Event()
        calls = []

        def loader():
            calls.append(1)
            loading.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Test did not release the background loader")
            return "new"

        stale = self.cache.get_or_load(
            namespace, None, loader, ttl_seconds=60, stale_while_revalidate=True
        )
        self.assertEqual(stale, "old")
        self.assertTrue(loading.wait(timeout=2))
        results, errors = [], []
        thread = self.start_call(
            lambda: self.load(namespace, loader), results, errors
        )
        try:
            self.wait_for_users(namespace, 2)
        finally:
            release.set()
        self.join_calls([thread])
        self.assertEqual(errors, [])
        self.assertEqual(results, ["new"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.cache._key_locks, {})

    def test_stale_requests_return_while_background_refresh_is_loading(self):
        namespace = "api.overview"
        key = self.cache.key(namespace, None)
        self.cache._store(key, "old", 60, 60)
        with self.cache._lock:
            self.cache._entries[key].expires_at = time.monotonic() - 1

        loading = threading.Event()
        release = threading.Event()
        calls = []

        def loader():
            calls.append(1)
            loading.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Test did not release the background loader")
            return "new"

        def request():
            return self.cache.get_or_load(
                namespace, None, loader, ttl_seconds=60, stale_while_revalidate=True
            )

        self.assertEqual(request(), "old")
        self.assertTrue(loading.wait(timeout=2))
        results, errors = [], []
        thread = self.start_call(request, results, errors)
        try:
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive(), "Stale response waited for refresh")
            self.assertEqual(errors, [])
            self.assertEqual(results, ["old"])
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
        self.join_calls([thread])
        # Wait for refresh completion through the regular single-flight path.
        self.assertEqual(self.load(namespace, loader), "new")
        self.assertEqual(self.cache._key_locks, {})


if __name__ == "__main__":
    unittest.main()
