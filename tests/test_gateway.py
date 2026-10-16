"""Exercise the real HTTP gateway and upstream WebSocket peers using loopback only.

Standard-library HTTP avoids a separate in-process test transport hiding middleware behavior.
"""
import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import uvicorn
from cryptography.fernet import Fernet
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from oni_rcon.rcon import Rcon, free_port
from oni_web.app import create_app
from oni_web.hub import Hub, LimitError
from oni_web.models import Call, Profile
from oni_web.operations import check_playlist, dispatch
from oni_web.performance import Performance
from oni_web.settings import Settings
from oni_web.vault import Vault


def run(coro):
    return asyncio.run(coro)


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


@asynccontextmanager
async def gateway(tmp_path, token="", commands=180):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(port=port, data_dir=tmp_path, token=token, origins=(base,), commands_per_minute=commands)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False, proxy_headers=False))
    task = asyncio.create_task(server.serve())
    await until(lambda: server.started or task.done())
    if task.done():
        await task
    try:
        yield app, base
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def http(base, path, method="GET", data=None, cookie="", headers=None):
    def go():
        body = json.dumps(data).encode() if data is not None else None
        request = Request(base + path, data=body, method=method,
            headers={"X-ONI-Request": "1", "Content-Type": "application/json", "Cookie": cookie, **(headers or {})})
        try:
            response = urlopen(request, timeout=15)
        except HTTPError as e:
            response = e
        with response:
            payload = response.read()
            try:
                value = json.loads(payload)
            except ValueError:
                value = payload.decode()
            return response.status, value, dict(response.headers)
    return await asyncio.to_thread(go)


def test_profiles_encrypted_and_never_returned(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as (app, base):
            password = "a-unique-secret-credential"
            status, profile, _ = await http(base, "/api/profiles", "POST", {"host": "localhost", "password": password})
            assert status == 200 and profile["has_password"]
            assert password not in json.dumps(profile) and "password" not in profile
            id_ = profile["id"]
            assert password.encode() not in (tmp_path / "vault.enc").read_bytes()
            assert Vault(tmp_path).data["profiles"][0]["password"] == password
            status, changed, _ = await http(base, f"/api/profiles/{id_}", "PUT", {"host": "localhost", "name": "Renamed", "password": None})
            assert status == 200 and changed["has_password"] and changed["name"] == "Renamed"
            assert app.state.hub.profiles[0]["password"] == password
            status, _, _ = await http(base, f"/api/profiles/{id_}", "DELETE")
            assert status == 200
            assert (await http(base, "/api/profiles"))[1]["profiles"] == []
    run(scenario())


def test_unremembered_credentials_do_not_persist(tmp_path):
    async def scenario():
        h = Hub(Settings(data_dir=tmp_path), Vault(tmp_path))
        await h.put_profile(Profile(password="session-only", remember=False))
        assert h.profiles[0]["password"] == "session-only"
        assert Vault(tmp_path).data["profiles"][0]["password"] == ""
    run(scenario())


def test_wrong_vault_key_preserves_file(tmp_path):
    v = Vault(tmp_path)
    v.update(by="Example")
    original = v.path.read_bytes()
    with pytest.raises(RuntimeError, match="decrypt"):
        Vault(tmp_path, Fernet.generate_key().decode())
    assert v.path.read_bytes() == original


def test_auth_origin_host_csrf_and_websocket(tmp_path):
    async def scenario():
        token = "long-web-application-token-123456789"
        async with gateway(tmp_path, token) as (app, base):
            assert (await http(base, "/api/profiles"))[0] == 401
            assert (await http(base, "/api/login", "POST", {"token": token}, headers={"Origin": "https://evil.example"}))[0] == 403
            assert (await http(base, "/api/login", "POST", {"token": token}, headers={"X-ONI-Request": ""}))[0] == 403
            assert (await http(base, "/api/session", headers={"Host": "evil.example"}))[0] == 403
            assert (await http(base, "/api/login", "POST", {"token": "bad"}))[0] == 401
            status, body, headers = await http(base, "/api/login", "POST", {"token": token})
            assert status == 200 and token not in json.dumps(body)
            cookie_header = next(v for k, v in headers.items() if k.lower() == "set-cookie")
            cookie = cookie_header.split(";")[0]
            assert "HttpOnly" in cookie_header and "SameSite=strict" in cookie_header
            assert (await http(base, "/api/profiles", cookie=cookie))[0] == 200
            wsurl = base.replace("http:", "ws:") + "/api/events"
            with pytest.raises(InvalidStatus):
                async with connect(wsurl, origin="https://evil.example", additional_headers={"Cookie": cookie}, proxy=None):
                    pass
            async with connect(wsurl, origin=base, additional_headers={"Cookie": cookie}, proxy=None) as ws:
                snapshot = json.loads(await ws.recv())
                assert snapshot[0]["kind"] == "stream"
                app.state.hub.emit({"kind": "event", "index": None, "ev": {"event": "chat", "text": token}})
                batch = json.loads(await asyncio.wait_for(ws.recv(), 2))
                assert token not in json.dumps(batch) and "[redacted]" in json.dumps(batch)
            assert (await http(base, "/api/logout", "POST", {}, cookie))[0] == 200
            assert (await http(base, "/api/profiles", cookie=cookie))[0] == 401
    run(scenario())


def test_validation_never_echoes_password_or_accepts_executable_settings(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as (_, base):
            for profile in ({"host": "bad/host", "password": "must-not-appear"},
                            {"ssh": "-oProxyCommand=evil", "password": "must-not-appear"},
                            {"host": "localhost", "restart_cmd": "echo evil", "password": "must-not-appear"},
                            {"url": "ws://user:must-not-appear@example.org", "password": "must-not-appear"}):
                status, body, _ = await http(base, "/api/profiles", "POST", profile)
                assert status == 422 and "must-not-appear" not in json.dumps(body)
            assert (await http(base, "/api/rpc/arbitrary_shell", "POST", {"command": "echo evil"}))[0] == 404
    run(scenario())


def test_wire_auth_ids_arguments_fragmentation_large_payload_and_events():
    async def scenario():
        seen, events = [], []
        large = "response\n" * 200000
        async def peer(ws):
            auth = json.loads(await ws.recv())
            assert auth == {"type": "auth", "password": "wire-secret"}
            await ws.send(json.dumps({"type": "auth", "ok": True, "version": "0.9.7"}))
            requests = [json.loads(await ws.recv()), json.loads(await ws.recv())]
            seen.extend(requests)
            await ws.send(json.dumps({"type": "event", "event": "chat", "text": "live"}))
            # Fragment one JSON message across WebSocket frames, and reply out of order.
            for req in reversed(requests):
                raw = json.dumps({"type": "reply", "id": req["id"], "ok": True, "text": large if req["command"] == "big" else "other"})
                await ws.send([raw[:100], raw[100:]])
            await ws.wait_closed()
        async with serve(peer, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            r = Rcon(url, "wire-secret", "Operator", lambda _, ev: events.append(ev), lambda *_: None)
            task = asyncio.create_task(r.run())
            try:
                await until(lambda: r.ws is not None)
                a, b = await asyncio.gather(r.call("big", "arbitrary text", 42), r.call("small"))
                assert a["text"] == large and b["text"] == "other"
                assert seen[0]["args"] == ["arbitrary text", "42"]
                assert seen[0]["by"] == "Operator" and seen[0]["id"] != seen[1]["id"]
                assert events[0]["text"] == "live"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(scenario())


def test_refused_auth_is_terminal_and_closes_socket():
    async def scenario():
        attempts = []
        closed = asyncio.Event()
        async def peer(ws):
            attempts.append(await ws.recv())
            await ws.send('{"type":"auth","ok":false,"error":"wrong password"}')
            await ws.wait_closed()
            closed.set()
        async with serve(peer, "127.0.0.1", 0) as server:
            r = Rcon(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "bad", "op", lambda *_: None, lambda *_: None)
            await asyncio.wait_for(r.run(), 2)
            await asyncio.wait_for(closed.wait(), 2)
            assert r.state == "denied" and len(attempts) == 1
    run(scenario())


def test_timeout_late_reply_never_resends():
    async def scenario():
        late, sent = [], []
        async def peer(ws):
            await ws.recv()
            await ws.send('{"type":"auth","ok":true}')
            req = json.loads(await ws.recv())
            sent.append(req)
            await asyncio.sleep(.1)
            await ws.send(json.dumps({"type": "reply", "id": req["id"], "ok": True, "text": "ran once"}))
            await ws.wait_closed()
        async with serve(peer, "127.0.0.1", 0) as server:
            r = Rcon(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "p", "op", lambda *_: None, lambda *_: None,
                     on_late=lambda _, line, reply: late.append((line, reply)))
            task = asyncio.create_task(r.run())
            try:
                await until(lambda: r.ws is not None)
                with pytest.raises(TimeoutError, match="isn't resent"):
                    await r.call("say", "hello", timeout=.02)
                await until(lambda: bool(late))
                assert late[0][0] == "say hello" and len(sent) == 1 and not r._pending
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(scenario())


def test_shared_connection_rate_limits_and_demo_cleanup(tmp_path):
    async def scenario():
        async with gateway(tmp_path, commands=2) as (app, base):
            result = await http(base, "/api/rpc/start_console", "POST", {"demo": True})
            assert result[0] == 200
            h = app.state.hub
            await until(lambda: all(c.ws for c in h.clients))
            clients = list(h.clients)
            again = await http(base, "/api/rpc/start_console", "POST", {"demo": True})
            assert again[0] == 200 and h.clients == clients
            args = {"index": 0, "_generation": result[1]["generation"]}
            assert (await http(base, "/api/rpc/call", "POST", {**args, "command": "status"}))[1]["ok"]
            assert (await http(base, "/api/rpc/call", "POST", {**args, "command": "players"}))[1]["ok"]
            assert (await http(base, "/api/rpc/call", "POST", {**args, "command": "help"}))[0] == 429
            assert (await http(base, "/api/rpc/call", "POST", {**args, "_generation": 0, "command": "say", "args": ["stale"]}))[0] == 409
            assert (await http(base, "/api/rpc/health_view", "POST", {}))[0] == 200
            await h.stop()
            assert not h.clients and not h.tasks and h.demo_forge is None and h.scratch is None
    run(scenario())


def test_file_operations_playlist_and_symlink_boundary(tmp_path):
    async def scenario():
        from oni_rcon.config import Server
        root = tmp_path / "server"
        content = root / "content"
        content.mkdir(parents=True)
        h = Hub(Settings(data_dir=tmp_path / "state"), Vault(tmp_path / "state"))
        h.clients = [object()]
        h.servers = [Server(port=49176, content_dir=str(content))]
        playlist = {"name": "Friday Night", "games": [{"map": m, "game": "Slayer"} for m in ("Guardian", "Valhalla", "The Pit")]}
        a = {"index": 0, "playlist": playlist, "replace": False}
        first = await dispatch(h, "save_playlist", a)
        assert not first["exists"]
        assert (await dispatch(h, "save_playlist", a))["exists"]
        value = await dispatch(h, "playlist_read", {"index": 0, "path": first["path"]})
        assert value["version"] == 2 and len(value["games"]) == 3
        with pytest.raises(Exception):
            await dispatch(h, "playlist_read", {"index": 0, "path": "content/../../vault.key"})
        with pytest.raises(ValueError):
            check_playlist({**playlist, "games": [playlist["games"][0]] * 3})
    run(scenario())


def test_frame_rate_matches_desktop_calculation():
    p = Performance()
    text = "host now=1000000 clock=13:46:40\n13:46:30 Server falling behind: under 54 frames a second for 60 of the last 60 seconds (29 on average, longest frame 125 ms) with 16 players"
    note = p.ingest(text, 1000000)
    r = p.reading(1000000)
    assert note.startswith("FALLING BEHIND")
    assert r["fps"] == 29 and r["behind"] and r["frame_ms"] == 125
    assert len(r["history"]) == 30
    p.ingest(text, 1000000)
    assert len(p.minutes) == 1
    assert p.reading(1000200)["fps"] == 60


def test_remote_bind_requires_authentication():
    with pytest.raises(ValueError, match="TOKEN"):
        Settings(host="0.0.0.0")
    with pytest.raises(ValueError, match="TOKEN"):
        Settings(host="127.0.0.1", origins=("https://oni.example.org",))


def test_unexpected_errors_do_not_leak_credentials_into_responses_or_logs(tmp_path, monkeypatch, caplog):
    async def scenario():
        async with gateway(tmp_path) as (app, base):
            h = app.state.hub
            h.running, h.generation = True, 1
            async def fail(_):
                raise RuntimeError("sensitive-command-output-do-not-log")
            monkeypatch.setattr(h, "call", fail)
            status, body, _ = await http(base, "/api/rpc/call", "POST", {"index": 0, "_generation": 1, "command": "status"})
            assert status == 500
            assert "sensitive-command-output-do-not-log" not in json.dumps(body)
            assert "sensitive-command-output-do-not-log" not in caplog.text
    run(scenario())


def sso_ticket(secret, server, exp, nonce="n" * 22):
    import base64, hashlib, hmac as _hmac
    body = base64.urlsafe_b64encode(json.dumps({"sid": server, "exp": exp, "n": nonce}).encode()).decode().rstrip("=")
    return body + "." + _hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def test_panel_sso_ticket_opens_one_session(tmp_path, monkeypatch):
    import time
    secret = "s" * 48
    monkeypatch.setenv("ONI_WEB_SSO_SECRET", secret)
    monkeypatch.setenv("ONI_WEB_SSO_SERVER", "15")

    async def scenario():
        async with gateway(tmp_path, token="t" * 40) as (app, base):
            assert (await http(base, "/api/profiles"))[0] == 401
            good = sso_ticket(secret, "15", time.time() + 60)
            status, page, headers = await http(base, "/sso?ticket=" + good)
            assert status == 200 and "url=/" in page
            cookie = headers["set-cookie"].split(";", 1)[0]
            assert "HttpOnly" in headers["set-cookie"] and "SameSite=strict" in headers["set-cookie"]
            assert (await http(base, "/api/profiles", cookie=cookie))[0] == 200
            assert (await http(base, "/sso?ticket=" + good))[0] == 403                                  # single use
            assert (await http(base, "/sso?ticket=" + sso_ticket(secret, "16", time.time() + 60, "a" * 22)))[0] == 403  # other server
            assert (await http(base, "/sso?ticket=" + sso_ticket(secret, "15", time.time() - 1, "b" * 22)))[0] == 403  # expired
            assert (await http(base, "/sso?ticket=" + sso_ticket(secret, "15", time.time() + 999, "c" * 22)))[0] == 403  # too long
            assert (await http(base, "/sso?ticket=" + sso_ticket("x" * 48, "15", time.time() + 60, "d" * 22)))[0] == 403  # wrong key
            assert (await http(base, "/sso?ticket=garbage"))[0] == 403
    run(scenario())


def test_panel_sso_off_without_secret(tmp_path, monkeypatch):
    import time
    monkeypatch.delenv("ONI_WEB_SSO_SECRET", raising=False)
    monkeypatch.setenv("ONI_WEB_SSO_SERVER", "15")

    async def scenario():
        async with gateway(tmp_path, token="t" * 40) as (app, base):
            assert (await http(base, "/sso?ticket=" + sso_ticket("", "15", time.time() + 60)))[0] == 403
    run(scenario())
