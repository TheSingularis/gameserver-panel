# Kosmos

A home for many game worlds: one container = a small web control panel **plus** the game server it manages, as separate processes
(the UI never blocks on the game, and a game crash never takes the UI down).
UI: a sidebar of named game servers (several per game is fine), each with install / start / stop / restart / update with a progress bar,
live console, config editor, port check and module-specific actions.

Each game is a **module** (`panel/modules/<game>/`) implementing `GameModule`
(install, launch command, ports, editable configs, extra actions). The core knows nothing about any game; things like
Steam login live inside the module that needs them. First module: **The Ship: Remasted**. Adding a game = a new module
folder + one line in `panel/modules/__init__.py`.

## Run (Unraid or any Docker host)
1. Unraid: copy `unraid/kosmos.xml` to `/boot/config/plugins/dockerMan/templates-user/`, then Docker > Add Container.
   Elsewhere (or Unraid with the Compose Manager plugin): set `PANEL_PASSWORD` in `docker-compose.yml` and `docker compose up -d`; data lives in `PANEL_DATA_DIR` (default `/mnt/user/appdata/kosmos`).
   The container carries the label `com.centurylinklabs.watchtower.enable=true`, so Watchtower updates it from ghcr whenever CI publishes a new image. To build locally instead: `docker compose -f docker-compose.dev.yml up -d --build`.
2. Open `http://<host>:8080`, log in, go to **Add game**, pick The Ship and name the server, then press **Install** (downloads ~700 MB) and **Start**.
   An existing single-game install (files in `/data/server` + `/data/config`) is adopted automatically.
3. First start creates `server.cfg`; edit name/password/port on the **Config** tab and restart.
4. Servers can be renamed any time (**Rename** next to the title). Game icons come from Steam's CDN, or from `panel/static/icons/<module>.png|jpg|svg` if you drop one there (works offline).
   Port conflicts between servers of the same game are not handled yet: give each its own ports in its config.

## Sign in with Keycloak (OIDC)
Optional. In Keycloak create a client with **Standard flow** on, and **Valid redirect URI** `<OIDC_PUBLIC_URL>/auth/callback`
(use a confidential client with a secret, or a public client; PKCE is always used). Then set on the container:

| Variable | Meaning |
|---|---|
| `OIDC_ISSUER` | realm URL, e.g. `https://keycloak.lan/realms/home` (enables OIDC) |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | the client; the secret is optional for a public client |
| `OIDC_PUBLIC_URL` | how your browser reaches the panel, e.g. `https://panel.lan` |
| `OIDC_ALLOWED_USERS` / `OIDC_ALLOWED_GROUPS` | who may log in (usernames or emails / groups or realm roles). **One is required**, otherwise the panel refuses to start. `OIDC_ALLOW_ANY=1` admits every user of the realm |
| `OIDC_NAME` | label on the sign-in button (default `SSO`) |

Groups need a *Group Membership* mapper on the client (token claim name `groups`); realm roles work without one.
`PANEL_PASSWORD` may then be left out; if both are set the login page offers both. The ID token signature is verified against the realm's keys.
Not yet verified against a real Keycloak (the tests use an in-process fake provider).

## Networking: The Ship: Remasted
Forward on your router to the host's LAN IP (give it a static IP / DHCP reservation). Use host networking on Linux, or `-p` mappings on Docker Desktop.

| Port | Protocol | Status |
|---|---|---|
| 7777-7778 | **UDP** | **Verified**: the server binds UDP 7777 (game) and 7778; forwarding these made it appear in the public in-game list |
| 7776-7778 | TCP | forwarded in the verified setup; whether TCP is required is not isolated yet |

The game port is set in `server.cfg` (the listed community servers use 7781); if you change it, forward that port instead.
**Never forward the panel port (8080).** It is password-protected but meant for the LAN (or a VPN / reverse proxy with TLS).
CGNAT (router WAN IP differs from your public IP) makes forwarding impossible; ask your ISP for a public IP.
Many routers cannot reach their own public IP from inside the LAN (no NAT loopback): test joining from outside (friend / phone hotspot), or favorite the LAN IP:7777.
Without the forwards the server ran but did **not** appear in the list, so the forwards are what make it public.

## Minecraft: Java Edition
Add it from the Add game page and pick the server type and Minecraft version there (the version list is loaded live; it falls back to a text field if it can't be fetched). Both can be changed later in the Config tab's `panel.properties`: `paper` (plugins, faster; the default) or `vanilla` (Mojang's own),
plus a `version` (`latest` or an exact one such as `1.21.8`) and the Java `memory`. "Install" / "Check for updates" downloads the jar and verifies its checksum
(Mojang's SHA-1, Paper's SHA-256). The server will not start until you accept Mojang's EULA with the button on the server page.

| Port | Protocol | Status |
|---|---|---|
| 25565 | **TCP** | Java Edition clients connect here; change `server-port` in `server.properties` and forward that port instead |
| 25565 | UDP | only if you turn on `enable-query` |

Worlds (`level-name`, plus its `_nether` and `_the_end`), `ops.json`, the whitelist and ban lists, `plugins/` and Paper's `config/` survive "Clean & reinstall";
everything else is re-downloaded. The stop timeout is 90 s so a big world can save before the process is killed.
The image bundles Java 8, 17, 21 and 25 (under `/opt/java/<major>`) and the panel picks the one a server's Minecraft version needs; set `java` in `panel.properties` to force one.

**Modpacks (Forge, NeoForge, Fabric, ...).** Choose "Modpack server zip" as the server type when adding a Minecraft server and pick the zip, or upload it later from the
Advanced tab. The panel unpacks it safely (no path escapes, no links), finds how to start it (`run.sh`/`start.sh`, Forge/NeoForge `unix_args.txt`, `fabric-server-launch.jar`, or a single jar)
and runs it with the right Java. Uploading a new zip replaces the pack's files but keeps the world, ops, whitelist and ban lists, and your edited server settings. If the start file isn't
recognised, set `start_file` in `panel.properties`. A pack is never re-downloaded, so "Clean & reinstall" is disabled for it; upload the zip again instead.
Uploads are capped at 4 GiB and the Java download step in the image is not yet verified against Adoptium.
Not yet run against the real download services or a real client (see the PR for what was and was not checked).

## Status: what is and isn't verified
- Tested here: panel core, auth, API, supervisor (start/stop/crash/force-kill/update/restart), Ship launch args, config
  persistence, UI via headless Chromium (`pytest`, 15 tests).
- Verified on a real desktop (Docker, Arch): image builds; SteamCMD anonymous download; `TSRDedicated.exe` is a **32-bit**
  exe and runs under `wine32` (win32 prefix); the server binds UDP 7777/7778, appears in the public in-game list once ports are
  forwarded, and **a client on a different network (phone hotspot) joined with no errors**.
- **No Steam account is needed.** The server downloads and logs on to Steam anonymously, and the server listing does not depend on a login.
  (An optional Steam sign-in action existed in early versions; it was removed because nothing needs it.)
- Not yet verified: running on Unraid; the published ghcr image from CI; password-protected join; long-running stability.

## Dev
`pip install -r requirements-dev.txt && pytest -q` then `PANEL_PASSWORD=devpassword PANEL_DATA=./data python -m panel.main`.
Add `PANEL_DEMO=1` to get a fake "Demo game" that installs in seconds, handy for trying the UI.
