"""Self-update. A release build swaps in the newest GitHub release for its next start; a pip/uv install is told how.

Set ONI_RCON_NO_UPDATE=1 to turn it off. Blocking network calls: run check() in a thread.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from . import __version__

REPO = "viik2k/oni-rcon"
RELEASES = f"https://github.com/{REPO}/releases/latest"
ASSET = {"win32": "oni-rcon-windows-x64.exe", "linux": "oni-rcon-linux-x64"}.get(sys.platform)


def fetch(url: str, timeout: float = 10) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": f"oni-rcon/{__version__}"}), timeout=timeout) as r:
        return r.read()


def newer(tag: str, than: str = __version__) -> bool:
    parse = lambda v: tuple(int(x) for x in v.lstrip("v").split("."))
    return parse(tag) > parse(than)


def check(exe: Path | None = None) -> str:
    """One line for the operator: what was updated or what to run. Empty when up to date, offline or turned off."""
    exe = exe or (Path(sys.executable) if getattr(sys, "frozen", False) else None)
    if exe:  # the copy the last update moved aside; still in use only if another window runs it
        with contextlib.suppress(OSError):
            exe.with_name(exe.name + ".old").unlink(missing_ok=True)
    if os.environ.get("ONI_RCON_NO_UPDATE"):
        return ""
    try:
        rel = json.loads(fetch(f"https://api.github.com/repos/{REPO}/releases/latest"))
        if not newer(tag := rel["tag_name"]):
            return ""
    except Exception:  # offline, rate limited, a tag that isn't x.y.z: try again next start
        return ""
    if not exe:
        return f"oni-rcon {tag} is out: uv tool upgrade oni-rcon (or pipx upgrade oni-rcon)"
    by_hand = f"oni-rcon {tag} is out, and the update failed: download it from {RELEASES}"
    try:
        asset = next(a for a in rel["assets"] if a["name"] == ASSET)
        data = fetch(asset["browser_download_url"], timeout=120)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if len(data) != asset["size"] or asset.get("digest", digest) != digest:
            return by_hand
        new, old = exe.with_name(exe.name + ".new"), exe.with_name(exe.name + ".old")
        new.write_bytes(data)
        new.chmod(exe.stat().st_mode)
        exe.rename(old)  # Windows can rename a running exe but not overwrite it
        try:
            new.rename(exe)
        except OSError:
            old.rename(exe)
            raise
    except Exception:  # read-only install dir, no asset for this platform, a cut-off download
        return by_hand
    return f"Updated to {tag}. Restart oni-rcon to use it."
