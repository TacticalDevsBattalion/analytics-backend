from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from app.core import keycloak
from app.core.user_accounts import SESSION_COOKIE, SESSION_SECONDS, account_operation, get_user_account_store, secure_session_cookie

router = APIRouter()


@router.get("/auth/provider", operation_id="getAuthenticationProvider")
def authentication_provider():
    provider = ("k2auth" if keycloak.k2auth() else "keycloak") if keycloak.enabled() else "local"
    return {"provider": provider, "login_url": "/api/auth/keycloak/login" if keycloak.enabled() else None,
            "password_login": keycloak.password_login_enabled()}


@router.get("/auth/keycloak/login", operation_id="beginKeycloakLogin")
def login(request: Request, action: str | None = None, login_hint: str | None = None):
    url, browser = account_operation(lambda: keycloak.begin_login(action, login_hint))
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(keycloak.LOGIN_COOKIE, browser, max_age=300, httponly=True, secure=secure_session_cookie(request), samesite="lax", path="/api/auth/keycloak")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/keycloak/callback", operation_id="completeKeycloakLogin")
def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None, kc_action_status: str | None = None):
    # Failures return the browser to the sign-in page instead of a raw JSON error.
    try:
        if error or not code or not state:
            raise HTTPException(400, "Keycloak sign-in was not completed")
        token, _ = account_operation(lambda: keycloak.complete_login(code, state, request.cookies.get(keycloak.LOGIN_COOKIE)))
        account_operation(lambda: get_user_account_store().logout(request.cookies.get(SESSION_COOKIE)))
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        reason = "cancelled" if error == "access_denied" else "unavailable" if exc.status_code == 503 else "failed"
        response = RedirectResponse(keycloak.frontend_with_error(reason), status_code=303)
    else:
        # After a password change Keycloak reports its outcome; the application confirms it.
        target = keycloak.frontend_with({"auth_notice": "password_updated"}) if kc_action_status == "success" else keycloak.configuration()["frontend"]
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=secure_session_cookie(request), samesite="strict", path="/")
    response.delete_cookie(keycloak.LOGIN_COOKIE, path="/api/auth/keycloak")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/k2/backchannel-logout", operation_id="k2BackchannelLogout", include_in_schema=False)
async def k2_backchannel_logout(request: Request):
    # Called by K2 Auth's server over the cluster network, not by browsers (form-encoded logout_token).
    body = await request.body()
    if len(body) > 32768:
        raise HTTPException(400, "Invalid logout token")
    values = parse_qs(body.decode("utf-8", "replace")).get("logout_token", [])
    token = values[0] if len(values) == 1 else ""
    await run_in_threadpool(account_operation, lambda: keycloak.backchannel_logout(token))
    return Response(status_code=200, headers={"Cache-Control": "no-store"})
