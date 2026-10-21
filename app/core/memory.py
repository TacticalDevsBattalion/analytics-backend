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
_slots: 'Slots | None' = None
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


class Slots:
    """Units of heavy work; a long period takes every unit and so runs alone.

    A waiting long computation is let in before new short ones, so it never starves.
    """

    def __init__(self, capacity: int):
        self.capacity, self.free, self.waiting_whole = capacity, capacity, 0
        self.condition = threading.Condition()

    def acquire(self, units: int = 1) -> int:
        units = min(max(units, 1), self.capacity)
        with self.condition:
            whole = units == self.capacity
            self.waiting_whole += whole
            try:
                self.condition.wait_for(lambda: self.free >= units and (whole or not self.waiting_whole))
            finally:
                self.waiting_whole -= whole
            self.free -= units
        return units

    def release(self, units: int = 1) -> None:
        with self.condition:
            self.free += units
            self.condition.notify_all()


def _semaphore() -> Slots:
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = Slots(_int_env('HEAVY_COMPUTE_CONCURRENCY', 2, 1, 32))
        return _slots


def period_days(date_range) -> int | None:
    """Days in a DateRange model or a {'from', 'to'} dict; None when unknown."""
    from datetime import date
    try:
        if isinstance(date_range, dict):
            start, end = (date.fromisoformat(str(date_range[key])[:10]) for key in ('from', 'to'))
        else:
            start, end = date_range.date_from, date_range.date_to
        return (end - start).days + 1
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


@contextmanager
def heavy_slot(days: int | None = None):
    """At most HEAVY_COMPUTE_CONCURRENCY (2) heavy computations hold their rows at once.

    A period longer than HEAVY_LONG_RANGE_DAYS (45) holds months of rows (~1.2 GB for
    four months) and runs alone, so two of them never meet under a 2 GB limit.
    """
    slots = _semaphore()
    long_range = days is not None and days > _int_env('HEAVY_LONG_RANGE_DAYS', 45, 1, 3660)
    units = slots.acquire(slots.capacity if long_range else 1)
    try:
        yield
    finally:
        slots.release(units)
        trim()
