"""Owner-scoped preferences and assets over the shared system dashboard."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field

from app.api.analytics_pages import PageQuery, load_page
from app.api.bi_routes import engine, no_store, operation, principal
from app.bi.models import BiModel, DashboardDefinition, DateRange, LayoutOverrideInput, QueryFilter, UserLayoutOverride, WidgetDefinition
from app.bi.security import require_permission, widget_visible
from app.bi.store import BiConflict, BiNotFound, get_bi_store
from app.bi.widgets import query_page, validate_widget
from app.core.user_accounts import PersonalDashboardPreferences, check_request_origin, get_user_account_store

router = APIRouter(prefix='/v1/analytics', dependencies=[Depends(no_store)])
DASHBOARD = 'system-dashboard'
CUSTOMIZABLE_MODES = {'CUSTOMIZABLE', 'FREE'}


class PersonalAppearance(BiModel):
    background: str = Field(default='default', pattern=r'^(default|navy|graphite|forest|gradient|image|#[0-9a-fA-F]{6})$')
    background_asset_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    background_url: str | None = Field(default=None, max_length=500)
    background_opacity: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)
    widget_opacity: float = Field(default=1, ge=0.2, le=1, allow_inf_nan=False)
    grid_spacing: int = Field(default=12, ge=0, le=32)
    density: str = Field(default='comfortable', pattern=r'^(compact|comfortable)$')
    card_radius: int = Field(default=12, ge=0, le=40)
    shadow: bool = True
    accent: str = Field(default='#59b8dd', pattern=r'^#[0-9a-fA-F]{6}$')


class PersonalUpdate(BiModel):
    expected_revision: int = Field(ge=0)
    appearance: PersonalAppearance | None = None
    overrides: list[LayoutOverrideInput] | None = Field(default=None, max_length=100)
    hidden_widget_ids: list[str] | None = Field(default=None, max_length=100)


class SavedView(BiModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=240)
    created_at: str | None = Field(default=None, max_length=100)
    filters: dict
    target: str = Field(default='dashboard', max_length=40)


class PersonalMigration(BiModel):
    legacy_preferences: PersonalDashboardPreferences | None = None
    saved_views: list[SavedView] = Field(default_factory=list, max_length=50)


def owner(user, permission):
    require_permission(user, permission)
    if user.user_id is None:
        raise HTTPException(403, 'Sign in to use a personal dashboard')
    return user.user_id


def preferences(connection, user_id):
    row = connection.execute('SELECT * FROM personal_dashboard_preferences WHERE user_id=?', (user_id,)).fetchone()
    return (json.loads(row['definition_json']), row['revision'], bool(row['migrated'])) if row else ({'appearance': PersonalAppearance().model_dump(), 'hidden_widget_ids': [], 'chart_styles': {}}, 0, False)


def put_preferences(connection, user_id, values, revision, migrated):
    connection.execute('INSERT INTO personal_dashboard_preferences VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,definition_json=excluded.definition_json,migrated=excluded.migrated,updated_at=excluded.updated_at', (user_id, revision, json.dumps(values, ensure_ascii=False), int(migrated), get_bi_store()._now()))


def require_parent_mode(definition, *, layouts=False, widgets=False):
    if (layouts and definition.layout_mode == 'LOCKED') or (widgets and definition.layout_mode not in CUSTOMIZABLE_MODES):
        raise PermissionError('The dashboard mode does not permit this personal change')


def transaction_parent(connection):
    row = connection.execute('SELECT definition_json FROM dashboards WHERE id=?', (DASHBOARD,)).fetchone()
    if row is None:
        raise BiNotFound('Dashboard was not found')
    return DashboardDefinition.model_validate_json(row['definition_json'])


def snapshot(user):
    user_id = owner(user, 'personal_dashboard.view')
    store = get_bi_store()
    definition = load_page('dashboard', user, personal=True)
    system = definition.widgets
    customizable = definition.layout_mode in CUSTOMIZABLE_MODES
    personal = [widget for widget in store.personal_widgets(DASHBOARD, user_id) if widget_visible(widget, user)] if customizable else []
    overrides = {item.widget_id: item.layout for item in store.overrides_snapshot(DASHBOARD, user_id)['overrides']} if definition.layout_mode != 'LOCKED' else {}
    with store._connection() as connection:
        values, revision, migrated = preferences(connection, user_id)
    hidden = set(values.get('hidden_widget_ids', [])) if customizable else set()
    all_widgets = [*system, *personal]
    for widget in all_widgets:
        if widget.id in overrides and not widget.is_locked:
            original, override = widget.layout, overrides[widget.id]
            width = override.w if widget.resizable else original.w
            x = min(override.x, 12 - width) if widget.movable else original.x
            widget.layout = original.model_validate({'x': x, 'y': override.y if widget.movable else original.y, 'w': min(width, 12 - x), 'h': override.h if widget.resizable else original.h})
        block = widget.visualization.options.get('legacy_block')
        style = values.get('chart_styles', {}).get('timeline' if block == 'timeline' else 'category' if block == 'departments' else 'purpose' if block == 'purposes' else '')
        if style and customizable:
            widget.visualization.options['view'] = style
    definition.widgets = [widget for widget in all_widgets if widget.id not in hidden or widget.is_locked or widget.mandatory or not widget.removable]
    appearance = PersonalAppearance.model_validate(values.get('appearance', {})).model_dump()
    if appearance['background_asset_id']:
        appearance['background_url'] = f'/api/v1/analytics/assets/{appearance["background_asset_id"]}'
    definition.name = 'Мій дашборд'
    return {'dashboard': definition, 'appearance': appearance, 'revision': revision, 'migrated': migrated, 'hidden_widget_ids': list(hidden), 'available_widgets': [widget for widget in system if widget.id in hidden]}


@router.get('/personal-dashboard', operation_id='getPersonalAnalyticsDashboard')
def get_personal(user=Depends(principal)):
    return operation(lambda: snapshot(user))


@router.put('/personal-dashboard', operation_id='savePersonalAnalyticsDashboard')
def update_personal(body: PersonalUpdate, request: Request, user=Depends(principal)):
    changes_layout = body.overrides is not None or body.hidden_widget_ids is not None
    user_id = owner(user, 'personal_dashboard.edit' if changes_layout or body.appearance is None else 'personal_dashboard.appearance.edit')
    check_request_origin(request)
    if body.appearance is not None:
        require_permission(user, 'personal_dashboard.appearance.edit')
    def run():
        current = snapshot(user)
        require_parent_mode(current['dashboard'], layouts=body.overrides is not None, widgets=body.hidden_widget_ids is not None)
        widgets = {widget.id: widget for widget in [*current['dashboard'].widgets, *current['available_widgets']]}
        store = get_bi_store()
        with store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            require_parent_mode(transaction_parent(connection), layouts=body.overrides is not None, widgets=body.hidden_widget_ids is not None)
            values, revision, migrated = preferences(connection, user_id)
            if revision != body.expected_revision:
                raise BiConflict('Personal dashboard changed elsewhere; reload before saving')
            if body.appearance is not None:
                appearance = body.appearance.model_dump()
                appearance['background_url'] = None
                if appearance['background_asset_id'] and not connection.execute('SELECT 1 FROM dashboard_assets WHERE id=? AND user_id=?', (appearance['background_asset_id'], user_id)).fetchone():
                    raise BiNotFound('Background asset was not found')
                values['appearance'] = appearance
            if body.hidden_widget_ids is not None:
                system = {widget.id: widget for widget in load_page('dashboard', user, personal=True).widgets}
                if len(set(body.hidden_widget_ids)) != len(body.hidden_widget_ids) or any(key not in system or system[key].is_locked or system[key].mandatory or not system[key].removable for key in body.hidden_widget_ids):
                    raise PermissionError('A protected or unavailable widget cannot be hidden')
                values['hidden_widget_ids'] = body.hidden_widget_ids
            if body.overrides is not None:
                if len({item.widget_id for item in body.overrides}) != len(body.overrides):
                    raise ValueError('Widget layout IDs must be unique')
                for item in body.overrides:
                    widget = widgets.get(item.widget_id)
                    if widget is None or widget.is_locked or (not widget.movable and (widget.layout.x, widget.layout.y) != (item.layout.x, item.layout.y)) or (not widget.resizable and (widget.layout.w, widget.layout.h) != (item.layout.w, item.layout.h)):
                        raise PermissionError('This widget layout is protected')
                # Keep overrides for dormant personal or currently unavailable
                # widgets; switching the parent mode must not erase their layout.
                for widget_id in widgets:
                    connection.execute('DELETE FROM dashboard_user_overrides WHERE dashboard_id=? AND user_id=? AND widget_id=?', (DASHBOARD, user_id, widget_id))
                for item in body.overrides:
                    store._save_record(connection, 'overrides', UserLayoutOverride(dashboard_id=DASHBOARD, user_id=user_id, widget_id=item.widget_id, layout=item.layout), 0)
                connection.execute('INSERT INTO dashboard_override_revisions VALUES (?,?,1) ON CONFLICT(dashboard_id,user_id) DO UPDATE SET revision=revision+1', (DASHBOARD, user_id))
            put_preferences(connection, user_id, values, revision + 1, migrated)
        return snapshot(user)
    return operation(run)


@router.post('/personal-dashboard/query', operation_id='queryPersonalAnalyticsDashboard')
def personal_query(body: PageQuery, user=Depends(principal)):
    return operation(lambda: query_page(snapshot(user)['dashboard'], body, user, engine()))


@router.post('/personal-dashboard/widgets', response_model=WidgetDefinition, operation_id='savePersonalAnalyticsWidget')
def save_widget(body: WidgetDefinition, request: Request, user=Depends(principal)):
    user_id = owner(user, 'personal_dashboard.view')
    check_request_origin(request)
    def run():
        require_parent_mode(load_page('dashboard', user, personal=True), widgets=True)
        existing = next((widget for widget in get_bi_store().personal_widgets(DASHBOARD, user_id) if widget.id == body.id), None)
        require_permission(user, 'personal_dashboard.widget.edit' if existing else 'personal_dashboard.widget.create')
        if body.owner_id not in {None, user_id} or body.data_scope != 'USER_SCOPE' or body.query.data_scope != 'USER_SCOPE' or body.query.fixed_scope is not None or body.fixed_filters:
            raise PermissionError('Personal widgets must use the assigned user scope')
        if existing is None and len(snapshot(user)['dashboard'].widgets) >= 100:
            raise ValueError('A dashboard supports at most 100 widgets')
        body.widget_kind = 'USER_WIDGET'
        body.owner_id = user_id
        body.is_locked = False
        body.mandatory = False
        validate_widget(body, user, engine())
        return get_bi_store().save_personal_widget(DASHBOARD, user_id, body)
    return operation(run)


@router.delete('/personal-dashboard/widgets/{widget_id}', operation_id='deletePersonalAnalyticsWidget')
def remove_widget(widget_id: str, expected_revision: int, request: Request, user=Depends(principal)):
    user_id = owner(user, 'personal_dashboard.widget.delete')
    check_request_origin(request)
    def run():
        store = get_bi_store()
        with store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            require_parent_mode(transaction_parent(connection), widgets=True)
            record = connection.execute('SELECT revision FROM dashboard_personal_widgets WHERE id=? AND dashboard_id=? AND user_id=?', (widget_id, DASHBOARD, user_id)).fetchone()
            if record is None:
                raise BiNotFound('Personal widget was not found')
            if record['revision'] != expected_revision:
                raise BiConflict('Personal widget changed elsewhere; reload before deleting')
            connection.execute('DELETE FROM dashboard_user_overrides WHERE widget_id=? AND user_id=?', (widget_id, user_id))
            connection.execute('DELETE FROM dashboard_personal_widgets WHERE id=? AND user_id=?', (widget_id, user_id))
            connection.execute('UPDATE bi_state SET configuration_revision=configuration_revision+1 WHERE id=1')
        return {'deleted': True}
    return operation(run)


def import_view(view, user, service, y):
    from app.api.models import FilterRequest
    filters = FilterRequest.model_validate(view.filters)
    date_range = DateRange.model_validate({'from': filters.date_from, 'to': filters.date_to})
    mappings = {'direction': 'direction', 'unit': 'zone', 'category': 'category', 'asset': 'device', 'group': 'team', 'bbak': 'department', 'rota': 'group', 'battalion': 'unit_id', 'purpose': 'purpose', 'class_name': 'target_type', 'result': 'result'}
    query_filters = [QueryFilter(field=field, operator='in', value=getattr(filters, key)) for key, field in mappings.items() if getattr(filters, key)]
    if filters.time_from is not None or filters.time_to is not None:
        query_filters = PageQuery(date_range=date_range, filters=query_filters, time_from=filters.time_from.isoformat() if filters.time_from else None, time_to=filters.time_to.isoformat() if filters.time_to else None).bounded_filters()
    builtin = 'map' if view.target == 'map' else 'records' if view.target == 'table' else 'summary'
    widget = WidgetDefinition(id='legacy-view-' + hashlib.sha256((user.user_id + ':' + view.id).encode()).hexdigest()[:32], title=view.name, builtin=builtin, widget_kind='USER_WIDGET', owner_id=user.user_id, date_mode='USE_FIXED_DATE', inherit_global_filters=False, query={'date_range': date_range, 'filters': query_filters, 'metrics': [{'key': 'flights'}] if builtin == 'summary' else []}, visualization={'type': 'kpi' if builtin == 'summary' else 'table'}, layout={'x': 0, 'y': y, 'w': 12, 'h': 5})
    validate_widget(widget, user, service)
    return widget


@router.post('/personal-dashboard/migrate', operation_id='migratePersonalAnalyticsDashboard')
def migrate(body: PersonalMigration, request: Request, user=Depends(principal)):
    user_id = owner(user, 'personal_dashboard.view')
    check_request_origin(request)
    def run():
        store = get_bi_store()
        with store._connection() as connection:
            values, revision, done = preferences(connection, user_id)
        if done:
            return snapshot(user)
        prefs = body.legacy_preferences
        if prefs is None:
            try:
                prefs = get_user_account_store().dashboard(user_id).preferences
            except (ValueError, HTTPException):
                prefs = None
        definition = load_page('dashboard', user, personal=True)
        variants = {'timeline_line': ('timeline', 'line', ['flights', 'effective']), 'timeline_bar': ('timeline', 'bar', ['flights', 'effective']), 'timeline_area': ('timeline', 'area', ['flights', 'effective']), 'category_chart': ('category', prefs.chart_styles.category if prefs else 'bars', ['flights']), 'purpose_chart': ('purpose', prefs.chart_styles.purpose if prefs else 'bars', ['flights'])}
        hidden = [widget.id for widget in definition.widgets if prefs and not widget.is_locked and not widget.mandatory and widget.removable and ((widget.builtin == 'summary' and prefs.metric_keys is not None and any(metric.key not in prefs.metric_keys for metric in widget.query.metrics)) or (widget.visualization.options.get('legacy_block') and prefs.blocks is not None and widget.visualization.options['legacy_block'] not in prefs.blocks))]
        creates_widgets = bool(body.saved_views or (prefs and any(block in variants for block in prefs.blocks or [])))
        changes_widgets = creates_widgets or bool(hidden)
        require_parent_mode(definition, widgets=changes_widgets)
        bottom = max([widget.layout.y + widget.layout.h for widget in definition.widgets] or [0])
        imported = [import_view(view, user, engine(), bottom + index * 5) for index, view in enumerate(body.saved_views)]
        if prefs:
            values['appearance'].update(background=prefs.background, density=prefs.density or 'comfortable')
            # Retain imported chart preferences while restrictive modes leave
            # them dormant, just like already saved personal layouts.
            values['chart_styles'] = prefs.chart_styles.model_dump()
            if definition.layout_mode in CUSTOMIZABLE_MODES:
                values['hidden_widget_ids'] = hidden
            for block in prefs.blocks or []:
                if block not in variants:
                    continue
                builtin, view, keys = variants[block]
                widget = WidgetDefinition(id='legacy-block-' + hashlib.sha256((user_id + ':' + block).encode()).hexdigest()[:32], title={'timeline_line': 'Динаміка: лінії', 'timeline_bar': 'Динаміка: стовпчики', 'timeline_area': 'Динаміка: області', 'category_chart': 'Вильоти за кафедрами', 'purpose_chart': 'Вильоти за метою'}[block], builtin=builtin, widget_kind='USER_WIDGET', owner_id=user_id, visualization={'type': 'donut' if view == 'donut' else view if view in {'line', 'bar', 'area'} else 'bar', 'options': {'view': view, 'legacy_block': block}}, query={'metrics': [{'key': key} for key in keys]}, layout={'x': 0, 'y': bottom + len(imported) * 5, 'w': 12, 'h': 5})
                validate_widget(widget, user, engine())
                imported.append(widget)
        if imported:
            require_permission(user, 'personal_dashboard.widget.create')
        values['migration_archive'] = body.model_dump(mode='json')
        existing_ids = {widget.id for widget in store.personal_widgets(DASHBOARD, user_id)}
        if len(definition.widgets) + len(existing_ids | {widget.id for widget in imported}) > 100:
            raise ValueError('Migration would exceed 100 widgets; reduce saved views first')
        with store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            require_parent_mode(transaction_parent(connection), widgets=changes_widgets)
            _, actual_revision, done = preferences(connection, user_id)
            if done:
                return snapshot(user)
            if actual_revision != revision:
                raise BiConflict('Personal settings changed during migration; retry')
            now = store._now()
            for widget in imported:
                widget.dashboard_id = DASHBOARD
                widget.revision = 1
                widget.created_at = widget.updated_at = now
                if connection.execute('SELECT 1 FROM dashboard_widgets WHERE id=?', (widget.id,)).fetchone():
                    raise BiConflict('Imported widget ID is already used')
                connection.execute('INSERT OR IGNORE INTO dashboard_personal_widgets VALUES (?,?,?,?,?,?,?)', (widget.id, user_id, DASHBOARD, 1, widget.model_dump_json(), now, now))
            put_preferences(connection, user_id, values, revision + 1, True)
        return snapshot(user)
    return operation(run)


@router.post('/personal-dashboard/assets', operation_id='uploadPersonalDashboardAsset')
async def upload_asset(request: Request, user=Depends(principal)):
    user_id = owner(user, 'personal_dashboard.appearance.edit')
    check_request_origin(request)
    content_type = request.headers.get('content-type', '').split(';')[0]
    if content_type not in {'image/png', 'image/jpeg', 'image/webp'}:
        raise HTTPException(422, 'Use a PNG, JPEG or WebP image')
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 5 * 1024 * 1024:
            raise HTTPException(413, 'Background images are limited to 5 MiB')
        chunks.append(chunk)
    data = b''.join(chunks)
    valid = data.startswith(b'\x89PNG\r\n\x1a\n') if content_type == 'image/png' else data.startswith(b'\xff\xd8\xff') if content_type == 'image/jpeg' else data.startswith(b'RIFF') and data[8:12] == b'WEBP'
    if not valid:
        raise HTTPException(422, 'Image content does not match its type')
    def run():
        store = get_bi_store()
        identifier = uuid.uuid4().hex
        directory = store.path.parent / 'dashboard-assets'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / identifier
        path.write_bytes(data)
        try:
            with store._connection() as connection:
                connection.execute('INSERT INTO dashboard_assets VALUES (?,?,?,?,?)', (identifier, user_id, content_type, size, store._now()))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {'asset_id': identifier, 'url': f'/api/v1/analytics/assets/{identifier}'}
    return operation(run)


@router.get('/assets/{asset_id}', operation_id='getPersonalDashboardAsset')
def get_asset(asset_id: str, user=Depends(principal)):
    user_id = owner(user, 'personal_dashboard.view')
    def run():
        store = get_bi_store()
        with store._connection() as connection:
            row = connection.execute('SELECT * FROM dashboard_assets WHERE id=? AND user_id=?', (asset_id, user_id)).fetchone()
        if row is None:
            raise BiNotFound('Background asset was not found')
        path = store.path.parent / 'dashboard-assets' / row['id']
        if not path.is_file():
            raise BiNotFound('Background asset was not found')
        return FileResponse(path, media_type=row['content_type'], headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store'})
    return operation(run)
