"""Permission-checked cache invalidation and persistent background warming jobs."""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta
from threading import Event, Lock, Thread
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, StringConstraints, model_validator

from app.bi.engine import QueryEngine, QueryError
from app.bi.models import BiModel, DateRange, QueryRequest
from app.bi.store import BiConflict, get_bi_store
from app.core.cache import cache
from app.core.config import get_backend_config, get_settings

logger = logging.getLogger(__name__)
_warming_lock = Lock()
_running_jobs: set[str] = set()


class CacheInvalidation(BiModel):
    target: Literal['PERIOD', 'CURRENT_PERIOD', 'METRIC', 'KPI', 'DICTIONARY', 'ALL']
    date_range: DateRange | None = None
    key: str | None = Field(default=None, min_length=1, max_length=128, pattern=r'^[a-zA-Z][a-zA-Z0-9_]*$')

    @model_validator(mode='after')
    def required_fields(self):
        if self.target == 'PERIOD' and self.date_range is None:
            raise ValueError('A period invalidation requires a date range')
        if self.target in {'METRIC', 'KPI'} and self.key is None:
            raise ValueError('Metric/KPI invalidation requires its key')
        if self.date_range and (self.date_range.date_to - self.date_range.date_from).days > 3660:
            raise ValueError('Invalidation range cannot exceed ten years')
        return self


class CacheWarmRequest(BiModel):
    periods: list[Literal['CURRENT_MONTH', 'PREVIOUS_MONTH', 'LAST_HORIZON', 'CUSTOM']] = Field(default_factory=lambda: ['CURRENT_MONTH', 'PREVIOUS_MONTH', 'LAST_HORIZON'], min_length=1, max_length=4)
    date_range: DateRange | None = None
    metric_keys: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(default_factory=lambda: ['flights', 'effective', 'efficiency'], min_length=1, max_length=30)

    @model_validator(mode='after')
    def custom_range(self):
        if 'CUSTOM' in self.periods and self.date_range is None:
            raise ValueError('Custom warming requires a date range')
        if len(self.periods) != len(set(self.periods)) or len(self.metric_keys) != len(set(self.metric_keys)):
            raise ValueError('Warming periods and metrics must be unique')
        if self.date_range and (self.date_range.date_to - self.date_range.date_from).days > 3660:
            raise ValueError('Warming range cannot exceed ten years')
        return self


def today() -> date:
    return datetime.now(ZoneInfo(get_backend_config().analytics.timezone)).date()


def period_tags(start: date, end: date) -> list[str]:
    return [f'data:{(start + timedelta(days=offset)).isoformat()}' for offset in range((end - start).days + 1)]


def invalidate(body: CacheInvalidation) -> dict:
    if body.target == 'ALL':
        return {'target': body.target, 'invalidated_entries': cache.clear()}
    if body.target in {'PERIOD', 'CURRENT_PERIOD'}:
        end = body.date_range.date_to if body.target == 'PERIOD' else today()
        start = body.date_range.date_from if body.target == 'PERIOD' else end.replace(day=1)
        tags = period_tags(start, end)
    elif body.target == 'DICTIONARY':
        tags = ['dictionary']
    else:
        tags = [f'{body.target.lower()}:{body.key}']
    return {'target': body.target, 'generation_updates': cache.invalidate_tags(tags), 'affected_partitions': len(tags)}


def warm_queries(body: CacheWarmRequest) -> list[QueryRequest]:
    from app.bi.cache_policy import CachePolicy
    end = today()
    month = end.replace(day=1)
    previous = month - timedelta(days=1)
    oldest = end - timedelta(days=CachePolicy.from_env().horizon_days - 1)
    ranges = {'CURRENT_MONTH': (max(month, oldest), end), 'PREVIOUS_MONTH': (max(previous.replace(day=1), oldest), previous), 'LAST_HORIZON': (oldest, end)}
    if body.date_range:
        if 'CUSTOM' in body.periods and (body.date_range.date_from < oldest or body.date_range.date_to > end):
            raise QueryError('Custom warming must stay within the retained cache horizon through today')
        ranges['CUSTOM'] = (body.date_range.date_from, body.date_range.date_to)
    return [QueryRequest.model_validate({'metrics': [{'key': key} for key in body.metric_keys], 'dimensions': [{'field': 'date', 'granularity': 'day'}, {'field': 'department'}], 'date_range': {'from': ranges[period][0], 'to': ranges[period][1]}}) for period in body.periods if ranges[period][0] <= ranges[period][1]]


def warming_limits() -> tuple[int, int]:
    try:
        duration = int(os.getenv('CACHE_WARM_MAX_SECONDS', '300'))
        lease = int(os.getenv('CACHE_WARM_JOB_LEASE_SECONDS', '90'))
    except ValueError as exc:
        raise QueryError('Cache warming limits must be integer seconds') from exc
    if not 30 <= duration <= 900 or not 5 <= lease <= 300:
        raise QueryError('Cache warming time budget must be 30–900 seconds and lease 5–300 seconds')
    return duration, lease


def start_warming(body: CacheWarmRequest, principal) -> dict:
    settings = get_settings()
    if settings.cache_enabled is False or (settings.cache_enabled is None and not get_backend_config().cache.enabled):
        raise QueryError('Caching is disabled')
    duration, lease = warming_limits()
    store = get_bi_store()
    service = QueryEngine(store)
    queries = warm_queries(body)
    definitions, _ = service._definitions()
    # Validate every metric, dependency, field and scope before creating a job.
    for query in queries:
        service._validated(query, principal, definitions)
    with _warming_lock:
        owner = uuid.uuid4().hex
        job = store.create_cache_job(body.model_dump(mode='json', by_alias=True), principal.user_id, len(queries), lease_owner=owner, lease_seconds=lease, max_runtime_seconds=duration)
        _running_jobs.add(job['id'])
    stopped = Event()
    deadline = time.monotonic() + duration

    def heartbeat():
        while not stopped.wait(lease / 3):
            try:
                if not store.renew_cache_job(job['id'], owner, lease):
                    stopped.set()
                    return
            except Exception:
                logger.warning('analytics_cache_job_heartbeat_failed job_id=%s', job['id'])
                # Retry on the next tick. The SQLite lease handles crashed workers.

    def run():
        completed = 0
        try:
            store.update_cache_job(job['id'], 'running', completed, lease_owner=owner)
            for query in queries:
                if stopped.is_set() or time.monotonic() >= deadline:
                    store.update_cache_job(job['id'], 'failed', completed, 'timeout', lease_owner=owner)
                    return
                service.execute(query, principal)
                completed += 1
                store.update_cache_job(job['id'], 'running', completed, lease_owner=owner)
                if time.monotonic() >= deadline:
                    store.update_cache_job(job['id'], 'failed', completed, 'timeout', lease_owner=owner)
                    return
            store.update_cache_job(job['id'], 'complete', completed, lease_owner=owner)
        except Exception:
            logger.warning('analytics_cache_warming_failed job_id=%s completed=%s', job['id'], completed)
            try:
                store.update_cache_job(job['id'], 'failed', completed, 'analytics_unavailable', lease_owner=owner)
            except Exception:
                # Another worker may already have expired this lost reservation.
                logger.warning('analytics_cache_job_finalization_failed job_id=%s', job['id'])
        finally:
            stopped.set()
            heartbeat_thread.join(timeout=min(5, lease))
            with _warming_lock:
                _running_jobs.discard(job['id'])
    try:
        heartbeat_thread = Thread(target=heartbeat, name=f'analytics-warm-heartbeat-{job["id"][:8]}', daemon=True)
        heartbeat_thread.start()
        Thread(target=run, name=f'analytics-warm-{job["id"][:8]}', daemon=True).start()
    except Exception:
        stopped.set()
        with _warming_lock:
            _running_jobs.discard(job['id'])
        store.update_cache_job(job['id'], 'failed', 0, 'scheduling_failed', lease_owner=owner)
        raise
    return job
