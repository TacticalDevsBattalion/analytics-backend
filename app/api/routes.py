import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from .models import (
    ComparisonRequest,
    ComparisonResponse,
    EventRow,
    FilterOptions,
    FilterRequest,
    GeoFeatureCollection,
    HealthResponse,
    Metric,
    KpiOptions,
    KpiPreviewRequest,
    KpiPreviewResponse,
    SourceStatus,
    TimelinePoint,
)
from app.core.config import get_backend_config, get_settings
from app.core.kpi_configuration import KpiDataUnavailable
from app.core.user_accounts import check_request_origin, legacy_administrator, session_identity
from app.services import analytics, comparison
from app.services.bi_legacy import legacy_operation

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get('/health', response_model=HealthResponse, operation_id='getHealth')
def health():
    return {"status": "ok"}


@router.get('/source/status', response_model=SourceStatus, operation_id='getSourceStatus')
def source_status():
    return analytics.source_status()


@router.get('/filters/options', response_model=FilterOptions, operation_id='getFilterOptions')
def filter_options(request: Request):
    return legacy_operation(request, analytics.options, ('metric.view', 'dashboard.view', 'statistics.view', 'comparison.view'))


@router.get('/source/schema', operation_id='getSourceSchema')
def source_schema(request: Request):
    return legacy_operation(request, analytics.source_schema, ('analytics.admin',))


@router.post('/analytics/overview', response_model=list[Metric], operation_id='getAnalyticsOverview')
def overview(filters: FilterRequest, request: Request):
    try:
        return legacy_operation(request, lambda: analytics.overview(filters))
    except (analytics.UnsupportedKpiSource, KpiDataUnavailable) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/analytics/kpi-options', response_model=KpiOptions, operation_id='getKpiOptions')
def kpi_options(request: Request):
    try:
        return legacy_operation(request, analytics.kpi_options, ('kpi.view', 'kpi.manage', 'analytics.admin'))
    except analytics.UnsupportedKpiSource as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("KPI options source request failed")
        raise HTTPException(status_code=503, detail="Analytics data source is temporarily unavailable") from exc


@router.post('/analytics/kpi-preview', response_model=KpiPreviewResponse, operation_id='previewPurposeKpi')
def kpi_preview(request: KpiPreviewRequest, http_request: Request):
    try:
        return legacy_operation(http_request, lambda: analytics.kpi_preview(request.filters, request.kpi), ('kpi.view', 'kpi.manage', 'analytics.admin'))
    except (analytics.UnsupportedKpiSource, KpiDataUnavailable) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("KPI preview source request failed")
        raise HTTPException(status_code=503, detail="Analytics data source is temporarily unavailable") from exc


@router.post('/analytics/comparison', response_model=ComparisonResponse, operation_id='getAnalyticsComparison')
def compare_analytics(request: ComparisonRequest, http_request: Request):
    try:
        def compare():
            from app.services.bi_legacy import current_principal
            user = current_principal()
            if request.mode != 'periods' and user.user_id is not None and not user.has('comparison.view_peer_raw'):
                raise HTTPException(403, detail='Use the BI comparison API for restricted peer comparison')
            return comparison.compare(request)
        return legacy_operation(http_request, compare, ('comparison.view',))
    except comparison.ComparisonValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except (analytics.UnsupportedKpiSource, KpiDataUnavailable) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Analytics comparison source request failed")
        raise HTTPException(
            status_code=503,
            detail="Analytics data source is temporarily unavailable. Please try again later.",
        ) from exc


@router.post('/analytics/timeline', response_model=list[TimelinePoint], operation_id='getAnalyticsTimeline')
def timeline(filters: FilterRequest, request: Request):
    try:
        return legacy_operation(request, lambda: analytics.timeline(filters))
    except (analytics.UnsupportedKpiSource, KpiDataUnavailable) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post('/analytics/events', response_model=list[EventRow], operation_id='getAnalyticsEvents')
def events(filters: FilterRequest, request: Request):
    return legacy_operation(request, lambda: analytics.events(filters))


@router.post('/analytics/map', response_model=GeoFeatureCollection, operation_id='getAnalyticsMap')
def map_data(filters: FilterRequest, request: Request):
    return legacy_operation(request, lambda: analytics.geojson(filters))


@router.get('/cache/status', operation_id='getCacheStatus')
def cache_status(request: Request):
    return legacy_operation(request, analytics.cache_stats, ('analytics.cache.manage',))


@router.post('/cache/clear', operation_id='clearCache')
def clear_cache(request: Request, namespace: str | None = None):
    settings = get_settings()
    config = get_backend_config().cache

    if not config.admin.enabled:
        # Do not expose an unauthenticated cache-flush endpoint by accident.
        raise HTTPException(status_code=404, detail='Cache administration is disabled')

    user = session_identity(request)
    session_admin = user is not None and user.role == 'administrator'
    supplied = request.headers.get(config.admin.header, '')
    cache_key = bool(settings.cache_admin_token.strip()) and secrets.compare_digest(supplied.encode('utf-8'), settings.cache_admin_token.encode('utf-8'))
    if not (session_admin or legacy_administrator(request) or cache_key):
        if not settings.cache_admin_token.strip() and user is None:
            raise HTTPException(status_code=404, detail='Cache administration is disabled')
        raise HTTPException(status_code=403, detail='Invalid cache administration token')
    if session_admin and not (legacy_administrator(request) or cache_key):
        check_request_origin(request)

    from app.core.shared_cache import SharedCacheUnavailable
    try:
        cleared = analytics.clear_cache(namespace)
    except SharedCacheUnavailable:
        raise HTTPException(503, 'Shared cache is unavailable; distributed invalidation was not acknowledged') from None
    except ValueError:
        raise HTTPException(422, 'Invalid cache namespace') from None
    return {
        'cleared': cleared,
        'namespace': namespace,
        'stats': analytics.cache_stats(),
    }
