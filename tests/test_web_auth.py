import base64
import hashlib
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webui import access_control, platform_auth  # noqa: E402
from webui.log_filters import RedactRequestQueryFilter  # noqa: E402


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("request failed")


class _FakeOIDCHTTP:
    def __init__(self, issuer, jwk, private_key):
        self.issuer = issuer
        self.jwk = jwk
        self.private_key = private_key
        self.claim_overrides = {}
        self.expected_nonce = ""
        self.last_token_data = None
        self.fail_token_exchange = False

    def get(self, url, **_kwargs):
        if url.endswith("/.well-known/openid-configuration"):
            return _Response(
                {
                    "issuer": self.issuer,
                    "authorization_endpoint": self.issuer + "/authorize",
                    "token_endpoint": self.issuer + "/token",
                    "jwks_uri": self.issuer + "/jwks",
                    "end_session_endpoint": self.issuer + "/logout",
                }
            )
        if url.endswith("/jwks"):
            return _Response({"keys": [self.jwk]})
        return _Response({}, 404)

    def post(self, url, *, data, auth, **_kwargs):
        self.last_token_data = data
        if self.fail_token_exchange:
            return _Response({}, 400)
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "sub": "subject-1",
            "aud": "shadow-stock",
            "iat": now,
            "exp": now + 300,
            "nonce": self.expected_nonce,
            "preferred_username": "example-user",
            "name": "Example User",
            "groups": ["stock-users"],
        }
        claims.update(self.claim_overrides)
        token = jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "k1"})
        return _Response({"id_token": token, "access_token": "discard-me"})


def _jwk_for(private_key):
    raw = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    raw.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    return raw


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.secret_file = root / "client-secret"
        self.secret_file.write_text("example-client-secret", encoding="utf-8")
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.http = _FakeOIDCHTTP(
            "https://auth.example.com", _jwk_for(self.private_key), self.private_key
        )
        self.config = platform_auth.WebAuthConfig(
            issuer="https://auth.example.com",
            client_id="shadow-stock",
            client_secret_file=str(self.secret_file),
            redirect_uri="https://stock.example.com/auth/callback",
            post_logout_redirect_uri="https://stock.example.com/",
            session_db=str(root / "auth.db"),
        )
        self.service = platform_auth.WebAuthService(self.config, http=self.http)
        platform_auth._service = self.service
        access_control.reset_agent_authenticator()

    def tearDown(self):
        platform_auth.reset_web_auth_service()
        access_control.reset_agent_authenticator()
        self.temp.cleanup()

    def _claims(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": self.config.issuer,
            "sub": "subject-1",
            "aud": self.config.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": "nonce-1",
            "groups": ["stock-users"],
        }
        claims.update(overrides)
        return claims

    def _token(self, claims, key=None):
        return jwt.encode(
            claims,
            key or self.private_key,
            algorithm="RS256",
            headers={"kid": "k1"},
        )

    def test_state_is_one_time_and_pkce_challenge_matches_verifier(self):
        state, nonce, verifier, challenge = self.service.store.create_login_transaction(
            return_to="/research?code=600519", ttl_seconds=60
        )
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(challenge, expected)
        transaction = self.service.store.consume_login_transaction(state)
        self.assertEqual(transaction["nonce"], nonce)
        self.assertEqual(transaction["return_to"], "/research?code=600519")
        with self.assertRaises(platform_auth.WebAuthError):
            self.service.store.consume_login_transaction(state)

    def test_verified_identity_mapping_is_stable_and_session_is_revocable(self):
        first = self.service.store.upsert_identity(self._claims())
        second = self.service.store.upsert_identity(self._claims(name="Changed Name"))
        self.assertEqual(first.shadow_user_id, second.shadow_user_id)
        session = self.service.store.create_session(first, ttl_seconds=300)
        self.assertEqual(
            self.service.store.authenticate_session(session.session_token).identity.shadow_user_id,
            first.shadow_user_id,
        )
        rotated = self.service.store.rotate_session(session.session_token)
        self.assertIsNone(self.service.store.authenticate_session(session.session_token))
        self.assertIsNotNone(self.service.store.authenticate_session(rotated.session_token))
        self.service.store.revoke_session(rotated.session_token)
        self.assertIsNone(self.service.store.authenticate_session(rotated.session_token))

    def test_id_token_validates_signature_issuer_audience_exp_iat_and_nonce(self):
        valid = self._token(self._claims())
        claims = self.service.oidc.verify_id_token(valid, nonce="nonce-1")
        self.assertEqual(claims["sub"], "subject-1")

        cases = [
            (self._claims(iss="https://wrong.example.com"), "issuer"),
            (self._claims(aud="wrong-client"), "audience"),
            (self._claims(exp=int(time.time()) - 120), "expired"),
            (self._claims(iat=int(time.time()) + 300), "not_yet_valid"),
            (self._claims(iat=int(time.time()) - 3600), "issued_at_too_old"),
            (self._claims(nonce="wrong-nonce"), "nonce"),
        ]
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        tokens = [(self._token(item), reason) for item, reason in cases]
        tokens.append((self._token(self._claims(), other_key), "signature"))
        for token, reason in tokens:
            with self.subTest(token=token[-12:]):
                with self.assertRaises(platform_auth.WebAuthError) as raised:
                    self.service.oidc.verify_id_token(token, nonce="nonce-1")
                self.assertEqual(raised.exception.reason, reason)

    def test_normal_login_returns_original_path_and_sets_strict_cookie(self):
        from webui.api_server import app

        with TestClient(app, base_url="https://stock.example.com") as client:
            start = client.get(
                "/auth/login", params={"return_to": "/research?code=600519"},
                follow_redirects=False,
            )
            self.assertEqual(start.status_code, 302)
            query = parse_qs(urlsplit(start.headers["location"]).query)
            self.http.expected_nonce = query["nonce"][0]
            callback = client.get(
                "/auth/callback",
                params={"state": query["state"][0], "code": "example-code"},
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 303)
            self.assertEqual(callback.headers["location"], "/research?code=600519")
            cookie = callback.headers["set-cookie"]
            self.assertIn("Secure", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=lax", cookie)
            self.assertIn("Path=/", cookie)
            self.assertNotIn("Domain=", cookie)
            self.assertGreaterEqual(len(self.http.last_token_data["code_verifier"]), 43)

    def test_group_gate_and_pkce_exchange_failure_do_not_create_session(self):
        from webui.api_server import app

        with TestClient(app, base_url="https://stock.example.com") as client:
            start = client.get("/auth/login", follow_redirects=False)
            query = parse_qs(urlsplit(start.headers["location"]).query)
            self.http.expected_nonce = query["nonce"][0]
            self.http.claim_overrides = {"groups": ["unrelated-users"]}
            denied = client.get(
                "/auth/callback",
                params={"state": query["state"][0], "code": "example-code"},
                follow_redirects=False,
            )
            self.assertEqual(denied.status_code, 403)
            self.assertNotIn(platform_auth.SESSION_COOKIE, denied.cookies)

            start = client.get("/auth/login", follow_redirects=False)
            query = parse_qs(urlsplit(start.headers["location"]).query)
            self.http.fail_token_exchange = True
            with self.assertLogs("webui", level="WARNING") as captured:
                failed = client.get(
                    "/auth/callback",
                    params={"state": query["state"][0], "code": "bad-code"},
                    follow_redirects=False,
                )
            self.assertEqual(failed.status_code, 400)
            rendered = "\n".join(captured.output)
            self.assertIn("oidc_callback_rejected stage=exchange_code", rendered)
            self.assertIn("reason=unspecified", rendered)
            self.assertNotIn("bad-code", rendered)
            self.assertNotIn(query["state"][0], rendered)

    def test_open_redirects_are_rejected(self):
        for value in (
            "https://evil.example/steal",
            "//evil.example/steal",
            "/\\evil.example/steal",
            "/%2f%2fevil.example/steal",
            "javascript:alert(1)",
        ):
            self.assertEqual(platform_auth.sanitize_return_to(value), "/")
        self.assertEqual(platform_auth.sanitize_return_to("/stock?q=1"), "/stock?q=1")

    def test_route_matrix_browser_admin_and_machine_boundaries(self):
        from webui.api_server import app

        self.assertEqual(access_control.unclassified_routes(app), [])
        with TestClient(app, base_url="https://stock.example.com") as client:
            page = client.get("/", follow_redirects=False)
            self.assertEqual(page.status_code, 302)
            api = client.get("/api/auth/me", follow_redirects=False)
            self.assertEqual(api.status_code, 401)
            machine = client.get("/api/machine/runtime-health", follow_redirects=False)
            self.assertEqual(machine.status_code, 401)
            self.assertNotIn("location", machine.headers)

            bearer_on_page = client.get(
                "/", headers={"Authorization": "Bearer " + "x" * 40}, follow_redirects=False
            )
            self.assertEqual(bearer_on_page.status_code, 302)

            user = self.service.store.upsert_identity(self._claims())
            session = self.service.store.create_session(user, ttl_seconds=300)
            client.cookies.set(platform_auth.SESSION_COOKIE, session.session_token)
            self.assertEqual(client.get("/api/auth/me").status_code, 200)
            self.assertEqual(client.get("/api/env").status_code, 403)
            cookie_on_machine = client.get(
                "/api/machine/runtime-health", follow_redirects=False
            )
            self.assertEqual(cookie_on_machine.status_code, 401)
            with self.assertLogs("webui.security_audit", level="INFO") as captured:
                denied_write = client.post(
                    "/api/env",
                    json={
                        "updates": {
                            "OIDC_CLIENT_SECRET": "do-not-log",
                            "portfolio_value": "123456.78",
                        }
                    },
                    headers={"Origin": "https://stock.example.com"},
                )
            self.assertEqual(denied_write.status_code, 403)
            audit_text = "\n".join(captured.output)
            self.assertNotIn("do-not-log", audit_text)
            self.assertNotIn("123456.78", audit_text)
            self.assertNotIn(session.session_token, audit_text)

            admin_claims = self._claims(sub="admin-sub", groups=["stock-users", "stock-admins"])
            admin = self.service.store.upsert_identity(admin_claims)
            admin_session = self.service.store.create_session(admin, ttl_seconds=300)
            client.cookies.set(platform_auth.SESSION_COOKIE, admin_session.session_token)
            self.assertEqual(client.get("/api/env").status_code, 200)

    def test_public_health_is_stateless_and_readiness_is_protected_detail(self):
        from webui.api_server import app

        with patch("runtime_health.snapshot", side_effect=AssertionError("must not run")):
            with TestClient(app, base_url="https://stock.example.com") as client:
                self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
                legacy = client.get("/api/health").json()
                self.assertEqual(legacy, {"ok": True, "data": {"status": "ok"}})

        with patch(
            "runtime_health.snapshot",
            return_value={"ready": True, "status": "ready", "revision": "example"},
        ):
            with TestClient(app, base_url="https://stock.example.com") as client:
                ready = client.get("/readyz")
                self.assertEqual(ready.status_code, 200)
                self.assertTrue(ready.json()["ready"])

    def test_local_and_global_logout_revoke_server_sessions(self):
        from webui.api_server import app

        user = self.service.store.upsert_identity(self._claims())
        first = self.service.store.create_session(user, ttl_seconds=300)
        second = self.service.store.create_session(user, ttl_seconds=300)
        with TestClient(app, base_url="https://stock.example.com") as client:
            client.cookies.set(platform_auth.SESSION_COOKIE, first.session_token)
            local = client.post(
                "/auth/logout",
                headers={"Origin": "https://stock.example.com"},
                follow_redirects=False,
            )
            self.assertEqual(local.status_code, 303)
            self.assertIsNone(self.service.store.authenticate_session(first.session_token))
            self.assertIsNotNone(self.service.store.authenticate_session(second.session_token))

            client.cookies.set(platform_auth.SESSION_COOKIE, second.session_token)
            global_logout = client.post(
                "/auth/logout/all",
                headers={"Origin": "https://stock.example.com"},
                follow_redirects=False,
            )
            self.assertEqual(global_logout.status_code, 303)
            self.assertTrue(global_logout.headers["location"].startswith(self.config.issuer))
            self.assertIsNone(self.service.store.authenticate_session(second.session_token))

    def test_agent_scope_returns_json_403_and_never_302(self):
        from webui.api_server import app

        class FakeAuthenticator:
            def authenticate(self, authorization):
                if authorization != "Bearer " + "x" * 40:
                    raise ValueError("invalid")
                return SimpleNamespace(
                    agent_id="foliant-test", scopes=frozenset({"stock.read"})
                )

        access_control._agent_authenticator = FakeAuthenticator()
        with TestClient(app, base_url="https://stock.example.com") as client:
            response = client.get(
                "/api/machine/research/600519",
                headers={"Authorization": "Bearer " + "x" * 40},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("location", response.headers)

    def test_access_log_filter_removes_callback_and_financial_query_values(self):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1", "GET", "/auth/callback?code=secret&state=secret", "1.1", 303),
            None,
        )
        self.assertTrue(RedactRequestQueryFilter().filter(record))
        rendered = record.getMessage()
        self.assertNotIn("code=", rendered)
        self.assertNotIn("state=", rendered)
        self.assertIn("?<redacted>", rendered)


if __name__ == "__main__":
    unittest.main()
