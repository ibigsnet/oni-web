# ONI Web

A browser operations console for **Project Reclaimer Halo 3** servers. FastAPI owns the game-server connections and credentials; React + TypeScript provides the operator interface. The original `oni-rcon` Python RCON client and React console logic are reused rather than approximated.

**Protocol:** Project Reclaimer's JSON-over-WebSocket RCON, over TCP. This repository does **not** implement Source RCON, BattlEye, Minecraft RCON, or a UDP RCON variant. See [the source analysis](docs/oni-rcon-analysis.md) for the wire contract and upstream feature inventory.

## Run locally

Install Python **3.12+** and Node **24 LTS**. The sibling `oni-rcon` checkout is not needed at runtime.

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
npm.cmd ci --prefix frontend
npm.cmd run build
.\.venv\Scripts\python.exe run.py
```

Linux/macOS:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
npm ci --prefix frontend
npm run build
python run.py
```

Open **http://127.0.0.1:8000**. Add a server's host, RCON port, password, and optional name. Save a profile, then choose **CONNECT**, or connect the entire fleet. **EXPLORE DEMO** starts three upstream protocol simulators and a local Forge catalog; it requires no game installation or external account.

Development: run `python run.py` in one terminal and `npm run dev` in another, then open **http://127.0.0.1:5173**. Vite proxies `/api` and its WebSocket to FastAPI. PowerShell users can use `npm.cmd` if script execution policy blocks `npm.ps1`.

For runtime-only installs, use `requirements.txt` instead of `requirements-dev.txt`.

## Operations

The existing ONI desktop console is adapted for the browser, including its dark interface, keyboard controls, terminal colors, reduced-motion support, and address redaction:

| Area | Browser behavior |
| --- | --- |
| Connections | Host/port, direct `ws://` or `wss://` paths, IPv6, SSH forwarding, encrypted profiles, add/edit/delete, quick-connect, fleet connect, explicit disconnect/reconnect |
| Assets / F1 | Live roster, player dossier, medals, team/score/health information, tell/kick/ban/mute/team/VPN/admin actions |
| Intercepts / F2 | Combined chat, combat, traffic, moderation and operations feed; filters, follow-tail and scrollback |
| Operations / F3 | Status, maps, modes, rotation, queue/load, votes, broadcasts, server settings, playlists and custom game types |
| Blacklist / F4 | Bans, unbans, VPN allowances, address checks and admin lists; exact supported commands depend on server version |
| Console / F5 | Arbitrary commands, up/down history, quoted arguments and upstream free-text parsing, raw events, structured replies, late replies, `@all` fan-out (four stations concurrently) |
| Forge / F6 | Catalog/search/sorting/favourites, versions, compatibility, manifest-verified installs, update/withdrawal watch, acknowledgement; publishing and MINE use the desktop API endpoints |
| Health / F7 | Original crash/OOM/memory/service parsing; desktop frame-rate calculations and charts; requires trusted host configuration |
| Findings / F8 | Original chat/vote/kill observations and evidence review; findings do not automatically moderate players |
| Automation / F9 | Original rules, roles, templates, test mode, waits and Discord actions; one browser owns a renewable lease so tabs do not duplicate actions |
| Other | Command palette, cross-fleet player search, operator attribution, Superintendent, optional boot/archive art, local rounds JSONL |

`say`, `tell`, `kick`, and `servername` preserve the rest of the command line as one message argument, as upstream does. For example, `say it's a trap` does not require shell quoting in the console. New game types and playlists use the desktop server-file formats and validations.

All connected browsers share **one active fleet** and **one upstream socket per server**, respecting Reclaimer's four-tool limit. Changing the active fleet refreshes other browsers. A generation check rejects stale-tab commands before they can target a different server at the same index. Backend connections remain alive when a browser closes or signs out; use DISCONNECT or stop the backend to close them.

Automation runs while its owning console is open. Another open tab can take ownership after its lease expires (15 seconds). Replayed events are for display only, and do not re-execute automation. Backend command and chat budgets apply in addition to the original runner's limits.

### Differences from the native applications

**HOST ONE HERE** and **FIND SERVERS ON IT** use an optional headless helper built from the original Rust routines. Enable it as described below to provision/download/start dedicated servers or discover existing ones over SSH. Ordinary RCON operation needs only Python and Node. Native executable self-updates become normal versioned web deployments: rebuild and restart the service; a browser must not install desktop executables.

The native OS file picker becomes a browser file upload. Game-server files are addressed only through operator-configured content directories; the browser cannot submit executable host commands or arbitrary backend filesystem paths. The boot is skipped by default. History is session memory, not localStorage. Browser clipboard access needs HTTPS or localhost. The app is a single-operator tool with a shared vault/token, not a multi-tenant service.

### Optional native hosting and SSH discovery

Install stable Rust on the backend machine and build the helper:

```sh
cargo build --release --manifest-path native/Cargo.toml
```

Set `ONI_WEB_NATIVE_HELPER` in `.env` to the **absolute path** of `native/target/release/oni-web-native` (`oni-web-native.exe` on Windows), then restart FastAPI. Open the expandable hosting/discovery panel on the profiles screen. The helper has no Tauri/WebKit dependency and does not serve HTTP. Its private stdin/stdout interface is consumed only by FastAPI.

The helper preserves upstream's Steam/MCC detection, published checksum verification, Windows dedicated-server setup, Linux Docker setup, server settings, optional initial playlist, existing-folder behavior and SSH discovery. Generated/read RCON credentials stay in the backend and are remembered only in the encrypted vault if selected. Discovery retains the generated remote password-command, restart, health and ping integrations. Real hosting requires the backend account to own a Steam MCC/Halo 3 installation on Windows, or the corresponding game files and Docker permissions on Linux, as documented upstream. macOS cannot host through these routines. Hosting asks for confirmation with ABORT selected.

The normal Docker image leaves this optional integration disabled. Do not mount a Docker daemon socket or host game installation automatically; install the service/helper on the host, or explicitly configure the required binary/tools/mounts. Native hosting/discovery compilation and live provisioning remain unverified in the current workspace; see the verification record before enabling them on an operating server.

## Original config and host integrations

Set `ONI_RCON_CONFIG` to an operator-owned TOML file **before the first import**. The backend imports its profiles, defaults, `by`, `content_dir`, password sources, Forge settings, health/ping commands and desktop `restart_cmd` into the encrypted vault. The original file is not modified; later edits to it are not automatically synchronized. Keep that original file private if it contains plaintext credentials.

The import is opt-in: the gateway does not silently read a user's global native-app configuration. A password resolves in upstream order: saved/session password, `password_env`, trusted `password_command`, then `ONI_RCON_PASSWORD`; missing passwords prompt in the browser instead of a terminal. Forge accepts the original `forge_api_key`, `forge_api_key_env`, `forge_api_key_command`, or `RECLAIMERFORGE_API_KEY` environment variable.

See [the original annotated config](docs/oni-rcon.example.toml). Desktop configs may also contain `restart_cmd` in an individual server block. `health_cmd`, `ping_log`, and password/restart commands execute **on the backend machine**, or via the configured SSH host as upstream specifies. They are trusted operator code and cannot be added through HTTP profile forms.

SSH uses the backend account's installed `ssh`, keys/agent, known_hosts and SSH config, with `BatchMode=yes`. An SSH host alias can supply nonstandard ports or keys. Do not use the browser computer's loopback address for a remote backend: `127.0.0.1` always refers to the backend or configured SSH destination. In Docker, it refers to the container.

Forge installs and native file operations require a configured `content_dir` on a local server or SSH host. A direct remote WebSocket URL cannot transfer game files, exactly as upstream documents. Installs verify manifest sizes and SHA-256, download all assets before copying, verify destination copies, and atomically replace individual files. Like upstream, this is **per-file atomicity**, not an all-files rollback transaction. An install never loads content automatically. Local `dedicated.toml` edits keep a private `.bak` rollback copy.

## Security and deployment

The default bind is loopback. Set `ONI_WEB_TOKEN` to a random value of **at least 24 characters** to enable login. A non-loopback bind refuses to start without it. **Always enable the token behind a reverse proxy**, even if FastAPI itself binds to loopback. Use HTTPS for remote access and set `ONI_WEB_SECURE_COOKIE=true`. A reverse-proxy example is in [docs/nginx.conf.example](docs/nginx.conf.example).

Set `ONI_WEB_ORIGINS` to the exact browser origins (including nondefault ports), and preserve the browser's Host header through the proxy. Host checks protect against DNS rebinding; HTTP mutations require a custom header, and WebSocket upgrades require a matching Origin and authenticated session. No permissive CORS policy is installed. Login creates an HttpOnly, SameSite=Strict cookie expiring after eight hours. Tokens and RCON passwords are never returned by API endpoints or placed in localStorage, URLs or frontend configuration.

Saved profiles, remembered passwords, Forge keys and Discord URLs are stored in `data/vault.enc` with Fernet authenticated encryption (AES-128-CBC + HMAC-SHA256). A key from `ONI_WEB_ENCRYPTION_KEY` is preferred for managed deployments; otherwise the backend creates `data/vault.key`. Back up **the key and vault together**, or supply the same environment key after restoring. A missing/wrong key or corrupt vault fails closed, preserving the file. Unix permissions are owner-only; on Windows keep the data folder under the service account's private profile with appropriate NTFS ACLs. Encryption does not protect a compromised running backend or an attacker who obtains both the key and vault.

Unchecked “Remember password” retains a password only in backend process memory; reconnecting after a backend restart requires entering it again. API profile reads expose only a `has_password` flag. Passwords and API keys are scrubbed from upstream replies/events, including structured output.

Run **one backend worker**. An OS lock protects the data directory, and the hub serializes fleet changes and file installs. Rate limits apply per station (default 180 calls/minute, including UI polls), with 16 in-flight calls maximum; login/connect attempts and automation chat have separate limits. Event subscribers and pending uploads are bounded. Slow event subscribers are disconnected and can recover a bounded recent replay. Commands are never automatically retried by the web bridge.

RCON's `ws://` transport is plaintext. Keep the game listener private and use SSH, a VPN or `wss://`. TLS verification remains enabled. Backend credentials are not compiled into the frontend build.

Environment options, including encryption, origin, rate, demo and cookie settings, are documented in [.env.example](.env.example). `ONI_WEB_HOST`/`PORT` change the listener; update allowed origins when changing the browser port/hostname.

### Docker

```sh
# Set ONI_WEB_TOKEN in .env first.
docker compose up --build -d
```

The image builds the React bundle, runs the Python service as an unprivileged user, and persists the vault/key in a named volume. The compose file publishes only on host loopback. SSH keys, trusted TOML and content folders require explicit operator-managed mounts. Docker builds and remote TLS/SSH deployments need validation on the target host.

## Scripting and piping

The optional standard-library client uses the same authenticated gateway. Set `ONI_WEB_TOKEN` in its environment if login is enabled; it is not a command-line argument.

```sh
python scripts/command.py --command 'say Server maintenance in ten minutes'
python scripts/command.py --index 1 --command status
python scripts/command.py --profile SAVED_PROFILE_ID --command players
python scripts/command.py --delay 2 < commands.txt
```

PowerShell piping: `Get-Content commands.txt | python scripts/command.py --delay 2`. Each line produces its RCON reply as JSON on stdout. Exit codes: `0` all replies successful, `1` a refusal/timeout, `2` a gateway/input failure. The helper waits for connection state, never retries a command, and never runs input through a host shell. A lost HTTP response may mean a command ran: inspect the console before issuing it again.

## Verification

```sh
python -m pytest -q          # web gateway, real loopback protocol peers, security, Forge, files, CLI
npm run test:core            # dependency-free Node 24 tests for the actual TypeScript domain modules
npm test                     # retained upstream React-library/Vitest tests
npm run build               # full TypeScript check + production Vite build
```

Upstream Python tests are preserved separately in `tests/upstream`; run them explicitly with `python -m pytest tests/upstream`. They include native Textual UI tests unrelated to the web renderer and are excluded from the web acceptance gate. See [the verification record](docs/verification.md), including the unchanged terminal-rendering baseline failure and environment limits. Do not interpret a source parse or protocol fixture test as a browser or real-game-server test.

## Project structure and adaptation

```text
backend/oni_web/        FastAPI boundary, encrypted vault, shared hub, Forge/file operations
backend/vendor/oni_rcon Original Python package, retained with provenance and licensing
frontend/src/          Adapted original React console and browser session/profile UI
frontend/src/web/      Same-origin HTTP/WebSocket replacement for Tauri IPC
scripts/command.py    JSON/stdin batch client
native/               Optional headless original Rust hosting/discovery implementation
tests/                Web acceptance tests and TypeScript regression tests
tests/upstream/       Original Python regression suite
docs/                 Source analysis, provenance inventory, config, proxy example and verification
```

`oni_rcon.rcon.Rcon` is reused **byte-for-byte**. FastAPI schedules it, sends commands through its `call()` method, and forwards its state, live-event and late-reply callbacks. Browser WebSockets carry sanitized application events; they never connect to the game server. The original React engine keeps command parsing, fan-out, game controls, findings, medals, search, content/playlist editors and automation. `web/bridge.ts` replaces Tauri invokes/listeners with same-origin gateway calls. Python adapters reuse the original Forge/health/install/state/rounds modules and translate the desktop-only file formats, publishing endpoints and performance calculations. The optional native helper reuses the original Rust platform operations rather than duplicating game installation logic in Python.

## License and credit

GPL-3.0-or-later; see [LICENSE](LICENSE), [NOTICE](NOTICE) and [vendored provenance](backend/vendor/UPSTREAM.md). Upstream is © 2026 Arche Labs with portions © 2026 ibigs, LLC. Original notices are retained. This fan-made tool is not affiliated with Microsoft, Halo Studios, Project Reclaimer or ReclaimerForge and contains no game files.
