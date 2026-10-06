from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, netcheck
from .manager import ServerManager
from .oidc import STATE_COOKIE, STATE_TTL, Oidc, OidcConfig, OidcError

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("uvicorn.error")  # shows up in the container log


@dataclass
class Settings:
    password: str
    data_dir: Path
    module_id: str | None = None   # legacy single-game default (PANEL_MODULE); seeds the first server
    demo: bool = False
    oidc: OidcConfig | None = None   # OIDC_* env; when set, PANEL_PASSWORD becomes optional
    autostart_delay: float = 60      # seconds after the container starts before the first auto-start server
    autostart_stagger: float = 30    # seconds between auto-start servers (they are heavy to boot together)

    @classmethod
    def from_env(cls) -> "Settings":
        pw, oidc = os.environ.get("PANEL_PASSWORD", ""), OidcConfig.from_env()
        if pw and len(pw) < 8:
            raise SystemExit("PANEL_PASSWORD must be at least 8 characters")
        if not pw and not oidc:
            raise SystemExit("Set PANEL_PASSWORD (min 8 chars) and/or the OIDC_* variables")
        def secs(name: str, default: float) -> float:
            try:
                return max(0.0, float(os.environ.get(name, default)))
            except ValueError:
                raise SystemExit(f"{name} must be a number of seconds")
        return cls(pw, Path(os.environ.get("PANEL_DATA", "/data")), os.environ.get("PANEL_MODULE") or None,
                   os.environ.get("PANEL_DEMO") == "1", oidc, secs("AUTOSTART_DELAY", 60), secs("AUTOSTART_STAGGER", 30))


def build_info() -> dict:
    """Which build is running: short commit and build date, baked into the image (see Dockerfile and ci.yml)."""
    return {"commit": os.environ.get("PANEL_COMMIT", "dev")[:7] or "dev", "built": os.environ.get("PANEL_BUILT", "")}


def create_app(settings: Settings, manager: ServerManager | None = None, oidc: Oidc | None = None) -> FastAPI:
    mgr = manager or ServerManager(settings.data_dir, settings.demo, settings.module_id)
    secret = auth.load_secret(settings.data_dir)
    limiter = auth.LoginLimiter()
    sso = oidc or (Oidc(settings.oidc, secret) if settings.oidc else None)
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(mgr.run_autostart(settings.autostart_delay, settings.autostart_stagger))
        yield
        task.cancel()

    app = FastAPI(title="Kosmos", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.manager = mgr

    def require_auth(request: Request) -> None:
        if not auth.valid_token(secret, request.cookies.get(auth.COOKIE)):
            raise HTTPException(401, "not logged in")

    protected = [Depends(require_auth)]

    def server(sid: str):
        try:
            return mgr.get(sid)
        except KeyError:
            raise HTTPException(404, "no such server")

    # ---- auth ----------------------------------------------------------
    def start_session(response: Response, secure: bool = False) -> None:
        response.set_cookie(auth.COOKIE, auth.make_token(secret), httponly=True, secure=secure,
                            samesite="strict", max_age=auth.TTL)

    @app.get("/api/auth")
    async def auth_options():
        return {"password": bool(settings.password), "oidc": {"name": sso.cfg.name} if sso else None}

    @app.post("/api/login")
    async def login(request: Request, response: Response, body: dict):
        if not settings.password:
            raise HTTPException(403, "password login is disabled")
        who = request.client.host if request.client else "?"
        if limiter.blocked(who):
            raise HTTPException(429, "too many attempts, wait a minute")
        if not hmac.compare_digest(str(body.get("password", "")).encode(), settings.password.encode()):
            limiter.fail(who)
            raise HTTPException(401, "wrong password")
        start_session(response)
        return {"ok": True}

    # ---- OpenID Connect sign-in (only when OIDC_* is configured) ----------
    def sso_error(msg: str) -> RedirectResponse:
        r = RedirectResponse("/?" + urlencode({"sso_error": msg}), status_code=303)
        r.delete_cookie(STATE_COOKIE)
        return r

    @app.get("/auth/login")
    async def sso_login():
        if not sso:
            raise HTTPException(404, "OIDC is not configured")
        try:
            url, state = await sso.begin()
        except OidcError as e:
            return sso_error(str(e))
        r = RedirectResponse(url, status_code=303)
        # Lax, not Strict: the cookie must come back on the redirect from the provider.
        r.set_cookie(STATE_COOKIE, state, httponly=True, samesite="lax", secure=sso.cfg.secure_cookies, max_age=STATE_TTL)
        return r

    @app.get("/auth/callback")
    async def sso_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
        if not sso:
            raise HTTPException(404, "OIDC is not configured")
        if error:
            return sso_error("The provider reported an error: " + error[:80])
        try:
            who = await sso.finish(code, state, request.cookies.get(STATE_COOKIE))
        except OidcError as e:
            return sso_error(str(e))
        log.info("OIDC login: %s", who)
        r = RedirectResponse("/", status_code=303)
        r.delete_cookie(STATE_COOKIE)
        start_session(r, sso.cfg.secure_cookies)
        return r

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    # ---- servers & catalogue -------------------------------------------
    @app.get("/api/version", dependencies=protected)
    async def version():
        return build_info()

    @app.get("/api/servers", dependencies=protected)
    async def list_servers():
        return {"servers": mgr.list()}

    @app.get("/api/games", dependencies=protected)
    async def games():
        return {"games": mgr.available()}

    @app.post("/api/servers", dependencies=protected)
    async def add_server(body: dict):
        try:
            return mgr.add(str(body.get("module", "")), body.get("name")).summary()
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.patch("/api/servers/{sid}", dependencies=protected)
    async def update_server(sid: str, body: dict):
        server(sid)
        if "name" in body:
            mgr.rename(sid, str(body["name"]))
        if "autostart" in body:
            if not isinstance(body["autostart"], bool):
                raise HTTPException(400, "autostart must be true or false")
            mgr.set_autostart(sid, body["autostart"])
        return server(sid).summary()

    @app.delete("/api/servers/{sid}", dependencies=protected)
    async def remove_server(sid: str):
        server(sid)
        await mgr.remove(sid)
        return {"ok": True, "note": "Game files were kept on disk."}

    # ---- one server ----------------------------------------------------
    @app.get("/api/servers/{sid}", dependencies=protected)
    async def detail(sid: str):
        s = server(sid)
        return {**s.module.describe(), **s.summary(),
                "autostart_delay": settings.autostart_delay, "autostart_stagger": settings.autostart_stagger}

    @app.get("/api/servers/{sid}/status", dependencies=protected)
    async def status(sid: str):
        return server(sid).supervisor.status()

    @app.get("/api/servers/{sid}/ports", dependencies=protected)
    async def ports(sid: str):
        return {"ports": netcheck.port_status(server(sid).module.ports)}

    async def control(sid: str, name: str):
        sup = server(sid).supervisor
        try:
            await getattr(sup, name)()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return sup.status()

    for _name in ("start", "stop", "restart", "update", "reinstall"):
        def _make(name: str):
            async def handler(sid: str):
                return await control(sid, name)
            return handler
        app.post(f"/api/servers/{{sid}}/{_name}", dependencies=protected)(_make(_name))

    @app.get("/api/servers/{sid}/logs", dependencies=protected)
    async def logs(sid: str, n: int = 200):
        return {"lines": server(sid).supervisor.tail(max(1, min(n, 2000)))}

    @app.get("/api/servers/{sid}/logs/stream", dependencies=protected)
    async def logs_stream(sid: str):
        sup = server(sid).supervisor

        async def gen():
            async for line in sup.follow():
                yield "data: " + line.replace("\n", " ") + "\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    def cfg_path(sid: str, name: str) -> Path:
        mod = server(sid).module
        if name not in mod.config_files:  # allow-list, so no path traversal
            raise HTTPException(404, "unknown config file")
        return mod.config_dir / name

    @app.get("/api/servers/{sid}/config/{name}", dependencies=protected)
    async def get_config(sid: str, name: str):
        p = cfg_path(sid, name)
        return {"name": name, "content": p.read_text(errors="replace") if p.exists() else ""}

    @app.put("/api/servers/{sid}/config/{name}", dependencies=protected)
    async def put_config(sid: str, name: str, body: dict):
        p = cfg_path(sid, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(body.get("content", "")))
        return {"ok": True, "note": "restart the server to apply"}

    @app.post("/api/servers/{sid}/actions/{name}", dependencies=protected)
    async def action(sid: str, name: str, body: dict):
        fn = server(sid).module.actions().get(name)
        if not fn:
            raise HTTPException(404, "unknown action")
        try:
            return await fn(body, server(sid).supervisor.log)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/")
    async def index():
        # The build is written into the page itself, so the UI can show which version the browser is really running
        # and notice when the server has been updated underneath it. no-cache: a redeploy is never masked by a cached page.
        b = build_info()
        html = (STATIC / "index.html").read_text().replace('name="panel-build" content=""', f'name="panel-build" content="{b["commit"]}|{b["built"]}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
