"""Catalogue of game modules the panel knows about.

Adding a game = a new module class plus one entry in `catalog()`. Entries with no factory are
roadmap placeholders shown on the Add game page as "coming soon".
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .base import GameModule


@dataclass(frozen=True)
class ModuleInfo:
    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    factory: Callable[[Path, Path], GameModule] | None = None  # None = coming soon
    icon: str | None = None  # remote URL; a bundled panel/static/icons/<id>.png|jpg|svg takes precedence
    options: tuple[dict, ...] = ()  # choices asked for when adding a server (see GameModule.create_options)
    choices: Callable[..., Awaitable[dict]] | None = None  # async (key, picks) -> {"choices": [...], "latest": ...} for options with choices_from

    @property
    def status(self) -> str:
        return "available" if self.factory else "soon"


def _ship(config_dir: Path, server_dir: Path) -> GameModule:
    from .theship import TheShip
    return TheShip(config_dir, server_dir)


def _minecraft(config_dir: Path, server_dir: Path) -> GameModule:
    from .minecraft import Minecraft
    return Minecraft(config_dir, server_dir)


def _minecraft_choices(key: str, picks: dict):
    from .minecraft import Minecraft
    return Minecraft.option_choices(key, picks)


def _minecraft_options() -> tuple[dict, ...]:
    from .minecraft import Minecraft
    return tuple(Minecraft.create_options)


def _demo(config_dir: Path, server_dir: Path) -> GameModule:
    from .demo import Demo
    return Demo(config_dir, server_dir)


def catalog(demo: bool = False) -> dict[str, ModuleInfo]:
    items = [
        ModuleInfo("theship", "The Ship: Remasted",
                   "Murder-mystery hunt on a cruise ship. Dedicated server with a public listing; runs the Windows build under Wine.",
                   ("Steam", "Windows via Wine", "UDP 7777"), _ship,
                   # The Ship: Remasted community icon (app 383790, the client game: the server tool 443050 has no art). The browser loads
                   # it from Steam at runtime, nothing is bundled; offline it falls back to the grey letter tile.
                   "https://shared.fastly.steamstatic.com/community_assets/images/apps/383790/de54185795014585bc27f4649f85f926f7ea63e5.jpg"),
        ModuleInfo("minecraft", "Minecraft: Java Edition",
                   "Java Edition server as Paper (plugins, faster), Vanilla (Mojang's own) or your own modpack server zip (Forge, NeoForge, Fabric). Accept the EULA once, then start.",
                   ("Java", "Paper, Vanilla or modpack", "TCP 25565"), _minecraft,
                   # Grass block from the dashboard-icons set (Apache-2.0 repo; the art is Mojang's, used only to identify the game),
                   # pinned to a commit so it cannot change under us. The browser loads and caches it from jsDelivr at runtime,
                   # nothing is bundled; if it cannot load, the grey letter tile shows instead.
                   "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons@adca944175c9a3eb0471f78a4da87f237476d585/png/minecraft.png", _minecraft_options(), _minecraft_choices),
    ]
    if demo:
        items.append(ModuleInfo("demo", "Demo game",
                                "A fake server for trying out the panel. Installs in seconds and prints a heartbeat.",
                                ("Test only",), _demo))
    return {m.id: m for m in items}
