"""Optional Keycloak OIDC code flow. Tokens stay on the backend."""
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
    connection.execute("CREATE TABLE IF NOT EXISTS account_schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    if connection.execute("SELECT 1 FROM account_schema_migrations WHERE version='001_keycloak'").fetchone():
        return
    source = Path(__file__).resolve().parents[1] / "migrations" / "001_keycloak.sql"
    for statement in source.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute("INSERT INTO account_schema_migrations VALUES (?,?)", ("001_keycloak", datetime.now(timezone.utc).isoformat()))


def begin_login() -> tuple[str, str]:
    config = configuration()
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
    return config["issuer"] + "/protocol/openid-connect/auth?" + urlencode(parameters), browser


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


def validate_id_token(token: str, config: dict[str, str], nonce: str) -> dict:
    # Never select verification algorithms, issuer or JWKS URLs from token claims.
    with httpx.Client(timeout=15) as client:
        response = client.get(config["issuer"] + "/protocol/openid-connect/certs")
        response.raise_for_status()
        keys = response.json().get("keys", [])
    header = jwt.get_unverified_header(token)
    candidates = [key for key in keys if key.get("kid") == header.get("kid") and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"]
    if len(candidates) != 1:
        raise jwt.InvalidTokenError("Signing key is unavailable")
    key = jwt.PyJWK.from_dict(candidates[0], algorithm="RS256")
    claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=config["client_id"],
                        issuer=config["issuer"], options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise jwt.InvalidTokenError("Nonce does not match")
    if claims.get("azp") is not None and claims["azp"] != config["client_id"]:
        raise jwt.InvalidTokenError("Authorized party does not match")
    if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != config["client_id"]:
        raise jwt.InvalidTokenError("Authorized party is required")
    return claims


def establish_session(claims: dict, config: dict[str, str]) -> tuple[str, SessionUser]:
    subject = str(claims["sub"])
    identity = hashlib.sha256((config["issuer"] + "\0" + subject).encode()).hexdigest()
    user_id = "kc_" + identity
    username = "kc_" + identity[:24]
    display = str(claims.get("name") or claims.get("preferred_username") or username)[:120]
    realm_roles = claims.get("realm_access", {}).get("roles", [])
    role = "administrator" if config["admin_role"] and config["admin_role"] in realm_roles else "viewer"
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
            token = response.json().get("id_token")
        if not isinstance(token, str):
            raise jwt.InvalidTokenError("ID token is unavailable")
        return establish_session(validate_id_token(token, config, nonce), config)
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Keycloak sign-in could not be verified") from None
