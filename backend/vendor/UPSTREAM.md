# Vendored source

Source: local clone of https://github.com/viik2k/oni-rcon, commit
`a9550908bf838a55b0f705862d00a431c919bbb2`.

Python package version: `0.9.0`. Desktop package version: `1.6.1`.

`oni_rcon/` is a byte-for-byte copy of the original `src/oni_rcon/` tracked source. It includes the terminal application for provenance and upstream regression tests; the web service imports its protocol and reusable domain modules. Generated `__pycache__` contents are not distributable source and are ignored.

`frontend/src/` originated from `desktop/src/`, with Tauri imports replaced by the browser bridge. The session/profile screens and browser stylesheet are new; the engine/runner also have browser lifecycle, replay and automation ownership changes. The frontend is a derivative, not an independent reimplementation.

The Rust desktop code informed `forge_service.py`, `operations.py`, and `performance.py`. The optional `native/` helper retains seven original Rust modules and two shell resources byte-for-byte. Its entry point replaces Tauri IPC; its extracted authentication probe adds credential scrubbing before reporting discovery progress. No original checkout is modified or needed at runtime.

License: GPL-3.0-or-later. Copyright © 2026 Arche Labs; portions © 2026 ibigs, LLC. The root LICENSE and NOTICE retain the upstream terms and historical MIT notice.
