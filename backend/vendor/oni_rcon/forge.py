"""ReclaimerForge (www.reclaimerforge.net): the community catalog of forged maps, gametypes and playlists, read with
the operator's own API key. No key ships with oni-rcon, and it asks for nothing past catalog:read and assets:download.

From the developer docs: a Bearer key over HTTPS; 120 requests per 60 s per key, reported in X-RateLimit-Limit,
-Remaining and -Reset (Unix seconds), and Retry-After on a 429; GET /api/listings (page_size, updated_since, sort,
window for trending and rising), /api/listings/{id}/versions/{version}/manifest, /api/listings/changes
(updated_since, then the first page's as_of as updated_before on the pages after it) and /api/favourites.
Listings carry upvote_count, downvote_count, vote_count and view_count (the star rating is archived: rating_average
and rating_count are deprecated and no longer read), authors[] ({user_id, username, avatar_url, is_owner}, with
owner_id still the original owner), a metrics object with download_history_started_at, and versions with
version_label and release_notes. A change names published_version_ids and withdrawn_version_ids: a version can be
withdrawn while its listing stays up, and a withdrawn version is never one to select. Descriptions and release notes
are Markdown written by strangers; they are shown here as plain text with terminal control characters dropped.
/api/favourites is a bare array of active collections ({id, title, starts_at, ends_at, updated_at, listing_ids,
listings}), kept here as {"items": [...]} like any array reply.

ASSUMED, until checked against the OpenAPI schema (forgefake.py serves exactly these):
- GET /api/listings/{id} gives one listing with its "versions".
- a page is {"items": [...], "next_cursor": ...}, and the next one is asked for with cursor=...
- GET /api/listings takes q= to search.
- a manifest is {"reference", "assets": [{"path", "url", "size", "sha256"}]}, the reference being the name the
  dedicated server lists the map or gametype under once it has the files.
- a change is {"listing_id", "updated_at", "published_version_ids", "withdrawn_version_ids"}, and a listing's
  "status" reads "withdrawn" once the whole of it is. The older {"version_id", "change"} spelling still works.
- recent download counts sit in the listing or in its metrics object, as recent_downloads.
Field names are read tolerantly, as app.py reads the server's, so another spelling of the same thing still works.

Blocking HTTP (urllib) in threads, like update.py: nothing for the frozen build to miss, and 120 requests a minute
needs no connection pool.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import __version__
from .config import CommandFailed, run_command, write_atomic

FORGE_URL = "https://www.reclaimerforge.net"
SITE = "RECLAIMERFORGE.NET"
SCOPES = ("catalog:read", "assets:download")
SORTS = ["trending", "rising", "latest", "updated", "downloads", "rated", "unrated", "overlooked"]
WINDOWS = ["24h", "7d", "30d"]
WINDOWED = {"trending", "rising"}  # the sorts a window is for, per the docs
WINDOW_SECONDS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
CREDIT = ("ReclaimerForge is built and kept running by one of the Reclaimer community, on their own time, and they "
          "signed off on this link-up. Every map, gametype and playlist in F6 is theirs to host and ours to play. "
          "Section Three is in their debt.")
RESERVE = 20  # of a key's requests each minute, kept back for the operator: background checks stop short of it
MAX_WAIT = 60  # seconds: a longer Retry-After or quota reset isn't waited out, it's reported
MAX_JSON = 8 << 20  # bytes in one reply
MAX_ASSET = 1 << 30  # bytes: a manifest claiming more for one file is refused before anything is fetched
KEY_LIKE = re.compile(r"rfk_[A-Za-z0-9_\-]{6,}")
HIDDEN = "rfk_…████"
WITHDRAWN = {"withdrawn", "removed", "unlisted", "taken_down", "takedown", "deleted"}
UNPUBLISHED = {"draft", "pending", "unpublished"}  # a version that isn't out yet: not one to install
UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")  # all but \n and \t, and bidi overrides


# --- the key: never printed, never logged ------------------------------------------------------------------------
_LIVE: set[str] = set()  # every key loaded this session: blanked wherever text is shown


class Secret:
    """A key that never prints: repr, str and f-strings all show it blanked. .value goes in the Authorization header
    and nowhere else."""
    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = str(value).strip()
        if self.value:
            _LIVE.add(self.value)

    def __repr__(self) -> str:
        return f"Secret({HIDDEN!r})"

    def __str__(self) -> str:
        return HIDDEN

    def __format__(self, spec: str) -> str:
        return HIDDEN

    def __bool__(self) -> bool:
        return bool(self.value)


def scrub(text: str) -> str:
    """text with every loaded key, and anything shaped like one, blanked. The same str when there's none."""
    if not isinstance(text, str) or not (KEY_LIKE.search(text) or any(k in text for k in _LIVE)):
        return text
    for k in _LIVE:
        text = text.replace(k, HIDDEN)
    return KEY_LIKE.sub(HIDDEN, text)


def scrub_data(v):
    """A reply or event with any key in it blanked, for showing. The same object when there's none."""
    if isinstance(v, str):
        return scrub(v)
    if isinstance(v, dict):
        new = {k: scrub_data(x) for k, x in v.items()}
        return v if all(new[k] is v[k] for k in v) else new
    if isinstance(v, list):
        new = [scrub_data(x) for x in v]
        return v if all(a is b for a, b in zip(new, v)) else new
    return v


@dataclass
class KeyFound:
    key: Secret | None = None
    source: str = ""  # where it came from, for the F6 pane: never any part of the key itself
    error: str = ""


def load_key(settings: dict, env=os.environ) -> KeyFound:
    """forge_api_key (there only if the operator chose to remember it) > forge_api_key_env > forge_api_key_command
    > $ONI_RCON_FORGE_KEY. Unlike a missing RCON password this never stops the console: Forge is just off."""
    if settings.get("forge_api_key"):
        return KeyFound(Secret(settings["forge_api_key"]), "the config file")
    name = str(settings.get("forge_api_key_env") or "")
    if name and env.get(name, "").strip():
        return KeyFound(Secret(env[name]), f"${name}")
    if cmd := settings.get("forge_api_key_command"):
        try:
            out = run_command(cmd, "forge_api_key_command")
        except CommandFailed as e:
            return KeyFound(None, "forge_api_key_command", scrub(str(e)))
        if out.strip():
            return KeyFound(Secret(out), "forge_api_key_command")
        return KeyFound(None, "forge_api_key_command", "forge_api_key_command printed nothing.")
    if env.get("ONI_RCON_FORGE_KEY", "").strip():
        return KeyFound(Secret(env["ONI_RCON_FORGE_KEY"]), "$ONI_RCON_FORGE_KEY")
    return KeyFound(None, "", f"${name} is empty or not set." if name else "")


@dataclass
class ForgeSetup:
    """What the console needs to reach Forge, from __main__: the key and where things are kept."""
    key: Secret | None = None
    source: str = ""
    error: str = ""
    url: str = FORGE_URL
    poll: float = 600  # seconds between looks at the changes feed
    state_dir: Path | None = None  # forge-state.json and rounds.jsonl; None keeps them in memory only
    cache_dir: Path | None = None  # replies and verified downloads; None keeps none
    config: Path | None = None  # the config file "remember the key" may write to; None offers no remembering


# --- errors, in words ---------------------------------------------------------------------------------------------
EXPLAIN = {
    401: ("KEY REFUSED", "ReclaimerForge refused your API key: it's mistyped, revoked or expired. Make a new one on "
                         "reclaimerforge.net and load it again (k on F6)."),
    403: ("MISSING SCOPE", "Your key isn't allowed to do that. oni-rcon needs a key with the catalog:read and "
                           "assets:download scopes, and nothing more. The account behind it also needs the Developer "
                           "role and a verified email, or its keys stop working."),
    404: ("NOT AVAILABLE", "That listing or version isn't available. It may have been withdrawn or made private."),
    409: ("CHANGED MEANWHILE", "That listing changed state while you were looking at it. Refresh (Ctrl+R) and try "
                               "again."),
    413: ("TOO LARGE", "ReclaimerForge won't serve something that large."),
    422: ("NOT ACCEPTED", "ReclaimerForge couldn't accept that request."),
    429: ("RATE LIMITED", "Your key has used its requests for this minute. Wait a moment and try again."),
    503: ("FORGE BUSY", "ReclaimerForge is down for a moment. Try again shortly."),
    502: ("BAD REPLY", "ReclaimerForge sent back something oni-rcon can't read."),
    0: ("NO UPLINK", "Can't reach reclaimerforge.net. Check your connection."),
}


class ForgeError(Exception):
    """A request that didn't work, as the operator should read it: .text in plain words, .short for a heading."""

    def __init__(self, status: int, detail: str = "", retry_after: float | None = None):
        self.status, self.detail, self.retry_after = status, scrub(str(detail))[:300], retry_after
        super().__init__(self.text)

    @property
    def short(self) -> str:
        return EXPLAIN.get(self.status, (f"FORGE ERROR {self.status}",))[0]

    @property
    def text(self) -> str:
        words = EXPLAIN[self.status][1] if self.status in EXPLAIN else f"ReclaimerForge answered {self.status}."
        if self.status == 429 and self.retry_after:
            words = f"Your key has used its requests for this minute. Try again in {int(self.retry_after) + 1}s."
        elif self.status in (0, 409, 422, 502) and self.detail:  # a 409's detail says which state it conflicts with
            words += f" ({self.detail})"
        return words


class Unverified(Exception):
    """A download that doesn't match its manifest, in plain words. Nothing that fails the check is kept."""


def detail_of(body: bytes) -> str:
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        d = json.loads(body)
        v = pick(d, "message", "detail", "error", "title")
        return v if isinstance(v, str) else json.dumps(v) if v else ""
    return ""


def retry_after(v) -> float | None:
    """Retry-After as seconds from now: it may come as seconds or as an HTTP date."""
    if not v:
        return None
    with contextlib.suppress(ValueError):
        return max(0.0, float(v))
    with contextlib.suppress(TypeError, ValueError, IndexError, OverflowError):
        return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
    return None


def loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def secure(url: str) -> bool:
    """https, or plain http to this machine only (the fake Forge in --demo and the tests)."""
    u = urlsplit(url)
    return u.scheme == "https" or u.scheme == "http" and loopback(u.hostname)


def origin(url: str) -> tuple:
    u = urlsplit(url)
    return u.scheme, (u.hostname or "").lower(), u.port or {"https": 443, "http": 80}.get(u.scheme)


# --- the quota ----------------------------------------------------------------------------------------------------
@dataclass
class Quota:
    """The key's requests this minute, as the last reply's X-RateLimit headers told it."""
    limit: int | None = None
    remaining: int | None = None
    reset_at: float = 0.0  # monotonic

    def read(self, headers) -> None:
        if headers is None:
            return
        with contextlib.suppress(TypeError, ValueError):
            self.limit = int(headers.get("X-RateLimit-Limit"))
        with contextlib.suppress(TypeError, ValueError):
            self.remaining = int(headers.get("X-RateLimit-Remaining"))
        with contextlib.suppress(TypeError, ValueError):
            reset = float(headers.get("X-RateLimit-Reset"))  # a Unix time in seconds per the docs; seconds from now too
            self.reset_at = time.monotonic() + (reset - time.time() if reset > 1e9 else reset)

    def wait(self, background: bool = False) -> float:
        """Seconds to hold off before the next request: none while there's quota to spare. Background work stops
        RESERVE short, so the operator browsing F6 never finds the key spent by the watcher."""
        if self.remaining is None or self.remaining > (RESERVE if background else 0):
            return 0.0
        return max(0.0, self.reset_at - time.monotonic())

    def __str__(self) -> str:
        if self.remaining is None:
            return ""
        return f"{self.remaining}/{self.limit or '?'} requests left this minute"


# --- reading what comes back --------------------------------------------------------------------------------------
def pick(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k) not in (None, ""):
                return d[k]
    return default


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def items_of(reply: dict) -> list:
    v = pick(reply, "items", "results", "listings", "changes", "data")
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def next_page(reply: dict) -> dict | None:
    """The query for the page after this one, or None at the end."""
    cur = pick(reply, "next_cursor", "cursor_next", "next")
    if isinstance(cur, (str, int)) and not isinstance(cur, bool) and not str(cur).startswith(("http:", "https:", "/")):
        return {"cursor": cur}
    page = num(reply.get("page"))
    pages = num(pick(reply, "pages", "total_pages", "page_count"))
    if page is not None and (pick(reply, "has_more", "has_next") is True or pages is not None and page < pages):
        return {"page": int(page) + 1}
    return None


def tidy(v, lines: bool = False) -> str:
    """Text from Forge as it's safe on a terminal. Titles, names and descriptions are written by strangers, and a
    terminal obeys escape sequences: control characters go, and so do the bidi overrides that make one name read as
    another. One line unless `lines`, which keeps the newlines and tabs Markdown uses. It is shown as plain text,
    never rendered, so no link or markup in it does anything."""
    s = UNSAFE.sub("", "" if v is None else str(v))
    return s.strip() if lines else " ".join(s.split())


def shorten(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def id_list(v) -> list[str]:
    """The ids in a list of them (published_version_ids, listing_ids), in order, without blanks or repeats."""
    out = []
    for i in v if isinstance(v, list) else []:
        if isinstance(i, (str, int)) and not isinstance(i, bool) and str(i) and str(i) not in out:
            out.append(str(i))
    return out


def listing_id(x) -> str:
    return str(pick(x, "id", "listing_id", default=""))


def version_id(x) -> str:
    return str(pick(x, "id", "version_id", default="")) if isinstance(x, dict) else str(x or "")


def version_label(x) -> str:
    return (tidy(pick(x, "version_label", "version", "label", "name", "number", default=version_id(x)))
            if isinstance(x, dict) else "")


def notes_of(v) -> str:
    """A version's release notes (Markdown on the website) as one line of plain text."""
    return tidy(pick(v, "release_notes", "notes", "changelog", "summary", default=""))


def blurb_of(x) -> str:
    """A listing's description (Markdown on the website) as plain text, line breaks kept."""
    return tidy(pick(x, "description", "summary", "short_description", default=""), lines=True)


def title_of(x) -> str:
    return tidy(pick(x, "title", "name", default="")) or "untitled"


def kind_of(x) -> str:
    k = str(pick(x, "kind", "type", "category", "content_type", default="")).lower()
    return {"game_type": "gametype", "game type": "gametype", "variant": "gametype", "map_variant": "map"}.get(k, k)


def authors_of(x) -> list[dict]:
    """The listing's authors, the original owner first: [{"user_id", "username", "is_owner"}]. A listing may have
    several; Forge's stable id for each is user_id."""
    a = x.get("authors") if isinstance(x, dict) else None
    out = [{"user_id": str(pick(e, "user_id", "id", default="")),
            "username": tidy(pick(e, "username", "display_name", "name", default="")),
            "is_owner": e.get("is_owner") is True}
           for e in a if isinstance(e, dict)] if isinstance(a, list) else []
    return sorted((e for e in out if e["username"] or e["user_id"]), key=lambda e: not e["is_owner"])


def owner_of(x) -> str:
    """The original owner's id: owner_id, which Forge keeps whoever else has since been added as an author."""
    o = pick(x, "owner_id", "author_id", "creator_id")
    if o is None and isinstance(x.get("owner"), dict):
        o = pick(x["owner"], "id", "owner_id")
    if o is None:
        o = next((e["user_id"] for e in authors_of(x) if e["is_owner"] and e["user_id"]), None)
    return str(o or "")


def author_of(x) -> str:
    """Who made it, as the names to show: every author, the owner first."""
    if names := [e["username"] for e in authors_of(x) if e["username"]]:
        return ", ".join(names)
    a = pick(x, "author", "owner_name", "owner_display_name", "author_name", "creator")
    if isinstance(a, dict):
        a = pick(a, "display_name", "name", "username", "handle")
    if not a and isinstance(x.get("owner"), dict):
        a = pick(x["owner"], "display_name", "name", "username", "handle")
    return tidy(a)


def is_withdrawn(x) -> bool:
    """A listing or a version that Forge says is withdrawn."""
    return (str(pick(x, "status", "state", default="")).lower() in WITHDRAWN
            or pick(x, "withdrawn") is True or bool(pick(x, "withdrawn_at")))


def usable(v) -> bool:
    """Whether a version can be selected for an install: not withdrawn, and out rather than a draft."""
    return (isinstance(v, dict) and not is_withdrawn(v)
            and str(pick(v, "status", "state", default="")).lower() not in UNPUBLISHED)


def versions_of(x) -> list[dict]:
    v = pick(x, "versions", "releases")
    return [e for e in v if isinstance(e, dict)] if isinstance(v, list) else []


def when_of(v) -> datetime | None:
    return parse_iso(pick(v, "published_at", "created_at", "released_at"))


def newest(vs: list[dict]) -> dict | None:
    """The most recent of some versions: by date when each has one, else the first, as Forge lists them."""
    dated = [(when_of(v), v) for v in vs]
    if dated and all(d for d, _ in dated):
        return max(dated, key=lambda p: p[0])[1]
    return vs[0] if vs else None


def latest_of(x) -> dict | None:
    """The newest version that can be installed. A version Forge has withdrawn is never it, even if the listing's
    own latest_version still names it."""
    gone = {version_id(v) for v in versions_of(x) if not usable(v)}
    lv = pick(x, "latest_version", "current_version", "latest_release", "latest")
    if isinstance(lv, dict) and usable(lv) and version_id(lv) not in gone:
        return lv
    if isinstance(lv, str) and lv not in gone:
        return {"id": lv}
    return newest([v for v in versions_of(x) if usable(v)])


def votes_of(x) -> tuple:
    """(thumbs up, thumbs down, votes), each None when the listing doesn't say. The star rating (rating_average,
    rating_count) is archived and deprecated by Forge, doesn't rank anything, and isn't read."""
    up, down, total = num(pick(x, "upvote_count")), num(pick(x, "downvote_count")), num(pick(x, "vote_count"))
    if up is None and down is None:  # another spelling of the same thing
        r = pick(x, "ratings", "rating_counts", "votes")
        if isinstance(r, dict):
            up = num(pick(r, "up", "positive", "likes", "thumbs_up"))
            down = num(pick(r, "down", "negative", "dislikes", "thumbs_down"))
    if total is None and (up is not None or down is not None):
        total = (up or 0) + (down or 0)
    return up, down, total


def metrics_of(x) -> dict:
    m = x.get("metrics") if isinstance(x, dict) else None
    return m if isinstance(m, dict) else {}


def recent_of(x, window: str = ""):
    """Recent downloads: for the window asked for when the listing breaks them down, else what it gives."""
    for src in (x, metrics_of(x)):
        r = pick(src, "recent_downloads", "downloads_recent", "window_downloads", "downloads_window",
                 f"recent_downloads_{window}", f"downloads_{window}")
        if isinstance(r, dict):
            r = pick(r, window, "7d", "30d", "24h")
        if num(r) is not None:
            return num(r)
    return None


def views_of(x):
    """Lifetime views of the listing page."""
    for src in (x, metrics_of(x)):
        if (n := num(pick(src, "view_count", "views"))) is not None:
            return n
    return None


def history_since(x, window: str = "") -> datetime | None:
    """When Forge began recording download history, if that is later than the start of `window`: the recent count
    then covers only part of it. Recent windows fill from deployment onward and nothing is backfilled."""
    began = parse_iso(pick(metrics_of(x), "download_history_started_at"))
    span = WINDOW_SECONDS.get(window)
    return began if began and span and began > datetime.now(timezone.utc) - timedelta(seconds=span) else None


def compat_of(x):
    """What a listing says it runs on. Compatibility is set per version and frozen when it's published, so the
    newest installable version's says when the listing itself doesn't."""
    keys = ("compatibility", "compatible_versions", "reclaimer_versions", "compatible_with", "requires", "game_version")
    spec = pick(x, *keys)
    if spec is None and (v := latest_of(x)):
        spec = pick(v, *keys)
    return spec


def version_tuple(v) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", str(v).split("-")[0].split("+")[0])[:3])


def compatible(spec, server_version) -> bool | None:
    """Whether a listing says it runs on a server of this version: None when either side doesn't say. A spec may be
    a version ("0.9.7", "0.9.x"), a range (">=0.9.5, <0.10"), a list of either, or {"min", "max"}."""
    have = version_tuple(server_version) if server_version else ()
    if not have or spec in (None, "", [], {}):
        return None
    if isinstance(spec, dict):
        inner = pick(spec, "reclaimer", "server", "dedicated", "versions", "range")
        if inner is not None:
            return compatible(inner, server_version)
        lo, hi = pick(spec, "min", "from", "minimum"), pick(spec, "max", "to", "maximum")
        if lo is None and hi is None:
            return None
        return (lo is None or have >= version_tuple(lo)) and (hi is None or have[:len(version_tuple(hi))]
                                                               <= version_tuple(hi))
    if isinstance(spec, list):
        said = [compatible(s, server_version) for s in spec]
        return True if any(said) else None if all(s is None for s in said) else False
    ok = True
    for term in filter(None, re.split(r"[,\s]+", str(spec))):
        m = re.fullmatch(r"(>=|<=|>|<|==|=|\^|~)?v?([\d.]*\d)(?:\.[x*])?|[x*]", term.strip())
        if not m:
            return None
        if term.strip() in ("x", "*"):
            continue
        op, want = m.group(1), version_tuple(m.group(2))
        part, h, w = have[:len(want)], (have + (0, 0))[:3], (want + (0, 0))[:3]  # "<=0.9" takes in 0.9.7
        ok &= {">=": h >= w, ">": h > w, "<=": part <= want, "<": h < w}.get(op, part == want)
    return ok


def utc_iso(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def parse_iso(s) -> datetime | None:
    with contextlib.suppress(TypeError, ValueError):
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def before(s: str, seconds: float) -> str:
    """An ISO time moved back: the overlap a watermark keeps, so a change written late isn't missed."""
    d = parse_iso(s)
    return utc_iso(d - timedelta(seconds=seconds)) if d else s


def change_listing(c: dict) -> str:
    return str(pick(c, "listing_id", "id", default=""))


def change_key(c: dict) -> str:
    """What makes a changes-feed entry the same entry when the overlap reads it again: the listing, the time, and
    which versions it published or withdrew. Deduplication is by these, as the docs ask, never by position."""
    key = "|".join([change_listing(c), *(str(pick(c, k, default="")) for k in ("version_id", "change", "updated_at"))])
    for name, short in (("published_version_ids", "pub"), ("withdrawn_version_ids", "wd")):
        if ids := id_list(c.get(name)):
            key += f"|{short}:{','.join(sorted(ids))}"
    return key


def active_now(c: dict, now: datetime | None = None) -> bool:
    """Whether a favourites collection is on: it starts at starts_at (inclusive) and ends at ends_at (exclusive). A
    bound it doesn't give is open, and one that can't be read leaves the collection as Forge sent it."""
    now = now or datetime.now(timezone.utc)
    start, end = parse_iso(pick(c, "starts_at")), parse_iso(pick(c, "ends_at"))
    return (start is None or start <= now) and (end is None or now < end)


def favourite_listings(collections: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """The listings the collections feature, one after another in each collection's order and without repeats, and a
    note for each saying which collection it's in and until when."""
    out, seen, notes = [], set(), {}
    for c in collections:
        by_id = {listing_id(x): x for x in c.get("listings") or [] if isinstance(x, dict) and listing_id(x)}
        order = [i for i in id_list(c.get("listing_ids")) if i in by_id]
        for lid in order + [i for i in by_id if i not in order]:
            if lid not in seen:
                seen.add(lid)
                out.append(by_id[lid])
                end = str(pick(c, "ends_at", default=""))[:10]
                notes[lid] = tidy(pick(c, "title", default="Favourites")) + (f", until {end}" if end else "")
    return out, notes


def sha_hex(v) -> str:
    """A manifest's SHA-256 as 64 lowercase hex digits: bare, or as sha256:... Anything else can't be checked."""
    s = str(v or "").strip()
    s = s.split(":", 1)[1] if s.lower().startswith("sha256:") else s
    if not re.fullmatch(r"[0-9a-fA-F]{64}", s):
        raise ValueError("not a SHA-256")
    return s.lower()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# --- the client ---------------------------------------------------------------------------------------------------
def cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "oni-rcon"


class Cache:
    """Replies by URL, served again while fresh and, when Forge can't be reached, however old. They're kept in a
    folder of the key's own, named by a hash of it, so two keys never see each other's replies. No key and no
    header is ever written. Downloads are kept by their SHA-256 and checked again each time they're used."""

    def __init__(self, root: Path | None, key: Secret | None):
        self.dir = root / "replies" / hashlib.sha256(key.value.encode()).hexdigest()[:16] if root and key else None
        self.assets = root / "assets" if root else None

    def _file(self, url: str) -> Path:
        return self.dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def get(self, url: str, max_age: float | None) -> tuple[dict, float] | None:
        """(reply, its age in seconds) when there's one no older than max_age; any age when that's None."""
        if not self.dir:
            return None
        try:
            entry = json.loads(self._file(url).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        age = time.time() - (num(entry.get("at")) or 0)
        if not isinstance(entry.get("body"), dict) or max_age is not None and age > max_age:
            return None
        return entry["body"], age

    def put(self, url: str, body: dict) -> None:
        if self.dir:
            with contextlib.suppress(OSError):
                write_atomic(self._file(url), json.dumps({"url": url, "at": time.time(), "body": body}))

    def drop(self, url: str) -> None:
        if self.dir:
            with contextlib.suppress(OSError):
                self._file(url).unlink()


class ForgeClient:
    """One key's view of ReclaimerForge. Every call is a GET: retried on a 429 (after its Retry-After) or a 503,
    with jitter, never on anything else."""

    def __init__(self, key: Secret | None, base: str = FORGE_URL, cache_dir: Path | None = None,
                 sleep=asyncio.sleep, timeout: float = 20):
        if not secure(base):
            raise ValueError(f"{base} isn't https: oni-rcon sends your Forge key over https only")
        self.key, self.base, self.origin = key, base.rstrip("/"), origin(base)
        self.quota, self.sleep, self.timeout = Quota(), sleep, timeout
        self.cache = Cache(cache_dir, key)
        self.stop = threading.Event()  # set on quit: a download in a thread gives up at its next chunk
        self.sent = 0  # requests this session

    def url(self, path: str, params: dict | None = None) -> str:
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        return f"{self.base}{path}" + (f"?{urlencode(params)}" if params else "")

    def _request(self, url: str) -> Request:
        req = Request(url, headers={"User-Agent": f"oni-rcon/{__version__}", "Accept": "application/json"})
        if self.key and origin(url) == self.origin:  # the key goes to Forge itself and nowhere else, redirects
            req.add_unredirected_header("Authorization", f"Bearer {self.key.value}")  # included
        return req

    @staticmethod
    def _open(req: Request, timeout: float):
        # the system's proxy for the real site; none for this machine, where a proxy would only be in the way
        opener = build_opener(ProxyHandler({})) if loopback(urlsplit(req.full_url).hostname) else build_opener()
        return opener.open(req, timeout=timeout)

    def _fetch(self, url: str) -> tuple[int, object, bytes]:
        try:
            with self._open(self._request(url), self.timeout) as r:
                return r.status, r.headers, r.read(MAX_JSON + 1)
        except HTTPError as e:
            body = b""
            with contextlib.suppress(Exception):
                body = e.read(4096)
            return e.code, e.headers, body
        except (URLError, OSError, ValueError, HTTPException) as e:  # offline, refused, timed out, TLS, cut off
            raise ForgeError(0, str(getattr(e, "reason", "") or e) or type(e).__name__) from None

    async def _get(self, url: str, background: bool) -> dict:
        for attempt in range(4):
            if hold := self.quota.wait(background):
                if hold > MAX_WAIT:
                    raise ForgeError(429, retry_after=hold)
                await self.sleep(hold + random.uniform(0, 1))
            self.sent += 1
            status, headers, body = await asyncio.to_thread(self._fetch, url)
            self.quota.read(headers)
            if 200 <= status < 300:
                try:
                    data = json.loads(body) if len(body) <= MAX_JSON else None
                except ValueError:
                    data = None
                if isinstance(data, list):  # an unpaginated array, as /api/favourites sends: kept as items
                    data = {"items": data}
                if not isinstance(data, dict):
                    raise ForgeError(502, "not a JSON object" if data is not None or len(body) <= MAX_JSON
                                     else "reply too large")
                return data
            err = ForgeError(status, detail_of(body), retry_after(headers.get("Retry-After") if headers else None))
            if status not in (429, 503) or attempt == 3:
                raise err
            wait = err.retry_after if err.retry_after is not None else self.quota.wait() or 2 ** attempt
            if wait > MAX_WAIT:
                raise err
            # jittered, so consoles that hit the limit together don't come back together
            await self.sleep(wait + random.uniform(0, max(0.5, wait / 4)))
        raise AssertionError("unreachable")

    async def get(self, path: str, params: dict | None = None, *, max_age: float = 0, background: bool = False,
                  stale_ok: bool = True) -> dict:
        """A reply as a dict. Served from the cache while fresh (max_age seconds); when Forge can't be reached or
        is rate limiting, an older copy is served with "_stale" set to its age in seconds."""
        if not self.key:
            raise ForgeError(401, "no key loaded")
        url = self.url(path, params)
        if max_age and (hit := self.cache.get(url, max_age)):
            return hit[0]
        try:
            body = await self._get(url, background)
        except ForgeError as e:
            if stale_ok and e.status in (0, 429, 503) and (hit := self.cache.get(url, None)):
                return {**hit[0], "_stale": round(hit[1])}
            raise
        self.cache.put(url, body)
        return body

    async def listings(self, sort: str = "trending", window: str = "7d", query: str = "", page_size: int = 50,
                       more: dict | None = None, fresh: bool = False) -> tuple[list, dict | None, dict]:
        """(listings, the query for the next page or None, the reply itself)."""
        reply = await self.get("/api/listings", {"page_size": page_size, "sort": sort, "q": query.strip(),
                                                 "window": window if sort in WINDOWED else None, **(more or {})},
                               max_age=0 if fresh else 60)
        return items_of(reply), next_page(reply), reply

    async def favourites(self, fresh: bool = False) -> tuple[list[dict], dict]:
        """(the collections on now, the reply itself). The feed lists active collections, but a copy served from the
        cache while Forge is down may be old, so the dates are checked here too. It's read apart from the listings'
        own changes: curation and its schedule move on their own."""
        reply = await self.get("/api/favourites", max_age=0 if fresh else 60)
        return [c for c in items_of(reply) if active_now(c)], reply

    def forget(self, lid: str) -> None:
        """Drop the kept copy of a listing: Forge has said it changed, so the next look fetches it again."""
        self.cache.drop(self.url(f"/api/listings/{quote(lid, safe='')}"))

    async def listing(self, lid: str, background: bool = False) -> dict:
        reply = await self.get(f"/api/listings/{quote(lid, safe='')}", max_age=0 if background else 60,
                               background=background, stale_ok=not background)
        inner = pick(reply, "listing", "item", "data")
        return inner if isinstance(inner, dict) else reply

    async def manifest(self, lid: str, vid: str) -> dict:
        reply = await self.get(f"/api/listings/{quote(lid, safe='')}/versions/{quote(vid, safe='')}/manifest",
                               stale_ok=False)
        inner = pick(reply, "manifest", "data")
        return inner if isinstance(inner, dict) else reply

    async def changes(self, since: str, page_size: int = 100, background: bool = True) -> tuple[list, str]:
        """Every change since `since` (UTC, ISO 8601), and the feed's as_of: the next watermark. The pages after
        the first are pinned to that as_of with updated_before, since the feed moves while it's read."""
        first = await self.get("/api/listings/changes", {"updated_since": since, "page_size": page_size},
                               background=background, stale_ok=False)
        as_of = str(pick(first, "as_of", default=""))
        if not as_of:
            raise ForgeError(502, "the changes feed gave no as_of to page from")
        out, reply = items_of(first), first
        for _ in range(50):  # a feed that never ends is a fault, not a reason to spend the quota
            if not (nxt := next_page(reply)):
                break
            reply = await self.get("/api/listings/changes", {"updated_since": since, "updated_before": as_of,
                                                             "page_size": page_size, **nxt},
                                   background=background, stale_ok=False)
            out += items_of(reply)
        return out, as_of

    async def download(self, link: str, size: int, sha256: str) -> Path:
        """The asset, fetched once into the cache and checked against the manifest's size and SHA-256 before anything
        can use it. Raises Unverified when it doesn't match; nothing that fails is kept."""
        try:
            digest = sha_hex(sha256)
        except ValueError:
            raise Unverified("The manifest gives no SHA-256 for that file, so it can't be checked.") from None
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_ASSET:
            raise Unverified("The manifest gives that file no size oni-rcon will accept.")
        if not self.cache.assets:
            raise Unverified("oni-rcon has nowhere to keep downloads.")
        path = self.cache.assets / digest
        if path.is_file() and path.stat().st_size == size and await asyncio.to_thread(file_sha, path) == digest:
            return path
        url = urljoin(self.base + "/", link)
        if not secure(url):
            raise Unverified("The manifest links that file over plain http, so it isn't fetched.")
        for attempt in range(3):
            try:
                await asyncio.to_thread(self._stream, url, size, digest, path)
                return path
            except ForgeError as e:
                if e.status not in (429, 503) or attempt == 2 or (e.retry_after or 0) > MAX_WAIT:
                    raise
                wait = e.retry_after if e.retry_after is not None else 2 ** attempt
                await self.sleep(wait + random.uniform(0, max(0.5, wait / 4)))
        raise AssertionError("unreachable")

    def _stream(self, url: str, size: int, digest: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
        h, got = hashlib.sha256(), 0
        try:
            self.sent += 1
            with self._open(self._request(url), self.timeout) as r, open(part, "wb") as f:
                if origin(url) == self.origin:
                    self.quota.read(r.headers)
                while chunk := r.read(1 << 16):
                    if self.stop.is_set():
                        raise Unverified("Stopped before the download finished.")
                    got += len(chunk)
                    if got > size:  # stop the moment it runs past what the manifest promised
                        raise Unverified(f"The file is bigger than the manifest says ({size} bytes): not used.")
                    h.update(chunk)
                    f.write(chunk)
            if got != size:
                raise Unverified(f"The file came to {got} bytes, not the {size} the manifest says: not used.")
            if h.hexdigest() != digest:
                raise Unverified("The file doesn't match the manifest's SHA-256: not used.")
            os.replace(part, path)
        except HTTPError as e:
            raise ForgeError(e.code, "", retry_after(e.headers.get("Retry-After") if e.headers else None)) from None
        except (URLError, OSError, ValueError, HTTPException) as e:
            raise ForgeError(0, str(getattr(e, "reason", "") or e) or type(e).__name__) from None
        finally:
            with contextlib.suppress(OSError):
                part.unlink()
