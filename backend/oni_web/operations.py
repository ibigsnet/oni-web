"""Desktop file operations, constrained to operator-configured server folders.

No browser input becomes a shell command or arbitrary backend path. Remote commands
quote every path; executable settings are imported exclusively from a trusted TOML.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

import tomlkit

from oni_rcon import install
from oni_rcon.health import run
from . import server_settings
from .forge_service import NoRedirect
from .hub import Budget
from .vault import private_write


def slug(name):
    if not isinstance(name, str):
        raise ValueError("Give it a name")
    result = "-".join(re.findall(r"[a-z0-9]+", name.lower()))
    if not result or len(result) > 150:
        raise ValueError("Name must contain letters or numbers (up to 150)")
    return result


def integer(value, lo, hi, label):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{label} must be a whole number from {lo} to {hi}")
    return value


def check_playlist(p):
    if not isinstance(p, dict) or set(p) - {"version", "name", "vote_seconds", "games"}:
        raise ValueError("Unknown playlist setting")
    if p.get("version", 2) != 2:
        raise ValueError("Only version 2 playlists are supported")
    name = p.get("name", "").strip()
    slug(name)
    games = p.get("games", [])
    if not isinstance(games, list) or not 3 <= len(games) <= 128:
        raise ValueError("A playlist needs 3–128 games")
    vote = integer(p.get("vote_seconds", 20), 5, 120, "Vote duration")
    seen, out = set(), []
    for game in games:
        if not isinstance(game, dict) or set(game) - {"map", "game", "name", "weight", "options", "world", "rules", "anti_cheat", "voice_team_only"}:
            raise ValueError("Unknown game setting")
        mp, mode = game.get("map", ""), game.get("game", "")
        if not isinstance(mp, str) or not isinstance(mode, str) or not mp.strip() or not mode.strip():
            raise ValueError("Each game needs a map and game type")
        identity = (mp.strip(), mode.strip())
        if identity in seen:
            raise ValueError("The same map and game type appears twice; use a weight instead")
        seen.add(identity)
        weight = integer(game.get("weight", 1), 1, 100, "Weight")
        out.append({k: v for k, v in game.items() if v is not None and v != "" and not (k == "weight" and weight == 1)})
    return {"version": 2, "name": name, "vote_seconds": vote, "games": out}


class Files:
    """Exactly three path namespaces: content/, playlists/, and dedicated.toml."""
    def __init__(self, server):
        self.server = server
        target = install.target_for(server)
        if isinstance(target, str):
            raise ValueError(target)
        self.target = target

    def relative(self, path):
        if path == "dedicated.toml":
            return "../dedicated.toml"
        safe = install.safe_path(path)
        if safe.startswith("playlists/"):
            return "../" + safe
        if safe.startswith("content/"):
            return safe[len("content/"):]
        raise ValueError("Path must be inside content/ or playlists/")

    def local(self, path):
        rel = self.relative(path)
        root = self.target.root.expanduser().absolute()
        allowed = root if path.startswith("content/") else root.parent
        destination = root / rel
        # A symlinked content root or playlist folder cannot turn a web request into a write elsewhere.
        if root.is_symlink() or not destination.resolve().is_relative_to(allowed.resolve()) or destination.is_symlink():
            raise ValueError("Server path escapes through a symlink")
        if path.startswith("playlists/") and not destination.resolve().is_relative_to((root.parent / "playlists").resolve()):
            raise ValueError("Invalid playlist path")
        return destination

    async def read(self, path):
        if isinstance(self.target, install.LocalTarget):
            p = self.local(path)
            if p.stat().st_size > 2_000_000:
                raise ValueError("Server file exceeds 2 MB")
            return await asyncio.to_thread(p.read_text, encoding="utf-8")
        remote = install.rpath(self.target.root, self.relative(path))
        code, result = await self.target._ssh(f"head -c 2000001 -- {remote}", timeout=30)
        if code or len(result.encode()) > 2_000_000:
            raise ValueError("Cannot read server file or it exceeds 2 MB")
        return result

    async def write(self, path, text, replace):
        if len(text.encode()) > 2_000_000:
            raise ValueError("Server file exceeds 2 MB")
        if isinstance(self.target, install.LocalTarget):
            p = self.local(path)
            if p.exists() and not replace:
                return False
            await asyncio.to_thread(private_write, p, text.encode())
            return True
        dest = install.rpath(self.target.root, self.relative(path))
        folder = install.rpath(self.target.root, str(PurePosixPath(self.relative(path)).parent))
        # stdin is a local temporary file; its path never goes to the remote shell.
        with tempfile.TemporaryFile() as source:
            source.write(text.encode())
            source.seek(0)
            check = "" if replace else f"[ ! -e {dest} ] || exit 17; "
            cmd = f"mkdir -p -- {folder} && {check}umask 077; t=$(mktemp {dest}.XXXXXX) || exit 1; cat > \"$t\" && mv -- \"$t\" {dest}; r=$?; rm -f -- \"$t\"; exit $r"
            code, result = await self.target._ssh(cmd, stdin=source, timeout=30)
        if code == 17:
            return False
        if code:
            raise ValueError("Could not write server file")
        return True

    async def playlists(self):
        if isinstance(self.target, install.LocalTarget):
            root = self.local("playlists/placeholder.json").parent
            return sorted(p.name for p in root.glob("*.json") if not p.is_symlink())[:60]
        folder = install.rpath(self.target.root, "../playlists")
        code, output = await self.target._ssh(f"find {folder} -maxdepth 1 -type f -name '*.json' -printf '%f\\n'", timeout=30)
        if code:
            return []
        return sorted(install.safe_path(x) for x in output.splitlines() if x.endswith(".json") and "/" not in x)[:60]


async def dispatch(h, name, a):
    index = a["index"]
    h.station(index)
    server = h.servers[index]
    if name == "restart_server":
        cmd = h.profile(index).get("restart_cmd", "")
        if not cmd:
            raise ValueError("No restart_cmd in the trusted configuration")
        result = await run(cmd, server.ssh, 180)
        if result.strip().startswith("NOT RESTARTED"):
            raise ValueError(result.strip())
        return None
    files = Files(server)
    # Serialize operations with installs so existence checks and replacement decisions stay coherent.
    async with h.install_lock:
        if name in ("playlists", "playlist_library"):
            paths = ["playlists/" + p for p in await files.playlists()]
            if name == "playlists":
                return paths
            out = []
            for path in paths:
                row = {"path": path, "here": path.endswith(".playlist.json")}
                try:
                    value = json.loads(await files.read(path))
                    row.update(name=value.get("name", path), games=len(value.get("games", [])))
                except (OSError, ValueError) as e:
                    row["problem"] = str(e)
                out.append(row)
            return out
        if name == "playlist_read":
            path = a["path"]
            if not path.endswith(".json"):
                raise ValueError("Playlist files must be JSON")
            return json.loads(await files.read(path))
        if name == "save_playlist":
            value = check_playlist(a["playlist"])
            path = a.get("path") or f"playlists/{slug(value['name'])}.playlist.json"
            if not re.fullmatch(r"playlists/[a-zA-Z0-9][a-zA-Z0-9._-]*\.playlist\.json", path):
                raise ValueError("Only playlists made in this console may be overwritten")
            ok = await files.write(path, json.dumps(value, indent=2) + "\n", bool(a.get("path") or a.get("replace")))
            return {"path": path, "exists": not ok}
        if name == "save_game_type":
            title, mode = a["name"].strip(), a["mode"]
            if not 1 <= len(title) <= 48 or any(ord(c) < 32 for c in title):
                raise ValueError("A game type name is 1–48 printable characters")
            if mode not in ("Slayer", "CaptureTheFlag", "Oddball", "KingOfTheHill", "Vip", "Juggernaut", "Territories", "Assault", "Infection", "EscalationSlayer"):
                raise ValueError("Unknown base game type")
            ranges = {"rounds.teams": (0, 1), "rounds.minutes": (0, 60), "rounds.limit": (1, 10),
                      "respawn.lives": (0, 50), "respawn.seconds": (0, 240), "player.shields": (0, 5)}
            options = a.get("options", {})
            if not isinstance(options, dict):
                raise ValueError("Options must be an object")
            for k, v in options.items():
                if k not in ranges:
                    raise ValueError("Unsupported game option")
                integer(v, *ranges[k], k)
            score = a.get("score_to_win", a.get("scoreToWin"))
            rules = {} if score is None else {"score_to_win": integer(score, 0, 32767, "Score")}
            value = {"version": 1, "name": title, "mode": mode, "rules": rules, "options": options,
                     "script": {"version": 1, "name": title, "initial": []}}
            path = f"Game Modes/{slug(title)}.json"
            ok = await files.write("content/" + path, json.dumps(value, indent=2) + "\n", bool(a.get("replace")))
            return {"path": path, "exists": not ok}
        if name == "server_settings":
            # The settings page: the schema and this server's values (never the RCON password or ports).
            return server_settings.read_settings(tomlkit.parse(await files.read("dedicated.toml")), server.port)
        if name == "server_setting":
            # One setting ({key, value}) or a page's worth ({changes: {key: value}}), all checked by the schema
            # before anything is written; admin_add/admin_remove edit the server's own admins list.
            changes = a.get("changes")
            if changes is None:
                key, value = a["key"], a["value"]
                if key in ("admin_add", "admin_remove"):
                    value = server_settings.check_legacy_admin(value)
                changes = {key: value}
            if not isinstance(changes, dict) or len(changes) > len(server_settings.SCHEMA):
                raise ValueError("Changes must be a table of settings")
            text = await files.read("dedicated.toml")
            doc = tomlkit.parse(text)
            if set(changes) & {"admin_add", "admin_remove"}:
                if len(changes) != 1:
                    raise ValueError("Add or remove one admin at a time")
                (key, value), = changes.items()
                selected = server_settings.server_entry(doc, server.port)
                admins = list(server_settings.plain(selected.get("admins", [])))
                if (value in admins) == (key == "admin_add"):
                    raise ValueError("Admin is already present" if key == "admin_add" else "Admin is not present")
                selected["admins"] = admins + [value] if key == "admin_add" else [v for v in admins if v != value]
            else:
                server_settings.apply_changes(doc, server.port, changes)
            out = tomlkit.dumps(doc)
            tomlkit.parse(out)  # what is written must read back
            # Keep a rollback copy of the exact previous text on the same host.
            # Backup remains in the configured folder and contains secrets, so use private permissions.
            if isinstance(files.target, install.LocalTarget):
                p = files.local("dedicated.toml")
                await asyncio.to_thread(private_write, p.with_suffix(".toml.bak"), text.encode())
            await files.write("dedicated.toml", out, True)
            return {"applies": sorted({server_settings.BY_KEY[k]["applies"] for k in changes if k in server_settings.BY_KEY}),
                    "warnings": server_settings.id_warnings(changes)}
    raise ValueError("Unknown file operation")


WEBHOOK = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{5,30}/[A-Za-z0-9_-]{20,100}")


async def discord(h, name, a):
    hooks = {**h.vault.data["webhooks"]}
    key = a.get("name", "").lower().strip()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", key):
        raise ValueError("Use a webhook name with 1–32 letters, numbers, - or _")
    if name == "discord_add":
        url = a.get("url", "")
        if not isinstance(url, str) or not WEBHOOK.fullmatch(url):
            raise ValueError("Use a Discord HTTPS webhook URL")
        if key not in hooks and len(hooks) >= 25:
            raise ValueError("25 webhooks is the limit")
        hooks[key] = url
        h.vault.update(webhooks=hooks)
        return None
    if name == "discord_remove":
        hooks.pop(key, None)
        h.vault.update(webhooks=hooks)
        return None
    if key not in hooks:
        raise ValueError("No webhook by that name")
    h.command_budget.take("discord")
    if h.demo:
        return 204
    text = a.get("text", "")
    if not isinstance(text, str):
        raise ValueError("Discord message must be text")
    payload = json.dumps({"content": text[:2000], "username": "ONI RCON", "allowed_mentions": {"parse": []}}).encode()

    def post():
        req = Request(hooks[key], data=payload, headers={"Content-Type": "application/json", "User-Agent": "oni-web/1.0"}, method="POST")
        try:
            with build_opener(NoRedirect()).open(req, timeout=10) as response:
                return response.status
        except HTTPError as e:
            return e.code
        except (URLError, OSError):
            raise ValueError("Discord did not answer; message was not resent") from None
    return await asyncio.to_thread(post)
