"""Versioned initial pages; preserve edits and deletions on every upgrade."""
from __future__ import annotations

from app.bi.engine import seed_default_metrics
from app.bi.models import DashboardDefinition, SortField, WidgetDefinition

BOOTSTRAP_VERSION = 2
_V1_METRICS = frozenset({'flights', 'effective', 'efficiency', 'events', 'ammunition', 'lost_devices', 'weighted_result'})
_LABELS = {
    'flights': 'Вильоти', 'effective': 'Результативні вильоти', 'detected': 'Виявлено',
    'affected': 'Уражено ОС', 'destroyed': 'Знищено ОС', 'efficiency': 'Результативність',
    'avg_flights_per_position': 'Середні вильоти на позицію за добу',
    'weighted_efficiency': 'Зважений KPI',
}


def dashboard_configuration():
    """Read published original page options without creating another database."""
    from app.core.app_configuration import default_application_configuration, get_configuration_store
    configuration = get_configuration_store()
    return (configuration.snapshot().config if configuration.path.is_file() else default_application_configuration()).dashboard


def _widget(identifier, title, keys, *, builtin=None, visualization='table', dimensions=(),
            layout=None, section='overview', options=None, cross_filter=False):
    permissions = ['kpi.view'] if 'weighted_efficiency' in keys else []
    return WidgetDefinition(
        id=identifier, title=title, widget_type='kpi' if visualization in {'kpi', 'total_with_highlight'} else 'table' if visualization == 'table' else 'chart',
        layout=layout or {'x': 0, 'y': 0, 'w': 12, 'h': 6}, builtin=builtin, section=section,
        query={'metrics': [{'key': key} for key in keys], 'dimensions': [{'field': field, **({'granularity': 'day'} if field == 'date' else {})} for field in dimensions]},
        visualization={'type': visualization, 'options': options or {}},
        permissions={'required_permissions': permissions},
        interaction={'click_action': 'CROSS_FILTER' if cross_filter else 'DRILL_THROUGH', 'target_section': section},
    )


_RESULT_KEYS = ['affected', 'destroyed', 'target_results', 'target_detected']
# The overview widgets take rows 7–30, directly below the department overview.
_OVERVIEW_TOP, _OVERVIEW_HEIGHT = 7, 24


def _overview_widgets():
    # Statistics overview: flights and results by corps (direction) and by responsibility zone.
    return [
        _widget('stats-overview-flights-direction', 'Вильоти за напрямками (АК)', ['flights'], dimensions=['direction'], visualization='bar', layout={'x': 0, 'y': 7, 'w': 4, 'h': 7}, cross_filter=True),
        _widget('stats-overview-flights-zone', 'Вильоти за зонами відповідальності', ['zone_flights'], dimensions=['zone'], visualization='horizontal_bar', layout={'x': 4, 'y': 7, 'w': 8, 'h': 9}, cross_filter=True),
        _widget('stats-overview-results-direction', 'Ураження ОС і цілей за АК', _RESULT_KEYS, dimensions=['direction'], layout={'x': 0, 'y': 16, 'w': 12, 'h': 5}, cross_filter=True),
        _widget('stats-overview-results-zone', 'Ураження ОС і цілей за зонами відповідальності', _RESULT_KEYS, dimensions=['zone'], layout={'x': 0, 'y': 21, 'w': 12, 'h': 10}, cross_filter=True),
    ]


_POSITION_KEYS = ['flights', 'day_flights', 'night_flights', 'effective', 'efficiency']


def _position_widgets(top):
    # Statistics "Positions": who flew how much by day and night, per position.
    table = _widget('stats-positions-table', 'Позиції: вильоти вдень і вночі', _POSITION_KEYS, dimensions=['category', 'bbak_title', 'position', 'team'], visualization='position_board', section='positions', layout={'x': 0, 'y': top + 6, 'w': 12, 'h': 16}, cross_filter=True)
    table.query.sort = [SortField(field='flights', direction='desc')]
    return [
        _widget('stats-positions-category', 'Вильоти вдень і вночі за кафедрами', ['day_flights', 'night_flights'], dimensions=['category'], visualization='stacked_bar', section='positions', layout={'x': 0, 'y': top, 'w': 12, 'h': 6}, cross_filter=True),
        table,
    ]


def _pages(configuration):
    main = []
    for index, key in enumerate(configuration.metric_keys):
        section = 'effectiveness' if key in {'effective', 'efficiency', 'weighted_efficiency'} else 'targets' if key in {'detected', 'affected', 'destroyed'} else 'overview'
        main.append(_widget(f'main-kpi-{key}', _LABELS[key], [key], builtin='summary', visualization='kpi', section=section,
                            layout={'x': index % 6 * 2, 'y': index // 6 * 2, 'w': 2, 'h': 2}))
    row = (len(main) + 5) // 6 * 2
    blocks = {
        'map': ('map', 'Мапа подій', 'map', ['events'], 'overview', 'table', 6, 6),
        'timeline': ('timeline', 'Динаміка вильотів', 'timeline', ['flights', 'effective', 'efficiency'], 'overview', 'timeline', 6, 6),
        'departments': ('average', 'Середні вильоти за кафедрами', 'average', ['avg_flights_per_position', 'flights'], 'overview', 'table', 12, 6),
        'purposes': ('purpose', 'Результативність за метою', 'purpose', ['flights', 'effective', 'efficiency'], 'effectiveness', 'table', 12, 7),
        'events': ('records', 'Записи подій', 'records', ['events'], 'overview', 'table', 12, 8),
        'lost_devices': ('losses', 'Втрати засобів', 'losses', ['lost_devices'], 'losses', 'table', 12, 8),
    }
    column = 0
    row_height = 0
    for block in configuration.blocks:
        identifier, title, builtin, keys, section, visualization, width, height = blocks[block]
        if column + width > 12:
            row += row_height
            column = row_height = 0
        main.append(_widget(f'main-{identifier}', title, keys, builtin=builtin, visualization=visualization, section=section,
                            layout={'x': column, 'y': row, 'w': width, 'h': height}, options={'legacy_block': block}))
        if identifier == 'timeline':
            main[-1].interaction.target_section = 'positions'  # A clicked day opens its positions.
        column += width
        row_height = max(row_height, height)
        if column == 12:
            row += row_height
            column = row_height = 0

    statistics = [
        _widget('stats-overview-category', 'Огляд за кафедрами', ['flights', 'effective', 'efficiency'], builtin='category', dimensions=['category'], layout={'x': 0, 'y': 0, 'w': 12, 'h': 7}, cross_filter=True),
        _widget('stats-effectiveness-purpose', 'Результативність за метою', ['flights', 'effective', 'efficiency'], builtin='purpose', dimensions=['purpose'], section='effectiveness', layout={'x': 0, 'y': 7, 'w': 12, 'h': 7}, cross_filter=True),
        _widget('target-total', 'Уражено та знищено цілей', ['target_results', 'target_destroyed'], visualization='total_with_highlight', section='targets', layout={'x': 0, 'y': 14, 'w': 4, 'h': 3}, options={'total_metric': 'target_results', 'highlight_metric': 'target_destroyed'}),
        _widget('target-results', 'Результати за типом цілі', ['target_affected', 'target_destroyed', 'target_results', 'flights_per_affected', 'flights_per_destroyed', 'flights_per_result'], dimensions=['target_type'], section='targets', layout={'x': 4, 'y': 14, 'w': 8, 'h': 6}, cross_filter=True),
        _widget('stats-ak', 'Результати за АК', ['target_affected', 'target_destroyed', 'target_results'], dimensions=['direction'], visualization='grouped_bar', section='targets', layout={'x': 0, 'y': 20, 'w': 6, 'h': 6}, cross_filter=True),
        _widget('stats-zone', 'Результати за зоною відповідальності', ['target_affected', 'target_destroyed', 'target_results'], dimensions=['zone'], visualization='grouped_bar', section='targets', layout={'x': 6, 'y': 20, 'w': 6, 'h': 6}, cross_filter=True),
        _widget('stats-category-shares', 'Частки результатів за кафедрами', ['target_affected', 'target_destroyed'], dimensions=['category'], visualization='percent_stacked_bar', section='targets', layout={'x': 0, 'y': 26, 'w': 12, 'h': 6}, cross_filter=True),
        _widget('stats-losses', 'Втрати засобів', ['lost_devices'], builtin='losses', section='losses', layout={'x': 0, 'y': 32, 'w': 12, 'h': 8}),
        _widget('stats-resources', 'Витрати боєприпасів', ['ammunition'], dimensions=['category'], section='resources', layout={'x': 0, 'y': 40, 'w': 12, 'h': 6}, cross_filter=True),
    ]
    for widget in statistics:
        if widget.layout.y >= _OVERVIEW_TOP:
            widget.layout.y += _OVERVIEW_HEIGHT
    statistics[1:1] = _overview_widgets()
    statistics += _position_widgets(max(widget.layout.y + widget.layout.h for widget in statistics))
    return [
        DashboardDefinition(id='system-dashboard', name='Центральний Dashboard', slug='central-dashboard', layout_mode='CUSTOMIZABLE', widgets=main),
        DashboardDefinition(id='system-statistics', name='Statistics', slug='statistics', type='STATISTICS', layout_mode='LAYOUT_EDITABLE', widgets=statistics),
    ]


def _v1_pages():
    """Exact normalized stock definitions distinguish edits from defaults."""
    references = [{'key': 'flights'}, {'key': 'effective'}]
    overview = WidgetDefinition(id='system-widget-overview', title='Вильоти та результативність', widget_type='kpi', layout={'x': 0, 'y': 0, 'w': 12, 'h': 3}, query={'metrics': [*references, {'key': 'efficiency'}]}, visualization={'type': 'kpi'})
    timeline = WidgetDefinition(id='system-widget-trend', title='Динаміка вильотів', layout={'x': 0, 'y': 3, 'w': 12, 'h': 6}, query={'metrics': references, 'dimensions': [{'field': 'date', 'granularity': 'day'}], 'sort': [{'field': 'date', 'direction': 'asc'}]}, visualization={'type': 'line'})
    statistics = WidgetDefinition(id='system-widget-statistics', title='Статистика за підрозділами', widget_type='table', layout={'x': 0, 'y': 0, 'w': 12, 'h': 7}, query={'metrics': [*references, {'key': 'efficiency'}], 'dimensions': [{'field': 'department'}, {'field': 'group'}], 'sort': [{'field': 'flights', 'direction': 'desc'}]}, visualization={'type': 'table'})
    return [DashboardDefinition(id='system-dashboard', name='Центральний Dashboard', slug='central-dashboard', layout_mode='LAYOUT_EDITABLE', widgets=[overview, timeline]), DashboardDefinition(id='system-statistics', name='Statistics', slug='statistics', type='STATISTICS', widgets=[statistics])]


def _stock(current, original):
    if current.revision > 1:
        return False
    ignored = {'revision', 'created_at', 'updated_at', 'widgets', 'dashboard_id'}
    return current.model_dump(exclude=ignored) == original.model_dump(exclude=ignored)


def _upgrade_page(store, connection, page, old_page):
    row = connection.execute('SELECT * FROM dashboards WHERE id=?', (page.id,)).fetchone()
    if row is None:
        return  # A previously deleted page is an administrator decision.
    current = store._decode('dashboards', row, connection)
    widgets = {widget.id: widget for widget in current.widgets}
    deleted = {widget.id for widget in old_page.widgets if widget.id not in widgets}
    for original in old_page.widgets:
        existing = widgets.get(original.id)
        if existing is not None and _stock(existing, original):
            connection.execute('DELETE FROM dashboard_user_overrides WHERE widget_id=?', (existing.id,))
            connection.execute('DELETE FROM dashboard_widgets WHERE id=?', (existing.id,))
            connection.execute('UPDATE bi_state SET configuration_revision=configuration_revision+1 WHERE id=1')
            del widgets[existing.id]
    if _stock(current, old_page) and current.layout_mode != page.layout_mode:
        store._save_record(connection, 'dashboards', current.model_copy(update={'layout_mode': page.layout_mode}), current.revision)
    new_widgets = page.widgets
    if page.id == 'system-dashboard':
        new_widgets = [widget for widget in new_widgets if not (widget.id.startswith('main-kpi-') and 'system-widget-overview' in deleted) and not (widget.id == 'main-timeline' and 'system-widget-trend' in deleted)]
    elif 'system-widget-statistics' in deleted:
        new_widgets = []
    # Existing edited/custom layouts remain untouched. New content starts below it.
    offset = max((widget.layout.y + widget.layout.h for widget in widgets.values()), default=0)
    for widget in new_widgets:
        if connection.execute('SELECT 1 FROM dashboard_widgets WHERE id=?', (widget.id,)).fetchone() or connection.execute('SELECT 1 FROM dashboard_personal_widgets WHERE id=?', (widget.id,)).fetchone():
            continue
        shifted = widget.model_copy(deep=True, update={'dashboard_id': page.id})
        shifted.layout.y += offset
        if shifted.layout.y > 10000:
            continue
        store._save_record(connection, 'widgets', shifted, 0)


def _bootstrap_pages(store):
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=?', (BOOTSTRAP_VERSION,)).fetchone():
            return
        upgrading = bool(connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=1').fetchone())

        class Definitions:
            metrics = ()
            def seed_definitions(self, metrics=(), **kwargs):
                self.metrics = tuple(metrics)

        definitions = Definitions()
        seed_default_metrics(definitions)
        for metric in definitions.metrics:
            if upgrading and metric.key in _V1_METRICS:
                continue  # Never recreate deleted legacy metrics while adding new keys.
            if not connection.execute('SELECT 1 FROM metric_definitions WHERE metric_key=? OR id=?', (metric.key, metric.id)).fetchone() and not connection.execute('SELECT 1 FROM kpi_definitions WHERE metric_key=?', (metric.key,)).fetchone():
                store._save_record(connection, 'metrics', metric, 0)
        for page, old_page in zip(_pages(dashboard_configuration()), _v1_pages()):
            if upgrading:
                _upgrade_page(store, connection, page, old_page)
            elif not connection.execute('SELECT 1 FROM dashboards WHERE id=? OR slug=?', (page.id, page.slug)).fetchone():
                store._save_record(connection, 'dashboards', page, 0)
                for widget in page.widgets:
                    store._save_record(connection, 'widgets', widget.model_copy(update={'dashboard_id': page.id}), 0)
        if not upgrading:
            connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (1, store._now()))
        connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (BOOTSTRAP_VERSION, store._now()))


def bootstrap_success_semantics(store):
    """Upgrade only the old stock calculation, once; retain administrator edits."""
    from app.bi.engine import default_metric_definitions
    from app.bi.models import QueryFilter
    from app.bi.reports import _calculation_fields
    template = next(metric for metric in default_metric_definitions() if metric.key == 'effective')
    legacy = template.model_copy(update={'filters': [QueryFilter(field='is_effective', operator='eq', value=True)]})
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=3').fetchone():
            return
        row = connection.execute("SELECT * FROM metric_definitions WHERE metric_key='effective'").fetchone()
        if row is not None:
            current = store._decode('metrics', row, connection)
            if current.id == template.id and _calculation_fields(current.model_dump(mode='json')) == _calculation_fields(legacy.model_dump(mode='json')):
                store._save_record(connection, 'metrics', current.model_copy(update={'filters': template.filters}), current.revision)
        connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (3, store._now()))


def bootstrap_metric_texts(store):
    """Replace the old built-in metric titles/descriptions once; keep edited ones."""
    from app.bi.engine import default_metric_definitions
    from app.bi.metric_texts import updated_texts
    builtin_ids = {metric.key: metric.id for metric in default_metric_definitions()}
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=5').fetchone():
            return
        for row in connection.execute('SELECT * FROM metric_definitions').fetchall():
            current = store._decode('metrics', row, connection)
            # Only built-in records; an administrator's own metric may reuse a key.
            if current.id != builtin_ids.get(current.key):
                continue
            changes = updated_texts(current.key, current.title, current.description)
            if changes:
                store._save_record(connection, 'metrics', current.model_copy(update=changes), current.revision)
        # Version 5: version 4 skipped built-ins whose ids differ from their keys.
        connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (5, store._now()))


def bootstrap_overview_widgets(store):
    """Add the zone/corps overview metrics and widgets once; never re-add deleted ones."""
    from app.bi.engine import default_metric_definitions
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=6').fetchone():
            return
        for metric in default_metric_definitions():
            if metric.key in {'zone_flights', 'target_detected'} and not connection.execute('SELECT 1 FROM metric_definitions WHERE metric_key=? OR id=?', (metric.key, metric.id)).fetchone():
                store._save_record(connection, 'metrics', metric, 0)
        # Only next to the stock overview; a page an administrator rebuilt is left alone.
        if connection.execute("SELECT 1 FROM dashboard_widgets WHERE id='stats-overview-category' AND dashboard_id='system-statistics'").fetchone():
            missing = [widget for widget in _overview_widgets() if not connection.execute('SELECT 1 FROM dashboard_widgets WHERE id=?', (widget.id,)).fetchone()]
            if missing:
                # Make room: everything at or below the insertion row moves down.
                for row in connection.execute("SELECT * FROM dashboard_widgets WHERE dashboard_id='system-statistics'").fetchall():
                    current = store._decode('widgets', row, connection)
                    if current.layout.y >= _OVERVIEW_TOP:
                        moved = current.model_copy(deep=True)
                        moved.layout.y += _OVERVIEW_HEIGHT
                        store._save_record(connection, 'widgets', moved, current.revision)
            for widget in missing:
                store._save_record(connection, 'widgets', widget.model_copy(update={'dashboard_id': 'system-statistics'}), 0)
        connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (6, store._now()))


def bootstrap_ammunition_spending(store):
    """Count ammunition only on strike and mining flights; keep edited calculations."""
    from app.bi.engine import default_metric_definitions
    from app.bi.metric_texts import updated_texts
    from app.bi.reports import _calculation_fields
    template = next(metric for metric in default_metric_definitions() if metric.key == 'ammunition')
    legacy = template.model_copy(update={'filters': []})
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=7').fetchone():
            return
        row = connection.execute('SELECT * FROM metric_definitions WHERE id=?', (template.id,)).fetchone()
        if row is not None:
            current = store._decode('metrics', row, connection)
            changes = updated_texts(current.key, current.title, current.description)
            if _calculation_fields(current.model_dump(mode='json')) == _calculation_fields(legacy.model_dump(mode='json')):
                changes['filters'] = template.filters
            if changes:
                store._save_record(connection, 'metrics', current.model_copy(update=changes), current.revision)
        connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (7, store._now()))


def bootstrap_positions(store):
    """Add the "Positions" statistics section once; the dashboard timeline opens it."""
    from app.bi.engine import default_metric_definitions
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=8').fetchone():
            return
        for metric in default_metric_definitions():
            if metric.key in {'day_flights', 'night_flights'} and not connection.execute('SELECT 1 FROM metric_definitions WHERE metric_key=? OR id=?', (metric.key, metric.id)).fetchone():
                store._save_record(connection, 'metrics', metric, 0)
        # Only next to the stock statistics; a page an administrator rebuilt is left alone.
        if connection.execute("SELECT 1 FROM dashboard_widgets WHERE id='stats-overview-category' AND dashboard_id='system-statistics'").fetchone():
            rows = connection.execute("SELECT * FROM dashboard_widgets WHERE dashboard_id='system-statistics'").fetchall()
            bottom = max((store._decode('widgets', row, connection).layout for row in rows), key=lambda layout: layout.y + layout.h, default=None)
            for widget in _position_widgets(bottom.y + bottom.h if bottom else 0):
                if not connection.execute('SELECT 1 FROM dashboard_widgets WHERE id=?', (widget.id,)).fetchone():
                    store._save_record(connection, 'widgets', widget.model_copy(update={'dashboard_id': 'system-statistics'}), 0)
        # A click on a day of the stock timeline opens the positions of that day.
        row = connection.execute("SELECT * FROM dashboard_widgets WHERE id='main-timeline'").fetchone()
        if row is not None:
            timeline = store._decode('widgets', row, connection)
            action = timeline.interaction
            if action and action.click_action == 'DRILL_THROUGH' and (action.target_page or 'STATISTICS') == 'STATISTICS' and action.target_section == 'overview':
                store._save_record(connection, 'widgets', timeline.model_copy(update={'interaction': action.model_copy(update={'target_section': 'positions'})}), timeline.revision)
        connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (8, store._now()))


def bootstrap_position_board(store):
    """Show the stock positions table as the grouped day/night board."""
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=9').fetchone():
            return
        row = connection.execute("SELECT * FROM dashboard_widgets WHERE id='stats-positions-table'").fetchone()
        if row is not None:
            widget = store._decode('widgets', row, connection)
            # Only the widget as shipped in v8; one an administrator changed is left alone.
            if widget.visualization.type == 'table' and [metric.key for metric in widget.query.metrics] == ['flights', 'day_flights', 'night_flights', 'efficiency']:
                query = widget.query.model_copy(update={'metrics': [type(widget.query.metrics[0])(key=key) for key in _POSITION_KEYS]})
                layout = widget.layout.model_copy(update={'h': max(widget.layout.h, 16)})
                store._save_record(connection, 'widgets', widget.model_copy(update={'query': query, 'layout': layout, 'visualization': widget.visualization.model_copy(update={'type': 'position_board'})}), widget.revision)
        connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (9, store._now()))


def bootstrap_loss_penalty(store):
    """Add the lost-drone "minus" to the stock effectiveness once; keep edited ones."""
    from app.bi.engine import default_metric_definitions
    from app.bi.metric_texts import updated_texts
    from app.bi.reports import _calculation_fields
    templates = {metric.key: metric for metric in default_metric_definitions()}
    efficiency = templates['efficiency']
    legacy = efficiency.model_copy(update={'numerator': 'effective'})
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM bi_bootstrap_versions WHERE version=10').fetchone():
            return
        for key in ('penalized', 'effective_net'):
            metric = templates[key]
            if not connection.execute('SELECT 1 FROM metric_definitions WHERE metric_key=? OR id=?', (metric.key, metric.id)).fetchone():
                store._save_record(connection, 'metrics', metric, 0)
        for key in ('effective', 'efficiency'):
            row = connection.execute('SELECT * FROM metric_definitions WHERE id=?', (templates[key].id,)).fetchone()
            if row is None:
                continue
            current = store._decode('metrics', row, connection)
            changes = updated_texts(current.key, current.title, current.description)
            if key == 'efficiency' and _calculation_fields(current.model_dump(mode='json')) == _calculation_fields(legacy.model_dump(mode='json')):
                changes['numerator'] = 'effective_net'
            if changes:
                store._save_record(connection, 'metrics', current.model_copy(update=changes), current.revision)
        connection.execute('INSERT INTO bi_bootstrap_versions VALUES (?,?)', (10, store._now()))


def bootstrap(store):
    _bootstrap_pages(store)
    bootstrap_success_semantics(store)
    bootstrap_metric_texts(store)
    bootstrap_overview_widgets(store)
    bootstrap_ammunition_spending(store)
    bootstrap_positions(store)
    bootstrap_position_board(store)
    bootstrap_loss_penalty(store)
