# oni-rcon source analysis

Analyzed local checkout: `C:\Users\death\Documents\GithubProjects\oni-rcon`, commit `a9550908bf838a55b0f705862d00a431c919bbb2`. Python version `0.9.0`; desktop version `1.6.1`. The tracked-file inventory records source hashes and sizes in [upstream-inventory.json](upstream-inventory.json).

There are 135 tracked paths. The referenced `docs/hosting-demo.mp4` is absent from this worktree and marked accordingly in the inventory; all available tracked source/config/documentation files were inventoried.

## What the original actually is

ONI RCON is an operator console specifically for **Project Reclaimer Halo 3 dedicated servers**. Its README reports testing against server 0.9.7; later desktop features target newer 0.9.x commands/config formats (for example shared-list admin commands require 0.9.16+). It is not a generic RCON protocol library and does not claim other games. The demo speaks the same JSON protocol and is useful for compatibility tests, not proof against every game-server release.

There are two applications:

| Source | Responsibility |
| --- | --- |
| `pyproject.toml`, `uv.lock` | Python 3.11+, Textual >=8, websockets >=15, pytest; `oni-rcon` entry point |
| `src/oni_rcon/__main__.py` | CLI targets, TOML selection, operator/password sources, demo/setup/intro, optional update handling |
| `src/oni_rcon/rcon.py` | JSON WebSocket RCON, authentication, request futures, live/late dispatch, retries; grouped OpenSSH tunnels |
| `src/oni_rcon/config.py`, example TOML | Target parsing, defaults, password/env/command resolution, atomic private TOML persistence |
| `src/oni_rcon/app.py`, `wizard.py`, `oni.tcss` | Textual application, setup, fleet polling, commands, seven panels and dialogs |
| `src/oni_rcon/medals.py`, `superintendent.py` | Kill-feed-derived medals and deterministic reactions |
| `src/oni_rcon/art.py`, `archive.py`, `emblem.txt`, `prefs.py` | ONI presentation, boot/archive and preferences |
| `src/oni_rcon/health.py` | Read-only host reports, crash signatures, bare engine failures, OOMs, memory/swap, container and systemd health |
| `src/oni_rcon/forge.py` | Forge key handling, HTTPS API, caching, quota/backoff, tolerant catalog readers and verified downloads |
| `src/oni_rcon/install.py` | Safe manifest paths, local/SSH copies, destination verification and per-file replacement |
| `src/oni_rcon/state.py`, `stats.py` | Installed versions/withdrawals, update acknowledgement and anonymized local round records |
| `src/oni_rcon/demo.py`, `forgefake.py` | Local RCON/game/host/Forge simulators |
| `src/oni_rcon/update.py` | Native executable/update checks |
| `desktop/package.json`, Vite/TS config | React 19, Zustand 5, JetBrains Mono, Vite 7, TypeScript 5.9, Vitest 3, Tauri 2 bindings |
| `desktop/src-tauri/Cargo.toml`, Cargo.lock | Rust 1.82+, Tokio, tungstenite, reqwest/rustls, serde, TOML editors, SHA-256, axum and Tauri plugins |
| `desktop/src-tauri/src/rcon.rs` | Independent Rust implementation of the same wire contract and tunnels |
| `desktop/src-tauri/src/lib.rs`, `hub.rs` | Native IPC surface, connection hub, 40 ms UI event batches, health/Forge watches and installs |
| `desktop/src-tauri/src/config.rs`, `util.rs` | Desktop config, secret scrubbing, process/filesystem helpers |
| `desktop/src-tauri/src/forge.rs`, `install.rs`, `fstate.rs`, `stats.rs` | Desktop Forge reads **and publishing**, installs, persisted state and rounds |
| `desktop/src-tauri/src/health.rs` | Health plus log-derived frame-rate analysis, 30-minute charts and 5-minute averages |
| `desktop/src-tauri/src/automation.rs` | Rules/roles storage, masked Discord webhook URLs, named webhook posting |
| `desktop/src-tauri/src/host.rs`, shell scripts | Steam/Docker provisioning, verified server releases, SSH discovery, dedicated.toml edits, playlists/game types |
| `desktop/src-tauri/src/demo.rs`, `fakeforge.rs` | Desktop simulators |
| `desktop/src/engine.ts`, `actions.ts` | Stations, polls, command/fleet execution, controls, events, conditions, findings and automation wiring |
| `desktop/src/lib/` | Quoting, redaction, IDs, search, content grouping, validation, findings, rules, runner, Forge and performance presentation |
| `desktop/src/components/`, `art/`, `theme.css` | Desktop panels, dialogs, forms, terminal presentation, archive art and Superintendent |
| `tests/`, desktop Rust/Vitest tests | Protocol/config/health/Forge/install/state/UI/domain regression tests |
| `docs/AUTOMATION.md`, `hosting.md`, `SERVER-PERFORMANCE.md` | Operator integration contracts and native hosting/performance behavior |
| `.github/workflows/`, packaging, release scripts | Python tests/binary builds, native signed desktop installers/updater releases |

## Wire protocol and exact completion semantics

The game opens a **WebSocket over TCP**, usually on the game port (example **49176**), with optional dedicated `rcon_port`. There is no Source length/ID/type/NUL binary packet header, UDP checksum, BattlEye login packet or Source terminator probe.

The JSON messages are:

```json
{"type":"auth","password":"<backend-held password>"}
{"type":"auth","ok":true,"server":"<server name>","version":"0.9.x"}
{"type":"command","command":"say","args":["hello everyone"],"id":1,"by":"Operator"}
{"type":"reply","id":1,"ok":true,"text":"<server text>","data":{}}
{"type":"event","event":"chat","name":"Player","text":"hello"}
```

The exact `text` and optional `data` values come from the game server. ONI does not invent a fixed result for an arbitrary command. Player/status field readers accept multiple spellings, and raw JSON remains available. Events include chat, kills, joins/leaves, moderation, control and voting; newer/unknown message types are surfaced as events.

In the Python implementation, command IDs are increasing integers. One pending future is stored per ID; a reply resolves that future independently of the order other replies/events arrive. One connection serves commands and events; the server admits four tools. The desktop limits fleet sign-ins to eight at a time and command fan-out to four at a time.

**“Multi-packet” here means WebSocket fragmentation/TCP segmentation.** The websockets runtime assembles a complete WebSocket message before JSON decoding. `max_size=None` permits large upstream messages. Upstream does **not** implement concatenation of several independent JSON reply messages with the same ID: the first reply completes the command. Inventing Source packet assembly would change the original behavior.

Opening/authenticating each have a 10-second timeout. `call()` defaults to 10 seconds. A timeout says the command may still run and **does not resend it**; up to 200 timed-out IDs are remembered so late replies can be logged. A disconnect fails pending futures and clears late IDs because replies belong to their original connection.

Connection failures retry after 2 seconds plus integer jitter up to half that delay. Backoff doubles to a 60-second base; a successful sign-in resets it. **Refused authentication is terminal** and never automatically retried: the upstream game can lock an address after five wrong passwords in ten minutes. A manual reconnect requires an explicit operator action. `ws://` is plaintext; `wss://` verifies TLS. The Python client disables automatic WebSocket proxies (`proxy=None`).

SSH is native OpenSSH key authentication, one process per destination carrying all its forwards. It binds local forwards to 127.0.0.1, probes readiness, uses keepalives and restarts with capped backoff. Browser packets must never bypass this backend layer.

## Results beyond the wire

The original application derives a presentation from replies/events: rosters and dossiers, bans/admins/VPN lists, maps/modes/playlists, vote state, colored command logs, medals from observed kills, alert conditions, admin-call findings, and Superintendent reactions. It polls the selected roster/status every three seconds and spreads other station status polls over fifteen seconds. Missing data stays unknown; a reconnect cannot reconstruct unseen medals or round boundaries.

Forge reads the real API using a private bearer key, honors quota and Retry-After, caches data, verifies manifest byte counts and SHA-256, and installs via local files or the configured SSH host. Update checks overlap the feed by five minutes and periodically reconcile installed listings in full. Withdrawals are acknowledged separately from removal. The desktop additionally uploads chunks, publishes drafts, edits/withdraws/deletes owned listings, and attempts to clean failed drafts.

Health runs operator-provided report commands, not automatic remediation. It correlates crashes with population and RCON drops, strips player identifiers/addresses from host logs, and tracks memory/swap/OOM/container/service data. The desktop also computes lag averages from server log reports. Rules are deliberately constrained to messages, Discord, waits, votes and shuffles; they are not arbitrary host scripts or automatic punishments. VIP vote protection is part of the original runner.

Original profiles/passwords are TOML (with env/command alternatives), not encrypted browser storage. Native windows can access local/remote server files and provision servers. These are separate from the wire protocol. The web feature map, optional native helper and deployment differences are in the root README.

## Adaptation decisions

FastAPI best reuses the existing Python RCON, Forge, install, health and state implementations. Reusing the React console retains far more behavior than replacing it with a text box. Tauri IPC is replaced by a same-origin HTTP/WebSocket gateway, with backend-only credentials and a shared fleet. Files/host commands originate only from trusted operator configuration. Browser profile endpoints accept a narrow connection model and cannot submit arbitrary executable settings.

New web protections include authenticated encryption, optional token sessions, Host/Origin/CSRF checks, bounded queues/uploads, command/in-flight limits, generation checks for stale station indices, and an automation lease. Browser stream replay never re-executes rule actions. This is GPL-derived code; the original source/notices are retained.

The remaining platform operations are retained as an optional headless Rust helper: seven modules and two shell resources are copied intact, with a small private JSON-lines adapter replacing Tauri. This introduces a Rust build requirement only for provisioning/discovery, in exchange for preserving the existing installer/discovery implementation and its validation rather than maintaining another platform-specific rewrite. The Python gateway still owns browser-facing APIs and encrypted persistence.
