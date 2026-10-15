import asyncio
import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import serve

from oni_rcon import forge
from oni_rcon.rcon import Rcon
from oni_web import forge_service
from oni_web.app import create_app
from oni_web.hub import Hub, LimitError
from oni_web.models import Profile
from oni_web.settings import Settings
from oni_web.vault import Vault
from test_gateway import gateway, http, run, until


def test_forge_demo_manifest_install_and_single_use_plan(tmp_path):
    async def scenario():
        h = Hub(Settings(data_dir=tmp_path), Vault(tmp_path))
        try:
            await h.start(demo=True)
            await until(lambda: all(c.ws for c in h.clients))
            catalog = await forge_service.dispatch(h, "forge_listings", {"sort": "latest"})
            assert catalog["items"]
            summary = catalog["items"][0]
            full = (await forge_service.dispatch(h, "forge_listing", {"lid": forge.listing_id(summary)}))["listing"]
            version = forge.latest_of(full)
            plan = await forge_service.dispatch(h, "forge_plan", {"index": 0, "listing": full, "version": version})
            assert "plan" in plan, plan
            await forge_service.dispatch(h, "forge_put", {"plan": plan["plan"]})
            installed = h.fstate.installed(h.servers[0].where)
            assert forge.listing_id(full) in installed
            for file in installed[forge.listing_id(full)]["files"]:
                path = Path(h.servers[0].content_dir) / file["path"]
                assert path.is_file() and forge.file_sha(path) == file["sha256"]
            with pytest.raises(ValueError, match="expired"):
                await forge_service.dispatch(h, "forge_put", {"plan": plan["plan"]})
        finally:
            await h.stop()
    run(scenario())


def test_forge_publish_uses_original_upload_shape_and_cleans_failed_drafts(tmp_path, monkeypatch):
    async def scenario():
        h = Hub(Settings(data_dir=tmp_path), Vault(tmp_path))
        h.forge = SimpleNamespace(forget=lambda _: None)
        calls = []
        async def write(c, method, path, body=None, raw=None):
            calls.append((method, path, body, raw))
            if path == "/api/listings":
                return {"id": "listing", "versions": [{"id": "version", "status": "draft"}]}
            if path.endswith("/uploads"):
                assert body == {"original_filename": "map.json", "file_size": 6, "file_type": "auto"}
                return {"id": "upload", "chunk_size": 4}
            if "/chunks/0" in path:
                return {"received_size": 4, "next_chunk_index": 1}
            if "/chunks/1" in path:
                return {"received_size": 6, "next_chunk_index": 2}
            if path.endswith("/publish"):
                raise forge.ForgeError(422, "invalid file")
            return {}
        monkeypatch.setattr(forge_service, "write", write)
        with pytest.raises(forge.ForgeError):
            await forge_service.publish(h, None, {"title": "Map", "version_label": "1.0"}, [("map.json", b"abcdef")])
        assert [raw for _, path, _, raw in calls if "/chunks/" in path] == [b"abcd", b"ef"]
        assert calls[-1][:2] == ("DELETE", "/api/listings/listing")
    run(scenario())


def test_automation_lease_has_one_owner_and_backend_chat_limit(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as (app, base):
            h = app.state.hub
            first = {"X-ONI-Client": "tab-one"}
            second = {"X-ONI-Client": "tab-two"}
            assert (await http(base, "/api/rpc/automation_lease", "POST", {}, headers=first))[1]["owner"]
            assert not (await http(base, "/api/rpc/automation_lease", "POST", {}, headers=second))[1]["owner"]
            h.automation_until = time.monotonic() - 1
            assert (await http(base, "/api/rpc/automation_lease", "POST", {}, headers=second))[1]["owner"]
            assert (await http(base, "/api/rpc/automation_discord", "POST", {"name": "alerts", "text": "test"}, headers=first))[0] == 400
            for _ in range(20):
                h.automation_budget.take("server")
            with pytest.raises(LimitError):
                h.automation_budget.take("server")
    run(scenario())


def test_profile_edits_apply_on_next_connect_and_readonly_refresh_preserves_subset(tmp_path):
    async def scenario():
        async def peer(ws):
            await ws.recv()
            await ws.send('{"type":"auth","ok":true,"version":"test"}')
            await ws.wait_closed()
        async with serve(peer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with gateway(tmp_path) as (app, base):
                _, profile, _ = await http(base, "/api/profiles", "POST", {"port": port, "password": "first-secret"})
                _, info, _ = await http(base, "/api/rpc/start_console", "POST", {"ids": [profile["id"]]})
                h = app.state.hub
                await until(lambda: h.clients[0].ws is not None)
                initial = h.clients[0]
                refreshed = (await http(base, "/api/rpc/start_console", "POST", {}))[1]
                assert refreshed["generation"] == info["generation"] and h.clients[0] is initial
                await http(base, f"/api/profiles/{profile['id']}", "PUT", {"port": port, "password": "second-secret"})
                _, changed, _ = await http(base, "/api/rpc/start_console", "POST", {"ids": [profile["id"]]})
                assert changed["generation"] > info["generation"]
                assert h.clients[0] is not initial and h.clients[0].password == "second-secret"
    run(scenario())


def test_reconnect_fails_pending_commands_without_resending():
    async def scenario():
        attempts, commands = [], []
        async def peer(ws):
            attempts.append(await ws.recv())
            await ws.send('{"type":"auth","ok":true}')
            if len(attempts) == 1:
                commands.append(json.loads(await ws.recv()))
                await ws.close()
            else:
                await ws.wait_closed()
        async with serve(peer, "127.0.0.1", 0) as server:
            r = Rcon(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "password", "op", lambda *_: None, lambda *_: None)
            task = asyncio.create_task(r.run())
            try:
                await until(lambda: r.ws is not None)
                with pytest.raises(ConnectionError, match="connection lost"):
                    await r.call("kick", "Player", "reason")
                await until(lambda: len(attempts) == 2, timeout=5)
                assert len(commands) == 1 and not r._pending
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(scenario())


def test_trusted_config_import_is_once_encrypted_and_preserves_original(tmp_path):
    async def scenario():
        path = tmp_path / "original.toml"
        text = 'by = "Imported Operator"\nhealth_cmd = "echo host mem_available_mb=8192"\n[[server]]\nport = 49176\npassword = "original-secret"\nrestart_cmd = "echo restart"\ncontent_dir = "./server/content"\n'
        path.write_text(text)
        settings = Settings(data_dir=tmp_path / "data", config_path=str(path))
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            assert app.state.hub.profiles[0]["restart_cmd"] == "echo restart"
            assert app.state.hub.profiles[0]["password"] == "original-secret"
            assert app.state.hub.by == "Imported Operator"
        assert path.read_text() == text
        assert b"original-secret" not in (settings.data_dir / "vault.enc").read_bytes()
        app2 = create_app(settings)
        async with app2.router.lifespan_context(app2):
            assert len(app2.state.hub.profiles) == 1
    run(scenario())


def test_cli_preserves_free_text_and_quoted_arguments():
    import importlib.util
    spec = importlib.util.spec_from_file_location("oni_cli", Path(__file__).parents[1] / "scripts/command.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.parse_line("say it's a trap") == ["say", "it's a trap"]
    assert mod.parse_line('tell "Echo 4" please switch teams') == ["tell", "Echo 4", "please switch teams"]
    assert mod.parse_line('map "The Pit"') == ["map", "The Pit"]
