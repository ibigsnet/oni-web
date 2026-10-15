import json
from pathlib import Path

import pytest

from oni_web.hub import Hub
from oni_web.native import invoke, remember_found
from oni_web.settings import Settings
from oni_web.vault import Vault
from test_gateway import run


def test_native_helper_is_opt_in_and_does_not_accept_browser_paths(tmp_path):
    async def scenario():
        h = Hub(Settings(data_dir=tmp_path), Vault(tmp_path))
        assert not (await invoke(h, "status", {}))["enabled"]
        with pytest.raises(ValueError, match="disabled"):
            await invoke(h, "host", {"executable": "browser-path"})
    run(scenario())


def test_found_native_credentials_are_backend_only_and_templates_preserved(tmp_path):
    async def scenario():
        h = Hub(Settings(data_dir=tmp_path), Vault(tmp_path))
        data = {"servers": [{"host": "127.0.0.1", "port": 49176, "ssh": "operator@game-box",
                             "name": "Found server", "password": "generated-rcon-secret", "content_dir": "/srv/game/content",
                             "password_command": ["ssh", "operator@game-box", "fixed backend command"],
                             "restart_cmd": "docker restart game-1"}], "notes": [],
                "templates": [["health_cmd", '"docker logs --since {since} game-{port}"']]}
        result = await remember_found(h, data, True)
        assert result["added"] == 1 and "generated-rcon-secret" not in json.dumps(result)
        assert "generated-rcon-secret" not in json.dumps(h.public_profiles())
        assert b"generated-rcon-secret" not in (tmp_path / "vault.enc").read_bytes()
        assert h.profiles[0]["health_cmd"] == "docker logs --since {since} game-49176"
        assert h.profiles[0]["restart_cmd"] == "docker restart game-1"
        assert (await remember_found(h, data, True))["added"] == 0
    run(scenario())


def test_native_helper_receives_stdin_and_streams_only_public_progress(tmp_path, monkeypatch):
    async def scenario():
        executable = tmp_path / "helper.exe"
        executable.write_bytes(b"not executed by this test")
        h = Hub(Settings(data_dir=tmp_path / "data", native_helper=str(executable.resolve())), Vault(tmp_path / "data"))
        sent = []
        class Input:
            def write(self, data): sent.append(json.loads(data))
            async def drain(self): pass
            def close(self): pass
        async def lines():
            yield b'{"type":"progress","text":"Preparing game box"}\n'
            yield b'{"type":"result","ok":true,"data":{"servers":[],"notes":[],"templates":[]}}\n'
        class Process:
            stdin = Input()
            stdout = lines()
            returncode = 0
            async def wait(self): return 0
        async def spawn(*args, **kwargs):
            assert args == (str(executable.resolve()),)
            assert "shell" not in kwargs
            return Process()
        monkeypatch.setattr("oni_web.native.asyncio.create_subprocess_exec", spawn)
        q, _ = h.subscribe()
        result = await invoke(h, "find", {"dest": "operator@game-box"})
        assert result["added"] == 0
        assert sent == [{"action": "find", "dest": "operator@game-box"}]
        assert (await q.get())["kind"] == "setup-line"
        h.subscribers.discard(q)
    run(scenario())
