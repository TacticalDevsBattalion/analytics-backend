"""Session login, administrator account management and per-user preferences."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from app.core import keycloak
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.api.configuration_routes import require_administrator
from app.core.user_accounts import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    CreateUserRequest,
    DashboardResponse,
    DashboardUpdate,
    LoginRequest,
    PublicUser,
    SessionResponse,
    SessionUser,
    UpdateUserRequest,
    account_operation,
    check_request_origin,
    get_user_account_store,
    require_signed_user,
    secure_session_cookie,
    session_identity,
)


class PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default validation response includes input values.
                # An account form must never echo a password, including when invalid.
                errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
                return JSONResponse(status_code=422, content={"detail": errors}, headers={"Cache-Control": "no-store"})

        return validated


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(route_class=PrivateValidationRoute, dependencies=[Depends(no_store)])


@router.get("/auth/session", response_model=SessionResponse)
def auth_session(request: Request):
    enabled = keycloak.enabled() or account_operation(lambda: get_user_account_store().enabled())
    return SessionResponse(enabled=enabled, user=session_identity(request))


@router.post("/auth/login", response_model=SessionResponse)
def auth_login(body: LoginRequest, request: Request, response: Response):
    if keycloak.enabled():
        raise HTTPException(403, "Use Keycloak to sign in")
    check_request_origin(request)
    store = get_user_account_store()
    client = request.client.host if request.client is not None else "unknown"
    token, user = account_operation(lambda: store.login(body.username, body.password.get_secret_value(), client))
    previous = request.cookies.get(SESSION_COOKIE)
    account_operation(lambda: store.logout(previous))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=secure_session_cookie(request), samesite="strict", path="/")
    return SessionResponse(enabled=True, user=user)


@router.post("/auth/logout", response_model=SessionResponse)
def auth_logout(request: Request, response: Response):
    check_request_origin(request)
    store = get_user_account_store()
    account_operation(lambda: store.logout(request.cookies.get(SESSION_COOKIE)))
    enabled = keycloak.enabled() or account_operation(store.enabled)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=secure_session_cookie(request), samesite="strict")
    return SessionResponse(enabled=enabled, user=None)


@router.get("/me/dashboard", response_model=DashboardResponse)
def personal_dashboard(user: SessionUser = Depends(require_signed_user)):
    return account_operation(lambda: get_user_account_store().dashboard(user.id))


@router.put("/me/dashboard", response_model=DashboardResponse)
def save_personal_dashboard(body: DashboardUpdate, user: SessionUser = Depends(require_signed_user)):
    return account_operation(lambda: get_user_account_store().save_dashboard(user.id, body))


@router.get("/admin/users", dependencies=[Depends(require_administrator)])
def account_list():
    return {"users": account_operation(lambda: get_user_account_store().users())}


@router.post("/admin/users", status_code=201, response_model=PublicUser, dependencies=[Depends(require_administrator)])
def create_account(body: CreateUserRequest):
    # Without accounts, require_administrator can only authorize the legacy key.
    return account_operation(lambda: get_user_account_store().create_user(body))


@router.patch("/admin/users/{user_id}", response_model=PublicUser, dependencies=[Depends(require_administrator)])
def update_account(user_id: str, body: UpdateUserRequest):
    return account_operation(lambda: get_user_account_store().update_user(user_id, body))
