# gameserver-panel

One container = a small web control panel **plus** the game server it manages, as separate processes
(the UI never blocks on the game, and a game crash never takes the UI down).
UI: start / stop / restart / update (SteamCMD), edit config, live console, and module-specific actions.

Each game is a **module** (`panel/modules/<game>/`) implementing `GameModule`
(install, launch command, ports, editable configs, extra actions). The core knows nothing about any game; things like
Steam login live inside the module that needs them. First module: **The Ship: Remasted**. Adding a game = a new module
folder + one line in `panel/modules/__init__.py`.

## Run (Unraid or any Docker host)
1. Unraid: copy `unraid/gameserver-panel.xml` to `/boot/config/plugins/dockerMan/templates-user/`, then Docker > Add Container.
   Elsewhere: set `PANEL_PASSWORD` in `docker-compose.yml`, `docker compose up -d --build`.
2. Open `http://<host>:8080`, log in, press **Update** (downloads the server, ~700 MB), then **Start**.
3. First start creates `server.cfg`; edit name/password/port in the UI and restart.

## Networking: The Ship: Remasted
Forward on your router to the host's LAN IP (give it a static IP / DHCP reservation). Use host networking (default here).

| Port | Protocol | Purpose |
|---|---|---|
| 7776-7778 | TCP **and** UDP | game (default 7777, configurable in server.cfg) |
| 443 | TCP **and** UDP | used by the server per community docs (can clash with Unraid HTTPS UI; move that) |

**Never forward the panel port (8080).** It is password-protected but meant for the LAN (or a VPN / reverse proxy with TLS).
CGNAT (router WAN IP differs from your public IP) makes forwarding impossible; ask your ISP for a public IP.
Verify: server appears in-game (Dedicated/All tab, versions must match) from the LAN, then from a mobile-data phone or a friend.

## Status: what is and isn't verified
- Tested here: panel core, auth, API, supervisor (start/stop/crash/force-kill/update/restart), Ship launch args, config
  persistence, UI via headless Chromium (`pytest`, 15 tests).
- **Not tested**: the Docker image build, SteamCMD download, and running the real Windows server under Wine (the
  development sandbox had no Docker daemon and blocked Steam/Debian hosts).
- Open risk: community reports say the server wants a running Steam client on an account that owns the game
  (`SteamAPI_Init` failure otherwise). The module's Steam login action only runs `steamcmd +login`; whether that satisfies the
  game is unknown until tried on real hardware.

## Dev
`pip install -r requirements-dev.txt && pytest -q` then `PANEL_PASSWORD=devpassword PANEL_DATA=./data python -m panel.main`.
