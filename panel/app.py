from __future__ import annotations

import asyncio
import hmac
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, netcheck
from .manager import ServerManager

STATIC = Path(__file__).parent / "static"


@dataclass
class Settings:
    password: str
    data_dir: Path
    module_id: str | None = None   # legacy single-game default (PANEL_MODULE); seeds the first server
    demo: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        pw = os.environ.get("PANEL_PASSWORD", "")
        if len(pw) < 8:
            raise SystemExit("PANEL_PASSWORD must be set (min 8 chars)")
        return cls(pw, Path(os.environ.get("PANEL_DATA", "/data")), os.environ.get("PANEL_MODULE") or None,
                   os.environ.get("PANEL_DEMO") == "1")


def create_app(settings: Settings, manager: ServerManager | None = None) -> FastAPI:
    mgr = manager or ServerManager(settings.data_dir, settings.demo, settings.module_id)
    secret = auth.load_secret(settings.data_dir)
    limiter = auth.LoginLimiter()
    app = FastAPI(title="gameserver-panel", docs_url=None, redoc_url=None)
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
    @app.post("/api/login")
    async def login(request: Request, response: Response, body: dict):
        who = request.client.host if request.client else "?"
        if limiter.blocked(who):
            raise HTTPException(429, "too many attempts, wait a minute")
        if not hmac.compare_digest(str(body.get("password", "")).encode(), settings.password.encode()):
            limiter.fail(who)
            raise HTTPException(401, "wrong password")
        response.set_cookie(auth.COOKIE, auth.make_token(secret), httponly=True,
                            samesite="strict", max_age=auth.TTL)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    # ---- servers & catalogue -------------------------------------------
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
    async def rename_server(sid: str, body: dict):
        server(sid)
        return mgr.rename(sid, str(body.get("name", ""))).summary()

    @app.delete("/api/servers/{sid}", dependencies=protected)
    async def remove_server(sid: str):
        server(sid)
        await mgr.remove(sid)
        return {"ok": True, "note": "Game files were kept on disk."}

    # ---- one server ----------------------------------------------------
    @app.get("/api/servers/{sid}", dependencies=protected)
    async def detail(sid: str):
        s = server(sid)
        return {**s.module.describe(), **s.summary()}

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

    for _name in ("start", "stop", "restart", "update"):
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
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
