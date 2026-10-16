"""Keycloak OIDC security tests with signed JWTs and isolated local accounts."""
import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.keycloak_routes import router
from app.core import keycloak
from app.core.user_accounts import (
    SESSION_COOKIE, AuthenticationFailed, CreateUserRequest, UpdateUserRequest,
    UserAccountStore,
)


class KeycloakFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key()))
        cls.jwk.update(kid="test-key", use="sig", alg="RS256")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "users.sqlite3"
        self.store = UserAccountStore(self.path)
        self.local_admin = self.store.create_user(CreateUserRequest(username="local-admin", display_name="Local admin", password="local-admin-password-42", role="administrator"))
        environment = patch.dict(os.environ, {"KEYCLOAK_ENABLED": "true", "KEYCLOAK_ISSUER": "https://identity.example/realms/analytics", "KEYCLOAK_CLIENT_ID": "analytics-ui", "KEYCLOAK_REDIRECT_URI": "https://analytics.example/api/auth/keycloak/callback", "KEYCLOAK_FRONTEND_URL": "https://analytics.example/", "KEYCLOAK_CLIENT_SECRET": "test-confidential-secret", "KEYCLOAK_ADMIN_ROLE": "analytics-admin", "KEYCLOAK_ALLOW_HTTP": "", "APP_USERS_DB": str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)
        store = patch.object(keycloak, "get_user_account_store", return_value=self.store)
        store.start()
        self.addCleanup(store.stop)
        self.config = keycloak.configuration()
        self.http = MagicMock()
        self.http.get.return_value.json.return_value = {"keys": [self.jwk]}
        client = patch.object(keycloak.httpx, "Client")
        mocked = client.start()
        mocked.return_value.__enter__.return_value = self.http
        self.addCleanup(client.stop)

    def claims(self, **updates):
        now = int(time.time())
        return {"iss": self.config["issuer"], "aud": self.config["client_id"], "sub": "federated-person-1", "iat": now, "exp": now + 300, "nonce": "expected-nonce", "name": "Federated person", "realm_access": {"roles": []}, **updates}

    def signed(self, claims=None, **updates):
        return jwt.encode(claims or self.claims(**updates), self.private_key, algorithm="RS256", headers={"kid": "test-key"})

    def begin(self):
        url, browser = keycloak.begin_login()
        query = parse_qs(urlsplit(url).query)
        return url, query, browser

class KeycloakTests(KeycloakFixture):
    def test_authorization_code_uses_pkce_browser_bound_state_and_nonce(self):
        url, query, browser = self.begin()
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["response_type"], ["code"])
        verifier, nonce = keycloak.consume_state(query["state"][0], browser)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(query["nonce"], [nonce])
        self.assertNotIn(verifier, url)
        self.assertNotIn(browser, url)
        with self.assertRaises(HTTPException):
            keycloak.consume_state(query["state"][0], browser)

    def test_wrong_browser_expiry_and_replayed_state_cannot_create_sessions(self):
        _, query, browser = self.begin()
        with self.assertRaises(HTTPException):
            keycloak.consume_state(query["state"][0], "another-browser")
        # A wrong browser does not invalidate a legitimate in-progress login.
        keycloak.consume_state(query["state"][0], browser)
        _, query, browser = self.begin()
        with patch.object(keycloak.time, "time", return_value=time.time() + 600):
            with self.assertRaises(HTTPException):
                keycloak.consume_state(query["state"][0], browser)

    def test_jwt_verifies_signature_issuer_audience_nonce_and_fixed_algorithm(self):
        valid = keycloak.validate_id_token(self.signed(), self.config, "expected-nonce")
        self.assertEqual(valid["sub"], "federated-person-1")
        tokens = [
            self.signed(nonce="wrong-nonce"),
            self.signed(aud="another-client"),
            self.signed(iss="https://attacker.example/realms/analytics"),
            self.signed(exp=int(time.time()) - 10),
            self.signed(iat=int(time.time()) + 3600),
            self.signed(azp="another-client"),
            self.signed(aud=[self.config["client_id"], "other-client"]),
            jwt.encode(self.claims(), self.other_key, algorithm="RS256", headers={"kid": "test-key"}),
            jwt.encode(self.claims(), "attacker-controlled-secret-with-32-bytes", algorithm="HS256", headers={"kid": "test-key"}),
            jwt.encode(self.claims(), "", algorithm="none", headers={"kid": "test-key"}),
        ]
        for token in tokens:
            with self.subTest(token_header=jwt.get_unverified_header(token)):
                with self.assertRaises(jwt.PyJWTError):
                    keycloak.validate_id_token(token, self.config, "expected-nonce")
        for field in ("exp", "iat", "iss", "aud", "sub", "nonce"):
            claims = self.claims()
            claims.pop(field)
            with self.subTest(missing=field), self.assertRaises(jwt.PyJWTError):
                keycloak.validate_id_token(self.signed(claims), self.config, "expected-nonce")

    def test_unrecognized_or_ambiguous_jwks_key_is_rejected(self):
        self.http.get.return_value.json.return_value = {"keys": [{**self.jwk, "kid": "different-key"}]}
        with self.assertRaises(jwt.PyJWTError):
            keycloak.validate_id_token(self.signed(), self.config, "expected-nonce")
        self.http.get.return_value.json.return_value = {"keys": [self.jwk, self.jwk]}
        with self.assertRaises(jwt.PyJWTError):
            keycloak.validate_id_token(self.signed(), self.config, "expected-nonce")

    def test_successful_code_flow_consumes_state_and_registers_existing_auth_session(self):
        _, query, browser = self.begin()
        self.http.post.return_value.json.return_value = {"id_token": self.signed(nonce=query["nonce"][0])}
        token, user = keycloak.complete_login("one-use-code", query["state"][0], browser)
        self.assertTrue(user.id.startswith("kc_"))
        self.assertEqual(user.role, "viewer")
        self.assertEqual(self.store.session_user(token).id, user.id)
        posted = self.http.post.call_args.kwargs["data"]
        self.assertEqual(posted["code"], "one-use-code")
        self.assertEqual(posted["redirect_uri"], self.config["redirect_uri"])
        self.assertNotEqual(posted["code_verifier"], query["code_challenge"][0])
        with self.assertRaises(HTTPException):
            keycloak.complete_login("one-use-code", query["state"][0], browser)
        self.assertEqual(self.http.post.call_count, 1)

    def test_wrong_signed_nonce_never_registers_an_account(self):
        _, query, browser = self.begin()
        self.http.post.return_value.json.return_value = {"id_token": self.signed(nonce="another-nonce")}
        with self.assertRaises(HTTPException) as failed:
            keycloak.complete_login("code", query["state"][0], browser)
        self.assertEqual(failed.exception.status_code, 401)
        self.assertEqual(len(self.store.users()), 1)

    def test_identity_link_preserves_application_roles_and_local_suspension(self):
        first, user = keycloak.establish_session(self.claims(), self.config)
        promoted = self.store.update_user(user.id, UpdateUserRequest(role="administrator"))
        self.assertIsNone(self.store.session_user(first))
        second, returned = keycloak.establish_session(self.claims(name="Renamed person"), self.config)
        self.assertEqual(returned.id, user.id)
        self.assertEqual(returned.role, "administrator")
        self.assertEqual(returned.display_name, "Renamed person")
        self.assertEqual(len(self.store.users()), 2)
        self.store.update_user(user.id, UpdateUserRequest(active=False))
        self.assertIsNone(self.store.session_user(second))
        with self.assertRaises(AuthenticationFailed):
            keycloak.establish_session(self.claims(realm_access={"roles": ["analytics-admin"]}), self.config)

    def test_configured_role_promotes_on_every_sign_in_but_never_demotes(self):
        # An account that signed in before the role was assigned becomes an administrator.
        _, user = keycloak.establish_session(self.claims(sub="new-admin"), self.config)
        self.assertEqual(user.role, "viewer")
        _, promoted = keycloak.establish_session(self.claims(sub="new-admin", realm_access={"roles": ["analytics-admin"]}), self.config)
        self.assertEqual(promoted.role, "administrator")
        # Losing the Keycloak role does not remove application rights; administrators manage them.
        _, kept = keycloak.establish_session(self.claims(sub="new-admin"), self.config)
        self.assertEqual(kept.role, "administrator")

    def test_role_is_found_in_client_roles_access_token_and_ignores_case(self):
        _, client_role = keycloak.establish_session(self.claims(sub="client-role", resource_access={"analytics-ui": {"roles": ["Analytics-Admin"]}}), self.config)
        self.assertEqual(client_role.role, "administrator")
        _, other_client = keycloak.establish_session(self.claims(sub="other-client", resource_access={"another-app": {"roles": ["analytics-admin"]}}), self.config)
        self.assertEqual(other_client.role, "viewer")
        now = int(time.time())
        access = jwt.encode({"iss": self.config["issuer"], "sub": "from-access", "azp": "analytics-ui", "aud": "account", "iat": now, "exp": now + 300, "realm_access": {"roles": ["analytics-admin"]}}, self.private_key, algorithm="RS256", headers={"kid": "test-key"})
        self.assertEqual(keycloak.access_token_roles(access, self.config, "from-access"), {"analytics-admin"})
        # Another subject's or another client's token, or a foreign signature, grants nothing.
        self.assertEqual(keycloak.access_token_roles(access, self.config, "someone-else"), set())
        forged = jwt.encode({"iss": self.config["issuer"], "sub": "from-access", "azp": "analytics-ui", "exp": now + 300, "realm_access": {"roles": ["analytics-admin"]}}, self.other_key, algorithm="RS256", headers={"kid": "test-key"})
        self.assertEqual(keycloak.access_token_roles(forged, self.config, "from-access"), set())
        foreign = jwt.encode({"iss": self.config["issuer"], "sub": "from-access", "azp": "another-app", "exp": now + 300, "realm_access": {"roles": ["analytics-admin"]}}, self.private_key, algorithm="RS256", headers={"kid": "test-key"})
        self.assertEqual(keycloak.access_token_roles(foreign, self.config, "from-access"), set())

    def test_issuer_namespaces_federated_identity_and_cannot_link_by_display_name(self):
        _, a = keycloak.establish_session(self.claims(name="Same name"), self.config)
        _, b = keycloak.establish_session(self.claims(name="Same name"), {**self.config, "issuer": "https://other.example/realms/analytics"})
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(len(self.store.users()), 3)

    def test_http_requires_explicit_development_setting_and_provider_route_hides_secrets(self):
        with patch.dict(os.environ, {"KEYCLOAK_ISSUER": "http://identity.example/realms/analytics"}):
            with self.assertRaises(HTTPException):
                keycloak.configuration()
        app = FastAPI()
        app.include_router(router, prefix="/api")
        with TestClient(app) as client:
            provider = client.get("/api/auth/provider")
            self.assertEqual(provider.json(), {"provider": "keycloak", "login_url": "/api/auth/keycloak/login", "password_login": True})
            self.assertNotIn("test-confidential-secret", provider.text)
            started = client.get("/api/auth/keycloak/login", follow_redirects=False)
            self.assertEqual(started.status_code, 302)
            cookie = started.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=lax", cookie)
            self.assertNotIn("code_verifier", started.headers["location"])

    def test_callback_sets_only_opaque_session_cookie_and_replay_fails(self):
        app = FastAPI()
        app.include_router(router, prefix="/api")
        with TestClient(app, base_url="https://analytics.example") as client:
            started = client.get("/api/auth/keycloak/login", follow_redirects=False)
            query = parse_qs(urlsplit(started.headers["location"]).query)
            signed = self.signed(nonce=query["nonce"][0])
            self.http.post.return_value.json.return_value = {"id_token": signed}
            callback = client.get("/api/auth/keycloak/callback", params={"code": "code", "state": query["state"][0]}, follow_redirects=False)
            self.assertEqual(callback.status_code, 303, callback.text)
            self.assertEqual(callback.headers["location"], self.config["frontend"])
            cookies = callback.headers.get_list("set-cookie")
            session = next(value for value in cookies if value.startswith(SESSION_COOKIE + "="))
            self.assertIn("HttpOnly", session)
            self.assertIn("Secure", session)
            self.assertIn("SameSite=strict", session)
            self.assertNotIn(signed, session)
            opaque = client.cookies.get(SESSION_COOKIE)
            self.assertIsNotNone(self.store.session_user(opaque))
            replayed = client.get("/api/auth/keycloak/callback", params={"code": "code", "state": query["state"][0]}, follow_redirects=False)
            self.assertEqual(replayed.status_code, 303)
            self.assertEqual(replayed.headers["location"], self.config["frontend"] + "?auth_error=failed")

    def test_cancelled_or_incomplete_callback_returns_to_sign_in_page(self):
        app = FastAPI()
        app.include_router(router, prefix="/api")
        with TestClient(app, base_url="https://analytics.example") as client:
            started = client.get("/api/auth/keycloak/login", follow_redirects=False)
            state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
            cancelled = client.get("/api/auth/keycloak/callback", params={"error": "access_denied", "state": state}, follow_redirects=False)
            self.assertEqual(cancelled.status_code, 303)
            self.assertEqual(cancelled.headers["location"], self.config["frontend"] + "?auth_error=cancelled")
            self.assertIsNone(client.cookies.get(SESSION_COOKIE))
            missing = client.get("/api/auth/keycloak/callback", follow_redirects=False)
            self.assertEqual(missing.headers["location"], self.config["frontend"] + "?auth_error=failed")
        self.http.post.assert_not_called()

    def test_logout_ends_keycloak_session_through_rp_initiated_logout(self):
        # Only with the redirect sign-in; the own form never opens a Keycloak browser session.
        self.enterContext(patch.dict(os.environ, {"KEYCLOAK_PASSWORD_LOGIN": "false"}))
        from app.api.account_routes import router as account_router
        app = FastAPI()
        app.include_router(account_router, prefix="/api")
        with patch("app.api.account_routes.get_user_account_store", return_value=self.store), patch("app.core.user_accounts.get_user_account_store", return_value=self.store), TestClient(app, base_url="https://analytics.example") as client:
            response = client.post("/api/auth/logout", headers={"Origin": "https://analytics.example"})
            self.assertEqual(response.status_code, 200, response.text)
            url = urlsplit(response.json()["logout_url"])
            self.assertEqual(url.scheme + "://" + url.netloc + url.path, self.config["issuer"] + "/protocol/openid-connect/logout")
            self.assertEqual(parse_qs(url.query), {"client_id": ["analytics-ui"], "post_logout_redirect_uri": [self.config["frontend"]]})
            self.assertNotIn("test-confidential-secret", response.text)



class KeycloakPasswordLoginTests(KeycloakFixture):
    """The application's own sign-in form; Keycloak checks the password."""

    def answer(self, status, body):
        response = MagicMock(status_code=status)
        response.json.return_value = body
        if status >= 400:
            response.raise_for_status.side_effect = keycloak.httpx.HTTPStatusError("error", request=MagicMock(), response=response)
        self.http.post.return_value = response

    def test_correct_password_creates_a_session_without_a_nonce(self):
        claims = self.claims()
        del claims["nonce"]
        now = int(time.time())
        access = jwt.encode({"iss": self.config["issuer"], "sub": claims["sub"], "azp": "analytics-ui", "exp": now + 300, "realm_access": {"roles": ["analytics-admin"]}}, self.private_key, algorithm="RS256", headers={"kid": "test-key"})
        self.answer(200, {"id_token": self.signed(claims), "access_token": access})
        token, user = keycloak.password_login("person", "short", "10.0.0.1")
        self.assertEqual(self.store.session_user(token).id, user.id)
        # The administrator role came only in the access token.
        self.assertEqual(user.role, "administrator")
        posted = self.http.post.call_args.kwargs["data"]
        self.assertEqual((posted["grant_type"], posted["username"], posted["client_secret"]), ("password", "person", "test-confidential-secret"))

    def test_token_signed_by_another_key_is_rejected(self):
        self.answer(200, {"id_token": jwt.encode(self.claims(), self.other_key, algorithm="RS256", headers={"kid": "test-key"})})
        with self.assertRaises(HTTPException) as raised:
            keycloak.password_login("person", "secret", "10.0.0.1")
        self.assertEqual(raised.exception.status_code, 502)

    def test_wrong_password_setup_and_disabled_grant_are_distinguished(self):
        self.answer(401, {"error": "invalid_grant", "error_description": "Invalid user credentials"})
        with self.assertRaises(AuthenticationFailed):
            keycloak.password_login("person", "wrong", "10.0.0.1")
        self.answer(400, {"error": "invalid_grant", "error_description": "Account is not fully set up"})
        with self.assertRaises(keycloak.SetupRequired):
            keycloak.password_login("person", "secret", "10.0.0.1")
        self.answer(400, {"error": "unauthorized_client", "error_description": "Client not allowed for direct access grants"})
        with self.assertRaises(HTTPException) as raised:
            keycloak.password_login("person", "secret", "10.0.0.1")
        self.assertEqual(raised.exception.status_code, 503)

    def test_attempts_are_rate_limited_before_reaching_keycloak(self):
        from app.core.user_accounts import LoginRateLimited
        self.answer(401, {"error": "invalid_grant"})
        with self.assertRaises(LoginRateLimited):
            for _ in range(100):
                try:
                    keycloak.password_login("person", "wrong", "10.0.0.1")
                except AuthenticationFailed:
                    pass
        calls = self.http.post.call_count
        with self.assertRaises(LoginRateLimited):
            keycloak.password_login("person", "right", "10.0.0.1")
        self.assertEqual(self.http.post.call_count, calls)

    def test_can_be_switched_off(self):
        with patch.dict(os.environ, {"KEYCLOAK_PASSWORD_LOGIN": "false"}):
            with self.assertRaises(HTTPException) as raised:
                keycloak.password_login("person", "secret", "10.0.0.1")
        self.assertEqual(raised.exception.status_code, 404)
        self.http.post.assert_not_called()

    def client(self):
        from app.api.account_routes import router as account_router
        app = FastAPI()
        app.include_router(account_router, prefix="/api")
        self.enterContext(patch("app.api.account_routes.get_user_account_store", return_value=self.store))
        self.enterContext(patch("app.core.user_accounts.get_user_account_store", return_value=self.store))
        return self.enterContext(TestClient(app, base_url="https://analytics.example"))

    def test_route_sets_the_session_cookie_and_logout_stays_in_the_application(self):
        claims = self.claims()
        del claims["nonce"]
        self.answer(200, {"id_token": self.signed(claims)})
        client = self.client()
        origin = {"Origin": "https://analytics.example"}
        response = client.post("/api/auth/keycloak/password", json={"username": "person@example", "password": "short"}, headers=origin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(SESSION_COOKIE, response.headers["set-cookie"])
        self.assertEqual(response.json()["user"]["display_name"], "Federated person")
        self.assertIsNone(client.post("/api/auth/logout", headers=origin).json()["logout_url"])

    def test_route_errors_never_echo_the_password(self):
        client = self.client()
        origin = {"Origin": "https://analytics.example"}
        self.answer(401, {"error": "invalid_grant", "error_description": "Invalid user credentials"})
        wrong = client.post("/api/auth/keycloak/password", json={"username": "person", "password": "secret-value-1"}, headers=origin)
        self.assertEqual(wrong.status_code, 401)
        invalid = client.post("/api/auth/keycloak/password", json={"username": "", "password": "secret-value-2"}, headers=origin)
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn("secret-value", wrong.text + invalid.text)
        self.answer(400, {"error": "invalid_grant", "error_description": "Account is not fully set up"})
        self.assertEqual(client.post("/api/auth/keycloak/password", json={"username": "new", "password": "x"}, headers=origin).status_code, 409)
        self.assertEqual(client.post("/api/auth/keycloak/password", json={"username": "person", "password": "x"}, headers={"Origin": "https://evil.example"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
