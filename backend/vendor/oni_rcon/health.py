"""Host and server health: crashes read out of each server's log, and the host's memory.

One command per server (the top-level `health_cmd`, run where `ping_log` runs) prints a plain-text report:

    host now=1760083200 clock=08:15:01 mem_available_mb=812 swap_used_mb=310 swap_total_mb=2048 oom_kill=2
    container started=2026-10-10T08:12:44Z finished=0001-01-01T00:00:00Z oom_killed=false exit_code=0 restarts=1
    <every other line is a line of the server's log>

`{"kind": "host", ...}` and `{"kind": "container", ...}` as JSON lines read the same. Nothing here writes to a server,
restarts one or kills one: the commands only read.

Player IDs and IPv4 addresses are stripped from every line the moment it's read. What's kept from a log is only
what a pattern below picked out: a time, an exception code, a module, an RVA, an exit code.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .install import SSH_OPTS

# The 0.9.11 image under Wine crashes the game process when a player with the blue developer helmet is in the game.
# The log says so in three lines; the first is missing from the other kind of crash, which is a different fault.
#   Experimental startup exception 0xC0000005 in halo3.dll at RVA 0x14C73E
#   Error: Engine probe worker failed: exit code: 1
#   HH:MM:SS [<server>] stopped (exit code: 1); restarting in 5 s.
EXCEPTION = re.compile(r"exception\s+(0x[0-9a-f]{1,16})\s+in\s+([\w.\-]{1,40})(?:\s+at\s+RVA\s+(0x[0-9a-f]{1,16}))?", re.I)
PROBE = re.compile(r"Engine probe worker failed:\s*exit code:\s*(-?\d+)", re.I)
STOPPED = re.compile(r"(?:^|\s)(\d\d):(\d\d):(\d\d)\s+\[[^\]]*\]\s+stopped \(exit code:\s*(-?\d+)\);\s*restarting in \d+\s*s",
                     re.I)
# The service that patches the helmet bug (halo-blueflame) says these in its journal.
MOVED_TAG = re.compile(r"moved tag", re.I)
NOT_PINNED = re.compile(r"NOT the pinned build", re.I)

STAMP = re.compile(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d(?::?\d\d)?)?)\s+")  # docker, journalctl
HEX_ID = re.compile(r"\b[0-9a-f]{32,}\b", re.I)  # a player ID, whether 32 or 64 digits long
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
UNIT = re.compile(r"[A-Za-z0-9@._:\-]+")  # a systemd unit name: nothing a shell would read

SIGNATURE, BARE, OOM, RESTART = "SIGNATURE", "BARE", "OOM-KILL", "CONTAINER-RESTART"
DAY = 86400
DUP = 2.0  # seconds: one crash read twice (a bare clock time can come out a second apart) is still one crash
DROP_NEAR = 90.0  # seconds between a crash in the log and the RCON drop that goes with it (the two clocks differ a little)
SAME_OOM = 180.0  # seconds within which the host's counter and a container's own flag are the same kill
STALE = 180.0  # a player count older than this, before a crash, isn't the crowd that was there
LAST = 5  # crashes kept in view per server
MAX_LINES = 50_000


def hexed(v: str | None) -> str:
    """0xc0000005 as 0xC0000005, so the same fault always reads the same."""
    return "0x" + v[2:].upper() if v else ""


def scrub(text: str) -> str:
    """text without player IDs or IPv4 addresses."""
    return IPV4.sub("[ip]", HEX_ID.sub("[id]", text))


# --- the report ------------------------------------------------------------------------------------------------
def parse_ts(s: str) -> float | None:
    """An RFC 3339 time (docker --timestamps, journalctl short-iso) as epoch seconds; UTC without a zone. None for
    docker's 'never' (year 1)."""
    try:
        d = datetime.fromisoformat(s.strip().replace(",", "."))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp() if d.year > 2000 else None


def split_stamp(line: str) -> tuple[float | None, str]:
    m = STAMP.match(line)
    return (parse_ts(m[1]), line[m.end():]) if m else (None, line)


def _num(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None


def _flag(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


@dataclass
class Box:
    """A container, as docker inspect says."""
    started: float | None = None
    finished: float | None = None
    oom_killed: bool = False
    exit_code: int | None = None
    restarts: int | None = None


@dataclass
class Report:
    host: dict = field(default_factory=dict)
    box: Box | None = None
    lines: list = field(default_factory=list)  # the log, scrubbed


def record(line: str) -> tuple[str, dict] | None:
    """(kind, fields) for a host or container line, in key=value or JSON; None for anything else."""
    if line.startswith("{"):
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if isinstance(d, dict) and d.get("kind") in ("host", "container"):
            return d["kind"], {k: str(v) for k, v in d.items() if k != "kind"}
        return None
    head, _, rest = line.partition(" ")
    if head in ("host", "container") and "=" in rest:
        return head, dict(w.split("=", 1) for w in rest.split() if "=" in w)
    return None


def parse_report(text: str) -> Report:
    rep = Report()
    for raw in text.splitlines()[:MAX_LINES]:
        line = raw.strip()
        if not line:
            continue
        got = record(line)
        if got is None:
            rep.lines.append(scrub(line))
        elif got[0] == "host":
            f = got[1]
            rep.host = {k: n for k in ("now", "mem_available_mb", "swap_used_mb", "swap_total_mb", "oom_kill")
                        if (n := _num(f.get(k))) is not None}
            if re.fullmatch(r"\d\d:\d\d:\d\d", f.get("clock", "")):
                rep.host["clock"] = f["clock"]
        else:
            f = got[1]
            rep.box = Box(parse_ts(f.get("started", "")), parse_ts(f.get("finished", "")), _flag(f.get("oom_killed")),
                          _num(f.get("exit_code")), _num(f.get("restarts")))
    return rep


def clock_for(host: dict, now: float) -> Callable[[int, int, int], float]:
    """Turns a bare HH:MM:SS from a log into epoch seconds: the latest moment that clock face showed, counting back
    from the host's own now and clock when the report gives them, from `now` here when it doesn't."""
    ref = host.get("now", now)
    if "clock" in host:
        h, m, s = map(int, host["clock"].split(":"))
        sod = h * 3600 + m * 60 + s
    elif "now" in host:
        sod = int(ref) % DAY
    else:
        lt = time.localtime(ref)
        sod = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    return lambda h, m, s: ref - ((sod - (h * 3600 + m * 60 + s)) % DAY)


# --- crashes ---------------------------------------------------------------------------------------------------
@dataclass
class Crash:
    ts: float
    cls: str
    key: str = ""  # the station it happened on; empty for the host as a whole
    host: str = ""  # the ssh destination, or "" for this machine
    code: str = ""
    module: str = ""
    rva: str = ""
    exit: int | None = None
    players: int | None = None  # how many were on, as far as this console saw
    rcon: bool = False  # the RCON connection dropped at that moment too

    @property
    def detail(self) -> str:
        if self.cls == SIGNATURE:
            return f"{self.code} {self.module}+{self.rva}"
        if self.cls == BARE:
            return f"engine probe failed, exit {self.exit}, no exception" if self.exit is not None else "no exception"
        if self.cls == OOM:
            return f"exit {self.exit}" if self.exit is not None else "counter rose"
        return "container started again"


def crashes_in(lines: list[str], at: Callable[[int, int, int], float]) -> list[Crash]:
    """The crashes in a server's log. A crash is the engine probe's failure, with the exception before it when there
    is one (SIGNATURE) and without when there isn't (BARE: another fault), and the 'stopped' line after it. A line
    the log says twice is read once. A stop with neither behind it is a restart for another reason: not a crash."""
    out: list[Crash] = []
    cur: dict | None = None

    def flush(stop_ts: float | None = None, code: int | None = None) -> None:
        nonlocal cur
        if cur and (cur["exc"] or cur["probe"] is not None):
            ts = stop_ts or cur["ts"]
            if ts:
                exc = cur["exc"]
                exit_code = code if code is not None else cur["probe"]
                if exc and exc[2]:
                    out.append(Crash(ts, SIGNATURE, code=exc[0], module=exc[1], rva=exc[2], exit=exit_code))
                else:
                    out.append(Crash(ts, BARE, exit=exit_code))
        cur = None

    for line in lines:
        stamp, body = split_stamp(line)
        if m := EXCEPTION.search(body):
            exc = (hexed(m[1]), m[2], hexed(m[3]))
            if cur and (cur["probe"] is not None or (cur["exc"] and cur["exc"] != exc)):
                flush()
            cur = cur or {"exc": None, "probe": None, "ts": None}
            cur["exc"] = exc
            cur["ts"] = cur["ts"] or stamp
        elif m := PROBE.search(body):
            cur = cur or {"exc": None, "probe": None, "ts": None}
            if cur["probe"] is None:
                cur["probe"] = int(m[1])
            cur["ts"] = cur["ts"] or stamp
        elif (m := STOPPED.search(body)) and cur:
            flush(stamp or at(int(m[1]), int(m[2]), int(m[3])), int(m[4]))
    flush()
    return out


# --- levels ----------------------------------------------------------------------------------------------------
def mem_level(avail_mb: int | None, min_free_mb: int) -> str:
    """crit under health_min_free_mb (the alert), warn under twice that, else ok; '' when the host didn't say."""
    if avail_mb is None:
        return ""
    return "crit" if avail_mb < min_free_mb else "warn" if avail_mb < 2 * min_free_mb else "ok"


def swap_level(used_mb: int | None, total_mb: int | None) -> str:
    """Swap in use: warn from a quarter of it, crit from a half. Nothing to say when there's no swap."""
    if used_mb is None or not total_mb:
        return ""
    return "crit" if used_mb * 2 >= total_mb else "warn" if used_mb * 4 >= total_mb else "ok"


# --- the monitor -----------------------------------------------------------------------------------------------
@dataclass
class Notice:
    level: str  # crit or warn
    key: str  # the station, or "" for the host
    text: str


@dataclass
class Host:
    avail: int | None = None
    swap_used: int | None = None
    swap_total: int | None = None
    oom: int | None = None  # the cgroup's oom_kill counter
    oom_first: int | None = None  # what it was when this console first looked
    at: float = 0.0


@dataclass
class Service:
    active: str = ""
    moved: float | None = None  # the last "moved tag" line
    warn: str = ""  # the last "NOT the pinned build" line, scrubbed
    warn_at: float | None = None
    at: float = 0.0


class Monitor:
    """What the health command's reports add up to. Crashes are kept for a day. A crash that turns up in a report is
    news (a notice) unless this console had no earlier look at that server and the crash came before it started."""

    def __init__(self, min_free_mb: int = 1024, started: float | None = None):
        self.min_free = min_free_mb
        self.started = time.time() if started is None else started
        self.events: list[Crash] = []
        self.hosts: dict[str, Host] = {}
        self.boxes: dict[str, Box] = {}
        self.services: dict[str, Service] = {}
        self.seen: set[str] = set()  # stations with a report in
        self.low: dict[str, bool] = {}  # hosts short of memory right now
        self.counts: dict[str, deque] = {}  # key -> (when, players) as the roster was read
        self.drops: dict[str, deque] = {}  # key -> when its RCON connection dropped

    # what the rest of the console tells us
    def sample(self, key: str, players: int | None, now: float | None = None) -> None:
        if players is not None:
            self.counts.setdefault(key, deque(maxlen=400)).append((time.time() if now is None else now, players))

    def dropped(self, key: str, now: float | None = None) -> None:
        """The RCON connection to a server went away: the server restarted, whatever the log says."""
        now = time.time() if now is None else now
        self.drops.setdefault(key, deque(maxlen=50)).append(now)
        for e in self.events:  # the log got there first
            if e.key == key and not e.rcon and abs(e.ts - now) <= DROP_NEAR:
                e.rcon, e.players = True, self.count_at(key, now)

    def count_at(self, key: str, t: float) -> int | None:
        for when, n in reversed(self.counts.get(key, ())):
            if when <= t + 1:
                return n if t - when <= STALE else None
        return None

    def drop_near(self, key: str, t: float) -> float | None:
        near = [d for d in self.drops.get(key, ()) if abs(d - t) <= DROP_NEAR]
        return min(near, key=lambda d: abs(d - t)) if near else None

    # reports in
    def ingest(self, key: str, host: str, text: str, now: float | None = None) -> list[Notice]:
        """Fold one server's report in. Returns what's news."""
        now = time.time() if now is None else now
        rep = parse_report(text)
        notes = self._host(host, rep.host, now) if rep.host else []
        found = crashes_in(rep.lines, clock_for(rep.host, now))
        if rep.box:
            found += self._box(key, host, rep.box, now)
            self.boxes[key] = rep.box
        for ev in found:
            if now - ev.ts >= DAY:  # older than anything kept: not a crash to hold, or to say twice
                continue
            ev.key, ev.host = key, host
            if self._add(ev) and (key in self.seen or ev.ts >= self.started):
                notes.append(self.notice(ev))
        self.seen.add(key)
        self.events = sorted((e for e in self.events if now - e.ts < DAY), key=lambda e: e.ts)[-500:]
        return notes

    def _box(self, key: str, host: str, box: Box, now: float) -> list[Crash]:
        out, prev = [], self.boxes.get(key)
        if box.oom_killed or box.exit_code == 137:
            out.append(Crash(box.finished or now, OOM, exit=box.exit_code))
        if prev and prev.started and box.started and box.started != prev.started and not out and not any(
                e.cls == OOM and e.key == key and box.started - 300 <= e.ts <= box.started + 5 for e in self.events):
            out.append(Crash(box.started, RESTART))  # an OOM-killed container restarts too: that's the OOM's row
        return out

    def _host(self, host: str, h: dict, now: float) -> list[Notice]:
        old, notes = self.hosts.get(host), []
        snap = self.hosts[host] = Host(h.get("mem_available_mb"), h.get("swap_used_mb"), h.get("swap_total_mb"),
                                       h.get("oom_kill"), old.oom_first if old and old.oom_first is not None
                                       else h.get("oom_kill"), now)
        if snap.avail is not None:
            low = snap.avail < self.min_free
            if low and not self.low.get(host):
                notes.append(Notice("crit", "", f"LOW MEMORY  {snap.avail} MB available, under the {self.min_free} MB "
                                                f"limit{f' ({scrub(host)})' if host else ''}"))
            self.low[host] = low or (self.low.get(host, False) and snap.avail < self.min_free * 1.1)  # eased, not flapping
        if old and old.oom is not None and snap.oom is not None and snap.oom > old.oom:
            ev = Crash(now, OOM, host=host)
            if self._add(ev):
                notes.append(self.notice(ev, f"the kernel killed {snap.oom - old.oom} process"
                                             f"{'es' if snap.oom - old.oom > 1 else ''}, host oom_kill is {snap.oom}"))
        return notes

    def _add(self, ev: Crash) -> bool:
        """Keep ev unless it repeats one already held. True when it's news: not a repeat, and not the same OOM kill
        the host's counter has already announced."""
        for e in self.events:
            if (e.cls, e.key, e.host, e.code, e.rva) == (ev.cls, ev.key, ev.host, ev.code, ev.rva) and abs(e.ts - ev.ts) <= DUP:
                return False
        news = True
        if ev.cls == OOM:
            near = [e for e in self.events if e.cls == OOM and e.host == ev.host and e.key != ev.key
                    and abs(e.ts - ev.ts) <= SAME_OOM]
            if near and not ev.key:
                return False  # a container's own row already says it
            for e in near:
                if not e.key:  # the counter got there first: this row, which names the server, replaces it
                    self.events.remove(e)
                    news = False
        if ev.key:
            drop = self.drop_near(ev.key, ev.ts)
            ev.rcon = drop is not None
            ev.players = self.count_at(ev.key, drop if drop is not None else ev.ts)
        self.events.append(ev)
        return news

    @staticmethod
    def notice(ev: Crash, said: str = "") -> Notice:
        what = {SIGNATURE: f"SIGNATURE CRASH  {ev.code} in {ev.module} at RVA {ev.rva}",
                BARE: "BARE CRASH  engine probe failed, no exception line",
                OOM: f"OOM-KILL  {said or 'the container was killed for memory'}",
                RESTART: "CONTAINER RESTART  the container started again"}[ev.cls]
        drop = f"  ·  {ev.players} player{'s' if ev.players != 1 else ''} dropped" if ev.players else ""
        return Notice("warn" if ev.cls == RESTART else "crit", ev.key, what + drop)

    # what the tab shows
    def recent(self, per_server: int = LAST) -> list[Crash]:
        """The latest crashes, newest first, at most per_server for each server."""
        out, n = [], Counter()
        for e in sorted(self.events, key=lambda e: -e.ts):
            if n[e.key] < per_server:
                n[e.key] += 1
                out.append(e)
        return out

    def short(self) -> bool:
        return any(self.low.values())


# --- the workaround service ------------------------------------------------------------------------------------
def service_command(unit: str) -> str:
    """Read-only: whether the unit is active, and what its journal said about moving a tag or the wrong build."""
    if not UNIT.fullmatch(unit):
        raise ValueError(f"{unit!r} isn't a systemd unit name")
    u = shlex.quote(unit)
    return (f"systemctl is-active {u}; journalctl -u {u} --no-pager -o short-iso --since '24 hours ago' 2>&1 "
            f"| grep -iE 'moved tag|not the pinned build' | tail -n 40")


def parse_service(text: str, now: float) -> Service:
    """The service's state (first line) and the times of its last "moved tag" and "NOT the pinned build" lines."""
    lines = [scrub(x.strip()) for x in text.splitlines() if x.strip()]
    svc = Service(lines[0].split()[0].lower() if lines else "unknown", at=now)
    for line in lines[1:]:
        ts, body = split_stamp(line)
        if MOVED_TAG.search(body):
            svc.moved = ts or svc.moved
        if NOT_PINNED.search(body):
            svc.warn, svc.warn_at = body[:140], ts
    return svc


@dataclass
class HealthSetup:
    """What the config asks for, apart from health_cmd (which each server carries). `source` stands in for the
    commands, for --demo and the tests: async report(port, since) and service(unit)."""
    min_free_mb: int = 1024
    service: str = ""
    source: object = None

    @classmethod
    def from_settings(cls, s: dict) -> HealthSetup:
        free = s.get("health_min_free_mb", 1024)
        if isinstance(free, bool) or not isinstance(free, (int, float)) or free <= 0:
            raise ValueError("health_min_free_mb must be a number of megabytes above 0")
        unit = s.get("health_service", "")
        if not isinstance(unit, str) or (unit and not UNIT.fullmatch(unit)):
            raise ValueError("health_service must be a systemd unit name, such as halo-blueflame")
        return cls(int(free), unit)


class HealthError(Exception):
    pass


async def run(cmd: str, ssh: str = "", timeout: float = 30) -> str:
    """What a command prints: on the ssh destination as ping_log runs, or on this machine without one. Whatever it
    prints is the report even if it exited badly (one container gone shouldn't hide the host); nothing printed and
    a bad exit is an error, in words with no address in them."""
    if ssh:
        if not (exe := shutil.which("ssh")):
            raise HealthError("no ssh client on PATH")
        proc = await asyncio.create_subprocess_exec(exe, *SSH_OPTS, ssh, cmd, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    else:
        proc = await asyncio.create_subprocess_shell(cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HealthError(f"no answer in {timeout:g}s") from None
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        raise
    text = out.decode(errors="replace")
    if proc.returncode and not text.strip():
        last = (err.decode(errors="replace").strip().splitlines() or [f"exited {proc.returncode}"])[-1]
        raise HealthError(scrub(last))
    return text
