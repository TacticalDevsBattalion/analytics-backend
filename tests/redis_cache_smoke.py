"""Opt-in real Redis integration: python tests/redis_cache_smoke.py.

Requires REDIS_URL. Creates and removes only keys under a random test prefix.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import redis

from app.bi.models import QueryResult
from app.core.config import Settings
from app.core.shared_cache import SharedCache, _RELEASE


def main():
    url = os.environ.get("REDIS_URL", "")
    if not url:
        raise SystemExit("REDIS_URL is required for the Redis integration check")
    prefix = "integration-" + uuid.uuid4().hex
    settings = Settings(_env_file=None, redis_url=url, cache_key_prefix=prefix, cache_enabled=True, cache_lease_seconds=1, cache_lock_wait_seconds=3, cache_l1_ttl_seconds=1)
    client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
    client.ping()
    caches = [SharedCache(client, settings_getter=lambda: settings) for _ in range(4)]
    for cache in caches:
        cache._config = lambda: SimpleNamespace(enabled=True, stale_if_error_seconds=0, stale_while_revalidate=False)
    count = 0
    count_lock = threading.Lock()

    def loader():
        nonlocal count
        with count_lock:
            count += 1
        time.sleep(0.12)
        return QueryResult(rows=[{"value": 42}])

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda cache: cache.get_or_load("bi.query", "test", loader, ttl_seconds=30, dependency_tags=["metric:flights"]), caches))
        assert count == 1, f"Distributed singleflight repeated source loading: {count}"
        assert all(isinstance(result, QueryResult) and result.rows[0]["value"] == 42 for result in results)
        caches[1].invalidate_tags(["metric:flights"])
        assert not caches[0].probe("bi.query", "test", dependency_tags=["metric:flights"])[0], "L1 returned invalidated Redis value"

        snapshot = caches[0].generations(["data:2026-01-01"], namespace="bi.period")
        caches[1].invalidate_tags(["data:2026-01-01"])
        caches[0].put("bi.period", "test", "obsolete", ttl_seconds=30, dependency_tags=["data:2026-01-01"], expected_generations=snapshot)
        assert not caches[1].probe("bi.period", "test", dependency_tags=["data:2026-01-01"])[0], "Obsolete source result crossed revision fence"

        generations, remote = caches[0]._snapshot([], "bi.owner")
        key = caches[0]._value_key("bi.owner", "test", generations, remote)
        client.set(key + ":lease", "new-owner", px=2000)
        caches[0]._publish("bi.owner", "test", "obsolete", 30, 0, [], generations, remote, owner="old-owner")
        assert client.get(key) is None, "Old owner overwrote newer lease"
        client.eval(_RELEASE, 1, key + ":lease", "old-owner")
        assert client.get(key + ":lease") == b"new-owner", "Old owner released newer lease"
        status = caches[0].stats()
        assert status["redis_available"] and status["redis_memory_bytes"] > 0
        print("Redis integration passed: shared typed values, distributed singleflight, fresh L1 invalidation, generation fencing, owner-safe Lua release/publication.")
    finally:
        # The UUID is generated here and never supplied by a user or environment.
        for key in client.scan_iter(match=f"{prefix}:v2:*", count=100):
            client.delete(key)
        client.close()


if __name__ == "__main__":
    main()
