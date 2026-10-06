"""Minecraft: Java Edition dedicated server, as Paper or Vanilla.

Install downloads server.jar and verifies its checksum: Vanilla from Mojang's version manifest (SHA-1), Paper from
PaperMC's download service (SHA-256). It needs a Java runtime on PATH (or MINECRAFT_JAVA); the container image ships one.
Which flavor, which Minecraft version and how much memory live in `panel.properties`, shown on the Config tab like any
other settings file; `server.properties` and `eula.txt` live on the config volume and are linked into the server
folder, so a clean reinstall can never lose them. Worlds, player lists and plugins are declared in `persistent_paths`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import httpx

from ..base import ConfigField, GameModule, LaunchSpec, LogFn, Port

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
USER_AGENT = "gameserver-panel/1.0 (https://github.com/TheSingularis/gameserver-panel)"  # PaperMC requires a contact UA
EULA_URL = "https://www.minecraft.net/en-us/eula"
FLAVORS = ("paper", "vanilla")
JAR = "server.jar"
MARKER = ".panel-install.json"  # which flavor/version/build the jar is, so "update" knows if anything changed
SIMPLE_VERSION = re.compile(r"^\d+(\.\d+)*$")  # skips snapshots, pre-releases and release candidates
MEMORY = re.compile(r"^\d{1,5}[MG]$", re.I)
LEVEL_NAME = re.compile(r"^[\w .-]{1,64}$")

SERVER_DEFAULTS = """\
motd=A Minecraft Server
server-port=25565
max-players=20
gamemode=survival
difficulty=easy
online-mode=true
white-list=false
pvp=true
view-distance=10
level-name=world
level-seed=
"""

PANEL_DEFAULTS = """\
# Panel settings for this server. The type, version and memory are read when you run "Check for updates" / start.
# flavor: paper (plugins, faster) or vanilla (Mojang's own server). version: latest, or an exact one such as 1.21.8.
flavor=paper
version=latest
memory=2G
"""


def parse_properties(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


class Minecraft(GameModule):
    id = "minecraft"
    name = "Minecraft: Java Edition"
    description = "Java Edition server, Paper (plugins, faster) or Vanilla (Mojang's own). Needs the EULA accepted once."
    ports = [
        Port(25565, 25565, "tcp", "Java Edition clients connect here"),
        Port(25565, 25565, "udp", "only used if you turn on enable-query", required=False),
    ]
    config_files = ["server.properties", "panel.properties"]
    config_schema = {
        "server.properties": [
            ConfigField("motd", "Server message", help="Shown under the name in the multiplayer list"),
            ConfigField("server-port", "Port", "number", help="If you change this, forward the new port"),
            ConfigField("max-players", "Max players", "number"),
            ConfigField("gamemode", "Game mode", "select", options=("survival", "creative", "adventure", "spectator")),
            ConfigField("difficulty", "Difficulty", "select", options=("peaceful", "easy", "normal", "hard")),
            ConfigField("online-mode", "Check players against Mojang accounts", "bool",
                        help="Turn off only for a private LAN: it lets anyone join under any name"),
            ConfigField("white-list", "Whitelist", "bool", help="Only players on the whitelist can join"),
            ConfigField("pvp", "Players can hurt each other", "bool"),
            ConfigField("view-distance", "View distance (chunks)", "number", help="Lower is lighter on the server"),
            ConfigField("level-name", "World folder name", help="Changing this starts a new world"),
            ConfigField("level-seed", "World seed", help="Only used when a world is first created"),
        ],
        "panel.properties": [
            ConfigField("flavor", "Server type", "select", options=FLAVORS,
                        help="Paper adds plugin support; Vanilla is Mojang's own. Applied by Check for updates."),
            ConfigField("version", "Minecraft version", help="latest, or an exact one such as 1.21.8. Applied by Check for updates."),
            ConfigField("memory", "Memory for Java", help="For example 2G or 4096M"),
        ],
    }
    stop_timeout = 90.0  # saving a big world on shutdown can take a while; killing it mid-save corrupts chunks

    def __init__(self, config_dir: Path, server_dir: Path):
        super().__init__(config_dir, server_dir)
        self._transport: httpx.AsyncBaseTransport | None = None  # tests inject a fake network here

    # -- settings ----------------------------------------------------------
    def prepare(self) -> None:
        """Create the settings files so the Config tab has something to show before the first install."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        for name, text in (("server.properties", SERVER_DEFAULTS), ("panel.properties", PANEL_DEFAULTS)):
            if not (self.config_dir / name).exists():
                (self.config_dir / name).write_text(text)

    def _props(self, name: str) -> dict[str, str]:
        try:
            return parse_properties((self.config_dir / name).read_text(errors="replace"))
        except OSError:
            return {}

    def _panel_settings(self) -> tuple[str, str, str]:
        p = self._props("panel.properties")
        flavor = p.get("flavor", "paper").lower()
        if flavor not in FLAVORS:
            raise RuntimeError(f"unknown server type {flavor!r} in panel.properties (use paper or vanilla)")
        return flavor, p.get("version", "latest").strip() or "latest", self._memory()

    def _memory(self) -> str:
        memory = self._props("panel.properties").get("memory", "2G")
        return memory if MEMORY.fullmatch(memory) else "2G"

    @property
    def persistent_paths(self) -> list[str]:  # type: ignore[override]
        level = self._props("server.properties").get("level-name", "world")
        if not LEVEL_NAME.fullmatch(level) or level in (".", ".."):
            level = "world"
        # Worlds (Vanilla/old Paper keep the nether and end beside the main world), who may join, and plugins with their settings.
        return [level, f"{level}_nether", f"{level}_the_end", "ops.json", "whitelist.json",
                "banned-players.json", "banned-ips.json", "plugins", "config"]

    # -- EULA --------------------------------------------------------------
    def eula_accepted(self) -> bool:
        return self._props("eula.txt").get("eula", "").lower() == "true"

    def prompts(self) -> list[dict]:
        if self.eula_accepted():
            return []
        return [{"id": "eula", "action": "accept_eula", "link": EULA_URL, "button": "I accept the Minecraft EULA",
                 "text": "Minecraft will not start until you accept Mojang's End User License Agreement. "
                         "Read it, then accept: this writes eula=true for this server."}]

    def actions(self):
        return {"accept_eula": self._accept_eula}

    async def _accept_eula(self, params: dict, log: LogFn) -> dict:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "eula.txt").write_text(f"# Accepted through the panel. See {EULA_URL}\neula=true\n")
        log("[panel] Minecraft EULA accepted")
        return {"ok": True}

    # -- install -----------------------------------------------------------
    def is_installed(self) -> bool:
        return (self.server_dir / JAR).is_file()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, headers={"User-Agent": USER_AGENT},
                                 follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))

    async def _json(self, client: httpx.AsyncClient, url: str):
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

    async def _resolve_vanilla(self, client: httpx.AsyncClient, version: str) -> dict:
        manifest = await self._json(client, MOJANG_MANIFEST)
        want = manifest["latest"]["release"] if version == "latest" else version
        entry = next((v for v in manifest["versions"] if v["id"] == want), None)
        if not entry:
            raise RuntimeError(f"Mojang has no Minecraft version {want!r}")
        server = (await self._json(client, entry["url"])).get("downloads", {}).get("server")
        if not server:
            raise RuntimeError(f"Minecraft {want} has no dedicated server download")
        return {"version": want, "build": "", "url": server["url"], "algo": "sha1", "sum": server["sha1"]}

    async def _resolve_paper(self, client: httpx.AsyncClient, version: str) -> dict:
        if version != "latest":
            candidates = [version]
        else:
            listed = (await self._json(client, PAPER_API)).get("versions", [])
            flat = [v for g in (listed.values() if isinstance(listed, dict) else [listed]) for v in (g if isinstance(g, list) else [g])]
            candidates = sorted({v for v in flat if isinstance(v, str) and SIMPLE_VERSION.fullmatch(v)}, key=version_key, reverse=True)
        for v in candidates:  # newest first; a brand-new version may not have a stable build yet
            builds = await self._json(client, f"{PAPER_API}/versions/{v}/builds")
            builds = builds.get("builds", []) if isinstance(builds, dict) else builds
            stable = [b for b in builds if str(b.get("channel", "")).upper() == "STABLE" and "server:default" in b.get("downloads", {})]
            if stable:
                best = max(stable, key=lambda b: int(b["id"]))
                d = best["downloads"]["server:default"]
                return {"version": v, "build": str(best["id"]), "url": d["url"], "algo": "sha256", "sum": d["checksums"]["sha256"]}
        raise RuntimeError(f"Paper has no stable build for {version if version != 'latest' else 'any version'}")

    async def _download(self, client: httpx.AsyncClient, url: str, dest: Path, algo: str, expected: str) -> None:
        digest = hashlib.new(algo)
        done = 0
        try:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                with dest.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        self.on_progress(done * 100 / total if total else None, "Downloading")
            if digest.hexdigest().lower() != expected.lower():
                raise RuntimeError(f"downloaded server.jar failed its {algo.upper()} check; not using it")
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

    async def install(self, log: LogFn) -> None:
        self.prepare()
        self.server_dir.mkdir(parents=True, exist_ok=True)
        flavor, version, _ = self._panel_settings()
        self.on_progress(None, "Looking up the latest build")
        async with self._client() as client:
            found = await (self._resolve_paper if flavor == "paper" else self._resolve_vanilla)(client, version)
            want = {"flavor": flavor, "version": found["version"], "build": found["build"], "sum": found["sum"]}
            try:
                have = json.loads((self.server_dir / MARKER).read_text())
            except (OSError, ValueError):
                have = None
            if have == want and self.is_installed():
                log(f"[panel] Minecraft {flavor} {found['version']} is already up to date")
                return
            log(f"[panel] downloading {flavor} {found['version']}" + (f" build {found['build']}" if found["build"] else ""))
            part = self.server_dir / (JAR + ".part")
            await self._download(client, found["url"], part, found["algo"], found["sum"])
            os.replace(part, self.server_dir / JAR)
        (self.server_dir / MARKER).write_text(json.dumps(want))
        self.on_progress(100.0, "Done")
        log(f"[panel] installed {flavor} {found['version']}")

    # -- launch ------------------------------------------------------------
    def _link(self, name: str) -> None:
        """Make server_dir/<name> point at the copy on the config volume (moving a real file there first if it is the only one)."""
        src, dst = self.config_dir / name, self.server_dir / name
        if dst.exists() and not dst.is_symlink():
            if not src.exists():
                shutil.move(str(dst), str(src))
            else:
                dst.unlink()
        if dst.is_symlink() and Path(os.readlink(dst)) != src:
            dst.unlink()
        if not dst.is_symlink() and src.exists():
            dst.symlink_to(src)

    @staticmethod
    def _find_java() -> str:
        java = os.environ.get("MINECRAFT_JAVA") or shutil.which("java")
        if not java:
            raise RuntimeError("Java was not found in this container (set MINECRAFT_JAVA, or use an image that includes Java)")
        return java

    def launch_spec(self) -> LaunchSpec:
        if not self.eula_accepted():
            raise RuntimeError("Accept the Minecraft EULA first (use the button on this server's page).")
        java = self._find_java()
        self.prepare()
        self.server_dir.mkdir(parents=True, exist_ok=True)
        for name in ("server.properties", "eula.txt"):
            self._link(name)
        return LaunchSpec([java, f"-Xmx{self._memory()}", "-jar", JAR, "--nogui"], self.server_dir)
