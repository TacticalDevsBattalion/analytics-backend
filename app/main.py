import logging
import os
import time
import uuid
from threading import Thread

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.api.configuration_routes import router as configuration_router
from app.api.account_routes import router as account_router
from app.api.keycloak_routes import router as keycloak_router
from app.api.bi_routes import router as bi_router
from app.api.kpi_policy_routes import router as kpi_policy_router
from app.api.analytics_pages import router as analytics_pages_router
from app.api.personal_dashboard import router as personal_dashboard_router
from app.core.config import get_backend_config, get_settings
from app.core.user_accounts import require_analytics_access
from app.services import analytics
from app.core.observability import request_id

settings = get_settings()
config = get_backend_config()
api_config = config.app.api
cors_config = config.app.cors
logger = logging.getLogger(__name__)
level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=level if level in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'} else 'INFO')
# httpx INFO includes full source URLs and SQL parameter query strings.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

app = FastAPI(
    title=api_config.title,
    version=api_config.version,
    docs_url=api_config.docs_url,
    openapi_url=api_config.openapi_url,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=cors_config.allow_credentials,
    allow_methods=cors_config.allow_methods,
    allow_headers=cors_config.allow_headers,
)
app.include_router(router, prefix=api_config.router_prefix, dependencies=[Depends(require_analytics_access)])
app.include_router(configuration_router, prefix=api_config.router_prefix)
app.include_router(account_router, prefix=api_config.router_prefix)
app.include_router(keycloak_router, prefix=api_config.router_prefix)
app.include_router(bi_router, prefix=api_config.router_prefix)
app.include_router(kpi_policy_router, prefix=api_config.router_prefix)
app.include_router(analytics_pages_router, prefix=api_config.router_prefix)
app.include_router(personal_dashboard_router, prefix=api_config.router_prefix)


@app.middleware('http')
async def performance_log(request, call_next):
    identifier = uuid.uuid4().hex
    token = request_id.set(identifier)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers['X-Request-ID'] = identifier
        logger.info('http_request query_id=%s method=%s path=%s status=%s total_ms=%.2f', identifier, request.method, request.url.path, response.status_code, (time.perf_counter() - started) * 1000)
        return response
    finally:
        request_id.reset(token)


@app.get('/health', include_in_schema=False)
def process_health():
    # Redis/ClickHouse outages are reported by authenticated diagnostics. They
    # must not create a restart loop for an otherwise available API process.
    return {'status': 'ok'}


@app.on_event("startup")
def warm_runtime_cache() -> None:
    from app.bi.bootstrap import bootstrap
    from app.bi.store import get_bi_store
    bootstrap(get_bi_store())
    def legacy_warm():
        try:
            analytics.warm_cache()
        except Exception:
            logger.warning('Backend option cache warmup unavailable')
    Thread(target=legacy_warm, name='analytics-options-warm', daemon=True).start()
    if os.getenv('CACHE_WARM_ON_STARTUP', 'false').lower() in {'1', 'true', 'yes'}:
        from app.bi.cache_admin import CacheWarmRequest, start_warming
        from app.bi.models import DataScope
        from app.bi.security import Principal
        try:
            start_warming(CacheWarmRequest(), Principal(None, 'administrator', frozenset({'*'}), DataScope(scope_type='ALL'), 'EXACT'))
        except Exception:
            logger.warning('Backend aggregate cache warmup could not be scheduled')

