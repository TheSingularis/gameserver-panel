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
from typing import Callable

import httpx

from ..base import ConfigField, GameModule, LaunchSpec, LogFn, Port
from ...archive import UnsafeArchive, safe_extract

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests put a fake network here
USER_AGENT = "gameserver-panel/1.0 (https://github.com/TheSingularis/gameserver-panel)"  # PaperMC requires a contact UA
EULA_URL = "https://www.minecraft.net/en-us/eula"
FLAVORS = ("paper", "vanilla", "pack")
JAVA_CHOICES = ("auto", "8", "17", "21", "25")
JAVA_HOMES = Path(os.environ.get("MINECRAFT_JAVA_ROOT", "/opt/java"))  # <root>/<major>/bin/java, as laid out by the image
PACK_MARKER = ".panel-pack.json"
START_SCRIPTS = ("run.sh", "start.sh", "startserver.sh", "ServerStart.sh", "start-server.sh", "launch.sh")
JAR = "server.jar"
MARKER = ".panel-install.json"  # which flavor/version/build the jar is, so "update" knows if anything changed
VERSION_CHOICE = re.compile(r"^(latest|\d+(\.\d+)*)$")  # what a person may ask for when creating a server
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
# flavor: paper (plugins, faster), vanilla (Mojang's own) or pack (a modpack server zip you upload).
# version: latest, or an exact one such as 1.21.8 (paper/vanilla only).
flavor={flavor}
version={version}
memory=2G
java=auto
start_file=
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


def java_major_for(mc: str) -> int:
    """Which Java a Minecraft version needs: 1.16 and older 8, 1.17-1.20.4 17, 1.20.5-1.21.x 21, 26.x and newer 25."""
    parts = [int(p) for p in re.findall(r"\d+", mc)[:3]]
    if not parts:
        return 21
    if parts[0] != 1:
        return 25 if parts[0] >= 26 else 21
    minor, patch = (parts + [0, 0])[1], (parts + [0, 0])[2]
    if minor <= 16:
        return 8
    if minor < 20 or (minor == 20 and patch < 5):
        return 17
    return 21


def guess_mc_version(root: Path) -> str | None:
    """Best-effort Minecraft version of an unpacked server, from the folders Forge/NeoForge/Paper leave behind."""
    for pattern in ("libraries/net/minecraftforge/forge/*", "libraries/net/minecraft/server/*", "versions/*"):
        for d in sorted(root.glob(pattern)):
            m = re.match(r"(1\.\d+(?:\.\d+)?|\d{2}\.\d+(?:\.\d+)?)(?:-|$)", d.name)
            if m and d.is_dir():
                return m.group(1)
    for d in sorted(root.glob("libraries/net/neoforged/neoforge/*")):
        m = re.match(r"(\d+)\.(\d+)\.", d.name)  # NeoForge 21.1.x is Minecraft 1.21.1
        if m and d.is_dir():
            return f"1.{m.group(1)}.{m.group(2)}" if int(m.group(1)) < 26 else f"{m.group(1)}.{m.group(2)}"
    return None


def detect_entry(root: Path) -> dict | None:
    """How a server pack starts: its own script, a Forge/NeoForge args file, or a launcher jar. None if unclear."""
    for name in START_SCRIPTS:
        if (root / name).is_file():
            return {"kind": "script", "entry": name}
    for pattern in ("libraries/net/minecraftforge/forge/*/unix_args.txt", "libraries/net/neoforged/neoforge/*/unix_args.txt"):
        found = sorted(root.glob(pattern))
        if found:
            return {"kind": "args", "entry": found[-1].relative_to(root).as_posix()}
    if (root / "fabric-server-launch.jar").is_file():
        return {"kind": "jar", "entry": "fabric-server-launch.jar"}
    jars = [p.name for p in root.glob("*.jar") if "installer" not in p.name.lower()]
    preferred = [j for j in jars if re.match(r"(forge|neoforge|server|minecraft_server|paper|spigot)", j, re.I)]
    pick = preferred if len(preferred) == 1 else jars if len(jars) == 1 else []
    return {"kind": "jar", "entry": pick[0]} if pick else None


def kind_of(entry: str) -> str:
    return "script" if entry.endswith(".sh") else "args" if entry.endswith(".txt") else "jar"


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
                        help="Paper adds plugin support; Vanilla is Mojang's own; pack runs a modpack server zip you upload (Advanced tab)."),
            ConfigField("version", "Minecraft version", help="latest, or an exact one such as 1.21.8. Applied by Check for updates.",
                        choices_from=True, older_warning=True, applies_when={"flavor": ["paper", "vanilla"]}),
            ConfigField("memory", "Memory for Java", help="For example 2G or 4096M"),
            ConfigField("java", "Java version", "select", options=JAVA_CHOICES,
                        help="auto picks the Java your Minecraft version needs (8, 17, 21 or 25)"),
            ConfigField("start_file", "Start file (modpacks)",
                        help="Only if the panel could not tell how your pack starts: its script, args file or jar, e.g. run.sh"),
        ],
    }
    stop_timeout = 90.0  # saving a big world on shutdown can take a while; killing it mid-save corrupts chunks
    create_options = [
        {"key": "flavor", "label": "Server type", "choices": list(FLAVORS), "default": "paper",
         "labels": {"paper": "Paper (plugins, faster)", "vanilla": "Vanilla (Mojang's own)",
                    "pack": "Modpack server zip (Forge, NeoForge, Fabric)"}},
        # Free-form (any release), with a list the page loads from `option_choices`; a modpack brings its own version.
        {"key": "version", "label": "Minecraft version", "default": "latest", "pattern": VERSION_CHOICE.pattern,
         "hint": "use latest or a version like 1.21.8", "choices_from": True, "applies_when": {"flavor": ["paper", "vanilla"]}},
    ]

    def __init__(self, config_dir: Path, server_dir: Path):
        super().__init__(config_dir, server_dir)
        self._transport: httpx.AsyncBaseTransport | None = None  # tests inject a fake network here

    # -- version lists for the Add game page ---------------------------------
    @staticmethod
    async def option_choices(key: str, picks: dict, transport: httpx.AsyncBaseTransport | None = None) -> dict:
        """Releases to offer for `key` ("version") given the other picks ({"flavor": ...}); newest first, plus which one "latest" means."""
        flavor = picks.get("flavor")
        if key != "version" or flavor not in ("paper", "vanilla"):
            raise ValueError("no list of choices for that")
        async with httpx.AsyncClient(transport=transport or _TEST_TRANSPORT, headers={"User-Agent": USER_AGENT},
                                     follow_redirects=True, timeout=httpx.Timeout(15.0)) as client:
            if flavor == "vanilla":
                manifest = (await client.get(MOJANG_MANIFEST)).raise_for_status().json()
                found = [v["id"] for v in manifest["versions"] if v.get("type") == "release" and SIMPLE_VERSION.fullmatch(v["id"])]
                latest = manifest["latest"]["release"]
            else:
                listed = (await client.get(PAPER_API)).raise_for_status().json().get("versions", [])
                flat = [v for g in (listed.values() if isinstance(listed, dict) else [listed]) for v in (g if isinstance(g, list) else [g])]
                found = sorted({v for v in flat if isinstance(v, str) and SIMPLE_VERSION.fullmatch(v)}, key=version_key, reverse=True)
                latest = found[0] if found else None
        if not found:
            raise RuntimeError("no versions were listed")
        return {"choices": found, "latest": latest}

    # -- settings ----------------------------------------------------------
    def prepare(self, options: dict | None = None) -> None:
        """Create the settings files so the Config tab has something to show before the first install."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        flavor = (options or {}).get("flavor", "paper")
        version = (options or {}).get("version", "latest") if flavor != "pack" else "latest"
        for name, text in (("server.properties", SERVER_DEFAULTS),
                           ("panel.properties", PANEL_DEFAULTS.format(flavor=flavor, version=version))):
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
    def upload_accept(self) -> str | None:  # type: ignore[override]
        return ".zip" if self._flavor() == "pack" else None

    def _flavor(self) -> str:
        return self._props("panel.properties").get("flavor", "paper").lower()

    def reinstall_blocker(self) -> str | None:
        if self._flavor() == "pack":
            return "A modpack can't be re-downloaded: upload its server zip again instead (worlds and player lists are kept)."
        return None

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

    def _pack_state(self) -> dict | None:
        try:
            return json.loads((self.server_dir / PACK_MARKER).read_text())
        except (OSError, ValueError):
            return None

    def prompts(self) -> list[dict]:
        out = []
        if self._flavor() == "pack" and not self._pack_state():
            out.append({"id": "pack", "kind": "upload", "button": "Choose server zip",
                        "text": "Upload your modpack's server files (the zip from CurseForge or Modrinth marked \"server pack\")."})
        if not self.eula_accepted():
            out.append({"id": "eula", "action": "accept_eula", "link": EULA_URL, "button": "I accept the Minecraft EULA",
                 "text": "Minecraft will not start until you accept Mojang's End User License Agreement. "
                         "Read it, then accept: this writes eula=true for this server."})
        return out

    def actions(self):
        return {"accept_eula": self._accept_eula}

    async def _accept_eula(self, params: dict, log: LogFn) -> dict:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "eula.txt").write_text(f"# Accepted through the panel. See {EULA_URL}\neula=true\n")
        log("[panel] Minecraft EULA accepted")
        return {"ok": True}

    # -- install -----------------------------------------------------------
    def is_installed(self) -> bool:
        if self._flavor() == "pack":
            return self._pack_state() is not None
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
        if flavor == "pack":  # nothing to download: the pack comes from an upload
            if self._pack_state():
                log("[panel] this server runs an uploaded pack; upload a new zip (Advanced tab) to update it")
                return
            raise RuntimeError("Upload your modpack's server zip first (the Choose server zip button on this page).")
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

    # -- uploaded modpack ---------------------------------------------------
    def _keep_on_repack(self) -> list[str]:
        level = self.persistent_paths[:3]  # world, nether, end
        return level + ["ops.json", "whitelist.json", "banned-players.json", "banned-ips.json", "usercache.json"]

    def accept_upload(self, path: Path, log: LogFn) -> None:
        if self._flavor() != "pack":
            raise RuntimeError("This server is set to Paper or Vanilla. Set Server type to pack in panel.properties to upload a modpack.")
        self.prepare()
        incoming = self.server_dir.parent / (self.server_dir.name + ".incoming")
        shutil.rmtree(incoming, ignore_errors=True)
        try:
            self.on_progress(None, "Unpacking")
            count = safe_extract(path, incoming)
            log(f"[panel] unpacked {count} files")
            found = detect_entry(incoming)
            self.server_dir.mkdir(parents=True, exist_ok=True)
            self.on_progress(None, "Replacing the old files")
            self.wipe(self._keep_on_repack(), log)  # mods and configs are replaced; worlds and player lists stay
            for child in incoming.iterdir():
                if (self.server_dir / child.name).exists():  # a kept world beats the one shipped in the zip
                    continue
                shutil.move(str(child), str(self.server_dir / child.name))
        except UnsafeArchive as e:
            raise ValueError(str(e)) from e
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
        self._adopt_pack_settings(log)
        mc = guess_mc_version(self.server_dir)
        state = {"kind": found["kind"] if found else "unknown", "entry": found["entry"] if found else None, "mc": mc}
        (self.server_dir / PACK_MARKER).write_text(json.dumps(state))
        if found:
            log(f"[panel] this pack starts with {found['entry']}" + (f" (Minecraft {mc})" if mc else ""))
        else:
            log("[panel] unpacked, but I could not tell how this pack starts: set Start file on the Config tab (panel.properties)")
        self.on_progress(100.0, "Done")

    def _adopt_pack_settings(self, log: LogFn) -> None:
        """A pack's own server.properties seeds ours once; its eula.txt is never trusted: you accept the EULA yourself."""
        shipped, mine = self.server_dir / "server.properties", self.config_dir / "server.properties"
        if shipped.is_file() and not shipped.is_symlink():
            if not mine.exists() or mine.read_text() == SERVER_DEFAULTS:
                shutil.copyfile(shipped, mine)
                log("[panel] using the server.properties that came with the pack")
            shipped.unlink()
        eula = self.server_dir / "eula.txt"
        if eula.exists() and not eula.is_symlink():
            eula.unlink()

    def _java_bin(self, mc: str | None) -> tuple[str, Path | None]:
        """The java to run with, and its install folder when it is one of the image's. Setting `java` wins over guessing."""
        if os.environ.get("MINECRAFT_JAVA"):
            return os.environ["MINECRAFT_JAVA"], None
        choice = self._props("panel.properties").get("java", "auto")
        major = int(choice) if choice in JAVA_CHOICES and choice != "auto" else java_major_for(mc) if mc else 21
        home = JAVA_HOMES / str(major)
        if (home / "bin" / "java").is_file():
            return str(home / "bin" / "java"), home
        fallback = shutil.which("java")
        if fallback:
            return fallback, None
        raise RuntimeError(f"Java {major} was not found in this container (the image should include it)")

    def _apply_memory(self, memory: str) -> None:
        """Forge/NeoForge packs read heap size from user_jvm_args.txt: set -Xmx there, keep their other flags."""
        f = self.server_dir / "user_jvm_args.txt"
        if f.is_file():
            kept = [l for l in f.read_text().splitlines() if not re.match(r"\s*-Xm[sx]\S*", l)]
            f.write_text("\n".join(kept + [f"-Xmx{memory}"]) + "\n")

    def _pack_launch(self, memory: str) -> LaunchSpec:
        state = self._pack_state() or {}
        override = self._props("panel.properties").get("start_file", "").strip()
        entry = override or state.get("entry")
        if not entry:
            raise RuntimeError("I could not tell how this pack starts. Set Start file on the Config tab (e.g. run.sh).")
        target = (self.server_dir / entry).resolve()
        if self.server_dir.resolve() not in target.parents or not target.is_file():
            raise RuntimeError(f"Start file {entry!r} was not found inside the server folder.")
        java, home = self._java_bin(state.get("mc"))
        self._apply_memory(memory)
        env = {"JAVA_HOME": str(home), "PATH": f"{home / 'bin'}:{os.environ.get('PATH', '')}"} if home else {}
        kind = kind_of(entry) if override else state.get("kind") or kind_of(entry)
        if kind == "script":
            return LaunchSpec(["bash", entry], self.server_dir, env)
        if kind == "args":
            args = [f"@{entry}"]
            if (self.server_dir / "user_jvm_args.txt").is_file():
                args.insert(0, "@user_jvm_args.txt")
            return LaunchSpec([java, *args, "--nogui"], self.server_dir, env)
        return LaunchSpec([java, f"-Xmx{memory}", "-jar", entry, "--nogui"], self.server_dir, env)

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

    def launch_spec(self) -> LaunchSpec:
        if not self.eula_accepted():
            raise RuntimeError("Accept the Minecraft EULA first (use the button on this server's page).")
        self.prepare()
        self.server_dir.mkdir(parents=True, exist_ok=True)
        for name in ("server.properties", "eula.txt"):
            self._link(name)
        if self._flavor() == "pack":
            return self._pack_launch(self._memory())
        try:
            mc = json.loads((self.server_dir / MARKER).read_text()).get("version")
        except (OSError, ValueError):
            mc = None
        java, home = self._java_bin(mc)
        env = {"JAVA_HOME": str(home), "PATH": f"{home / 'bin'}:{os.environ.get('PATH', '')}"} if home else {}
        return LaunchSpec([java, f"-Xmx{self._memory()}", "-jar", JAR, "--nogui"], self.server_dir, env)
