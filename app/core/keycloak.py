"""Optional Keycloak OIDC sign-in. Tokens stay on the backend.

Two ways in: the application's own form (password grant, the default) and the
redirect code flow to Keycloak's page, kept for accounts that must finish setup there.

With AUTH_PROVIDER=k2auth the sign-in goes through K2 Auth, the shared login page of
the K2 apps (as in Літопис): code flow + PKCE only, no password form here. K2 Auth's
`sub` is the person's Keycloak `sub`, so accounts are kept under KEYCLOAK_ISSUER and
people signed in with Keycloak before keep their account and access."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from fastapi import HTTPException

from app.core.user_accounts import (
    SESSION_SECONDS, AuthenticationFailed, SessionUser, get_user_account_store,
    password_hash, token_digest,
)

LOGIN_COOKIE = "analytics_oidc_login"


def k2auth() -> bool:
    """Sign-in through K2 Auth instead of Keycloak directly."""
    return os.environ.get("AUTH_PROVIDER", "").strip().lower() == "k2auth"


def enabled() -> bool:
    return k2auth() or os.environ.get("KEYCLOAK_ENABLED", "").lower() in {"1", "true", "yes"}


def password_login_enabled() -> bool:
    """The client must allow "Direct access grants" in Keycloak. Never with K2 Auth:
    the password is typed only on its page."""
    return enabled() and not k2auth() and os.environ.get("KEYCLOAK_PASSWORD_LOGIN", "true").lower() in {"1", "true", "yes"}


def k2auth_logout_enabled() -> bool:
    """«Вийти» also ends K2 Auth's session in this browser (the default)."""
    return os.environ.get("K2_AUTH_LOGOUT", "true").lower() in {"1", "true", "yes"}


class SetupRequired(Exception):
    """Keycloak needs the user to finish account setup (new password, OTP…) on its own page."""


def configuration() -> dict[str, str]:
    if not enabled():
        raise HTTPException(404, "Keycloak authentication is disabled")
    env = lambda name, default="": os.environ.get(name, default).strip()  # noqa: E731
    keycloak_issuer = env("KEYCLOAK_ISSUER").rstrip("/")
    if k2auth():
        issuer = env("K2_AUTH_ISSUER").rstrip("/")
        client = env("K2_AUTH_CLIENT_ID", "analytics")
        secret = env("K2_AUTH_CLIENT_SECRET")
        callback = env("K2_AUTH_REDIRECT_URI") or env("KEYCLOAK_REDIRECT_URI")
        # Accounts are the person's Keycloak identity (same sub), whoever signed them in.
        identity_issuer = keycloak_issuer or issuer
    else:
        issuer, client, secret, callback = keycloak_issuer, env("KEYCLOAK_CLIENT_ID"), env("KEYCLOAK_CLIENT_SECRET"), env("KEYCLOAK_REDIRECT_URI")
        identity_issuer = issuer
    frontend = env("KEYCLOAK_FRONTEND_URL")
    allow_http = os.environ.get("KEYCLOAK_ALLOW_HTTP", "").lower() in {"1", "true"}
    for value in (issuer, callback, frontend, identity_issuer):
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username or parsed.password or parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise HTTPException(503, "Keycloak URLs must be configured with HTTPS")
    if not client or (k2auth() and not secret):
        raise HTTPException(503, "Keycloak client is not configured")
    return {"issuer": issuer, "client_id": client, "redirect_uri": callback,
            "frontend": frontend, "secret": secret,
            "admin_role": env("KEYCLOAK_ADMIN_ROLE"),
            "provider": "k2auth" if k2auth() else "keycloak",
            "identity_issuer": identity_issuer,
            # Client roles are read for this client and, with K2 Auth, for the app's Keycloak client.
            "keycloak_client_id": env("KEYCLOAK_CLIENT_ID")}


_discovered: dict[str, tuple[float, dict]] = {}
DISCOVERY_SECONDS = 3600


def endpoints(config: dict[str, str]) -> dict[str, str]:
    """Keycloak's fixed paths; K2 Auth's from its discovery document (cached for an hour)."""
    issuer = config["issuer"]
    if config.get("provider") != "k2auth":
        return {"authorization_endpoint": issuer + "/protocol/openid-connect/auth",
                "token_endpoint": issuer + "/protocol/openid-connect/token",
                "jwks_uri": issuer + "/protocol/openid-connect/certs",
                "end_session_endpoint": issuer + "/protocol/openid-connect/logout"}
    cached = _discovered.get(issuer)
    if cached is None or cached[0] < time.time():
        with httpx.Client(timeout=15) as client:
            response = client.get(issuer + "/.well-known/openid-configuration")
            response.raise_for_status()
            document = response.json()
        # The issuer and every endpoint must be K2 Auth's own: never follow a document elsewhere.
        if str(document.get("issuer", "")).rstrip("/") != issuer:
            raise ValueError("Discovery issuer does not match")
        origin = urlsplit(issuer)
        found = {}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri", "end_session_endpoint"):
            value = document.get(name)
            if isinstance(value, str) and (urlsplit(value).scheme, urlsplit(value).netloc) == (origin.scheme, origin.netloc):
                found[name] = value
        if not {"authorization_endpoint", "token_endpoint", "jwks_uri"} <= found.keys():
            raise ValueError("Discovery document is incomplete")
        cached = (time.time() + DISCOVERY_SECONDS, found)
        _discovered[issuer] = cached
    return cached[1]


def _token_request(config: dict[str, str], data: dict[str, str]) -> httpx.Response:
    """K2 Auth takes client_secret_basic only; Keycloak keeps client_secret_post as before."""
    with httpx.Client(timeout=15) as client:
        if config.get("provider") == "k2auth":
            # RFC 6749 §2.3.1: id and secret are form-encoded before Basic.
            basic = (quote(config["client_id"], safe=""), quote(config["secret"], safe=""))
            return client.post(endpoints(config)["token_endpoint"], data=data, auth=basic)
        if config["secret"]:
            data = {**data, "client_secret": config["secret"]}
        return client.post(endpoints(config)["token_endpoint"], data={**data, "client_id": config["client_id"]})


def _migrate(connection) -> None:
    from app.core import database
    if database.is_postgres(connection):
        return  # Part of the PostgreSQL schema.
    connection.execute("CREATE TABLE IF NOT EXISTS account_schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version in ("001_keycloak", "002_federated_sessions"):
        if connection.execute("SELECT 1 FROM account_schema_migrations WHERE version=?", (version,)).fetchone():
            continue
        source = Path(__file__).resolve().parents[1] / "migrations" / f"{version}.sql"
        for statement in source.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute("INSERT INTO account_schema_migrations VALUES (?,?)", (version, datetime.now(timezone.utc).isoformat()))


# Keycloak actions a signed-in user may start from the application.
ACTIONS = {"password": "UPDATE_PASSWORD"}


def begin_login(action: str | None = None, login_hint: str | None = None) -> tuple[str, str]:
    """Keycloak's own page also handles what the password form cannot: a temporary
    password, OTP setup, and with an action a voluntary password change."""
    config = configuration()
    if action is not None and (action not in ACTIONS or config["provider"] == "k2auth"):
        raise HTTPException(400, "Unknown Keycloak action")
    authorize = endpoints(config)["authorization_endpoint"]
    state, browser, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with get_user_account_store()._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migrate(connection)
        connection.execute("DELETE FROM oidc_login_states WHERE expires_at<?", (time.time(),))
        connection.execute("DELETE FROM federated_sessions WHERE token_digest NOT IN (SELECT token_digest FROM sessions)")
        connection.execute("INSERT INTO oidc_login_states VALUES (?,?,?,?,?)", (token_digest(state), token_digest(browser), verifier, nonce, time.time() + 300))
    parameters = {"client_id": config["client_id"], "redirect_uri": config["redirect_uri"],
                  "response_type": "code", "scope": "openid profile roles" if config["provider"] == "k2auth" else "openid profile email", "state": state,
                  "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    if action:
        parameters["kc_action"] = ACTIONS[action]
    if login_hint and len(login_hint) <= 255 and login_hint.isprintable():
        parameters["login_hint"] = login_hint
    return authorize + ("&" if urlsplit(authorize).query else "?") + urlencode(parameters), browser


def logout_url() -> str | None:
    """RP-initiated logout ends the Keycloak SSO session, not only the local one."""
    try:
        config = configuration()
        if config["provider"] == "k2auth" and not k2auth_logout_enabled():
            return None
        end_session = endpoints(config).get("end_session_endpoint")
    except (HTTPException, httpx.HTTPError, ValueError):
        return None
    if not end_session:
        return None
    parameters = {"client_id": config["client_id"], "post_logout_redirect_uri": config["frontend"]}
    return end_session + ("&" if urlsplit(end_session).query else "?") + urlencode(parameters)


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
        response = client.get(endpoints(config)["jwks_uri"])
        response.raise_for_status()
        keys = response.json().get("keys", [])
    header = jwt.get_unverified_header(token)
    candidates = [key for key in keys if key.get("kid") == header.get("kid") and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"]
    if len(candidates) != 1:
        raise jwt.InvalidTokenError("Signing key is unavailable")
    return jwt.PyJWK.from_dict(candidates[0], algorithm="RS256")


def token_roles(claims: dict, client_id: str, *more_clients: str) -> set[str]:
    """Realm roles and this client's roles. K2 Auth's ID token carries the realm roles
    as `roles` and Keycloak's client roles in `resource_access`."""
    roles = list(claims.get("realm_access", {}).get("roles", []))
    for client in {client_id, *more_clients} - {""}:
        roles += claims.get("resource_access", {}).get(client, {}).get("roles", [])
    return {str(role) for role in roles}


def k2auth_roles(claims: dict, config: dict[str, str]) -> set[str]:
    if config.get("provider") != "k2auth":
        return set()
    realm = claims.get("roles")
    roles = {str(role) for role in realm if isinstance(role, str)} if isinstance(realm, list) else set()
    return roles | token_roles(claims, config["client_id"], config.get("keycloak_client_id", ""))


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
    issuer = config.get("identity_issuer") or config["issuer"]
    identity = hashlib.sha256((issuer + "\0" + subject).encode()).hexdigest()
    user_id = "kc_" + identity
    username = "kc_" + identity[:24]
    display = str(claims.get("name") or claims.get("preferred_username") or username)[:120]
    roles = {role.casefold() for role in token_roles(claims, config["client_id"]) | k2auth_roles(claims, config) | set(extra_roles)}
    is_admin = bool(config["admin_role"]) and config["admin_role"].casefold() in roles
    role = "administrator" if is_admin else "viewer"
    token = secrets.token_urlsafe(32)
    store = get_user_account_store()
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migrate(connection)
        row = connection.execute("SELECT users.* FROM federated_identities JOIN users ON users.id=federated_identities.user_id WHERE issuer=? AND subject=?", (issuer, subject)).fetchone()
        if row is None:
            created = datetime.now(timezone.utc).isoformat()
            connection.execute("INSERT INTO users (id,username,username_key,display_name,role,active,created_at,password_hash,auth_version) VALUES (?,?,?,?,?,1,?,?,0)", (user_id, username, username, display, role, created, password_hash(secrets.token_urlsafe(48))))
            connection.execute("INSERT INTO federated_identities VALUES (?,?,?)", (issuer, subject, user_id))
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
        if config.get("provider") == "k2auth":
            # K2 Auth's back-channel logout names the login session (sid), or the person.
            sid = claims.get("sid")
            connection.execute("INSERT INTO federated_sessions VALUES (?,?,?,?)", (token_digest(token), config["issuer"], subject, str(sid)[:255] if isinstance(sid, str) else None))
        return token, SessionUser(id=user_id, username=row["username"], display_name=display, role=row["role"])


def complete_login(code: str, state: str, browser: str | None) -> tuple[str, SessionUser]:
    config = configuration()
    verifier, nonce = consume_state(state, browser)
    data = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": config["redirect_uri"], "code_verifier": verifier}
    try:
        response = _token_request(config, data)
        response.raise_for_status()
        answer = response.json()
        token = answer.get("id_token")
        if not isinstance(token, str):
            raise jwt.InvalidTokenError("ID token is unavailable")
        claims = validate_id_token(token, config, nonce)
        # K2 Auth's access token is opaque; its ID token has the roles.
        extra = set() if config["provider"] == "k2auth" else access_token_roles(answer.get("access_token"), config, str(claims["sub"]))
        return establish_session(claims, config, extra)
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Keycloak sign-in could not be verified") from None


def password_login(username: str, password: str, client: str) -> tuple[str, SessionUser]:
    """Sign in with the application's own form: the backend checks the password with Keycloak."""
    config = configuration()
    if not password_login_enabled():
        raise HTTPException(404, "Password sign-in is disabled")
    store = get_user_account_store()
    store._reserve_login_attempt(username, client)
    data = {"grant_type": "password", "username": username,
            "password": password, "scope": "openid profile email"}
    try:
        response = _token_request(config, data)
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


BACKCHANNEL_EVENT = "http://schemas.openid.net/event/backchannel-logout"


def backchannel_logout(logout_token: str) -> int:
    """K2 Auth ended a login (its «Вийти» or the admin's «Завершити всі сеанси»): end the
    application sessions of that login (sid), or every session of the person without one.
    Returns how many sessions ended."""
    config = configuration()
    if config["provider"] != "k2auth":
        raise HTTPException(404, "Back-channel logout is not enabled")
    if not logout_token or len(logout_token) > 16384:
        raise HTTPException(400, "Invalid logout token")
    try:
        claims = jwt.decode(logout_token, _signing_key(logout_token, config).key, algorithms=["RS256"], audience=config["client_id"],
                            issuer=config["issuer"], options={"require": ["iat", "iss", "aud", "jti", "events"]}, leeway=30)
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(400, "Invalid logout token") from None
    events = claims.get("events")
    sid, subject = claims.get("sid"), claims.get("sub")
    if not isinstance(events, dict) or BACKCHANNEL_EVENT not in events or "nonce" in claims or not (isinstance(sid, str) or isinstance(subject, str)):
        raise HTTPException(400, "Invalid logout token")
    with get_user_account_store()._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _migrate(connection)
        if isinstance(sid, str):
            rows = connection.execute("SELECT token_digest FROM federated_sessions WHERE issuer=? AND sid=?", (config["issuer"], sid)).fetchall()
        else:
            rows = connection.execute("SELECT token_digest FROM federated_sessions WHERE issuer=? AND subject=?", (config["issuer"], subject)).fetchall()
        for row in rows:
            connection.execute("DELETE FROM sessions WHERE token_digest=?", (row["token_digest"],))
            connection.execute("DELETE FROM federated_sessions WHERE token_digest=?", (row["token_digest"],))
        return len(rows)
