"""Administration of SQL datasets, change history and usage. Administrators only."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from app.api.bi_routes import no_store, operation, principal
from app.bi import activity
from app.bi.models import BiModel
from app.bi.security import Principal
from app.bi.sql_datasets import DatasetNotFound, get_dataset_store
from app.bi.store import get_bi_store
from app.core.user_accounts import check_request_origin

router = APIRouter(prefix='/v1/admin', dependencies=[Depends(no_store)])


def administrator(user: Principal = Depends(principal)) -> Principal:
    if not user.is_administrator:
        raise HTTPException(403, 'Only administrators manage SQL datasets')
    return user


def run(callback):
    def guarded():
        try:
            return callback()
        except DatasetNotFound as exc:
            raise HTTPException(404, str(exc)) from None
    return operation(guarded)


class DatasetVersion(BiModel):
    sql: str = Field(min_length=1, max_length=100_000)
    comment: str = Field(default='', max_length=500)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class DatasetPublish(BiModel):
    version: int = Field(ge=1)


class DatasetTest(BiModel):
    version: int | None = Field(default=None, ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=50, ge=1, le=500)


@router.get('/sql/datasets', operation_id='listSqlDatasets')
def list_datasets(user: Principal = Depends(administrator)):
    return run(lambda: get_dataset_store().list())


@router.get('/sql/datasets/{key}', operation_id='getSqlDataset')
def get_dataset(key: str, user: Principal = Depends(administrator)):
    return run(lambda: get_dataset_store().get(key))


@router.post('/sql/datasets/{key}/versions', operation_id='saveSqlDatasetVersion')
def save_version(key: str, body: DatasetVersion, request: Request, user: Principal = Depends(administrator)):
    check_request_origin(request)
    return run(lambda: get_dataset_store().save_version(key, body.sql, actor=user.user_id, comment=body.comment, title=body.title, description=body.description))


@router.post('/sql/datasets/{key}/publish', operation_id='publishSqlDataset')
def publish(key: str, body: DatasetPublish, request: Request, user: Principal = Depends(administrator)):
    check_request_origin(request)
    run(lambda: get_dataset_store().publish(key, body.version, actor=user.user_id))
    return {'dataset_key': key, 'published_version': body.version}


@router.post('/sql/datasets/{key}/archive', operation_id='archiveSqlDataset')
def archive(key: str, request: Request, user: Principal = Depends(administrator)):
    check_request_origin(request)
    run(lambda: get_dataset_store().archive(key, actor=user.user_id))
    return {'dataset_key': key, 'archived': True}


@router.post('/sql/datasets/{key}/test', operation_id='testSqlDataset')
def test(key: str, body: DatasetTest, request: Request, user: Principal = Depends(administrator)):
    """A draft or published version on real data, limited to a few rows (still readonly)."""
    check_request_origin(request)
    rows = run(lambda: get_dataset_store().run(key, body.parameters, version=body.version, limit=body.limit))
    return {'rows': rows, 'columns': list(rows[0]) if rows else []}


@router.get('/usage', operation_id='getAnalyticsUsage')
def usage(days: int = Query(30, ge=1, le=365), user: Principal = Depends(administrator)):
    return run(lambda: activity.usage(get_bi_store(), days))


@router.get('/history', operation_id='getAnalyticsHistory')
def history(entity_type: str | None = None, entity_id: str | None = None, limit: int = Query(200, ge=1, le=1000), user: Principal = Depends(administrator)):
    return run(lambda: activity.history(get_bi_store(), entity_type, entity_id, limit))
