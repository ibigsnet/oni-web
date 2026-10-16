"""Same-origin HTTP/WebSocket boundary. Run one worker: it owns the fleet and the encrypted vault."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import binascii
import base64
import json
import logging
import os
import re
import secrets
import shutil
import time
import tomllib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from pydantic import ValidationError
from starlette.staticfiles import StaticFiles

from . import forge_service
from .hub import Budget, Hub, LimitError
from .models import Call, Profile
from .settings import Settings, loopback
from .vault import Vault

COOKIE = "oni_session"
SESSION_SECONDS = 8 * 3600
ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("oni_web")


def import_config(settings, vault):
    """Opt-in one-time migration. Never rewrite the original TOML or import executable settings from HTTP."""
    if not settings.config_path or vault.data.get("imported_config"):
        return
    path = Path(settings.config_path).resolve()
    with path.open("rb") as f:
        data = tomllib.load(f)
    profiles = list(vault.data["profiles"])
    for entry in data.get("server", []):
        raw = {**data.get("defaults", {}), **entry}
        public = {k: raw[k] for k in ("name", "host", "port", "url", "ssh", "password") if k in raw}
        validated = Profile(**public).model_dump()
        validated.update({k: raw[k] for k in ("content_dir", "password_env", "password_command", "restart_cmd") if k in raw})
        for k in ("health_cmd", "ping_log"):
            validated[k] = data.get(k, "").replace("{port}", str(validated["port"])) if not validated["url"] else ""
        profiles.append({**validated, "id": uuid.uuid4().hex})
    trusted = {k: v for k, v in data.items() if k.startswith(("health_", "forge_"))}
    key = data.get("forge_api_key", "")
    if not key and data.get("forge_api_key_env"):
        key = os.getenv(data["forge_api_key_env"], "")
    if not key and data.get("forge_api_key_command"):
        from oni_rcon.config import run_command
        key = run_command(data["forge_api_key_command"], "Forge key command")
    vault.update(profiles=profiles, trusted=trusted, by=data.get("by") or "operator", forge_key=key,
                 imported_config=str(path))


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    vault = Vault(settings.data_dir, settings.encryption_key)
    hub = Hub(settings, vault)
    sessions: dict[str, float] = {}
    login_budget, request_budget, connect_budget = Budget(10, 60), Budget(1200, 60), Budget(10, 60)
    uploads: dict[str, list[tuple[str, bytes]]] = {}
    upload_at: dict[str, float] = {}
    upload_lock = asyncio.Lock()
    publish_lock = asyncio.Lock()
    native_lock = asyncio.Lock()

    def expire_uploads():
        for cid, at in list(upload_at.items()):
            if time.monotonic() - at > 600:
                uploads.pop(cid, None)
                upload_at.pop(cid, None)

    @asynccontextmanager
    async def lifespan(app):
        # A process-level lock prevents two workers from racing the vault or using two RCON slots per server.
        # OS file locks work across platforms without a runtime dependency.
        lock_file = open(settings.data_dir / "instance.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.write(b"1")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            lock_file.close()
            raise RuntimeError("Another ONI Web process owns this data directory. Use one worker") from e
        try:
            import_config(settings, vault)
            hub.profiles = [dict(p) for p in vault.data["profiles"]]
            hub.by = vault.data["by"]
            app.state.hub = hub
            yield
        finally:
            await hub.stop()
            lock_file.close()

    app = FastAPI(title="ONI Web", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.hub, app.state.settings = hub, settings

    def authorized(conn):
        if not settings.token:
            # No-token mode is for a direct loopback connection. Proxies must configure a token.
            return bool(conn.client and loopback(conn.client.host))
        auth = conn.headers.get("authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), settings.token.encode()):
            return True
        sid = conn.cookies.get(COOKIE, "")
        return sessions.get(sid, 0) > time.time()

    @app.middleware("http")
    async def boundary(request, call_next):
        origin = request.headers.get("origin")
        authority = request.headers.get("host", "")
        allowed_hosts = {urlsplit(x).netloc for x in settings.origins}
        # Exact authority checking also protects loopback deployments from DNS rebinding.
        if authority not in allowed_hosts or (origin and origin not in settings.origins):
            return JSONResponse({"detail": "Origin or Host is not allowed"}, 403)
        if request.url.path.startswith("/api/"):
            if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-oni-request") != "1":
                return JSONResponse({"detail": "Missing request protection header"}, 403)
            if request.url.path not in ("/api/session", "/api/login") and not authorized(request):
                return JSONResponse({"detail": "Sign in to continue"}, 401)
            try:
                request_budget.take(request.client.host if request.client else "unknown")
            except LimitError as e:
                return JSONResponse({"detail": str(e)}, 429, headers={"Retry-After": "60"})
            # Reject oversized JSON before Pydantic or JSON parsing. Streaming uploads have their own bound.
            if request.method in ("POST", "PUT", "PATCH") and request.url.path not in ("/api/uploads", "/api/server-upload"):
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        return JSONResponse({"detail": "Request exceeds 2 MB"}, 413)
                request._body = bytes(body)
        try:
            response = await call_next(request)
        except Exception as exc:
            # Catch before ServerErrorMiddleware can rethrow a credential-bearing
            # upstream exception into the ASGI server's traceback logger.
            log.error("Request failed: %s", type(exc).__name__)
            response = JSONResponse({"detail": "Operation failed. Check server availability and backend logs"}, 500)
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        })
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/forge/image/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; font-src 'self'; media-src 'self'; connect-src 'self' ws: wss:; "
                "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        return response

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid(request, exc):
        # Validation errors normally include the raw input, which may contain a password.
        return JSONResponse({"detail": "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())}, 422)

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({"detail": hub.clean(str(exc))}, 400)

    @app.exception_handler(LimitError)
    async def limited(request, exc):
        return JSONResponse({"detail": str(exc)}, 429, headers={"Retry-After": "60"})

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        # Never log request bodies, connection URLs, or exception reprs containing credentials.
        log.error("Request failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "Operation failed. Check server availability and backend logs"}, 500)

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": authorized(request), "login_required": bool(settings.token),
                "demo": settings.allow_demo}

    @app.post("/api/login")
    async def login(request: Request):
        login_budget.take(request.client.host if request.client else "unknown")
        body = await request.json()
        token = body.get("token", "") if isinstance(body, dict) else ""
        if not isinstance(token, str) or not settings.token or not hmac.compare_digest(token.encode(), settings.token.encode()):
            raise HTTPException(401, "Invalid access token")
        for sid in list(sessions):
            if sessions[sid] <= time.time():
                del sessions[sid]
        if len(sessions) >= 100:
            raise LimitError("Too many active sessions")
        sid = secrets.token_urlsafe(32)
        sessions[sid] = time.time() + SESSION_SECONDS
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, sid, max_age=SESSION_SECONDS, httponly=True,
                            secure=settings.secure_cookie, samesite="strict", path="/")
        return response

    # IBIGS 2026-10-16: one-click sign-in from the hosting panel. the hosting panel checks that the signed-in customer owns
    # this server, then sends the browser here with a ticket it signed: base64url(JSON {sid, exp, n}) "." hex
    # HMAC-SHA256 under ONI_WEB_SSO_SECRET (shared only by the hosting panel and this server). The ticket must name this
    # server (ONI_WEB_SSO_SERVER), expire within two minutes and be unused; it then opens the same session a
    # token login does. The answer is a page that moves on by itself: a same-site navigation, so the SameSite=Strict
    # session cookie is sent (a redirect inside the panel's cross-site click would drop it).
    sso_seen: dict[str, float] = {}

    @app.get("/sso")
    async def sso(request: Request, ticket: str = ""):
        login_budget.take(request.client.host if request.client else "unknown")
        secret = os.getenv("ONI_WEB_SSO_SECRET", "")
        server = os.getenv("ONI_WEB_SSO_SERVER", "")
        now = time.time()
        try:
            if len(secret) < 32 or not server or len(ticket) > 600 or "." not in ticket:
                raise ValueError
            body, sig = ticket.rsplit(".", 1)
            good = hmac.new(secret.encode(), body.encode(), "sha256").hexdigest()
            if not hmac.compare_digest(sig.encode(), good.encode()):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            nonce = str(data.get("n", ""))
            if (str(data.get("sid")) != server or not (now < float(data.get("exp", 0)) <= now + 120)
                    or len(nonce) < 16 or nonce in sso_seen):
                raise ValueError
        except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
            return HTMLResponse("<!doctype html><title>Sign-in link expired</title><body style=\"font-family:sans-serif;"
                                "background:#111;color:#ddd;padding:3em\"><h2>This sign-in link has expired</h2>"
                                "<p>Open the console again from your server's page in the panel.</p>", 403)
        for n in [n for n, t in sso_seen.items() if t <= now]:
            del sso_seen[n]
        sso_seen[nonce] = float(data["exp"]) + 1
        for sid in list(sessions):
            if sessions[sid] <= now:
                del sessions[sid]
        if len(sessions) >= 100:
            raise LimitError("Too many active sessions")
        sid = secrets.token_urlsafe(32)
        sessions[sid] = now + SESSION_SECONDS
        page = ("<!doctype html><meta charset=utf-8><meta http-equiv=\"refresh\" content=\"0;url=/\">"
                "<title>Signing in…</title><body style=\"background:#111\">")
        response = HTMLResponse(page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.set_cookie(COOKIE, sid, max_age=SESSION_SECONDS, httponly=True,
                            secure=settings.secure_cookie, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        sid = request.cookies.get(COOKIE, "")
        sessions.pop(sid, None)
        uploads.pop(request.headers.get("x-oni-client", ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/profiles")
    async def profiles():
        return {"profiles": hub.public_profiles(), "by": hub.by}

    @app.post("/api/profiles")
    async def add_profile(profile: Profile):
        return await hub.put_profile(profile)

    @app.put("/api/profiles/{id_}")
    async def edit_profile(id_: str, profile: Profile):
        return await hub.put_profile(profile, id_)

    @app.delete("/api/profiles/{id_}")
    async def delete_profile(id_: str):
        await hub.delete_profile(id_)
        return {"ok": True}

    @app.post("/api/uploads")
    async def upload(request: Request):
        async with upload_lock:
            return await read_upload(request)

    async def read_upload(request):
        # Browser file pickers supply bytes, never paths on the backend's filesystem.
        expire_uploads()
        cid = request.headers.get("x-oni-client", "")
        filename = request.headers.get("x-oni-filename", "")
        from urllib.parse import unquote
        filename = unquote(filename)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()+\-\[\]]{0,127}", filename):
            raise ValueError("Choose a file with a simple filename")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", cid):
            raise ValueError("Missing client identifier")
        if len(uploads) >= 16 and cid not in uploads:
            raise LimitError("Too many pending uploads")
        files = uploads.setdefault(cid, [])
        upload_at[cid] = time.monotonic()
        if any(n == filename for n, _ in files):
            raise ValueError("The same filename was already selected")
        payload = bytearray()
        async with asyncio.timeout(120):
            async for chunk in request.stream():
                payload.extend(chunk)
                if len(payload) + sum(len(b) for fs in uploads.values() for _, b in fs) > 100 * 1024 * 1024:
                    raise HTTPException(413, "Pending uploads exceed 100 MiB. Clear or publish the selected files")
        if not payload or len(files) >= 10 or sum(len(b) for _, b in files) + len(payload) > 100 * 1024 * 1024:
            raise ValueError("Upload 1–10 nonempty files totaling at most 100 MiB")
        files.append((filename, bytes(payload)))
        return {"name": filename, "size": len(payload)}

    # IBIGS 2026-10-16: "upload to my server". Files stream to disk (never memory) in the server's Upload folder,
    # mounted at ONI_WEB_SERVER_UPLOAD_DIR; the server's own sorter (ibigs-content-upload) then puts mods, server
    # files, lobby settings and Forge content where they belong and leaves a note in Upload/done or Upload/failed.
    # A batch (one file or one folder) is written under .web-<batch>/ and only moved into view when it is complete.
    SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+\-\[\]',&!#@~]{0,127}")

    def upload_root() -> Path:
        root = os.getenv("ONI_WEB_SERVER_UPLOAD_DIR", "")
        if not root or not Path(root).is_dir():
            raise HTTPException(404, "Uploading to this server isn't set up")
        return Path(root)

    def batch_dir(root: Path, batch: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", batch):
            raise ValueError("Bad upload batch")
        return root / f".web-{batch}"

    @app.get("/api/server-upload")
    async def server_upload_status():
        root = upload_root()
        results = []
        for state in ("done", "failed"):
            folder = root / state
            if not folder.is_dir():
                continue
            for note in folder.glob("*.txt"):
                with contextlib.suppress(OSError):
                    results.append({"state": state, "name": note.name[:-4], "at": note.stat().st_mtime,
                                    "text": note.read_text(errors="replace")[:2000]})
        waiting = sorted(p.name for p in root.iterdir() if p.name not in ("done", "failed") and not p.name.startswith("."))
        free = shutil.disk_usage(root).free
        return {"results": sorted(results, key=lambda r: r["at"], reverse=True)[:30], "waiting": waiting,
                "free_bytes": free}

    @app.post("/api/server-upload")
    async def server_upload(request: Request):
        root = upload_root()
        batch = request.headers.get("x-oni-batch", "")
        from urllib.parse import unquote
        rel = unquote(request.headers.get("x-oni-path", ""))
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        if not parts or len(parts) > 12 or not all(SEGMENT.fullmatch(p) and p not in (".", "..") for p in parts):
            raise ValueError("Use simple file and folder names (letters, numbers, spaces, . _ - ( ) [ ])")
        size = int(request.headers.get("content-length") or 0)
        if size <= 0 or size > 32 * 1024 ** 3:
            raise ValueError("Each file must be between 1 byte and 32 GB")
        if shutil.disk_usage(root).free < size + 1024 ** 3:
            raise ValueError("Not enough free disk space on the server for this file")
        base = batch_dir(root, batch)
        target = base.joinpath(*parts)
        base.mkdir(mode=0o775, exist_ok=True)
        target.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f"{rel} was already sent in this upload")
        written = 0
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o664)
        try:
            async for chunk in request.stream():
                written += len(chunk)
                if written > size:
                    raise ValueError("The file is bigger than announced")
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
        except BaseException:
            os.close(fd)
            fd = -1
            with contextlib.suppress(OSError):
                target.unlink()
            raise
        finally:
            if fd >= 0:
                os.close(fd)
        return {"path": "/".join(parts), "size": written}

    @app.post("/api/server-upload/done")
    async def server_upload_done(request: Request):
        root = upload_root()
        a = await request.json()
        base = batch_dir(root, str(a.get("batch", "")))
        if not base.is_dir():
            raise ValueError("Nothing was uploaded in this batch")
        moved = []
        for item in sorted(base.iterdir()):
            dest = root / item.name
            n = 2
            while dest.exists():
                stem, dot, ext = item.name.rpartition(".") if item.is_file() else (item.name, "", "")
                dest = root / (f"{stem}-{n}.{ext}" if dot else f"{item.name}-{n}")
                n += 1
            # older timestamps: the sorter waits for folders to settle; this one is complete
            for p in [item, *item.rglob("*")] if item.is_dir() else [item]:
                with contextlib.suppress(OSError):
                    os.utime(p, (time.time() - 120, time.time() - 120), follow_symlinks=False)
            item.rename(dest)
            moved.append(dest.name)
        shutil.rmtree(base, ignore_errors=True)
        return {"queued": moved}

    @app.post("/api/server-upload/cancel")
    async def server_upload_cancel(request: Request):
        root = upload_root()
        a = await request.json()
        shutil.rmtree(batch_dir(root, str(a.get("batch", ""))), ignore_errors=True)
        return {"ok": True}

    # IBIGS 2026-10-16: small previews of a Forge listing's screenshots. Forge serves only full-size PNGs (2560x1440,
    # ~650 KB); this fetches one from Forge itself (fixed https origin, no key: they are public), shrinks it to a
    # 480 px JPEG and keeps it on disk, so browsers get ~30 KB from this console instead.
    thumb_gate = asyncio.Semaphore(3)
    UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    def make_preview(url: str, out: Path) -> None:
        from io import BytesIO
        from urllib.request import Request as UrlRequest, urlopen as url_open
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 40_000_000
        with url_open(UrlRequest(url, headers={"User-Agent": "oni-web (IBIGS)"}), timeout=20) as r:
            if not str(r.headers.get("content-type", "")).startswith("image/"):
                raise ValueError("not an image")
            raw = r.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("image too big")
        with Image.open(BytesIO(raw)) as im:
            im.draft("RGB", (960, 540))
            im = im.convert("RGB")
            im.thumbnail((480, 270))
            buf = BytesIO()
            im.save(buf, "JPEG", quality=78, optimize=True, progressive=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(buf.getvalue())
        tmp.replace(out)
        kept = sorted(out.parent.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for old in kept[:-400]:
            with contextlib.suppress(OSError):
                old.unlink()

    @app.get("/api/forge/image/{lid}/{image}")
    async def forge_image(lid: str, image: str, v: str = ""):
        from oni_rcon.forge import FORGE_URL
        if not UUID.fullmatch(lid) or not (image == "thumb" or UUID.fullmatch(image)) or not re.fullmatch(r"\d{0,24}", v):
            raise HTTPException(404, "No such image")
        folder = settings.data_dir / "forge-previews"
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{lid}-{image}-{v or 0}.jpg"
        if not out.exists():
            path = f"/api/listings/{lid}/thumbnail" + (f"?v={v}" if v else "") if image == "thumb" else \
                f"/api/listings/{lid}/images/{image}"
            async with thumb_gate:
                if not out.exists():
                    try:
                        await asyncio.to_thread(make_preview, FORGE_URL + path, out)
                    except Exception as e:  # Forge down, 404, not an image: the panel just shows no picture
                        log.info("forge preview %s/%s: %s", lid, image, type(e).__name__)
                        raise HTTPException(404, "No preview") from None
        return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/rpc/{name}")
    async def rpc(name: str, request: Request):
        args = await request.json()
        if not isinstance(args, dict):
            raise ValueError("Arguments must be an object")
        cid = request.headers.get("x-oni-client", "")
        result = await dispatch(name, args, cid)
        return JSONResponse(hub.clean(result))

    async def dispatch(name, a, cid):
        if "index" in a or "indices" in a:
            generation = a.pop("_generation", None)
            if not hub.running or generation != hub.generation:
                raise HTTPException(409, "The active fleet changed. Reopen the console before operating a station")
        if name == "launch_info":
            return {"mode": "console" if hub.running else "setup", "version": "web-1.0.0", "error": "",
                    "setup": {"by": hub.by, "existing": hub.public_profiles(), "path": "encrypted vault", "ask_by": False}}
        if name == "connection_states":
            return {"generation": hub.generation, "links": [hub.links.get(i, {"state": "connecting"})
                                                               for i in range(len(hub.clients))]}
        if name in ("host_status", "setup_host", "setup_find"):
            from .native import invoke as native
            action = {"host_status": "status", "setup_host": "host", "setup_find": "find"}[name]
            if action != "status":
                connect_budget.take("native")
            if native_lock.locked():
                raise LimitError("A native hosting/discovery operation is already running")
            async with native_lock:
                return await native(hub, action, a)
        if name == "start_console":
            demo = a.get("demo", False)
            if not isinstance(demo, bool) or demo and not settings.allow_demo:
                raise ValueError("Demo is disabled or invalid")
            ids = a.get("ids")
            if ids is not None and (not isinstance(ids, list) or any(not isinstance(x, str) for x in ids)):
                raise ValueError("ids must be a list of profile IDs")
            # Browser refreshes rejoin the existing fleet, preserving demo or a quick-connect subset.
            if hub.running and not demo and ids is None and "by" not in a:
                return hub.info()
            connect_budget.take("fleet")
            by = a.get("by", hub.by)
            if not isinstance(by, str) or not 1 <= len(by.strip()) <= 64 or any(ord(c) < 32 for c in by):
                raise ValueError("Operator name must be 1–64 printable characters")
            info = await hub.start(demo, ids, by.strip())
            vault.update(by=hub.by)
            return info
        if name == "setup_open":
            # Going to profiles does not disrupt other browsers or upstream connections.
            return None
        if name == "call":
            return await hub.call(Call(**a))
        if name == "automation_call":
            if hub.automation_owner != cid or hub.automation_until <= time.monotonic():
                raise ValueError("This browser does not own the automation lease")
            call = Call(**a)
            if call.command in ("say", "tell"):
                hub.automation_budget.take(str(call.index))
            return await hub.call(call)
        if name == "reconnect":
            connect_budget.take(str(a.get("index")))
            return await hub.reconnect(a["index"], a.get("password"))
        if name == "disconnect":
            return await hub.disconnect(a["index"])
        if name == "disconnect_all":
            async with hub.lock:
                await hub.stop()
                hub.emit({"kind": "reload", "text": "Fleet disconnected by operator"})
            return None
        if name == "set_password":
            for i in a["indices"]:
                connect_budget.take(str(i))
                await hub.reconnect(i, a["password"])
            return None
        if name == "set_labels":
            labels = a.get("labels", [])
            if isinstance(labels, list) and len(labels) == len(hub.servers) and all(isinstance(x, str) and len(x) < 1024 for x in labels):
                hub.labels = labels
            return None
        if name == "health_now":
            request_budget.take("health")
            await hub.health_now()
            return None
        if name == "health_view":
            return hub.health_view()
        if name == "forge_set_key":
            key = a.get("key", "")
            if not isinstance(key, str) or len(key) > 4096:
                raise ValueError("Invalid Forge key")
            hub.session_forge_key = key
            if a.get("remember"):
                vault.update(forge_key=key)
            forge_service.connect_forge(hub)
            return {"status": forge_service.forge_status(hub), "warn": ""}
        if name == "forge_status":
            return forge_service.forge_status(hub)
        if name == "forge_state":
            return hub.forge_state()
        if name == "forge_ack":
            hub.fstate.acknowledge(a["lid"])
            hub.emit({"kind": "forge-state", "state": hub.forge_state()})
            return None
        if name == "forge_publish":
            if publish_lock.locked():
                raise LimitError("Another publish is in progress")
            async with publish_lock:
                expire_uploads()
                a["_files"] = uploads.pop(cid, [])
                upload_at.pop(cid, None)
                return await forge_service.dispatch(hub, name, a)
        if name in {"forge_listings", "forge_favourites", "forge_listing", "forge_mine", "forge_plan", "forge_put", "forge_write", "forge_publish"}:
            return await forge_service.dispatch(hub, name, a)
        if name == "upload_clear":
            uploads.pop(cid, None)
            upload_at.pop(cid, None)
            return None
        if name == "prefs_intro":
            prefs = {**vault.data["prefs"]}
            for k, v in (("full_intro", a.get("full")), ("intro_seen", a.get("seen"))):
                if isinstance(v, bool):
                    prefs[k] = v
            vault.update(prefs=prefs)
            return prefs
        if name == "automation_load":
            return {"rules": vault.data["rules"], "roles": vault.data["roles"],
                    "webhooks": {k: "••••••" for k in vault.data["webhooks"]}}
        if name == "automation_save":
            if not isinstance(a.get("rules"), list) or not isinstance(a.get("roles"), dict):
                raise ValueError("Rules must be a list and roles a table")
            vault.update(rules=a["rules"], roles=a["roles"])
            hub.emit({"kind": "automation-changed"})
            return None
        if name == "automation_lease":
            if not cid or len(cid) > 64:
                raise ValueError("Missing client identifier")
            if hub.automation_owner == cid or hub.automation_until <= time.monotonic():
                hub.automation_owner, hub.automation_until = cid, time.monotonic() + 15
                return {"owner": True}
            return {"owner": False}
        if name in ("discord_add", "discord_remove", "discord_post"):
            from .operations import discord
            return await discord(hub, name, a)
        if name == "automation_discord":
            if hub.automation_owner != cid or hub.automation_until <= time.monotonic():
                raise ValueError("This browser does not own the automation lease")
            from .operations import discord
            return await discord(hub, "discord_post", a)
        if name == "remove_server":
            hub.station(a["index"])
            if hub.demo:
                raise ValueError("Demo stations are not saved profiles")
            await hub.delete_profile(hub.profile_ids[a["index"]])
            return None
        if name in ("playlists", "playlist_library", "playlist_read", "save_playlist", "save_game_type", "server_setting", "restart_server"):
            from .operations import dispatch as operations
            return await operations(hub, name, a)
        if name == "log_error":
            log.warning("Browser reported an error")
            return None
        if name == "update_check":
            return ""  # deploy web updates as a versioned backend, never an in-browser native updater
        raise HTTPException(404, "Unknown operation")

    @app.websocket("/api/events")
    async def events(ws: WebSocket):
        # Cookie auth alone is insufficient: browsers attach cookies to cross-site WebSocket handshakes.
        if (ws.headers.get("origin") not in settings.origins
                or ws.headers.get("host") not in {urlsplit(x).netloc for x in settings.origins}
                or not authorized(ws)):
            await ws.close(code=1008)
            return
        await ws.accept()
        q = None
        receive_task = None
        queue_task = None
        try:
            after = max(0, int(ws.query_params.get("after", "0")))
            q, snapshot = hub.subscribe(after)
            await ws.send_json(snapshot)
            receive_task = asyncio.create_task(ws.receive())
            while authorized(ws):
                queue_task = asyncio.create_task(q.get())
                done, _ = await asyncio.wait((queue_task, receive_task), timeout=15,
                                             return_when=asyncio.FIRST_COMPLETED)
                if receive_task in done:
                    # The event stream is receive-only from the browser's perspective.
                    if receive_task.result().get("type") != "websocket.disconnect":
                        await ws.close(code=1008, reason="Use HTTP for commands")
                    return
                if queue_task not in done:
                    queue_task.cancel()
                    await asyncio.gather(queue_task, return_exceptions=True)
                    await ws.send_json([{"kind": "heartbeat", "generation": hub.generation}])
                    continue
                first = queue_task.result()
                if first["kind"] == "overflow":
                    await ws.close(code=1013, reason="Event queue full; reconnect to resync")
                    return
                batch = [first]
                await asyncio.sleep(0.04)
                while not q.empty() and len(batch) < 250:
                    batch.append(q.get_nowait())
                await ws.send_json(batch)
            await ws.close(code=1008, reason="Session expired")
        except (WebSocketDisconnect, RuntimeError):
            pass
        except (ValueError, LimitError):
            await ws.close(code=1008)
        finally:
            pending = [t for t in (receive_task, queue_task) if t is not None]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if q:
                hub.subscribers.discard(q)

    dist = ROOT / "frontend" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Unknown endpoint")
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
        return Response("Frontend is not built. Run npm install --prefix frontend and npm run build, or use Vite on port 5173.", media_type="text/plain", status_code=503)

    return app
