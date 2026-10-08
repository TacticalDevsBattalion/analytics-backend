"""Public application settings and a separately authenticated administration API."""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Callable, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field, ValidationError

from app.core.app_configuration import (
    AdministrationSnapshot,
    ApplicationConfiguration,
    ConfigurationConflict,
    ConfigurationModel,
    ConfigurationRevisionNotFound,
    default_application_configuration,
    get_configuration_store,
)
from app.core.user_accounts import account_operation, get_user_account_store, legacy_administrator, require_account_administrator, session_identity

logger = logging.getLogger(__name__)
T = TypeVar("T")


def administration_enabled() -> bool:
    return bool(os.environ.get("APP_ADMIN_TOKEN", "").strip()) or account_operation(lambda: get_user_account_store().enabled())


def require_administrator(request: Request) -> None:
    require_account_administrator(request)


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])


class PublicConfiguration(ConfigurationModel):
    revision: int
    config: ApplicationConfiguration
    administration_enabled: bool


class PreviewRequest(ConfigurationModel):
    config: ApplicationConfiguration
    base_revision: int = Field(ge=1, strict=True)


class DraftRequest(PreviewRequest):
    expected_draft_version: int = Field(ge=0, strict=True)


class RevisionRequest(ConfigurationModel):
    base_revision: int = Field(ge=1, strict=True)


class PublishRequest(RevisionRequest):
    expected_draft_version: int = Field(ge=0, strict=True)


class RollbackRequest(RevisionRequest):
    target_revision: int = Field(ge=1, strict=True)


class PreviewResponse(ConfigurationModel):
    valid: bool
    revision: int
    config: ApplicationConfiguration


def run_operation(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except ConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigurationRevisionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (sqlite3.Error, OSError, ValidationError) as exc:
        logger.exception("Application configuration storage is unavailable")
        raise HTTPException(status_code=503, detail="Application configuration is temporarily unavailable") from exc


@router.get("/app-configuration", response_model=PublicConfiguration)
def public_configuration(request: Request):
    accounts_enabled = account_operation(lambda: get_user_account_store().enabled())
    snapshot = run_operation(lambda: get_configuration_store().snapshot())
    configuration = snapshot.config
    if accounts_enabled and not legacy_administrator(request) and session_identity(request) is None:
        # The login screen may display branding but must not expose private KPI
        # rules, unit aliases, custom menus or other shared team settings.
        configuration = default_application_configuration()
        configuration.appearance.title = snapshot.config.appearance.title
        configuration.appearance.subtitle = snapshot.config.appearance.subtitle
    return PublicConfiguration(revision=snapshot.revision, config=configuration, administration_enabled=administration_enabled())


@router.get("/admin/app-configuration", response_model=AdministrationSnapshot, dependencies=[Depends(require_administrator)])
def administrator_configuration():
    return run_operation(lambda: get_configuration_store().snapshot())


@router.get("/admin/filter-columns", dependencies=[Depends(require_administrator)])
def filter_columns():
    """Real ClickHouse columns of the flights table that no built-in filter covers."""
    from app.bi.extra_fields import available_columns

    try:
        return {"columns": available_columns()}
    except Exception as exc:
        logger.exception("ClickHouse schema is unavailable")
        raise HTTPException(status_code=503, detail="ClickHouse schema is temporarily unavailable") from exc


@router.put("/admin/app-configuration/draft", response_model=AdministrationSnapshot, dependencies=[Depends(require_administrator)])
@router.post("/admin/app-configuration/draft", response_model=AdministrationSnapshot, dependencies=[Depends(require_administrator)])
def save_configuration_draft(request: DraftRequest):
    return run_operation(lambda: get_configuration_store().save_draft(request.config, request.base_revision, request.expected_draft_version))


@router.post("/admin/app-configuration/preview", response_model=PreviewResponse, dependencies=[Depends(require_administrator)])
def preview_configuration(request: PreviewRequest):
    # Validate the candidate without saving a draft or changing the live revision.
    return run_operation(lambda: get_configuration_store().preview(request.config, request.base_revision))


@router.post("/admin/app-configuration/publish", response_model=AdministrationSnapshot, dependencies=[Depends(require_administrator)])
def publish_configuration(request: PublishRequest):
    return run_operation(lambda: get_configuration_store().publish(request.base_revision, request.expected_draft_version))


@router.post("/admin/app-configuration/rollback", response_model=AdministrationSnapshot, dependencies=[Depends(require_administrator)])
def rollback_configuration(request: RollbackRequest):
    return run_operation(lambda: get_configuration_store().rollback(request.target_revision, request.base_revision))
