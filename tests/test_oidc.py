"""OIDC login against an in-process fake Keycloak (discovery, token, JWKS, userinfo)."""
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from panel.manager import ServerManager
from panel.oidc import Oidc, OidcConfig
from tests.conftest import FAKE_CATALOG

ISS = "https://kc.test/realms/home"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "k1", "alg": "RS256", "use": "sig"}


class FakeProvider:
    """Remembers the nonce from the authorize step so the ID token it issues matches."""
    def __init__(self, **claims):
        self.claims, self.nonce, self.challenge, self.sign_key = claims, None, None, KEY
        self.token_status = 200

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISS, "authorization_endpoint": ISS + "/auth", "token_endpoint": ISS + "/token",
                                             "jwks_uri": ISS + "/certs", "userinfo_endpoint": ISS + "/userinfo"})
        if path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [JWK]})
        if path.endswith("/userinfo"):
            return httpx.Response(200, json={"sub": "u1", **self.claims.get("userinfo", {})})
        if path.endswith("/token"):
            form = parse_qs(req.content.decode())
            assert form["code"] == ["good-code"] and form["code_verifier"]
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            idc = {"iss": ISS, "aud": "panel", "sub": "u1", "exp": int(time.time()) + 300, "nonce": self.nonce,
                   **{k: v for k, v in self.claims.items() if k != "userinfo"}}
            tok = jwt.encode(idc, self.sign_key, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": tok, "access_token": "at"})
        return httpx.Response(404)


def make(tmp_path, provider, **cfg):
    config = OidcConfig(ISS, "panel", "https://panel.test", **cfg)
    oidc = Oidc(config, b"s" * 32, httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)))
    app = create_app(Settings("", tmp_path, oidc=config), ServerManager(tmp_path, modules=FAKE_CATALOG), oidc)
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://panel.test")


async def login(c, provider, code="good-code"):
    r = await c.get("/auth/login")
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["https://panel.test/auth/callback"]
    provider.nonce = q["nonce"][0]
    return await c.get("/auth/callback", params={"code": code, "state": q["state"][0]})


async def test_allowed_user_gets_a_session(tmp_path):
    p = FakeProvider(preferred_username="Jordan")
    async with make(tmp_path, p, allowed_users={"jordan"}) as c:
        assert (await c.get("/api/servers")).status_code == 401
        r = await login(c, p)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert (await c.get("/api/servers")).status_code == 200


async def test_group_from_userinfo_is_accepted(tmp_path):
    p = FakeProvider(preferred_username="sam", userinfo={"groups": ["/gamers"]})
    async with make(tmp_path, p, allowed_groups={"gamers"}) as c:
        await login(c, p)
        assert (await c.get("/api/servers")).status_code == 200


async def test_user_outside_allow_list_is_refused(tmp_path):
    p = FakeProvider(preferred_username="mallory")
    async with make(tmp_path, p, allowed_users={"jordan"}) as c:
        r = await login(c, p)
        assert "sso_error" in r.headers["location"] and "not+allowed" in r.headers["location"]
        assert (await c.get("/api/servers")).status_code == 401


async def test_forged_token_signature_is_refused(tmp_path):
    p = FakeProvider(preferred_username="jordan")
    p.sign_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with make(tmp_path, p, allowed_users={"jordan"}) as c:
        r = await login(c, p)
        assert "sso_error" in r.headers["location"]
        assert (await c.get("/api/servers")).status_code == 401


async def test_wrong_state_and_missing_cookie_are_refused(tmp_path):
    p = FakeProvider(preferred_username="jordan")
    async with make(tmp_path, p, allowed_users={"jordan"}) as c:
        await c.get("/auth/login")
        r = await c.get("/auth/callback", params={"code": "good-code", "state": "forged"})
        assert "sso_error" in r.headers["location"]
    async with make(tmp_path, p, allowed_users={"jordan"}) as fresh:  # no state cookie at all
        r = await fresh.get("/auth/callback", params={"code": "good-code", "state": "x"})
        assert "sso_error" in r.headers["location"]


async def test_provider_error_and_options(tmp_path):
    p = FakeProvider(preferred_username="jordan")
    async with make(tmp_path, p, allowed_users={"jordan"}, name="Keycloak") as c:
        r = await c.get("/auth/callback", params={"error": "access_denied"})
        assert "sso_error" in r.headers["location"]
        assert (await c.get("/api/auth")).json() == {"password": False, "oidc": {"name": "Keycloak"}}
        assert (await c.post("/api/login", json={"password": "x"})).status_code == 403


def test_config_requires_an_allow_list(monkeypatch):
    for k, v in {"OIDC_ISSUER": ISS, "OIDC_CLIENT_ID": "panel", "OIDC_PUBLIC_URL": "https://panel.test"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit):
        OidcConfig.from_env()
    monkeypatch.setenv("OIDC_ALLOWED_GROUPS", "Gamers, admins")
    assert OidcConfig.from_env().allowed_groups == {"gamers", "admins"}
