"""Optional Keycloak OIDC sign-in. Tokens stay on the backend.

Two ways in: the application's own form (password grant, the default) and the
redirect code flow to Keycloak's page, kept for accounts that must finish setup there."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import HTTPException

from app.core.user_accounts import (
    SESSION_SECONDS, AuthenticationFailed, SessionUser, get_user_account_store,
    password_hash, token_digest,
)

LOGIN_COOKIE = "analytics_oidc_login"


def enabled() -> bool:
    return os.environ.get("KEYCLOAK_ENABLED", "").lower() in {"1", "true", "yes"}


def password_login_enabled() -> bool:
    """The client must allow "Direct access grants" in Keycloak."""
    return enabled() and os.environ.get("KEYCLOAK_PASSWORD_LOGIN", "true").lower() in {"1", "true", "yes"}


class SetupRequired(Exception):
    """Keycloak needs the user to finish account setup (new password, OTP…) on its own page."""


def configuration() -> dict[str, str]:
    if not enabled():
        raise HTTPException(404, "Keycloak authentication is disabled")
    issuer = os.environ.get("KEYCLOAK_ISSUER", "").strip().rstrip("/")
    client = os.environ.get("KEYCLOAK_CLIENT_ID", "").strip()
    callback = os.environ.get("KEYCLOAK_REDIRECT_URI", "").strip()
    frontend = os.environ.get("KEYCLOAK_FRONTEND_URL", "").strip()
    allow_http = os.environ.get("KEYCLOAK_ALLOW_HTTP", "").lower() in {"1", "true"}
    for value in (issuer, callback, frontend):
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username or parsed.password or parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise HTTPException(503, "Keycloak URLs must be configured with HTTPS")
    if not client:
        raise HTTPException(503, "Keycloak client is not configured")
    return {"issuer": issuer, "client_id": client, "redirect_uri": callback,
            "frontend": frontend, "secret": os.environ.get("KEYCLOAK_CLIENT_SECRET", ""),
            "admin_role": os.environ.get("KEYCLOAK_ADMIN_ROLE", "").strip()}


def _migrate(connection) -> None:
    from app.core import database
    if database.is_postgres(connection):
        return  # Part of the PostgreSQL schema.
    connection.execute("CREATE TABLE IF NOT EXISTS account_schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if connection.execute("SELECT 1 FROM account_schema_migrations WHERE version='001_keycloak'").fetchone():
        return
    source = Path(__file__).resolve().parents[1] / "migrations" / "001_keycloak.sql"
    for statement in source.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute("INSERT INTO account_schema_migrations VALUES (?,?)", ("001_keycloak", datetime.now(timezone.utc).isoformat()))


# Keycloak actions a signed-in user may start from the application.
ACTIONS = {"password": "UPDATE_PASSWORD"}


def begin_login(action: str | None = None, login_hint: str | None = None) -> tuple[str, str]:
    """Keycloak's own page also handles what the password form cannot: a temporary
    password, OTP setup, and with an action a voluntary password change."""
    config = configuration()
    if action is not None and action not in ACTIONS:
        raise HTTPException(400, "Unknown Keycloak action")
    state, browser, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with get_user_account_store()._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migrate(connection)
        connection.execute("DELETE FROM oidc_login_states WHERE expires_at<?", (time.time(),))
        connection.execute("INSERT INTO oidc_login_states VALUES (?,?,?,?,?)", (token_digest(state), token_digest(browser), verifier, nonce, time.time() + 300))
    parameters = {"client_id": config["client_id"], "redirect_uri": config["redirect_uri"],
                  "response_type": "code", "scope": "openid profile email", "state": state,
                  "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    if action:
        parameters["kc_action"] = ACTIONS[action]
    if login_hint and len(login_hint) <= 255 and login_hint.isprintable():
        parameters["login_hint"] = login_hint
    return config["issuer"] + "/protocol/openid-connect/auth?" + urlencode(parameters), browser


def logout_url() -> str | None:
    """RP-initiated logout ends the Keycloak SSO session, not only the local one."""
    try:
        config = configuration()
    except HTTPException:
        return None
    parameters = {"client_id": config["client_id"], "post_logout_redirect_uri": config["frontend"]}
    return config["issuer"] + "/protocol/openid-connect/logout?" + urlencode(parameters)


def frontend_with_error(reason: str) -> str:
    return frontend_with({"auth_error": reason})


def frontend_with(parameters: dict[str, str]) -> str:
    frontend = configuration()["frontend"]
    return frontend + ("&" if urlsplit(frontend).query else "?") + urlencode(parameters)


def consume_state(state: str, browser: str | None) -> tuple[str, str]:
    if not browser or not state or len(state) > 128 or len(browser) > 128:
        raise HTTPException(400, "Invalid or expired sign-in request")
    with get_user_account_store()._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migrate(connection)
        row = connection.execute("SELECT * FROM oidc_login_states WHERE state_hash=?", (token_digest(state),)).fetchone()
        if row is None or row["expires_at"] < time.time() or not secrets.compare_digest(row["browser_hash"], token_digest(browser)):
            raise HTTPException(400, "Invalid or expired sign-in request")
        connection.execute("DELETE FROM oidc_login_states WHERE state_hash=?", (token_digest(state),))
        return row["verifier"], row["nonce"]


def _signing_key(token: str, config: dict[str, str]):
    # Never select verification algorithms, issuer or JWKS URLs from token claims.
    with httpx.Client(timeout=15) as client:
        response = client.get(config["issuer"] + "/protocol/openid-connect/certs")
        response.raise_for_status()
        keys = response.json().get("keys", [])
    header = jwt.get_unverified_header(token)
    candidates = [key for key in keys if key.get("kid") == header.get("kid") and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"]
    if len(candidates) != 1:
        raise jwt.InvalidTokenError("Signing key is unavailable")
    return jwt.PyJWK.from_dict(candidates[0], algorithm="RS256")


def token_roles(claims: dict, client_id: str) -> set[str]:
    """Realm roles and this client's roles."""
    roles = list(claims.get("realm_access", {}).get("roles", []))
    roles += claims.get("resource_access", {}).get(client_id, {}).get("roles", [])
    return {str(role) for role in roles}


def access_token_roles(token: object, config: dict[str, str], subject: str) -> set[str]:
    """Keycloak puts roles into the access token by default, not into the ID token.
    Its audience is the resource servers, so only signature, issuer, subject and the
    authorized party (this client) are checked."""
    if not isinstance(token, str):
        return set()
    try:
        claims = jwt.decode(token, _signing_key(token, config).key, algorithms=["RS256"], issuer=config["issuer"],
                            options={"verify_aud": False, "require": ["exp", "iss", "sub"]})
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError):
        return set()
    if str(claims.get("sub")) != subject or claims.get("azp") not in {None, config["client_id"]}:
        return set()
    return token_roles(claims, config["client_id"])


def validate_id_token(token: str, config: dict[str, str], nonce: str | None) -> dict:
    key = _signing_key(token, config)
    # The password grant has no browser round trip, so no nonce to bind.
    required = ["exp", "iat", "iss", "aud", "sub"] + (["nonce"] if nonce is not None else [])
    claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=config["client_id"],
                        issuer=config["issuer"], options={"require": required})
    if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise jwt.InvalidTokenError("Nonce does not match")
    if claims.get("azp") is not None and claims["azp"] != config["client_id"]:
        raise jwt.InvalidTokenError("Authorized party does not match")
    if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != config["client_id"]:
        raise jwt.InvalidTokenError("Authorized party is required")
    return claims


def establish_session(claims: dict, config: dict[str, str], extra_roles: set[str] = frozenset()) -> tuple[str, SessionUser]:
    subject = str(claims["sub"])
    identity = hashlib.sha256((config["issuer"] + "\0" + subject).encode()).hexdigest()
    user_id = "kc_" + identity
    username = "kc_" + identity[:24]
    display = str(claims.get("name") or claims.get("preferred_username") or username)[:120]
    roles = {role.casefold() for role in token_roles(claims, config["client_id"]) | set(extra_roles)}
    is_admin = bool(config["admin_role"]) and config["admin_role"].casefold() in roles
    role = "administrator" if is_admin else "viewer"
    token = secrets.token_urlsafe(32)
    store = get_user_account_store()
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migrate(connection)
        row = connection.execute("SELECT users.* FROM federated_identities JOIN users ON users.id=federated_identities.user_id WHERE issuer=? AND subject=?", (config["issuer"], subject)).fetchone()
        if row is None:
            created = datetime.now(timezone.utc).isoformat()
            connection.execute("INSERT INTO users (id,username,username_key,display_name,role,active,created_at,password_hash,auth_version) VALUES (?,?,?,?,?,1,?,?,0)", (user_id, username, username, display, role, created, password_hash(secrets.token_urlsafe(48))))
            connection.execute("INSERT INTO federated_identities VALUES (?,?,?)", (config["issuer"], subject, user_id))
            row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        elif not row["active"]:
            raise AuthenticationFailed("Account is disabled")
        # Application access and local suspension are managed by its administrators.
        # The Keycloak administrator role promotes on every sign-in but never demotes.
        if is_admin and row["role"] != "administrator":
            connection.execute("UPDATE users SET role='administrator' WHERE id=?", (row["id"],))
            row = connection.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        user_id = row["id"]
        connection.execute("UPDATE users SET display_name=? WHERE id=?", (display, user_id))
        connection.execute("UPDATE account_state SET ever_enabled=1 WHERE id=1")
        connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (token_digest(token), user_id, row["auth_version"], store.clock(), store.clock() + SESSION_SECONDS))
        return token, SessionUser(id=user_id, username=row["username"], display_name=display, role=row["role"])


def complete_login(code: str, state: str, browser: str | None) -> tuple[str, SessionUser]:
    config = configuration()
    verifier, nonce = consume_state(state, browser)
    data = {"grant_type": "authorization_code", "code": code, "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"], "code_verifier": verifier}
    if config["secret"]:
        data["client_secret"] = config["secret"]
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(config["issuer"] + "/protocol/openid-connect/token", data=data)
            response.raise_for_status()
            answer = response.json()
        token = answer.get("id_token")
        if not isinstance(token, str):
            raise jwt.InvalidTokenError("ID token is unavailable")
        claims = validate_id_token(token, config, nonce)
        return establish_session(claims, config, access_token_roles(answer.get("access_token"), config, str(claims["sub"])))
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Keycloak sign-in could not be verified") from None


def password_login(username: str, password: str, client: str) -> tuple[str, SessionUser]:
    """Sign in with the application's own form: the backend checks the password with Keycloak."""
    config = configuration()
    if not password_login_enabled():
        raise HTTPException(404, "Password sign-in is disabled")
    store = get_user_account_store()
    store._reserve_login_attempt(username, client)
    data = {"grant_type": "password", "client_id": config["client_id"], "username": username,
            "password": password, "scope": "openid profile email"}
    if config["secret"]:
        data["client_secret"] = config["secret"]
    try:
        with httpx.Client(timeout=15) as http:
            response = http.post(config["issuer"] + "/protocol/openid-connect/token", data=data)
    except httpx.HTTPError:
        raise HTTPException(503, "Keycloak is unavailable") from None
    if response.status_code in {400, 401}:
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        error, description = str(answer.get("error", "")), str(answer.get("error_description", "")).lower()
        if error == "invalid_grant" and "not fully set up" in description:
            raise SetupRequired("Account setup must be completed in Keycloak")
        if error == "invalid_grant":
            raise AuthenticationFailed("Username or password is incorrect")
        # unauthorized_client: "Direct access grants" is off for this client.
        raise HTTPException(503, "Password sign-in is not allowed for this Keycloak client")
    try:
        response.raise_for_status()
        answer = response.json()
        token = answer.get("id_token")
        if not isinstance(token, str):
            raise jwt.InvalidTokenError("ID token is unavailable")
        claims = validate_id_token(token, config, None)
        result = establish_session(claims, config, access_token_roles(answer.get("access_token"), config, str(claims["sub"])))
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(502, "Keycloak sign-in could not be verified") from None
    store.clear_login_attempts(username)
    return result
