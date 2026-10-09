"""Datapacks (Paper and Vanilla) and plugins (Paper) that a person uploads and manages from the panel.

Datapacks live in `<world>/datapacks` as a zip or a folder with a `pack.mcmeta`. While the server runs the panel types
the console commands (`datapack enable|disable`, then a reload for a new pack); while it is stopped it moves the pack in
or out of `datapacks-disabled/<world>/` so the world's own `level.dat` is never edited. Plugins are `.jar` files in
`plugins/`; a disabled one is renamed to `.jar.off`. Paper cannot load or unload a plugin safely, so plugin changes need
the server stopped. A plugin's config folder next to its jar is never touched.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
import struct
import zipfile
from pathlib import Path, PurePosixPath

from ...archive import UnsafeArchive, _check

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+()'-]{0,95}$")  # a file or folder name we are willing to type into the console
LEVEL_NAME = re.compile(r"^[\w .-]{1,64}$")
MAX_ENTRIES = 50_000
MAX_UNPACKED = 4 * 1024**3
KINDS = {
    "datapacks": {"label": "Datapacks", "accept": ".zip"},
    "plugins": {"label": "Plugins", "accept": ".jar"},
}
STATE_FILE = ".panel-datapacks.json"  # what live toggles did since the world last saved (level.dat only updates then)


# -- reading level.dat (read-only) ---------------------------------------------------------------
def _nbt(buf: io.BytesIO, tag: int):
    def rd(fmt):
        size = struct.calcsize(fmt)
        data = buf.read(size)
        if len(data) != size:
            raise ValueError("short NBT")
        return struct.unpack(">" + fmt, data)[0]

    def string():
        return buf.read(rd("H")).decode("utf-8", "replace")

    if tag in (1,):
        return rd("b")
    if tag == 2:
        return rd("h")
    if tag == 3:
        return rd("i")
    if tag == 4:
        return rd("q")
    if tag == 5:
        return rd("f")
    if tag == 6:
        return rd("d")
    if tag == 7:
        return buf.read(rd("i"))
    if tag == 8:
        return string()
    if tag == 9:
        inner, n = rd("B"), rd("i")
        return [_nbt(buf, inner) for _ in range(max(n, 0))]
    if tag == 10:
        out = {}
        while (t := rd("B")) != 0:
            key = string()
            out[key] = _nbt(buf, t)
        return out
    if tag == 11:
        return [rd("i") for _ in range(rd("i"))]
    if tag == 12:
        return [rd("q") for _ in range(rd("i"))]
    raise ValueError("unknown NBT tag")


def read_datapack_state(level_dat: Path) -> tuple[list[str], list[str]] | None:
    """(enabled, disabled) pack ids the world last saved, or None when level.dat is missing or unreadable."""
    try:
        buf = io.BytesIO(gzip.decompress(level_dat.read_bytes()))
        if buf.read(1) != b"\x0a":
            return None
        buf.read(struct.unpack(">H", buf.read(2))[0])
        root = _nbt(buf, 10)
        packs = root.get("Data", {}).get("DataPacks", {})
        return [str(x) for x in packs.get("Enabled", [])], [str(x) for x in packs.get("Disabled", [])]
    except (OSError, ValueError, struct.error, EOFError):
        return None


# -- checking uploads -----------------------------------------------------------------------------
def clean_filename(name: object, suffix: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    if not base.lower().endswith(suffix):
        raise ValueError(f"Choose a {suffix} file.")
    base = base[: -len(suffix)] + suffix  # normalise the extension's case
    if not NAME.fullmatch(base):
        raise ValueError("Rename the file first: use letters, numbers, spaces and - _ . + ( ) only, up to 96 characters.")
    return base


def inspect_zip(path: Path) -> zipfile.ZipFile:
    """Open a zip and refuse unsafe members (absolute or `..` paths, links) and absurd sizes, before anything is kept."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError("That is not a valid zip file.") from None
    infos = zf.infolist()
    try:
        for i in infos:
            _check(i)
    except UnsafeArchive as e:
        zf.close()
        raise ValueError(str(e)) from None
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNPACKED:
        zf.close()
        raise ValueError("That archive is too big to be a datapack or plugin.")
    return zf


def _fmt(value) -> str | None:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list) and value and all(isinstance(x, int) for x in value):
        return ".".join(map(str, value))
    return None


def _bound(value, top: bool):
    """A format bound as (major, minor). A bare number covers every minor of that major, so as an upper bound it is open."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return (value, 10**9 if top else 0)
    if isinstance(value, list) and len(value) == 2 and all(isinstance(x, int) for x in value):
        return (value[0], value[1])
    return None


def declared_range(pack: dict):
    """The data-pack formats a pack says it works with, as ((major, minor), (major, minor)), or None when it says nothing.
    Reads the old pack_format and supported_formats fields as well as the newer min_format and max_format."""
    los, his = [], []

    def add(lo, hi):
        if lo and hi:
            los.append(lo)
            his.append(hi)
    if "pack_format" in pack:
        add(_bound(pack["pack_format"], False), _bound(pack["pack_format"], True))
    sf = pack.get("supported_formats")
    if isinstance(sf, dict):
        add(_bound(sf.get("min_inclusive"), False), _bound(sf.get("max_inclusive"), True))
    elif isinstance(sf, list) and len(sf) == 2 and all(isinstance(x, int) for x in sf):
        add(_bound(sf[0], False), _bound(sf[1], True))
    elif sf is not None:
        add(_bound(sf, False), _bound(sf, True))
    if "min_format" in pack or "max_format" in pack:
        add(_bound(pack.get("min_format", pack.get("max_format")), False), _bound(pack.get("max_format", pack.get("min_format")), True))
    return (min(los), max(his)) if los else None


def server_data_format(server_dir: Path, jar: str = "server.jar") -> tuple[int, int] | None:
    """(major, minor) data-pack format of the installed game, from the version.json inside its jar; None when unknown."""
    try:
        with zipfile.ZipFile(server_dir / jar) as zf:
            data = json.loads(zf.read("version.json"))["pack_version"]
        major = data.get("data_major", data.get("data"))
        return (int(major), int(data.get("data_minor", 0))) if isinstance(major, int) else None
    except (OSError, KeyError, ValueError, TypeError, zipfile.BadZipFile):
        return None


def format_fits(rng, server: tuple[int, int] | None) -> bool | None:
    """Whether a pack's declared range includes the server's format; None when either side is unknown."""
    if not rng or not server:
        return None
    return rng[0] <= server <= rng[1]


def describe_pack_meta(raw: bytes) -> dict:
    try:
        meta = json.loads(raw.decode("utf-8-sig"))
        pack = meta["pack"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("pack.mcmeta is not valid JSON with a \"pack\" section.") from None
    if not isinstance(pack, dict):
        raise ValueError("pack.mcmeta is not valid JSON with a \"pack\" section.")
    desc = pack.get("description", "")
    if isinstance(desc, list):
        desc = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in desc)
    elif isinstance(desc, dict):
        desc = str(desc.get("text", ""))
    fmt = _fmt(pack.get("pack_format")) or _fmt(pack.get("min_format"))
    return {"description": " ".join(str(desc).split())[:200], "format": fmt, "range": declared_range(pack)}


def check_datapack_zip(path: Path) -> dict:
    with inspect_zip(path) as zf:
        names = {i.filename.replace("\\", "/") for i in zf.infolist()}
        if "pack.mcmeta" not in names:
            nested = sorted(n for n in names if PurePosixPath(n).name == "pack.mcmeta")
            if nested:
                raise ValueError(f"pack.mcmeta is inside a folder ({nested[0]}). Zip the folder's contents, so pack.mcmeta sits at the top.")
            raise ValueError("This zip has no pack.mcmeta, so it is not a datapack. (A resource pack or a mod will not work here.)")
        if not any(n.startswith("data/") for n in names):
            raise ValueError("This zip has no data/ folder, so it contains no datapack content.")
        return describe_pack_meta(zf.read("pack.mcmeta"))


def read_plugin_meta(path: Path) -> dict:
    """Name, version and authors from plugin.yml or paper-plugin.yml (simple top-level keys only)."""
    with inspect_zip(path) as zf:
        for entry in ("paper-plugin.yml", "plugin.yml"):
            try:
                text = zf.read(entry).decode("utf-8-sig", "replace")
            except KeyError:
                continue

            def key(k):
                m = re.search(rf"^{k}:[ \t]*(.+?)[ \t]*$", text, re.M)
                return m.group(1).strip("'\"") if m else ""
            authors = key("author") or key("authors").strip("[]").replace("'", "").replace('"', "")
            if not key("name"):
                raise ValueError(f"{entry} has no name.")
            return {"title": key("name"), "version": key("version"), "authors": authors, "description": key("description")[:200]}
    raise ValueError("This jar has no plugin.yml or paper-plugin.yml, so Paper would not load it. (A Forge/Fabric mod will not work here.)")


# -- the module part --------------------------------------------------------------------------------
class ContentMixin:
    """Mixed into the Minecraft module; needs _props(), _flavor(), server_dir and console_input from it."""

    def _level(self) -> str:
        level = self._props("server.properties").get("level-name", "world")
        return level if LEVEL_NAME.fullmatch(level) and level not in (".", "..") else "world"

    def content_kinds(self) -> list[dict]:
        flavor = self._flavor()
        out = []
        if flavor in ("paper", "vanilla"):
            out.append({"id": "datapacks", **KINDS["datapacks"]})
        if flavor == "paper":
            out.append({"id": "plugins", **KINDS["plugins"]})
        return out

    def _kind(self, kind: str) -> str:
        if kind not in {k["id"] for k in self.content_kinds()}:
            raise ValueError("This server does not take that.")
        return kind

    # -- paths
    def _pack_dir(self) -> Path:
        return self.server_dir / self._level() / "datapacks"

    def _aside_dir(self) -> Path:
        return self.server_dir / "datapacks-disabled" / self._level()

    def _plugin_dir(self) -> Path:
        return self.server_dir / "plugins"

    def _reload_command(self) -> str:
        return "minecraft:reload" if self._flavor() == "paper" else "reload"  # Paper's plain /reload also reloads plugins

    # -- listing
    @staticmethod
    def _size(p: Path) -> int:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())

    def _pack_meta(self, p: Path) -> dict:
        try:
            if p.is_dir():
                return describe_pack_meta((p / "pack.mcmeta").read_bytes())
            with zipfile.ZipFile(p) as zf:
                return describe_pack_meta(zf.read("pack.mcmeta"))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            return {"description": "", "format": None, "broken": True}

    def _live_state(self) -> dict:
        try:
            return json.loads((self.server_dir / STATE_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def _save_live_state(self, state: dict) -> None:
        p = self.server_dir / STATE_FILE
        if state:
            p.write_text(json.dumps(state))
        else:
            p.unlink(missing_ok=True)

    def _list_datapacks(self, running: bool) -> dict:
        saved = read_datapack_state(self.server_dir / self._level() / "level.dat")
        disabled_in_world = {x for x in (saved[1] if saved else []) if x.startswith("file/")}
        live = self._live_state() if running else {}
        if not running and live:
            self._save_live_state({})  # the world has saved on stop: level.dat is the truth again
        items = []
        server_fmt = server_data_format(self.server_dir)
        for base, aside in ((self._pack_dir(), False), (self._aside_dir(), True)):
            if not base.is_dir():
                continue
            for p in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                if p.is_symlink() or p.name.startswith(".") or not ((p.is_dir() and (p / "pack.mcmeta").is_file()) or (p.is_file() and p.suffix.lower() == ".zip")):
                    continue
                enabled = (not aside) and f"file/{p.name}" not in disabled_in_world
                if not aside and p.name in live:
                    enabled = bool(live[p.name])
                items.append({"name": p.name, "folder": p.is_dir(), "size": self._size(p), "enabled": enabled, "aside": aside,
                              "needs_live": (not aside) and not enabled and not running and f"file/{p.name}" in disabled_in_world,
                              **self._pack_meta(p)})
                fits = format_fits(items[-1].pop("range", None), server_fmt)
                items[-1]["fits"] = fits  # False = the pack was made for other game versions
        return {"items": items}

    def _plugin_jar_meta(self, p: Path) -> dict:
        try:
            return read_plugin_meta(p)
        except (OSError, ValueError, zipfile.BadZipFile):
            return {"title": "", "version": "", "authors": "", "description": "", "broken": True}

    def _list_plugins(self) -> dict:
        items = []
        d = self._plugin_dir()
        if d.is_dir():
            for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
                low = p.name.lower()
                if p.is_symlink() or not p.is_file() or not (low.endswith(".jar") or low.endswith(".jar.off")):
                    continue
                meta = self._plugin_jar_meta(p)
                folders = {c.name.lower() for c in d.iterdir() if c.is_dir()}
                items.append({"name": p.name, "size": p.stat().st_size, "enabled": low.endswith(".jar"),
                              "has_config": (meta.get("title") or "").lower() in folders, **meta})
        return {"items": items}

    async def content_list(self, kind: str, running: bool) -> dict:
        self._kind(kind)
        info = self._list_datapacks(running) if kind == "datapacks" else self._list_plugins()
        return {"kind": kind, **info, "live": bool(running and self.console_input and kind == "datapacks")}

    # -- adding
    def content_add(self, kind: str, tmp: Path, filename: str, running: bool) -> list[str]:
        """Keep an uploaded file. Returns the console commands to send now (only datapacks on a running server)."""
        self._kind(kind)
        if kind == "plugins":
            if running:
                raise RuntimeError("Stop the server first: Paper cannot load or replace a plugin safely while it runs.")
            name = clean_filename(filename, ".jar")
            read_plugin_meta(tmp)
            d = self._plugin_dir()
            d.mkdir(parents=True, exist_ok=True)
            if (d / (name + ".off")).exists():
                (d / (name + ".off")).unlink()  # replacing a disabled copy enables the new one
            self._place(tmp, d / name)  # only the jar is replaced; the plugin's config folder stays
            return []
        name = clean_filename(filename, ".zip")
        check_datapack_zip(tmp)
        d = self._pack_dir()
        if not (self.server_dir / self._level()).is_dir():
            raise RuntimeError("There is no world yet. Start the server once so it creates the world, then add datapacks.")
        d.mkdir(parents=True, exist_ok=True)
        if (self._aside_dir() / name).exists():
            (self._aside_dir() / name).unlink()
        self._place(tmp, d / name)
        if not running:
            return []
        state = self._live_state()
        state[name] = True
        self._save_live_state(state)
        return [self._reload_command()]

    @staticmethod
    def _place(tmp: Path, target: Path) -> None:
        part = target.with_name("." + target.name + ".part")  # hidden, and not a .zip/.jar: neither game scans it
        shutil.copyfile(tmp, part)
        os.replace(part, target)

    # -- changing
    def content_change(self, kind: str, action: str, name: str, running: bool) -> list[str]:
        self._kind(kind)
        if not NAME.fullmatch(name or ""):
            raise ValueError("unknown item")
        if kind == "plugins":
            return self._change_plugin(action, name, running)
        return self._change_datapack(action, name, running)

    def _change_plugin(self, action: str, name: str, running: bool) -> list[str]:
        if running:
            raise RuntimeError("Stop the server first: Paper cannot load or unload a plugin safely while it runs.")
        d = self._plugin_dir()
        src = d / name
        if not src.is_file() or src.is_symlink() or src.parent != d:
            raise ValueError("That plugin is not here any more.")
        if action == "remove":
            src.unlink()  # the jar only; its config folder stays so settings survive a reinstall
        elif action == "disable" and name.endswith(".jar"):
            os.replace(src, d / (name + ".off"))
        elif action == "enable" and name.endswith(".jar.off"):
            if (d / name[:-4]).exists():
                raise ValueError("A plugin with that name is already enabled.")
            os.replace(src, d / name[:-4])
        else:
            raise ValueError("unknown action")
        return []

    def _change_datapack(self, action: str, name: str, running: bool) -> list[str]:
        live_pack, aside_pack = self._pack_dir() / name, self._aside_dir() / name
        here = live_pack.exists() and not live_pack.is_symlink()
        away = aside_pack.exists() and not aside_pack.is_symlink()
        if not (here or away):
            raise ValueError("That datapack is not here any more.")
        if action == "remove":
            if running:
                raise RuntimeError("Stop the server first: it keeps datapack files open while it runs.")
            target = live_pack if here else aside_pack
            shutil.rmtree(target) if target.is_dir() else target.unlink()
            return []
        if action not in ("enable", "disable"):
            raise ValueError("unknown action")
        if running:
            if not here:
                raise RuntimeError("This pack was disabled while the server was stopped. Stop the server and enable it, then start again.")
            state = self._live_state()
            state[name] = action == "enable"
            self._save_live_state(state)
            return [f'datapack {action} "file/{name}"']
        if action == "disable":
            if away:
                return []
            self._aside_dir().mkdir(parents=True, exist_ok=True)
            os.replace(live_pack, aside_pack)
        else:
            if here:
                saved = read_datapack_state(self.server_dir / self._level() / "level.dat")
                if saved and f"file/{name}" in saved[1]:
                    raise RuntimeError("The world has this pack switched off. Start the server and enable it there.")
                return []
            self._pack_dir().mkdir(parents=True, exist_ok=True)
            os.replace(aside_pack, live_pack)
        return []
