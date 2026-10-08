from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from app.core import keycloak
from app.core.user_accounts import SESSION_COOKIE, SESSION_SECONDS, account_operation, get_user_account_store, secure_session_cookie

router = APIRouter()


@router.get("/auth/provider", operation_id="getAuthenticationProvider")
def authentication_provider():
    return {"provider": "keycloak" if keycloak.enabled() else "local", "login_url": "/api/auth/keycloak/login" if keycloak.enabled() else None}


@router.get("/auth/keycloak/login", operation_id="beginKeycloakLogin")
def login(request: Request):
    url, browser = account_operation(keycloak.begin_login)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(keycloak.LOGIN_COOKIE, browser, max_age=300, httponly=True, secure=secure_session_cookie(request), samesite="lax", path="/api/auth/keycloak")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/keycloak/callback", operation_id="completeKeycloakLogin")
def callback(request: Request, code: str, state: str):
    token, _ = account_operation(lambda: keycloak.complete_login(code, state, request.cookies.get(keycloak.LOGIN_COOKIE)))
    account_operation(lambda: get_user_account_store().logout(request.cookies.get(SESSION_COOKIE)))
    response = RedirectResponse(keycloak.configuration()["frontend"], status_code=303)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=secure_session_cookie(request), samesite="strict", path="/")
    response.delete_cookie(keycloak.LOGIN_COOKIE, path="/api/auth/keycloak")
    response.headers["Cache-Control"] = "no-store"
    return response
