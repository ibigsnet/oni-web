"""Which servers to manage, how to reach them, and where each password comes from."""
from __future__ import annotations

import contextlib
import getpass
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Server:
    port: int = 0  # RCON TCP port: the server's game port unless rcon_port is set in dedicated.toml
    host: str = "127.0.0.1"  # RCON address; with ssh, as seen from the ssh destination
    name: str = ""  # label in the UI; empty = the name the server reports
    ssh: str = ""  # reach host:port through an SSH tunnel to this destination (key auth)
    url: str = ""  # ws:// or wss:// URL instead of host/port, e.g. behind an HTTPS reverse proxy
    password: str = field(default="", repr=False)
    password_env: str = ""
    password_command: str | list[str] = ""
    content_dir: str = ""  # where the server loads Forge content from; with ssh, a path on the ssh destination
    # not a [[server]] key (older versions refuse unknown ones): set from the top-level ping_log by load_config
    ping_log: str = field(default="", init=False)
    health_cmd: str = field(default="", init=False)  # likewise, from the top-level health_cmd; {since} is filled in per run

    def __post_init__(self):
        if not (self.url or self.port):
            raise ValueError("every server needs a port or a url")

    @property
    def where(self) -> str:
        return self.url or f"{self.host}:{self.port}" + (f" via ssh {self.ssh}" if self.ssh else "")


def parse_target(target: str, **common) -> Server:
    """PORT, HOST:PORT, [IPv6]:PORT or a ws(s):// URL."""
    if "://" in target:
        return Server(url=target, **common)
    host, _, port = target.rpartition(":")
    if not (port.isdigit() and 0 < int(port) < 65536):
        raise ValueError(f"{target!r} isn't PORT, HOST:PORT, [IPv6]:PORT or a ws(s):// URL")
    return Server(host=host.strip("[]") or "127.0.0.1", port=int(port), **common)


def user_config() -> Path:
    base = Path(os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "oni-rcon" / "config.toml"


def default_config() -> Path | None:
    for p in (os.environ.get("ONI_RCON_CONFIG"), "oni-rcon.toml", user_config()):
        if p and Path(p).is_file():
            return Path(p)
    return None


def load_config(path: Path) -> tuple[str, list[Server]]:
    """Returns (moderator name, servers). [defaults] applies to every [[server]] unless it sets the key itself."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"{path}: {e}") from None
    base = data.get("defaults", {})
    try:
        servers = [Server(**{**base, **s}) for s in data.get("server", [])]
    except (TypeError, ValueError) as e:
        raise SystemExit(f"{path}: {e}") from None
    if not servers:
        raise SystemExit(f"{path}: no [[server]] entries")
    tmpl = data.get("ping_log")
    if isinstance(tmpl, str):  # {port} = the server's RCON port; run on its ssh destination, or here without one
        for s in servers:
            s.ping_log = "" if s.url else tmpl.replace("{port}", str(s.port))
    cmd = data.get("health_cmd")
    if isinstance(cmd, str):  # same: {port} is this server's RCON port, and it runs where ping_log does
        for s in servers:
            s.health_cmd = "" if s.url else cmd.replace("{port}", str(s.port))
    return data.get("by", ""), servers


def resolve_passwords(servers: list[Server]) -> None:
    """password > password_env > password_command > $ONI_RCON_PASSWORD > one prompt shared by the rest."""
    ran: dict[str, str] = {}
    typed = None
    for s in servers:
        if s.password:
            pass
        elif s.password_env and os.environ.get(s.password_env):
            s.password = os.environ[s.password_env]
        elif s.password_command:
            key = repr(s.password_command)
            if key not in ran:
                ran[key] = _run(s.password_command)
            s.password = ran[key]
        elif os.environ.get("ONI_RCON_PASSWORD"):
            s.password = os.environ["ONI_RCON_PASSWORD"]
        else:
            if typed is None:
                try:
                    typed = getpass.getpass("RCON password (used for every server without its own): ")
                except (EOFError, KeyboardInterrupt):
                    raise SystemExit("no RCON password given") from None
            s.password = typed
        if not s.password:
            raise SystemExit(f"{s.where}: empty RCON password")


class CommandFailed(Exception):
    pass


def run_command(cmd: str | list[str], what: str) -> str:
    """The first line a command prints. A string runs through the shell; a list runs as-is (no quoting surprises,
    works the same on Windows)."""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:  # not installed, or waiting on something that never comes
        raise CommandFailed(f"{what} failed: {e}") from None
    if r.returncode:
        raise CommandFailed(f"{what} exited {r.returncode}: {r.stderr.strip()}")
    return r.stdout.splitlines()[0] if r.stdout else ""


def _run(cmd: str | list[str]) -> str:
    try:
        return run_command(cmd, "password_command")
    except CommandFailed as e:
        raise SystemExit(str(e)) from None


def forge_settings(path: Path | None) -> dict:
    """The config file's top-level forge_* keys: where the Forge API key comes from, and how often to check for
    updates. Empty without a file, or with one load_config has already said is unreadable."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, TypeError, tomllib.TOMLDecodeError):
        return {}
    return {k: v for k, v in data.items() if k.startswith("forge_")}


def health_settings(path: Path | None) -> dict:
    """The config file's top-level health_* keys (health_min_free_mb, health_service). Top level only: an older
    oni-rcon exits on a key it doesn't know inside [defaults] or [[server]], and ignores one out here."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, TypeError, tomllib.TOMLDecodeError):
        return {}
    return {k: v for k, v in data.items() if k.startswith("health_") and k != "health_cmd"}


def write_atomic(path: Path, text: str, private: bool = False) -> None:
    """Write through a temp file, so a crash midway leaves the old file whole. `private`: owner-only on Linux and
    macOS (Windows profiles are per-user already)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if private:
        with contextlib.suppress(OSError):
            tmp.chmod(0o600)
    os.replace(tmp, path)


def remember_forge_key(path: Path, key: str) -> None:
    """Save the Forge API key in the config file: only ever when the operator ticks for it. It goes in at the top,
    after the opening comments, where a top-level key is always valid TOML, or replaces one saved there before."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    line = f"forge_api_key = {toml_str(key)}\n"
    tables = next((i for i, x in enumerate(lines) if x.startswith("[")), len(lines))
    old = next((i for i, x in enumerate(lines[:tables]) if re.match(r"forge_api_key\s*=", x)), None)
    if old is not None:
        lines[old] = line
    else:
        lines.insert(next((i for i, x in enumerate(lines) if x.strip() and not x.lstrip().startswith("#")),
                          len(lines)), line)
    text = "".join(lines)
    if tomllib.loads(text).get("forge_api_key") != key:  # never leave the operator a config that won't load
        raise ValueError("the key couldn't be saved in that config file")
    write_atomic(path, text, private=True)


def toml_str(s: str) -> str:
    """A TOML basic string. Not json.dumps: JSON escapes astral characters as surrogate pairs, which TOML rejects."""
    out = "".join(f"\\u{ord(c):04x}" if ord(c) < 0x20 or ord(c) == 0x7F else "\\" + c if c in '"\\' else c for c in s)
    return f'"{out}"'


def add_servers(path: Path, servers: list[Server], by: str = "", remember: bool = True) -> None:
    """Append [[server]] blocks, so the comments and settings already in the file stay as they were."""
    new = not path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "" if new else path.read_text(encoding="utf-8")
    if new:
        text = ("# oni-rcon servers, written by the setup screen. Edit freely: oni-rcon.example.toml lists every option.\n"
                + (f"by = {toml_str(by)}\n" if by else ""))
    for s in servers:
        text += "\n[[server]]\n"
        if s.name:
            text += f"name = {toml_str(s.name)}\n"
        text += f"url = {toml_str(s.url)}\n" if s.url else f"host = {toml_str(s.host)}\nport = {s.port}\n"
        text += f"ssh = {toml_str(s.ssh)}\n"  # always set, so a [defaults] tunnel doesn't apply to a direct server
        if remember and s.password:
            text += f"password = {toml_str(s.password)}\n"
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    if remember:
        with contextlib.suppress(OSError):  # owner-only on Linux and macOS; Windows profiles are per-user already
            path.chmod(0o600)
