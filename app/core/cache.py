from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from typing import Any, Callable, Iterator, TypeVar

from pydantic import BaseModel

from app.core.config import get_backend_config

T = TypeVar("T")


@dataclass
class CacheEntry:
    value: Any
    created_at: float
    expires_at: float
    stale_until: float


@dataclass
class _KeyLock:
    lock: Any
    users: int = 0


class MemoryCache:
    """Small process-local TTL cache with stale-on-error support.

    The FastAPI handlers in this project are synchronous and may execute in
    different worker threads. A global lock protects the LRU store and a
    per-key lock prevents a thundering herd when an expired key is refreshed.
    Nested loaders must use an acyclic dependency graph of cache keys.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        # Nested API/dataset/schema loads must never share an unrelated lock.
        # Retain each key's lock only while a loader or its waiters use it.
        self._key_locks: dict[str, _KeyLock] = {}
        self._loading_keys = threading.local()
        self._hits = 0
        self._misses = 0
        self._stale_hits = 0
        self._background_refreshes = 0
        self._background_refresh_failures = 0
        self._refreshing: set[str] = set()

    def _config(self):
        return get_backend_config().cache

    @staticmethod
    def _normalize(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return MemoryCache._normalize(
                value.model_dump(mode="json", exclude_none=False)
            )
        if isinstance(value, (date, datetime, dt_time)):
            return value.isoformat()
        elif isinstance(value, dict):
            return {
                str(key): MemoryCache._normalize(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        elif isinstance(value, (list, tuple, set, frozenset)):
            normalized = [MemoryCache._normalize(item) for item in value]
            # Filter selections are set-like. Sorting also makes cache keys stable
            # when the frontend sends the same values in a different order.
            try:
                return sorted(
                    normalized,
                    key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
                )
            except TypeError:
                return normalized
        return value

    def key(self, namespace: str, payload: Any = None) -> str:
        normalized = self._normalize(payload)
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    def _clone(self, value: T) -> T:
        if self._config().copy_on_read:
            return copy.deepcopy(value)
        return value

    def _get_entry(self, key: str) -> CacheEntry | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def _prune_dead_locked(self, now: float) -> None:
        dead = [
            key
            for key, entry in self._entries.items()
            if now >= entry.stale_until
        ]
        for key in dead:
            self._entries.pop(key, None)

    def _store(self, key: str, value: T, ttl_seconds: int, stale_seconds: int) -> T:
        now = time.monotonic()
        entry = CacheEntry(
            value=copy.deepcopy(value) if self._config().copy_on_write else value,
            created_at=now,
            expires_at=now + max(0, ttl_seconds),
            stale_until=now + max(0, ttl_seconds) + max(0, stale_seconds),
        )
        with self._lock:
            self._prune_dead_locked(now)
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._config().max_entries:
                self._entries.popitem(last=False)
        return self._clone(entry.value)

    @contextmanager
    def _key_lock(self, key: str) -> Iterator[None]:
        loading_keys = getattr(self._loading_keys, "keys", None)
        if loading_keys is None:
            loading_keys = self._loading_keys.keys = set()
        if key in loading_keys:
            raise RuntimeError(f"Recursive cache load for {key!r}")

        with self._lock:
            key_lock = self._key_locks.get(key)
            if key_lock is None:
                key_lock = self._key_locks[key] = _KeyLock(threading.Lock())
            key_lock.users += 1

        try:
            with key_lock.lock:
                loading_keys.add(key)
                try:
                    yield
                finally:
                    loading_keys.remove(key)
        finally:
            with self._lock:
                key_lock.users -= 1
                if key_lock.users == 0:
                    self._key_locks.pop(key, None)

    def _refresh_in_background(
        self,
        key: str,
        loader: Callable[[], T],
        ttl_seconds: int,
        stale_seconds: int,
    ) -> None:
        with self._lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)
            self._background_refreshes += 1

        def run() -> None:
            try:
                with self._key_lock(key):
                    entry = self._get_entry(key)
                    if entry is None or time.monotonic() >= entry.expires_at:
                        value = loader()
                        self._store(key, value, ttl_seconds, stale_seconds)
            except Exception:
                # Keep the existing stale value until stale_until. A failed
                # background refresh must never make an old value "fresh" again.
                with self._lock:
                    self._background_refresh_failures += 1
            finally:
                with self._lock:
                    self._refreshing.discard(key)

        thread = threading.Thread(
            target=run,
            name=f"cache-refresh-{key.split(':', 1)[0]}",
            daemon=True,
        )
        thread.start()

    def get_or_load(
        self,
        namespace: str,
        payload: Any,
        loader: Callable[[], T],
        *,
        ttl_seconds: int,
        stale_if_error_seconds: int | None = None,
        stale_while_revalidate: bool | None = None,
        dependency_tags=(),
        shared: bool = True,
    ) -> T:
        # Local-only callers retain the original semantics. Revision tags and
        # distributed leases are implemented by the global SharedCache facade.
        config = self._config()
        if not config.enabled or ttl_seconds <= 0:
            return loader()

        key = self.key(namespace, payload)
        now = time.monotonic()
        entry = self._get_entry(key)
        if entry is not None and now < entry.expires_at:
            with self._lock:
                self._hits += 1
            return self._clone(entry.value)

        with self._lock:
            self._misses += 1

        stale_seconds = (
            config.stale_if_error_seconds
            if stale_if_error_seconds is None
            else stale_if_error_seconds
        )
        use_swr = (
            config.stale_while_revalidate
            if stale_while_revalidate is None
            else stale_while_revalidate
        )
        # Background refreshes use the same key lock as foreground loaders.
        # Serve eligible stale values before waiting on that lock.
        if use_swr and entry is not None and now < entry.stale_until:
            with self._lock:
                self._stale_hits += 1
            self._refresh_in_background(key, loader, ttl_seconds, stale_seconds)
            return self._clone(entry.value)

        # Only one thread refreshes a key. Other callers wait for that loader and
        # reuse the freshly loaded value instead of repeating the upstream query.
        with self._key_lock(key):
            now = time.monotonic()
            entry = self._get_entry(key)
            if entry is not None and now < entry.expires_at:
                with self._lock:
                    self._hits += 1
                return self._clone(entry.value)

            # Stale-while-revalidate: once TTL expires, return the last known
            # value immediately and refresh it exactly once in a daemon thread.
            # This keeps dashboards responsive without allowing stale values to
            # be re-stored as fresh by nested caches.
            if (
                use_swr
                and entry is not None
                and now < entry.stale_until
            ):
                with self._lock:
                    self._stale_hits += 1
                self._refresh_in_background(
                    key, loader, ttl_seconds, stale_seconds
                )
                return self._clone(entry.value)

            try:
                value = loader()
            except Exception:
                now = time.monotonic()
                stale_entry = self._get_entry(key)
                if stale_entry is not None and now < stale_entry.stale_until:
                    with self._lock:
                        self._stale_hits += 1
                    return self._clone(stale_entry.value)
                raise

            return self._store(key, value, ttl_seconds, stale_seconds)

    def clear(self, namespace: str | None = None) -> int:
        with self._lock:
            if namespace is None:
                count = len(self._entries)
                self._entries.clear()
                return count

            prefix = f"{namespace}:"
            keys = [key for key in self._entries if key.startswith(prefix)]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            self._prune_dead_locked(time.monotonic())
            return {
                "enabled": self._config().enabled,
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "stale_hits": self._stale_hits,
                "background_refreshes": self._background_refreshes,
                "background_refresh_failures": self._background_refresh_failures,
                "refreshing": len(self._refreshing),
                "max_entries": self._config().max_entries,
            }


from app.core.shared_cache import SharedCache  # preserve MemoryCache for local-only consumers

cache = SharedCache()
