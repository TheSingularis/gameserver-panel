import sys
from pathlib import Path

from panel.modules.base import GameModule, LaunchSpec, Port


class FakeModule(GameModule):
    """Stand-in game: prints a line then sleeps. Proves the panel core is game-agnostic."""
    id, name = "fake", "Fake"
    ports = [Port(1000, 1001, "udp")]
    config_files = ["fake.cfg"]

    def __init__(self, config_dir: Path, server_dir: Path, script="import time;print('hello',flush=True);time.sleep(60)"):
        super().__init__(config_dir, server_dir)
        self.script, self.installed, self.installs = script, False, 0

    def is_installed(self): return self.installed

    async def install(self, log):
        self.installs += 1
        log("installing")
        self.installed = True

    def launch_spec(self):
        return LaunchSpec([sys.executable, "-c", self.script], self.server_dir)

    def actions(self):
        async def echo(params, log):
            if "bad" in params:
                raise ValueError("bad")
            return {"echo": params}
        return {"echo": echo}
