"""One upstream connection per station, shared by every browser tab.

The actual wire implementation is the unmodified upstream oni_rcon.rcon.Rcon.
This layer owns lifecycle, browser fan-out, profile identity and command budgets.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import json
import os
import re
import secrets
import tempfile
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from websockets.exceptions import WebSocketException

from oni_rcon.config import Server, run_command
from oni_rcon.demo import FakeHost, serve_fakes
from oni_rcon.forge import scrub, scrub_data, utc_iso
from oni_rcon.health import HealthSetup, Monitor, parse_service, run, service_command
from oni_rcon.rcon import Rcon, Tunnel
from oni_rcon.state import ForgeState
from oni_rcon.stats import Rounds

from .models import Call, Profile
from .settings import Settings
from .vault import Vault
from .performance import Performance


class LimitError(Exception):
    pass


class Budget:
    """Sliding windows, bounded by station count; no unbounded per-IP limiter map."""
    def __init__(self, limit: int, seconds: float = 60):
        self.limit, self.seconds = limit, seconds
        self.used: dict[str, deque] = defaultdict(deque)

    def take(self, key: str):
        now = time.monotonic()
        # Expire inactive keys as well as timestamps (login keys are untrusted IPs).
        for old in list(self.used):
            if not self.used[old] or self.used[old][-1] <= now - self.seconds:
                del self.used[old]
        q = self.used[key]
        while q and q[0] <= now - self.seconds:
            q.popleft()
        if len(q) >= self.limit or len(self.used) > 10000:
            raise LimitError("Rate limit reached. Wait before trying again")
        q.append(now)


class Hub:
    def __init__(self, settings: Settings, vault: Vault):
        self.settings, self.vault = settings, vault
        self.lock = asyncio.Lock()
        self.profiles = copy.deepcopy(vault.data["profiles"])
        self.clients: list[Rcon] = []
        self.servers: list[Server] = []
        self.profile_ids: list[str] = []
        self.links: dict[int, dict] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.extra: list = []
        self.tunnels: list[Tunnel] = []
        self.tunnel_links: dict[str, dict] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.recent: deque[dict] = deque(maxlen=500)
        self.sequence = 0
        self.instance = uuid.uuid4().hex
        self.generation = 0
        self.running = False
        self.profiles_revision = 0
        self.active_revision = -1
        self.active_by = ""
        self.demo = False
        self.labels: list[str] = []
        self.gate = asyncio.Semaphore(8)
        self.command_budget = Budget(settings.commands_per_minute)
        self.automation_budget = Budget(20)
        self.inflight: dict[int, int] = defaultdict(int)
        self.fake_host = None
        self.scratch = None
        self.demo_forge = None
        self.monitor = Monitor()
        self.hsetup = HealthSetup()
        self.health_errors: dict[str, str] = {}
        self.services = {}
        self.performance: dict[str, Performance] = {}
        self.health_at = {}
        self.health_lock = asyncio.Lock()
        self.rounds = Rounds(settings.data_dir / "rounds.jsonl")
        self.fstate = ForgeState(settings.data_dir / "forge-state.json")
        self.forge = None
        self.forge_source = "environment" if os.getenv("RECLAIMERFORGE_API_KEY") else "encrypted vault"
        self.session_forge_key = ""
        self.plans = {}
        self.install_lock = asyncio.Lock()
        self.by = vault.data["by"]
        self.automation_owner = ""
        self.automation_until = 0.0

    def secrets(self):
        return {p.get("password", "") for p in self.profiles} | {c.password for c in self.clients} | {
            self.session_forge_key, self.vault.data.get("forge_key", ""), self.settings.token,
            os.getenv("ONI_RCON_PASSWORD", ""), os.getenv("RECLAIMERFORGE_API_KEY", ""),
            *self.vault.data.get("webhooks", {}).values(),
        }

    def clean(self, value):
        # Whitelist public profile fields separately. Upstream data is untrusted, including echoes.
        value = scrub_data(value)
        secrets = sorted((x for x in self.secrets() if x), key=len, reverse=True)

        def walk(v):
            if isinstance(v, dict):
                return {k: "[redacted]" if any(s in k.lower() for s in ("password", "secret", "token", "api_key"))
                        else walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, str):
                for secret in secrets:
                    v = v.replace(secret, "[redacted]")
                v = re.sub(r'''(?i)(\b(?:rcon[_ -]?)?password\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
                           r"\1[redacted]", v)
                # Terminal controls must not reach accessible labels or downloaded logs.
                return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
            return v
        return walk(value)

    def emit(self, message: dict):
        self.sequence += 1
        message = self.clean({**message, "seq": self.sequence})
        if message.get("kind") in ("event", "late", "log", "toast", "health-notice"):
            self.recent.append(message)
        for q in tuple(self.subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Never block the RCON receive loop on a slow browser. Close it so it can resync.
                self.subscribers.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"kind": "overflow"})

    def subscribe(self, after: int = 0):
        if len(self.subscribers) >= 32:
            raise LimitError("Too many browser event streams")
        q = asyncio.Queue(maxsize=1024)
        self.subscribers.add(q)
        history = [{**m, "replay": True} for m in self.recent if m["seq"] > after]
        # Current state follows replay. Replaying link transitions would reset medals and retrigger rules.
        return q, [{"kind": "stream", "instance": self.instance, "generation": self.generation},
                   *history, *({**m, "snapshot": True} for m in self.links.values()), *self.tunnel_links.values(),
                   {"kind": "health", "view": self.health_view()},
                   {"kind": "automation-changed"},
                   {"kind": "forge-state", "state": self.forge_state()}]

    def public_profiles(self):
        return [{k: p.get(k) for k in ("id", "name", "host", "port", "url", "ssh", "remember")} |
                {"has_password": bool(p.get("password") or p.get("password_env") or p.get("password_command")),
                 "connected": p["id"] in self.profile_ids and self.running} for p in self.profiles]

    def save_profiles(self, profiles: list[dict]):
        persisted = [{**p, "password": p.get("password", "") if p.get("remember", True) else ""} for p in profiles]
        self.vault.update(profiles=persisted)
        self.profiles = profiles
        self.profiles_revision += 1

    async def put_profile(self, spec: Profile, id_: str | None = None):
        async with self.lock:
            if len(self.profiles) >= 256 and id_ is None:
                raise ValueError("256 profiles is the limit")
            old = next((p for p in self.profiles if p["id"] == id_), None)
            if id_ and old is None:
                raise ValueError("No such profile")
            item = {**(old or {}), **spec.model_dump(exclude={"password"}), "id": id_ or uuid.uuid4().hex,
                    "password": spec.password if spec.password is not None else (old or {}).get("password", "")}
            identity = item["url"] or (item["host"], item["port"], item["ssh"])
            if any((p["url"] or (p["host"], p["port"], p["ssh"])) == identity and p["id"] != item["id"]
                   for p in self.profiles):
                raise ValueError("This server is already saved")
            self.save_profiles([item if p["id"] == id_ else p for p in self.profiles] + ([] if old else [item]))
            return next(p for p in self.public_profiles() if p["id"] == item["id"])

    async def delete_profile(self, id_: str):
        async with self.lock:
            if not any(p["id"] == id_ for p in self.profiles):
                raise ValueError("No such profile")
            # Close active links before a removed credential can be used again.
            await self.stop()
            self.save_profiles([p for p in self.profiles if p["id"] != id_])
            self.emit({"kind": "reload", "text": "Profiles changed. Reopen the console."})

    async def password_for(self, profile):
        if profile.get("password"):
            return profile["password"]
        if profile.get("password_env") and os.getenv(profile["password_env"]):
            return os.environ[profile["password_env"]]
        if profile.get("password_command"):
            # This field can only be imported from a trusted local config, never from an API model.
            return await asyncio.to_thread(run_command, profile["password_command"], "password command")
        return os.getenv("ONI_RCON_PASSWORD", "")

    async def start(self, demo=False, ids: list[str] | None = None, by: str | None = None):
        async with self.lock:
            if not demo:
                available = {p["id"] for p in self.profiles}
                if ids is not None and (not ids or set(ids) - available):
                    raise ValueError("Select saved server profiles")
                if not available:
                    raise ValueError("Add a server profile first")
            if by is not None:
                self.by = by
            try:
                return await self._start(demo, ids)
            except BaseException:
                await self.stop()
                raise

    async def _start(self, demo, ids):
        selected = [p for p in self.profiles if ids is None or p["id"] in ids]
        if not demo and not selected:
            raise ValueError("Add a server profile first")
        wanted = [p["id"] for p in selected]
        if self.running and self.demo == demo and self.active_by == self.by and (demo or
                self.profile_ids == wanted and self.active_revision == self.profiles_revision):
            return self.info()
        await self.stop()
        self.generation += 1
        self.demo = demo
        self.profile_ids = wanted
        self.monitor = Monitor()
        self.hsetup = HealthSetup.from_settings(self.vault.data.get("trusted", {}))
        self.monitor = Monitor(self.hsetup.min_free_mb)
        if demo:
            self.scratch = tempfile.TemporaryDirectory(prefix="oni-web-demo-")
            folders = [Path(self.scratch.name) / f"content-{i}" for i in range(3)]
            for d in folders:
                d.mkdir()
            self.fake_host = FakeHost()
            demo_password = secrets.token_urlsafe(24)
            ports, self.extra = await serve_fakes(password=demo_password, content_dirs=folders, host=self.fake_host)
            self.servers = [Server(port=p, password=demo_password, content_dir=str(d)) for p, d in zip(ports, folders)]
            from oni_rcon.forgefake import serve
            self.demo_forge = await asyncio.to_thread(serve)
            self.session_forge_key = self.demo_forge[1].key
            self.hsetup.service = "blueflame-demo"
            self.fstate = ForgeState(None)
            self.rounds = Rounds(None)
        else:
            self.servers = []
            for p in selected:
                s = Server(**{k: p.get(k, "") for k in ("host", "port", "name", "url", "ssh", "content_dir")})
                s.password = await self.password_for(p)
                s.health_cmd, s.ping_log = p.get("health_cmd", ""), p.get("ping_log", "")
                self.servers.append(s)
            self.fstate = ForgeState(self.settings.data_dir / "forge-state.json")
            self.rounds = Rounds(self.settings.data_dir / "rounds.jsonl")
        self.labels = [s.name or s.where for s in self.servers]
        remotes = defaultdict(list)
        for s in self.servers:
            if s.ssh and not s.url:
                remotes[s.ssh].append((s.host, s.port))
        for dest, rs in remotes.items():
            t = Tunnel(dest, rs, self.tunnel_state)
            self.tunnels.append(t)
            self.extra.append(asyncio.create_task(t.run()))
        self.clients = []
        for i, s in enumerate(self.servers):
            tunnel = next((t for t in self.tunnels if t.dest == s.ssh and not s.url), None)
            host = f"[{s.host}]" if ":" in s.host else s.host
            url = s.url or (f"ws://127.0.0.1:{tunnel.local[s.host, s.port]}" if tunnel else f"ws://{host}:{s.port}")
            client = Rcon(url, s.password, self.by,
                lambda c, ev, i=i: self.emit({"kind": "event", "index": i, "ev": ev}),
                lambda c, state, detail, i=i: self.state(i, c, state, detail),
                ready=tunnel.ready if tunnel else None, gate=self.gate,
                on_late=lambda c, line, reply, i=i: self.emit({"kind": "late", "index": i, "line": line, "reply": reply}))
            self.clients.append(client)
            if s.password:
                self.tasks[i] = asyncio.create_task(client.run())
            else:
                self.state(i, client, "offline", "Password required")
            if s.ping_log:
                self.extra.append(asyncio.create_task(self.follow_pings(i, s)))
        from .forge_service import connect_forge
        connect_forge(self)
        self.extra.append(asyncio.create_task(self.background()))
        self.running = True
        self.active_revision = self.profiles_revision
        self.active_by = self.by
        self.emit({"kind": "reload", "text": "The active fleet changed"})
        return self.info()

    def state(self, i, client, state, detail):
        previous = self.links.get(i, {}).get("state")
        retry = re.search(r"retry in (\d+)s", detail)
        note = {"kind": "link", "index": i, "state": state, "detail": detail,
                "info": self.clean(client.info), "retry_in": int(retry[1]) if retry else 0}
        self.links[i] = note
        if state != "online" and i < len(self.servers):
            self.rounds.lost(self.servers[i].where)
            if previous == "online":
                self.monitor.dropped(str(i))
        self.emit(note)

    def tunnel_state(self, tunnel, state, detail):
        note = {"kind": "tunnel", "dest": tunnel.dest, "state": state, "detail": detail}
        self.tunnel_links[tunnel.dest] = note
        self.emit(note)

    def station(self, index):
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(self.clients):
            raise ValueError("No such station")
        return self.clients[index]

    async def call(self, request: Call):
        c = self.station(request.index)
        self.command_budget.take(str(request.index))
        if self.inflight[request.index] >= self.settings.max_inflight:
            raise LimitError("Too many commands in flight on this server")
        self.inflight[request.index] += 1
        try:
            reply = await c.call(request.command, *request.args, timeout=request.timeout or 10)
            if request.command == "status" and reply.get("ok") and isinstance(reply.get("data"), dict):
                data = reply["data"]
                self.monitor.sample(str(request.index), data.get("players"))
                s = self.servers[request.index]
                self.rounds.observe(s.where, data, lambda m, g: self.fstate.playing(s.where, m, g))
            return reply
        except TimeoutError as e:
            return {"ok": None, "text": str(e)}
        except (OSError, ConnectionError, WebSocketException) as e:
            return {"ok": False, "text": str(e)}
        finally:
            self.inflight[request.index] -= 1

    async def reconnect(self, index, password=None):
        c = self.station(index)
        # Authentication refusal is terminal until an explicit reconnect with a new credential.
        if c.state == "denied" and not password:
            raise ValueError("Authentication was refused. Enter the password before reconnecting")
        if password is not None:
            if not isinstance(password, str) or not 8 <= len(password) <= 4096:
                raise ValueError("Enter a password (8–4096 characters)")
            c.password = password
        task = self.tasks.get(index)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.tasks[index] = asyncio.create_task(c.run())

    async def disconnect(self, index):
        c = self.station(index)
        task = self.tasks.pop(index, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.state(index, c, "offline", "Disconnected by operator; reconnect manually")

    def forge_state(self):
        return {**self.fstate.data, "alarms": self.fstate.alarms()}

    def health_view(self):
        m = self.monitor
        return {"on": bool(self.fake_host or any(s.health_cmd for s in self.servers)),
                "watched": [bool(self.fake_host or s.health_cmd) for s in self.servers],
                "crashes": [dataclasses.asdict(c) | {"detail": c.detail} for c in m.recent()],
                "hosts": {k: dataclasses.asdict(v) for k, v in m.hosts.items()},
                "boxes": {k: dataclasses.asdict(v) for k, v in m.boxes.items()},
                "services": self.services, "service": self.hsetup.service, "service_err": {},
                "errors": [self.health_errors.get(str(i), "") for i in range(len(self.servers))],
                "seen": bool(m.seen), "short": m.short(), "min_free": m.min_free,
                "frames": {k: p.reading(time.time()) for k, p in self.performance.items() if p.read_at is not None}}

    def info(self):
        from .forge_service import forge_status
        return self.clean({"by": self.by, "version": "web-1.0.0", "generation": self.generation, "demo": self.demo,
            "intro": "full" if self.vault.data["prefs"].get("full_intro") else "off",
            "full_intro": self.vault.data["prefs"].get("full_intro", False), "hint": "", "waiting":
            [i for i, c in enumerate(self.clients) if not c.password],
            "stations": [{"index": i, "where": s.where, "name": s.name, "ssh": s.ssh, "url": s.url,
                          "content_dir": s.content_dir, "host": s.host, "port": s.port,
                          "profile_id": self.profile_ids[i] if i < len(self.profile_ids) else "",
                          "restart": not self.demo and bool(self.profile(i).get("restart_cmd"))}
                         for i, s in enumerate(self.servers)],
            "tunnels": [t.dest for t in self.tunnels], "forge": forge_status(self),
            "forge_state": self.forge_state(), "health": self.health_view()})

    def profile(self, index):
        if self.demo:
            return {}
        return next((p for p in self.profiles if p["id"] == self.profile_ids[index]), {})

    async def health_now(self):
        if self.health_lock.locked():
            return
        async with self.health_lock:
            gate = asyncio.Semaphore(4)

            async def one(i, s):
                if not (self.fake_host or s.health_cmd):
                    return
                async with gate:
                    try:
                        since = "86400s" if str(i) not in self.health_at else f"{min(86400, max(900, int((time.time() - self.health_at[str(i)]) * 2 + 300)))}s"
                        text = await self.fake_host.report(s.port) if self.fake_host else await run(
                            s.health_cmd.replace("{since}", since), s.ssh)
                        for n in self.monitor.ingest(str(i), s.ssh, text):
                            self.emit({"kind": "health-notice", "index": i if n.key else None,
                                       "level": n.level, "text": n.text})
                        p = self.performance.setdefault(str(i), Performance())
                        if notice := p.ingest(text, time.time()):
                            self.emit({"kind": "health-notice", "index": i, "level": "warn", "text": notice, "quiet": True})
                        self.health_at[str(i)] = time.time()
                        self.health_errors.pop(str(i), None)
                    except Exception as e:
                        self.health_errors[str(i)] = f"{self.labels[i]}: {scrub(str(e))}"
            await asyncio.gather(*(one(i, s) for i, s in enumerate(self.servers)))
            if self.hsetup.service:
                for dest in {s.ssh for s in self.servers if s.health_cmd or self.fake_host}:
                    try:
                        text = await self.fake_host.service(self.hsetup.service) if self.fake_host else await run(
                            service_command(self.hsetup.service), dest)
                        self.services[dest] = dataclasses.asdict(parse_service(text, time.time()))
                    except Exception as e:
                        self.health_errors["service"] = scrub(str(e))
            self.emit({"kind": "health", "view": self.health_view()})

    async def background(self):
        from .forge_service import watch
        health_at = forge_at = 0
        while True:
            now = time.monotonic()
            if now >= health_at:
                await self.health_now()
                health_at = now + (3 if self.demo else 60)
            if now >= forge_at:
                try:
                    await watch(self)
                except Exception as e:
                    self.emit({"kind": "log", "station": "FORGE", "text": scrub(str(e)), "color": "red"})
                forge_at = now + max(60, self.vault.data.get("trusted", {}).get("forge_poll", 600))
            await asyncio.sleep(1)

    async def follow_pings(self, i, s):
        """The original log follower, including remote cleanup on disconnect and jitter-free backoff."""
        import shutil
        delay = 2
        while True:
            if s.ssh:
                ssh = shutil.which("ssh")
                if not ssh:
                    return
                proc = await asyncio.create_subprocess_exec(ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                    s.ssh, f"{s.ping_log} & p=$!; cat >/dev/null; kill $p", limit=1 << 20,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            else:
                proc = await asyncio.create_subprocess_shell(s.ping_log, limit=1 << 20,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                async for line in proc.stdout:
                    match = re.search(r"player ID ([0-9a-f]+), ping (\d+) ms", line.decode(errors="replace"))
                    if match:
                        delay = 2
                        self.emit({"kind": "ping", "index": i, "id": match[1], "ms": int(match[2])})
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def stop(self):
        if self.forge:
            self.forge.stop.set()
        async with self.install_lock:
            await self._stop()

    async def _stop(self):
        self.running = False
        if self.forge:
            self.forge.stop.set()
        tasks = [*self.tasks.values(), *(x for x in self.extra if isinstance(x, asyncio.Task))]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for handle in self.extra:
            if hasattr(handle, "close"):
                handle.close()
                if hasattr(handle, "wait_closed"):
                    await handle.wait_closed()
        self.tasks.clear()
        self.extra.clear()
        self.clients.clear()
        self.servers.clear()
        self.links.clear()
        self.tunnels.clear()
        self.tunnel_links.clear()
        self.recent.clear()
        self.plans.clear()
        self.inflight.clear()
        self.health_errors.clear()
        self.health_at.clear()
        self.services.clear()
        self.performance.clear()
        self.automation_owner = ""
        self.automation_until = 0
        self.fake_host = None
        self.session_forge_key = ""
        if self.demo_forge:
            await asyncio.to_thread(self.demo_forge[2].shutdown)
            self.demo_forge[2].server_close()
            self.demo_forge = None
        if self.scratch:
            self.scratch.cleanup()
            self.scratch = None
