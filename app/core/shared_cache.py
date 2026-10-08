"""Bounded local cache backed by optional Redis, with revision fencing.

Callers must authorize and validate requests before supplying cache keys.
Only registered Pydantic models and typed JSON values cross process boundaries.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from typing import Any, Callable, Iterable, Iterator, TypeVar

from pydantic import BaseModel

from app.core import cache_codec
from app.core.config import get_backend_config, get_settings

T = TypeVar("T")
log = logging.getLogger(__name__)


class SharedCacheUnavailable(RuntimeError):
    """A requested distributed invalidation could not be acknowledged."""


@dataclass
class _LocalEntry:
    value: Any
    fresh_until: float
    stale_until: float
    size: int
    namespace: str
    dependencies: tuple[str, ...]


@dataclass
class _KeyLock:
    lock: Any
    users: int = 0


_RELEASE = """-- cache release
if redis.call('get', KEYS[1]) == ARGV[1] then
 return redis.call('del', KEYS[1])
end
return 0
"""
_RENEW = """-- cache renew
if redis.call('get', KEYS[1]) == ARGV[1] then
 return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_PUBLISH = """-- cache publish: generation fence and optional lease fence
local n = tonumber(ARGV[1])
for i = 1,n do
 if tonumber(redis.call('get', KEYS[i]) or '0') ~= tonumber(ARGV[i+1]) then return 0 end
end
local lease = ARGV[n+2]
if lease ~= '' and redis.call('get', KEYS[n+1]) ~= lease then return 0 end
redis.call('set', KEYS[n+2], ARGV[n+3], 'PX', ARGV[n+4])
return 1
"""
_INVALIDATE = """-- cache invalidate: publish tag revisions and epoch atomically
local result = {}
for i,key in ipairs(KEYS) do
 result[i] = redis.call('incr', key)
end
return result
"""


class SharedCache:
    """Redis is optional; source reads continue through outages.

    Redis generation keys intentionally have no expiry. Deploy Redis with a
    volatile eviction policy so an evicted generation cannot revive old values.
    Local entries use short TTLs, byte bounds and fresh distributed revisions.
    """

    def __init__(self, redis_client=None, settings_getter=None) -> None:
        self._settings_getter = settings_getter or get_settings
        self._client = redis_client
        self._injected = redis_client is not None
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _LocalEntry] = OrderedDict()
        self._bytes = 0
        self._key_locks: dict[str, _KeyLock] = {}
        self._loading_keys = threading.local()
        self._local_generations: dict[str, int] = {}
        self._local_epoch = 0
        self._redis_available = False
        self._retry_at = 0.0
        self._hits = self._misses = self._stale_hits = 0
        self._redis_hits = self._l1_hits = self._redis_errors = 0
        self._skipped_large = self._serialization_errors = self._corrupt_values = 0
        self._lease_waits = self._lease_timeouts = self._lease_acquired = 0
        self._loads = self._load_errors = 0
        self._load_milliseconds = 0.0
        self._lookup_count = 0
        self._lookup_milliseconds = 0.0
        self._background_refreshes = self._background_refresh_failures = 0
        self._refreshing: set[str] = set()

    def _settings(self):
        return self._settings_getter()

    def _config(self):
        return get_backend_config().cache

    def _enabled(self):
        value = self._settings().cache_enabled
        return self._config().enabled if value is None else value

    def _configured(self):
        return self._injected or bool(self._settings().redis_url.strip())

    @staticmethod
    def _normalize(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return SharedCache._normalize(value.model_dump(mode="json", exclude_none=False))
        if isinstance(value, (datetime, date, dt_time)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): SharedCache._normalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        if isinstance(value, (list, tuple)):
            # Query dimensions, sort clauses, series and formula operands are ordered.
            return [SharedCache._normalize(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted((SharedCache._normalize(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
        return value

    @staticmethod
    def _namespace(namespace):
        if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", namespace):
            raise ValueError("Invalid cache namespace")
        return namespace

    def key(self, namespace: str, payload: Any = None) -> str:
        namespace = self._namespace(namespace)
        encoded = json.dumps(self._normalize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str)
        return f"{namespace}:{hashlib.sha256(encoded.encode()).hexdigest()}"

    @staticmethod
    def _tags(tags):
        values = tuple(tags)
        if len(values) > 4096 or any(not isinstance(tag, str) or not tag or len(tag) > 240 for tag in values):
            raise ValueError("Invalid cache dependency tags")
        normalized = tuple(sorted(set(values)))
        return normalized

    def _generation_names(self, tags, namespace):
        names = ["__all__"]
        if namespace is not None:
            self._namespace(namespace)
            # clear('bi') invalidates bi.period and bi:period, while leaving api intact.
            boundaries = [match.start() for match in re.finditer(r"[.:]", namespace)]
            names.extend(f"__namespace__:{namespace[:end]}" for end in boundaries if end > 0)
            names.append(f"__namespace__:{namespace}")
        names.extend(self._tags(tags))
        return tuple(dict.fromkeys(names))

    def _prefix(self):
        return f"{self._settings().cache_key_prefix}:v2"

    def _generation_key(self, name):
        return f"{self._prefix()}:generation:{hashlib.sha256(name.encode()).hexdigest()}"

    def _redis(self, method, *args, **kwargs):
        if not self._configured() or time.monotonic() < self._retry_at:
            raise SharedCacheUnavailable("Shared cache is unavailable")
        try:
            if self._client is None:
                import redis
                from redis.backoff import NoBackoff
                from redis.retry import Retry
                settings = self._settings()
                self._client = redis.Redis.from_url(settings.redis_url, socket_timeout=settings.cache_redis_timeout_seconds, socket_connect_timeout=settings.cache_redis_timeout_seconds, decode_responses=False, max_connections=64, retry=Retry(NoBackoff(), 0))
            result = getattr(self._client, method)(*args, **kwargs)
        except Exception as exc:
            with self._lock:
                was_available = self._redis_available
                self._redis_available = False
                self._redis_errors += 1
                self._retry_at = time.monotonic() + self._settings().cache_retry_seconds
                if was_available:
                    self._clear_local_locked()
                    self._local_epoch += 1
            log.warning("Shared cache unavailable (%s); source reads remain available", type(exc).__name__)
            raise SharedCacheUnavailable("Shared cache is unavailable") from exc
        with self._lock:
            if not self._redis_available:
                self._clear_local_locked()
                self._local_epoch += 1
                self._redis_available = True
            self._retry_at = 0.0
        return result

    def _snapshot(self, tags, namespace, shared=True):
        names = self._generation_names(tags, namespace)
        if shared and self._configured():
            try:
                values = self._redis("mget", [self._generation_key(name) for name in names])
                return {name: int(value or 0) for name, value in zip(names, values)}, True
            except (SharedCacheUnavailable, ValueError):
                pass
        with self._lock:
            result = {name: self._local_generations.get(name, 0) for name in names}
            result["__local_epoch__"] = self._local_epoch
        return result, False

    def generations(self, tags: Iterable[str] = (), namespace: str | None = None) -> dict[str, int]:
        return self._snapshot(tags, namespace)[0]

    def _value_key(self, namespace, payload, generations, remote):
        identity = json.dumps(generations, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{self.key(namespace, payload)}|{identity}".encode()).hexdigest()
        return f"{self._prefix()}:value:{'redis' if remote else 'local'}:{digest}"

    def _clear_local_locked(self):
        count = len(self._entries)
        self._entries.clear()
        self._bytes = 0
        return count

    def _remove_locked(self, key):
        entry = self._entries.pop(key, None)
        if entry:
            self._bytes -= entry.size

    def _prune_locked(self):
        now = time.monotonic()
        for key, entry in tuple(self._entries.items()):
            if now >= entry.stale_until:
                self._remove_locked(key)

    @staticmethod
    def _memory_size(value, seen=None):
        seen = seen if seen is not None else set()
        if id(value) in seen:
            return 0
        seen.add(id(value))
        size = sys.getsizeof(value)
        if isinstance(value, BaseModel):
            size += SharedCache._memory_size(value.__dict__, seen)
        elif isinstance(value, dict):
            size += sum(SharedCache._memory_size(key, seen) + SharedCache._memory_size(item, seen) for key, item in value.items())
        elif isinstance(value, (list, tuple, set, frozenset)):
            size += sum(SharedCache._memory_size(item, seen) for item in value)
        return size

    def _local_store(self, key, namespace, value, size, fresh_seconds, stale_seconds, dependencies=()):
        settings = self._settings()
        lifetime = min(max(0, fresh_seconds + stale_seconds), settings.cache_l1_ttl_seconds)
        if lifetime <= 0 or size > settings.cache_max_value_bytes:
            return
        size = max(size, self._memory_size(value)) + sys.getsizeof(key) + 256
        if size > settings.cache_l1_max_bytes:
            return
        now = time.monotonic()
        with self._lock:
            self._prune_locked()
            self._remove_locked(key)
            self._entries[key] = _LocalEntry(copy.deepcopy(value), now + min(max(0, fresh_seconds), lifetime), now + lifetime, size, namespace, tuple(dependencies))
            self._bytes += size
            while len(self._entries) > settings.cache_l1_max_entries or self._bytes > settings.cache_l1_max_bytes:
                oldest = next(iter(self._entries))
                self._remove_locked(oldest)

    def _lookup(self, namespace, payload, tags, shared=True):
        started = time.monotonic()
        try:
            return self._lookup_value(namespace, payload, tags, shared)
        finally:
            with self._lock:
                self._lookup_count += 1
                self._lookup_milliseconds += (time.monotonic() - started) * 1000

    def _lookup_value(self, namespace, payload, tags, shared=True):
        generations, remote = self._snapshot(tags, namespace, shared)
        key = self._value_key(namespace, payload, generations, remote)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry and now < entry.stale_until:
                self._entries.move_to_end(key)
                if now < entry.fresh_until:
                    self._l1_hits += 1
                    return True, copy.deepcopy(entry.value), None, generations, remote, key
                stale_value = copy.deepcopy(entry.value)
            else:
                self._remove_locked(key)
                stale_value = None
        if remote:
            try:
                raw = self._redis("get", key)
                if raw is not None:
                    wrapped = cache_codec.loads(raw, self._settings().cache_max_value_bytes)
                    fresh = float(wrapped["fresh_until"]) - time.time()
                    stale = float(wrapped["stale_until"]) - time.time()
                    value = wrapped["value"]
                    if stale > 0:
                        self._local_store(key, namespace, value, len(raw), max(0, fresh), max(0, stale - max(0, fresh)), tags)
                        if fresh > 0:
                            with self._lock:
                                self._redis_hits += 1
                            return True, copy.deepcopy(value), None, generations, remote, key
                        stale_value = copy.deepcopy(value)
            except SharedCacheUnavailable:
                generations, remote = self._snapshot(tags, namespace, shared=False)
                key = self._value_key(namespace, payload, generations, remote)
                stale_value = None
            except Exception:
                with self._lock:
                    self._corrupt_values += 1
        return False, None, stale_value, generations, remote, key

    def probe(self, namespace: str, payload: Any, *, dependency_tags: Iterable[str] = (), shared=True):
        if not self._enabled():
            return False, None
        hit, value, _, _, _, _ = self._lookup(namespace, payload, self._tags(dependency_tags), shared)
        with self._lock:
            if hit:
                self._hits += 1
            else:
                self._misses += 1
        return hit, value

    def _publish(self, namespace, payload, value, ttl, stale_seconds, tags, generations, remote, owner=None):
        current, current_remote = self._snapshot(tags, namespace, remote)
        if current != generations or current_remote != remote:
            return
        key = self._value_key(namespace, payload, generations, remote)
        now = time.time()
        try:
            raw = cache_codec.dumps({"fresh_until": now + ttl, "stale_until": now + ttl + stale_seconds, "value": value})
        except (ValueError, TypeError):
            with self._lock:
                self._serialization_errors += 1
            return
        if len(raw) > self._settings().cache_max_value_bytes:
            with self._lock:
                self._skipped_large += 1
            return
        if remote:
            names = tuple(generations)
            lease_key = f"{key}:lease"
            keys = [self._generation_key(name) for name in names] + [lease_key, key]
            args = [len(names)] + [generations[name] for name in names] + [owner or "", raw, max(1, int((ttl + stale_seconds) * 1000))]
            try:
                published = self._redis("eval", _PUBLISH, len(keys), *keys, *args)
                if not published:
                    return
            except SharedCacheUnavailable:
                return
        self._local_store(key, namespace, value, len(raw), ttl, stale_seconds, tags)

    def put(self, namespace, payload, value, *, ttl_seconds, dependency_tags=(), shared=True, expected_generations=None):
        if not self._enabled() or ttl_seconds <= 0:
            return
        tags = self._tags(dependency_tags)
        generations, remote = self._snapshot(tags, namespace, shared)
        if expected_generations is not None and any(expected_generations.get(name) != revision for name, revision in generations.items()):
            return
        self._publish(namespace, payload, value, ttl_seconds, 0, tags, generations, remote)

    @contextmanager
    def _key_lock(self, key) -> Iterator[None]:
        loading = getattr(self._loading_keys, "keys", None)
        if loading is None:
            loading = self._loading_keys.keys = set()
        if key in loading:
            raise RuntimeError(f"Recursive cache load for {key!r}")
        with self._lock:
            lock = self._key_locks.setdefault(key, _KeyLock(threading.Lock()))
            lock.users += 1
        try:
            with lock.lock:
                loading.add(key)
                try:
                    yield
                finally:
                    loading.remove(key)
        finally:
            with self._lock:
                lock.users -= 1
                if not lock.users:
                    self._key_locks.pop(key, None)

    @contextmanager
    def _lease(self, key, owner):
        stopped = threading.Event()
        settings = self._settings()

        def renew():
            while not stopped.wait(max(0.1, settings.cache_lease_seconds / 3)):
                try:
                    if not self._redis("eval", _RENEW, 1, key, owner, int(settings.cache_lease_seconds * 1000)):
                        return
                except SharedCacheUnavailable:
                    return

        thread = threading.Thread(target=renew, name="cache-lease-renew", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)
            try:
                self._redis("eval", _RELEASE, 1, key, owner)
            except SharedCacheUnavailable:
                pass

    def _load(self, loader):
        started = time.monotonic()
        with self._lock:
            self._loads += 1
        try:
            return loader()
        except Exception:
            with self._lock:
                self._load_errors += 1
            raise
        finally:
            with self._lock:
                self._load_milliseconds += (time.monotonic() - started) * 1000

    def _refresh_background(self, identity, callback):
        with self._lock:
            if identity in self._refreshing:
                return
            self._refreshing.add(identity)
            self._background_refreshes += 1

        def run():
            try:
                callback()
            except Exception:
                with self._lock:
                    self._background_refresh_failures += 1
            finally:
                with self._lock:
                    self._refreshing.discard(identity)
        threading.Thread(target=run, name="cache-background-refresh", daemon=True).start()

    def singleflight(self, namespace, payload, loader, *, dependency_tags=(), shared=True):
        """Serialize a transient fill without storing the combined bundle.

        The loader should re-probe its individual partitions after the lease.
        Waiting callers then reuse partitions published by the previous owner.
        """
        if not self._enabled():
            return self._load(loader)
        tags = self._tags(dependency_tags)
        with self._key_lock(self.key(namespace, payload)):
            started = time.monotonic()
            owner = uuid.uuid4().hex
            while True:
                generations, remote = self._snapshot(tags, namespace, shared)
                if not remote:
                    return self._load(loader)
                key = self._value_key(namespace, payload, generations, remote) + ":lease"
                try:
                    acquired = self._redis("set", key, owner, nx=True, px=int(self._settings().cache_lease_seconds * 1000))
                except SharedCacheUnavailable:
                    continue
                if acquired:
                    with self._lock:
                        self._lease_acquired += 1
                    with self._lease(key, owner):
                        return self._load(loader)
                with self._lock:
                    self._lease_waits += 1
                if time.monotonic() - started >= self._settings().cache_lock_wait_seconds:
                    with self._lock:
                        self._lease_timeouts += 1
                    return self._load(loader)
                time.sleep(0.025)

    def get_or_load(self, namespace, payload, loader: Callable[[], T], *, ttl_seconds, stale_if_error_seconds=None, stale_while_revalidate=None, dependency_tags=(), shared=True) -> T:
        if not self._enabled():
            return self._load(loader)
        if ttl_seconds <= 0:
            return self.singleflight(namespace, payload, loader, dependency_tags=dependency_tags, shared=shared)
        tags = self._tags(dependency_tags)
        stale_seconds = max(0, self._config().stale_if_error_seconds if stale_if_error_seconds is None else stale_if_error_seconds)
        use_swr = self._config().stale_while_revalidate if stale_while_revalidate is None else stale_while_revalidate
        hit, value, stale, generations, remote, key = self._lookup(namespace, payload, tags, shared)
        with self._lock:
            if hit:
                self._hits += 1
            else:
                self._misses += 1
        if hit:
            return value
        if use_swr and stale_seconds > 0 and stale is not None:
            with self._lock:
                self._stale_hits += 1
            self._refresh_background(key, lambda: self.get_or_load(namespace, payload, loader, ttl_seconds=ttl_seconds, stale_if_error_seconds=stale_seconds, stale_while_revalidate=False, dependency_tags=tags, shared=shared))
            return stale
        with self._key_lock(self.key(namespace, payload)):
            started = time.monotonic()
            owner = uuid.uuid4().hex
            while True:
                hit, value, stale, generations, remote, key = self._lookup(namespace, payload, tags, shared)
                if hit:
                    with self._lock:
                        self._hits += 1
                    return value
                acquired = False
                if remote:
                    try:
                        acquired = bool(self._redis("set", f"{key}:lease", owner, nx=True, px=int(self._settings().cache_lease_seconds * 1000)))
                    except SharedCacheUnavailable:
                        # Redis went away after the generation snapshot; retry locally.
                        continue
                    if not acquired:
                        with self._lock:
                            self._lease_waits += 1
                        if time.monotonic() - started >= self._settings().cache_lock_wait_seconds:
                            with self._lock:
                                self._lease_timeouts += 1
                            # A slow owner must not be overwritten. Compute uncached.
                            return self._load(loader)
                        time.sleep(0.025)
                        continue
                    with self._lock:
                        self._lease_acquired += 1
                try:
                    if acquired:
                        with self._lease(f"{key}:lease", owner):
                            value = self._load(loader)
                            self._publish(namespace, payload, value, ttl_seconds, stale_seconds, tags, generations, remote, owner)
                    else:
                        value = self._load(loader)
                        self._publish(namespace, payload, value, ttl_seconds, stale_seconds, tags, generations, remote)
                    return copy.deepcopy(value)
                except Exception:
                    # Never serve a stale value after its scope/config revision changed.
                    _, _, latest_stale, current, current_remote, _ = self._lookup(namespace, payload, tags, shared)
                    if stale_seconds > 0 and latest_stale is not None and current == generations and current_remote == remote:
                        with self._lock:
                            self._stale_hits += 1
                        return latest_stale
                    raise

    def invalidate_tags(self, tags: Iterable[str]):
        names = self._tags(tags)
        if not names:
            return {}
        if any(name.startswith("data:") and name != "data:epoch" for name in names) and "data:epoch" not in names:
            # A read fence only: day cache keys do not include this epoch, so
            # correcting one date does not evict unrelated daily partitions.
            names = (*names, "data:epoch")
        with self._lock:
            for key, entry in tuple(self._entries.items()):
                applicable = self._generation_names(entry.dependencies, entry.namespace)
                if set(names).intersection(applicable):
                    self._remove_locked(key)
        if self._configured():
            # Failure is explicit: an administrator must not receive false success.
            values = self._redis("eval", _INVALIDATE, len(names), *(self._generation_key(name) for name in names))
            return {name: int(value) for name, value in zip(names, values)}
        with self._lock:
            for name in names:
                self._local_generations[name] = self._local_generations.get(name, 0) + 1
            return {name: self._local_generations[name] for name in names}

    def invalidate_namespace(self, namespace):
        namespace = self._namespace(namespace)
        return self.invalidate_tags([f"__namespace__:{namespace}"])

    def clear(self, namespace=None):
        if namespace is not None:
            self._namespace(namespace)
        with self._lock:
            if namespace is None:
                count = self._clear_local_locked()
            else:
                keys = [key for key, entry in self._entries.items() if entry.namespace == namespace or entry.namespace.startswith((f"{namespace}.", f"{namespace}:"))]
                count = len(keys)
                for key in keys:
                    self._remove_locked(key)
        self.invalidate_tags(["__all__" if namespace is None else f"__namespace__:{namespace}"])
        return count

    def stats(self):
        with self._lock:
            self._prune_locked()
            settings = self._settings()
            result = {"enabled": self._enabled(), "entries": len(self._entries), "hits": self._hits, "misses": self._misses, "stale_hits": self._stale_hits, "background_refreshes": self._background_refreshes, "background_refresh_failures": self._background_refresh_failures, "refreshing": len(self._refreshing), "max_entries": settings.cache_l1_max_entries, "l1_bytes": self._bytes, "l1_max_bytes": settings.cache_l1_max_bytes, "max_value_bytes": settings.cache_max_value_bytes, "l1_hits": self._l1_hits, "redis_hits": self._redis_hits, "redis_configured": self._configured(), "redis_available": self._redis_available, "degraded": self._configured() and not self._redis_available, "redis_errors": self._redis_errors, "skipped_large": self._skipped_large, "serialization_errors": self._serialization_errors, "corrupt_values": self._corrupt_values, "lease_waits": self._lease_waits, "lease_timeouts": self._lease_timeouts, "lease_acquired": self._lease_acquired, "loads": self._loads, "load_errors": self._load_errors, "load_milliseconds": round(self._load_milliseconds, 2)}
            result.update({"hit_ratio": round(self._hits / (self._hits + self._misses), 4) if self._hits + self._misses else 0.0, "lookup_count": self._lookup_count, "lookup_milliseconds": round(self._lookup_milliseconds, 2), "average_lookup_milliseconds": round(self._lookup_milliseconds / self._lookup_count, 3) if self._lookup_count else 0.0})
        if self._configured():
            try:
                info = self._redis("info", "memory")
                result["redis_memory_bytes"] = int(info.get("used_memory", 0))
                keyspace = self._redis("info", "keyspace")
                result["redis_keys_count"] = sum(int(database.get("keys", 0)) for database in keyspace.values() if isinstance(database, dict))
            except SharedCacheUnavailable:
                result["redis_memory_bytes"] = None
                result["redis_keys_count"] = None
            result["redis_available"] = self._redis_available
            result["degraded"] = not self._redis_available
        return result
