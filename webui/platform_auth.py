"""Native OIDC and opaque server-side sessions for the Stock Web application.

OIDC tokens are verified and discarded during callback processing. The browser only receives a
random session handle; PostgreSQL stores its SHA-256 digest and normalized identity attributes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlencode, urlsplit

import requests

import _bootstrap
from db_compat import connect as db_connect

SESSION_COOKIE = "__Host-shadow_stock_session"
_OIDC_SCOPES = "openid profile email groups"
_ALLOWED_JWT_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")


class WebAuthError(ValueError):
    """A deliberately non-sensitive authentication failure."""

    def __init__(self, message: str, *, reason: str = "unspecified") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class WebAuthConfig:
    issuer: str
    client_id: str
    client_secret_file: str
    redirect_uri: str
    post_logout_redirect_uri: str
    required_group: str = "stock-users"
    admin_group: str = "stock-admins"
    session_db: str = ""
    session_ttl_seconds: int = 12 * 60 * 60
    transaction_ttl_seconds: int = 10 * 60
    clock_skew_seconds: int = 60
    id_token_max_age_seconds: int = 10 * 60
    metadata_ttl_seconds: int = 15 * 60

    @classmethod
    def from_env(cls) -> WebAuthConfig:
        return cls(
            issuer=os.getenv("SHADOW_OIDC_ISSUER", "").strip().rstrip("/"),
            client_id=os.getenv("SHADOW_OIDC_CLIENT_ID", "shadow-stock").strip(),
            client_secret_file=os.getenv("SHADOW_OIDC_CLIENT_SECRET_FILE", "").strip(),
            redirect_uri=os.getenv("SHADOW_OIDC_REDIRECT_URI", "").strip(),
            post_logout_redirect_uri=os.getenv(
                "SHADOW_OIDC_POST_LOGOUT_REDIRECT_URI", ""
            ).strip(),
            required_group=os.getenv("SHADOW_OIDC_REQUIRED_GROUP", "stock-users").strip(),
            admin_group=os.getenv("SHADOW_OIDC_ADMIN_GROUP", "stock-admins").strip(),
            session_db=os.getenv("SHADOW_OIDC_SESSION_DB", "").strip(),
            session_ttl_seconds=_positive_int("SHADOW_OIDC_SESSION_TTL_SECONDS", 12 * 60 * 60),
            transaction_ttl_seconds=_positive_int(
                "SHADOW_OIDC_TRANSACTION_TTL_SECONDS", 10 * 60
            ),
            clock_skew_seconds=_positive_int("SHADOW_OIDC_CLOCK_SKEW_SECONDS", 60),
            id_token_max_age_seconds=_positive_int(
                "SHADOW_OIDC_ID_TOKEN_MAX_AGE_SECONDS", 10 * 60
            ),
            metadata_ttl_seconds=_positive_int("SHADOW_OIDC_METADATA_TTL_SECONDS", 15 * 60),
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.issuer
            and self.client_id
            and self.client_secret_file
            and self.redirect_uri
            and self.post_logout_redirect_uri
            and self.required_group
            and self.admin_group
        )

    @property
    def database_path(self) -> str:
        return self.session_db or _bootstrap.db_path("web_auth.db")

    def validate(self) -> None:
        if not self.configured:
            raise WebAuthError("OIDC is not configured")
        if self.client_id != "shadow-stock":
            raise WebAuthError("unexpected OIDC client")
        _require_https_url("issuer", self.issuer)
        _require_https_url("redirect URI", self.redirect_uri)
        _require_https_url("post logout URI", self.post_logout_redirect_uri)
        if urlsplit(self.redirect_uri).path != "/auth/callback":
            raise WebAuthError("OIDC callback path must be /auth/callback")
        redirect = urlsplit(self.redirect_uri)
        post_logout = urlsplit(self.post_logout_redirect_uri)
        if (redirect.scheme, redirect.netloc) != (post_logout.scheme, post_logout.netloc):
            raise WebAuthError("post logout URI must use the canonical application origin")
        secret_path = Path(self.client_secret_file).expanduser()
        if not secret_path.is_file():
            raise WebAuthError("OIDC client secret file is unavailable")


@dataclass(frozen=True, slots=True)
class BrowserIdentity:
    shadow_user_id: str
    issuer: str
    subject: str
    username: str
    display_name: str
    email: str
    groups: tuple[str, ...]

    def in_group(self, group: str) -> bool:
        return group in self.groups


@dataclass(frozen=True, slots=True)
class SessionRecord:
    identity: BrowserIdentity
    session_token: str
    expires_at: float


class SessionStore:
    """Small persistent store for OIDC transactions, identities, and revocable sessions."""

    def __init__(self, database_path: str = "", *, connect_fn: Callable | None = None,
                 is_postgres: bool = True) -> None:
        self.database_path = database_path
        self._connect_fn = connect_fn or db_connect
        self._is_postgres = is_postgres
        self._init_lock = threading.Lock()
        self._initialized = False

    def _raw_connect(self):
        return self._connect_fn(self.database_path)

    def _connect(self):
        self._ensure_schema()
        return self._raw_connect()

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            conn = self._raw_connect()
            try:
                statements = (
                    """
                    CREATE TABLE IF NOT EXISTS web_auth_transactions (
                        state_hash TEXT PRIMARY KEY,
                        nonce TEXT NOT NULL,
                        code_verifier TEXT NOT NULL,
                        return_to TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        created_at REAL NOT NULL
                    )
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS shadow_identities (
                        shadow_user_id TEXT PRIMARY KEY,
                        issuer TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        username TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        email TEXT NOT NULL,
                        groups_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        UNIQUE (issuer, subject)
                    )
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS web_sessions (
                        session_hash TEXT PRIMARY KEY,
                        shadow_user_id TEXT NOT NULL,
                        groups_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        revoked_at REAL,
                        FOREIGN KEY (shadow_user_id) REFERENCES shadow_identities(shadow_user_id)
                    )
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_web_sessions_user
                        ON web_sessions(shadow_user_id, expires_at)
                    """,
                )
                for statement in statements:
                    conn.execute(statement)
                conn.commit()
            finally:
                conn.close()
            self._initialized = True

    def create_login_transaction(
        self, *, return_to: str, ttl_seconds: int
    ) -> tuple[str, str, str, str]:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        now = time.time()
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM web_auth_transactions WHERE expires_at <= ?", (now,))
            conn.execute(
                """INSERT INTO web_auth_transactions
                   (state_hash, nonce, code_verifier, return_to, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_digest(state), nonce, verifier, return_to, now + ttl_seconds, now),
            )
        return state, nonce, verifier, challenge

    def consume_login_transaction(self, state: str) -> dict[str, Any]:
        if not state or len(state) > 512:
            raise WebAuthError("invalid login state")
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            lock_clause = " FOR UPDATE" if self._is_postgres else ""
            row = conn.execute(
                """SELECT nonce, code_verifier, return_to, expires_at
                   FROM web_auth_transactions WHERE state_hash = ?""" + lock_clause,
                (_digest(state),),
            ).fetchone()
            conn.execute(
                "DELETE FROM web_auth_transactions WHERE state_hash = ?", (_digest(state),)
            )
            conn.commit()
        finally:
            conn.close()
        if not row or float(row["expires_at"]) <= now:
            raise WebAuthError("invalid or expired login state")
        return dict(row)

    def upsert_identity(self, claims: dict[str, Any]) -> BrowserIdentity:
        issuer = str(claims.get("iss") or "").strip()
        subject = str(claims.get("sub") or "").strip()
        if not issuer or not subject:
            raise WebAuthError("verified identity is incomplete")
        groups = _normalize_groups(claims.get("groups"))
        username = str(claims.get("preferred_username") or subject)
        display_name = str(claims.get("name") or username)
        email = str(claims.get("email") or "")
        now = time.time()
        proposed_id = str(uuid.uuid4())
        groups_json = json.dumps(groups, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as conn, conn:
            insert_prefix = "INSERT INTO" if self._is_postgres else "INSERT OR IGNORE INTO"
            conflict = " ON CONFLICT (issuer, subject) DO NOTHING" if self._is_postgres else ""
            conn.execute(
                f"""{insert_prefix} shadow_identities
                   (shadow_user_id, issuer, subject, username, display_name, email,
                    groups_json, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?){conflict}""",
                (
                    proposed_id,
                    issuer,
                    subject,
                    username,
                    display_name,
                    email,
                    groups_json,
                    now,
                    now,
                ),
            )
            conn.execute(
                """UPDATE shadow_identities SET username = ?, display_name = ?, email = ?,
                   groups_json = ?, last_seen_at = ? WHERE issuer = ? AND subject = ?""",
                (username, display_name, email, groups_json, now, issuer, subject),
            )
            row = conn.execute(
                """SELECT shadow_user_id FROM shadow_identities
                   WHERE issuer = ? AND subject = ?""",
                (issuer, subject),
            ).fetchone()
        return BrowserIdentity(
            shadow_user_id=str(row["shadow_user_id"]),
            issuer=issuer,
            subject=subject,
            username=username,
            display_name=display_name,
            email=email,
            groups=groups,
        )

    def create_session(self, identity: BrowserIdentity, *, ttl_seconds: int) -> SessionRecord:
        token = secrets.token_urlsafe(48)
        now = time.time()
        expires_at = now + ttl_seconds
        groups_json = json.dumps(identity.groups, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                """INSERT INTO web_sessions
                   (session_hash, shadow_user_id, groups_json, created_at, expires_at, revoked_at)
                   VALUES (?, ?, ?, ?, ?, NULL)""",
                (_digest(token), identity.shadow_user_id, groups_json, now, expires_at),
            )
        return SessionRecord(identity=identity, session_token=token, expires_at=expires_at)

    def authenticate_session(self, token: str) -> SessionRecord | None:
        if not token or len(token) > 512:
            return None
        now = time.time()
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                """SELECT s.expires_at, s.groups_json, i.shadow_user_id, i.issuer, i.subject,
                          i.username, i.display_name, i.email
                   FROM web_sessions s
                   JOIN shadow_identities i ON i.shadow_user_id = s.shadow_user_id
                   WHERE s.session_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?""",
                (_digest(token), now),
            ).fetchone()
        if not row:
            return None
        identity = BrowserIdentity(
            shadow_user_id=str(row["shadow_user_id"]),
            issuer=str(row["issuer"]),
            subject=str(row["subject"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            email=str(row["email"]),
            groups=tuple(json.loads(row["groups_json"])),
        )
        return SessionRecord(identity=identity, session_token=token, expires_at=row["expires_at"])

    def rotate_session(self, token: str) -> SessionRecord | None:
        current = self.authenticate_session(token)
        if not current:
            return None
        replacement = secrets.token_urlsafe(48)
        with closing(self._connect()) as conn, conn:
            updated = conn.execute(
                """UPDATE web_sessions SET session_hash = ?
                   WHERE session_hash = ? AND revoked_at IS NULL""",
                (_digest(replacement), _digest(token)),
            ).rowcount
        if updated != 1:
            return None
        return SessionRecord(
            identity=current.identity,
            session_token=replacement,
            expires_at=current.expires_at,
        )

    def revoke_session(self, token: str) -> None:
        if not token or len(token) > 512:
            return
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE web_sessions SET revoked_at = ? WHERE session_hash = ?",
                (time.time(), _digest(token)),
            )

    def revoke_user_sessions(self, shadow_user_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """UPDATE web_sessions SET revoked_at = ?
                   WHERE shadow_user_id = ? AND revoked_at IS NULL""",
                (time.time(), shadow_user_id),
            )


class OIDCClient:
    def __init__(self, config: WebAuthConfig, http: Any = requests) -> None:
        self.config = config
        self.http = http
        self._metadata: tuple[float, dict[str, Any]] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None
        self._cache_lock = threading.Lock()

    def authorization_url(self, *, state: str, nonce: str, challenge: str) -> str:
        metadata = self._get_metadata()
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "scope": _OIDC_SCOPES,
            "redirect_uri": self.config.redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{metadata['authorization_endpoint']}?{urlencode(params)}"

    def exchange_code(self, *, code: str, verifier: str) -> dict[str, Any]:
        if not code or len(code) > 4096:
            raise WebAuthError("authorization code is missing")
        metadata = self._get_metadata()
        client_secret = Path(self.config.client_secret_file).read_text(encoding="utf-8").strip()
        if not client_secret:
            raise WebAuthError("OIDC client secret is unavailable")
        try:
            response = self.http.post(
                metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.config.redirect_uri,
                    "code_verifier": verifier,
                },
                auth=(self.config.client_id, client_secret),
                timeout=15,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise WebAuthError("OIDC token exchange failed") from exc
        if not isinstance(payload, dict) or not payload.get("id_token"):
            raise WebAuthError("OIDC token response is incomplete")
        return payload

    def verify_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        try:
            import jwt

            header = jwt.get_unverified_header(id_token)
            algorithm = str(header.get("alg") or "")
            if algorithm not in _ALLOWED_JWT_ALGORITHMS:
                raise WebAuthError(
                    "ID Token algorithm is not allowed", reason="algorithm"
                )
            kid = str(header.get("kid") or "")
            keys = self._get_jwks().get("keys") or []
            candidates = [
                key
                for key in keys
                if (not kid or str(key.get("kid") or "") == kid)
                and key.get("use") in (None, "", "sig")
                and key.get("alg") in (None, "", algorithm)
            ]
            if len(candidates) != 1:
                self._jwks = None
                keys = self._get_jwks().get("keys") or []
                candidates = [
                    key
                    for key in keys
                    if (not kid or str(key.get("kid") or "") == kid)
                    and key.get("use") in (None, "", "sig")
                    and key.get("alg") in (None, "", algorithm)
                ]
            if len(candidates) != 1:
                raise WebAuthError(
                    "ID Token signing key is unavailable", reason="signing_key"
                )
            signing_key = jwt.PyJWK.from_dict(candidates[0], algorithm=algorithm).key
            claims = jwt.decode(
                id_token,
                key=signing_key,
                algorithms=[algorithm],
                issuer=self.config.issuer,
                audience=self.config.client_id,
                leeway=self.config.clock_skew_seconds,
                options={
                    "require": ["iss", "sub", "aud", "exp", "iat", "nonce"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except WebAuthError:
            raise
        except Exception as exc:
            reason = {
                "DecodeError": "decode",
                "ExpiredSignatureError": "expired",
                "ImmatureSignatureError": "not_yet_valid",
                "InvalidAlgorithmError": "algorithm",
                "InvalidAudienceError": "audience",
                "InvalidIssuedAtError": "issued_at",
                "InvalidIssuerError": "issuer",
                "InvalidKeyError": "signing_key",
                "InvalidSignatureError": "signature",
                "MissingRequiredClaimError": "missing_claim",
            }.get(type(exc).__name__, "validation")
            raise WebAuthError("ID Token validation failed", reason=reason) from exc
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise WebAuthError("ID Token nonce mismatch", reason="nonce")
        if time.time() - float(claims["iat"]) > (
            self.config.id_token_max_age_seconds + self.config.clock_skew_seconds
        ):
            raise WebAuthError(
                "ID Token issued-at time is too old", reason="issued_at_too_old"
            )
        # The OIDC provider may return requested profile claims from UserInfo instead of
        # duplicating them in the ID Token.  An explicitly present malformed groups claim is
        # still rejected; an absent claim is completed from UserInfo by the callback.
        raw_groups = claims.get("groups")
        claims["groups"] = () if raw_groups is None else _normalize_groups(raw_groups)
        return claims

    def fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        if not access_token or len(access_token) > 8192:
            raise WebAuthError("OIDC access token is unavailable", reason="userinfo")
        endpoint = str(self._get_metadata().get("userinfo_endpoint") or "")
        if not endpoint:
            raise WebAuthError("OIDC UserInfo endpoint is unavailable", reason="userinfo")
        try:
            response = self.http.get(
                endpoint,
                timeout=10,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise WebAuthError("OIDC UserInfo request failed", reason="userinfo") from exc
        if not isinstance(payload, dict) or not str(payload.get("sub") or "").strip():
            raise WebAuthError("OIDC UserInfo response is invalid", reason="userinfo")
        payload["groups"] = _normalize_groups(payload.get("groups"))
        return payload

    def complete_profile_claims(
        self, claims: dict[str, Any], token_response: dict[str, Any]
    ) -> dict[str, Any]:
        if claims.get("groups"):
            return claims
        userinfo = self.fetch_userinfo(str(token_response.get("access_token") or ""))
        if not secrets.compare_digest(
            str(userinfo.get("sub") or ""), str(claims.get("sub") or "")
        ):
            raise WebAuthError("OIDC UserInfo subject mismatch", reason="userinfo_subject")
        claims["groups"] = userinfo["groups"]
        for name in ("preferred_username", "name", "email"):
            if not claims.get(name) and userinfo.get(name):
                claims[name] = userinfo[name]
        return claims

    def global_logout_url(self) -> str:
        endpoint = self._get_metadata().get("end_session_endpoint")
        if not endpoint:
            return self.config.post_logout_redirect_uri
        params = {
            "client_id": self.config.client_id,
            "post_logout_redirect_uri": self.config.post_logout_redirect_uri,
        }
        return f"{endpoint}?{urlencode(params)}"

    def _get_metadata(self) -> dict[str, Any]:
        now = time.time()
        if self._metadata and self._metadata[0] > now:
            return self._metadata[1]
        with self._cache_lock:
            if self._metadata and self._metadata[0] > now:
                return self._metadata[1]
            url = f"{self.config.issuer}/.well-known/openid-configuration"
            try:
                response = self.http.get(url, timeout=10, headers={"Accept": "application/json"})
                response.raise_for_status()
                metadata = response.json()
            except Exception as exc:
                raise WebAuthError("OIDC discovery failed") from exc
            if not isinstance(metadata, dict) or metadata.get("issuer") != self.config.issuer:
                raise WebAuthError("OIDC discovery issuer mismatch")
            for name in (
                "authorization_endpoint",
                "token_endpoint",
                "userinfo_endpoint",
                "jwks_uri",
            ):
                _require_https_url(name, str(metadata.get(name) or ""))
            if metadata.get("end_session_endpoint"):
                _require_https_url("end session endpoint", metadata["end_session_endpoint"])
            self._metadata = (now + self.config.metadata_ttl_seconds, metadata)
            return metadata

    def _get_jwks(self) -> dict[str, Any]:
        now = time.time()
        if self._jwks and self._jwks[0] > now:
            return self._jwks[1]
        metadata = self._get_metadata()
        try:
            response = self.http.get(
                metadata["jwks_uri"], timeout=10, headers={"Accept": "application/json"}
            )
            response.raise_for_status()
            jwks = response.json()
        except Exception as exc:
            raise WebAuthError("OIDC signing keys unavailable") from exc
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise WebAuthError("OIDC signing keys are invalid")
        self._jwks = (now + self.config.metadata_ttl_seconds, jwks)
        return jwks


class WebAuthService:
    def __init__(self, config: WebAuthConfig, *, http: Any = requests,
                 session_connect_fn: Callable | None = None,
                 session_is_postgres: bool = True) -> None:
        config.validate()
        self.config = config
        self.store = SessionStore(
            config.database_path,
            connect_fn=session_connect_fn,
            is_postgres=session_is_postgres,
        )
        self.oidc = OIDCClient(config, http=http)


_service: WebAuthService | None = None
_service_lock = threading.Lock()


def get_web_auth_service() -> WebAuthService:
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = WebAuthService(WebAuthConfig.from_env())
    return _service


def reset_web_auth_service() -> None:
    """Tests and configuration reloads may reset the lazy singleton."""
    global _service
    _service = None


def sanitize_return_to(value: str | None) -> str:
    value = str(value or "/").strip()
    if len(value) > 2048 or not value.startswith("/") or value.startswith("//"):
        return "/"
    decoded = unquote(value)
    if (
        decoded.startswith("//")
        or "\\" in decoded
        or any(ord(char) < 32 for char in decoded)
    ):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


def set_session_cookie(response: Any, record: SessionRecord) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        record.session_token,
        max_age=max(0, int(record.expires_at - time.time())),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _normalize_groups(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise WebAuthError("groups claim is invalid", reason="groups")
    groups = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
    return groups


def _require_https_url(label: str, value: str) -> None:
    parsed = urlsplit(value)
    allow_http = os.getenv("SHADOW_OIDC_ALLOW_HTTP_FOR_TESTS", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if (parsed.scheme != "https" and not (allow_http and parsed.scheme == "http")) or not parsed.netloc:
        raise WebAuthError(f"{label} must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise WebAuthError(f"{label} must not contain credentials")


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
