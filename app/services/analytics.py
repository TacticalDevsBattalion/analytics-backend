from __future__ import annotations
import hashlib
import json

from app.api.models import FilterRequest
from app.core.cache import cache
from app.core.config import get_backend_config, get_settings
from app.core.kpi_configuration import KpiConfiguration, current_kpi_snapshot
from . import mock_analytics
from .clickhouse_analytics import clickhouse_analytics


class UnsupportedKpiSource(ValueError):
    pass


def _require_kpi_source(source: str, kpi: KpiConfiguration, *, preview: bool = False):
    if source != "clickhouse" and (preview or kpi.purpose_rules):
        raise UnsupportedKpiSource("Per-purpose KPI calculations require the full ClickHouse flight dataset")


def _source():
    return get_settings().active_source


def _cache_config():
    return get_backend_config().cache


def cache_dependencies(filters: FilterRequest | None = None, *, kpi: bool = False):
    from app.bi.cache_policy import date_tags
    tags = ['dictionary']
    if filters is not None and (filters.date_to - filters.date_from).days <= 3660:
        tags.extend(date_tags(filters.date_from, filters.date_to))
    if kpi:
        tags.append('kpi:mission')
    return tags


def cache_ttl(filters: FilterRequest, ttl: int):
    # Older legacy contracts allow arbitrary ranges. Do not allocate millions
    # of day tags or cache an unfenced result for those exceptional requests.
    return ttl if (filters.date_to - filters.date_from).days <= 3660 else 0


def _semantic_payload(source: str, filters: FilterRequest | None = None, *, include_kpi: bool = True):
    from .bi_legacy import current_principal
    principal = current_principal()
    return {'source': source, 'filters': filters, 'semantic': semantic_signature(include_kpi=include_kpi), 'access': principal.cache_key() if principal else None}


def semantic_signature(*, include_kpi: bool = True):
    config, settings = get_backend_config(), get_settings()
    identity = {name: getattr(settings, name, None) for name in ('active_source', 'clickhouse_url', 'clickhouse_host', 'clickhouse_port', 'clickhouse_database', 'clickhouse_user')}
    payload = {'source': identity, 'tables': config.clickhouse.model_dump(mode='json'), 'fields': config.clickhouse_fields.model_dump(mode='json'), 'analytics': config.analytics.model_dump(mode='json')}
    if include_kpi:
        snapshot = current_kpi_snapshot()
        payload.update(kpi_revision=snapshot.revision, kpi=snapshot.fingerprint)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _options_uncached(source: str):
    if source == "mock":
        return mock_analytics.filter_options()
    return clickhouse_analytics.filter_options()


def options():
    from .bi_legacy import current_principal, scoped_options
    principal = current_principal()
    if principal is not None and (principal.scope.scope_type != 'ALL' or principal.scope.filters):
        return scoped_options(principal)
    source = _source()
    cfg = _cache_config()
    return cache.get_or_load(
        "api.filter_options",
        _semantic_payload(source),
        lambda: _options_uncached(source),
        ttl_seconds=cfg.ttl_seconds.filter_options,
        dependency_tags=cache_dependencies(),
    )


def _overview_uncached(source: str, filters: FilterRequest, kpi: KpiConfiguration):
    _require_kpi_source(source, kpi)
    if source == "mock":
        return mock_analytics.overview(filters)
    return clickhouse_analytics.overview(filters, kpi)


def overview(filters: FilterRequest):
    from .bi_legacy import scoped_filter
    filters = scoped_filter(filters)
    source = _source()
    cfg = _cache_config()
    snapshot = current_kpi_snapshot()
    return cache.get_or_load(
        "api.overview",
        {**_semantic_payload(source, filters), "kpi": snapshot.fingerprint},
        lambda: _overview_uncached(source, filters, snapshot.config),
        ttl_seconds=cache_ttl(filters, cfg.ttl_seconds.overview),
        dependency_tags=cache_dependencies(filters, kpi=True),
    )


def _timeline_uncached(source: str, filters: FilterRequest, kpi: KpiConfiguration):
    _require_kpi_source(source, kpi)
    if source == "mock":
        return mock_analytics.timeline(filters)
    return clickhouse_analytics.timeline(filters, kpi)


def timeline(filters: FilterRequest):
    from .bi_legacy import scoped_filter
    filters = scoped_filter(filters)
    source = _source()
    cfg = _cache_config()
    snapshot = current_kpi_snapshot()
    return cache.get_or_load(
        "api.timeline",
        {**_semantic_payload(source, filters), "kpi": snapshot.fingerprint},
        lambda: _timeline_uncached(source, filters, snapshot.config),
        ttl_seconds=cache_ttl(filters, cfg.ttl_seconds.timeline),
        dependency_tags=cache_dependencies(filters, kpi=True),
    )


def kpi_options():
    source = _source()
    _require_kpi_source(source, KpiConfiguration(), preview=True)
    from .bi_legacy import current_principal, scoped_option_rows
    principal = current_principal()
    if principal is not None and (principal.scope.scope_type != 'ALL' or principal.scope.filters):
        rows, _ = scoped_option_rows(principal)
        return {'purposes': sorted({str(row['main_purpose']) for row in rows if row.get('main_purpose')}), 'results': sorted({str(row['main_result']) for row in rows if row.get('main_result')})}
    return cache.get_or_load(
        "api.kpi_options", _semantic_payload(source), clickhouse_analytics.kpi_options,
        ttl_seconds=_cache_config().ttl_seconds.filter_options,
        stale_if_error_seconds=0, stale_while_revalidate=False,
        dependency_tags=cache_dependencies(),
    )


def kpi_preview(filters: FilterRequest, kpi: KpiConfiguration):
    from .bi_legacy import scoped_filter
    filters = scoped_filter(filters)
    source = _source()
    _require_kpi_source(source, kpi, preview=True)
    from app.core.kpi_configuration import kpi_fingerprint
    return cache.get_or_load('api.kpi_preview', {**_semantic_payload(source, filters, include_kpi=False), 'kpi': kpi_fingerprint(kpi)},
        lambda: clickhouse_analytics.kpi_preview(filters, kpi, source),
        ttl_seconds=cache_ttl(filters, _cache_config().ttl_seconds.overview),
        dependency_tags=cache_dependencies(filters, kpi=True), stale_if_error_seconds=0, stale_while_revalidate=False)


def _events_uncached(source: str, filters: FilterRequest):
    if source == "mock":
        return mock_analytics.events(filters)
    return clickhouse_analytics.events(filters)


def events(filters: FilterRequest):
    from .bi_legacy import scoped_filter
    filters = scoped_filter(filters)
    source = _source()
    cfg = _cache_config()
    return cache.get_or_load(
        "api.events",
        _semantic_payload(source, filters),
        lambda: _events_uncached(source, filters),
        ttl_seconds=cache_ttl(filters, cfg.ttl_seconds.events),
        dependency_tags=cache_dependencies(filters),
    )


def _geojson_uncached(source: str, filters: FilterRequest):
    if source == "mock":
        return mock_analytics.geojson(filters)
    return clickhouse_analytics.geojson(filters)


def geojson(filters: FilterRequest):
    from .bi_legacy import scoped_filter
    filters = scoped_filter(filters)
    source = _source()
    cfg = _cache_config()
    return cache.get_or_load(
        "api.map",
        _semantic_payload(source, filters),
        lambda: _geojson_uncached(source, filters),
        ttl_seconds=cache_ttl(filters, cfg.ttl_seconds.map),
        dependency_tags=cache_dependencies(filters),
    )


def _source_status_uncached(settings, source: str):
    if source == "mock":
        return {
            "source": "mock",
            "label": "Demo data",
            "connected": True,
            "upstream": None,
            "message": "Local synthetic dataset",
        }
    return clickhouse_analytics.status()


def source_status():
    settings = get_settings()
    source = settings.active_source
    cfg = _cache_config()
    return cache.get_or_load(
        "api.source_status",
        {"source": source},
        lambda: _source_status_uncached(settings, source),
        ttl_seconds=cfg.ttl_seconds.source_status,
        stale_if_error_seconds=0,
        stale_while_revalidate=False,
    )



def source_schema():
    source = _source()
    if source == "clickhouse":
        return clickhouse_analytics.schema_status()
    return {"source": source, "entities": {}}

def warm_cache() -> None:
    cfg = _cache_config()
    if cfg.enabled and cfg.warmup.filter_options:
        options()


def clear_cache(namespace: str | None = None) -> int:
    return cache.clear(namespace)


def cache_stats():
    return cache.stats()
