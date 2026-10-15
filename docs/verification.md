# Verification record

Workspace: `oni-web`, 2026-10-15. No real game server was modified, no real Forge listing was published, and no native game provisioning ran during verification.

## Passing web acceptance checks

| Check | Result |
| --- | --- |
| `python -m pytest -q` | 24 passed; real loopback HTTP/WebSocket integration and domain/security tests |
| `node --test tests/frontend.test.mjs` | 7 passed against actual TypeScript modules using Node 24's TypeScript transform |
| Available TypeScript compiler source parse | All 37 `.ts`/`.tsx` files parsed with no syntax errors |
| Standalone browser bridge semantic/type check | Zero diagnostics with strict/unused checks |
| Python compileall and CLI `--help` | Passed |
| Frontend lock preparation | `npm install --package-lock-only --offline --ignore-scripts` succeeded with the retained upstream resolution and web package manifest |
| Vendored source comparison | Python source matches the original tracked-file hashes; original Rust implementation modules/resources are retained intact |

The web tests exercise actual loopback peers: JSON auth, request IDs, string arguments/operator attribution, out-of-order replies, live events, WebSocket fragmentation, responses over 1 MiB, terminal authentication refusal and socket closure, pending-call failure on disconnect, automatic reconnect, timeout/late reply without command resends, and shared upstream connections.

Security/storage checks cover encrypted profile persistence, absent plaintext credentials in public profile responses, edit-without-password semantics, session-only passwords, wrong-key preservation, login/logout/cookie flags, HTTP Host/Origin/custom-header checks, WebSocket origin/auth checks, command limits, stale fleet generation rejection, unknown executable settings, recursive secret scrubbing, private error logging, upload filename boundaries, single-use Forge install plans, manifest/destination hashes, trusted config import, and automation ownership/budgets. File/CLI/performance tests cover the adapted formats and calculations. Native adapter tests validate opt-in behavior, private stdin IPC, public progress, encrypted found credentials and imported host templates; they do not execute the Rust binary.

The TypeScript tests cover upstream quoting/free text, recursive address redaction, player-ID equivalence, medals, chat budgets, cancellation of delayed rule actions, and replacement of a setup progress WebSocket with a fresh console snapshot. Stale socket callbacks cannot change the new connection's state.

The available Python dependency installations were used read-only for local verification after registry access failed. This does not add a source/runtime dependency on the sibling repository: `run.py` loads this project's own vendored package. A fresh network-based install of requirements still needs validation in an unrestricted environment.

## Retained upstream regression suite

An explicit run including the original Python suite reported **168 passed, 1 failed, 2 skipped**, plus upstream simulator socket-cleanup warnings. At that point it included 12 web tests; the single failure was:

```text
tests/upstream/test_oni_rcon.py::test_feed_holds_still_and_keeps_its_width
```

The same test was run directly from the untouched `oni-rcon` clone and failed at the same line/assertion: a Textual RichLog line's width/wrapping expectation. This is a native terminal-renderer baseline failure, not the React web renderer. Original source/tests remain unchanged. The separate `tests/upstream` directory is excluded from the web test gate and can be run explicitly; its failure is not marked as passing or silently xfailed.

## Verification limits

The full React/Vite build and retained Vitest suite could not run. Outbound package installation was blocked (`WinError 10013` during pip access); an offline `npm ci` also failed because required tarballs, including `zustand`, were absent from the cache. Source parsing and bridge type checks do not substitute for a complete React type check or browser QA.

The optional native helper could not be compiled/tested because no Rust toolchain is installed. Its original modules are copied intact, but the new headless entry point needs a real Cargo build. The native CI job is supplied, not executed here. Run `cargo test --manifest-path native/Cargo.toml` and `cargo build --release --manifest-path native/Cargo.toml` before enabling it.

No real Project Reclaimer server, external Forge write, production TLS proxy, real SSH host, Docker image build, or game installation was used. The tests establish behavior against the upstream simulators and controlled peers. Validate those integrations on the intended deployment platform before treating the application as production-verified.

Required follow-up verification where dependencies/toolchains are available:

```sh
python -m pip install -r requirements-dev.txt
npm ci --prefix frontend
npm run build
npm test
python -m pytest -q
npm run test:core
# Optional hosting/discovery:
cargo test --manifest-path native/Cargo.toml
cargo build --release --manifest-path native/Cargo.toml
```

Then run `python run.py`, exercise the demo in a browser at desktop/tablet widths, and test an operator-controlled real game connection. Test native provisioning only on the intended backend host after reviewing its platform requirements and confirmation.
