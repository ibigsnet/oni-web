# IBIGS branch of oni-web

Upstream: https://github.com/darkxb23r/oni-web (GPL-3.0; keep LICENSE and NOTICE; we run it as a hosted
service per customer and publish our changes here). Base commit 1c3ab31.

## How IBIGS deploys it (pilot live on a customer VPS, 2026-10-16)

One instance per customer, inside the customer's own VM — never shared between customers:

- Container `ibigs/oni-web:<sha>` (host network, read-only, no-new-privileges, 1 GiB cap), listening on the
  VM's own 10.60 address, port 8470.
- `ONI_WEB_TOKEN` (48 hex, generated per VM, lives in `/srv/ibigs/oni-web/token`), `ONI_WEB_SECURE_COOKIE=true`,
  `ONI_WEB_ORIGINS=https://<name>.console.ibigs.us`.
- Profiles are seeded once from the stack's own `.env`/`dedicated.toml` via `ONI_RCON_CONFIG` (name, 127.0.0.1,
  the [[server]] port, the RCON password). No `health_cmd`/`ping_log`/`restart_cmd`: nothing in the vault can
  execute anything.
- TLS terminates at the node's NAT gateway (Caddy, HTTP-01, ports 80/443 opened in its nftables); the plain-HTTP
  hop is only the private 10.60 segment. DNS `<name>.console.ibigs.us` -> the gateway's public IP (Cloudflare).
- Setup script: workstation `oni-web-setup.sh` (to be folded into the template as an
  addon + the hosting panel button).

## Changes from upstream

- Dockerfile: debian 13 base already ships an `operator` group; `useradd` joins it instead of failing.

## Feature parity with the IBIGS oni TUI (to do)

The web console covers upstream oni-rcon (F1-F9 as README describes). Our TUI additions that call the `oni`
wrapper on the VM are NOT here yet; the container cannot exec the wrapper. Plan: a small authenticated bridge
(wrapper actions exposed by a host-side systemd socket, or run oni-web as a host service like the TUI) for:

- F7 lobby start/stop/restart + STARTING/WAITING states, REMOVE/RESTORE lobby (ibigs.23)
- F12 settings (live-apply on 0.9.18), Steam Workshop install, getting-started checklist
- F3 playlist deploy via the wrapper; one-click Forge DEPLOY (ibigs.24)
- F9 game/console updates with the progress view (ibigs.22)
- Superintendent status words for long operations (ibigs.24)
