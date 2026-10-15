"""The ONI terminal: a Textual UI over one RCON connection per server."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
import shutil
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from os.path import commonprefix
from typing import Callable

from rich.console import Group
from rich.json import JSON
from rich.measure import measure_renderables
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.color import Color
from textual.containers import Center, Grid, Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.markup import escape
from textual.screen import ModalScreen, Screen
from textual.suggester import SuggestFromList
from textual.theme import Theme
from textual.widgets import (Button, Checkbox, Collapsible, DataTable, Footer, Input, Label, ListItem, ListView,
                             OptionList, RichLog, Select, Static, TabbedContent, TabPane)
from textual.widgets.option_list import Option
from textual.worker import WorkerState

from .archive import FILES, TOTAL, frame as archive_frame
from .art import (AMBER, CYAN, DIM, GOLD, GREEN, GREY, INK, RED, WHITE, biosig, blend, decrypt, emblem, fit, gauge, hbar,
                  pixels, spark, split_bar)
from . import health as hc
from .config import Server, remember_forge_key
from .forge import (CREDIT, SITE, SORTS, WINDOWED, WINDOWS, WITHDRAWN, ForgeClient, ForgeError, ForgeSetup, Secret,
                    Unverified, author_of, authors_of, before, blurb_of, change_key, change_listing, compat_of,
                    compatible, favourite_listings, history_since, id_list, is_withdrawn, kind_of, latest_of,
                    listing_id, notes_of, owner_of, recent_of, scrub, scrub_data, shorten, tidy, title_of, usable, utc_iso,
                    version_id, version_label, versions_of, views_of, votes_of, when_of)
from .health import BARE, OOM, RESTART, SIGNATURE, HealthSetup, Monitor, Notice, mem_level, swap_level
from .install import SSH_OPTS, InstallError, place, plan, size_words, target_for
from .medals import Medals
from .prefs import Prefs
from .rcon import Rcon, Tunnel
from .state import ForgeState, norm, refs_of
from .stats import Rounds
from .superintendent import BELOW, BIG, HUGE, LARGE, MINI, SMALL, WORDS, XL, Superintendent, below, card as sup_card

ONI = Theme(name="oni", primary=AMBER, secondary=CYAN, accent=CYAN, warning="#E8A33D", error=RED, success=GREEN,
            foreground=WHITE, background=INK, surface="#0B0F14", panel="#111821", dark=True)

TEAMS = ["red", "blue", "green", "orange", "purple", "gold", "brown", "pink"]  # the engine's team order
TEAM_ORDER = {t: i for i, t in enumerate(TEAMS)}
TEAM_COLOR = dict(zip(TEAMS, ["#E5484D", "#4C8DFF", "#4CC27A", "#F08A24", "#A066E0", "#E8C547", "#A0714A", "#F07FB8"]))
BAN_TIMES = [("Permanent", ""), ("30 minutes", "30m"), ("2 hours", "2h"), ("12 hours", "12h"), ("1 day", "1d"),
             ("7 days", "7d"), ("2 weeks", "2w"), ("30 days", "30d")]
MUTE_TIMES = [("Until restart", ""), ("10 minutes", "10m"), ("1 hour", "1h"), ("12 hours", "12h"), ("1 day", "1d")]
COMMANDS = ["status", "players", "say", "tell", "kick", "ban", "banip", "unban", "bans", "vpn", "vpnallow", "vpnrevoke",
            "mute", "unmute", "endround", "endgame", "maps", "modes", "map", "mode", "load", "nextmap", "servername",
            "password", "maxping", "teamcount", "shuffle", "team", "vote", "startvote", "passvote", "cancelvote", "help"]

CATS = {"chat": WHITE, "combat": RED, "traffic": CYAN, "moderation": AMBER, "ops": GREY}
KIND = {"chat": "chat", "kill": "combat", "join": "traffic", "leave": "traffic", "refused": "traffic",
        "kick": "moderation", "ban": "moderation", "unban": "moderation", "mute": "moderation",
        "unmute": "moderation", "cheat": "moderation"}
GLYPH = {"health": "⚠", "chat": "»", "kill": "✕", "join": "▲", "leave": "▼", "refused": "⊘", "kick": "◆", "ban": "■", "unban": "□",
         "mute": "◈", "unmute": "◇", "cheat": "!", "vote": "◉", "control": "•", "uplink": "≡", "forge": "⬢"}
STATE = {"online": ("◉", GREEN), "connecting": ("◌", AMBER), "offline": ("○", RED), "denied": ("⊘", RED)}
SPIN = "◐◓◑◒"
PANE_FOCUS = {"assets": "#players", "intercepts": "#feed", "operations": "#op-load", "blacklist": "#bans",
              "console": "#cmd", "forge": "#listings", "health": "#crashes"}
TABS = ("f1", "f2", "f3", "f4", "f5", "f6", "f7")
HEALTH_EVERY = 60  # seconds between a server's health reports
HEALTH_GATE = 4  # reports being fetched at once
BANNER_FOR = 30  # seconds a crash banner stays up unless it's clicked
LEVEL = {"ok": GREEN, "warn": AMBER, "crit": RED, "": DIM}
CRASH_COLOR = {SIGNATURE: RED, BARE: AMBER, OOM: RED, RESTART: CYAN}
# the catalog's orders, what Forge is featuring, then what's on the selected server, from forge-state.json
VIEWS = [*SORTS, "favourites", "installed"]
VIEW_NAMES = {"installed": "INSTALLED HERE"}
OVERLAP = 300  # seconds the changes feed is read back over each time: a change written late isn't missed
RECONCILE = 6 * 3600  # seconds between fetching every installed listing whole, for whatever the feed missed
KIND_COLOR = {"map": CYAN, "gametype": AMBER, "playlist": GOLD}
ALERT = re.compile(r"\b(admins?|mods?|hack\w*|cheat\w*|aimbot|wallhack)\b", re.I)
ADDRESS_KEYS = {"address", "ip", "ip_address", "addr"}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
REFS = ("killer", "victim", "name", "player", "from", "sender")  # event fields that can name a player by engine ID
SPREE = 5  # kills without dying that make a spree, and earn the marker in the roster
FLEET = 12  # more stations than this: slimmer cards, a summary on the boot screen, paced fan-out
FANOUT = 4  # an @all command or broadcast reaches this many stations at a time
SIGN_INS = 8  # stations signing in at once
SLOW_POLL = 15  # seconds in which every station's status comes round once
QUIET_PHASES = {"", "unknown", "none"}  # a phase that says nothing: left off the card, for the map and mode
SEPARATORS = (" · ", " | ", " - ", " — ", ": ")  # between a community's tag and a server's own name
FREE_TEXT = {"say": 0, "tell": 1, "kick": 1, "servername": 0}  # console commands whose last argument is the rest

OPS = [("load", "LOAD MAP+MODE", "primary"), ("map", "CHANGE MAP", "default"), ("mode", "CHANGE MODE", "default"),
       ("nextmap", "QUEUE NEXT", "default"), ("endround", "END ROUND", "warning"), ("endgame", "END GAME", "error"),
       ("shuffle", "SHUFFLE TEAMS", "default"), ("teamcount", "TEAM COUNT", "default"),
       ("startvote", "CALL VOTE", "default"), ("passvote", "PASS VOTE", "success"),
       ("cancelvote", "CANCEL VOTE", "default"), ("broadcast", "BROADCAST", "primary"),
       ("servername", "RENAME", "default"), ("password", "JOIN PASSWORD", "default"),
       ("maxping", "PING LIMIT", "default"), ("reconnect", "RECONNECT", "default")]
BAN_OPS = [("newban", "NEW BAN", "error"), ("unban", "UNBAN  u", "warning"), ("vpnallow", "VPN ALLOW  a", "default"),
           ("vpnrevoke", "VPN REVOKE  r", "default"), ("vpncheck", "CHECK IP", "default")]
OP_GROUPS = [("MATCH", ["load", "map", "mode", "nextmap", "endround", "endgame"]),
             ("TEAMS & VOTES", ["shuffle", "teamcount", "startvote", "passvote", "cancelvote"]),
             ("SERVER", ["broadcast", "servername", "password", "maxping", "reconnect"])]
PLAYER_OPS = [("tell", "TELL", "t"), ("kick", "KICK", "k"), ("ban", "BAN", "b"), ("mute", "MUTE", "m"),
              ("team", "TEAM", "j"), ("vpnallow", "VPN OK", "v"), ("copy", "COPY ID", "y")]

# What each control does, in plain words: shown when the mouse rests on it.
TIPS = {
    "op-load": "Pick a map and a mode and switch to them now. Ends the current game.",
    "op-map": "Switch to another map, keeping the current mode. Ends the current game.",
    "op-mode": "Switch to another mode, keeping the current map. Ends the current game.",
    "op-nextmap": "Choose what plays after this game, without interrupting it.",
    "op-endround": "End this round as if its time ran out.",
    "op-endgame": "End the whole game now, with the scores as they stand.",
    "op-shuffle": "Mix the players up across the teams.",
    "op-teamcount": "Spread the players over 2 to 8 teams.",
    "op-startvote": "Ask the players to vote: end the round, shuffle, kick someone, and more.",
    "op-passvote": "Pass the vote that's under way, whatever the count.",
    "op-cancelvote": "Call off the vote that's under way.",
    "op-broadcast": "Send a [Server] message to everyone in this or every server.  Ctrl+B",
    "op-servername": "Change the name in the server browser, until the server restarts.",
    "op-password": "Make players type a password to join (or remove it).",
    "op-maxping": "Stop players with a high ping from joining.",
    "op-reconnect": "Drop this connection and sign in again.",
    "bl-newban": "Ban a player ID, an IP address or a whole range.",
    "bl-unban": "Lift the selected ban.  Key: u",
    "bl-vpnallow": "Let a player or address join through a VPN.  Key: a",
    "bl-vpnrevoke": "Take back the selected VPN allowance.  Key: r",
    "bl-vpncheck": "Check whether an address looks like a VPN.",
    "pl-tell": "Send a private message only they can see.  Key: t",
    "pl-kick": "Remove them from the game. They can rejoin after a short wait.  Key: k",
    "pl-ban": "Keep them out, for a while or for good.  Key: b",
    "pl-mute": "Silence their text and voice chat (or lift it).  Key: m",
    "pl-team": "Move them to another team.  Key: j",
    "pl-vpnallow": "Let them join through a VPN.  Key: v",
    "pl-copy": "Copy their player ID, for a ban list or a report.  Key: y",
    "add-server": "Add another server with the setup screen.",
    "f-local": "Show only what happens on the selected server.",
    "fg-install": "Put the selected version on this server, every file checked against Forge's manifest.  Key: i",
    "fg-load": "Load the installed map or gametype now, once the server lists it. Ends the current game.  Key: l",
    "fg-ack": "Say you've seen that it was withdrawn: CONDITION goes back to GREEN. It stays flagged.  Key: a",
    "fg-key": "Load your own ReclaimerForge API key.  Key: k",
    "fg-more": "Fetch the next page of the catalog.  Key: n",
    "super": "The Superintendent watches every server's feed and reacts: it welcomes a join, startles at a call for an "
             "admin, scowls at a cheat flag, approves a kick or ban, is impressed by a medal and unimpressed by a mute.",
    "forge-sort": "How the catalog is ordered, what ReclaimerForge is featuring (favourites), or what's installed "
                  "on this server.  Key: s",
    "forge-window": "The stretch of time trending and rising count over.  Key: w",
    "raw": "Also print every event the servers push, as raw data.",
}
TAB_TIPS = {"assets": "Players on the selected server: click one for their file and actions.",
            "intercepts": "Live chat, kills, joins and moderation from every server, and a box to talk back.",
            "operations": "Change the map or mode, end rounds, run votes, and server settings.",
            "blacklist": "Bans, and players allowed to join through a VPN.",
            "console": "Type server commands directly. For when a button doesn't cover it.",
            "forge": "The ReclaimerForge catalog: community maps, gametypes and playlists for your servers.",
            "health": "Crashes read from each server's log, the host's memory, and whether the workaround service runs."}

# A connection failure in words to act on: (what the error says, card text, the full explanation)
EXPLAIN = [
    (("ssh: connect to host", "could not resolve hostname"), "SSH CAN'T REACH THE BOX",
     "SSH couldn't reach that machine. Check the user@host spelling, and that SSH runs there."),
    (("host key verification",), "SSH DOESN'T KNOW THE BOX",
     "SSH hasn't seen that machine before. Connect once with ssh in a terminal to trust it, then retry."),
    (("permission denied", "publickey"), "SSH REFUSED YOUR KEY",
     "SSH refused your key. Load it into your SSH agent, or name it in ~/.ssh/config."),
    (("no ssh client",), "SSH NOT INSTALLED", "There's no SSH on this computer, so the tunnel can't open."),
    (("connect call failed", "connection refused", "refused the network connection", "errno 111", "10061", "1225"),
     "NOTHING ON THAT PORT",
     "Nothing answered on that port. Is the server running, with an RCON password set in dedicated.toml?"),
    (("name or service not known", "getaddrinfo", "nodename nor servname", "no address associated", "11001"),
     "UNKNOWN HOST", "Can't find that host name. Check the spelling."),
    (("network is unreachable", "no route to host"), "NO ROUTE", "Can't reach that network. Check your connection or VPN."),
    (("timed out", "timeout", "10060", "semaphore"), "NO ANSWER",
     "No answer. Check the address and port, and any firewall in between."),
    (("invalid http", "did not receive a valid http", "rejected websocket", "invalid status", "speak rcon"),
     "NOT AN RCON PORT",
     "Something answered, but it isn't RCON. RCON listens on the game port."),
    (("isn't a valid uri", "invalid uri", "scheme isn't"), "BAD LINK", "That isn't a valid ws:// or wss:// link."),
    (("connection closed", "connection lost", "no close frame"), "CONNECTION DROPPED", "The connection dropped."),
]


def explain(detail: str, state: str = "offline", short: bool = False) -> str:
    """Why a station isn't online, for someone who has never seen a socket error."""
    d = re.sub(r";?\s*retry in \d+s$", "", detail or "").strip()
    low = d.lower()
    if state == "denied":
        if any(k in low for k in ("too many", "locked", "lockout")):
            return "LOCKED OUT" if short else "Locked out after too many wrong passwords. Wait ten minutes, then retry."
        return "PASSWORD REFUSED" if short else ("The server refused the password. Check it against dedicated.toml "
                                                 "[rcon]. Five wrong tries in ten minutes lock you out for a while.")
    for keys, card, text in EXPLAIN:
        if any(k in low for k in keys):
            return card if short else text
    return (d.upper() or "NO CARRIER") if short else d or "Not connected."



# --- reading tolerant: field names come from the docs, so every lookup accepts the plausible spellings ---------
def pick(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k) not in (None, ""):
                return d[k]
    return default


def num(v):
    """v as a number, or None. A count may come as the list of what it counts."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    return len(v) if isinstance(v, (list, tuple)) else None


def team_of(p: dict) -> str:
    t = pick(p, "team", "team_name", "color")
    if isinstance(t, int) and not isinstance(t, bool) and 0 <= t < len(TEAMS):
        return TEAMS[t]
    return str(t).lower() if t is not None else ""


def target_of(p: dict) -> str:
    """Player ID first: numbers shift when someone leaves between a refresh and a keypress."""
    pid = pick(p, "player_id", "id", "xuid")
    if pid:
        return str(pid)
    n = pick(p, "number", "index")
    return f"#{n}" if n is not None else str(pick(p, "name", default="?"))


def who(v, names: dict) -> str:
    if isinstance(v, dict):
        return str(pick(v, "name", default=names.get(pick(v, "engine_id"), "?")))
    if isinstance(v, int) and not isinstance(v, bool):
        return names.get(v, f"#{v}")
    return str(v) if v not in (None, "") else "?"


ID_KEY = re.compile(r"(^|_)(id|xuid|uid|guid|uuid)$", re.I)  # roster fields that carry an ID: engine_id, player_id...
ID_LIKE = re.compile(r"\d{6,}|[0-9a-f]{16,}", re.I)  # what a player ID looks like, as against a callsign
SLOT = 1 << 64


def id_forms(v) -> set[str]:
    """Every spelling an ID can take: as written, the same number in decimal and in hex, and for a long hex ID either
    64-bit half of it. A kill names its players by one of a roster entry's IDs, in whichever base the server picks."""
    if isinstance(v, bool) or not isinstance(v, (int, str)) or v == "":
        return set()
    s = str(v).strip().lower()
    out = {s}
    if re.fullmatch(r"-?\d+", s):
        n = int(s) % SLOT
        out |= {str(n), f"{n:x}"}
    elif re.fullmatch(r"[0-9a-f]{5,}", s):
        out.add(str(int(s, 16)))
        if len(s) > 16:
            for half in (s[:16], s[-16:]):
                out |= {half, str(int(half, 16))}
    return out


def player_ids(p: dict) -> set[str]:
    """Every ID a roster entry carries, in every spelling id_forms knows."""
    out: set[str] = set()
    for k, v in p.items():
        if ID_KEY.search(k):
            out |= id_forms(v)
    return out


def resolve(ev: dict, names: dict, ident: Callable | None = None) -> dict:
    """A copy of the event that names its players outright. An engine ID is a slot the engine hands to the next
    player to join, so it has to be read against the roster when the event arrives, not when the feed redraws.
    `ident` names a killer or victim the server gave by another ID than the engine's."""
    out = {}
    for k, v in ev.items():
        if k in ("killer", "victim") and ident and (n := ident(v)) is not None:
            out[k] = n
        elif k in REFS and isinstance(v, (int, dict)) and not isinstance(v, bool):
            out[k] = who(v, names)
        else:
            out[k] = v
    return out


def tally(players: list) -> list[tuple[str, int, int]]:
    """(team, players, total score) per team, in the engine's team order; empty when nobody is on a team."""
    teams: dict[str, list] = {}
    for p in players:
        if team := team_of(p):
            entry = teams.setdefault(team, [0, 0])
            entry[0] += 1
            entry[1] += num(pick(p, "score")) or 0
    return [(t, n, s) for t, (n, s) in sorted(teams.items(), key=lambda kv: TEAM_ORDER.get(kv[0], len(TEAMS)))]


def sides(players: list) -> list[tuple[str, int, int]]:
    """The teams, or none in a free-for-all, which can put every player on a team of their own."""
    teams = tally(players)
    return [] if len(teams) > 2 and all(n == 1 for _, n, _ in teams) else teams


def bar(v, width: int = 5) -> Text:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return Text("—", DIM)
    return gauge(v, width, GREEN if v > .6 else AMBER if v > .3 else RED)


# The server's RCON has no ping. It logs one line per player as they connect, and `maxping` judges that number.
JOIN_PING = re.compile(r"player ID ([0-9a-f]+), ping (\d+) ms")


def ping_cell(ms) -> Text:
    return Text("—", DIM) if ms is None else Text(str(ms), GREEN if ms < 100 else AMBER if ms < 200 else RED)


def team_strip(teams: list, width: int = 24) -> Text | None:
    """The score race between the teams as one bar, for under the roster. None without at least two teams."""
    if len(teams) < 2:
        return None
    parts = [(max(score, 0), TEAM_COLOR.get(team, WHITE)) for team, _, score in teams]
    if len(teams) == 2:
        (a, _, sa), (b, _, sb) = teams
        return Text.assemble((f" {a.upper()} {sa} ", parts[0][1]), split_bar(parts, width),
                             (f" {sb} {b.upper()} ", parts[1][1]))
    names = Text("  ").join(Text(f"{t.upper()} {s}", c) for (t, _, s), (_, c) in zip(teams, parts))
    return Text(" ") + names + Text(" ") + split_bar(parts, width) + Text(" ")


def until(ts) -> str:
    if not isinstance(ts, (int, float)):
        return "PERMANENT"
    left = int(ts - time.time())
    if left <= 0:
        return "expired"
    d, h, m = left // 86400, left % 86400 // 3600, left % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def clock(ts) -> str:
    """HH:MM:SS for an event's time, in seconds or milliseconds; now when it has none that reads."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        with contextlib.suppress(OverflowError, ValueError, OSError):
            return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts).strftime("%H:%M:%S")
    return time.strftime("%H:%M:%S")


def when(ts) -> str:
    """The time of day of an epoch time, with the date when it wasn't today."""
    t = datetime.fromtimestamp(ts)
    return t.strftime("%H:%M:%S" if t.date() == datetime.now().date() else "%m-%d %H:%M:%S")


def age(seconds: float) -> str:
    s = max(0, int(seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s" if m else f"{s}s"


def redact_addr(v, on: bool) -> str:
    return "███.███.███.███" if on and v else str(v or "—")


def redact_text(s: str, on: bool) -> str:
    """s with any IPv4 address in it blanked, while addresses are redacted. A Forge key is blanked whatever."""
    s = scrub(s)
    return IPV4.sub("███.███.███.███", s) if on else s


def redact_data(v, on: bool):
    """A reply or event with every address in it blanked, while addresses are redacted: for showing, not sending.
    A Forge key is blanked whatever."""
    if not on:
        return scrub_data(v)
    if isinstance(v, dict):
        return {k: redact_addr(x, on) if k in ADDRESS_KEYS and isinstance(x, str) else redact_data(x, on)
                for k, x in v.items()}
    if isinstance(v, list):
        return [redact_data(x, on) for x in v]
    return redact_text(v, on) if isinstance(v, str) else v


def clip_id(v) -> str:
    """A long ID (32 hex digits) cut to its first 8, for the feed; the dossier and console have it whole."""
    s = str(v)
    return s[:8] + "…" if len(s) > 16 and re.fullmatch(r"[0-9a-fA-F-]+", s) else s


def q(a: str) -> str:
    return f'"{a}"' if not a or " " in a else a


def parse_command(line: str) -> list[str]:
    """A console line as the command and its arguments. Quotes group words as in a shell, except in the message a
    say, tell, kick or rename carries: that's the rest of the line as typed, so an apostrophe in it is just one, and
    the server gets it as one argument rather than word by word."""
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split, lex.commenters = True, ""
    words = [lex.get_token()]
    lead = FREE_TEXT.get((words[0] or "").lower())
    if lead is None:
        return [w for w in [*words, *lex] if w is not None]
    while len(words) <= lead and (w := lex.get_token()) is not None:
        words.append(w)
    rest = lex.instream.read().strip()
    if len(rest) > 1 and rest[0] == rest[-1] and rest[0] in "'\"" and rest[0] not in rest[1:-1]:
        rest = rest[1:-1]  # quoted whole, as a shell would want it
    return [w for w in words if w is not None] + ([rest] if rest else [])


def split_tag(name: str) -> tuple[str, str]:
    """("ALPHA", "Big Team Rockets") for "ALPHA · Big Team Rockets"; ("", name) when it carries no tag."""
    at = [(i, sep) for sep in SEPARATORS if 0 < (i := name.find(sep)) <= 24]
    if not at:
        return "", name
    i, sep = min(at)
    own = name[i + len(sep):].strip()
    return (name[:i].strip(), own) if own else ("", name)


def describe(ev: dict, names: dict, redact: bool = True) -> Text:
    """What an event says, after its time and station. Known kinds get a layout; anything else prints its fields."""
    kind = str(ev.get("event", "?"))
    cat = KIND.get(kind, "ops")
    line = Text()
    if kind == "chat":
        ch = str(ev.get("channel", "all"))
        if ch == "server":
            line.append("[SERVER] ", AMBER)
            line.append(redact_text(str(ev.get("text", "")), redact), AMBER)
        else:
            team = str(pick(ev, "team", default=ch.removeprefix("team").strip())).lower()
            line.append(f"[{ch.upper()}] ", DIM)
            line.append(who(pick(ev, "name", "player", "from", "sender"), names), TEAM_COLOR.get(team, WHITE))
            line.append(": " + redact_text(str(ev.get("text", "")), redact))
    elif kind == "kill":
        killer, victim = pick(ev, "killer"), pick(ev, "victim")
        k, v = who(killer, names), who(victim, names)
        if killer is None or k == v:
            line.append(v, WHITE)
            line.append(" died", DIM)
        else:
            line.append(k, WHITE)
            line.append(" ✕ ", RED)
            line.append(v)
        how = pick(ev, "weapon", "damage", "cause", "how")
        if how:
            line.append(f"  [{how}]", DIM)
        for medal in ev.get("_medals") or ():  # padded with blank braille, not spaces: wrap between pills only
            line.append("  ")
            line.append(f"\u2800{medal}\u2800".replace(" ", "\u2800"), f"{INK} on {GOLD}")
    elif kind == "health":
        line.append("HEALTH  " + str(ev.get("text", "")), RED if ev.get("level") == "crit" else AMBER)
    else:
        line.append(kind.upper(), CATS[cat])
        rest = {k: v for k, v in ev.items() if k not in ("type", "event", "time")}
        name = rest.pop("name", None) or rest.pop("player", None)
        if name is not None:
            line.append(f"  {who(name, names)}", WHITE)
        text = rest.pop("text", None)
        if text:
            line.append(f"  {redact_text(str(text), redact)}")
        for k, v in rest.items():
            v = (redact_addr(v, redact) if k in ADDRESS_KEYS else json.dumps(redact_data(v, redact))
                 if isinstance(v, (dict, list)) else redact_text(clip_id(v), redact))
            line.append(f"  {k}=", DIM)
            line.append(str(v))
    return line


def reaction_words(ev: dict, names: dict, redact: bool = True) -> str:
    """A few plain words for what the Superintendent reacted to, under its face. Addresses are blanked while they're
    redacted, and anything a stranger typed has its control characters dropped."""
    kind, name = str(ev.get("event", "?")), who(pick(ev, "name", "player", "from", "sender"), names)
    if kind == "kill" and ev.get("_medals"):
        return tidy(f"{ev['_medals'][-1]}: {who(pick(ev, 'killer'), names)}")
    if kind == "join":
        return tidy(f"{name} joined")
    if kind in ("kick", "ban", "mute"):
        reason = pick(ev, "reason", "text")
        return tidy(f"{kind} {name}" + (f": {redact_text(str(reason), redact)}" if reason else ""))
    if kind == "chat" and ev.get("channel") != "server":
        return tidy(f"{name}: {redact_text(str(ev.get('text', '')), redact)}")
    return tidy(describe(ev, names, redact).plain)


def render_event(label: str, ev: dict, names: dict, redact: bool = True, width: int = 14) -> Text:
    """One feed line for a pushed event: time, station, glyph, then what it says."""
    kind = str(ev.get("event", "?"))
    line = Text(clock(ev.get("time")), DIM)
    line.append(f" {label[:width]:<{width}} ", CYAN)
    line.append(f"{GLYPH.get(kind, '·')} ", CATS[KIND.get(kind, "ops")])
    return line + describe(ev, names, redact)


def vote_text(v) -> Text:
    """The vote under way: what it's on, and the tally as a tug of war when the server gives one."""
    if not v:
        return Text("none under way", DIM)
    if not isinstance(v, dict):
        return Text(json.dumps(v))
    t = Text(str(pick(v, "subject", "type", "kind", default="vote")).upper(), AMBER)
    if target := pick(v, "target", "player", "name"):
        t.append(f" {target}")
    yes, no = num(pick(v, "yes", "for")), num(pick(v, "no", "against"))
    if yes is not None or no is not None:
        yes, no = yes or 0, no or 0
        t += Text.assemble(("\nYES ", DIM), (f"{yes} ", GREEN), split_bar([(yes, GREEN), (no, RED)], 6),
                           (f" {no}", RED), (" NO", DIM))
    return t


@dataclass(eq=False)  # told apart by identity: comparing by value would compare everything they hold
class Station:
    server: Server
    label: str
    rcon: Rcon | None = None
    worker: object = None
    prev: str = ""
    data: dict = field(default_factory=dict)  # last good `data` per read command (status, players, bans, ...)
    inflight: set = field(default_factory=set)
    medals: Medals = field(default_factory=Medals)
    activity: deque = field(default_factory=lambda: deque(maxlen=1000))  # when recent events came in (monotonic)
    alerts: int = 0  # calls for an admin and anti-cheat hits not yet looked at
    flash: float = 0.0  # the card pulses until this monotonic time
    retry_at: float = 0.0  # when the next reconnect attempt goes out
    first_seen: dict = field(default_factory=dict)  # player key -> when their row first appeared
    painted: bool = False  # players drawn once: anyone new after this gets a NEW flag
    index: int = 0  # its place in the sidebar
    tag: str = ""  # the community tag its reported name starts with, when the label leaves it off
    drawn: tuple = ()  # what its card shows now: repainted only when this changes
    polled: float = 0.0  # when its players were last fetched by the slow poll
    pings: dict = field(default_factory=dict)  # player ID -> the ping they joined with, from the server's log
    here: dict = field(default_factory=dict)  # every spelling of an ID on the roster now -> callsign
    past: dict = field(default_factory=dict)  # the long ones from earlier rosters and joins: a kill by someone who just left
    aliases: dict = field(default_factory=dict)  # an ID nobody here answers to -> PLAYER n, so it's never shown raw
    unmatched: bool = False  # said once that kills name players by an ID the roster doesn't carry
    check_at: float = 0.0  # when its next health report is due (monotonic)
    checking: bool = False
    health_ok: float = 0.0  # when the last report came in (epoch), 0 before the first
    health_err: str = ""  # why the last report didn't, scrubbed; empty while they come

    @property
    def online(self) -> bool:
        return bool(self.rcon and self.rcon.state == "online")

    @property
    def players(self) -> list:
        return self.data.get("players", {}).get("players") or []

    @property
    def names(self) -> dict:
        return {p.get("engine_id"): str(pick(p, "name", default="?")) for p in self.players if p.get("engine_id") is not None}

    def learn(self, players: list) -> None:
        """Read the roster for the IDs a kill might name its players by. Short ones (engine slots) are only good
        while the player holds the slot; long ones are kept, so a kill by someone who just left still reads."""
        self.here = {f: str(pick(p, "name", default="?")) for p in players for f in player_ids(p)}
        self.past.update({f: n for f, n in self.here.items() if len(f) >= 8})
        while len(self.past) > 4000:  # ponytail: oldest out
            del self.past[next(iter(self.past))]

    def ident(self, v) -> str | None:
        """The callsign a killer or victim ID belongs to. An ID that belongs to nobody here becomes PLAYER n and
        never shows raw. None for what who() reads itself: a name, a slot number, a dict."""
        forms = id_forms(v)
        for f in forms:
            if (n := self.here.get(f) or self.past.get(f)) is not None:
                return n
        if isinstance(v, str) and ID_LIKE.fullmatch(v.strip()) or isinstance(v, int) and len(str(abs(v))) >= 6:
            return self.aliases.setdefault(min(forms), f"PLAYER {len(self.aliases) + 1}")
        return None

    def rate(self, buckets: int, span: float, whole: bool = False) -> list[int]:
        """Events in each of the last `buckets` stretches of `span` seconds, oldest first. `whole` leaves out the
        stretch still under way, so the counts change once a stretch rather than with every event."""
        now, out = time.monotonic(), [0] * buckets
        if whole:
            now -= now % span
        while self.activity and now - self.activity[0] > 300:  # older than any card's trace
            self.activity.popleft()
        for t in self.activity:
            if 0 <= (i := buckets - 1 - int((now - t) // span)) < buckets:
                out[i] += 1
        return out


# --- dialogs: titles and bodies are Text, because they carry player names and markup in a name must stay a name ---
class Dialog(ModalScreen):
    """A modal that dims the screen behind it as its box rises into place (the -enter transitions in oni.tcss)."""
    DEFAULT_CLASSES = "-enter"

    @staticmethod
    def keys(*hints: str) -> Static:
        return Static(Text("   ·   ".join(hints), DIM), classes="dialog-keys")

    def on_mount(self) -> None:
        self.call_after_refresh(self.remove_class, "-enter")


class Confirm(Dialog):
    BINDINGS = [Binding("escape", "dismiss(False)", show=False)]

    def __init__(self, title: str, body: str, verb: str = "EXECUTE", danger: bool = True):
        super().__init__()
        self.t, self.b, self.verb, self.danger = title, body, verb, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog danger" if self.danger else "dialog"):
            yield Static(Text(self.t), classes="dialog-title")
            yield Static(Text(self.b), classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button("ABORT", id="no")
                yield Button(self.verb, id="yes", variant="error" if self.danger else "primary")
            yield self.keys("Esc cancel", "← → choose", "Enter confirm")

    def on_mount(self) -> None:
        self.query_one("#no").focus()  # the safe choice is the default

    def on_button_pressed(self, e: Button.Pressed) -> None:
        e.stop()
        self.dismiss(e.button.id == "yes")


class Form(Dialog):
    """fields: (key, label, default) where a list default is a Select of (label, value) and anything else an Input.
    Keys in `secret` are typed masked and kept exactly as typed."""
    BINDINGS = [Binding("escape", "dismiss(None)", show=False)]

    def __init__(self, title: str, fields: list, verb: str = "OK", danger: bool = False, note: str = "",
                 required: tuple = (), secret: tuple = ()):
        super().__init__()
        self.t, self.fields, self.verb, self.danger, self.note, self.required = title, fields, verb, danger, note, required
        self.secret = secret

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog danger" if self.danger else "dialog"):
            yield Static(Text(self.t), classes="dialog-title")
            if self.note:
                yield Static(Text(self.note), classes="dialog-body")
            for key, label, default in self.fields:
                yield Label(label + ("  (required)" if key in self.required else ""))
                if isinstance(default, list):
                    yield Select(default, allow_blank=False, id=f"field-{key}")
                else:
                    yield Input(str(default), id=f"field-{key}", password=key in self.secret)
            with Horizontal(classes="dialog-buttons"):
                yield Button("ABORT", id="no")
                yield Button(self.verb, id="yes", variant="error" if self.danger else "primary")
            yield self.keys("Esc cancel", "Tab next field", "Enter " + self.verb.lower())

    def on_mount(self) -> None:
        self.query("Input, Select").first().focus()

    @on(Input.Submitted)
    def _enter(self, e: Input.Submitted) -> None:
        e.stop()
        self.submit()

    def on_button_pressed(self, e: Button.Pressed) -> None:
        e.stop()
        self.submit() if e.button.id == "yes" else self.dismiss(None)

    def submit(self) -> None:
        vals = {}
        for key, *_ in self.fields:
            v = self.query_one(f"#field-{key}").value
            vals[key] = "" if not isinstance(v, str) else v if key in self.secret else v.strip()
        missing = [k for k in self.required if not vals[k]]
        if missing:
            box = self.query_one(f"#field-{missing[0]}")
            box.focus()
            box.add_class("-missing")  # a red flash on the empty box says which one
            self.set_timer(0.6, lambda: box.remove_class("-missing"))
        else:
            self.dismiss(vals)


class Pick(Dialog):
    """A filterable list; returns the chosen value, or None when aborted."""
    BINDINGS = [Binding("escape", "dismiss(None)", show=False), Binding("down", "focus_list", show=False)]

    def __init__(self, title: str, options: list):
        super().__init__()
        self.t, self.opts, self.shown = title, options, options

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog pick"):
            yield Static(Text(self.t), classes="dialog-title")
            if len(self.opts) > 8:
                yield Input(placeholder="type to filter…", id="pick-filter")
            yield OptionList(id="pick-list")
            yield self.keys("Esc cancel", "↑ ↓ move", "Enter or click to choose")

    def on_mount(self) -> None:
        self.fill("")
        self.query("#pick-filter, #pick-list").first().focus()

    def fill(self, text: str) -> None:
        text = text.lower()
        self.shown = [(l, v) for l, v in self.opts
                      if text in (l.plain if isinstance(l, Text) else str(l)).lower() or text in str(v).lower()]
        ol = self.query_one(OptionList)
        ol.clear_options()
        ol.add_options([Option(l if isinstance(l, Text) else Text(str(l)), id=str(i)) for i, (l, _) in enumerate(self.shown)])
        if self.shown:
            ol.highlighted = 0

    def action_focus_list(self) -> None:
        self.query_one(OptionList).focus()

    @on(Input.Changed, "#pick-filter")
    def _filter(self, e: Input.Changed) -> None:
        e.stop()
        self.fill(e.value)

    @on(Input.Submitted, "#pick-filter")
    def _first(self, e: Input.Submitted) -> None:
        e.stop()
        if self.shown:
            self.dismiss(self.shown[self.query_one(OptionList).highlighted or 0][1])

    @on(OptionList.OptionSelected)
    def _chosen(self, e: OptionList.OptionSelected) -> None:
        e.stop()
        self.dismiss(self.shown[int(e.option.id)][1])


GUIDE = [
    ("GETTING AROUND", [
        ("Servers", "Your servers are on the left. Click one, or press 1 to 9. A green ◉ is connected; a red ○ "
                    "says why it isn't, and retries by itself."),
        ("Go to", "g lists every server, the busiest first: type part of a name and press Enter."),
        ("Tabs", "F1 to F7, or click the names along the top."),
        ("Anything", "Ctrl+P opens a searchable list of every action. Rest the mouse on a button to see what it does."),
        ("Superintendent", "The small green face on the left, on every tab, watches every server's feed. It welcomes a "
                           "join, startles at a call for an admin, scowls at a cheat flag, approves a kick or ban, is "
                           "impressed by a medal and unimpressed by a mute; the word under it says how it feels."),
    ]),
    ("F1  ASSETS  ·  the players", [
        ("Pick a player", "Click a row, or move with ↑ ↓. Their file opens on the right, with buttons to tell, kick, "
                          "ban, mute, move or allow them through a VPN."),
        ("Ping", "PING is what a player joined with, read from the server's log (needs ping_log in the config; "
                  "RCON has no live ping). Over 200 is red."),
        ("Keys", "t tell  ·  k kick  ·  b ban  ·  m mute  ·  j team  ·  v VPN allow  ·  y copy their ID"),
        ("Flags", "ADM an admin  ·  MUT muted  ·  KIA dead right now  ·  NEW just joined  ·  ★5 on a killing spree"),
        ("Their file", "The glyph is drawn from their player ID: the same player always gets the same one. Medals and "
                       "their streak count what this console has seen since it connected."),
    ]),
    ("F2  INTERCEPTS  ·  what's happening", [
        ("The feed", "Chat, kills, joins, kicks and bans from every server, newest at the bottom. Scroll up to read "
                     "back; it holds still until you press End."),
        ("Talk back", "Type in the box at the bottom to chat as [Server]. Start with @all to reach every server: they "
                      "go out a few at a time, and the command log tallies how it went."),
        ("Medals", "Kills carry their Halo 3 medals as they happen: double kill and up, sprees, killjoys."),
        ("Alerts", "Chat asking for an admin, or naming a cheat, pops up, beeps and turns the CONDITION at the top red. "
                   "Servers you aren't looking at show a ⚑ count until you open them or this feed."),
    ]),
    ("F3  OPERATIONS  ·  running the match", [
        ("Buttons", "Change map or mode, queue what plays next, end the round, shuffle teams, run votes, broadcast, "
                    "and server settings. Anything that ends a game asks first."),
        ("Credit", "When the map or gametype playing came from ReclaimerForge, the sitrep's FORGE line names it and "
                   "whoever made it."),
    ]),
    ("F4  BLACKLIST  ·  bans", [
        ("Lists", "Every ban by player, address and device, and who may join through a VPN. Pick a row, then UNBAN "
                  "or REVOKE."),
    ]),
    ("F5  CONSOLE  ·  typing commands", [
        ("Commands", "For anything without a button. Type help for the server's list; ↑ ↓ bring back earlier ones."),
    ]),
    ("F6  FORGE  ·  community content", [
        ("The catalog", "Maps, gametypes and playlists from ReclaimerForge. s changes the order, w the time window "
                        "trending and rising count over, and / searches. Pick one for its file and its versions; n "
                        "fetches more. RATING is thumbs up and down. FAVOURITES is what ReclaimerForge is featuring "
                        "now, for as long as each collection runs."),
        ("Install", "i puts the selected version on this server. Every file is checked against Forge's manifest "
                    "before it's copied into the server's content_dir and again once it's there, and replacing "
                    "anything asks first. ◉ marks what's installed here; ▲ means a newer version is out."),
        ("Load now", "l loads an installed map or gametype, once the server lists it. Whether a server picks up new "
                     "content without a restart is up to the server, so installing never loads anything by itself."),
        ("Updates", "Every few minutes oni-rcon asks Forge what changed among what you've installed. A new version "
                    "gets a toast and ▲. A withdrawn listing, or the very version you installed, is flagged ⚠ in the "
                    "F3 rotation and on F6, and turns the CONDITION amber until you acknowledge it: s to INSTALLED "
                    "HERE, pick it, then a. Installing a newer version clears a withdrawn version's flag. A withdrawn "
                    "version is never picked to install."),
        ("Your key", "Forge takes your own API key: k loads one. It's only ever sent to reclaimerforge.net, and it's "
                     "never shown on screen."),
        ("Thanks", CREDIT),
    ]),
    ("F7  HEALTH  ·  crashes and memory", [
        ("Crashes", "Each server's last crashes, read from its log: SIGNATURE is the known crash (an exception with its "
                    "code and RVA, the blue developer helmet under Wine), BARE is the engine probe failing with no "
                    "exception, which is a different fault, OOM-KILL is a container killed for memory, and "
                    "CONTAINER-RESTART is one that started again. PLAYERS is how many were on. DROPPED means this "
                    "console's RCON connection fell at the same moment."),
        ("Host", "Free memory (amber under twice health_min_free_mb, red under it), swap, the kernel's oom_kill "
                 "counter, and each container's start time and OOMKilled flag."),
        ("Workaround", "Whether the named systemd service is running, when it last moved a tag, and any warning that "
                       "the build isn't the pinned one. Hidden without health_service."),
        ("Alerts", "A new crash, or memory falling under the limit, flashes a banner over every tab (click it away), "
                   "goes in the F2 feed and beeps. Crashes from before you opened this console aren't alerts."),
        ("Turning it on", "Set health_cmd in the config: this tab says how when it's off. Ctrl+R reads everything now."),
    ]),
    ("SAFETY", [
        ("Addresses", "Player IPs are hidden. Press x to show them, and again to hide them before you stream."),
        ("Confirming", "Risky actions ask first, and the safe choice (ABORT) is already selected."),
        ("Passwords", "A refused password is never retried by itself: five wrong tries lock you out. RECONNECT on F3 "
                      "asks for it again."),
    ]),
]


class Help(Dialog):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss", show=False)]

    def compose(self) -> ComposeResult:
        crest = Table.grid(padding=(0, 3))
        crest.add_column()
        crest.add_column(vertical="middle")
        crest.add_row(emblem(8, 16), Text.assemble(("OFFICE OF NAVAL INTELLIGENCE\n", AMBER),
                                                   ("SECTION THREE  ·  REMOTE CONSOLE TERMINAL\n\n", DIM),
                                                   ("Every key, button and readout, in plain words.", WHITE)))
        parts = [crest, Text()]
        for section, rows in GUIDE:
            g = Table.grid(padding=(0, 2))
            g.add_column(style=CYAN, no_wrap=True, width=14)
            g.add_column()
            for k, v in rows:
                g.add_row(k, Text(v, WHITE))
            parts += [Text(section, AMBER), g, Text()]
        with Vertical(classes="dialog help"):
            yield Static("FIELD MANUAL", classes="dialog-title")
            with VerticalScroll(id="help-body"):
                yield Static(Group(*parts))
            yield self.keys("Esc or ? close", "↑ ↓ scroll", "Ctrl+P every action")

    def on_mount(self) -> None:
        self.query_one("#help-body").focus()


class Archive(Screen):
    """The boot sequence's first act, the Halo ASCII Archive from the design project: the ONI emblem, Section Three,
    Installation 04, 343 Guilty Spark and the Superintendent decrypt one file at a time (archive.frame draws each
    moment). Then Boot plays the last act, the emblem returning for the clearance check. Any key skips both, and F1
    to F6 go straight to their tab. Only with full animations: with them off there's nothing to watch."""

    def compose(self) -> ComposeResult:
        with Vertical(id="boot"):
            with Center():
                yield Static(id="boot-emblem")
            yield Static(id="boot-title")
            with Center():
                yield Static(id="boot-log")

    def on_mount(self) -> None:
        self.t0 = time.monotonic()
        self.query_one("#boot-log").styles.height = len(FILES) + 3  # the files, the prompt, a gap and the gauge
        self.query_one("#boot-emblem").styles.content_align_vertical = "middle"
        self.set_interval(0.07, self.tick)
        self.tick()

    def on_key(self, e: events.Key) -> None:
        if e.key not in TABS:
            e.stop()
        self.action_skip()

    def tick(self) -> None:
        el = time.monotonic() - self.t0
        if el >= TOTAL:
            self.app.switch_screen(Boot())
            return
        f = archive_frame(el)
        rows, cols = min(24, self.app.size.height - 9 - len(FILES) - 1), self.app.size.width - 4
        self.query_one("#boot-emblem").styles.height = rows
        art = (emblem(rows, cols, reveal=f.oni[0], scan=f.oni[1]) if f.oni else
               pixels(fit(f.art, rows, cols)) if f.art else Text())
        self.query_one("#boot-emblem", Static).update(art)
        self.query_one("#boot-title", Static).update(Text.assemble(
            ("\n" + f.title + "\n", f.color), (f.sub, DIM), (f.tag, f.tag_color)))
        out = Text()
        for label, done in f.files:
            out += Boot.dotted(label, "DECRYPTED" if done else "DECRYPTING", GREEN if done else AMBER)
        out += Text("\n" * (len(FILES) - len(f.files))) + Text("> ", WHITE) + Text(
            "█" if int(el * 3) % 2 == 0 else " ", AMBER) + Text("\n\n")
        out += gauge(f.progress, 56, AMBER) + Text(f" {round(f.progress * 100):>3}%", DIM)
        self.query_one("#boot-log", Static).update(out)

    def action_skip(self) -> None:
        if self.app.screen is self:
            self.app.pop_screen()
            self.app.welcome()


class Boot(Screen):
    """The splash: decrypts the title and materialises the emblem while the stations sign in. Any key skips it,
    and F1 to F6 go straight to their tab. With animations off (TEXTUAL_ANIMATIONS) it all appears at once."""
    HEADING = "O F F I C E   O F   N A V A L   I N T E L L I G E N C E"
    SHOW_DOWN = 5  # a fleet's stations that can't connect, listed by name; any more are counted

    def compose(self) -> ComposeResult:
        with Vertical(id="boot"):
            with Center():
                yield Static(id="boot-emblem")
            yield Static(id="boot-title")
            with Center():  # align centres children as one block, and the emblem is full width
                yield Static(id="boot-log")

    def on_mount(self) -> None:
        self.t0, self.done, self.drawn = time.monotonic(), None, None
        # fixed, so the emblem doesn't shift as lines come in: one per tunnel and station (a fleet gets a tally and
        # the stations that can't connect), the rest, the progress bar
        self.lines = (len(self.app.tunnels) + (2 + self.SHOW_DOWN if self.app.fleet else len(self.app.stations)) + 5
                      + bool(self.app.fsetup.key))
        self.query_one("#boot-log").styles.height = self.lines
        self.set_interval(0.07, self.tick)
        self.tick()

    def on_key(self, e: events.Key) -> None:
        if e.key not in TABS:
            e.stop()  # skipping shouldn't also redact, broadcast or switch station unseen
        self.action_skip()

    @staticmethod
    def dotted(k: str, v: str, color: str) -> Text:
        dots = max(3, min(46 - len(k), 59 - len(k) - len(v)))  # a long value takes from the dots, not the next line
        return Text(f"> {k} ", WHITE) + Text("." * dots, DIM) + Text(f" {v}\n", color)

    def tick(self) -> None:
        app, el = self.app, time.monotonic() - self.t0
        t = el if app.animation_level == "full" else 9.0  # past every effect: drawn whole at once
        # leave room for the title, the log (a line per tunnel and station) and the progress bar
        rows = app.size.height - 9 - self.lines
        if t < 1.9 or self.drawn != rows:  # materialise, sweep once, then leave it be
            self.query_one("#boot-emblem", Static).update(
                emblem(rows, reveal=t / 0.9, scan=(t - 0.8) / 0.9 if 0.8 < t < 1.7 else None))
            self.drawn = rows
        self.query_one("#boot-title", Static).update(Text.assemble(
            ("\n" + decrypt(self.HEADING, t / 0.8, int(t / 0.07)) + "\n", AMBER),
            ("SECTION THREE  ·  REMOTE CONSOLE TERMINAL  ·  ", DIM), ("TOP SECRET", RED)))

        spin = SPIN[int(el * 8) % 4]
        log = [("AUTHENTICATING OPERATOR", app.by.upper(), AMBER, True),
               ("OPENING SECURE CHANNELS", f"{len(app.stations)} STATION{'S' * (len(app.stations) != 1)}", AMBER, True)]
        for tun in app.tunnels.values():
            v, c = ((f"{spin} OPENING", AMBER) if tun.state == "opening" else ("ESTABLISHED", GREEN)
                    if tun.state == "up" else (explain(tun.detail, short=True), RED))
            log.append((f"SSH TUNNEL {tun.dest.upper()[:34]}", v, c, tun.state != "opening"))
        links = [(f"[{i + 1}] {st.label.upper()[:34]}", *app.link(st, spin)) for i, st in enumerate(app.stations)]
        if app.fleet:  # a line each would scroll off the screen and take a quarter of a second apiece
            up, settled = sum(st.online for st in app.stations), all(ok for *_, ok in links)
            log.append((f"STATIONS 1-{len(links)}", f"{'' if settled else spin + ' '}{up}/{len(links)} SECURE",
                        GREEN if up == len(links) else AMBER if not settled or up else RED, settled))
            down = [line for line in links if line[3] and line[2] == RED]
            log += down[:self.SHOW_DOWN]
            if len(down) > self.SHOW_DOWN:
                log.append((f"AND {len(down) - self.SHOW_DOWN} MORE", "SEE THE SIDEBAR", RED, True))
        else:
            log += links
        if app.fsetup.key:  # the catalog the community built: named on the way in
            log.append(("FORGE CATALOG", SITE, GREEN, True))
        steps = int(el / 0.22)
        out = Text()
        for k, v, c, _ in log[:steps]:
            out += self.dotted(k, v, c)
        settled = all(ok for *_, ok in log)
        finished = steps > len(log) and settled or el > 15
        if finished:
            ok = any(st.online for st in app.stations)
            out += self.dotted("CLEARANCE", *(("GRANTED", GREEN) if ok else ("PENDING", AMBER) if not settled
                                              else ("NO STATION REACHABLE", RED)))
            self.done = self.done or time.monotonic()
            if time.monotonic() - self.done > 1.1:
                self.action_skip()
        else:  # the prompt blinks while it works
            out += Text("> ", WHITE) + Text("█" if int(el * 3) % 2 == 0 else " ", AMBER) + Text("\n")
        progress = (sum(ok for *_, ok in log[:steps]) + finished) / (len(log) + 1)
        out += Text("\n") + gauge(progress, 56, AMBER) + Text(f" {round(progress * 100):>3}%", DIM)
        self.query_one("#boot-log", Static).update(out)

    def action_skip(self) -> None:
        if self.app.screen is self:
            self.app.pop_screen()
            self.app.welcome()


class Assets(Horizontal):
    BINDINGS = [Binding("t", "app.player('tell')", "Tell"), Binding("k", "app.player('kick')", "Kick"),
                Binding("b", "app.player('ban')", "Ban"), Binding("m", "app.player('mute')", "Mute"),
                Binding("j", "app.player('team')", "Team"), Binding("v", "app.player('vpnallow')", "VPN allow"),
                Binding("y", "app.player('copy')", "Copy ID")]


class Blacklist(Vertical):
    BINDINGS = [Binding("n", "app.bl('newban')", "New ban"), Binding("u", "app.bl('unban')", "Unban"),
                Binding("a", "app.bl('vpnallow')", "VPN allow"), Binding("r", "app.bl('vpnrevoke')", "VPN revoke")]


class ForgePane(Vertical):
    BINDINGS = [Binding("i", "app.forge('install')", "Install"), Binding("l", "app.forge('load')", "Load now"),
                Binding("a", "app.forge('ack')", "Acknowledge"),
                Binding("s", "app.forge('sort')", "Sort"), Binding("w", "app.forge('window')", "Window"),
                Binding("n", "app.forge('more')", "More"), Binding("y", "app.forge('copy')", "Copy ID"),
                Binding("k", "app.forge('key')", "Key")]


def rating_text(x: dict) -> Text:
    """Thumbs up and down. The star rating is archived by Forge and no longer shown."""
    up, down, total = votes_of(x)
    if up is None and down is None:
        return Text("—", DIM)
    if not total:
        return Text("no votes", DIM)
    return Text(f"▲{up or 0:,}", GREEN) + Text(f" ▼{down or 0:,}", DIM)


def fit_text(x: dict, version) -> Text:
    """Whether a listing says it runs on the selected server's version: ✓, ✕ or ? when either doesn't say."""
    ok = compatible(compat_of(x), version)
    return Text("✓", GREEN) if ok else Text("✕", RED) if ok is False else Text("?", DIM)


def compat_words(spec) -> str:
    if isinstance(spec, list):
        return ", ".join(map(str, spec))
    if isinstance(spec, dict):
        return " ".join(f"{k} {v}" for k, v in spec.items())
    return str(spec) if spec not in (None, "") else "not stated"


def day(ts) -> str:
    return str(ts or "")[:10] or "—"


def same(a, b) -> bool:
    """Whether two cells read and look the same. Text's own == leaves out its base style, the colour a name
    takes from its team."""
    if isinstance(a, Text) and isinstance(b, Text):
        return a.plain == b.plain and a.style == b.style and a.spans == b.spans
    return type(a) is type(b) and a == b


class Card(Static):
    """A station in the sidebar. A card never changes size, so a repaint skips the layout pass: with a fleet of
    them repainting every second, layout was most of what the console spent its time on."""

    wide = 0  # its width, as last laid out: asking per repaint re-arranges the whole screen, with 200 of them

    def on_resize(self, event: events.Resize) -> None:
        self.wide = event.size.width

    def show(self, content) -> None:
        self.update(content, layout=False)


class Stations(ListView):
    """The sidebar list. A card scrolled out of it isn't redrawn: Textual re-arranges the whole screen for a repaint of
    something it isn't showing, and with 200 stations that was most of what the console spent its time on."""

    def sync(self) -> None:
        """Works out which cards are in view, and draws the ones that just came into it."""
        app, high = self.app, self.outer_size.height
        if not high:  # not laid out yet, or hidden: everything counts
            return
        row, top = (2 if app.fleet else 4), int(self.scroll_y)
        seen = range(top // row, (top + high - 1) // row + 1)
        if seen != app.seen:
            app.seen = seen
            for st in app.stations[seen.start:seen.stop]:
                app.paint_card(st)

    def on_resize(self) -> None:
        self.sync()

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        self.sync()


class Roster(DataTable):
    """A table refreshed in place. Rows stay put, so a refresh never scrolls the view, and the cursor stays with
    the selected row's id when rows come, go or reorder: an unban must hit the entry the operator chose. Only the
    cells that changed are touched: a poll that finds the same numbers redraws nothing."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.ids: list[str] = []
        self.cells: list[list] = []

    @property
    def selected(self) -> str | None:
        return self.ids[self.cursor_row] if 0 <= self.cursor_row < len(self.ids) else None

    def fill(self, rows: list[tuple[str, list]]) -> None:
        """rows: (id, cells) in display order."""
        keep, have, changed = self.selected, self.row_count, len(rows) != self.row_count
        for r, (_, cells) in enumerate(rows):
            if r < have:
                for c, v in enumerate(cells):
                    if not same(v, self.cells[r][c]):
                        self.update_cell_at(Coordinate(r, c), v, update_width=True)
                        changed = True
            else:
                self.add_row(*cells, key=str(r))
        for r in range(have - 1, len(rows) - 1, -1):
            self.remove_row(str(r))
        self.ids, self.cells = [i for i, _ in rows], [list(cells) for _, cells in rows]
        if keep in self.ids and self.ids.index(keep) != self.cursor_row:
            self.move_cursor(row=self.ids.index(keep))
        if changed:
            self.refresh()


class Feed(RichLog):
    """A log that follows new lines only while it's scrolled to the bottom, so reading back isn't yanked away, and
    counts what came in meanwhile. Lines written while its tab is hidden wrap to the width it will have: hidden, it
    has no width, and a plain RichLog then wraps everything written to it at 78 columns."""
    unread = 0
    inset = 8  # what the tabs' width loses to the border, padding and scrollbar; measured whenever it's shown
    room = 0  # what a line wraps to while shown, 0 while hidden. Measured when that changes, never per line: the
    #           first size read after a write re-arranges the whole screen, and that was most of what a couple of
    #           hundred servers' events cost

    def measure(self) -> None:
        if self.size.width:
            self.room = self.scrollable_content_region.width
            self.app.tabs_width = self.app.query_one(TabbedContent).size.width
            self.inset = self.app.tabs_width - self.room + (
                0 if self.show_vertical_scrollbar else self.styles.scrollbar_size_vertical)  # as if the bar is there

    def on_resize(self, event: events.Resize) -> None:  # RichLog's own runs as well
        self.measure()

    def on_show(self) -> None:
        self.measure()

    def on_hide(self) -> None:
        self.room = 0

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        if new < old or self.is_vertical_scroll_end:  # only a person scrolls up; the bottom resumes following
            self.auto_scroll = self.is_vertical_scroll_end
            if self.auto_scroll and self.unread:
                self.unread, self.border_subtitle = 0, ""

    def write(self, content, width: int | None = None, expand: bool = False, shrink: bool = True,
              scroll_end: bool | None = None, animate: bool = False):
        if width is None and (self.room or self.app.tabs_width > self.inset):
            console = self.app.console
            line = measure_renderables(console, console.options, [content]).maximum
            # shown: RichLog's own sum, spelled out so it doesn't ask for the size. Hidden: as wide as the line, up
            # to what's there once shown
            width = max(min(line, self.room), self.min_width) if self.room else min(line, self.app.tabs_width - self.inset)
        if not self.auto_scroll:
            self.unread += 1
            self.border_subtitle = f"▼ {self.unread} NEW  ·  End to follow"
        return super().write(content, width, expand, shrink, scroll_end, animate)

    def clear(self):
        self.unread, self.border_subtitle, self.auto_scroll = 0, "", True
        return super().clear()


class CommandInput(Input):
    BINDINGS = [Binding("up", "history(-1)", show=False), Binding("down", "history(1)", show=False)]

    def action_history(self, step: int) -> None:
        app, h = self.app, self.app.history
        if h:
            if app.hpos == len(h):  # leaving what's being typed: keep it for the way back down
                app.draft = self.value
            app.hpos = max(0, min(len(h), app.hpos + step))
            self.value = h[app.hpos] if app.hpos < len(h) else app.draft
            self.cursor_position = len(self.value)


# --- the app ------------------------------------------------------------------------------------------------------
class OniApp(App):
    CSS_PATH = "oni.tcss"
    TITLE = "ONI RCON"
    seen = range(1 << 30)  # the cards in view in the sidebar, once it's laid out: the rest aren't redrawn
    tabs_width = 0  # the tabs' width as last laid out, for the feeds written to while hidden
    BINDINGS = [
        Binding("f1", "tab('assets')", "Assets"), Binding("f2", "tab('intercepts')", "Intercepts"),
        Binding("f3", "tab('operations')", "Ops"), Binding("f4", "tab('blacklist')", "Blacklist"),
        Binding("f5", "tab('console')", "Console"), Binding("f6", "tab('forge')", "Forge"),
        Binding("f7", "tab('health')", "Health"),
        Binding("ctrl+b", "broadcast", "Broadcast"),
        Binding("ctrl+r", "refresh", "Refresh"), Binding("x", "redact", "Redact"),
        Binding("question_mark", "help", "Help"), Binding("slash", "focus_input", "Type", show=False),
        Binding("g", "goto", "Go to", show=False),
        *[Binding(str(i), f"station({i - 1})", show=False) for i in range(1, 10)],
    ]

    def __init__(self, servers: list[Server], by: str, intro: bool | str = True, updater: Callable[[], str] | None = None,
                 hint: str = "", forge: ForgeSetup | None = None, prefs: Prefs | None = None,
                 health: HealthSetup | None = None):
        super().__init__()
        # intro: "full" (the archive, then the clearance check), "quick" (the clearance check), "off"; True is quick
        self.by, self.intro, self.updater, self.hint = by, {True: "quick", False: "off"}.get(intro, intro), updater, hint
        self.prefs = prefs or Prefs()
        self.fsetup = forge or ForgeSetup()
        self.fstate = ForgeState(self.fsetup.state_dir / "forge-state.json" if self.fsetup.state_dir else None)
        self.rounds = Rounds(self.fsetup.state_dir / "rounds.jsonl" if self.fsetup.state_dir else None)
        self.forge: ForgeClient | None = None
        self.listings: list[dict] = []  # the catalog as fetched, in its order
        self.listing_rows: dict[str, dict] = {}
        self.details: dict[str, dict] = {}  # listing id -> the listing with its versions, once fetched
        self.forge_next: dict | None = None  # the query for the catalog's next page
        self.forge_note, self.forge_stale, self.forge_opened = "", 0, False
        self.featured: dict[str, str] = {}  # listing id -> the favourites collection it's in, while that view is up
        self.sup, self.sup_key, self.sup_fit = Superintendent(), None, None  # it, what it drew, the layout it fits
        self.versions_for = ""  # the listing whose versions the table shows: its cursor starts on the newest installable
        self.detail_want: str | None = None  # the listing whose versions are being fetched
        self.watching, self.watch_said = False, ""  # a look at the changes feed under way; the last fault told
        self.frame, self.lit = 0, set()  # lit: cards mid-pulse
        self.stations = [Station(s, s.name or s.where, index=i) for i, s in enumerate(servers)]
        self.by_rcon: dict[Rcon, Station] = {}
        self.cards = [Card(classes="card") for _ in servers]
        self.fleet = len(servers) > FLEET
        self.label_width = self.measure_labels()
        self.sel, self.redact, self.raw_events = 0, True, False
        self.feed: deque = deque(maxlen=3000)
        self.filters, self.local_only = set(CATS), False
        self.tunnels: dict[str, Tunnel] = {}
        self.history: list[str] = []
        self.hpos, self.draft = 0, ""
        self.row_players: dict[str, dict] = {}
        self.ban_rows: dict[str, str] = {}
        self.vpn_rows: dict[str, str] = {}
        self.alert_at = float("-inf")  # when the last alert came in; each station counts its own unseen ones
        self.hsetup = health or HealthSetup()
        self.mon = Monitor(self.hsetup.min_free_mb)
        self.health_gate: asyncio.Semaphore | None = None  # made in the loop that uses it
        self.service_at: dict[str, float] = {}  # ssh destination ("" = here) -> when its service check is next due
        self.service_err: dict[str, str] = {}
        self.banner_until = 0.0

    @property
    def cur(self) -> Station:
        return self.stations[self.sel]

    def notify(self, message: str, *, markup: bool = False, **kw) -> None:
        if "title" in kw:
            kw["title"] = scrub(str(kw["title"]))
        # toasts carry chat and server text: never read it as markup, and never show a key in it
        super().notify(scrub(str(message)), markup=markup, **kw)

    def compose(self) -> ComposeResult:
        yield Static(id="masthead")
        yield Static(id="banner")  # a crash or a memory squeeze, on every tab until it's clicked away
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Stations(*[ListItem(c) for c in self.cards], id="stations")
                yield Button("+ ADD SERVER", id="add-server", compact=True)
                with Center():
                    yield Static(id="crest")
                yield Static(id="super")  # always shown: the crest above gives way before it does
                yield Static(id="uplink")
            with TabbedContent(id="tabs", initial="assets"):
                with TabPane("[dim]F1[/] ASSETS", id="assets"):
                    with Assets():
                        yield Roster(id="players", cursor_type="row", zebra_stripes=True)
                        with VerticalScroll(id="dossier-box"):
                            yield Static(id="dossier")
                            with Grid(id="player-actions"):
                                for op, label, _ in PLAYER_OPS:
                                    yield Button(label, id=f"pl-{op}", compact=True,
                                                 variant="error" if op in ("kick", "ban") else "default")
                            with Collapsible(title="RAW DATA", id="raw-box"):
                                yield Static(id="dossier-raw")
                with TabPane("[dim]F2[/] INTERCEPTS", id="intercepts"):
                    with Horizontal(id="filters"):
                        for cat in CATS:
                            yield Checkbox(cat.upper(), True, id=f"f-{cat}")
                        yield Checkbox("THIS STATION ONLY", False, id="f-local")
                    yield Feed(id="feed", wrap=True, max_lines=3000)
                    yield Input(id="say", placeholder="» type to chat as [Server] on this server   ·   start with @all "
                                                      "to reach every server")
                with TabPane("[dim]F3[/] OPERATIONS", id="operations"):
                    with Horizontal(id="ops-top"):
                        yield Static(id="sitrep")
                        with Vertical(id="ops-panel"):
                            ops = {op: (label, variant) for op, label, variant in OPS}
                            for group, names in OP_GROUPS:
                                yield Static(group, classes="ops-head")
                                with Grid(classes="ops-grid"):
                                    for op in names:
                                        yield Button(ops[op][0], id=f"op-{op}", variant=ops[op][1], compact=True)
                    with Horizontal(id="ops-bottom"):
                        yield Roster(id="rotation", cursor_type="none", zebra_stripes=True)
                        yield Static(id="theatre")
                with TabPane("[dim]F4[/] BLACKLIST", id="blacklist"):
                    with Blacklist():
                        yield Roster(id="bans", cursor_type="row", zebra_stripes=True)
                        yield Roster(id="vpn", cursor_type="row", zebra_stripes=True)
                        with Horizontal(classes="bar"):
                            for op, label, variant in BAN_OPS:
                                yield Button(label, id=f"bl-{op}", variant=variant)
                with TabPane("[dim]F5[/] CONSOLE", id="console"):
                    yield Feed(id="console-log", wrap=True, max_lines=5000)
                    with Horizontal(id="console-bar"):
                        yield CommandInput(id="cmd", placeholder="command   ·   @all <command> runs on every station   ·   ↑↓ history",
                                           suggester=SuggestFromList(COMMANDS, case_sensitive=False))
                        yield Checkbox("RAW EVENTS", False, id="raw")
                with TabPane("[dim]F6[/] FORGE", id="forge"):
                    with ForgePane():
                        with Horizontal(id="forge-bar"):
                            yield Input(id="forge-q", placeholder="search the catalog   ·   Enter to search")
                            yield Select([(VIEW_NAMES.get(x, x.upper()), x) for x in VIEWS],
                                         value="trending", allow_blank=False, id="forge-sort")
                            yield Select([(x.upper(), x) for x in WINDOWS], value="7d", allow_blank=False,
                                         id="forge-window")
                        with Horizontal(id="forge-main"):
                            yield Roster(id="listings", cursor_type="row", zebra_stripes=True)
                            with VerticalScroll(id="listing-box"):
                                yield Static(id="listing")
                                yield Roster(id="versions", cursor_type="row", zebra_stripes=True)
                                with Grid(id="forge-actions"):
                                    yield Button("INSTALL  i", id="fg-install", variant="primary", compact=True)
                                    yield Button("LOAD NOW  l", id="fg-load", compact=True)
                                    yield Button("ACKNOWLEDGE  a", id="fg-ack", variant="warning", compact=True)
                                    yield Button("KEY  k", id="fg-key", compact=True)
                                    yield Button("MORE  n", id="fg-more", compact=True)
                                with Collapsible(title="RAW DATA", id="forge-raw-box"):
                                    yield Static(id="listing-raw")
                                yield Static(id="forge-credit")
                with TabPane("[dim]F7[/] HEALTH", id="health"):
                    yield Static(id="health-setup")
                    yield Roster(id="crashes", cursor_type="row", zebra_stripes=True)
                    with Horizontal(id="health-bottom"):
                        with Vertical(id="host-box"):
                            yield Static(id="host")
                            yield Roster(id="containers", cursor_type="none", zebra_stripes=True)
                        yield Static(id="workaround")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(ONI)
        self.theme = "oni"
        cols = {"#players": ["#", "CALLSIGN", "TEAM", "SCORE", "K", "D", "K/D", "PING", "HEALTH", "SHIELD", "FLAGS"],
                "#rotation": ["#", "MAP", "MODE"], "#bans": ["TYPE", "TARGET", "NAME", "REASON", "EXPIRES", "BY"],
                "#vpn": ["ALLOWED THROUGH VPN", "NOTE"],
                "#listings": ["", "TYPE", "TITLE", "AUTHOR", "RATING", "RECENT", "FIT"],
                "#versions": ["", "VERSION", "PUBLISHED", "NOTES"],
                "#crashes": ["WHEN", "SERVER", "CLASS", "PLAYERS", "DETAIL", "RCON"],
                "#containers": ["SERVER", "STARTED", "UP", "OOMKILLED", "EXIT", "RESTARTS"]}
        for sel, c in cols.items():
            if not (t := self.query_one(sel, DataTable)).columns:  # once, whatever mounts twice
                t.add_columns(*c)
        for sel, title in {"#players": "ASSETS IN THEATRE", "#dossier-box": "DOSSIER", "#feed": "SIGINT FEED",
                           "#sitrep": "SITREP", "#theatre": "THEATRE", "#rotation": "ROTATION", "#bans": "BLACKLIST",
                           "#vpn": "VPN ALLOWANCES", "#console-log": "COMMAND LOG", "#listings": "FORGE CATALOG",
                           "#listing-box": "FORGE FILE", "#versions": "VERSIONS", "#host-box": "HOST",
                           "#workaround": "WORKAROUND"}.items():
            self.query_one(sel).border_title = title
        for wid, tip in TIPS.items():  # escaped: "[Server]" in a tip is text, not a style
            self.query_one(f"#{wid}").tooltip = escape(tip)
        tabs = self.query_one(TabbedContent)
        for pane, tip in TAB_TIPS.items():
            tabs.get_tab(pane).tooltip = escape(tip)
        self.log_cmd(Text("ONI remote console. Commands go to the selected station; `@all` prefixes run on every "
                          "station; `help` asks the server; `clear` clears this log.", DIM))

        self.set_class(self.fleet, "-fleet")
        self.forge_connect()
        self.paint_forge()
        remotes: dict[str, list] = {}
        for st in self.stations:
            if st.server.ssh and not st.server.url:
                remotes.setdefault(st.server.ssh, []).append((st.server.host, st.server.port))
        for dest, r in remotes.items():
            self.tunnels[dest] = Tunnel(dest, r, self.on_tunnel)
            self.run_worker(self.tunnels[dest].run(), group="tunnels", exit_on_error=False)
        gate = asyncio.Semaphore(SIGN_INS)
        for st in self.stations:
            s, ready = st.server, None
            if s.url:
                url = s.url
            elif s.ssh:
                tun = self.tunnels[s.ssh]
                url, ready = f"ws://127.0.0.1:{tun.local[(s.host, s.port)]}", tun.ready
            else:
                url = f"ws://[{s.host}]:{s.port}" if ":" in s.host else f"ws://{s.host}:{s.port}"
            st.rcon = Rcon(url, s.password, self.by, self.on_rcon_event, self.on_rcon_state, ready, gate=gate,
                           on_late=self.on_late_reply)
            self.by_rcon[st.rcon] = st
            st.worker = self.run_worker(st.rcon.run(), group="rcon", exit_on_error=False)
            if s.ping_log:
                self.run_worker(self.follow_pings(st), group="pings", exit_on_error=False)

        self.health_gate = asyncio.Semaphore(HEALTH_GATE)
        now = time.monotonic()
        for st in self.stations:  # the first reports spread out, so a fleet isn't a burst of ssh
            st.check_at = now + min(st.index, 20) * 0.5
        if self.hsetup.service:
            for st in self.stations:
                if self.hsetup.source or not st.server.url:
                    self.service_at.setdefault(st.server.ssh, now + 1)
        self.set_interval(2, self.health_tick)
        self.set_interval(1, self.tick)
        self.set_interval(3, self.poll_fast)
        self.set_interval(1, self.poll_slow)
        self.set_interval(0.15, self.animate_cards)
        self.set_interval(0.12, self.tick_super)
        self.set_interval(self.fsetup.poll, self.forge_watch)
        self.set_timer(5, self.forge_watch)  # once soon after start, for what changed while the console was closed
        self.paint_all()
        if self.intro == "full" and self.animation_level == "full":
            self.prefs.intro_seen = True
            self.prefs.save()
            self.push_screen(Archive())
        elif self.intro != "off":
            self.push_screen(Boot())
        else:
            self.welcome()
        self.on_resize()
        if self.updater:  # a thread of its own rather than a worker: a slow download must not hold up quitting
            threading.Thread(target=self.check_update, daemon=True).start()

    async def follow_pings(self, st: Station) -> None:
        """Follows the server's log for the ping each player joined with. Restarts, slowing down, if it ends."""
        s, delay = st.server, 2
        while True:
            if s.ssh:
                if not (ssh := shutil.which("ssh")):
                    return
                # the remote shell holds ssh's stdin and kills the follower when it closes: a dropped link or a quit
                # leaves no `docker logs -f` behind
                proc = await asyncio.create_subprocess_exec(
                    ssh, *SSH_OPTS, s.ssh, f"{s.ping_log} & p=$!; cat >/dev/null; kill $p", limit=1 << 20,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            else:
                proc = await asyncio.create_subprocess_shell(
                    s.ping_log, limit=1 << 20, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
            try:
                async for line in proc.stdout:
                    if m := JOIN_PING.search(line.decode(errors="replace")):
                        delay = 2
                        st.pings[m[1]] = int(m[2])
                        if len(st.pings) > 5000:  # ponytail: oldest out; a week of joins on a busy server fits
                            del st.pings[next(iter(st.pings))]
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    # --- health: crashes from the logs, the host's memory (see health.py) -------------------------------------
    def watched(self, st: Station) -> bool:
        """Whether this server has a health report to read: a command for it, or a stand-in (--demo, tests)."""
        return bool(self.hsetup.source or st.server.health_cmd)

    def health_tick(self) -> None:
        """Every couple of seconds: start the reports and service checks that are due."""
        if not self.is_running:
            return
        now = time.monotonic()
        for st in self.stations:
            if self.watched(st) and not st.checking and now >= st.check_at:
                st.checking = True
                self.run_worker(self.health_check(st), group="health", exit_on_error=False)
        for host, due in list(self.service_at.items()):
            if now >= due:
                self.service_at[host] = now + HEALTH_EVERY + 3600  # until the check says it's done
                self.run_worker(self.health_service(host), group="health", exit_on_error=False)

    async def health_check(self, st: Station) -> None:
        """One server's report. It asks for the last day the first time, then for what's come since the last one
        (with room for a gap): the same crash seen again is dropped by the monitor."""
        notes: list[Notice] = []
        try:
            first = str(st.index) not in self.mon.seen
            since = "86400s" if first else f"{min(86400, max(900, int((time.time() - st.health_ok) * 2) + 300))}s"
            async with self.health_gate:
                if src := self.hsetup.source:
                    text = await src.report(st.server.port, since)
                else:
                    text = await hc.run(st.server.health_cmd.replace("{since}", since), st.server.ssh)
            notes = self.mon.ingest(str(st.index), st.server.ssh, text)
            st.health_ok, st.health_err = time.time(), ""
        except Exception as e:  # ssh down, a script that fails, a stand-in that broke: say so, keep going
            err = hc.scrub(str(e) if isinstance(e, hc.HealthError) else f"{type(e).__name__}: {e}")
            if err != st.health_err:
                self.log_event(st, {"event": "uplink", "text": f"HEALTH REPORT FAILED  {err}"})
            st.health_err = err
        finally:
            st.checking = False
            st.check_at = time.monotonic() + HEALTH_EVERY
        if self.is_running:
            for n in notes:
                self.health_notice(n)
            self.paint_health()

    async def health_service(self, host: str) -> None:
        unit = self.hsetup.service
        try:
            async with self.health_gate:
                if src := self.hsetup.source:
                    text = await src.service(unit)
                else:
                    text = await hc.run(hc.service_command(unit), host)
            self.mon.services[host] = hc.parse_service(text, time.time())
            self.service_err.pop(host, None)
        except Exception as e:
            self.service_err[host] = hc.scrub(str(e) if isinstance(e, hc.HealthError) else f"{type(e).__name__}: {e}")
        finally:
            self.service_at[host] = time.monotonic() + HEALTH_EVERY
        if self.is_running:
            self.paint_health()

    def health_notice(self, n: Notice) -> None:
        """A crash or a memory squeeze that came up while this console was open: into the feed, onto a banner, and
        the alert (a beep, the card, the CONDITION)."""
        st = self.stations[int(n.key)] if n.key else None
        self.log_event(st, {"event": "health", "text": n.text, "level": n.level})
        banner = self.query_one("#banner", Static)
        banner.update(Text(f" ⚠ {st.label if st else 'HOST'}  {n.text}      click to dismiss"))
        banner.set_class(n.level != "crit", "-warn")
        banner.add_class("-on")
        self.banner_until = time.monotonic() + BANNER_FOR
        self.alert(st)

    def hide_banner(self) -> None:
        self.banner_until = 0.0
        self.query_one("#banner").remove_class("-on", "-dim")

    @on(events.Click, "#banner")
    def _banner(self) -> None:
        self.hide_banner()

    def paint_health(self) -> None:
        now = time.time()
        on = any(self.watched(st) for st in self.stations)
        setup, crashes, host_box = (self.query_one(i) for i in ("#health-setup", "#crashes", "#host-box"))
        setup.display, crashes.display, host_box.display = not on, on, on
        self.query_one("#workaround").display = bool(self.hsetup.service)
        if not on:
            setup.update(self.health_howto())
        label = lambda key: self.stations[int(key)].label if key else "HOST"
        rows = [(f"{e.key}:{e.cls}:{int(e.ts)}",
                 [Text(f"{when(e.ts)}  {age(now - e.ts)} ago"), Text(label(e.key), WHITE),
                  Text(e.cls, CRASH_COLOR[e.cls]), Text(str(e.players) if e.players is not None else "—",
                                                       AMBER if e.players else DIM),
                  Text(e.detail, DIM), Text("DROPPED", CYAN) if e.rcon else Text("—", DIM)])
                for e in self.mon.recent()]
        t = self.query_one("#crashes", Roster)
        t.fill(rows)
        t.border_title = Text(f"CRASHES · LAST 24 H · {len(rows)}")
        t.border_subtitle = Text("NONE IN THE LAST 24 H", GREEN) if not rows and self.mon.seen else ""
        hosts = Table.grid(padding=(0, 2))
        hosts.add_column(style=DIM, no_wrap=True)
        hosts.add_column()
        seen_hosts = {st.server.ssh for st in self.stations if self.watched(st)}
        self.query_one("#host-box").border_title = Text("HOST" + ("" if len(seen_hosts) != 1 else " · " + redact_text(
            next(iter(seen_hosts)) or "this machine", self.redact)))
        for h in sorted(seen_hosts):
            snap = self.mon.hosts.get(h)
            if len(seen_hosts) > 1:
                hosts.add_row(Text("HOST", AMBER), Text(redact_text(h or "this machine", self.redact), AMBER))
            if not snap:
                bad = next((st.health_err for st in self.stations if st.server.ssh == h and st.health_err), "")
                hosts.add_row("", Text(bad and f"no report yet: {bad}" or "waiting for the first report", RED if bad else DIM))
                continue
            lvl = mem_level(snap.avail, self.mon.min_free)
            hosts.add_row("MEMORY FREE", Text(f"{snap.avail} MB" if snap.avail is not None else "—", LEVEL[lvl])
                          + Text(f"   under {self.mon.min_free} MB" if lvl == "crit" else "", RED))
            lvl = swap_level(snap.swap_used, snap.swap_total)
            hosts.add_row("SWAP USED", Text(f"{snap.swap_used} / {snap.swap_total} MB" if snap.swap_used is not None
                                           else "—", LEVEL[lvl]))
            since = (snap.oom or 0) - (snap.oom_first or 0)
            hosts.add_row("OOM KILLS", Text(str(snap.oom) if snap.oom is not None else "—",
                                            RED if since else AMBER if snap.oom else GREEN)
                          + Text(f"   +{since} since you opened this" if since else "", RED))
            hosts.add_row("REPORT", Text(f"{age(now - snap.at)} ago", DIM))
        for st in self.stations:
            if st.health_err and self.watched(st):
                hosts.add_row(Text("FAILED", RED), Text(f"{st.label}: {st.health_err}", AMBER))
        self.query_one("#host", Static).update(hosts)
        boxes = [(str(i), [Text(st.label, WHITE), Text(when(b.started) if b.started else "—"),
                           Text(age(now - b.started) if b.started else "—", DIM),
                           Text("YES", RED) if b.oom_killed else Text("no", GREEN),
                           Text(str(b.exit_code) if b.exit_code is not None else "—",
                                RED if b.exit_code == 137 else DIM),
                           Text(str(b.restarts) if b.restarts is not None else "—", DIM)])
                 for i, st in enumerate(self.stations) if (b := self.mon.boxes.get(str(st.index)))]
        self.query_one("#containers", Roster).fill(boxes)
        if self.hsetup.service:
            self.query_one("#workaround", Static).update(self.workaround_text(now))
            self.query_one("#workaround").border_title = Text(f"WORKAROUND · {self.hsetup.service}")

    def workaround_text(self, now: float) -> Table:
        g = Table.grid(padding=(0, 2))
        g.add_column(style=DIM, no_wrap=True)
        g.add_column()
        hosts = sorted({st.server.ssh for st in self.stations if self.hsetup.source or not st.server.url})
        if not hosts:
            g.add_row("", Text("No server here has an ssh destination or a box of its own to check.", DIM))
        for h in hosts:
            svc, err = self.mon.services.get(h), self.service_err.get(h)
            if len(hosts) > 1:
                g.add_row(Text("HOST", AMBER), Text(redact_text(h or "this machine", self.redact), AMBER))
            if err:
                g.add_row(Text("FAILED", RED), Text(err, AMBER))
            if not svc:
                g.add_row("", Text("waiting for the first check", DIM))
                continue
            up = svc.active == "active"
            g.add_row("SERVICE", Text(f"{'●' if up else '○'} {svc.active.upper()}", GREEN if up else RED))
            g.add_row("MOVED TAG", Text(f"{when(svc.moved)}  {age(now - svc.moved)} ago", WHITE) if svc.moved
                      else Text("none in the last 24 h", DIM))
            g.add_row("BUILD", Text(f"⚠ NOT THE PINNED BUILD  {when(svc.warn_at) if svc.warn_at else ''}\n{svc.warn}", RED)
                      if svc.warn else Text("no warning in the last 24 h", GREEN))
        return g

    @staticmethod
    def health_howto() -> Text:
        return Text.assemble(
            ("HEALTH WATCH IS OFF\n\n", AMBER),
            ("Give oni-rcon a command that prints a small report, and this tab shows crashes read from each server's log, "
             "the host's memory and whether a container was killed for it. Add it to the config file, above the first "
             "[table]:\n\n", WHITE),
            ("  health_cmd = ", CYAN), ("\"docker logs --timestamps --since {since} my-server-{port} 2>&1\"\n\n", WHITE),
            ("{port} is the server's RCON port, {since} how far back to read. It runs on the server's ssh host, or here "
             "without one, and only reads. Add a host line (memory) and a container line (docker inspect) for the "
             "HOST panel: oni-rcon.example.toml has a whole docker example, and the README says what each line "
             "means.\n\n", DIM),
            ("health_min_free_mb", CYAN), (" (default 1024) is the free memory below which it alerts.  ", DIM),
            ("health_service", CYAN), (" names a systemd service to watch.", DIM))

    def check_update(self) -> None:
        try:
            msg = self.updater()
        except Exception:  # best effort, and a traceback from a thread would land on top of the screen
            return
        if msg:
            with contextlib.suppress(RuntimeError):  # quit in the meantime
                self.call_from_thread(self.notify, msg, title="UPDATE", timeout=20)

    def welcome(self) -> None:
        """Once the boot screen is gone: a pointer for someone new, if __main__ asked for one."""
        if self.hint:
            self.notify(self.hint, title="WELCOME, OPERATOR", timeout=15)
            self.hint = ""

    def on_resize(self) -> None:
        self.set_class(self.size.width < 140, "-narrow")  # on the app: the boot screen may be the one on top
        self.set_class(self.size.width >= 200, "-wide")
        self.set_class(self.size.width < 120, "-tight")  # F3's sitrep and button grid no longer fit side by side
        self.paint_masthead()
        self.call_after_refresh(self.refit)
        # the sidebar crest takes what the station cards, the add button and the uplink leave, and goes when that's
        # too little
        self.fit_sidebar()
        free = (self.size.height - 11 - self.sup_rows() - len(self.tunnels)
                - (2 if self.fleet else 4) * len(self.stations))
        crest = self.query_one("#crest", Static)
        crest.update(art := emblem(min(free, 16), 32))
        crest.display = bool(art.plain)

    def sidebar_room(self) -> tuple[int, int, int]:
        """(rows the sidebar has once the add button and the uplink are counted, what all the station cards need,
        the card height)."""
        card = 2 if self.fleet else 4
        return self.size.height - 10 - len(self.tunnels), card * len(self.stations), card

    def sup_across(self) -> int:
        return self.query_one("#sidebar").size.width - 2  # a cell of padding each side of the strip

    def sup_size(self) -> int:
        """The Superintendent's face: the biggest that fits, across (beside it, its words need 14 cells; the larger
        faces put them underneath instead) and down with every station card showing; failing that the list gives up
        rows (it scrolls) to two cards, then to one; failing that the smallest face, which is what a very short
        terminal gets."""
        room, need, card = self.sidebar_room()
        across = self.sup_across()
        for n, spare in [(HUGE, need), (XL, need), (LARGE, need), (BIG, need), (SMALL, need), (MINI, need),
                         (SMALL, 2 * card), (MINI, 2 * card), (MINI, card)]:
            if across >= (n if n >= LARGE else n + 1 + WORDS) and room - min(need, spare) >= self.sup_rows(n):
                return n
        return MINI

    def sup_rows(self, n: int | None = None) -> int:
        """Rows its strip takes: the face, its words if they're under it, a line above and the rule over that."""
        n = n or self.sup_size()
        return n // 2 + 2 + BELOW * below(n, self.sup_across())

    def fit_sidebar(self) -> None:
        """The strip and the uplink stay on screen: the station list takes what they leave, and scrolls past it."""
        room, _, card = self.sidebar_room()
        self.query_one("#stations").styles.max_height = max(card, room - self.sup_rows())
    def tick_super(self) -> None:
        """Redraw the Superintendent when its face or words changed: a blink, an easing expression, a new reaction."""
        if not self.is_running:  # the timer can fire once more while quitting
            return
        self.sup.motion = self.animation_level != "none"
        box, n = self.query_one("#super", Static), self.sup_size()
        if (n, self.size) != self.sup_fit:  # the terminal or the sidebar changed: the list makes room again
            self.sup_fit = n, self.size
            self.fit_sidebar()
        art, key = sup_card(self.sup, box.content_size.width or 27, n)
        if key != self.sup_key:
            self.sup_key = key
            box.update(art)

    def refit(self) -> None:
        """After a resize, once laid out: the panels that shape themselves to the room they have."""
        if self.is_running:
            self.tabs_width = self.query_one(TabbedContent).size.width
            self.paint_dossier()
            self.paint_ops()

    # --- connections -----------------------------------------------------------------------------------------
    def tunnel_of(self, st: Station) -> Tunnel | None:
        return self.tunnels.get(st.server.ssh) if st.server.ssh and not st.server.url else None

    def link(self, st: Station, spin: str = "◌") -> tuple[str, str, bool]:
        """Where a station's sign-in stands, for the boot log: (word, colour, settled)."""
        state, tun = st.rcon.state if st.rcon else "connecting", self.tunnel_of(st)
        if state == "online":
            return "SECURE", GREEN, True
        if state == "connecting" and not (tun and tun.state == "down"):
            return f"{spin} {'AWAITING TUNNEL' if tun and tun.state != 'up' else 'HANDSHAKE'}", AMBER, False
        return self.why(st), RED, True

    def why(self, st: Station, short: bool = True) -> str:
        """Why a station isn't online, looking through to its SSH tunnel when that's what's holding it up."""
        rc, tun = st.rcon, self.tunnel_of(st)
        if tun and tun.state != "up" and (rc is None or rc.state in ("connecting", "offline")):
            if tun.state == "down":
                return explain(tun.detail, short=short)
            return "OPENING SSH TUNNEL" if short else "Opening the SSH tunnel…"
        if rc is None or rc.state == "connecting":
            return "CONNECTING…" if short else "Connecting…"
        return explain(rc.detail, rc.state, short=short)

    def on_rcon_state(self, rcon: Rcon, state: str, detail: str) -> None:
        if not self.is_running:  # a late reply or event while quitting: the widgets are already gone
            return
        st = self.by_rcon[rcon]
        if state == "online":
            st.medals.new_game()  # what happened while we were away is unknown
            self.relabel()
            if st is self.cur:  # its version decides what fits it in the catalog
                self.paint_forge()
            self.fetch(st, "status", "players", "maps", "modes", "nextmap", "vote", "bans", "vpn")
            st.polled = time.monotonic()
            self.log_event(st, {"event": "uplink", "text": f"SECURE  {detail}"})
        elif state == "denied":
            self.log_event(st, {"event": "uplink", "text": f"SIGN-IN REFUSED: {detail}"})
            self.notify(f"{explain(detail, state)}\nNot retried by itself. RECONNECT (F3) asks for the password again.",
                        title=f"{st.label} · SIGN-IN REFUSED", severity="error", timeout=20)
        elif state == "offline" and st.prev == "online":
            self.log_event(st, {"event": "uplink", "text": f"LOST  {detail}"})
            tun = self.tunnel_of(st)
            if not (tun and tun.state == "down"):  # the server restarting, until its log says otherwise
                self.mon.dropped(str(st.index))
                if self.watched(st):
                    st.check_at = min(st.check_at, time.monotonic() + 8)  # it comes back in 5 s: read the log then
        if state != "online":
            self.rounds.lost(st.server.where)  # how a round ends unseen isn't known: it isn't counted
        retry = re.search(r"retry in (\d+)s", detail) if state == "offline" else None
        st.retry_at = time.monotonic() + int(retry[1]) if retry else 0.0
        if state != "connecting":
            st.prev = state
        self.paint_card(st)
        self.paint_masthead()

    def on_tunnel(self, tun: Tunnel, state: str, detail: str) -> None:
        if not self.is_running:  # a late reply or event while quitting: the widgets are already gone
            return
        if state == "down":
            self.log_event(None, {"event": "uplink", "text": f"SSH {tun.dest} DOWN  {detail}"})
        for st in self.stations:  # the ones waiting on it say why in its words
            if self.tunnel_of(st) is tun and not st.online:
                self.paint_card(st)
        self.paint_uplink()

    def on_worker_state_changed(self, e) -> None:
        if e.state == WorkerState.ERROR:
            self.notify(repr(e.worker.error), title="INTERNAL FAULT", severity="error", timeout=15)

    def relabel(self) -> None:
        """Server names usually share a community prefix ("Some Clan | OCE ..."): show only what differs."""
        reported = [st.rcon.info.get("server", "") if st.rcon else "" for st in self.stations]
        names = [n for n in reported if n]
        pre = commonprefix(names) if len(set(names)) > 1 else ""
        cut = max(pre.rfind(" "), pre.rfind("|")) + 1
        before = [st.label for st in self.stations]
        # several communities on one console ("ALPHA · Big Team", "BRAVO · Lockout"): the tag goes on the card's
        # second line, and the label keeps what tells a server apart
        tags = Counter(split_tag(n[cut:].strip(" |·-"))[0] for n in names)
        for st, n in zip(self.stations, reported):
            if n and not st.server.name:
                st.label = n[cut:].strip(" |·-") or n
                tag, own = split_tag(st.label)
                st.label, st.tag = (own, tag) if tag and tags[tag] > 1 else (st.label, "")
        seen = Counter(st.label for st in self.stations)
        for st in self.stations:  # two communities can each run a "Big Team": those keep their tag
            if st.tag and seen[st.label] > 1:
                st.label, st.tag = f"{st.tag} · {st.label}", ""
        self.label_width = self.measure_labels()
        for st in self.stations:
            self.paint_card(st)
        if [st.label for st in self.stations] != before:
            self.repaint_feed()  # the lines so far name stations as they were labelled then

    def measure_labels(self) -> int:
        """How wide the feed's station column is: as wide as the longest label, within reason."""
        return max(4, min(18, max((len(s.label) for s in self.stations), default=4)))

    def reconnect(self, st: Station) -> None:
        st.worker.cancel()
        st.worker = self.run_worker(st.rcon.run(), group="rcon", exit_on_error=False)

    # --- data ------------------------------------------------------------------------------------------------
    def fetch(self, st: Station, *what: str) -> None:
        what = tuple(w for w in what if w not in st.inflight)
        if st.online and what:
            st.inflight.update(what)

            async def go():
                try:
                    await self._fetch(st, what)
                finally:
                    st.inflight.difference_update(what)
            self.run_worker(go(), group="fetch", exit_on_error=False)

    async def _fetch(self, st: Station, what: tuple) -> None:
        replies = await asyncio.gather(*(st.rcon.call(w) for w in what), return_exceptions=True)
        for w, r in zip(what, replies):
            if isinstance(r, dict) and r.get("ok") and isinstance(r.get("data"), dict):
                if w == "status" and r["data"].get("phase") != st.data.get("status", {}).get("phase"):
                    st.medals.new_game()
                if w == "players":
                    st.learn(r["data"].get("players") or [])
                if w == "status":  # rounds played, kept locally: see stats.py
                    where = st.server.where
                    self.rounds.observe(where, r["data"], lambda m, g, where=where: self.fstate.playing(where, m, g))
                st.data[w] = r["data"]
                if w in ("status", "players") and self.watched(st):  # the crowd, for a crash's player count
                    self.mon.sample(str(st.index), num(r["data"].get("players")) if w == "status"
                                    else len(r["data"].get("players") or []))
        self.paint(st, what)

    def poll_fast(self) -> None:
        if not self.is_running:  # the 3 s timer can fire once more while quitting
            return
        self.fetch(self.cur, "players", "status")
        if self.query_one(TabbedContent).active == "operations":
            self.fetch(self.cur, "vote")

    def poll_slow(self) -> None:
        """Each second, the stations due: every one comes round once in SLOW_POLL seconds, spread out rather than
        all at once, so a fleet is a steady trickle of requests and repaints instead of a burst. An empty server's
        roster isn't asked for: its status says nobody's there, and a join brings the roster in."""
        if not self.is_running:
            return
        now = time.monotonic()
        due = sorted((st for st in self.stations if st.online and now - st.polled >= SLOW_POLL), key=lambda s: s.polled)
        for st in due[:-(-len(self.stations) // SLOW_POLL)]:
            st.polled = now
            empty = num(st.data.get("status", {}).get("players")) == 0 and not st.players
            self.fetch(st, "status", *(() if empty else ("players",)))

    async def cmd(self, st: Station, command: str, *args: str, toast: bool = True, show_data: bool = False,
                  quiet: bool = False, brief: bool = False) -> dict:
        """Run one command, write it to the command log (the audit trail), toast the result. `ok` comes back None
        when no reply came in time: it may still have run. `quiet` keeps even a failure out of the toasts, and
        `brief` logs only the station and its reply, for a fan-out that names the command once and sums up."""
        try:
            r = await st.rcon.call(command, *args)
            r = {**r, "ok": bool(r.get("ok"))}  # None is kept for no reply at all
        except TimeoutError as e:
            r = {"ok": None, "text": str(e)}
        except Exception as e:
            r = {"ok": False, "text": str(e) or type(e).__name__}
        self.log_reply(st, None if brief else " ".join([command, *map(q, args)]), r, show_data)
        ok = r.get("ok")
        if toast or not (ok or quiet):
            self.notify(self.reply_text(r), title=f"{st.label} · {command.upper()}",
                        severity="information" if ok else "warning" if ok is None else "error")
        return r

    def reply_text(self, r: dict) -> str:
        ok = r.get("ok")
        return redact_text(str(r.get("text") or ("done" if ok else "no reply" if ok is None else "failed")), self.redact)

    def log_reply(self, st: Station, line: str | None, r: dict, show_data: bool = False, late: bool = False) -> None:
        """The command and its reply in the command log; with no line, one line of station and reply."""
        ok = r.get("ok")
        reply = Text(self.reply_text(r), GREEN if ok else AMBER if ok is None else RED)
        if line is None:
            self.log_cmd(Text(f"  {'✓' if ok else '…' if ok is None else '✕'} ", reply.style)
                         + Text(f"{st.label}  ", CYAN) + reply)
        else:
            self.log_cmd(Text(time.strftime("%H:%M:%S "), DIM) + Text(f"{st.label} ", CYAN)
                         + Text(redact_text(f"{'« late reply to' if late else '»'} {line}", self.redact),
                                DIM if late else WHITE))
            self.log_cmd(Text("  ") + reply)
        if show_data and r.get("data") is not None:
            self.log_cmd(JSON.from_data(redact_data(r["data"], self.redact)))

    def on_late_reply(self, rcon: Rcon, line: str, r: dict) -> None:
        """A reply that came after its command timed out: logged, so the outcome isn't left a mystery."""
        if self.is_running:
            self.log_reply(self.by_rcon[rcon], line, r, late=True)

    async def fanout(self, stations: list[Station], command: str, *args: str, show_data: bool = False) -> None:
        """One command on many stations, FANOUT at a time. All at once, a fleet's replies queue up behind each
        other until they time out, and whatever the servers share (a host, a tunnel, a gateway) takes the whole
        burst. Each reply goes in the command log as usual; a fleet gets one tally at the end, not a toast each."""
        gate, many = asyncio.Semaphore(FANOUT), len(stations) > 1
        if many:
            self.log_cmd(Text(time.strftime("%H:%M:%S "), DIM) + Text("@all ", CYAN) + Text(redact_text(
                f"» {' '.join([command, *map(q, args)])}  →  {len(stations)} stations", self.redact), WHITE))

        async def one(st: Station) -> dict:
            async with gate:
                return await self.cmd(st, command, *args, toast=False, show_data=show_data, quiet=many, brief=many)
        results = await asyncio.gather(*(one(st) for st in stations))
        if len(results) < 2 or not self.is_running:
            return
        ok, late = sum(r.get("ok") is True for r in results), sum(r.get("ok") is None for r in results)
        bad = len(results) - ok - late
        tally = Text.assemble((time.strftime("%H:%M:%S "), DIM), ("@all ", CYAN), (command, WHITE),
                              (f"  ·  {len(results)} stations  ·  ", DIM), (f"{ok} ok", GREEN),
                              *([("  ·  ", DIM), (f"{late} no reply yet", AMBER)] if late else []),
                              *([("  ·  ", DIM), (f"{bad} failed", RED)] if bad else []))
        self.log_cmd(tally)
        self.notify(tally.plain[9:], title=f"@ALL {command.upper()}",
                    severity="information" if ok == len(results) else "warning" if ok else "error")

    def send_all(self, stations: list[Station], command: str, *args: str, show_data: bool = False) -> None:
        self.run_worker(self.fanout(stations, command, *args, show_data=show_data), group="cmd", exit_on_error=False)

    def send(self, st: Station, command: str, *args: str, then: tuple = (), **kw) -> None:
        async def go():
            r = await self.cmd(st, command, *args, **kw)
            if then and r.get("ok"):
                await self._fetch(st, then)
        self.run_worker(go(), group="cmd", exit_on_error=False)

    def on_rcon_event(self, rcon: Rcon, ev: dict) -> None:
        if not self.is_running:  # a late reply or event while quitting: the widgets are already gone
            return
        st = self.by_rcon[rcon]
        if self.raw_events:
            self.log_cmd(Text(f"{st.label} ◂ ", CYAN) + Text(json.dumps(redact_data(ev, self.redact)), DIM))
        names = st.names
        if ev.get("event") == "join":  # a join names the player by ID: kills that follow may use it
            st.past.update({f: str(pick(ev, "name", "player", default="?")) for f in id_forms(pick(ev, "id", "player_id"))
                            if len(f) >= 8})
        raw_ids = [ev.get(k) for k in ("killer", "victim")]
        ev = resolve(ev, names, st.ident)
        kind, text = ev.get("event"), str(ev.get("text", ""))
        if kind == "kill":
            if any(i in st.aliases.values() for i in (ev.get("killer"), ev.get("victim"))):
                self.fetch(st, "players")  # someone the roster hasn't caught up with: ask again
                self.unmatched(st, raw_ids)
            killer = pick(ev, "killer")
            ev["_medals"] = st.medals.kill(None if killer is None else who(killer, names),
                                           who(pick(ev, "victim"), names), time.monotonic())
        st.activity.append(time.monotonic())
        self.log_event(st, ev)
        self.sup.react(ev, reaction_words(ev, names, self.redact) if self.sup.wants(ev) else "")
        if kind == "chat" and ev.get("channel") != "server" and ALERT.search(text):
            self.notify(redact_text(f"{who(pick(ev, 'name', 'player'), names)}: {text}", self.redact),
                        title=f"CALL FOR ADMIN · {st.label}", severity="warning", timeout=12)
            self.alert(st)
        elif kind == "cheat":
            self.notify(describe(ev, names, self.redact).plain, title=f"ANTI-CHEAT · {st.label}", severity="error",
                        timeout=12)
            self.alert(st)
        if kind in ("join", "leave", "refused", "kick", "ban", "mute", "unmute", "control"):
            self.fetch(st, "players", "status")
        if kind in ("ban", "unban"):
            self.fetch(st, "bans")
        if kind == "vote":
            self.fetch(st, "vote")
        if kind == "control":
            self.fetch(st, "nextmap")

    def unmatched(self, st: Station, ids: list) -> None:
        """Once per station: kills name players by an ID the roster has no field for. Field names only, never values:
        this is what's needed to teach id_forms the server's spelling."""
        if st.unmatched or not any(isinstance(i, (int, str)) and ID_LIKE.fullmatch(str(i)) for i in ids):
            return
        st.unmatched = True
        keys = sorted({k for p in st.players for k, v in p.items() if isinstance(v, (int, str))})
        self.log_cmd(Text(f"{st.label}: kills name players by an ID no roster field matches, so they read PLAYER n. "
                          f"The roster has: {', '.join(keys) or 'nothing yet'}", AMBER))

    def alert(self, st: Station | None) -> None:
        """Beep, pulse the station's card, and raise the condition. It counts as unseen, on the card and in the
        masthead, unless the operator is looking at that station or at the feed. No station: the host's."""
        self.bell()
        self.alert_at = time.monotonic()
        if st:
            st.flash = self.alert_at + 3
            if st is not self.cur and self.query_one(TabbedContent).active != "intercepts":
                st.alerts += 1
        self.paint_masthead()

    @property
    def unseen(self) -> int:
        return sum(st.alerts for st in self.stations)

    def condition(self) -> tuple[str, str]:
        """RED while an alert is unseen and for a moment after any; AMBER while a station is down, the host is short
        of memory, or something installed from Forge has been withdrawn and nobody has acknowledged it; else GREEN."""
        if self.unseen or time.monotonic() - self.alert_at < 15:
            return "RED", RED
        if not all(st.online for st in self.stations) or self.fstate.alarms() or self.mon.short():
            return "AMBER", AMBER
        return "GREEN", GREEN

    def log_event(self, st: Station | None, ev: dict) -> None:
        self.feed.append((st, ev))
        if self.shown(st, ev):
            self.query_one("#feed", RichLog).write(self.feed_line(st, ev))

    def shown(self, st: Station | None, ev: dict) -> bool:
        return KIND.get(ev.get("event"), "ops") in self.filters and not (self.local_only and st not in (None, self.cur))

    def feed_line(self, st: Station | None, ev: dict) -> Text:
        return render_event(st.label if st else "HOST" if ev.get("event") == "health" else "LINK", ev, {}, self.redact,
                            self.label_width)  # resolved on arrival

    # --- painting --------------------------------------------------------------------------------------------
    def paint(self, st: Station, what: tuple) -> None:
        if not self.is_running:  # a late reply or event while quitting: the widgets are already gone
            return
        self.paint_card(st)
        if st is not self.cur:
            return
        if "players" in what:
            self.paint_players()
        if {"status", "nextmap", "vote", "players"} & set(what):
            self.paint_ops()
        if {"bans", "vpn"} & set(what):
            self.paint_bans()

    def paint_all(self, feed: bool = True) -> None:
        for st in self.stations:
            self.paint_card(st)
        self.paint_players()
        self.paint_ops()
        self.paint_bans()
        self.paint_forge()
        self.paint_uplink()
        self.paint_masthead()
        if feed:
            self.repaint_feed()

    def tick(self) -> None:
        """Once a second: the clock, the condition, and the activity traces."""
        if not self.is_running:  # the 1 s timer can fire once more while quitting
            return
        self.paint_masthead()
        for st in self.stations:
            self.paint_card(st)
        rate = sum(sum(st.rate(1, 60)) for st in self.stations)
        live = any(st.online for st in self.stations)
        title = Text.assemble(
            ("SIGINT FEED  ", AMBER),
            ("●" if live else "○", RED if live and time.time() % 2 < 1 else blend(RED, INK, .4) if live else DIM),
            (f" LIVE · {rate}/min" if live else " NO SIGNAL", DIM))
        feed = self.query_one("#feed")
        if feed.border_title != title:
            feed.border_title = title
        if self.banner_until:
            if time.monotonic() > self.banner_until:
                self.hide_banner()
            else:
                self.query_one("#banner").set_class(time.time() % 2 < 1, "-dim")  # flashes
        if self.query_one(TabbedContent).active == "health" and int(time.monotonic()) % 5 == 0:
            self.paint_health()  # the ages move

    def paint_masthead(self) -> None:
        cond, color = self.condition()
        if cond == "RED" and time.time() % 2 < 1:  # blink
            color = blend(RED, INK, .55)
        online, unseen = sum(st.online for st in self.stations), self.unseen
        assets = sum(num(st.data.get("status", {}).get("players")) or 0 for st in self.stations if st.online)
        wide = self.size.width >= 140  # else just the essentials, so nothing gets cut mid-word
        g = Table.grid(expand=True)
        for j in ("left", "center", "right"):
            g.add_column(justify=j, no_wrap=True)
        g.add_row(Text("▲ ", AMBER) + Text("OFFICE OF NAVAL INTELLIGENCE" if wide else "ONI", AMBER)
                  + Text("  ·  SECTION III" if self.size.width >= 160 else "", DIM),
                  Text.assemble((f" CONDITION {cond}" + (f" · ⚑ {unseen}" if unseen else "") + " ",
                                 f"{INK} on {color}"),
                                (f"  {online}/{len(self.stations)} {'STATIONS SECURE' if wide else 'SECURE'}  ·  "
                                 f"{assets} ASSETS", CYAN if online else RED)),
                  Text("TOP SECRET // " if wide else "", RED) + Text(f"OPERATOR {self.by.upper()}  ")
                  + Text(time.strftime("%H:%M:%S"), DIM))
        self.query_one("#masthead", Static).update(g, layout=False)  # one line, always

    def beating(self, st: Station) -> bool:
        """Whether the station's heartbeat is lit this frame, each on a phase of its own. A fleet has none: dozens of
        cards blinking out of step is noise, and each blink is a repaint. Its traces show which ones are alive."""
        if self.animation_level == "none" or self.fleet or not st.online:
            return False
        return (self.frame + st.index * 7) % 20 < 2

    def paint_card(self, st: Station) -> None:
        """Draws the station's card, if anything it shows has changed: most seconds, for most of a fleet, nothing."""
        if st.index not in self.seen:
            st.drawn = ()  # drawn when it scrolls into view
            return
        card, s = self.cards[st.index], st.data.get("status", {})
        state = st.rcon.state if st.rcon else "connecting"
        glyph, color = STATE[state]
        if self.animation_level != "none" and state == "connecting":
            glyph = SPIN[self.frame % 4]
        elif self.beating(st):
            color = "#B4F5D0"  # a heartbeat: brighter for a moment every few seconds
        n, mx = num(s.get("players")), num(s.get("max_players"))
        width = card.wide or 33
        if not st.online:  # why, in words to act on, and when it tries again
            left = st.retry_at - time.monotonic()
            detail = (self.why(st), int(left) + 1 if left > 0 else 0)
        else:
            phase = str(s.get("phase") or "")
            where = [x for x in (st.tag, s.get("map"), s.get("mode")) if x]
            # what's been happening there, in 5 s steps across what the card has room for. A fleet's cards move on
            # a step at a time: redrawn with every event, a few busy servers keep the whole sidebar repainting
            steps = 10 if self.fleet else max(4, width - 13)
            detail = (tuple(where), phase, tuple(st.rate(steps, 5, whole=self.fleet)))
        key = (state, glyph, color, st.label, st.alerts, n, mx, width, self.fleet, detail)
        if key == st.drawn:
            return
        st.drawn = key

        g = Table.grid(expand=True, padding=(0, 1), pad_edge=False)
        g.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
        g.add_column(justify="right", no_wrap=True)
        count = Text(f"{n}/{mx}" if st.online and n is not None else "", AMBER if n else DIM)
        g.add_row(Text(f"{glyph} ", color) + Text(f"{st.index + 1}  {st.label.upper()}", WHITE),
                  Text.assemble((f"⚑ {st.alerts}  ", RED), count) if st.alerts else count)
        if not st.online:
            why, left = detail
            g.add_row(Text("   " + why, color if state != "connecting" else AMBER),
                      Text(f"↻ {left}s", DIM) if left else Text("F3 ↻", DIM) if state == "denied" else Text())
            card.show(g)
            return
        where, phase, trace = detail
        place = Text("   ") + Text(" · ").join(Text(str(x).replace("_", " "), CYAN if x == st.tag else DIM)
                                                for x in where)
        if self.fleet:  # two lines: who and how full, then where and what's been happening
            g.add_row(place, spark(list(trace)))
            card.show(g)
            return
        g.add_row(place, Text("" if phase.lower() in QUIET_PHASES else phase.replace("_", " ").upper(),
                              GREEN if phase == "in_game" else DIM))
        full = n / mx if n is not None and mx else 0
        card.show(Group(g, Text("   ") + gauge(full, 8, RED if full >= 1 else AMBER if full >= .75 else GREEN)
                        + Text("  ") + spark(list(trace))))

    def animate_cards(self) -> None:
        """The 0.15 s frame: spinners, the online heartbeat, retry countdowns, and alert pulses."""
        if not self.is_running:
            return
        self.frame += 1
        now, moving = time.monotonic(), self.animation_level != "none"
        for i, (st, card) in enumerate(zip(self.stations, self.cards)):
            if i not in self.seen:
                continue
            pulsing = moving and st.flash > now
            if pulsing:  # red swelling and fading, about once a second
                card.parent.styles.background = Color.parse(RED).with_alpha(0.1 + 0.3 * abs(self.frame % 6 - 3) / 3)
                self.lit.add(i)
            elif i in self.lit:
                card.parent.styles.clear_rule("background")
                self.lit.discard(i)
                pulsing = True  # one last repaint, back to the stylesheet's colour
            if pulsing:
                card.notify_style_update()  # the card caches its parent's colour; a CSS transition can't do this
                st.drawn = ()  # so it's drawn again over the new colour
            state = st.rcon.state if st.rcon else "connecting"
            if (pulsing or moving and (state == "connecting" or state == "online" and not self.fleet
                                       and (self.frame + st.index * 7) % 20 in (0, 2))
                    or state != "online" and self.frame % 7 == 0):
                self.paint_card(st)

    def paint_uplink(self) -> None:
        t = Text.assemble(("OPERATOR  ", DIM), (self.by, AMBER), "\n")
        for dest, tun in self.tunnels.items():
            glyph, color = {"up": ("◉", GREEN), "opening": ("◌", AMBER)}.get(tun.state, ("○", RED))
            t.append(f"SSH {dest}  ", DIM)
            t.append(f"{glyph} {tun.state.upper()}\n", color)
        t.append("ADDRESSES ", DIM)
        t.append("REDACTED" if self.redact else "VISIBLE", RED if self.redact else GREEN)
        self.query_one("#uplink", Static).update(t)

    def paint_players(self) -> None:
        st, now = self.cur, time.monotonic()
        players = sorted(st.players, key=lambda p: (TEAM_ORDER.get(team_of(p), len(TEAMS)), team_of(p),
                                                    -(num(pick(p, "score")) or 0)))
        self.row_players, rows = {}, []
        for i, p in enumerate(players):
            team, name = team_of(p), str(pick(p, "name", default="?"))
            k, d = pick(p, "kills"), pick(p, "deaths")
            kd = f"{k / max(d, 1):.2f}" if isinstance(k, int) and isinstance(d, int) else "—"
            flags = Text()
            if (spree := st.medals.spree.get(name, 0)) >= SPREE:
                flags.append(f"★{spree} ", GOLD)
            if p.get("admin"):
                flags.append("ADM ", AMBER)
            if p.get("muted"):
                flags.append("MUT ", RED)
            if p.get("alive") is False:
                flags.append("KIA ", DIM)
            key = target_of(p)
            key = key if key not in self.row_players else f"{key}~{i}"
            self.row_players[key] = p
            if now - st.first_seen.setdefault(key, now if st.painted else 0.0) < 12:
                flags.append("NEW", GREEN)
            rows.append((key, [str(pick(p, "number", default="")), Text(name, TEAM_COLOR.get(team, WHITE)),
                               Text(team.upper() or "—", TEAM_COLOR.get(team, DIM)), str(pick(p, "score", default="—")),
                               str(k if k is not None else "—"), str(d if d is not None else "—"), kd,
                               ping_cell(st.pings.get(str(pick(p, "player_id", "id")))), bar(p.get("health")), bar(p.get("shields")), flags]))
        t = self.query_one("#players", Roster)
        t.fill(rows)
        st.painted = st.painted or "players" in st.data  # everyone here at the first look isn't news
        mx = st.data.get("players", {}).get("max_players") or st.data.get("status", {}).get("max_players")
        t.border_title = Text(f"ASSETS IN THEATRE · {len(players)}/{mx or '?'}")
        t.border_subtitle = team_strip(sides(players))
        self.paint_dossier()

    def cur_player(self) -> dict | None:
        return self.row_players.get(self.query_one("#players", Roster).selected)

    def paint_dossier(self) -> None:
        box, p = self.query_one("#dossier", Static), self.cur_player()
        self.query_one("#player-actions").display = self.query_one("#raw-box").display = bool(p)
        if not p:
            if self.cur.online:
                msg = Text.assemble(("NO ASSETS IN THEATRE\n\n", DIM),
                                    ("Nobody is playing on this server right now.\nPlayers appear here as they join.", DIM))
            else:
                msg = Text.assemble(("STATION OFFLINE\n\n", RED), (self.why(self.cur, short=False), WHITE),
                                    ("\n\nIt retries by itself. To retry now: F3, then RECONNECT.", DIM))
            box.update(Text("\n\n") + msg)
            box.styles.text_align = "center"
            return
        box.styles.text_align = "left"
        self.query_one("#pl-mute", Button).label = "UNMUTE" if p.get("muted") else "MUTE"
        team, name = team_of(p), str(pick(p, "name", default="?"))
        color = TEAM_COLOR.get(team, WHITE)
        k, d = pick(p, "kills"), pick(p, "deaths")
        spree, earned = self.cur.medals.spree.get(name, 0), self.cur.medals.earned.get(name)

        def facts(rows: list) -> Table:
            g = Table.grid(padding=(0, 2))
            g.add_column(style=DIM, no_wrap=True)
            g.add_column()
            for a, b in rows:
                g.add_row(a, b if isinstance(b, Text) else Text(str(b)))
            return g
        glyph, head = biosig(str(pick(p, "player_id", "id", default=name)), color), facts([
            ("CALLSIGN", Text(name, color)),
            ("SERVICE TAG", pick(p, "service_tag", "tag", default="—")),
            ("TEAM", Text(team.upper() or "—", TEAM_COLOR.get(team, DIM))),
            ("STATUS", " · ".join(["ALIVE" if p.get("alive") else "KIA" if p.get("alive") is False else "—"]
                                  + ["ADMIN"] * bool(p.get("admin")) + ["MUTED"] * bool(p.get("muted")))),
            ("SCORE", f"{pick(p, 'score', default='—')}    K {k if k is not None else '—'} / D {d if d is not None else '—'}"),
            ("STREAK", Text(f"★ {spree} without dying", GOLD) if spree >= SPREE
             else Text(f"{spree} without dying") if spree else Text("—", DIM))])
        if self.query_one("#dossier-box").content_size.width in range(1, 46):  # narrow: the glyph above the file
            head = Group(glyph, Text(), head)
        else:
            side = Table.grid(padding=(0, 2))
            side.add_row(glyph, head)
            head = side
        rows = [("PLAYER ID", pick(p, "player_id", "id", default="—")),
                ("ADDRESS", Text(redact_addr(pick(p, "address", "ip"), self.redact), RED if self.redact else WHITE)),
                ("JOIN PING", Text.assemble(ping_cell(ms), (" ms", DIM)) if (ms := self.cur.pings.get(
                    str(pick(p, "player_id", "id")))) is not None else Text("—", DIM)),
                ("HEALTH", bar(p.get("health"), 14)), ("SHIELDS", bar(p.get("shields"), 14)),
                ("LAST DEATH", f"{pick(p, 'seconds_since_last_death')}s ago"
                 if isinstance(pick(p, "seconds_since_last_death"), (int, float)) else "—")]
        guests = pick(p, "guest_players", "guests")
        if guests:
            rows.append(("GUESTS", str(len(guests) if isinstance(guests, list) else guests)))
        if earned:
            rows.append(("MEDALS", Text(" · ".join(f"{m} ×{n}" if n > 1 else m for m, n in earned.most_common()), GOLD)))
        box.update(Group(Text("PERSONNEL FILE", AMBER) + Text("  //  CLASSIFIED", RED), Text(),
                         head, Text(), facts(rows)))
        self.query_one("#dossier-raw", Static).update(JSON.from_data(redact_data(p, self.redact)))

    def paint_ops(self) -> None:
        st = self.cur
        s, vote = st.data.get("status", {}), st.data.get("vote", {})
        info = st.rcon.info if st.rcon else {}
        yes_no = lambda v, on="ON", off="OFF": Text(on, GREEN) if v else Text(off, DIM) if v is not None else Text("—", DIM)
        g = Table.grid(padding=(0, 2))
        g.add_column(style=DIM, no_wrap=True)
        g.add_column()
        for a, b in [("STATION", Text(str(s.get("name") or info.get("server") or st.label), AMBER)),
                     ("UPLINK", f"{st.server.where}   v{info.get('version', '?')}"),
                     ("PUBLIC ADDRESS", Text(redact_addr(s.get("address"), self.redact), RED if self.redact else WHITE)),
                     ("PHASE", Text(str(s.get("phase") or "—").replace("_", " ").upper(), CYAN)
                                if str(s.get("phase") or "").lower() not in QUIET_PHASES else Text("—", DIM)),
                     ("MAP", str(s.get("map") or "—")), ("MODE", str(s.get("mode") or "—")),
                     *[("FORGE", self.credit(e)) for e in self.fstate.playing(st.server.where, s.get("map"),
                                                                              s.get("mode"))],
                     ("NEXT", str(s.get("next") or "playlist")),
                     ("PLAYERS", f"{s.get('players', '—')} / {s.get('max_players', '—')}"),
                     ("JOIN PASSWORD", yes_no(s.get("password_required"), "SET", "OPEN")),
                     ("PING LIMIT", f"{s['max_ping']} ms" if s.get("max_ping") else "off"),
                     ("ANTI-CHEAT", f"{s.get('anti_cheat', '—')}" + ("  (active)" if s.get("anti_cheat_active") else "")),
                     ("VPN BLOCK", yes_no(s.get("block_vpn"))), ("TEXT CHAT", yes_no(s.get("text_chat"))),
                     ("VOICE", yes_no(s.get("voice"))), ("HIDDEN", yes_no(s.get("hidden"), "YES", "NO")),
                     ("VOTE", vote_text(vote.get("vote")))]:
            g.add_row(a, b if isinstance(b, Text) else Text(str(b)))
        self.query_one("#sitrep", Static).update(g)
        voting = bool(vote.get("vote")) if "vote" in st.data else None  # unknown until the first fetch: leave on
        for op, *_ in OPS:  # greyed out when it can't work right now, so nobody wonders why nothing happened
            off = (op != "reconnect" and not st.online or op in ("passvote", "cancelvote") and voting is False
                   or op == "startvote" and voting is True)
            self.query_one(f"#op-{op}", Button).disabled = off
        self.paint_theatre()
        here = str(s.get("map") or "").lower().replace(" ", "_")
        pulled = {n: e for lid, e in self.fstate.installed(st.server.where).items() if self.fstate.pulled(lid, e)
                  for n in refs_of(e)}  # withdrawn from Forge, and still in this rotation: flagged
        rows, marked = [], False
        for i, e in enumerate(st.data.get("nextmap", {}).get("rotation") or [], 1):
            mp, md = str(pick(e, "map", "base_map", default="?")), str(pick(e, "mode", "game", default="?"))
            now = not marked and mp.lower().replace(" ", "_") == here
            marked |= now
            style = AMBER if now else ""
            flag = lambda v: Text(v, style) + (Text("  ⚠ WITHDRAWN", RED) if norm(v) in pulled else Text())
            rows.append((str(i), [Text("▶" if now else str(i), style), flag(mp), flag(md)]))
        self.query_one("#rotation", Roster).fill(rows)

    @staticmethod
    def credit(e: dict) -> Text:
        """Who made what's playing: the title, and its author when Forge named one."""
        return (Text(str(e.get("title") or e.get("listing_id")), GOLD)
                + Text(f"  by {e['author']}" if e.get("author") else "", WHITE)
                + Text(f"  v{e['version']}" if e.get("version") else "", DIM))

    def paint_theatre(self) -> None:
        """Who's winning: each team's numbers and score as bars, or a leaderboard when there are no teams."""
        st = self.cur
        players, teams = st.players, sides(st.players)
        width = max(8, self.query_one("#theatre").content_size.width - 26)  # what the names and numbers leave the bar
        g = Table.grid(padding=(0, 2))
        if not players:
            self.query_one("#theatre", Static).update(Text("NO ASSETS IN THEATRE" if st.online else "STATION OFFLINE",
                                                           DIM))
            return
        if teams:
            top = max(max(s, 0) for *_, s in teams) or 1
            for team, n, score in teams:
                c = TEAM_COLOR.get(team, WHITE)
                g.add_row(Text(team.upper(), c), Text(f"{n} ON FIELD", DIM), Text(str(score), c),
                          hbar(max(score, 0) / top, width, c))
        else:  # free for all: the five best
            best = sorted(players, key=lambda p: -(num(pick(p, "score")) or 0))[:5]
            top = max(num(pick(best[0], "score")) or 0, 1)
            for p in best:
                score = num(pick(p, "score")) or 0
                g.add_row(Text(str(pick(p, "name", default="?")), WHITE), Text(""), Text(str(score), AMBER),
                          hbar(max(score, 0) / top, width, AMBER))
        leaders = sorted(players, key=lambda p: -(num(pick(p, "score")) or 0))[:3]
        lines = [Text("TOP GUNS  ", DIM) + Text(" · ").join(
            Text(f"{pick(p, 'name', default='?')} {pick(p, 'score', default=0)}",
                 TEAM_COLOR.get(team_of(p), WHITE)) for p in leaders)]
        sprees = sorted(((n, name) for name, n in st.medals.spree.items() if n >= SPREE), reverse=True)
        if sprees:
            lines.append(Text("ON A SPREE  ", DIM) + Text(" · ").join(Text(f"{name} ★{n}", GOLD)
                                                                    for n, name in sprees[:4]))
        self.query_one("#theatre", Static).update(Group(g, *lines))

    def paint_bans(self) -> None:
        b, v = self.cur.data.get("bans", {}), self.cur.data.get("vpn", {})
        rows, self.ban_rows = [], {}
        for kind, color, lst in (("PLAYER", AMBER, b.get("players")), ("IP", RED, b.get("ips")),
                                 ("DEVICE", CYAN, b.get("devices"))):
            for e in lst or []:
                target = str(pick(e, "id", "player_id", "ip", "range", "address", "device", "fingerprint", default="?"))
                key = f"{kind}:{target}"
                key = key if key not in self.ban_rows else f"{key}~{len(self.ban_rows)}"
                self.ban_rows[key] = target
                shown = redact_addr(target, self.redact) if kind == "IP" else target
                left = until(pick(e, "expires", "until"))
                rows.append((key, [Text(kind, color), shown, str(pick(e, "name", default="")),
                                   str(pick(e, "reason", default="")),
                                   Text(left, RED if left == "PERMANENT" else DIM if left == "expired" else AMBER),
                                   str(pick(e, "banned_by", "by", default=""))]))
        t = self.query_one("#bans", Roster)
        t.fill(rows)
        t.border_title = f"BLACKLIST · {len(rows)}"
        rows, self.vpn_rows = [], {}
        for e in v.get("allowed") or []:
            target = str(pick(e, "target", "id", "player_id", "ip", "range", "address", default=e))
            key = target if target not in self.vpn_rows else f"{target}~{len(self.vpn_rows)}"
            self.vpn_rows[key] = target
            rows.append((key, [redact_text(target, self.redact), str(pick(e, "note", default=""))]))
        t = self.query_one("#vpn", Roster)
        t.fill(rows)
        t.border_title = Text(f"VPN ALLOWANCES · blocking {'ON' if v.get('block_vpn') else 'OFF'}"
                              + (f" · {v['ranges']} ranges" if isinstance(v.get("ranges"), int) else ""))
        for op, *_ in BAN_OPS:
            self.query_one(f"#bl-{op}", Button).disabled = not self.cur.online or (
                op == "unban" and not self.ban_rows or op == "vpnrevoke" and not self.vpn_rows)

    def repaint_feed(self) -> None:
        log = self.query_one("#feed", RichLog)
        log.clear()
        for st, ev in list(self.feed)[-1000:]:
            if self.shown(st, ev):
                log.write(self.feed_line(st, ev))

    def log_cmd(self, renderable) -> None:
        if isinstance(renderable, Text) and scrub(renderable.plain) != renderable.plain:  # a key typed or echoed
            renderable = Text(scrub(renderable.plain), renderable.style)
        self.query_one("#console-log", RichLog).write(renderable)

    # --- input -----------------------------------------------------------------------------------------------
    @on(ListView.Highlighted, "#stations")
    def _station(self, e: ListView.Highlighted) -> None:
        if not self.is_running:  # a late event while quitting: the widgets are already gone
            return
        if e.list_view.index is not None and e.list_view.index != self.sel:
            self.sel = e.list_view.index
            self.cur.alerts = 0  # looked at now
            for t in self.query(Roster):
                if t.row_count:
                    t.move_cursor(row=0)  # another station's lists: start at the top
            self.paint_all(feed=self.local_only)
            self.fetch(self.cur, "status", "players", "nextmap", "vote", "bans", "vpn")

    @on(DataTable.RowHighlighted, "#players")
    def _row(self, _) -> None:
        if not self.is_running:  # a late event while quitting: the widgets are already gone
            return
        self.paint_dossier()

    @on(TabbedContent.TabActivated)
    def _tab(self, e: TabbedContent.TabActivated) -> None:
        if not self.is_running:  # a late event while quitting: the widgets are already gone
            return
        if e.pane.id == "forge" and not self.forge_opened:  # fetched when first wanted, not at every start
            self.forge_opened = True
            self.forge_load()
        if e.pane.id == "health":  # looked at now: the banner has done its job
            self.hide_banner()
            self.paint_health()
        if e.pane.id == "intercepts" and self.unseen:  # every station's alerts are in the feed: seen now
            for st in self.stations:
                st.alerts = 0
            self.paint_all(feed=False)

    @on(TabbedContent.TabActivated)
    def _slide_in(self, e: TabbedContent.TabActivated) -> None:
        """The new tab's content glides in from the right. A slide, not a fade: opacity changes get cached into the
        logs' rendered lines and leave them dim."""
        pane = e.pane
        pane.add_class("-from")  # jumps there: only -slide carries the transition

        def glide() -> None:
            pane.add_class("-slide").remove_class("-from")
            self.set_timer(0.3, lambda: pane.remove_class("-slide"))
        self.call_after_refresh(glide)

    @on(Checkbox.Changed)
    def _check(self, e: Checkbox.Changed) -> None:
        cid = e.checkbox.id or ""
        if cid == "raw":
            self.raw_events = e.value
        elif cid == "f-local":
            self.local_only = e.value
            self.repaint_feed()
        elif cid.startswith("f-"):
            (self.filters.add if e.value else self.filters.discard)(cid[2:])
            self.repaint_feed()

    @on(Input.Submitted, "#say")
    def _say(self, e: Input.Submitted) -> None:
        text, e.input.value = e.value.strip(), ""
        targets = self.stations if text.startswith("@all ") else [self.cur]
        text = text.removeprefix("@all ").strip()
        live = [st for st in targets if st.online]
        if text and live:
            self.send_all(live, "say", text)
        if text and not live:
            self.notify("Not connected: nothing was sent.", severity="warning")

    @on(Input.Submitted, "#cmd")
    def _command(self, e: Input.Submitted) -> None:
        line, e.input.value = e.value.strip(), ""
        if not line:
            return
        if line not in self.history[-1:]:
            self.history.append(line)
        self.hpos, self.draft = len(self.history), ""
        self.query_one("#console-log", RichLog).scroll_end(animate=False)  # you'll want to see the reply
        if line == "clear":
            self.query_one("#console-log", RichLog).clear()
            return
        fleet = line.startswith("@all ")
        try:
            parts = parse_command(line.removeprefix("@all "))
        except ValueError as err:
            self.log_cmd(Text(f"  {err}", RED))
            return
        if not parts:
            return
        if fleet:  # the ones that are down would only fail: say so once, rather than once each
            live = [st for st in self.stations if st.online]
            if len(live) < len(self.stations):
                self.log_cmd(Text(f"  {len(self.stations) - len(live)} station(s) offline: skipped", AMBER))
            self.send_all(live, parts[0], *parts[1:], show_data=True)
        else:
            self.send(self.cur, parts[0], *parts[1:], toast=False, show_data=True)

    @on(Button.Pressed)
    def _button(self, e: Button.Pressed) -> None:
        bid = e.button.id or ""
        if bid == "op-broadcast":
            self.action_broadcast()
        elif bid.startswith("op-"):
            self.op(bid[3:])
        elif bid.startswith("bl-"):
            self.bl(bid[3:])
        elif bid.startswith("pl-"):
            self.action_player(bid[3:])
        elif bid.startswith("fg-"):
            self.action_forge(bid[3:])
        elif bid == "add-server":
            self.action_add_server()

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        w = self.query_one(PANE_FOCUS[tab])
        if w.disabled or not w.display:  # a greyed-out button can't take focus: the pane's first one that can, else the server list
            w = next(iter(self.query_one(f"#{tab}").query("Button:enabled")), self.query_one("#stations"))
        w.focus()  # else focus left in the old pane pulls the tabs back to it

    def action_help(self) -> None:
        if not isinstance(self.screen, Help):
            self.push_screen(Help())

    @work(exclusive=True, group="dialog")
    async def action_add_server(self) -> None:
        if await self.push_screen_wait(Confirm(
                "ADD A SERVER", "Opens the setup screen. Connections close while you're there and reopen when you "
                                "come back.", verb="OPEN SETUP", danger=False)):
            self.exit("setup")


    def action_station(self, i: int) -> None:
        if i < len(self.stations):
            self.query_one("#stations", ListView).index = i

    @work(exclusive=True, group="dialog")
    async def action_goto(self) -> None:
        """Every station in a list to type into, the ones with players first: 1 to 9 only reach so far."""
        def order(st: Station):
            return not st.online, -(num(st.data.get("status", {}).get("players")) or 0), st.index

        def row(st: Station) -> Text:
            s = st.data.get("status", {})
            n, mx = num(s.get("players")), num(s.get("max_players"))
            glyph, color = STATE[st.rcon.state if st.rcon else "connecting"]
            return Text.assemble((f"{glyph} ", color), (f"{st.index + 1:>3}  ", DIM),
                                 (f"{n}/{mx}  " if st.online and n is not None else "", AMBER if n else DIM),
                                 (st.label, WHITE), (f"  {st.tag}" if st.tag else "", CYAN),
                                 (f"  {str(s.get('map') or '').replace('_', ' ')}" if st.online
                                  else f"  {self.why(st)}", DIM))
        i = await self.push_screen_wait(Pick("GO TO STATION", [(row(st), str(st.index))
                                                               for st in sorted(self.stations, key=order)]))
        if i is not None:
            self.action_station(int(i))

    def action_focus_input(self) -> None:
        tabs = self.query_one(TabbedContent)
        if tabs.active == "forge":
            self.query_one("#forge-q").focus()
            return
        if tabs.active != "intercepts":
            tabs.active = "console"
        self.query_one("#say" if tabs.active == "intercepts" else "#cmd").focus()

    def action_refresh(self) -> None:
        if self.query_one(TabbedContent).active == "health":  # every report and service check, now
            for st in self.stations:
                st.check_at = 0.0
            self.service_at = {h: 0.0 for h in self.service_at}
        if self.query_one(TabbedContent).active == "forge":
            self.details.clear()
            self.detail_want = None
            self.forge_load(fresh=True)
        self.fetch(self.cur, "status", "players", "maps", "modes", "nextmap", "vote", "bans", "vpn")
        for st in self.stations:
            self.fetch(st, "status")

    def action_redact(self) -> None:
        self.redact = not self.redact
        if self.redact:
            self.sup.last = ""  # what it last reacted to may have shown an address a moment ago
        self.paint_all()
        self.notify("Addresses redacted." if self.redact else "Addresses visible. Mind your stream.",
                    title="REDACTION", severity="information" if self.redact else "warning")

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        yield SystemCommand("Go to station", "every station, the busiest first  (g)", self.action_goto)
        for i, st in enumerate(self.stations):  # the palette reads titles as markup, and labels come from servers
            yield SystemCommand(escape(f"Station {i + 1}: {st.label}"), escape(st.server.where),
                                lambda i=i: self.action_station(i))
        for op, label, _ in OPS:
            if op == "broadcast":
                yield SystemCommand("Broadcast to stations", "say on this or every station", self.action_broadcast)
            else:
                yield SystemCommand(f"Ops: {label.title()}", f"{op} on the selected station", lambda op=op: self.op(op))
        for op, label, _ in BAN_OPS:
            yield SystemCommand(f"Blacklist: {label.split('  ')[0].title()}", op, lambda op=op: self.bl(op))
        yield SystemCommand("Toggle address redaction", "hide or show player IPs", self.action_redact)
        yield SystemCommand("Boot sequence: " + ("quick after the first run" if self.prefs.full_intro
                                                 else "full every time"),
                            "now: full the first time, quick after" if not self.prefs.full_intro
                            else "now: the full sequence plays every time", self.action_full_intro)
        yield SystemCommand("Health: read the reports now", "crashes, memory and the workaround service  (F7, Ctrl+R)",
                            lambda: (setattr(self, "service_at", {h: 0.0 for h in self.service_at}),
                                     [setattr(st, "check_at", 0.0) for st in self.stations]))
        yield SystemCommand("Help: field manual", "what everything does, in plain words  (?)", self.action_help)
        yield SystemCommand("Add a server", "open the setup screen", self.action_add_server)
        yield SystemCommand("Forge: Load API key", "your own ReclaimerForge key, for F6", lambda: self.action_forge("key"))
        yield SystemCommand("Forge: Refresh catalog", "fetch the catalog again", lambda: self.forge_load(fresh=True))

    def action_full_intro(self) -> None:
        """Switch between the full boot sequence every time and only the first time."""
        self.prefs.full_intro = not self.prefs.full_intro
        self.prefs.save()
        self.notify("The full boot sequence plays every time you start." if self.prefs.full_intro else
                    "The full boot sequence plays on the first run only; after that the boot is quick.",
                    title="BOOT SEQUENCE")

    # --- actions with dialogs (workers, so they can await the dialog) -----------------------------------------
    @work(exclusive=True, group="dialog")
    async def action_broadcast(self) -> None:
        f = await self.push_screen_wait(Form("BROADCAST", [("text", "Message ([Server] line in chat)", ""),
                                                           ("scope", "Transmit to", [("Every station", "all"),
                                                                                     (f"{self.cur.label} only", "one")])],
                                             verb="TRANSMIT", required=("text",)))
        if f:
            targets = [st for st in (self.stations if f["scope"] == "all" else [self.cur]) if st.online]
            if targets:
                self.send_all(targets, "say", f["text"])
            if len(targets) < 2:  # a fleet's tally says how it went
                self.notify(f"Transmitted to {targets[0].label}." if targets else "Not connected: nothing was sent.",
                            title="BROADCAST", severity="information" if targets else "warning")

    @work(exclusive=True, group="dialog")
    async def action_player(self, what: str) -> None:
        st, p = self.cur, self.cur_player()
        if not p:
            self.notify("No asset selected.", severity="warning")
            return
        t, name = target_of(p), str(pick(p, "name", default="?"))
        opt = lambda v: [v] if v else []
        if what == "copy":
            self.copy_to_clipboard(t)
            self.notify(t, title="PLAYER ID COPIED")
        elif what == "tell":
            f = await self.push_screen_wait(Form(f"TELL · {name}", [("text", "Message (only they see it)", "")],
                                                 verb="SEND", required=("text",)))
            if f:
                self.send(st, "tell", t, f["text"])
        elif what == "kick":
            f = await self.push_screen_wait(Form(f"KICK · {name}", [("reason", "Reason (they read it)", "")],
                                                 verb="KICK", danger=True,
                                                 note="Repeat kicks block rejoining for 10, 30, 60, then 120 minutes."))
            if f is not None:
                self.send(st, "kick", t, *opt(f["reason"]), then=("players", "status"))
        elif what == "ban":
            f = await self.push_screen_wait(Form(f"BAN · {name}", [("time", "Duration", BAN_TIMES),
                                                                   ("reason", "Reason", "")], verb="BAN", danger=True,
                                                 note="Bans their player ID, address and device together, on every "
                                                      "server sharing this ban list."))
            if f is not None:
                self.send(st, "ban", t, *opt(f["time"]), *opt(f["reason"]), then=("players", "status", "bans"))
        elif what == "mute":
            if p.get("muted"):
                self.send(st, "unmute", t, then=("players",))
                return
            f = await self.push_screen_wait(Form(f"MUTE · {name}", [("time", "Duration", MUTE_TIMES),
                                                                    ("reason", "Reason", "")], verb="MUTE",
                                                 note="Silences their text and voice chat. Rejoining keeps it."))
            if f is not None:
                self.send(st, "mute", t, *opt(f["time"]), *opt(f["reason"]), then=("players",))
        elif what == "team":
            team = await self.push_screen_wait(Pick(f"MOVE {name} TO", [(Text(c.upper(), TEAM_COLOR[c]), c)
                                                                         for c in TEAMS]))
            if team:
                self.send(st, "team", t, team, then=("players",))
        elif what == "vpnallow":
            f = await self.push_screen_wait(Form(f"VPN ALLOW · {name}", [("note", "Note (why)", "")], verb="ALLOW",
                                                 note="Lets them join through a VPN on every server sharing the ban list."))
            if f is not None:
                self.send(st, "vpnallow", t, *opt(f["note"]), then=("vpn",))

    def entries(self, st: Station, what: str) -> list:
        return [(Text.assemble((str(pick(e, "name", default="?")), WHITE), (f"  {e.get('kind', '')}", DIM),
                               (f"  {e.get('reference', '')}", DIM)), str(pick(e, "reference", "name")))
                for e in st.data.get(what, {}).get("entries") or []]

    @work(exclusive=True, group="dialog")
    async def op(self, name: str) -> None:
        st = self.cur
        s = st.data.get("status", {})
        if name == "reconnect":
            if st.rcon.state == "denied":
                f = await self.push_screen_wait(Form(
                    "RETRY SIGN-IN", [("pw", "RCON password", st.rcon.password)], verb="RETRY", required=("pw",),
                    secret=("pw",), note="This station refused the password. Five wrong passwords in ten minutes "
                                         "lock your address out for up to ten minutes."))
                if not f:
                    return
                st.rcon.password = st.server.password = f["pw"]
            self.reconnect(st)
            return
        if not st.online:
            self.notify(f"{st.label} is not connected.", severity="warning")
            return
        if name in ("load", "map", "mode", "nextmap"):
            mp = md = ""
            if name != "mode":
                maps = self.entries(st, "maps")
                if not maps:
                    self.notify("Map list not loaded yet.", severity="warning")
                    return
                mp = await self.push_screen_wait(Pick("SELECT MAP", maps))
                if not mp:
                    return
            if name != "map":
                modes = self.entries(st, "modes")
                if name == "nextmap":
                    modes = [(Text("(keep the current rules)", DIM), "")] + modes
                md = await self.push_screen_wait(Pick("SELECT MODE", modes))
                if md is None or (name != "nextmap" and not md):
                    return
            args = [a for a in (mp, md) if a]
            if name != "nextmap" and not await self.push_screen_wait(Confirm(
                    f"{name.upper()}  {' / '.join(args)}", "Ends the current game for everyone on this station; "
                                                           "the new one starts in the next lobby.")):
                return
            self.send(st, name, *args, then=("status", "nextmap"))
        elif name in ("endround", "endgame", "shuffle"):
            body = {"endround": "Ends the round as if its time ran out.",
                    "endgame": "Ends the game with the scores as they stand.",
                    "shuffle": "Shuffles players across the teams that have players."}[name]
            if await self.push_screen_wait(Confirm(name.upper(), body, danger=name != "shuffle")):
                self.send(st, name, then=("status", "players"))
        elif name == "teamcount":
            n = await self.push_screen_wait(Pick("SPREAD PLAYERS OVER", [(f"{i} teams", str(i)) for i in range(2, 9)]))
            if n:
                self.send(st, "teamcount", n, then=("players",))
        elif name == "startvote":
            subjects = st.data.get("vote", {}).get("subjects") or ["endround", "endgame", "shuffle", "kick", "playlist"]
            subj = await self.push_screen_wait(Pick("CALL A VOTE", [(x.upper(), x) for x in subjects]))
            if not subj:
                return
            args = [subj]
            if subj == "kick":
                who_ = await self.push_screen_wait(Pick("VOTE TO KICK", [(str(pick(p, "name")), target_of(p))
                                                                         for p in st.players]))
                if not who_:
                    return
                args.append(who_)
            self.send(st, "startvote", *args, then=("vote",))
        elif name in ("passvote", "cancelvote"):
            self.send(st, name, then=("vote",))
        elif name == "servername":
            f = await self.push_screen_wait(Form("RENAME STATION", [("name", "Server name (until restart)",
                                                                     s.get("name", ""))], verb="RENAME", required=("name",)))
            if f:
                self.send(st, "servername", f["name"], then=("status",))
        elif name == "password":
            f = await self.push_screen_wait(Form("JOIN PASSWORD", [("pw", "Password players need (empty = open server)", "")],
                                                 verb="SET", note="Lasts until restart. Doesn't touch the RCON password."))
            if f is not None:
                self.send(st, "password", f["pw"], then=("status",))
        elif name == "maxping":
            f = await self.push_screen_wait(Form("PING LIMIT", [("ms", "Highest join ping in ms, up to 1000 (off = none)",
                                                                 s.get("max_ping") or "off")], verb="SET", required=("ms",)))
            if f:
                self.send(st, "maxping", f["ms"], then=("status",))

    @work(exclusive=True, group="dialog")
    async def bl(self, name: str) -> None:
        st = self.cur
        if not st.online:
            self.notify(f"{st.label} is not connected.", severity="warning")
            return
        opt = lambda v: [v] if v else []
        if name == "newban":
            f = await self.push_screen_wait(Form("BAN BY ID OR ADDRESS", [
                ("target", "Player ID, IP address or range (CIDR)", ""), ("time", "Duration", BAN_TIMES),
                ("reason", "Reason", "")], verb="BAN", danger=True, required=("target",)))
            if f:
                self.send(st, "ban", f["target"], *opt(f["time"]), *opt(f["reason"]), then=("bans",))
        elif name in ("unban", "vpnrevoke"):
            sel, rows = ("#bans", self.ban_rows) if name == "unban" else ("#vpn", self.vpn_rows)
            target = rows.get(self.query_one(sel, Roster).selected)
            if not target:
                self.notify("Select an entry first.", severity="warning")
                return
            if await self.push_screen_wait(Confirm(f"{name.upper()}  {redact_text(target, self.redact)}",
                                                   "Lifts it together with its linked entries, on every server "
                                                   "sharing the ban list.", verb=name.upper(), danger=False)):
                self.send(st, name, target, then=("bans", "vpn"))
        elif name == "vpnallow":
            f = await self.push_screen_wait(Form("VPN ALLOW", [("target", "Player ID, IP address or range", ""),
                                                               ("note", "Note", "")], verb="ALLOW", required=("target",)))
            if f:
                self.send(st, "vpnallow", f["target"], *opt(f["note"]), then=("vpn",))
        elif name == "vpncheck":
            f = await self.push_screen_wait(Form("CHECK AN ADDRESS", [("ip", "IP address", "")], verb="CHECK",
                                                 required=("ip",)))
            if f:
                self.send(st, "vpn", f["ip"])

    # --- forge: the ReclaimerForge catalog --------------------------------------------------------------------
    def on_unmount(self) -> None:
        if self.forge:
            self.forge.stop.set()  # a download in a thread gives up at its next chunk

    def forge_connect(self) -> None:
        """The client for the key in hand, if there is one. A new key starts the catalog over."""
        s, self.forge = self.fsetup, None
        if s.key:
            try:
                self.forge = ForgeClient(s.key, s.url, s.cache_dir)
            except ValueError as e:
                s.error = str(e)
        self.listings, self.details, self.forge_next, self.forge_note, self.forge_stale = [], {}, None, "", 0
        self.detail_want, self.featured = None, {}

    def forge_load(self, more: bool = False, fresh: bool = False) -> None:
        if self.forge and (self.forge_next or not more):
            self.run_worker(self._forge_load(more, fresh), group="forge", exclusive=True, exit_on_error=False)

    async def _forge_load(self, more: bool, fresh: bool) -> None:
        sort, window = self.query_one("#forge-sort", Select).value, self.query_one("#forge-window", Select).value
        if sort == "installed":  # nothing to ask Forge: it's what forge-state.json says is here
            self.forge_next, self.forge_note, self.forge_stale = None, "", 0
            self.paint_forge()
            return
        if sort == "favourites":  # one unpaginated feed, not searched: the box narrows what's here as it's typed
            await self._forge_favourites(fresh)
            return
        self.featured = {}
        self.forge_note = "Fetching more of the catalog…" if more else "Fetching the catalog…"
        self.paint_forge()
        try:
            items, nxt, reply = await self.forge.listings(sort, window, self.query_one("#forge-q", Input).value,
                                                          more=self.forge_next if more else None, fresh=fresh)
        except ForgeError as e:
            self.forge_note = e.text
            if not more:
                self.listings = []
        else:
            had = {listing_id(x) for x in self.listings} if more else set()
            self.listings = (self.listings if more else []) + [x for x in items if listing_id(x) not in had]
            self.forge_next, self.forge_note, self.forge_stale = nxt, "", reply.get("_stale", 0)
        if self.is_running:
            self.paint_forge()

    async def _forge_favourites(self, fresh: bool) -> None:
        self.forge_next, self.forge_note = None, "Fetching the favourites…"
        self.paint_forge()
        try:
            collections, reply = await self.forge.favourites(fresh=fresh)
        except ForgeError as e:
            self.forge_note, self.listings, self.featured = e.text, [], {}
        else:
            self.listings, self.featured = favourite_listings(collections)
            self.forge_note = "" if self.listings else "ReclaimerForge isn't featuring anything right now."
            self.forge_stale = reply.get("_stale", 0)
        if self.is_running:
            self.paint_forge()

    async def _forge_detail(self, lid: str) -> None:
        try:
            d = await self.forge.listing(lid)
        except ForgeError as e:
            self.details[lid] = {**self.listing_rows.get(lid, {}), "_error": e.text}
        else:
            self.details[lid] = {**self.listing_rows.get(lid, {}), **d}
        if self.is_running and self.query_one("#listings", Roster).selected == lid:
            self.paint_listing()

    def forge_mark(self, x: dict) -> Text:
        """The catalog's first column: where this listing stands on the selected server. ◉ installed, ▲ a newer
        version is out, ⚠ withdrawn from Forge."""
        lid = listing_id(x)
        have = self.fstate.installed(self.cur.server.where).get(lid)
        if not have:
            return Text("")
        if self.fstate.pulled(lid, have):
            return Text("⚠", RED)
        latest = version_id(latest_of(x) or {}) or version_id(self.fstate.latest(lid) or {})
        return Text("▲", AMBER) if latest and latest != have.get("version_id") else Text("◉", GREEN)

    def paint_forge(self) -> None:
        """The catalog table, the readout on its border, and the file of the selected listing. What's typed in the
        search box narrows what's loaded at once; Enter asks Forge."""
        query = self.query_one("#forge-q", Input).value.strip().lower()
        sort, window = self.query_one("#forge-sort", Select).value, self.query_one("#forge-window", Select).value
        version = self.cur.rcon.info.get("version") if self.cur.rcon else None
        if sort == "installed":
            self.listings = self.installed_listings()
        rows, self.listing_rows = [], {}
        for x in self.listings:
            lid, kind, author, recent = listing_id(x), kind_of(x), author_of(x), recent_of(x, window)
            if not lid or lid in self.listing_rows or query and not any(
                    query in t.lower() for t in (title_of(x), author, kind)):
                continue
            self.listing_rows[lid] = x
            rows.append((lid, [self.forge_mark(x), Text(kind.upper() or "—", KIND_COLOR.get(kind, DIM)),
                               Text(title_of(x), WHITE), Text(author or "—", WHITE if author else DIM), rating_text(x),
                               str(recent) if recent is not None else "—", fit_text(x, version)]))
        t = self.query_one("#listings", Roster)
        t.fill(rows)
        t.border_title = Text(f"INSTALLED ON {self.cur.label.upper()} · {len(rows)}" if sort == "installed" else
                              f"FORGE CATALOG · {len(rows)}{'+' if self.forge_next else ''} · {str(sort).upper()}"
                              + (f" {window}" if sort in WINDOWED else ""))
        sub = [SITE]
        if self.forge and self.forge_stale:
            sub.append(f"OFFLINE COPY, {self.forge_stale // 60}m OLD")
        if self.forge and str(self.forge.quota):
            sub.append(str(self.forge.quota))
        t.border_subtitle = "  ·  ".join(sub)
        self.query_one("#forge-window", Select).disabled = sort not in WINDOWED
        self.query_one("#fg-more", Button).disabled = not (self.forge and self.forge_next)
        self.paint_listing()

    def cur_listing(self) -> dict | None:
        lid = self.query_one("#listings", Roster).selected
        return (self.details.get(lid) or self.listing_rows.get(lid)) if lid else None

    def no_key_text(self) -> Text:
        t = Text.assemble(("FORGE UPLINK  //  ", AMBER), ("NO KEY\n\n", RED),
                          ("ReclaimerForge is the community catalog of forged maps, gametypes and playlists. Browsing "
                           "and installing from it takes your own API key.\n\n", WHITE),
                          ("1  ", AMBER), ("Your ReclaimerForge account needs the Developer role (an Owner gives it) "
                                          "and a verified email.\n", WHITE),
                          ("2  ", AMBER), ("In Developer tools on reclaimerforge.net, make a key for oni-rcon alone, "
                                          "with the catalog:read and assets:download scopes and nothing more. It's "
                                          "shown when you make it, so save it then.\n", WHITE),
                          ("3  ", AMBER), ("Press k, or KEY below, to load it for this session. Or set "
                                          "$ONI_RCON_FORGE_KEY, or forge_api_key_env in the config file, and start "
                                          "oni-rcon again.\n", WHITE))
        if self.fsetup.error:
            t.append(f"\n{self.fsetup.error}", RED)
        return t

    def paint_listing(self) -> None:
        box, x, versions = self.query_one("#listing", Static), self.cur_listing(), self.query_one("#versions", Roster)
        self.query_one("#forge-credit", Static).update(
            Text.assemble(("INTELLIGENCE SOURCE  ", DIM), (SITE, AMBER), (f"\n{CREDIT}", DIM)))
        self.query_one("#listing-box").border_subtitle = (f"KEY FROM {self.fsetup.source.upper()}"
                                                          if self.forge and self.fsetup.source else "")
        versions.display = self.query_one("#forge-raw-box").display = bool(self.forge and x)
        self.query_one("#fg-install").display = self.query_one("#fg-load").display = bool(self.forge and x)
        self.query_one("#fg-ack").display = False
        if not self.forge:
            box.update(self.no_key_text())
            return
        if not x:
            words = self.forge_note or ("NO LISTINGS MATCH" if self.listings else "Nothing in the catalog yet.")
            box.update(Text("\n" + words, WHITE if self.forge_note else DIM))
            return
        lid = listing_id(x)
        if lid not in self.details and lid in self.listing_rows and self.detail_want != lid:
            self.detail_want = lid  # its versions come with the listing itself: asked for once per look
            self.run_worker(self._forge_detail(lid), group="forge-detail", exclusive=True, exit_on_error=False)
        window = self.query_one("#forge-window", Select).value
        kind, owner, total = kind_of(x), owner_of(x), num(pick(x, "downloads", "download_count", "total_downloads"))
        recent, version, spec = recent_of(x, window), self.cur.rcon.info.get("version") if self.cur.rcon else None, \
            compat_of(x)
        ok = compatible(spec, version)
        base = pick(x, "base_map", "map", "base_mode", "base_gametype")
        base_word = "from" if kind == "gametype" else "on"
        withdrawn = str(pick(x, "status", "state", default="")).lower()
        views, began = views_of(x), history_since(x, window)
        rows = [("TITLE", Text(title_of(x), AMBER)),
                ("TYPE", Text(kind.upper() or "—", KIND_COLOR.get(kind, DIM))
                 + Text(f"  {base_word} {str(base).replace('_', ' ')}" if base else "", DIM)),
                ("AUTHOR" if len(authors_of(x)) < 2 else "AUTHORS", Text(author_of(x) or "—", WHITE)
                 + Text(f"  {owner}" if owner else "", DIM)),
                ("RATING", rating_text(x)),
                ("DOWNLOADS", Text(f"{total:,}" if total is not None else "—", WHITE)
                 + Text(f"  ·  {recent} in {window if window in WINDOWS else 'recent days'}"
                        if recent is not None else "", DIM)
                 + Text(f"  (counted from {day(began.isoformat())}, when Forge began keeping history)"
                        if recent is not None and began else "", DIM))]
        if views is not None:
            rows.append(("VIEWS", Text(f"{views:,}", WHITE)))
        if note := self.featured.get(lid):
            rows.append(("FEATURED", Text(note, GOLD)))
        rows += [("RUNS ON", Text(compat_words(spec), WHITE) + (Text(
                     f"   {'✓' if ok else '✕' if ok is False else '?'} v{version} on {self.cur.label}",
                     GREEN if ok else RED if ok is False else DIM) if version else Text())),
                 ("STATUS", self.status_words(lid, withdrawn)),
                 ("UPDATED", day(pick(x, "updated_at", "updated"))),
                 ("LATEST", version_label(latest_of(x)) if latest_of(x) else "—"),
                 ("ON THIS SERVER", self.installed_words(x))]
        g = Table.grid(padding=(0, 2))
        g.add_column(style=DIM, no_wrap=True)
        g.add_column()
        for a, b in rows:
            g.add_row(a, b if isinstance(b, Text) else Text(str(b)))
        parts = [Text("CATALOG ENTRY", AMBER) + Text(f"  //  {lid}", DIM), Text(), g]
        if blurb := blurb_of(x):  # Markdown on the website; plain text here, so nothing in it does anything
            parts += [Text(), Text(shorten(blurb, 700), WHITE)]
        if x.get("_error"):
            parts += [Text(), Text(x["_error"], RED)]
        box.update(Group(*parts))
        have = self.fstate.installed(self.cur.server.where).get(lid)
        self.query_one("#fg-install", Button).disabled = is_withdrawn(x) or self.fstate.blocks(lid)
        self.query_one("#fg-ack").display = lid in self.fstate.alarms()
        self.query_one("#fg-load", Button).disabled = not have or kind_of(have) == "playlist"
        vs = versions_of(x)
        versions.display = bool(vs)
        versions.fill([(version_id(v), [self.version_mark(x, v), Text(version_label(v), WHITE if usable(v) else DIM),
                                        day(pick(v, "created_at", "published_at", "released_at")),
                                        Text(shorten(notes_of(v), 90), DIM)])
                       for v in vs])
        if vs and self.versions_for != lid:  # a listing newly shown: its cursor starts on the newest to install
            self.versions_for = lid
            if (first := version_id(latest_of(x) or {})) in versions.ids:
                versions.move_cursor(row=versions.ids.index(first))
        self.query_one("#listing-raw", Static).update(JSON.from_data(scrub_data(x)))

    def version_mark(self, x: dict, v: dict) -> Text:
        """◉ the version installed here, ✕ one its author has withdrawn (it can't be installed), ⚠ when that is the
        one installed here."""
        have = self.fstate.installed(self.cur.server.where).get(listing_id(x))
        here = bool(have) and have.get("version_id") == version_id(v)
        if not usable(v):
            return Text("⚠" if here else "✕", RED)
        return Text("◉", GREEN) if here else Text("")

    def status_words(self, lid: str, status: str) -> Text:
        """What the listing says about itself, and what oni-rcon has heard about the one installed here."""
        words = Text(status.upper() or "—", RED if status in WITHDRAWN else GREEN if status else DIM)
        if self.fstate.blocks(lid) and not status:
            words = Text("WITHDRAWN", RED)
        elif self.fstate.is_withdrawn(lid) and not self.fstate.blocks(lid):
            words += Text("  ·  installed version withdrawn", AMBER)
        return words

    @on(DataTable.RowHighlighted, "#listings")
    def _listing_row(self, _) -> None:
        if not self.is_running:  # a late event while quitting: the widgets are already gone
            return
        self.paint_listing()

    @on(Select.Changed, "#forge-sort, #forge-window")
    def _forge_order(self, _) -> None:
        if not self.is_running:  # a late event while quitting: the widgets are already gone
            return
        if self.forge_opened:
            self.forge_load()
        self.paint_forge()

    @on(Input.Changed, "#forge-q")
    def _forge_filter(self, _) -> None:
        if not self.is_running:  # a late event while quitting: the widgets are already gone
            return
        self.paint_forge()

    @on(Input.Submitted, "#forge-q")
    def _forge_search(self, e: Input.Submitted) -> None:
        e.stop()
        if not self.is_running:  # a late event while quitting: the widgets are already gone
            return
        self.forge_opened = True
        self.forge_load()
        self.query_one("#listings").focus()

    @work(exclusive=True, group="dialog")
    async def action_forge(self, what: str) -> None:
        if what == "key":
            await self.forge_key()
        elif what == "ack":
            await self.forge_ack()
        elif what in ("sort", "window"):
            sel = self.query_one(f"#forge-{what}", Select)
            if not sel.disabled:
                opts = VIEWS if what == "sort" else WINDOWS
                sel.value = opts[(opts.index(sel.value) + 1) % len(opts)]
        elif what == "install":
            await self.forge_install()
        elif what == "load":
            await self.forge_play()
        elif what == "more":
            self.forge_load(more=True)
        elif what == "copy":
            if x := self.cur_listing():
                self.copy_to_clipboard(listing_id(x))
                self.notify(listing_id(x), title="LISTING ID COPIED")

    async def forge_key(self) -> None:
        cfg = self.fsetup.config
        fields = [("key", "Your ReclaimerForge API key", "")]
        if cfg and cfg.is_file():  # remembering is a choice to make, never the default
            fields.append(("remember", "Remember it", [("No: for this session only", "no"),
                                                       (f"Yes: save it in {cfg.name}, readable only by you", "yes")]))
        f = await self.push_screen_wait(Form(
            "FORGE API KEY", fields, verb="LOAD", required=("key",), secret=("key",),
            note="Make one in Developer tools on reclaimerforge.net, for oni-rcon alone, with the catalog:read and "
                 "assets:download scopes and nothing more. oni-rcon only ever sends it to reclaimerforge.net, and "
                 "never shows it."))
        if not f:
            return
        # out of the form's result at once: a crash report lists what's in scope, and a Secret never prints
        self.fsetup.key, self.fsetup.source, self.fsetup.error = Secret(f.pop("key")), "typed this session", ""
        if f.get("remember") == "yes":
            try:
                remember_forge_key(cfg, self.fsetup.key.value)
                self.fsetup.source = f"{cfg.name}"
            except Exception as e:  # whatever it was, a toast: never a crash report with the key in its frames
                self.notify(f"Couldn't save it ({type(e).__name__}). It's loaded for this session.",
                            title="FORGE KEY", severity="warning")
        self.forge_connect()
        self.forge_opened = True
        self.forge_load()
        self.notify("Loaded. It goes to reclaimerforge.net only.", title="FORGE KEY")

    # --- forge: installing ------------------------------------------------------------------------------------
    def installed_words(self, x: dict) -> Text:
        st = self.cur
        have = self.fstate.installed(st.server.where).get(listing_id(x))
        if have:
            return Text(f"v{have.get('version', '?')}", GREEN) + Text(f"  installed {day(have.get('installed_at'))}",
                                                                      DIM)
        where = target_for(st.server)
        return Text("not installed", DIM) if not isinstance(where, str) else Text("can't install here: i says why",
                                                                                   AMBER)

    def sharing(self, st: Station) -> list[Station]:
        """The stations that load content from the same folder as this one: an install there is theirs too."""
        here = place(st.server)
        return [o for o in self.stations if o.server.content_dir and not o.server.url and place(o.server) == here]

    def listed(self, st: Station, entry: dict) -> tuple[str, str] | None:
        """Where the server lists an installed listing: ("maps" or "modes", the reference to load it by)."""
        names = refs_of(entry)
        for what in ("maps", "modes"):
            for e in st.data.get(what, {}).get("entries") or []:
                if any(norm(v) in names for v in (pick(e, "reference"), pick(e, "name")) if v):
                    return what, str(pick(e, "reference", "name"))
        return None

    def selected_version(self, x: dict) -> dict | None:
        vid = self.query_one("#versions", Roster).selected
        return next((v for v in versions_of(x) if version_id(v) == vid), None) or latest_of(x)

    async def forge_install(self) -> None:
        st, x = self.cur, self.cur_listing()
        if not (self.forge and x):
            self.notify("Pick a listing first." if self.forge else "Load your Forge key first (k).", severity="warning")
            return
        title, lid = title_of(x), listing_id(x)
        target = target_for(st.server)
        if isinstance(target, str):
            self.notify(target, title=f"{st.label} · CAN'T INSTALL HERE", severity="warning", timeout=15)
            return
        if is_withdrawn(x):
            self.notify("It's been withdrawn from ReclaimerForge, so it isn't installed anywhere new.",
                        title=f"FORGE · {title}", severity="warning")
            return
        v = self.selected_version(x)
        if not v or not version_id(v):
            self.notify("It has no version to install yet.", title=f"FORGE · {title}", severity="warning")
            return
        if not usable(v):
            self.notify("Its author has withdrawn that version, so it isn't installed. Pick another.",
                        title=f"FORGE · {title}", severity="warning")
            return
        vid, label = version_id(v), version_label(v)
        try:
            manifest = await self.forge.manifest(lid, vid)
            files = plan(manifest)
            there = await target.existing([f.path for f in files])
        except ForgeError as e:
            self.notify(e.text, title=f"FORGE · {e.short}", severity="error", timeout=15)
            return
        except (InstallError, OSError) as e:
            self.notify(explain(str(e)), title=f"FORGE · {title}", severity="error", timeout=15)
            return
        have = self.fstate.installed(st.server.where).get(lid)
        lines = [f"{len(files)} file{'s' * (len(files) != 1)}, {size_words(sum(f.size for f in files))}, into "
                 f"{st.server.content_dir} on {target.where}."]
        if have and have.get("version_id") != vid:
            lines.append(f"Replaces v{have.get('version', '?')}, installed {day(have.get('installed_at'))}.")
        elif have:
            lines.append("This version is installed already: it's copied again.")
        if there:
            lines.append("Already there, and replaced: " + ", ".join(sorted(there)) + ".")
        if others := [o.label for o in self.sharing(st) if o is not st]:
            lines.append(f"{', '.join(others)} load{'s' * (len(others) == 1)} from the same folder, so it's theirs too.")
        lines.append("Every file is checked against the manifest's size and SHA-256 before it's copied and again "
                     "once it's there. Loading it is a separate step.")
        replacing = bool(there or have)
        if not await self.push_screen_wait(Confirm(f"INSTALL  {title} v{label}", "\n\n".join(lines),
                                                   verb="REPLACE" if replacing else "INSTALL", danger=replacing)):
            return
        # a worker of its own: opening another dialog meanwhile must not cancel a copy halfway
        self.run_worker(self.forge_put(st, x, v, manifest, files, target), group="install", exit_on_error=False)

    async def forge_put(self, st: Station, x: dict, v: dict, manifest: dict, files: list, target) -> None:
        title, lid, vid, label = title_of(x), listing_id(x), version_id(v), version_label(v)
        stamp = Text(time.strftime("%H:%M:%S "), DIM) + Text(f"{st.label} ", CYAN)
        self.log_cmd(stamp + Text(f"» forge install {title} v{label}  ({lid} {vid})", WHITE))
        self.notify(f"Downloading {len(files)} file{'s' * (len(files) != 1)} and checking each one…",
                    title=f"FORGE · {title}")
        try:
            local = [await self.forge.download(f.url, f.size, f.sha256) for f in files]  # all checked, then copied
            for f, path in zip(files, local):
                await target.put(path, f)
        except (ForgeError, Unverified, InstallError, OSError) as e:
            if not self.is_running:
                return
            words = e.text if isinstance(e, ForgeError) else explain(str(e))
            self.log_cmd(Text("  ✕ " + words, RED))
            self.notify(words, title="FORGE · NOT INSTALLED", severity="error", timeout=20)
            return
        entry = {"listing_id": lid, "version_id": vid, "version": label, "title": title, "kind": kind_of(x)
                 or kind_of(manifest), "author": author_of(x), "owner_id": owner_of(x),
                 "reference": str(pick(manifest, "reference", "map_reference", "name", default="")),
                 "files": [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in files],
                 "content_dir": st.server.content_dir, "installed_at": utc_iso()}
        sharing = self.sharing(st)
        self.fstate.record([o.server.where for o in sharing], entry)
        if not self.is_running:
            return
        self.log_cmd(Text(f"  ✓ {len(files)} file{'s' * (len(files) != 1)} checked and in place", GREEN))
        self.paint_masthead()
        self.paint_ops()
        self.paint_forge()
        for o in sharing:  # does the server list it yet?
            if o.online:
                await self._fetch(o, ("maps", "modes"))
        if kind_of(entry) == "playlist":
            after = "Point the server's playlist setting at it to use it: oni-rcon doesn't edit dedicated.toml."
        elif self.listed(st, entry):
            after = "The server lists it now: l loads it."
        else:
            after = "The server doesn't list it yet: it may need a restart to pick new content up."
        self.notify(f"{title} v{label} is on {st.label}. {after}", title="FORGE · INSTALLED", timeout=15)

    async def forge_play(self) -> None:
        """Load an installed map or gametype now: the same as LOAD MAP+MODE on F3, with this half filled in."""
        st, x = self.cur, self.cur_listing()
        entry = self.fstate.installed(st.server.where).get(listing_id(x)) if x else None
        if not entry:
            self.notify("Install it on this server first (i).", severity="warning")
            return
        if kind_of(entry) == "playlist":
            self.notify("A playlist isn't loaded from here: point the server's playlist setting at it.",
                        severity="warning")
            return
        if not st.online:
            self.notify(f"{st.label} is not connected.", severity="warning")
            return
        await self._fetch(st, ("maps", "modes"))
        found = self.listed(st, entry)
        if not found:
            self.notify("The server doesn't list it yet. It may need a restart to pick new content up, which oni-rcon "
                        "can't do for you.", title=f"LOAD NOW · {entry.get('title', '')}", severity="warning",
                        timeout=15)
            return
        what, ref = found
        title = entry.get("title") or ref
        if what == "maps":
            other = await self.push_screen_wait(Pick(f"{title} · SELECT MODE", self.entries(st, "modes")))
            args = [ref, other]
        else:
            other = await self.push_screen_wait(Pick(f"{title} · SELECT MAP", self.entries(st, "maps")))
            args = [other, ref]
        if not other:
            return
        if await self.push_screen_wait(Confirm(f"LOAD  {' / '.join(args)}", "Ends the current game for everyone on "
                                               "this station; the new one starts in the next lobby.")):
            self.send(st, "load", *args, then=("status", "nextmap"))

    # --- forge: the update and withdrawal watcher -------------------------------------------------------------
    def installed_listings(self) -> list[dict]:
        """What forge-state.json says is on the selected server, shaped like catalog listings."""
        out = []
        for lid, e in self.fstate.installed(self.cur.server.where).items():
            out.append({"id": lid, "title": e.get("title"), "kind": e.get("kind"), "author": e.get("author"),
                        "owner_id": e.get("owner_id"), "status": "withdrawn" if self.fstate.blocks(lid) else "",
                        "latest_version": self.fstate.latest(lid) or {"id": e.get("version_id"),
                                                                      "version": e.get("version")}})
        return sorted(out, key=lambda x: title_of(x).lower())

    def forge_watch(self) -> None:
        """Every forge_poll seconds: what changed on Forge among what's installed. One look at a time, and none at
        all with nothing installed, so an idle console spends nothing of the key's quota."""
        if self.is_running and self.forge and self.fstate.ids() and not self.watching:
            self.watching = True
            self.run_worker(self._forge_watch(), group="forge-watch", exit_on_error=False)

    async def _forge_watch(self) -> None:
        try:
            ids, since = self.fstate.ids(), self.fstate.since() or utc_iso()
            try:
                changes, as_of = await self.forge.changes(before(since, OVERLAP))
            except ForgeError as e:
                if e.status in (401, 403, 502) and self.watch_said != e.short:  # one toast, not one per look
                    self.watch_said = e.short
                    self.notify(f"Checking for updates: {e.text}", title=f"FORGE · {e.short}", severity="warning",
                                timeout=15)
                return
            self.watch_said, keys = "", []
            for c in changes:  # the feed is read back over OVERLAP each time: what was handled before is skipped
                lid, key = change_listing(c), change_key(c)
                if not lid or self.fstate.seen(key) or key in keys:
                    continue
                keys.append(key)
                if lid in ids:
                    await self.forge_news(lid, c)
            self.fstate.saw(keys, as_of)
            if time.time() - (num(self.fstate.data.get("reconciled")) or 0) > RECONCILE:
                await self.forge_reconcile(ids)
        finally:
            self.watching = False

    async def forge_news(self, lid: str, c: dict) -> None:
        """One change to something installed. The feed names the versions it published and withdrew; the older
        spelling, a change word and a version_id, still reads. A version withdrawn while its listing stays up only
        matters if it's the one installed."""
        change = str(pick(c, "change", "type", "event", "kind", default="")).lower()
        status = str(pick(c, "status", default="")).lower()
        self.forge.forget(lid)  # what's kept of it, here and in the cache, is old now
        self.forge_refresh(lid)
        if "withdraw" in change or status in WITHDRAWN:
            self.forge_withdrawn(lid)
            return
        if "restor" in change or status in ("published", "live"):
            self.forge_restored(lid)
        installed = self.fstate.versions(lid)
        if gone := installed & set(id_list(c.get("withdrawn_version_ids"))):
            self.forge_withdrawn(lid, sorted(gone)[0])
        fresh = id_list(c.get("published_version_ids"))
        if (old := str(pick(c, "version_id", "latest_version_id", default=""))) and old not in fresh:
            fresh.append(old)
        if any(v not in installed for v in fresh):
            await self.forge_check(lid, fresh[-1])

    def forge_refresh(self, lid: str) -> None:
        """Forget the file held for a listing, so F6 asks for it again the next time it's shown."""
        self.details.pop(lid, None)
        if self.detail_want == lid:
            self.detail_want = None

    async def forge_check(self, lid: str, named: str = "") -> None:
        """An installed listing fetched whole, which is what the feed's entry is checked against: the order of
        versions and which are withdrawn are the listing's to say. If it can't be fetched, `named`, a version the
        feed said was published, is all there is to go on."""
        try:
            d = await self.forge.listing(lid, background=True)
        except ForgeError as e:
            if named and e.status != 404:
                self.forge_newer(lid, named)
            return
        self.forge_judge(lid, d)

    def forge_judge(self, lid: str, d: dict) -> None:
        """What a whole listing says about what's installed: withdrawn, or the installed version withdrawn, or a
        newer one out. After a version is withdrawn the newest left may be older than the one installed, and that
        isn't an update."""
        self.forge_refresh(lid)
        if is_withdrawn(d):
            self.forge_withdrawn(lid)
            return
        self.forge_restored(lid)
        installed = self.fstate.versions(lid)
        for v in versions_of(d):
            if is_withdrawn(v) and version_id(v) in installed:
                self.forge_withdrawn(lid, version_id(v))
                break
        latest = latest_of(d)
        if latest and version_id(latest):
            dates = {version_id(v): when_of(v) for v in versions_of(d)}
            if (lw := when_of(latest)) and installed and all(dates.get(i) and dates[i] >= lw for i in installed):
                return  # nothing newer than what's installed
            self.forge_newer(lid, version_id(latest), version_label(latest))

    async def forge_reconcile(self, ids: set) -> None:
        """Each installed listing fetched whole, a little apart, for anything the changes feed missed. Background
        requests stop short of the key's last few each minute, and a fault leaves the rest for next time. A listing
        that can't be found isn't called withdrawn: only Forge saying so counts."""
        for lid in sorted(ids):
            try:
                d = await self.forge.listing(lid, background=True)
            except ForgeError as e:
                if e.status in (0, 401, 403, 429, 503):
                    return
                continue
            self.forge_judge(lid, d)
            await asyncio.sleep(0.5)
        self.fstate.data["reconciled"] = time.time()
        self.fstate.save()

    def stations_at(self, servers: list[str]) -> list[Station]:
        return [st for st in self.stations if st.server.where in servers]

    def forge_newer(self, lid: str, vid: str, label: str = "") -> None:
        behind = self.fstate.newer(lid, vid, label)
        if behind and self.is_running:
            e = self.fstate.entry(lid) or {}
            names = ", ".join(st.label for st in self.stations_at(behind)) or "your servers"
            title = e.get("title") or lid
            self.notify(f"{title} {'v' + label if label else 'has a new version'} is out on ReclaimerForge. {names} "
                        f"run{'s' * (len(behind) == 1)} v{e.get('version', '?')}. Install it from F6 (▲).",
                        title="FORGE · UPDATE", timeout=20)
            self.log_event(None, {"event": "forge", "text": f"UPDATE  {title} {('v' + label) if label else ''}".strip()})
        if self.is_running:
            self.paint_forge()

    def forge_withdrawn(self, lid: str, vid: str = "") -> None:
        """The listing, or with `vid` just the installed version of it, was withdrawn from Forge."""
        if self.fstate.withdraw(lid, vid) and self.is_running:
            e = self.fstate.entry(lid) or {}
            title = e.get("title") or lid
            names = ", ".join(st.label for st in self.stations_at(self.fstate.where(lid, vid))) or "your servers"
            if vid:
                label = next((x.get("version") for es in self.fstate.data["servers"].values()
                              for x in es.values() if x.get("version_id") == vid), "") or "?"
                self.notify(f"{title} v{label} was withdrawn from ReclaimerForge by its author. It's still installed "
                            f"on {names}, and flagged ⚠ in the rotation on F3. Install another version from F6, or "
                            f"acknowledge it there (a).", title="FORGE · VERSION WITHDRAWN", severity="warning",
                            timeout=30)
                self.log_event(None, {"event": "forge", "text": f"WITHDRAWN  {title} v{label}  on {names}"})
            else:
                self.notify(f"{title} was withdrawn from ReclaimerForge. It's still installed on {names}, and flagged "
                            f"⚠ in the rotation on F3. Once you've dealt with it, acknowledge it on F6 (a).",
                            title="FORGE · WITHDRAWN", severity="warning", timeout=30)
                self.log_event(None, {"event": "forge", "text": f"WITHDRAWN  {title}  on {names}"})
        if self.is_running:
            self.paint_masthead()
            self.paint_ops()
            self.paint_forge()

    def forge_restored(self, lid: str) -> None:
        if self.fstate.is_withdrawn(lid):
            self.fstate.restore(lid)
            if self.is_running:
                self.paint_masthead()
                self.paint_ops()

    async def forge_ack(self) -> None:
        x = self.cur_listing()
        lid = listing_id(x) if x else ""
        if lid not in self.fstate.alarms():
            self.notify("Nothing here to acknowledge.", severity="warning")
            return
        if await self.push_screen_wait(Confirm(
                f"ACKNOWLEDGE  {title_of(x)}", "It stays installed, and stays flagged as withdrawn in the rotation. "
                "CONDITION goes back to green once nothing else needs you.", verb="ACKNOWLEDGE", danger=False)):
            self.fstate.acknowledge(lid)
            self.paint_masthead()
            self.paint_forge()
