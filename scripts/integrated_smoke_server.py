"""Isolated UI verification server; never connects to the configured warehouse.

Run with APP_*_DB pointing into .smoke and DATA_SOURCE=mock. The real application,
authentication and storage are used; only the warehouse adapter is a fixture.
"""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from app.bi.engine import QueryEngine
from app.bi.store import get_bi_store
from app.core.config import get_settings
from app.core.user_accounts import CreateUserRequest, get_user_account_store

settings = get_settings()
if settings.active_source != 'mock' or '.smoke' not in Path(get_bi_store().path).parts:
    raise RuntimeError('The UI fixture requires mock mode and an isolated .smoke database')


def rows(start, end):
    dataset = {source: [] for source in ('flights', 'events', 'ammunition', 'lost_devices')}
    for offset in range((end - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        for unit in (1, 2):
            identifier = f'{day}-flight-{unit}'
            dataset['flights'].append({'flight_id': identifier, 'date': day, 'time_range': '10:00 - 10:30', 'bbak_id': unit, 'bbak_title': f'Підрозділ {unit}', 'rota_id': unit * 10, 'rota_title': f'Рота {unit}', 'crew': f'Екіпаж {unit}', 'device': 'Контрольний засіб', 'device_type': 'FPV' if unit == 1 else 'Mavic', 'main_purpose': 'Удар' if unit == 1 else 'Розвідка', 'is_effective': unit == 1, 'direction': 'АК 1' if unit == 1 else 'АК 2', 'position': f'Позиція {unit}'})
            dataset['events'].append({'flight_id': identifier, 'date': day, 'time': '10:15', 'row_hash': f'event-{identifier}', 'target_id': f'target-{identifier}', 'target_class': 'Бронетехніка' if unit == 1 else 'ОС', 'units': ['Північ' if unit == 1 else 'Південь'], 'result': 'уражено' if offset % 2 else 'знищено', 'field_200': 2 if unit == 2 else 0, 'field_300': 3 if unit == 2 else 0})
            dataset['ammunition'].append({'flight_id': identifier, 'date': day, 'bc_name': 'виявлено', 'bc_count': 2})
            if offset % 7 == 0:
                dataset['lost_devices'].append({'flight_id': identifier, 'date': day, 'time': '10:20', 'row_hash': f'loss-{identifier}', 'device': 'Контрольний засіб', 'device_type': 'FPV' if unit == 1 else 'Mavic', 'result': 'втрата'})
    return deepcopy(dataset)


from app.main import app
from app.api import analytics_pages, bi_routes, personal_dashboard
from app.services import analytics, bi_legacy, mock_analytics

factory = lambda: QueryEngine(get_bi_store(), rows)
bi_routes.engine = analytics_pages.engine = personal_dashboard.engine = factory
analytics.warm_cache = lambda: None
analytics.kpi_options = lambda: {'purposes': ['Удар', 'Розвідка'], 'results': ['уражено', 'знищено']}
analytics.options = lambda: {**mock_analytics.filter_options(), 'category': ['FPV', 'Mavic'], 'direction': ['АК 1', 'АК 2'], 'unit': ['Північ', 'Південь'], 'group': ['Екіпаж 1', 'Екіпаж 2'], 'bbak': [{'id': 1, 'title': 'Підрозділ 1'}, {'id': 2, 'title': 'Підрозділ 2'}], 'rota': ['10', '20'], 'purpose': ['Удар', 'Розвідка'], 'class_name': ['Бронетехніка', 'ОС'], 'result': ['уражено', 'знищено']}
bi_legacy.hierarchy_options = lambda principal: {'departments': [{'id': '1', 'title': 'Підрозділ 1'}, {'id': '2', 'title': 'Підрозділ 2'}], 'groups': [{'id': '10', 'title': 'Рота 1', 'department_id': '1'}, {'id': '20', 'title': 'Рота 2', 'department_id': '2'}], 'teams': [{'id': 'Екіпаж 1', 'title': 'Екіпаж 1', 'department_id': '1', 'group_id': '10'}, {'id': 'Екіпаж 2', 'title': 'Екіпаж 2', 'department_id': '2', 'group_id': '20'}], 'categories': [{'id': 'FPV', 'title': 'FPV'}, {'id': 'Mavic', 'title': 'Mavic'}], 'crews': [{'id': 'Екіпаж 1', 'title': 'Екіпаж 1', 'category': 'FPV'}, {'id': 'Екіпаж 2', 'title': 'Екіпаж 2', 'category': 'Mavic'}]}
accounts = get_user_account_store()
if not accounts.enabled():
    accounts.create_user(CreateUserRequest(username='smoke-admin', display_name='UI verification', password='isolated-smoke-test-password', role='administrator'))
