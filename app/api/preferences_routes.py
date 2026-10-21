"""Each user's filters and display settings, kept on the server so every device shows the same."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.bi_routes import no_store, operation, principal
from app.bi.models import BiModel
from app.bi.security import Principal
from app.bi.store import get_bi_store
from app.core.user_accounts import check_request_origin

router = APIRouter(prefix='/v1', dependencies=[Depends(no_store)])
Section = Literal['filters', 'ui', 'map', 'whats_new', 'comparison', 'ranking']
MAX_BYTES = 64 * 1024


class PreferenceValue(BiModel):
    value: Any


def _owner(user: Principal) -> str:
    if not user.user_id:
        raise HTTPException(404, 'Preferences need a signed-in account')
    return user.user_id


@router.get('/me/preferences', operation_id='getUserPreferences')
def get_preferences(user: Principal = Depends(principal)):
    if not user.user_id:
        return {}

    def run():
        with get_bi_store()._connection() as connection:
            rows = connection.execute('SELECT section, revision, value_json, updated_at FROM user_preferences WHERE user_id=?', (user.user_id,)).fetchall()
        return {row['section']: {'value': json.loads(row['value_json']), 'revision': row['revision'], 'updated_at': row['updated_at']} for row in rows}
    return operation(run)


@router.put('/me/preferences/{section}', operation_id='saveUserPreference')
def save_preference(section: Section, body: PreferenceValue, request: Request, user: Principal = Depends(principal)):
    owner = _owner(user)
    check_request_origin(request)
    encoded = json.dumps(body.value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > MAX_BYTES:
        raise HTTPException(413, 'Preferences are limited to 64 KiB per section')

    def run():
        # The latest write wins: settings change one at a time from the user's own devices.
        now = datetime.now(timezone.utc).isoformat()
        with get_bi_store()._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT INTO user_preferences VALUES (?,?,1,?,?) ON CONFLICT(user_id, section) DO UPDATE SET revision=user_preferences.revision+1, value_json=excluded.value_json, updated_at=excluded.updated_at', (owner, section, encoded, now))
            revision = connection.execute('SELECT revision FROM user_preferences WHERE user_id=? AND section=?', (owner, section)).fetchone()[0]
        return {'section': section, 'revision': revision, 'updated_at': now}
    return operation(run)
