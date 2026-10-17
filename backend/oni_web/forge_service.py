"""Reuse upstream Forge reads, manifest validation, cache, downloads and install targets.

Write operations follow the desktop client's endpoints and are never automatically retried.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from oni_rcon import forge as f
from oni_rcon import install


def connect_forge(h):
    key = h.session_forge_key or h.vault.data.get("forge_key") or os.getenv("RECLAIMERFORGE_API_KEY", "")
    base = h.demo_forge[0] if h.demo_forge else f.FORGE_URL
    cache = Path(h.scratch.name) / "cache" if h.scratch else h.settings.data_dir / "cache"
    h.forge = f.ForgeClient(f.Secret(key), base=base, cache_dir=cache) if key else None


def forge_status(h):
    return {"key": bool(h.forge), "source": "demo" if h.demo else h.forge_source,
            "error": "", "quota": str(h.forge.quota) if h.forge else ""}


def client(h):
    if not h.forge:
        raise f.ForgeError(401, "Load your ReclaimerForge API key first")
    return h.forge


def error(e):
    return {"error": {"status": e.status, "short": e.short, "text": e.text}}


async def dispatch(h, name, a):
    try:
        c = client(h)
        if name == "forge_listings":
            items, more, reply = await search(c, a.get("sort", "trending"), a.get("window", "7d"), a.get("q", ""),
                                              more=a.get("more"), fresh=a.get("fresh", False))
            return {"items": items, "next": more, "stale": reply.get("_stale"), "status": forge_status(h)}
        if name == "forge_favourites":
            cols, reply = await c.favourites(a.get("fresh", False))
            items, notes = f.favourite_listings(cols)
            return {"items": items, "notes": notes, "stale": reply.get("_stale"), "status": forge_status(h)}
        if name == "forge_listing":
            lid = quote(a["lid"], safe="")
            x = await c.get(f"/api/listings/mine/{lid}", stale_ok=False) if a.get("mine") else await c.listing(a["lid"])
            return {"listing": inner(x)}
        if name == "forge_mine":
            return {"items": f.items_of(await c.get("/api/listings/mine", stale_ok=False)), "status": forge_status(h)}
        if name == "forge_plan":
            return await plan(h, a["index"], a["listing"], a["version"])
        if name == "forge_put":
            await put(h, a["plan"])
            return None
        if name == "forge_write":
            what, lid, vid = a["what"], quote(a["lid"], safe=""), quote(a.get("vid") or "", safe="")
            path = f"/api/listings/{lid}"
            method = "PATCH"
            body = a.get("change") or {}
            if what == "edit":
                original = inner(await c.get(f"/api/listings/mine/{lid}", stale_ok=False))
                allowed = ("title", "description", "tags", "visibility", "is_modded_map", "workshop_urls", "min_players", "max_players")
                body = {k: body.get(k, original.get(k)) for k in allowed if body.get(k, original.get(k)) is not None}
            elif what == "notes":
                path += f"/versions/{vid}"
            elif what == "withdraw":
                if not vid:
                    raise ValueError("Choose a version to withdraw")
                path += f"/versions/{vid}"
                method = "DELETE"
            elif what == "delete":
                path += f"/versions/{vid}" if vid else ""
                method = "DELETE"
            elif what != "edit":
                raise ValueError("Unknown Forge action")
            result = await write(c, method, path, body if method == "PATCH" else None)
            c.forget(a["lid"])
            return {"listing": inner(result)}
        if name == "forge_publish":
            return await publish(h, a.get("lid"), a["info"], a.pop("_files", []))
        raise ValueError("Unknown Forge operation")
    except f.ForgeError as e:
        return error(e)
    except (install.InstallError, f.Unverified) as e:
        return {"error": {"short": "NOT INSTALLED", "text": str(e)}}


async def search(c, sort, window, query, more=None, fresh=False, sweep_pages=10):
    """IBIGS 2026-10-16: Forge's own search, then (when it finds fewer than 10) a sweep of the catalog ranked by
    closeness, so a typo like "gluch" still finds "Gulch". Exact hits from Forge stay first."""
    from oni_rcon import fuzzy
    items, nxt, reply = await c.listings(sort, window, query, more=more, fresh=fresh)
    if more or not str(query).strip() or len(items) >= 10:
        return items, nxt, reply
    pool, cursor = [], None
    for _ in range(sweep_pages):
        try:
            page = await c.get("/api/listings", {"page_size": 50, "sort": sort,
                                                 "window": window if sort in f.WINDOWED else None, **(cursor or {})},
                               max_age=0 if fresh else 300)
        except f.ForgeError:
            break
        pool += f.items_of(page)
        cursor = f.next_page(page)
        if not cursor:
            break
    have = {f.listing_id(x) for x in items}
    extra = fuzzy.rank(query, [x for x in pool if f.listing_id(x) not in have],
                       lambda x: (f.title_of(x), f.author_of(x), f.kind_of(x), " ".join(map(str, x.get("tags") or []))))
    return items + extra, nxt, reply


def inner(v):
    return f.pick(v, "listing", "item", "data", default=v)


async def plan(h, index, listing, version):
    h.station(index)
    c = client(h)
    # Browser metadata is not an authority for withdrawal or manifest selection.
    lid, vid = f.listing_id(listing), f.version_id(version)
    x = await c.listing(lid, background=True)
    v = next((v for v in f.versions_of(x) if f.version_id(v) == vid), None)
    if f.is_withdrawn(x) or not v or not f.usable(v):
        raise install.InstallError("The listing/version is unavailable or withdrawn")
    s = h.servers[index]
    target = install.target_for(s)
    if isinstance(target, str):
        raise install.InstallError(target)
    manifest = await c.manifest(lid, vid)
    files = install.plan(manifest)
    there = await target.existing([x.path for x in files])
    sharing = [i for i, other in enumerate(h.servers) if install.place(other) == install.place(s)]
    id_ = uuid.uuid4().hex
    # Bound unfinished plans and make them expire; the token is single use.
    h.plans = {k: p for k, p in h.plans.items() if p["expires"] > time.monotonic()}
    if len(h.plans) >= 64:
        raise ValueError("Too many unfinished install plans")
    h.plans[id_] = {"index": index, "listing": x, "version": v, "manifest": manifest, "files": files,
                    "existing": there, "expires": time.monotonic() + 600, "sharing": sharing}
    body = (f"{len(files)} files, {install.size_words(sum(x.size for x in files))}, into {s.content_dir}.\n"
            f"Shared by: {', '.join(h.labels[i] for i in sharing)}.\n"
            + (f"Replaces: {', '.join(sorted(there))}.\n" if there else "")
            + "Every file is checked against the manifest before copying and again at its destination. Loading is separate.")
    return {"title": f"INSTALL · {f.title_of(x)}", "body": body, "replacing": bool(there), "plan": id_}


async def put(h, id_):
    async with h.install_lock:
        p = h.plans.pop(id_, None)
        if not p or p["expires"] < time.monotonic():
            raise ValueError("Install plan expired; review a new plan")
        c = client(h)
        x, v, files = p["listing"], p["version"], p["files"]
        index = p["index"]
        s = h.servers[index]
        target = install.target_for(s)
        if isinstance(target, str):
            raise install.InstallError(target)
        there = await target.existing([f.path for f in files])
        if there - p["existing"]:
            raise install.InstallError("Destination changed since review. Review a new install plan")
        local = [await c.download(f.url, f.size, f.sha256) for f in files]
        install.vet(files, local, f.listing_id(x), f.version_id(v))  # IBIGS: not held, strictly Halo 3 content
        for f_, path in zip(files, local):
            # Upstream path validation is lexical. Additionally reject symlinks at the web trust boundary.
            if isinstance(target, install.LocalTarget):
                root = target.root.resolve()
                dest = (root / f_.path).resolve()
                if not dest.is_relative_to(root) or (root / f_.path).is_symlink():
                    raise install.InstallError("Content path escapes through a symlink")
            await target.put(path, f_)
        entry = {"listing_id": f.listing_id(x), "version_id": f.version_id(v), "version": f.version_label(v),
                 "title": f.title_of(x), "kind": f.kind_of(x), "author": f.author_of(x), "owner_id": f.owner_of(x),
                 "reference": f.pick(p["manifest"], "reference", "map_reference", "name", default=""),
                 "files": [{k: value for k, value in dataclasses.asdict(z).items() if k != "url"} for z in files],
                 "content_dir": s.content_dir, "installed_at": f.utc_iso()}
        h.fstate.record([h.servers[i].where for i in p["sharing"]], entry)
        h.emit({"kind": "forge-state", "state": h.forge_state()})
        h.emit({"kind": "installed", "index": index, "entry": entry, "sharing": p["sharing"]})
        h.emit({"kind": "log", "station": "FORGE", "text": f"Installed {entry['title']}: {len(files)} verified files", "color": "green"})


async def watch(h):
    if not h.forge or not h.fstate.ids():
        return
    c = h.forge
    changes, as_of = await c.changes(f.before(h.fstate.since() or f.utc_iso(), 300))
    changed = set()
    keys = []
    for change in changes:
        key, lid = f.change_key(change), f.change_listing(change)
        keys.append(key)
        if not h.fstate.seen(key) and lid in h.fstate.ids():
            c.forget(lid)
            changed.add(lid)
    reconcile_due = time.time() - h.fstate.data.get("reconciled", 0) > 6 * 3600
    if reconcile_due:
        changed |= h.fstate.ids()
    for lid in changed:
        x = await c.listing(lid, background=True)
        news = False
        if f.is_withdrawn(x):
            news = h.fstate.withdraw(lid)
        else:
            h.fstate.restore(lid)
            installed = h.fstate.versions(lid)
            versions = f.versions_of(x)
            for v in versions:
                if f.version_id(v) in installed and f.is_withdrawn(v):
                    news |= h.fstate.withdraw(lid, f.version_id(v))
            latest = f.latest_of(x)
            dates = {f.version_id(v): f.when_of(v) for v in versions}
            when = f.when_of(latest) if latest else None
            if latest and not (when and installed and all(dates.get(v) and dates[v] >= when for v in installed)):
                behind = h.fstate.newer(lid, f.version_id(latest), f.version_label(latest))
                if behind:
                    h.emit({"kind": "toast", "title": "FORGE · UPDATE", "text": f"New version of {f.title_of(x)} available", "severity": "information", "timeout": 20})
        if news:
            h.emit({"kind": "toast", "title": "FORGE · WITHDRAWN", "text": f"{f.title_of(x)} is installed and has a withdrawal. Review F6.", "severity": "warning", "timeout": 30})
        h.emit({"kind": "forge-refresh", "lid": lid})
    h.fstate.saw(keys, as_of)
    if reconcile_due:
        h.fstate.data["reconciled"] = time.time()
    h.fstate.save()
    h.emit({"kind": "forge-state", "state": h.forge_state()})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


async def write(c, method, path, body=None, raw=None):
    """Writes are deliberate and single attempt, with no credential-bearing redirects."""
    def request():
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else b""
        req = c._request(c.url(path))
        req.method, req.data = method, data
        req.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        try:
            with build_opener(NoRedirect()).open(req, timeout=30) as r:
                payload = r.read(f.MAX_JSON + 1)
                if len(payload) > f.MAX_JSON:
                    raise f.ForgeError(502, "Response too large")
                return json.loads(payload) if payload else {}
        except HTTPError as e:
            raise f.ForgeError(e.code, f.detail_of(e.read(4096))) from None
        except (URLError, OSError) as e:
            raise f.ForgeError(0, "Forge did not answer; the write was not resent") from e
    return await asyncio.to_thread(request)


async def publish(h, lid, info, files):
    if h.demo:
        raise ValueError("Demo Forge is read/install only; publishing requires a real Forge account")
    c = client(h)
    if not files:
        raise ValueError("Choose files to publish first")
    original_id = lid
    x = inner(await write(c, "POST", "/api/listings", info)) if not lid else inner(await write(c, "POST",
        f"/api/listings/{quote(lid, safe='')}/versions", {k: info.get(k) for k in ("version_label", "release_notes")}))
    made = f.listing_id(x)
    vid = next((f.version_id(v) for v in f.versions_of(x) if v.get("status") == "draft"), "")
    if not made or not vid:
        raise f.ForgeError(502, "Forge returned no draft version to upload to")
    root = f"/api/listings/{quote(made, safe='')}"
    try:
        for name, payload in files:
            upload = await write(c, "POST", root + f"/versions/{quote(vid, safe='')}/uploads", {
                "original_filename": name, "file_size": len(payload), "file_type": "auto"})
            uid = str(f.pick(upload, "id", "upload_id", default=""))
            chunk = int(upload.get("chunk_size", 8 * 1024 * 1024))
            offset, index = int(upload.get("received_size", 0)), int(upload.get("next_chunk_index", 0))
            if not uid or not 1 <= chunk <= 100 * 1024 * 1024 or not 0 <= offset <= len(payload) or index < 0:
                raise f.ForgeError(502, "Invalid upload session")
            while offset < len(payload):
                end = min(offset + chunk, len(payload))
                reply = await write(c, "PUT", f"/api/files/uploads/{quote(uid, safe='')}/chunks/{index}", raw=payload[offset:end])
                got = int(reply.get("received_size", end))
                if not offset < got <= end:
                    raise f.ForgeError(502, "Upload stopped moving forward")
                offset, index = got, int(reply.get("next_chunk_index", index + 1))
            await write(c, "POST", f"/api/listings/uploads/{quote(uid, safe='')}/complete")
        result = await write(c, "POST", root + f"/versions/{quote(vid, safe='')}/publish")
        c.forget(made)
        return {"listing": inner(result)}
    except Exception:
        cleanup = root if original_id is None else root + f"/versions/{quote(vid, safe='')}"
        try:
            await write(c, "DELETE", cleanup)
        except Exception:
            h.emit({"kind": "toast", "title": "FORGE · DRAFT RETAINED", "text": "Publishing failed and its draft could not be removed. Review MINE in Forge.", "severity": "warning", "timeout": 30})
        raise
