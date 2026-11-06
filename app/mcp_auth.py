"""Persistent, scoped MCP authorization, separate from application sessions.

The MCP SDK owns protocol parsing, redirect matching and PKCE verification.
This module owns consent, account binding and atomic credential lifecycle.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from fastapi import HTTPException
from mcp.server.auth.middleware.client_auth import AuthenticationError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from app.auth_service import (
    authenticate_token,
    build_authorization_url,
    create_oauth_state,
    revoke_token,
)
from app.config import Settings
from app.db import get_connection

SCOPE = "favorites:read"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
GRANT_TTL = 90 * 86400
SESSION_COOKIE = "yabus_mcp_session"
CSRF_COOKIE = "yabus_mcp_csrf"
BROWSER_COOKIE = "yabus_mcp_browser"

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_clients (
    client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_pending (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
    expires_at INTEGER NOT NULL, browser_hash TEXT
);
CREATE TABLE IF NOT EXISTS mcp_login_sessions (
    hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_grants (
    id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    revoked_at INTEGER, resource TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_grants_account ON mcp_grants(account_id);
CREATE TABLE IF NOT EXISTS mcp_codes (
    hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES mcp_grants(id) ON DELETE CASCADE,
    params TEXT NOT NULL, expires_at INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mcp_tokens (
    hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES mcp_grants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, scopes TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_grant ON mcp_tokens(grant_id);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AccountCode(AuthorizationCode):
    grant_id: str


class AccountAccess(AccessToken):
    grant_id: str


class AccountRefresh(RefreshToken):
    grant_id: str


class MCPAuthorizationProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.issuer = settings.auth_public_base_url.rstrip("/")
        parsed = urlsplit(self.issuer)
        if (
            parsed.scheme != "https"
            and parsed.hostname not in {"localhost", "127.0.0.1"}
        ) or (
            parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("MCP requires an HTTPS AUTH_PUBLIC_BASE_URL with no path")
        self.resource = self.issuer + "/mcp"

    def initialize(self) -> None:
        with get_connection(self.settings.app_db_path) as db:
            db.executescript(SCHEMA)
            db.commit()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with get_connection(self.settings.app_db_path) as db:
            row = db.execute(
                "SELECT metadata FROM mcp_clients WHERE client_id=?", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = client_info.redirect_uris or []
        if not uris or len(uris) > 10:
            raise RegistrationError(
                "invalid_redirect_uri", "Provide 1 to 10 redirect URIs"
            )
        for uri in uris:
            p = urlsplit(str(uri))
            if (
                p.scheme != "https"
                and not (
                    p.scheme == "http"
                    and p.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            ) or (not p.hostname or p.fragment or p.username or p.password):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Use HTTPS or a loopback callback without a fragment",
                )
        if client_info.token_endpoint_auth_method not in {
            "none",
            "client_secret_post",
            "client_secret_basic",
        }:
            raise RegistrationError(
                "invalid_client_metadata", "Unsupported client authentication method"
            )
        if len(client_info.client_name or "") > 120:
            raise RegistrationError(
                "invalid_client_metadata", "Client name is too long"
            )
        # Only the registration response contains the client secret. Persist its hash.
        stored = client_info.model_copy(deep=True)
        if stored.client_secret:
            stored.client_secret = digest(stored.client_secret)
        with get_connection(self.settings.app_db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM mcp_clients").fetchone()[0] >= 10000:
                raise RegistrationError(
                    "invalid_client_metadata", "Registration capacity reached"
                )
            db.execute(
                "INSERT INTO mcp_clients VALUES (?,?,?)",
                (stored.client_id, stored.model_dump_json(), int(time.time())),
            )
            db.commit()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError(
                "invalid_request", "A valid S256 PKCE challenge is required"
            )
        if params.state is not None and len(params.state) > 2048:
            raise AuthorizeError("invalid_request", "State is too long")
        if params.resource not in {None, self.resource}:
            raise AuthorizeError("invalid_request", "Unknown resource")
        if set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_scope", "Only favorites:read is supported")
        params = params.model_copy(
            update={"resource": self.resource, "scopes": [SCOPE]}
        )
        pending_id = secrets.token_urlsafe(32)
        with get_connection(self.settings.app_db_path) as db:
            db.execute(
                "DELETE FROM mcp_login_sessions WHERE expires_at<=?",
                (int(time.time()),),
            )
            db.execute(
                "DELETE FROM mcp_pending WHERE expires_at<=?", (int(time.time()),)
            )
            db.execute("DELETE FROM mcp_codes WHERE expires_at<=?", (int(time.time()),))
            db.execute(
                "DELETE FROM mcp_tokens WHERE expires_at<=?", (int(time.time()),)
            )
            db.execute(
                "INSERT INTO mcp_pending(id,client_id,params,expires_at) VALUES(?,?,?,?)",
                (
                    pending_id,
                    client.client_id,
                    params.model_dump_json(),
                    int(time.time()) + 600,
                ),
            )
            db.commit()
        return self.issuer + "/oauth/consent?" + urlencode({"request_id": pending_id})

    def pending(self, pending_id: str):
        if len(pending_id) > 128:
            raise HTTPException(400, "Invalid authorization request")
        with get_connection(self.settings.app_db_path) as db:
            row = db.execute(
                "SELECT * FROM mcp_pending WHERE id=? AND expires_at>?",
                (pending_id, int(time.time())),
            ).fetchone()
        if row is None:
            raise HTTPException(
                400, "Authorization request expired. Connect again from your AI app."
            )
        return row

    def complete_consent(self, pending_id: str, account_id: int, approve: bool) -> str:
        now = int(time.time())
        with get_connection(self.settings.app_db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "DELETE FROM mcp_pending WHERE id=? AND expires_at>? RETURNING *",
                (pending_id, now),
            ).fetchone()
            if row is None:
                raise HTTPException(
                    400, "Authorization request expired or already used"
                )
            params = AuthorizationParams.model_validate_json(row["params"])
            query = {"state": params.state} if params.state is not None else {}
            if approve:
                grant_id, code = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
                db.execute(
                    "INSERT INTO mcp_grants VALUES(?,?,?,?,?,NULL,?)",
                    (
                        grant_id,
                        account_id,
                        row["client_id"],
                        now,
                        now + GRANT_TTL,
                        self.resource,
                    ),
                )
                db.execute(
                    "INSERT INTO mcp_codes(hash,grant_id,params,expires_at) VALUES(?,?,?,?)",
                    (digest(code), grant_id, row["params"], now + 120),
                )
                query["code"] = code
            else:
                query["error"] = "access_denied"
            db.commit()
        uri = str(params.redirect_uri)
        return uri + ("&" if "?" in uri else "?") + urlencode(query)

    async def load_authorization_code(self, client, authorization_code):
        with get_connection(self.settings.app_db_path) as db:
            row = db.execute(
                """SELECT c.*,g.client_id,g.account_id FROM mcp_codes c JOIN mcp_grants g ON g.id=c.grant_id
                WHERE c.hash=? AND c.consumed=0 AND c.expires_at>? AND g.revoked_at IS NULL AND g.client_id=?""",
                (digest(authorization_code), int(time.time()), client.client_id),
            ).fetchone()
        if row is None:
            return None
        params = AuthorizationParams.model_validate_json(row["params"])
        return AccountCode(
            code=authorization_code,
            client_id=client.client_id,
            expires_at=row["expires_at"],
            subject=str(row["account_id"]),
            grant_id=row["grant_id"],
            **params.model_dump(exclude={"state"}),
        )

    def _issue_tokens(
        self, db, grant_id: str, scopes: list[str], refresh: str | None = None
    ) -> OAuthToken:
        now = int(time.time())
        grant = db.execute(
            "SELECT * FROM mcp_grants WHERE id=? AND revoked_at IS NULL AND expires_at>?",
            (grant_id, now),
        ).fetchone()
        if grant is None:
            raise TokenError("invalid_grant", "Authorization expired or revoked")
        access = secrets.token_urlsafe(32)
        access_ttl = min(ACCESS_TTL, grant["expires_at"] - now)
        db.execute(
            "INSERT INTO mcp_tokens VALUES(?,?,?,?,?)",
            (digest(access), grant_id, "access", json.dumps(scopes), now + access_ttl),
        )
        refresh = refresh or secrets.token_urlsafe(48)
        # Persistent linking: keep the refresh token stable, with sliding inactivity
        # expiry and an absolute 90-day consent lifetime. Revocation ends the family.
        db.execute(
            "INSERT INTO mcp_tokens VALUES(?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET expires_at=excluded.expires_at",
            (
                digest(refresh),
                grant_id,
                "refresh",
                json.dumps(scopes),
                min(now + REFRESH_TTL, grant["expires_at"]),
            ),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        with get_connection(self.settings.app_db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """UPDATE mcp_codes SET consumed=1 WHERE hash=? AND consumed=0 AND expires_at>?
                AND grant_id IN (SELECT id FROM mcp_grants WHERE client_id=? AND revoked_at IS NULL) RETURNING grant_id""",
                (digest(authorization_code.code), int(time.time()), client.client_id),
            ).fetchone()
            if row is None:
                raise TokenError("invalid_grant", "Code expired or already used")
            tokens = self._issue_tokens(db, row["grant_id"], authorization_code.scopes)
            db.commit()
        return tokens

    def _load_token(self, value: str, kind: str):
        if len(value) > 512:
            return None
        with get_connection(self.settings.app_db_path) as db:
            return db.execute(
                """SELECT t.*,g.client_id,g.account_id,g.resource FROM mcp_tokens t JOIN mcp_grants g ON g.id=t.grant_id
                JOIN accounts a ON a.id=g.account_id WHERE t.hash=? AND t.kind=? AND t.expires_at>?
                AND g.expires_at>? AND g.revoked_at IS NULL AND g.resource=?""",
                (
                    digest(value),
                    kind,
                    int(time.time()),
                    int(time.time()),
                    self.resource,
                ),
            ).fetchone()

    async def load_access_token(self, token: str):
        row = self._load_token(token, "access")
        return (
            AccountAccess(
                token=token,
                client_id=row["client_id"],
                scopes=json.loads(row["scopes"]),
                expires_at=row["expires_at"],
                resource=self.resource,
                subject=str(row["account_id"]),
                grant_id=row["grant_id"],
            )
            if row
            else None
        )

    async def verify_token(self, token: str):
        return await self.load_access_token(token)

    async def load_refresh_token(self, client, refresh_token):
        reused = self._load_token(refresh_token, "used_refresh")
        if reused and reused["client_id"] == client.client_id:
            self.revoke_grant(reused["grant_id"], reused["account_id"])
            return None
        row = self._load_token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return AccountRefresh(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            resource=self.resource,
            subject=str(row["account_id"]),
            grant_id=row["grant_id"],
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with get_connection(self.settings.app_db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM mcp_tokens WHERE hash=? AND kind='refresh' AND expires_at>?",
                (digest(refresh_token.token), int(time.time())),
            ).fetchone()
            grant = db.execute(
                "SELECT client_id FROM mcp_grants WHERE id=?", (refresh_token.grant_id,)
            ).fetchone()
            if row is None or grant is None or grant[0] != client.client_id:
                # A concurrent exchange can have rotated the token after load.
                if (
                    grant
                    and grant[0] == client.client_id
                    and db.execute(
                        "SELECT 1 FROM mcp_tokens WHERE hash=? AND kind='used_refresh'",
                        (digest(refresh_token.token),),
                    ).fetchone()
                ):
                    db.execute(
                        "UPDATE mcp_grants SET revoked_at=? WHERE id=?",
                        (int(time.time()), refresh_token.grant_id),
                    )
                    db.commit()
                raise TokenError("invalid_grant", "Invalid refresh token")
            if not set(scopes).issubset(json.loads(row["scopes"])):
                raise TokenError("invalid_scope", "Scope cannot be expanded")
            persistent_refresh = refresh_token.token
            if client.token_endpoint_auth_method == "none":
                # Public clients cannot authenticate a refresh request. Rotate
                # their token and invalidate the family if a used token returns.
                db.execute(
                    "UPDATE mcp_tokens SET kind='used_refresh' WHERE hash=?",
                    (digest(refresh_token.token),),
                )
                persistent_refresh = None
            tokens = self._issue_tokens(
                db, refresh_token.grant_id, scopes, persistent_refresh
            )
            db.commit()
        return tokens

    async def revoke_token(self, token):
        with get_connection(self.settings.app_db_path) as db:
            db.execute(
                "UPDATE mcp_grants SET revoked_at=? WHERE id=?",
                (int(time.time()), token.grant_id),
            )
            db.commit()

    def account_grants(self, account_id: int):
        with get_connection(self.settings.app_db_path) as db:
            rows = db.execute(
                """SELECT g.id,g.created_at,g.expires_at,c.metadata FROM mcp_grants g
                JOIN mcp_clients c ON c.client_id=g.client_id WHERE g.account_id=? AND g.revoked_at IS NULL AND g.expires_at>?
                ORDER BY g.created_at DESC""",
                (account_id, int(time.time())),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "client_name": json.loads(row["metadata"]).get("client_name")
                or "MCP client",
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "scope": SCOPE,
            }
            for row in rows
        ]

    def revoke_grant(self, grant_id: str, account_id: int):
        with get_connection(self.settings.app_db_path) as db:
            db.execute(
                "UPDATE mcp_grants SET revoked_at=? WHERE id=? AND account_id=?",
                (int(time.time()), grant_id, account_id),
            )
            db.commit()


class HashedClientAuthenticator:
    """SDK token/revocation handlers with hash-only client-secret persistence."""

    def __init__(self, provider: MCPAuthorizationProvider):
        self.provider = provider

    async def authenticate_request(self, request: Request):
        from urllib.parse import unquote

        form = await request.form()
        client_id, secret = form.get("client_id"), form.get("client_secret")
        basic = request.headers.get("authorization", "")
        if basic.startswith("Basic "):
            try:
                basic_id, secret = (
                    base64.b64decode(basic[6:], validate=True).decode().split(":", 1)
                )
                basic_id, secret = unquote(basic_id), unquote(secret)
            except (ValueError, UnicodeError):
                raise AuthenticationError("Invalid client authentication") from None
            if client_id and client_id != basic_id:
                raise AuthenticationError("Conflicting client identities")
            client_id = basic_id
        client = await self.provider.get_client(str(client_id or ""))
        if client is None:
            raise AuthenticationError("Invalid client")
        if form.get("resource") not in {None, self.provider.resource}:
            raise AuthenticationError("Unknown resource")
        if client.token_endpoint_auth_method != "none":
            expected_basic = client.token_endpoint_auth_method == "client_secret_basic"
            if (
                expected_basic != basic.startswith("Basic ")
                or not isinstance(secret, str)
                or not client.client_secret
            ):
                raise AuthenticationError("Client authentication required")
            if not hmac.compare_digest(client.client_secret, digest(secret)):
                raise AuthenticationError("Invalid client authentication")
            if (
                client.client_secret_expires_at
                and client.client_secret_expires_at <= int(time.time())
            ):
                raise AuthenticationError("Client authentication expired")
        # The SDK also requires client_id in the token request form for Basic auth.
        from starlette.datastructures import FormData

        normalized = dict(form)
        normalized["client_id"] = client.client_id
        # RevocationRequest in SDK 1.x requires this field even for public clients.
        normalized.setdefault("client_secret", "")
        request._form = FormData(normalized)
        return client


def browser_principal(provider, request):
    from types import SimpleNamespace

    session = request.cookies.get(SESSION_COOKIE)
    if session:
        with get_connection(provider.settings.app_db_path) as db:
            row = db.execute(
                "SELECT account_id FROM mcp_login_sessions WHERE hash=? AND expires_at>?",
                (digest(session), int(time.time())),
            ).fetchone()
        if row:
            return SimpleNamespace(account_id=row[0])
    token = request.cookies.get("yabus_auth_token") or ""
    return authenticate_token(provider.settings, token) if token else None


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='zh-Hant'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} · YABus</title><style>"
        "body{font:17px/1.65 system-ui;background:#f2f5f4;color:#173b33;margin:0;padding:24px}"
        "main{max-width:600px;margin:6vh auto;background:white;padding:32px;border-radius:20px}"
        "button,a.button{display:inline-block;background:#176954;color:white;border:0;border-radius:8px;"
        "padding:12px 20px;font:inherit;margin:8px 8px 8px 0;text-decoration:none;cursor:pointer}"
        "button.secondary{background:#e6eeeb;color:#173b33}small{overflow-wrap:anywhere}a{color:#176954}"
        f"</style><main><p>YABus · 帳號連接</p><h1>{html.escape(title)}</h1>{body}</main></html>",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        },
    )


def cookie(response, request, name, value, *, path="/oauth"):
    response.set_cookie(
        name,
        value,
        max_age=600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path=path,
    )


def verify_csrf(provider, request, form):
    origin = request.headers.get("origin")
    expected = request.cookies.get(CSRF_COOKIE, "")
    provided = str(form.get("csrf", ""))
    if (
        origin != provider.issuer
        or not expected
        or not hmac.compare_digest(expected, provided)
    ):
        raise HTTPException(403, "Invalid consent form. Reload the page and try again.")


async def consent_page(provider, request):
    pending_id = request.query_params.get("request_id", "")
    pending = provider.pending(pending_id)
    client = await provider.get_client(pending["client_id"])
    principal = browser_principal(provider, request)
    params = AuthorizationParams.model_validate_json(pending["params"])
    body = f"<p><strong>{html.escape(client.client_name or 'MCP client')}</strong> 想要讀取你的 YABus 收藏與分類，並查詢收藏站牌的到站時間。</p>"
    body += f"<p>連接目的地：<small>{html.escape(urlsplit(str(params.redirect_uri)).netloc)}</small></p>"
    body += "<p>授權範圍：讀取已同步的收藏。有效期間最長 90 天，可隨時撤銷。</p>"
    if principal is None:
        body += "<p>請先登入存有收藏的 YABus 帳號。</p>"
        for provider_name, label in (("google", "Google"), ("discord", "Discord")):
            body += f"<a class='button' href='/oauth/login/{provider_name}?{urlencode({'request_id': pending_id})}'>使用 {label} 登入</a>"
    else:
        csrf = secrets.token_urlsafe(32)
        with get_connection(provider.settings.app_db_path) as db:
            identity = db.execute(
                "SELECT display_name,email FROM account_oauth_identities WHERE account_id=? ORDER BY provider LIMIT 1",
                (principal.account_id,),
            ).fetchone()
        label = (
            (identity["display_name"] or identity["email"])
            if identity
            else str(principal.account_id)
        )
        body += f"<p>目前帳號：<strong>{html.escape(label or str(principal.account_id))}</strong></p>"
        body += f"<form method='post' action='/oauth/consent'><input type='hidden' name='request_id' value='{html.escape(pending_id)}'>"
        body += f"<input type='hidden' name='csrf' value='{csrf}'><button name='decision' value='approve'>允許讀取收藏</button>"
        body += "<button class='secondary' name='decision' value='deny'>取消</button></form>"
        body += "<p>切換帳號： "
        for name in ("google", "discord"):
            body += f"<a href='/oauth/login/{name}?{urlencode({'request_id': pending_id})}'>{name.title()}</a> "
        body += "</p>"
        response = page("授權讀取收藏", body)
        cookie(response, request, CSRF_COOKIE, csrf)
        return response
    return page("連接你的收藏", body)


async def consent_submit(provider, request):
    form = await request.form()
    verify_csrf(provider, request, form)
    principal = browser_principal(provider, request)
    if principal is None:
        raise HTTPException(401, "Sign in again")
    if form.get("decision") not in {"approve", "deny"}:
        raise HTTPException(400, "Invalid decision")
    redirect = provider.complete_consent(
        str(form.get("request_id", "")),
        principal.account_id,
        form.get("decision") == "approve",
    )
    response = RedirectResponse(
        redirect,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
    if request.cookies.get(SESSION_COOKIE):
        with get_connection(provider.settings.app_db_path) as db:
            db.execute(
                "DELETE FROM mcp_login_sessions WHERE hash=?",
                (digest(request.cookies[SESSION_COOKIE]),),
            )
            db.commit()
    for name in (SESSION_COOKIE, CSRF_COOKIE, BROWSER_COOKIE):
        response.delete_cookie(name, path="/oauth")
    response.delete_cookie(BROWSER_COOKIE, path="/api/v1/auth")
    return response


async def login_start(provider, request):
    auth_provider = request.path_params["provider"]
    if auth_provider not in {"google", "discord"}:
        raise HTTPException(404)
    pending_id = request.query_params.get("request_id", "")
    provider.pending(pending_id)
    binding = secrets.token_urlsafe(32)
    with get_connection(provider.settings.app_db_path) as db:
        db.execute(
            "UPDATE mcp_pending SET browser_hash=? WHERE id=?",
            (digest(binding), pending_id),
        )
        db.commit()
    redirect = (
        provider.issuer + "/oauth/consent?" + urlencode({"request_id": pending_id})
    )
    state = create_oauth_state(
        provider.settings,
        provider=auth_provider,
        platform="web",
        redirect_uri=redirect,
        device_key=str(uuid4()),
    )
    response = RedirectResponse(
        build_authorization_url(provider.settings, provider=auth_provider, state=state),
        status_code=302,
    )
    cookie(response, request, BROWSER_COOKIE, binding, path="/api/v1/auth")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def finish_login(provider, request, result):
    """Handle only server-created MCP login states; never put app tokens in URLs."""
    parsed = urlsplit(result.redirect_uri)
    if result.redirect_uri.split("?", 1)[0] != provider.issuer + "/oauth/consent":
        return None
    if result.login_result is None:
        raise HTTPException(400, "Sign-in did not complete")
    # The upstream login creates an ordinary app token. Retire it immediately:
    # consent uses a short-lived server-side session with no app API privileges.
    principal = authenticate_token(provider.settings, result.login_result.token)
    if principal is None:
        raise HTTPException(401, "Sign-in did not complete")
    revoke_token(provider.settings, principal)
    pending_id = parse_qs(parsed.query).get("request_id", [""])[0]
    row = provider.pending(pending_id)
    binding = request.cookies.get(BROWSER_COOKIE, "")
    if (
        not binding
        or not row["browser_hash"]
        or not hmac.compare_digest(row["browser_hash"], digest(binding))
    ):
        raise HTTPException(403, "Sign-in must finish in the browser that started it")
    session = secrets.token_urlsafe(32)
    with get_connection(provider.settings.app_db_path) as db:
        db.execute(
            "INSERT INTO mcp_login_sessions VALUES(?,?,?)",
            (digest(session), principal.account_id, int(time.time()) + 600),
        )
        db.commit()
    response = RedirectResponse(
        result.redirect_uri,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
    cookie(response, request, SESSION_COOKIE, session)
    response.delete_cookie(BROWSER_COOKIE, path="/api/v1/auth")
    return response


async def connections(provider, request):
    principal = browser_principal(provider, request)
    if principal is None:
        return page(
            "管理 AI 授權",
            "<p>請先登入 YABus 網頁帳號，再回到此頁。</p><a class='button' href='/auth'>登入 YABus</a>",
        )
    if request.method == "POST":
        form = await request.form()
        verify_csrf(provider, request, form)
        provider.revoke_grant(str(form.get("grant_id", "")), principal.account_id)
        return RedirectResponse("/oauth/connections", status_code=303)
    csrf = secrets.token_urlsafe(32)
    body = "<p>以下應用程式可以讀取你的雲端收藏。撤銷後會立即停止存取。</p>"
    grants = provider.account_grants(principal.account_id)
    for grant in grants:
        body += f"<form method='post'><strong>{html.escape(grant['client_name'])}</strong> · 讀取收藏"
        body += f"<input type='hidden' name='grant_id' value='{html.escape(grant['id'])}'><input type='hidden' name='csrf' value='{csrf}'>"
        body += "<button class='secondary'>撤銷授權</button></form>"
    if not grants:
        body += "<p>目前沒有已授權的 AI 應用程式。</p>"
    body += "<p><a href='/account'>返回帳號</a></p>"
    response = page("管理 AI 授權", body)
    cookie(response, request, CSRF_COOKIE, csrf)
    return response
