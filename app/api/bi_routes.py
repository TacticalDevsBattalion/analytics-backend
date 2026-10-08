"""Version 2 BI definitions and one permission-aware analytics API."""
from __future__ import annotations

import logging
import sqlite3
from typing import Literal, Union

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field, ValidationError

from app.bi.engine import QueryEngine, QueryError, QueryPermissionError, validate_filters
from app.bi.formula import FormulaError, validate_formula
from app.bi.models import (
    ENTITY_MODELS, BatchQueryRequest, BiComparisonRequest, BiModel,
    CategoryWeight, DashboardDefinition, DataScope, DateRange, KpiDefinition, MetricDefinition,
    QueryFilter, QueryRequest, QueryResult, RoleDefinition, UserAccessDefinition,
    WidgetDefinition, WidgetLayout, WeightSet,
)
from app.bi.security import (
    PERMISSIONS, Principal, apply_user_data_scope, dashboard_visible,
    require_permission, resolve_principal, visible_dashboard, widget_visible,
)
from app.bi.store import BiConflict, BiNotFound, get_bi_store
from app.bi.visualizations import VISUALIZATIONS
from app.core.user_accounts import account_operation, get_user_account_store
from app.core.user_accounts import check_request_origin
from app.bi.cache_admin import CacheInvalidation, CacheWarmRequest
from app.core.shared_cache import SharedCacheUnavailable
from app.bi.mission_kpi import MissionKpiRequest

logger = logging.getLogger(__name__)


def no_store(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(prefix='/v1', dependencies=[Depends(no_store)])
Entity = Union[DashboardDefinition, WidgetDefinition, MetricDefinition, KpiDefinition, CategoryWeight, WeightSet, RoleDefinition, UserAccessDefinition]


def principal(request: Request) -> Principal:
    return resolve_principal(request)


def engine() -> QueryEngine:
    return QueryEngine(get_bi_store())


def operation(callback):
    try:
        return callback()
    except HTTPException:
        raise
    except (QueryPermissionError, PermissionError):
        raise HTTPException(403, 'Requested analytics access is not permitted') from None
    except BiNotFound:
        raise HTTPException(404, 'BI definition was not found') from None
    except BiConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except SharedCacheUnavailable:
        raise HTTPException(503, 'Shared cache is unavailable; distributed invalidation was not acknowledged') from None
    except (QueryError, FormulaError, ValueError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            detail = '; '.join(error['msg'] for error in exc.errors())
        else:
            detail = str(exc)
        raise HTTPException(422, detail) from None
    except (sqlite3.Error, OSError):
        logger.exception('BI storage operation failed')
        raise HTTPException(503, 'BI configuration storage is temporarily unavailable') from None
    except Exception:
        logger.exception('BI analytics operation failed')
        raise HTTPException(503, 'ClickHouse data is temporarily unavailable') from None


@router.get('/analytics/metadata', operation_id='getBiMetadata')
def metadata(user: Principal = Depends(principal)):
    if not user.has('metric.manage') and not user.is_administrator:
        from app.bi.catalog import catalog
        return operation(lambda: catalog(engine(), user))
    data = operation(lambda: engine().metadata(user))
    data['permissions'] = sorted((set(user.effective_permissions) - {'*'}) | {permission for permission in PERMISSIONS if user.has(permission)})
    data['data_scope'] = user.scope.model_dump(mode='json')
    data['scope'] = data['data_scope']
    data['access_revision'] = user.access_revision
    data['configuration_revision'] = get_bi_store().configuration_revision()
    data['permission_registry'] = list(PERMISSIONS) if user.is_administrator else []
    data['sources'] = ['flights', 'events', 'ammunition', 'lost_devices']
    data['visualizations'] = list(VISUALIZATIONS)
    return data


@router.get('/analytics/catalog', operation_id='getAnalyticsCatalog')
def semantic_catalog(user: Principal = Depends(principal)):
    from app.bi.catalog import catalog
    return operation(lambda: catalog(engine(), user))


@router.post('/analytics/catalog/options', operation_id='getAnalyticsCatalogOptions')
def semantic_options(body: DateRange, user: Principal = Depends(principal)):
    from app.bi.catalog import filter_options
    return operation(lambda: filter_options(engine(), user, body))


@router.post('/analytics/query', response_model=QueryResult, operation_id='queryBiAnalytics')
def query(body: QueryRequest, user: Principal = Depends(principal)):
    return operation(lambda: engine().execute(body, user))


@router.post('/analytics/query/batch', operation_id='batchQueryBiAnalytics')
def batch(body: BatchQueryRequest, user: Principal = Depends(principal)):
    return operation(lambda: engine().batch(body.queries, user))


@router.post('/analytics/comparison', operation_id='compareBiAnalytics')
def comparison(body: BiComparisonRequest, user: Principal = Depends(principal)):
    return operation(lambda: engine().comparison(body, user))


@router.post('/analytics/kpi/missions', operation_id='queryMissionKpi')
def mission_kpi(body: MissionKpiRequest, user: Principal = Depends(principal)):
    from app.bi.mission_kpi import calculate
    from app.core.app_configuration import ConfigurationRevisionNotFound
    def run():
        try:
            return calculate(body, user, engine())
        except ConfigurationRevisionNotFound:
            raise HTTPException(404, 'Published KPI rule revision was not found') from None
    return operation(run)


@router.get('/analytics/hierarchy', operation_id='getBiHierarchy')
def hierarchy(user: Principal = Depends(principal)):
    require_permission(user, 'metric.view')
    from app.services.bi_legacy import hierarchy_options
    return operation(lambda: hierarchy_options(user))


@router.get('/admin/cache/status', operation_id='getAnalyticsCacheStatus')
def cache_status(user: Principal = Depends(principal)):
    require_permission(user, 'analytics.cache.manage')
    from app.core.cache import cache
    from app.bi.cache_policy import CachePolicy
    from dataclasses import asdict
    return operation(lambda: {'cache': cache.stats(), 'policy': asdict(CachePolicy.from_env())})


@router.post('/admin/cache/invalidate', operation_id='invalidateAnalyticsCache')
def cache_invalidate(body: CacheInvalidation, request: Request, user: Principal = Depends(principal)):
    require_permission(user, 'analytics.cache.manage')
    check_request_origin(request)
    from app.bi.cache_admin import invalidate
    return operation(lambda: invalidate(body))


@router.post('/admin/cache/warm', status_code=202, operation_id='warmAnalyticsCache')
def cache_warm(body: CacheWarmRequest, request: Request, user: Principal = Depends(principal)):
    require_permission(user, 'analytics.cache.manage')
    check_request_origin(request)
    from app.bi.cache_admin import start_warming
    return operation(lambda: start_warming(body, user))


@router.get('/admin/cache/jobs/{job_id}', operation_id='getAnalyticsCacheJob')
def cache_job(job_id: str, user: Principal = Depends(principal)):
    require_permission(user, 'analytics.cache.manage')
    return operation(lambda: get_bi_store().cache_job(job_id))


@router.get('/analytics/dashboards', response_model=list[DashboardDefinition], operation_id='listBiDashboards')
def dashboards(type: Literal['DASHBOARD', 'STATISTICS', 'COMPARISON'] | None = None, user: Principal = Depends(principal)):
    if type:
        require_permission(user, {'DASHBOARD': 'dashboard.view', 'STATISTICS': 'statistics.view', 'COMPARISON': 'comparison.view'}[type])
    return operation(lambda: [load_dashboard(item.id, user) for item in get_bi_store().list('dashboards') if (type is None or item.type == type) and dashboard_visible(item, user)])


def load_dashboard(dashboard_id: str, user: Principal):
    if dashboard_id in CANONICAL_PAGES:
        from app.api.analytics_pages import load_page
        return load_page(CANONICAL_PAGES[dashboard_id], user)
    definition = visible_dashboard(get_bi_store().get('dashboards', dashboard_id), user, get_bi_store())
    available = {item['key'] for item in engine().metadata(user)['metrics']}
    definition.widgets = [widget for widget in definition.widgets if all(item.key in available for item in widget.query.metrics)]
    if not user.is_administrator:
        definition.assignments = []
    return definition


@router.get('/analytics/dashboards/{dashboard_id}', response_model=DashboardDefinition, operation_id='getBiDashboard')
def dashboard(dashboard_id: str, user: Principal = Depends(principal)):
    return operation(lambda: load_dashboard(dashboard_id, user))


def validate_widget(widget: WidgetDefinition, user: Principal):
    if widget.visualization.type not in VISUALIZATIONS:
        raise QueryError('Unsupported visualization type')
    validate_filters([*widget.query.filters, *widget.fixed_filters])
    if widget.visualization.type in {'text', 'markdown'}:
        return
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.core.config import get_backend_config
    today = datetime.now(ZoneInfo(get_backend_config().analytics.timezone)).date()
    query = widget.query.model_copy(deep=True)
    query.data_scope = widget.data_scope
    query.date_range = query.date_range or DateRange.model_validate({'from': today, 'to': today})
    if widget.data_scope == 'FIXED_SCOPE':
        query.fixed_scope = DataScope(scope_type='CUSTOM', filters=widget.fixed_filters)
    service = engine()
    service._validated(query, user, service._definitions()[0])


@router.post('/analytics/dashboards/{dashboard_id}/widgets', response_model=WidgetDefinition, operation_id='savePersonalBiWidget')
def save_personal_widget(dashboard_id: str, body: WidgetDefinition, request: Request, user: Principal = Depends(principal)):
    check_request_origin(request)
    if dashboard_id in CANONICAL_PAGES:
        if dashboard_id != 'system-dashboard':
            raise HTTPException(403, 'Personal widgets are disabled on Statistics')
        from app.api.personal_dashboard import save_widget
        return save_widget(body, request, user)
    def run():
        definition = load_dashboard(dashboard_id, user)
        if user.user_id is None or definition.type != 'DASHBOARD' or definition.layout_mode not in {'CUSTOMIZABLE', 'FREE'}:
            raise HTTPException(403, 'Personal widgets are disabled')
        existing = next((item for item in get_bi_store().personal_widgets(dashboard_id, user.user_id) if item.id == body.id), None)
        require_permission(user, 'widget.edit' if existing else 'widget.create')
        if body.owner_id not in {None, user.user_id} or body.data_scope != 'USER_SCOPE' or body.query.data_scope != 'USER_SCOPE' or body.query.fixed_scope is not None:
            raise HTTPException(403, 'Personal widgets must use your assigned data scope')
        if existing is None and len(definition.widgets) >= 100:
            raise QueryError('A dashboard supports at most 100 widgets')
        validate_widget(body, user)
        body.is_locked = False
        return get_bi_store().save_personal_widget(dashboard_id, user.user_id, body)
    return operation(run)


@router.delete('/analytics/dashboards/{dashboard_id}/widgets/{widget_id}', operation_id='deletePersonalBiWidget')
def delete_personal_widget(dashboard_id: str, widget_id: str, expected_revision: int, request: Request, user: Principal = Depends(principal)):
    check_request_origin(request)
    if dashboard_id in CANONICAL_PAGES:
        if dashboard_id != 'system-dashboard':
            raise HTTPException(403, 'Personal widgets are disabled on Statistics')
        from app.api.personal_dashboard import remove_widget
        return remove_widget(widget_id, expected_revision, request, user)
    require_permission(user, 'widget.delete')
    def run():
        definition = load_dashboard(dashboard_id, user)
        if user.user_id is None or definition.type != 'DASHBOARD' or definition.layout_mode not in {'CUSTOMIZABLE', 'FREE'}:
            raise HTTPException(403, 'Personal widgets are disabled')
        get_bi_store().delete_personal_widget(dashboard_id, user.user_id, widget_id, expected_revision)
        return {'deleted': True}
    return operation(run)


class DashboardQuery(BiModel):
    date_range: DateRange
    filters: list[QueryFilter] = Field(default_factory=list, max_length=100)


@router.post('/analytics/dashboards/{dashboard_id}/query', operation_id='queryBiDashboard')
def dashboard_query(dashboard_id: str, body: DashboardQuery, user: Principal = Depends(principal)):
    def run():
        definition = load_dashboard(dashboard_id, user)
        if dashboard_id in CANONICAL_PAGES:
            from app.api.analytics_pages import PageQuery
            from app.bi.widgets import query_page
            return query_page(definition, PageQuery.model_validate(body.model_dump()), user, engine())
        requests = []
        for widget in definition.widgets:
            item = widget.query.model_copy(deep=True)
            item.data_scope = widget.data_scope
            if widget.data_scope == 'FIXED_SCOPE':
                item.fixed_scope = DataScope(scope_type='CUSTOM', filters=widget.fixed_filters)
            item.filters = [*item.filters, *widget.fixed_filters]
            if widget.inherit_global_filters:
                item.filters.extend([*definition.global_filters, *body.filters])
            if widget.date_mode == 'INHERIT_GLOBAL_DATE':
                item.date_range = body.date_range
            requests.append(item)
        results = engine().batch(requests, user)['results'] if requests else []
        output = []
        for widget, result in zip(definition.widgets, results):
            value = {'widget_id': widget.id, 'status': result['status']}
            if 'data' in result:
                value['data'] = result['data']
            if 'error' in result:
                value['error'] = result['error']['message']
            output.append(value)
        return {'dashboard_id': definition.id, 'revision': definition.revision, 'results': output}
    return operation(run)


class OverrideItem(BiModel):
    widget_id: str
    layout: WidgetLayout


class OverrideUpdate(BiModel):
    expected_revision: int = Field(ge=0)
    overrides: list[OverrideItem] = Field(max_length=100)


@router.get('/analytics/dashboards/{dashboard_id}/overrides', operation_id='getBiDashboardOverrides')
def overrides(dashboard_id: str, user: Principal = Depends(principal)):
    def run():
        if dashboard_id == 'system-dashboard':
            return _personal_override_snapshot(user)
        definition = load_dashboard(dashboard_id, user)
        if user.user_id is None:
            return {'revision': 0, 'overrides': []}
        snapshot = get_bi_store().overrides_snapshot(dashboard_id, user.user_id)
        allowed = {widget.id for widget in definition.widgets}
        snapshot['overrides'] = [{'widget_id': value.widget_id, 'layout': value.layout.model_dump()} for value in snapshot['overrides'] if value.widget_id in allowed]
        return snapshot
    return operation(run)


@router.put('/analytics/dashboards/{dashboard_id}/overrides', operation_id='saveBiDashboardOverrides')
def save_overrides(dashboard_id: str, body: OverrideUpdate, request: Request, user: Principal = Depends(principal)):
    check_request_origin(request)
    if dashboard_id == 'system-dashboard':
        from app.api.personal_dashboard import PersonalUpdate, update_personal
        update_personal(PersonalUpdate.model_validate(body.model_dump()), request, user)
        return operation(lambda: _personal_override_snapshot(user))
    require_permission(user, 'statistics.admin_edit' if dashboard_id == 'system-statistics' else 'dashboard.layout.edit')
    def run():
        definition = load_dashboard(dashboard_id, user)
        if definition.layout_mode == 'LOCKED' or user.user_id is None:
            raise HTTPException(403, 'Personal layout editing is disabled')
        if len({item.widget_id for item in body.overrides}) != len(body.overrides):
            raise QueryError('Layout overrides must have unique widget IDs')
        allowed = {widget.id: widget for widget in definition.widgets if not widget.is_locked}
        for item in body.overrides:
            widget = allowed.get(item.widget_id)
            if widget is None or (not widget.movable and (widget.layout.x, widget.layout.y) != (item.layout.x, item.layout.y)) or (not widget.resizable and (widget.layout.w, widget.layout.h) != (item.layout.w, item.layout.h)):
                raise HTTPException(403, 'A protected or unavailable widget cannot be changed')
        result = get_bi_store().save_overrides(dashboard_id, user.user_id, [item.model_dump() for item in body.overrides], body.expected_revision)
        return {'revision': result['revision'], 'overrides': [{'widget_id': value.widget_id, 'layout': value.layout.model_dump()} for value in result['overrides']]}
    return operation(run)


ENTITY_PERMISSIONS = {'dashboards': 'dashboard.manage', 'widgets': 'widget.edit', 'metrics': 'metric.manage', 'kpis': 'kpi.manage', 'category_weights': 'metric.manage', 'weight_sets': 'metric.manage', 'roles': 'analytics.admin', 'user_access': 'analytics.admin'}
CANONICAL_PAGES = {'system-dashboard': 'dashboard', 'system-statistics': 'statistics'}


def _canonical_permission(dashboard_id):
    return 'statistics.admin_edit' if dashboard_id == 'system-statistics' else 'dashboard.admin_edit'


def _personal_override_snapshot(user):
    from app.api.personal_dashboard import snapshot
    personal = snapshot(user)
    if personal['dashboard'].layout_mode == 'LOCKED':
        return {'revision': personal['revision'], 'overrides': []}
    allowed = {widget.id for widget in personal['dashboard'].widgets}
    values = get_bi_store().overrides_snapshot('system-dashboard', user.user_id)
    return {'revision': personal['revision'], 'overrides': [{'widget_id': value.widget_id, 'layout': value.layout.model_dump()} for value in values['overrides'] if value.widget_id in allowed]}


def _save_canonical_page(definition, request, user):
    from app.api.analytics_pages import save_page
    return save_page(CANONICAL_PAGES[definition.id], definition, request, user)


def _save_canonical_widget(definition, existing, request, user):
    dashboard_id = existing.dashboard_id if existing else definition.dashboard_id
    if existing and definition.dashboard_id != dashboard_id:
        raise QueryError('A canonical widget cannot move between dashboards')
    page = get_bi_store().get('dashboards', dashboard_id)
    page.widgets = [definition if widget.id == definition.id else widget for widget in page.widgets]
    if not existing:
        page.widgets.append(definition)
    saved = _save_canonical_page(page, request, user)
    return next(widget for widget in saved.widgets if widget.id == definition.id)


def admin_access(entity: str, user: Principal, permission: str | None = None):
    if entity not in ENTITY_PERMISSIONS:
        raise HTTPException(404, 'BI entity was not found')
    require_permission(user, permission or ENTITY_PERMISSIONS[entity])


@router.get('/admin/bi/users', operation_id='listBiUsers')
def users(user: Principal = Depends(principal)):
    require_permission(user, 'analytics.admin')
    accounts = account_operation(lambda: get_user_account_store().users())
    return {'users': accounts, 'access': operation(lambda: get_bi_store().list('user_access')), 'roles': operation(lambda: get_bi_store().list('roles')), 'permissions': list(PERMISSIONS)}


@router.get('/admin/bi/{entity}', response_model=list[Entity], operation_id='listBiDefinitions')
def list_definitions(entity: str, user: Principal = Depends(principal)):
    admin_access(entity, user)
    def run():
        definitions = get_bi_store().list(entity)
        if entity in {'dashboards', 'widgets'}:
            definitions = [definition for definition in definitions if (definition.id if entity == 'dashboards' else definition.dashboard_id) not in CANONICAL_PAGES or user.has(_canonical_permission(definition.id if entity == 'dashboards' else definition.dashboard_id))]
        return definitions
    return operation(run)


@router.post('/admin/bi/{entity}', response_model=Entity, operation_id='saveBiDefinition')
def save_definition(entity: str, body: dict, request: Request, user: Principal = Depends(principal)):
    check_request_origin(request)
    def run():
        if entity not in ENTITY_MODELS:
            raise HTTPException(404, 'BI entity was not found')
        definition = ENTITY_MODELS[entity].model_validate(body)
        if entity == 'dashboards' and definition.id in CANONICAL_PAGES:
            return _save_canonical_page(definition, request, user)
        if entity == 'widgets':
            existing = next((item for item in get_bi_store().list('widgets') if item.id == definition.id), None)
            if definition.dashboard_id in CANONICAL_PAGES or (existing and existing.dashboard_id in CANONICAL_PAGES):
                return _save_canonical_widget(definition, existing, request, user)
            admin_access(entity, user, 'widget.create' if existing is None else 'widget.edit')
        else:
            admin_access(entity, user)
        if entity == 'widgets':
            existing = next((item for item in get_bi_store().list('widgets') if item.id == definition.id), None)
            if existing is None:
                require_permission(user, 'widget.create')
            elif existing.is_locked and not user.is_administrator:
                raise HTTPException(403, 'Widget is locked')
        if entity in {'metrics', 'kpis'}:
            engine().validate_definition(entity, definition, user)
            validate_filters(getattr(definition, 'filters', []))
            if definition.formula is not None:
                validate_formula(definition.formula)
        if entity in {'widgets', 'dashboards'}:
            if entity == 'dashboards' and not user.is_administrator:
                previous = next((item for item in get_bi_store().list('dashboards') if item.id == definition.id), None)
                old_widgets = {item.id: item for item in previous.widgets} if previous else {}
                new_widgets = {item.id: item for item in definition.widgets}
                ignored = {'revision', 'created_at', 'updated_at', 'dashboard_id'}
                for widget_id, old_widget in old_widgets.items():
                    new_widget = new_widgets.get(widget_id)
                    changed = new_widget is None or old_widget.model_dump(exclude=ignored) != new_widget.model_dump(exclude=ignored)
                    if changed:
                        if old_widget.is_locked:
                            raise HTTPException(403, 'Widget is locked')
                        require_permission(user, 'widget.delete' if new_widget is None else 'widget.edit')
                if any(widget_id not in old_widgets for widget_id in new_widgets):
                    require_permission(user, 'widget.create')
            widgets = [definition] if entity == 'widgets' else definition.widgets
            for widget in widgets:
                if widget.owner_id is not None:
                    raise QueryError('Personal widgets must be saved through their owner endpoint')
                if widget.visualization.type not in VISUALIZATIONS:
                    raise QueryError('Unsupported visualization type')
                validate_filters([*widget.query.filters, *widget.fixed_filters])
                if widget.data_scope == 'GLOBAL':
                    require_permission(user, 'analytics.global')
        if entity == 'user_access':
            if not any(account.id == definition.user_id for account in account_operation(lambda: get_user_account_store().users())):
                raise BiNotFound('Assigned user was not found')
        return get_bi_store().save(entity, definition, actor=user.user_id)
    return operation(run)


class DefinitionPreview(BiModel):
    entity: Literal['metrics', 'kpis']
    definition: dict
    query: QueryRequest


# Literal routes must precede /admin/bi/{entity} in FastAPI matching order.
@router.post('/admin/bi/preview', response_model=QueryResult, operation_id='previewBiDefinition')
def preview_definition(body: DefinitionPreview, user: Principal = Depends(principal)):
    admin_access(body.entity, user)
    return operation(lambda: engine().preview(body.entity, body.definition, body.query, user))


router.routes.sort(key=lambda route: '{entity}' in route.path)


@router.delete('/admin/bi/{entity}/{entity_id}', operation_id='deleteBiDefinition')
def delete_definition(entity: str, entity_id: str, expected_revision: int, request: Request, user: Principal = Depends(principal)):
    check_request_origin(request)
    def run():
        if entity == 'dashboards':
            if entity_id in CANONICAL_PAGES:
                require_permission(user, _canonical_permission(entity_id))
            else:
                admin_access(entity, user)
        elif entity == 'widgets':
            if not any(user.has(permission) for permission in ('widget.delete', 'dashboard.admin_edit', 'statistics.admin_edit')):
                raise PermissionError('Widget removal is not permitted')
        else:
            admin_access(entity, user)
        if entity in {'dashboards', 'widgets'}:
            definition = get_bi_store().get(entity, entity_id)
            canonical_id = definition.id if entity == 'dashboards' else definition.dashboard_id
            if canonical_id in CANONICAL_PAGES:
                require_permission(user, _canonical_permission(canonical_id))
                if definition.revision != expected_revision:
                    raise BiConflict('BI definition changed elsewhere; reload before deleting')
                if entity == 'widgets':
                    page = get_bi_store().get('dashboards', canonical_id)
                    if not any(widget.id == entity_id and widget.revision == expected_revision for widget in page.widgets):
                        raise BiConflict('BI definition changed elsewhere; reload before deleting')
                    page.widgets = [widget for widget in page.widgets if widget.id != entity_id]
                    _save_canonical_page(page, request, user)
                else:
                    if any(widget.is_locked or widget.mandatory or not widget.removable for widget in definition.widgets):
                        raise PermissionError('A protected canonical page cannot be removed')
                    get_bi_store().delete(entity, entity_id, expected_revision)
                return
        admin_access(entity, user, 'widget.delete' if entity == 'widgets' else None)
        if entity == 'widgets' and get_bi_store().get(entity, entity_id).is_locked and not user.is_administrator:
            raise HTTPException(403, 'Widget is locked')
        if entity == 'dashboards' and not user.is_administrator:
            definition = get_bi_store().get(entity, entity_id)
            if any(widget.is_locked for widget in definition.widgets):
                raise HTTPException(403, 'Widget is locked')
            if definition.widgets:
                require_permission(user, 'widget.delete')
        get_bi_store().delete(entity, entity_id, expected_revision)
    operation(run)
    return {'deleted': True}
