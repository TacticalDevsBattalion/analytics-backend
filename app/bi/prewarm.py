"""Keeps the quick periods of signed-in users' pages computed in advance.

Every few minutes one worker (a shared lease) goes through the periods of the filter
bar's quick buttons, calendar and reporting day, and computes Dashboard, Statistics
and the personal dashboard for every user with a valid session. Ready pages are
skipped, so a pass costs little until data expires; then the next pass recomputes it
before anyone asks.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from threading import Event, Thread

logger = logging.getLogger(__name__)
_stop = Event()
_thread: Thread | None = None
LEASE = 'bi.prewarm'


@dataclass(frozen=True)
class PrewarmSettings:
    enabled: bool = True
    interval_seconds: int = 240
    max_seconds: int = 600
    days: tuple[int, ...] = (7, 14, 30)
    reporting_cutoffs: tuple[str, ...] = ('15:00', '16:30')

    @classmethod
    def from_env(cls) -> 'PrewarmSettings':
        def numbers(name, default):
            value = os.getenv(name)
            return default if value is None else tuple(int(item) for item in value.split(',') if item.strip())
        cutoffs = os.getenv('CACHE_PREWARM_REPORTING_CUTOFFS')
        return cls(
            enabled=os.getenv('CACHE_PREWARM_ENABLED', 'true').lower() in {'1', 'true', 'yes'},
            interval_seconds=max(60, int(os.getenv('CACHE_PREWARM_INTERVAL_SECONDS', '240'))),
            max_seconds=max(30, int(os.getenv('CACHE_PREWARM_MAX_SECONDS', '600'))),
            days=numbers('CACHE_PREWARM_DAYS', cls.days),
            reporting_cutoffs=cls.reporting_cutoffs if cutoffs is None else tuple(item.strip() for item in cutoffs.split(',') if item.strip()),
        )


def quick_periods(today: date, settings: PrewarmSettings) -> list[dict]:
    """The filter bar's quick periods, most used first; mirrors FilterBar.buildQuickPeriod."""
    month_start = today.replace(day=1)
    calendar = [(today, today), (today - timedelta(days=1), today - timedelta(days=1)), *((today - timedelta(days=days - 1), today) for days in settings.days), (month_start, today)]
    # A reporting day N runs from the cutoff on day N-1 to the cutoff on day N.
    reporting = [(today - timedelta(days=1), today), (today - timedelta(days=2), today - timedelta(days=1)), *((today - timedelta(days=days), today) for days in settings.days), (month_start - timedelta(days=1), today)]
    periods = []
    for index, (start, end) in enumerate(calendar):
        periods.append({'date_range': {'from': start, 'to': end}})
        for cutoff in settings.reporting_cutoffs:
            start, end = reporting[index]
            periods.append({'date_range': {'from': start, 'to': end}, 'time_from': cutoff, 'time_to': cutoff})
    return periods


def principals() -> list:
    from app.bi.models import DataScope
    from app.bi.security import DEFAULT_VIEWER_PERMISSIONS, Principal, principal_for_user
    from app.core.keycloak import enabled as keycloak_enabled
    from app.core.user_accounts import get_user_account_store
    store = get_user_account_store()
    if not keycloak_enabled() and not store.enabled():
        return [Principal(None, 'viewer', DEFAULT_VIEWER_PERMISSIONS, DataScope(scope_type='ALL'))]
    return [principal_for_user(user) for user in store.active_users()]


def pages_for(user) -> list:
    """Page definitions the user sees, as the page routes load them."""
    from app.api.analytics_pages import load_page
    pages = []
    for page in ('dashboard', 'statistics'):
        try:
            pages.append(load_page(page, user))
        except Exception:
            continue  # Not permitted for this user.
    if user.user_id and user.has('personal_dashboard.view'):
        from app.api.personal_dashboard import snapshot
        try:
            pages.append(snapshot(user)['dashboard'])
        except Exception:
            pass
    return pages


def run_once(settings: PrewarmSettings | None = None, *, should_stop=lambda: False) -> dict:
    from app.api.analytics_pages import PageQuery
    from app.api.bi_routes import engine
    from app.bi.page_cache import page_json
    from app.core import memory
    settings = settings or PrewarmSettings.from_env()
    deadline = time.monotonic() + settings.max_seconds
    service = engine()
    users = [(user, pages_for(user)) for user in principals()]
    done = failed = 0
    started = time.monotonic()
    for period in quick_periods(service.today(), settings):
        body = PageQuery.model_validate(period)
        for user, pages in users:
            for definition in pages:
                # Never push the container toward its memory limit for a pass that can wait.
                if memory.under_pressure():
                    logger.info('analytics_prewarm_paused_memory pages=%s', done)
                    return {'pages': done, 'failed': failed, 'complete': False}
                if should_stop() or time.monotonic() >= deadline:
                    logger.info('analytics_prewarm_stopped pages=%s failed=%s seconds=%.1f', done, failed, time.monotonic() - started)
                    return {'pages': done, 'failed': failed, 'complete': False}
                try:
                    page_json(definition, body, user, service, serve_stale=False)
                    done += 1
                except Exception as exc:
                    failed += 1
                    logger.warning('analytics_prewarm_failed page=%s error=%s', definition.id, type(exc).__name__)
    logger.info('analytics_prewarm_complete users=%s pages=%s failed=%s seconds=%.1f', len(users), done, failed, time.monotonic() - started)
    return {'pages': done, 'failed': failed, 'complete': True}


def _loop(settings: PrewarmSettings):
    owner = uuid.uuid4().hex
    from app.core.cache import cache
    # Let the server finish starting before the first pass.
    if _stop.wait(20):
        return
    while not _stop.is_set():
        try:
            if cache.lease(LEASE, owner, settings.interval_seconds + settings.max_seconds):
                run_once(settings, should_stop=_stop.is_set)
        except Exception:
            logger.warning('analytics_prewarm_pass_failed', exc_info=True)
        _stop.wait(settings.interval_seconds)


def start() -> bool:
    global _thread
    from app.core.config import get_backend_config, get_settings
    settings = PrewarmSettings.from_env()
    cache_enabled = get_settings().cache_enabled
    if cache_enabled is None:
        cache_enabled = get_backend_config().cache.enabled
    # Without a cache the computed pages would be thrown away.
    if not settings.enabled or not cache_enabled or (_thread is not None and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = Thread(target=_loop, args=(settings,), name='analytics-prewarm', daemon=True)
    _thread.start()
    return True


def stop():
    _stop.set()
