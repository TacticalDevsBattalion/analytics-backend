"""Account identities, opaque sessions and isolated personal dashboard preferences."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, Iterator, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError, field_validator

from app.core.config import get_settings

PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60
SESSION_COOKIE = "analytics_session"
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_ACCOUNT_LIMIT = 10
LOGIN_CLIENT_LIMIT = 50
Role = Literal["administrator", "viewer"]
MetricKey = Literal["flights", "effective", "detected", "affected", "destroyed", "efficiency", "avg_flights_per_position", "weighted_efficiency"]
PersonalBlock = Literal["timeline", "map", "events", "departments", "purposes", "lost_devices", "timeline_line", "timeline_bar", "timeline_area", "category_chart", "purpose_chart"]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class AccountModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def normalize_username(value: str) -> str:
    return value.strip().lower()


def validate_username(value: str) -> str:
    username = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}", username):
        raise ValueError("Username must contain 3–64 letters, digits, periods, underscores or hyphens")
    return username


def validate_password(value: SecretStr) -> SecretStr:
    if not 12 <= len(value.get_secret_value()) <= 128:
        raise ValueError("Password must contain 12–128 characters")
    try:
        value.get_secret_value().encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Password must contain valid text") from None
    return value


class ChartStyles(AccountModel):
    timeline: Literal["combined", "bars", "line", "area"]
    category: Literal["bars", "donut"]
    purpose: Literal["bars", "donut"]


class PersonalDashboardPreferences(AccountModel):
    version: Literal[1]
    metric_keys: list[MetricKey] | None = Field(max_length=8)
    blocks: list[PersonalBlock] | None = Field(max_length=11)
    background: Literal["default", "navy", "graphite", "forest"]
    density: Literal["compact", "comfortable"] | None
    chart_styles: ChartStyles

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Only preference version 1 is supported")
        return value

    @field_validator("metric_keys", "blocks")
    @classmethod
    def unique_keys(cls, values):
        if values is not None and len(values) != len(set(values)):
            raise ValueError("Dashboard selections must be unique")
        return values


class SessionUser(AccountModel):
    id: str
    username: str
    display_name: str
    role: Role


class PublicUser(SessionUser):
    active: bool
    created_at: str


class SessionResponse(AccountModel):
    enabled: bool
    user: SessionUser | None


class DashboardResponse(AccountModel):
    revision: int
    preferences: PersonalDashboardPreferences | None


class DashboardUpdate(AccountModel):
    expected_revision: int = Field(ge=0)
    preferences: PersonalDashboardPreferences | None


class LoginRequest(AccountModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr

    _validate_password = field_validator("password")(validate_password)


class CreateUserRequest(AccountModel):
    username: str
    display_name: DisplayName
    password: SecretStr
    role: Role

    _validate_username = field_validator("username")(validate_username)
    _validate_password = field_validator("password")(validate_password)


class UpdateUserRequest(AccountModel):
    display_name: DisplayName | None = None
    role: Role | None = None
    active: bool | None = None
    password: SecretStr | None = None

    @field_validator("password")
    @classmethod
    def valid_optional_password(cls, value):
        return validate_password(value) if value is not None else None


class AccountConflict(ValueError):
    pass


class AccountNotFound(ValueError):
    pass


class AuthenticationFailed(ValueError):
    pass


class LoginRateLimited(ValueError):
    pass


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, expected = stored.split("$")
        iterations = int(rounds)
        if algorithm != "pbkdf2_sha256" or not PASSWORD_ITERATIONS <= iterations <= 2_000_000:
            return False
        salt_bytes = bytes.fromhex(salt)
        expected_bytes = bytes.fromhex(expected)
        if len(salt_bytes) != 32 or len(expected_bytes) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_DUMMY_HASH = password_hash(secrets.token_urlsafe(32))


class UserAccountStore:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self._initialization_lock = threading.Lock()
        self._initialized = False

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection(initialize=False) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE IF NOT EXISTS account_state (id INTEGER PRIMARY KEY CHECK(id=1), ever_enabled INTEGER NOT NULL CHECK(ever_enabled IN (0,1)))")
                connection.execute("INSERT OR IGNORE INTO account_state VALUES (1,0)")
                connection.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('administrator','viewer')), active INTEGER NOT NULL CHECK(active IN (0,1)), created_at TEXT NOT NULL, password_hash TEXT NOT NULL, auth_version INTEGER NOT NULL DEFAULT 0)")
                connection.execute("CREATE TABLE IF NOT EXISTS sessions (token_digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), auth_version INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL)")
                connection.execute("CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)")
                connection.execute("CREATE TABLE IF NOT EXISTS dashboard_preferences (user_id TEXT PRIMARY KEY REFERENCES users(id), revision INTEGER NOT NULL DEFAULT 0, preferences_json TEXT)")
                connection.execute("CREATE TABLE IF NOT EXISTS login_attempts (scope TEXT PRIMARY KEY, window_started REAL NOT NULL, attempts INTEGER NOT NULL)")
                connection.execute("UPDATE account_state SET ever_enabled=1 WHERE EXISTS (SELECT 1 FROM users)")
            self._initialized = True

    @contextmanager
    def _connection(self, initialize=True) -> Iterator[sqlite3.Connection]:
        if initialize:
            self._initialize()
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            if connection.in_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _public_user(row: sqlite3.Row) -> PublicUser:
        return PublicUser(id=row["id"], username=row["username"], display_name=row["display_name"], role=row["role"], active=bool(row["active"]), created_at=row["created_at"])

    def enabled(self) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT ever_enabled FROM account_state WHERE id=1").fetchone()
            if row is None:
                raise sqlite3.DatabaseError("Account activation state is unavailable")
            return bool(row["ever_enabled"])

    def users(self) -> list[PublicUser]:
        with self._connection() as connection:
            return [self._public_user(row) for row in connection.execute("SELECT * FROM users ORDER BY created_at, username_key")]

    def create_user(self, request: CreateUserRequest) -> PublicUser:
        request = CreateUserRequest.model_validate(request.model_dump())
        hashed = password_hash(request.password.get_secret_value())
        user_id = secrets.token_hex(16)
        created_at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() and request.role != "administrator":
                raise AccountConflict("The first account must be an administrator")
            if connection.execute("SELECT 1 FROM users WHERE username_key=?", (normalize_username(request.username),)).fetchone():
                raise AccountConflict("This username is already in use")
            connection.execute("INSERT INTO users VALUES (?,?,?,?,?,1,?,?,0)", (user_id, request.username, normalize_username(request.username), request.display_name, request.role, created_at, hashed))
            connection.execute("INSERT INTO dashboard_preferences VALUES (?,0,NULL)", (user_id,))
            connection.execute("UPDATE account_state SET ever_enabled=1 WHERE id=1")
            return self._public_user(connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def update_user(self, user_id: str, request: UpdateUserRequest) -> PublicUser:
        request = UpdateUserRequest.model_validate(request.model_dump(exclude_unset=True))
        supplied = request.model_fields_set
        if not supplied or any(getattr(request, field) is None for field in supplied):
            raise AccountConflict("Provide at least one non-null account field")
        hashed = password_hash(request.password.get_secret_value()) if request.password is not None else None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise AccountNotFound("Account was not found")
            next_role = request.role if "role" in supplied else row["role"]
            next_active = request.active if "active" in supplied else bool(row["active"])
            if row["role"] == "administrator" and row["active"] and (next_role != "administrator" or not next_active):
                count = connection.execute("SELECT count(*) FROM users WHERE role='administrator' AND active=1").fetchone()[0]
                if count <= 1:
                    raise AccountConflict("The last active administrator cannot be disabled or demoted")
            revoke = hashed is not None or next_role != row["role"] or not next_active
            connection.execute("UPDATE users SET display_name=?,role=?,active=?,password_hash=?,auth_version=? WHERE id=?", (request.display_name if "display_name" in supplied else row["display_name"], next_role, int(next_active), hashed or row["password_hash"], row["auth_version"] + int(revoke), user_id))
            if revoke:
                connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return self._public_user(connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def _reserve_login_attempt(self, username: str, client: str) -> None:
        now = self.clock()
        scopes = [(token_digest("account:" + normalize_username(username)), LOGIN_ACCOUNT_LIMIT), (token_digest("client:" + client), LOGIN_CLIENT_LIMIT)]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM login_attempts WHERE window_started<=?", (now - LOGIN_WINDOW_SECONDS,))
            for scope, limit in scopes:
                row = connection.execute("SELECT attempts FROM login_attempts WHERE scope=?", (scope,)).fetchone()
                if row is not None and row["attempts"] >= limit:
                    raise LoginRateLimited("Sign-in is temporarily unavailable; try again later")
            for scope, _ in scopes:
                connection.execute("INSERT INTO login_attempts VALUES (?,?,1) ON CONFLICT(scope) DO UPDATE SET attempts=attempts+1", (scope, now))

    def login(self, username: str, password: str, client: str) -> tuple[str, SessionUser]:
        self._reserve_login_attempt(username, client)
        with self._connection() as connection:
            candidate = connection.execute("SELECT * FROM users WHERE username_key=?", (normalize_username(username),)).fetchone()
        stored = candidate["password_hash"] if candidate is not None else _DUMMY_HASH
        valid = verify_password(password, stored)
        if not valid or candidate is None or not candidate["active"]:
            raise AuthenticationFailed("Username or password is incorrect")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM users WHERE id=?", (candidate["id"],)).fetchone()
            if current is None or not current["active"] or current["auth_version"] != candidate["auth_version"] or current["password_hash"] != stored:
                raise AuthenticationFailed("Username or password is incorrect")
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (token_digest(token), current["id"], current["auth_version"], now, now + SESSION_SECONDS))
            connection.execute("DELETE FROM login_attempts WHERE scope=?", (token_digest("account:" + normalize_username(username)),))
            return token, SessionUser(id=current["id"], username=current["username"], display_name=current["display_name"], role=current["role"])

    def session_user(self, token: str | None) -> SessionUser | None:
        if not token or len(token) > 128:
            return None
        with self._connection() as connection:
            row = connection.execute("SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token_digest=? AND sessions.expires_at>? AND users.active=1 AND users.auth_version=sessions.auth_version", (token_digest(token), self.clock())).fetchone()
            return SessionUser(id=row["id"], username=row["username"], display_name=row["display_name"], role=row["role"]) if row is not None else None

    def logout(self, token: str | None) -> None:
        if token and len(token) <= 128:
            with self._connection() as connection:
                connection.execute("DELETE FROM sessions WHERE token_digest=?", (token_digest(token),))

    @staticmethod
    def _dashboard(row: sqlite3.Row | None) -> DashboardResponse:
        if row is None:
            return DashboardResponse(revision=0, preferences=None)
        preferences = PersonalDashboardPreferences.model_validate_json(row["preferences_json"]) if row["preferences_json"] is not None else None
        return DashboardResponse(revision=row["revision"], preferences=preferences)

    def dashboard(self, user_id: str) -> DashboardResponse:
        with self._connection() as connection:
            if not connection.execute("SELECT 1 FROM users WHERE id=? AND active=1", (user_id,)).fetchone():
                raise AuthenticationFailed("Sign-in is required")
            return self._dashboard(connection.execute("SELECT * FROM dashboard_preferences WHERE user_id=?", (user_id,)).fetchone())

    def save_dashboard(self, user_id: str, request: DashboardUpdate) -> DashboardResponse:
        request = DashboardUpdate.model_validate(request.model_dump())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM users WHERE id=? AND active=1", (user_id,)).fetchone():
                raise AuthenticationFailed("Sign-in is required")
            row = connection.execute("SELECT * FROM dashboard_preferences WHERE user_id=?", (user_id,)).fetchone()
            revision = row["revision"] if row is not None else 0
            if revision != request.expected_revision:
                raise AccountConflict("Personal dashboard settings changed elsewhere; reload before saving")
            encoded = request.preferences.model_dump_json() if request.preferences is not None else None
            connection.execute("INSERT INTO dashboard_preferences VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,preferences_json=excluded.preferences_json", (user_id, revision + 1, encoded))
            return DashboardResponse(revision=revision + 1, preferences=request.preferences)


_stores: dict[str, UserAccountStore] = {}
_stores_lock = threading.Lock()


def get_user_account_store() -> UserAccountStore:
    path = str(Path(os.environ.get("APP_USERS_DB", "/app/data/app_users.sqlite3")).expanduser().resolve())
    with _stores_lock:
        if path not in _stores:
            _stores[path] = UserAccountStore(path)
        return _stores[path]


def account_operation(operation):
    try:
        return operation()
    except AccountConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers={"Cache-Control": "no-store"}) from exc
    except AccountNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc), headers={"Cache-Control": "no-store"}) from exc
    except AuthenticationFailed as exc:
        raise HTTPException(status_code=401, detail=str(exc), headers={"Cache-Control": "no-store"}) from exc
    except LoginRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Cache-Control": "no-store", "Retry-After": str(LOGIN_WINDOW_SECONDS)}) from exc
    except (sqlite3.Error, OSError, ValidationError):
        # Authentication fails closed; never return database diagnostics or input values.
        raise HTTPException(status_code=503, detail="Account storage is temporarily unavailable", headers={"Cache-Control": "no-store"}) from None


def legacy_administrator(request: Request) -> bool:
    expected = os.environ.get("APP_ADMIN_TOKEN", "").strip()
    supplied = request.headers.get("x-app-admin-token", "")
    return bool(expected) and secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def session_identity(request: Request) -> SessionUser | None:
    return account_operation(lambda: get_user_account_store().session_user(request.cookies.get(SESSION_COOKIE)))


def _origin(value: str, allow_path: bool = False) -> str | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        if value != value.strip() or any(ord(char) < 32 for char in value):
            return None
        if not allow_path and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"
    except ValueError:
        return None


def check_request_origin(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    # Browser requests send Origin; Referer covers older same-origin clients. API
    # callers carrying the explicit administrator header do not depend on cookies.
    origin_value = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin_value is None and referer is None:
        if request.cookies.get(SESSION_COOKIE) or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            raise HTTPException(status_code=403, detail="Request origin is not allowed", headers={"Cache-Control": "no-store"})
        return
    candidate = _origin(origin_value) if origin_value is not None else _origin(referer or "", allow_path=True)
    allowed = {_origin(str(request.base_url))}
    allowed.update(_origin(value) for value in get_settings().cors_origins)
    allowed.discard(None)
    if candidate is None or candidate not in allowed:
        raise HTTPException(status_code=403, detail="Request origin is not allowed", headers={"Cache-Control": "no-store"})


def secure_session_cookie(request: Request) -> bool:
    # Never allow an explicit false setting to weaken an HTTPS session cookie.
    configured = os.environ.get("APP_SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}
    return configured or request.url.scheme == "https"


def require_signed_user(request: Request) -> SessionUser:
    user = session_identity(request)
    expected_user = request.headers.get("x-analytics-user-id")
    # This header only guards against a different tab changing the shared cookie.
    # It never chooses the user whose data is read or written.
    if user is None or (expected_user is not None and expected_user != user.id):
        raise HTTPException(status_code=401, detail="Sign-in is required", headers={"Cache-Control": "no-store"})
    check_request_origin(request)
    return user


def require_analytics_access(request: Request) -> None:
    if request.scope.get("endpoint", None) is not None and request.scope["endpoint"].__name__ == "health":
        return
    from app.core.keycloak import enabled as keycloak_enabled
    if not keycloak_enabled() and not account_operation(lambda: get_user_account_store().enabled()):
        return
    if legacy_administrator(request):
        return
    require_signed_user(request)


def require_account_administrator(request: Request) -> SessionUser | None:
    if legacy_administrator(request):
        return None
    user = session_identity(request)
    if user is not None and user.role == "administrator":
        check_request_origin(request)
        return user
    enabled = account_operation(lambda: get_user_account_store().enabled())
    if not enabled and not os.environ.get("APP_ADMIN_TOKEN", "").strip():
        raise HTTPException(status_code=404, detail="Application administration is disabled", headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=403, detail="Administrator authentication is required", headers={"Cache-Control": "no-store"})
