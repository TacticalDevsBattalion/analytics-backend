"""Simplified user access: a profile, a unit and one switch per user, with an audit log."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from app.api.bi_routes import no_store, operation, principal
from app.bi import access_profiles
from app.bi.models import BiModel, UserAccessDefinition
from app.bi.security import Principal, require_permission
from app.bi.store import BiConflict, get_bi_store
from app.core.user_accounts import UpdateUserRequest, account_operation, check_request_origin, get_user_account_store

router = APIRouter(prefix='/v1', dependencies=[Depends(no_store)])


class AccessAssignment(BiModel):
    profile: Literal['company', 'battalion', 'command', 'admin']
    battalion_id: str | None = Field(default=None, max_length=64)
    company_id: str | None = Field(default=None, max_length=64)
    coordinates: bool = False
    # Current access revision; None for a user who has never had access assigned.
    expected_revision: int | None = None


class BulkAssignment(AccessAssignment):
    user_ids: list[str] = Field(min_length=1, max_length=500)


def _hierarchy(user: Principal) -> dict:
    from app.services.bi_legacy import hierarchy_options
    try:
        return hierarchy_options(user)
    except Exception:
        raise HTTPException(503, 'The list of battalions and companies is unavailable; check the ClickHouse connection') from None


def _titles(hierarchy: dict) -> tuple[dict[str, str], dict[str, str]]:
    return ({item['id']: item['title'] for item in hierarchy.get('departments', [])},
            {item['id']: item['title'] for item in hierarchy.get('groups', [])})


def _status(access: UserAccessDefinition, role: str) -> str:
    if access.profile is not None or role == 'administrator':
        return 'assigned'
    if access.revision and (access.role_ids or access.permissions or access.data_scope.scope_type != 'NONE'):
        return 'custom'  # Set up the old way, with roles and permissions.
    return 'pending'


def _row(account, access: UserAccessDefinition, titles) -> dict:
    battalions, companies = titles
    profile = access.profile or ('admin' if account.role == 'administrator' else None)
    battalion, company = battalions.get(access.battalion_id or ''), companies.get(access.company_id or '')
    status = _status(access, account.role)
    summary = (access_profiles.legacy_summary(access.data_scope.scope_type, len(access.permissions) + len(access.role_ids)) if status == 'custom'
               else access_profiles.summary(profile, battalion or access.battalion_id, company or access.company_id, access.coordinates))
    return {
        'id': account.id, 'username': account.username, 'display_name': account.display_name, 'role': account.role,
        'active': account.active, 'created_at': account.created_at, 'status': status,
        'profile': profile, 'battalion_id': access.battalion_id, 'company_id': access.company_id,
        'battalion': battalion, 'company': company, 'coordinates': access.coordinates, 'revision': access.revision,
        'summary': summary,
    }


@router.get('/admin/access', operation_id='listUserAccess')
def list_access(user: Principal = Depends(principal)):
    require_permission(user, 'analytics.admin')
    hierarchy = _hierarchy(user)
    titles = _titles(hierarchy)
    accounts = account_operation(lambda: get_user_account_store().users())
    store = get_bi_store()
    return {
        'profiles': [{'id': key, 'title': title} for key, title in access_profiles.TITLES.items()],
        'battalions': hierarchy.get('departments', []),
        'companies': hierarchy.get('groups', []),
        'users': [_row(account, operation(lambda: store.user_access(account.id)), titles) for account in accounts],
    }


def _assign(user_id: str, body: AccessAssignment, actor: Principal, hierarchy: dict, *, check_revision: bool) -> dict:
    accounts = get_user_account_store()
    account = next((item for item in account_operation(accounts.users) if item.id == user_id), None)
    if account is None:
        raise HTTPException(404, 'Account was not found')
    try:
        battalion_id, company_id = access_profiles.validate(body.profile, body.battalion_id, body.company_id, hierarchy)
    except access_profiles.AccessError as exc:
        raise HTTPException(422, str(exc)) from None
    store = get_bi_store()
    current = operation(lambda: store.user_access(user_id))
    assigned = not current.id.startswith('unassigned:')
    if check_revision and (current.revision if assigned else None) != body.expected_revision:
        raise HTTPException(409, 'Access changed elsewhere; reload before saving')
    before = current.model_dump(mode='json') if assigned else None
    updated = UserAccessDefinition(
        id=current.id if assigned else uuid.uuid4().hex, revision=current.revision if assigned else 0, user_id=user_id,
        profile=body.profile, battalion_id=battalion_id, company_id=company_id,
        coordinates=body.coordinates and body.profile in {'company', 'battalion'},
        # Kept consistent for older readers of the record.
        data_scope=access_profiles.scope_for(body.profile, battalion_id, company_id), comparison_precision='EXACT',
    )
    # The administrator profile is the account role; the last administrator stays.
    role = 'administrator' if body.profile == 'admin' else 'viewer'
    previous_role = account.role
    if role != previous_role:
        account_operation(lambda: accounts.update_user(user_id, UpdateUserRequest(role=role)))
    try:
        saved = operation(lambda: store.save('user_access', updated))
    except BaseException:
        if role != previous_role:
            account_operation(lambda: accounts.update_user(user_id, UpdateUserRequest(role=previous_role)))
        raise
    with store._connection() as connection:
        connection.execute('INSERT INTO access_audit VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex, user_id, actor.user_id, datetime.now(timezone.utc).isoformat(), json.dumps(before, ensure_ascii=False) if before else None, json.dumps(saved.model_dump(mode='json'), ensure_ascii=False)))
    account = account.model_copy(update={'role': role})
    return _row(account, saved, _titles(hierarchy))


@router.put('/admin/access/{user_id}', operation_id='assignUserAccess')
def assign_access(user_id: str, body: AccessAssignment, request: Request, user: Principal = Depends(principal)):
    require_permission(user, 'analytics.admin')
    check_request_origin(request)
    try:
        return _assign(user_id, body, user, _hierarchy(user), check_revision=True)
    except BiConflict as exc:
        raise HTTPException(409, str(exc)) from None


@router.put('/admin/access', operation_id='assignUserAccessInBulk')
def assign_access_in_bulk(body: BulkAssignment, request: Request, user: Principal = Depends(principal)):
    require_permission(user, 'analytics.admin')
    check_request_origin(request)
    hierarchy = _hierarchy(user)
    results, errors = [], []
    for user_id in dict.fromkeys(body.user_ids):
        try:
            results.append(_assign(user_id, body, user, hierarchy, check_revision=False))
        except HTTPException as exc:
            errors.append({'user_id': user_id, 'error': exc.detail})
        except BiConflict as exc:
            errors.append({'user_id': user_id, 'error': str(exc)})
    return {'users': results, 'errors': errors}


@router.get('/admin/access/{user_id}/audit', operation_id='getUserAccessAudit')
def access_audit(user_id: str, user: Principal = Depends(principal)):
    require_permission(user, 'analytics.admin')
    accounts = {item.id: item.display_name or item.username for item in account_operation(lambda: get_user_account_store().users())}

    def run():
        with get_bi_store()._connection() as connection:
            rows = connection.execute('SELECT * FROM access_audit WHERE user_id=? ORDER BY changed_at DESC LIMIT 50', (user_id,)).fetchall()
        return [{'changed_at': row['changed_at'], 'actor': accounts.get(row['actor_id'], row['actor_id']), 'before': json.loads(row['before_json']) if row['before_json'] else None, 'after': json.loads(row['after_json'])} for row in rows]
    return operation(run)
