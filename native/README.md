# Optional native helper

Build with stable Rust: `cargo build --release --manifest-path native/Cargo.toml`.
Run its unit tests with `cargo test --manifest-path native/Cargo.toml`.
Set `ONI_WEB_NATIVE_HELPER` to the resulting executable's absolute path.

This executable is a private FastAPI child process, not a network server or general command runner. It reads one bounded JSON request on stdin (`status`, `host`, or `find`) and emits JSON-line progress followed by one result. The result contains RCON credentials for backend-only encrypted storage. Never expose its stdout directly to a browser or keep raw IPC transcripts in logs.

`host.rs`, `config.rs`, `util.rs`, `rcon.rs`, `forge.rs`, `install.rs`, `health.rs`, `discover.sh` and `docker-health.sh` are unchanged upstream source at commit `a9550908bf838a55b0f705862d00a431c919bbb2`. `probe.rs` extracts the original setup authentication function with a local name change; `main.rs` adds its credential-redaction wrapper and replaces Tauri IPC. Cargo.lock starts from the original dependency resolution with the new root package; Cargo can remove unused Tauri lock entries on its first build. No Tauri dependency is built.

The gateway accepts only a backend-configured helper executable, validates SSH destinations and hosting settings, supplies initial playlists through private temporary files rather than browser-selected backend paths, streams public progress, and encrypts newly found credentials. Hosting is opt-in and requires an explicit confirmation in the browser. It acts on the backend account's game/Docker/SSH environment.

Native compilation and real provisioning were not executable in this workspace (no Rust toolchain). The Python IPC/persistence adapter is covered by tests. Validate the Rust build and the target platform before enabling provisioning; do not infer native verification from the web tests.

GPL-3.0-or-later; upstream © 2026 Arche Labs, portions © 2026 ibigs, LLC. See root LICENSE and NOTICE.
