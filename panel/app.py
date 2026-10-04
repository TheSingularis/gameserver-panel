from __future__ import annotations

import asyncio
import hmac
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from . import auth
from .modules import load_module
from .modules.base import GameModule
from .supervisor import Supervisor

STATIC = Path(__file__).parent / "static"


@dataclass
class Settings:
    password: str
    data_dir: Path
    module_id: str = "theship"

    @classmethod
    def from_env(cls) -> "Settings":
        pw = os.environ.get("PANEL_PASSWORD", "")
        if len(pw) < 8:
            raise SystemExit("PANEL_PASSWORD must be set (min 8 chars)")
        return cls(pw, Path(os.environ.get("PANEL_DATA", "/data")), os.environ.get("PANEL_MODULE", "theship"))


def create_app(settings: Settings, module: GameModule | None = None) -> FastAPI:
    module = module or load_module(settings.module_id, settings.data_dir / "config", settings.data_dir / "server")
    sup = Supervisor(module)
    secret = auth.load_secret(settings.data_dir)
    limiter = auth.LoginLimiter()
    app = FastAPI(title="gameserver-panel", docs_url=None, redoc_url=None)
    app.state.supervisor = sup

    def require_auth(request: Request) -> None:
        if not auth.valid_token(secret, request.cookies.get(auth.COOKIE)):
            raise HTTPException(401, "not logged in")

    protected = [Depends(require_auth)]

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

    @app.get("/api/module", dependencies=protected)
    async def get_module():
        return module.describe()

    @app.get("/api/status", dependencies=protected)
    async def status():
        return sup.status()

    async def control(fn):
        try:
            await fn()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return sup.status()

    @app.post("/api/start", dependencies=protected)
    async def start():
        return await control(sup.start)

    @app.post("/api/stop", dependencies=protected)
    async def stop():
        return await control(sup.stop)

    @app.post("/api/restart", dependencies=protected)
    async def restart():
        return await control(sup.restart)

    @app.post("/api/update", dependencies=protected)
    async def update():
        return await control(sup.update)

    @app.get("/api/logs", dependencies=protected)
    async def logs(n: int = 200):
        return {"lines": sup.tail(max(1, min(n, 2000)))}

    @app.get("/api/logs/stream", dependencies=protected)
    async def logs_stream():
        async def gen():
            async for line in sup.follow():
                yield "data: " + line.replace("\n", " ") + "\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    def cfg_path(name: str) -> Path:
        if name not in module.config_files:  # allow-list, so no path traversal
            raise HTTPException(404, "unknown config file")
        return module.config_dir / name

    @app.get("/api/config/{name}", dependencies=protected)
    async def get_config(name: str):
        p = cfg_path(name)
        return {"name": name, "content": p.read_text(errors="replace") if p.exists() else ""}

    @app.put("/api/config/{name}", dependencies=protected)
    async def put_config(name: str, body: dict):
        p = cfg_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(body.get("content", "")))
        return {"ok": True, "note": "restart the server to apply"}

    @app.post("/api/actions/{name}", dependencies=protected)
    async def action(name: str, body: dict):
        fn = module.actions().get(name)
        if not fn:
            raise HTTPException(404, "unknown action")
        try:
            return await fn(body, sup.log)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app
