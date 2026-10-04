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
Forward on your router to the host's LAN IP (give it a static IP / DHCP reservation). Use host networking on Linux, or `-p` mappings on Docker Desktop.

| Port | Protocol | Status |
|---|---|---|
| 7777-7778 | **UDP** | **Verified**: the server binds UDP 7777 (game) and 7778; forwarding these made it appear in the public in-game list |
| 7776-7778 | TCP | forwarded in the verified setup; whether TCP is required is not isolated yet |
| 443 | TCP/UDP | community docs mention it; it was **not** forwarded and the server still listed, so likely not needed |

The game port is set in `server.cfg` (the listed community servers use 7781); if you change it, forward that port instead.
**Never forward the panel port (8080).** It is password-protected but meant for the LAN (or a VPN / reverse proxy with TLS).
CGNAT (router WAN IP differs from your public IP) makes forwarding impossible; ask your ISP for a public IP.
Many routers cannot reach their own public IP from inside the LAN (no NAT loopback): test joining from outside (friend / phone hotspot), or favorite the LAN IP:7777.
Without the forwards the server ran but did **not** appear in the list, so the forwards are what make it public.

## Status: what is and isn't verified
- Tested here: panel core, auth, API, supervisor (start/stop/crash/force-kill/update/restart), Ship launch args, config
  persistence, UI via headless Chromium (`pytest`, 15 tests).
- Verified on a real desktop (Docker, Arch): image builds; SteamCMD anonymous download; `TSRDedicated.exe` is a **32-bit**
  exe and runs under `wine32` (win32 prefix); the server binds UDP 7777/7778, appears in the public in-game list once ports are
  forwarded, and **a client on a different network (phone hotspot) joined with no errors**.
- **No Steam account is needed.** The server logs on to Steam anonymously, so the panel's optional `steam_login` action is not required for
  The Ship (it was run once during testing; the server listing does not depend on it).
- Not yet verified: running on Unraid; the published ghcr image from CI; password-protected join; long-running stability.

## Dev
`pip install -r requirements-dev.txt && pytest -q` then `PANEL_PASSWORD=devpassword PANEL_DATA=./data python -m panel.main`.
