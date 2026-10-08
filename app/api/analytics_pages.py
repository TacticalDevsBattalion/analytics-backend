"""A single Dashboard and Statistics using the same stored widget engine."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from app.api.bi_routes import principal, engine, operation, no_store
from app.bi.catalog import catalog
from app.bi.models import BiModel, DashboardDefinition, DateRange, QueryFilter, WidgetDefinition
from app.bi.security import dashboard_visible, require_permission, widget_visible
from app.bi.store import get_bi_store
from app.bi.widgets import query_page, validate_widget
from app.core.user_accounts import check_request_origin

router = APIRouter(prefix='/v1/analytics', dependencies=[Depends(no_store)])
Page = Literal['dashboard', 'statistics']


class PageQuery(BiModel):
    date_range: DateRange
    filters: list[QueryFilter] = Field(default_factory=list, max_length=100)
    time_from: str | None = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$')
    time_to: str | None = Field(default=None, pattern=r'^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$')

    def bounded_filters(self):
        from datetime import datetime, time
        from zoneinfo import ZoneInfo
        from app.core.config import get_backend_config
        zone = ZoneInfo(get_backend_config().analytics.timezone)
        start = datetime.combine(self.date_range.date_from, time.fromisoformat(self.time_from) if self.time_from else time.min, zone)
        end = datetime.combine(self.date_range.date_to, time.fromisoformat(self.time_to) if self.time_to else time.max, zone)
        if start > end:
            raise ValueError('Date/time range end precedes start')
        return [*self.filters, *([QueryFilter(field='local_timestamp', operator='gte', value=start.timestamp()), QueryFilter(field='local_timestamp', operator='lte', value=end.timestamp())] if self.time_from or self.time_to else [])]


class WidgetPreview(PageQuery):
    widget: WidgetDefinition


@router.post('/widgets/preview', operation_id='previewAnalyticsWidget')
def preview_widget(body: WidgetPreview, user=Depends(principal)):
    def run():
        widget = body.widget
        if widget.data_scope == 'GLOBAL':
            require_permission(user, 'analytics.global_query')
        if not widget_visible(widget, user):
            raise PermissionError('The widget is unavailable')
        validate_widget(widget, user, engine())
        definition = DashboardDefinition(id='preview', name='Preview', slug='preview', widgets=[widget])
        result = query_page(definition, body, user, engine())['results'][0]
        if result['status'] == 'error':
            raise ValueError(result['error'])
        return result['data']
    return operation(run)


def load_page(page, user, *, personal=False):
    definition = get_bi_store().get('dashboards', 'system-dashboard' if page == 'dashboard' else 'system-statistics')
    if not personal and not dashboard_visible(definition, user):
        raise HTTPException(403, 'This analytics page is not permitted')
    allowed = {metric['key'] for metric in catalog(engine(), user)['metrics']}
    definition.widgets = [widget for widget in definition.widgets if widget_visible(widget, user) and all(metric.key in allowed for metric in widget.query.metrics)]
    if not user.is_administrator:
        definition.assignments = []
    return definition


@router.get('/pages/{page}', response_model=DashboardDefinition, operation_id='getAnalyticsPage')
def get_page(page: Page, user=Depends(principal)):
    return operation(lambda: load_page(page, user))


@router.put('/pages/{page}', response_model=DashboardDefinition, operation_id='saveAnalyticsPage')
def save_page(page: Page, body: DashboardDefinition, request: Request, user=Depends(principal)):
    require_permission(user, 'dashboard.admin_edit' if page == 'dashboard' else 'statistics.admin_edit')
    check_request_origin(request)
    def run():
        current = get_bi_store().get('dashboards', 'system-dashboard' if page == 'dashboard' else 'system-statistics')
        if body.id != current.id or body.type != current.type or not body.is_system or body.slug != current.slug:
            raise ValueError('The canonical page identity cannot change')
        desired = {widget.id: widget for widget in body.widgets}
        for old in current.widgets:
            updated = desired.get(old.id)
            if updated is None and (old.is_locked or old.mandatory or not old.removable):
                raise PermissionError('A protected system widget cannot be removed')
            if updated and old.is_locked and old.model_dump(exclude={'revision', 'updated_at', 'created_at'}) != updated.model_dump(exclude={'revision', 'updated_at', 'created_at'}):
                raise PermissionError('A locked widget cannot be changed')
            if updated and ((not old.movable and (old.layout.x, old.layout.y) != (updated.layout.x, updated.layout.y)) or (not old.resizable and (old.layout.w, old.layout.h) != (updated.layout.w, updated.layout.h))):
                raise PermissionError('The widget layout is protected')
        for widget in body.widgets:
            if widget.owner_id is not None or widget.widget_kind != 'SYSTEM_WIDGET':
                raise PermissionError('Personal widgets are stored separately')
            validate_widget(widget, user, engine())
        return get_bi_store().save('dashboards', body, body.revision, user.user_id)
    return operation(run)


@router.post('/pages/{page}/query', operation_id='queryAnalyticsPage')
def page_query(page: Page, body: PageQuery, user=Depends(principal)):
    return operation(lambda: query_page(load_page(page, user), body, user, engine()))
