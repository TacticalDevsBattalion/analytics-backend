"""Temporal cache policy, separate from the supported analytics date range."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def reporting_today() -> date:
    from app.core.config import get_backend_config
    return datetime.now(ZoneInfo(get_backend_config().analytics.timezone)).date()


def days(start: date, end: date):
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def date_tags(start: date, end: date) -> list[str]:
    return [f"data:{day.isoformat()}" for day in days(start, end)]


@dataclass(frozen=True)
class CachePolicy:
    horizon_days: int = 120
    hot_days: int = 2
    warm_days: int = 14
    hot_ttl_seconds: int = 600
    warm_ttl_seconds: int = 21600
    historical_ttl_seconds: int = 172800
    raw_max_rows: int = 10000

    @classmethod
    def from_env(cls) -> "CachePolicy":
        values = {}
        for name in cls.__dataclass_fields__:
            value = os.environ.get(f"ANALYTICS_CACHE_{name.upper()}")
            if value is not None:
                try:
                    values[name] = int(value)
                except ValueError as exc:
                    raise ValueError(f"ANALYTICS_CACHE_{name.upper()} must be an integer") from exc
        policy = cls(**values)
        if not 1 <= policy.horizon_days <= 3660 or not 0 <= policy.hot_days <= policy.warm_days <= policy.horizon_days:
            raise ValueError("Analytics cache age bands must be ordered within the cache horizon")
        if any(getattr(policy, name) <= 0 for name in ("hot_ttl_seconds", "warm_ttl_seconds", "historical_ttl_seconds", "raw_max_rows")):
            raise ValueError("Analytics cache TTLs and row limit must be positive")
        return policy

    def ttl_for_day(self, day: date, today: date | None = None) -> int:
        age = ((today or reporting_today()) - day).days
        if age > self.horizon_days:
            return 0
        if age <= self.hot_days:
            return self.hot_ttl_seconds
        if age <= self.warm_days:
            return self.warm_ttl_seconds
        return self.historical_ttl_seconds

    def ttl_for_range(self, start: date, end: date, today: date | None = None) -> int:
        current = today or reporting_today()
        if start > end or self.ttl_for_day(start, current) == 0:
            return 0
        return min(self.ttl_for_day(start, current), self.ttl_for_day(end, current))


def ttl_for_day(day: date, today: date | None = None) -> int:
    return CachePolicy.from_env().ttl_for_day(day, today)


def ttl_for_range(start: date, end: date, today: date | None = None) -> int:
    return CachePolicy.from_env().ttl_for_range(start, end, today)


def get_policy() -> CachePolicy:
    return CachePolicy.from_env()
