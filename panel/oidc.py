"""OpenID Connect sign-in (authorization code flow with PKCE), tested against Keycloak-style providers.

The panel is the relying party: it redirects to the provider, gets a code back, exchanges it at the
token endpoint, verifies the ID token signature against the provider's JWKS, then checks the user
against an allow-list. A successful login ends in the same signed session cookie as password login.

Configuration (environment):
  OIDC_ISSUER          e.g. https://keycloak.home.lan/realms/home       (enables OIDC)
  OIDC_CLIENT_ID       client id registered at the provider
  OIDC_CLIENT_SECRET   optional; omit for a public client (PKCE only)
  OIDC_PUBLIC_URL      how browsers reach this panel, e.g. https://panel.home.lan; the redirect URI is <it>/auth/callback
  OIDC_ALLOWED_USERS   comma list of usernames or emails allowed in
  OIDC_ALLOWED_GROUPS  comma list of group / realm-role names allowed in
  OIDC_ALLOW_ANY=1     let every authenticated user of the realm in (only if you really mean it)
  OIDC_NAME            label for the sign-in button (default "SSO")
Without an allow-list the panel refuses to start: a realm often has users who should not run game servers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
import jwt

STATE_COOKIE = "gsp_oidc"
STATE_TTL = 600
CALLBACK_PATH = "/auth/callback"
SIGNING_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512"]


class OidcError(Exception):
    """A login failure that is safe to show to the user."""


def _csv(name: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@dataclass
class OidcConfig:
    issuer: str
    client_id: str
    public_url: str
    client_secret: str | None = None
    allowed_users: set[str] = field(default_factory=set)
    allowed_groups: set[str] = field(default_factory=set)
    allow_any: bool = False
    name: str = "SSO"

    @property
    def redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + CALLBACK_PATH

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.lower().startswith("https://")

    @classmethod
    def from_env(cls) -> "OidcConfig | None":
        issuer = os.environ.get("OIDC_ISSUER", "").strip().rstrip("/")
        if not issuer:
            return None
        client_id, public = os.environ.get("OIDC_CLIENT_ID", "").strip(), os.environ.get("OIDC_PUBLIC_URL", "").strip()
        if not client_id or not public:
            raise SystemExit("OIDC_ISSUER is set, so OIDC_CLIENT_ID and OIDC_PUBLIC_URL are required")
        cfg = cls(issuer, client_id, public, os.environ.get("OIDC_CLIENT_SECRET") or None,
                  _csv("OIDC_ALLOWED_USERS"), _csv("OIDC_ALLOWED_GROUPS"),
                  os.environ.get("OIDC_ALLOW_ANY") == "1", os.environ.get("OIDC_NAME", "SSO").strip() or "SSO")
        if not (cfg.allowed_users or cfg.allowed_groups or cfg.allow_any):
            raise SystemExit("Set OIDC_ALLOWED_USERS and/or OIDC_ALLOWED_GROUPS (or OIDC_ALLOW_ANY=1) so the panel knows who may log in")
        return cfg


class Oidc:
    def __init__(self, cfg: OidcConfig, secret: bytes, http: httpx.AsyncClient | None = None):
        self.cfg, self._secret = cfg, secret
        self._http = http or httpx.AsyncClient(timeout=10)
        self._meta: dict | None = None

    # ---- signed state cookie (state + nonce + PKCE verifier survive the round trip to the provider) ----
    def _seal(self, data: dict) -> str:
        body = _b64(json.dumps(data).encode())
        return body + "." + hmac.new(self._secret, b"oidc:" + body.encode(), hashlib.sha256).hexdigest()

    def _open(self, cookie: str | None) -> dict:
        try:
            body, sig = (cookie or "").split(".", 1)
            good = hmac.new(self._secret, b"oidc:" + body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, good):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if data["exp"] < time.time():
                raise ValueError
            return data
        except Exception:
            raise OidcError("The login attempt expired. Please try again.") from None

    async def metadata(self) -> dict:
        if self._meta is None:
            try:
                r = await self._http.get(self.cfg.issuer + "/.well-known/openid-configuration")
                r.raise_for_status()
                meta = r.json()
            except Exception:
                raise OidcError("Could not reach the sign-in provider.") from None
            if meta.get("issuer", "").rstrip("/") != self.cfg.issuer:
                raise OidcError("The sign-in provider reported an unexpected issuer.")
            self._meta = meta
        return self._meta

    async def begin(self) -> tuple[str, str]:
        """Returns (provider URL to redirect to, value for the state cookie)."""
        meta = await self.metadata()
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        url = meta["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code", "client_id": self.cfg.client_id, "redirect_uri": self.cfg.redirect_uri,
            "scope": "openid profile email", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        return url, self._seal({"s": state, "n": nonce, "v": verifier, "exp": time.time() + STATE_TTL})

    async def finish(self, code: str | None, state: str | None, cookie: str | None) -> str:
        """Completes the login and returns the user's display name; raises OidcError if refused."""
        saved = self._open(cookie)
        if not code or not state or not hmac.compare_digest(state, saved["s"]):
            raise OidcError("The login response did not match this browser session. Please try again.")
        meta = await self.metadata()
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.cfg.redirect_uri,
                "client_id": self.cfg.client_id, "code_verifier": saved["v"]}
        if self.cfg.client_secret:
            form["client_secret"] = self.cfg.client_secret
        try:
            r = await self._http.post(meta["token_endpoint"], data=form)
            tokens = r.json()
        except Exception:
            raise OidcError("Could not reach the sign-in provider.") from None
        if r.status_code != 200 or "id_token" not in tokens:
            raise OidcError("The provider refused the login.")
        claims = await self._verify(tokens["id_token"], meta, saved["n"])
        if tokens.get("access_token") and meta.get("userinfo_endpoint"):
            try:  # userinfo often carries the groups the ID token leaves out
                u = await self._http.get(meta["userinfo_endpoint"], headers={"Authorization": "Bearer " + tokens["access_token"]})
                if u.status_code == 200 and u.json().get("sub") == claims["sub"]:
                    claims = {**u.json(), **claims}
            except Exception:
                pass
        return self._authorize(claims)

    async def _verify(self, id_token: str, meta: dict, nonce: str) -> dict:
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
            jwks = (await self._http.get(meta["jwks_uri"])).json()["keys"]
            jwk = next(k for k in jwks if k.get("kid") == kid or kid is None)
            claims = jwt.decode(id_token, jwt.PyJWK(jwk).key, algorithms=SIGNING_ALGS, audience=self.cfg.client_id,
                                issuer=meta["issuer"], options={"require": ["exp", "iss", "aud", "sub"]})
        except Exception:
            raise OidcError("The provider's login token could not be verified.") from None
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("The login response did not match this browser session. Please try again.")
        return claims

    def _authorize(self, claims: dict) -> str:
        names = {str(claims.get(k)).lower() for k in ("preferred_username", "email") if claims.get(k)}
        groups = {str(g).lstrip("/").lower() for g in claims.get("groups") or []}
        groups |= {str(r).lower() for r in (claims.get("realm_access") or {}).get("roles", [])}
        who = claims.get("preferred_username") or claims.get("email") or claims["sub"]
        if self.cfg.allow_any or names & self.cfg.allowed_users or groups & self.cfg.allowed_groups:
            return str(who)
        raise OidcError(f"{who} is signed in but is not allowed to use this panel.")
