"""Opt-in bridge to the unchanged native provisioning routines. No browser-supplied executable paths."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import tomllib
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .models import Profile
from .operations import check_playlist
from .vault import private_write


class HostPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=48)
    host: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=4096, repr=False)  # game join password, not RCON
    max_players: int = Field(default=16, ge=1, le=63, strict=True)
    hidden: bool = False
    internet: bool = False
    auto_update: bool = False
    playlist: dict | None = None


async def invoke(hub, action, args):
    remember = args.get("remember", True)
    if not isinstance(remember, bool):
        raise ValueError("remember must be true or false")
    executable = Path(hub.settings.native_helper) if hub.settings.native_helper else None
    if not executable or not executable.is_absolute() or not executable.is_file():
        if action == "status":
            return {"enabled": False, "reason": "Build the optional native helper and set ONI_WEB_NATIVE_HELPER to its absolute path"}
        raise ValueError("Native hosting/discovery is disabled. Configure ONI_WEB_NATIVE_HELPER on the backend")
    with tempfile.TemporaryDirectory(prefix="oni-native-") as scratch:
        request = {"action": action}
        join_password = ""
        if action == "host":
            plan = HostPlan.model_validate(args.get("settings", {}))
            join_password = plan.password
            if any(ord(c) < 32 for value in (plan.name, plan.host, plan.password) for c in value):
                raise ValueError("Names and passwords cannot contain control characters")
            settings = plan.model_dump(exclude={"playlist"})
            settings["playlist"] = ""
            if plan.playlist is not None:
                playlist = check_playlist(plan.playlist)
                path = Path(scratch) / "playlist.json"
                private_write(path, json.dumps(playlist).encode())
                settings["playlist"] = str(path)
            request["settings"] = settings
        if action == "find":
            destination = Profile(ssh=args.get("dest", "")).ssh
            if not destination:
                raise ValueError("Enter an SSH destination")
            request["dest"] = destination
        options = {"creationflags": 0x08000000} if os.name == "nt" else {}
        proc = await asyncio.create_subprocess_exec(str(executable), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=2_000_000, **options)
        result = None
        try:
            proc.stdin.write(json.dumps(request).encode() + b"\n")
            await proc.stdin.drain()
            proc.stdin.close()
            async with asyncio.timeout(1800 if action == "host" else 300):
                async for line in proc.stdout:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue  # child tool stdout is not a browser log
                    if not isinstance(message, dict):
                        continue
                    if message.get("type") == "progress":
                        text = str(message.get("text", ""))
                        if join_password:
                            text = text.replace(join_password, "[redacted]")
                        hub.emit({"kind": "setup-line", "text": text})
                    elif message.get("type") == "result":
                        result = message
                await proc.wait()
        except TimeoutError:
            raise ValueError("Native operation timed out. Inspect the backend host before trying again") from None
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        if not result or not result.get("ok"):
            error = result.get("error", "Native helper did not return a result") if result else "Native helper did not return a result"
            if join_password:
                error = error.replace(join_password, "[redacted]")
            raise ValueError(hub.clean(error))
        if action == "status":
            return {"enabled": True, **result["data"]}
        return await remember_found(hub, result["data"], remember)


async def remember_found(hub, data, remember):
    templates = {}
    for key, value in data.get("templates", []):
        if key in ("health_cmd", "ping_log"):
            templates[key] = tomllib.loads(f"value = {value}")["value"]
    async with hub.lock:
        profiles = [dict(p) for p in hub.profiles]
        found, added = [], 0
        for raw in data.get("servers", []):
            public = Profile(**{k: raw[k] for k in ("name", "host", "port", "ssh", "url", "password") if k in raw}).model_dump()
            identity = public["url"] or (public["host"], public["port"], public["ssh"])
            found.append({"where": public["url"] or f"{public['host']}:{public['port']}", "name": public["name"]})
            if any((p["url"] or (p["host"], p["port"], p["ssh"])) == identity for p in profiles):
                continue
            if len(profiles) >= 256:
                raise ValueError("256 profiles is the limit")
            item = {**public, "remember": remember, "id": uuid.uuid4().hex}
            for key in ("content_dir", "password_env", "password_command", "restart_cmd", "health_cmd", "ping_log"):
                item[key] = raw.get(key, "")
            for key, command in templates.items():
                item[key] = command.replace("{port}", str(public["port"]))
            profiles.append(item)
            added += 1
        hub.save_profiles(profiles)
        # Return only public metadata; native IPC credentials never reach the browser.
        return {"found": found, "added": added, "notes": data.get("notes", [])}
