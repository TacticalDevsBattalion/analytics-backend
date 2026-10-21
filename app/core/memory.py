"""Keeps the backend inside its container memory limit.

A month of source rows is ~0.5 GB of Python objects, so a few heavy requests at
once, or memory that glibc keeps after they finish, can reach a 2 GB pod limit.
Heavy computations take a slot, return freed memory to the system afterwards,
and background work waits while the container is close to its limit.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)
_slots: threading.BoundedSemaphore | None = None
_slots_lock = threading.Lock()
_libc = None


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def trim() -> None:
    """Hand memory freed by Python back to the system (glibc keeps it otherwise)."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL('libc.so.6')
        _libc.malloc_trim(0)
    except (OSError, AttributeError):
        _libc = False  # Not glibc; nothing to do.


def _read(path: str) -> int | None:
    try:
        value = Path(path).read_text().strip()
    except OSError:
        return None
    return None if value == 'max' else int(value)


def usage() -> tuple[int, int | None]:
    """Container memory in use and its limit (cgroup v2, then v1); the limit may be unknown."""
    used = _read('/sys/fs/cgroup/memory.current')
    if used is not None:
        limit = _read('/sys/fs/cgroup/memory.max')
    else:
        used = _read('/sys/fs/cgroup/memory/memory.usage_in_bytes') or 0
        limit = _read('/sys/fs/cgroup/memory/memory.limit_in_bytes')
        if limit is not None and limit >= 1 << 60:
            limit = None
    configured = _int_env('MEMORY_LIMIT_MB', 0, 0, 1 << 20)
    if configured:
        limit = configured * 1024 * 1024 if limit is None else min(limit, configured * 1024 * 1024)
    return used, limit


def under_pressure() -> bool:
    """True above MEMORY_PRESSURE_PERCENT (70) of the container limit."""
    used, limit = usage()
    return bool(limit) and used >= limit * _int_env('MEMORY_PRESSURE_PERCENT', 70, 10, 100) / 100


def _semaphore() -> threading.BoundedSemaphore:
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(_int_env('HEAVY_COMPUTE_CONCURRENCY', 2, 1, 32))
        return _slots


@contextmanager
def heavy_slot():
    """At most HEAVY_COMPUTE_CONCURRENCY (2) heavy computations hold their rows at once."""
    slots = _semaphore()
    slots.acquire()
    try:
        yield
    finally:
        slots.release()
        trim()
