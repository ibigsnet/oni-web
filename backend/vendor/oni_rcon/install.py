"""Forge content onto a server. A version's manifest names its files; each is downloaded and checked against the
manifest's size and SHA-256 before any is copied, then copied into the server's content_dir under a temporary name,
checked again where it landed, and only then moved into place.

A server behind an SSH tunnel gets its files over its own ssh process to the same destination, with the tunnel's
options (key authentication only, never a prompt): Windows' OpenSSH can't share the tunnel's connection. That takes a
POSIX shell and sha256sum on the game box, as the Reclaimer docker host has. A server on this machine gets a plain
copy. A server reached by a ws:// or wss:// link has no files oni-rcon can reach.

Whether the dedicated server picks new content up while it runs isn't known yet, so installing never loads anything:
loading is a separate step (F6, l).
"""
from __future__ import annotations

import asyncio
import os
import posixpath
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Server
from .forge import MAX_ASSET, file_sha, loopback, pick, sha_hex

SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+\-\[\]]*")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


class InstallError(Exception):
    """Why an install stopped, in plain words."""


@dataclass
class File:
    path: str  # inside content_dir, with / between folders
    url: str
    size: int
    sha256: str


def safe_path(name) -> str:
    """A manifest's file name as a path inside content_dir: nothing absolute, nothing hidden, nothing that climbs out."""
    raw = str(name or "")
    parts = raw.replace("\\", "/").split("/")
    if (not raw or len(parts) > 4 or re.match(r"[A-Za-z]:", raw)
            or not all(SAFE_PART.fullmatch(p) and not p.endswith((".", " ")) for p in parts)):
        raise InstallError(f"The manifest names a file oni-rcon won't write: {raw!r}. Nothing was installed.")
    return "/".join(parts)


def plan(manifest: dict) -> list[File]:
    """The files a manifest lists, each with the size and SHA-256 it must match, or InstallError when one can't be
    checked or wouldn't stay inside content_dir."""
    assets = pick(manifest, "assets", "files")
    if not isinstance(assets, list) or not assets:
        raise InstallError("That version's manifest lists no files.")
    out, seen = [], set()
    for a in assets:
        if not isinstance(a, dict):
            raise InstallError("That version's manifest isn't in a shape oni-rcon can read.")
        name = pick(a, "path", "name", "filename", "file")
        path, url, size = safe_path(name), pick(a, "url", "download_url", "href"), pick(a, "size", "bytes", "length")
        try:
            sha = sha_hex(pick(a, "sha256", "hash", "digest", "checksum"))
        except ValueError:
            raise InstallError(f"The manifest gives no SHA-256 for {path}, so it can't be checked. "
                               "Nothing was installed.") from None
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_ASSET:
            raise InstallError(f"The manifest gives {path} no size oni-rcon will accept. Nothing was installed.")
        if not url:
            raise InstallError(f"The manifest gives no link for {path}.")
        if path.lower() in seen:
            raise InstallError(f"The manifest lists {path} twice.")
        seen.add(path.lower())
        out.append(File(path, str(url), size, sha))
    return out


def size_words(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


# --- where the files go -----------------------------------------------------------------------------------------
class LocalTarget:
    def __init__(self, root: str):
        self.root, self.where = Path(root).expanduser(), "this computer"

    async def existing(self, paths: list[str]) -> set[str]:
        return {p for p in paths if (self.root / p).exists()}

    async def put(self, local: Path, f: File) -> None:
        def go():
            dest = self.root / f.path
            part = dest.with_name(dest.name + ".part")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local, part)
                if file_sha(part) != f.sha256:
                    raise InstallError(f"The copy of {f.path} doesn't match the manifest's SHA-256: removed.")
                os.replace(part, dest)
            except OSError as e:
                raise InstallError(f"Couldn't write {f.path} into {self.root}: {e.strerror or e}") from None
            finally:
                part.unlink(missing_ok=True)
        await asyncio.to_thread(go)


def rpath(root: str, rel: str = "") -> str:
    """A path on the game box for its shell: quoted, with a leading ~ left for that shell to expand."""
    full = posixpath.join(root, rel) if rel else root.rstrip("/") or "/"
    if full == "~":
        return '"$HOME"'
    if full.startswith("~/"):
        return '"$HOME"/' + shlex.quote(full[2:])
    return shlex.quote(full)


class SshTarget:
    def __init__(self, dest: str, root: str):
        self.dest, self.root, self.where = dest, root, dest

    async def _ssh(self, command: str, stdin=None, timeout: float = 600) -> tuple[int, str]:
        ssh = shutil.which("ssh")
        if not ssh:
            raise InstallError("no ssh client on PATH")
        proc = await asyncio.create_subprocess_exec(ssh, *SSH_OPTS, self.dest, command,
                                                    stdin=stdin or asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            raise InstallError("The game box stopped answering partway through.") from None
        finally:
            if proc.returncode is None:  # timed out, or the console is quitting: don't leave ssh behind
                proc.kill()
                await proc.wait()
        text = out.decode(errors="replace")
        if proc.returncode == 255:  # ssh itself failed: its words, which the console turns into plain ones
            lines = err.decode(errors="replace").strip().splitlines()
            raise InstallError(lines[-1] if lines else "ssh failed")
        return proc.returncode, text + err.decode(errors="replace")

    async def existing(self, paths: list[str]) -> set[str]:
        tests = "; ".join(f"[ -e {rpath(self.root, p)} ] && echo {shlex.quote(p)}" for p in paths)
        code, out = await self._ssh(f"{tests}; true", timeout=60)
        return {line for line in out.splitlines() if line in paths}

    async def put(self, local: Path, f: File) -> None:
        part, dest = rpath(self.root, f.path + ".part"), rpath(self.root, f.path)
        folder = rpath(self.root, posixpath.dirname(f.path)) if "/" in f.path else rpath(self.root)
        command = (f"command -v sha256sum >/dev/null 2>&1 || exit 4; mkdir -p -- {folder} || exit 5; "
                   f"cat > {part} || {{ rm -f -- {part}; exit 6; }}; "
                   f'if [ "$(sha256sum < {part} | cut -c1-64)" = {f.sha256} ]; then mv -f -- {part} {dest}; '
                   f"else rm -f -- {part}; exit 3; fi")
        with open(local, "rb") as fh:
            code, out = await self._ssh(command, stdin=fh)
        if code:
            said = out.strip().splitlines()[-1] if out.strip() else ""
            raise InstallError({
                3: f"The copy of {f.path} on {self.dest} doesn't match the manifest's SHA-256, so it was removed.",
                4: f"{self.dest} has no sha256sum, so a copy there can't be checked. Nothing was installed.",
                5: f"Couldn't make the folder for {f.path} on {self.dest}: {said}",
                6: f"Couldn't write {f.path} on {self.dest}: {said}",
            }.get(code, f"Copying {f.path} to {self.dest} failed: {said or f'exit {code}'}"))


def target_for(server: Server) -> LocalTarget | SshTarget | str:
    """Where a server's Forge content goes, or why it can't have any, in words."""
    if not server.content_dir:
        return ("This server has no content_dir: add content_dir = \"...\" to its [[server]] block in the config file, "
                "naming the folder the dedicated server loads content from (on the game box, with ssh).")
    if server.url:
        return ("This server is reached by a ws:// or wss:// link, so oni-rcon can't put files on it. Reach it with "
                "ssh instead.")
    if server.ssh:
        return SshTarget(server.ssh, server.content_dir)
    if loopback(server.host):
        return LocalTarget(server.content_dir)
    return "This server is on another machine with no ssh set, so oni-rcon can't put files on it. Set ssh for it."


def place(server: Server) -> tuple:
    """Servers with the same place share one content folder, and so one install."""
    return server.ssh, posixpath.normpath(server.content_dir) if server.content_dir else ""
