import asyncio
import hashlib
import json
import sys

import pytest
from rich.text import Text

from oni_rcon import app as appmod
from oni_rcon.app import (JOIN_PING, Boot, Form, Help, OniApp, Pick, emblem, explain, parse_command, redact_data, render_event,
                          resolve, same, sides, split_tag, target_of)
from oni_rcon.art import biosig, decrypt, hbar, spark, split_bar
from oni_rcon.config import Server, add_servers, load_config, parse_target, resolve_passwords
from oni_rcon.demo import serve_fakes
from oni_rcon.medals import Medals
from oni_rcon.rcon import Rcon, Tunnel


def test_targets():
    assert parse_target("11774").where == "127.0.0.1:11774"
    assert (parse_target("10.0.0.5:49176", ssh="box").where) == "10.0.0.5:49176 via ssh box"
    assert parse_target("[::1]:5000").host == "::1"
    assert parse_target("wss://rcon.example.org/s1").url == "wss://rcon.example.org/s1"
    with pytest.raises(ValueError):
        Server()
    for bad in ("foo", "host:", "99999", "1.2.3.4:x"):
        with pytest.raises(ValueError):
            parse_target(bad)


def test_config(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text(f'by = "op"\n[defaults]\nssh = "box"\npassword_command = [{json.dumps(sys.executable)}, "-c", "print(42)"]\n'
                 '[[server]]\nport = 1\n[[server]]\nport = 2\nssh = ""\npassword_env = "X_PW"\n')
    monkeypatch.setenv("X_PW", "fromenv")
    by, servers = load_config(p)
    resolve_passwords(servers)
    assert by == "op" and [s.ssh for s in servers] == ["box", ""]
    assert [s.password for s in servers] == ["42", "fromenv"]


def test_add_servers(tmp_path):
    p = tmp_path / "new" / "config.toml"
    add_servers(p, [Server(port=11774, name='Say "hi" \\ 🎮', password="pw")], by="op")
    add_servers(p, [Server(url="wss://x.example/s"), Server(host="::1", port=5, ssh="box", password="secret")],
                remember=False)
    by, servers = load_config(p)
    assert by == "op" and [s.where for s in servers] == ["127.0.0.1:11774", "wss://x.example/s", "::1:5 via ssh box"]
    assert servers[0].name == 'Say "hi" \\ 🎮' and servers[0].password == "pw" and not servers[2].password

    # appended to a hand-written file: its comments stay, and a [defaults] tunnel doesn't leak into a direct server
    p.write_text('# mine\n[defaults]\nssh = "game-box"\n[[server]]\nport = 1\n')
    add_servers(p, [Server(port=2, password="x")])
    assert p.read_text().startswith("# mine")
    assert [s.ssh for s in load_config(p)[1]] == ["game-box", ""]


def test_explain():
    assert explain("[Errno 111] Connect call failed ('127.0.0.1', 1); retry in 4s") .startswith("Nothing answered")
    assert explain("[Errno -2] Name or service not known; retry in 2s", short=True) == "UNKNOWN HOST"
    assert "password" in explain("Wrong password.", "denied")
    assert explain("something new; retry in 8s") == "something new"
    assert explain("[WinError 1225] The remote computer refused the network connection", short=True) == \
        "NOTHING ON THAT PORT"  # Windows words it differently


def test_emblem_fits():
    for rows, cols in [(30, 999), (20, 999), (16, 32), (12, 24), (8, 16), (6, 12)]:
        lines = emblem(rows, cols).split("\n")
        assert 0 < len(lines) <= rows and all(len(line) <= cols for line in lines)
        assert len({len(line) for line in lines}) == 1  # one block: the rows stay aligned when centred
    assert emblem(5).plain == "" and emblem(99, 11).plain == ""


def test_event_lines():
    names = {7: "Viper", 9: "Rook"}
    assert render_event("s1", {"event": "kill", "killer": 7, "victim": 9, "weapon": "sword"}, names).plain.endswith(
        "Viper ✕ Rook  [sword]")
    assert "died" in render_event("s1", {"event": "kill", "victim": 9}, names).plain
    assert "[SERVER] hi" in render_event("s1", {"event": "chat", "channel": "server", "text": "hi"}, {}).plain
    line = render_event("s1", {"event": "ban", "name": "Rook", "address": "1.2.3.4"}, {}).plain
    assert "1.2.3.4" not in line and "BAN  Rook" in line
    assert target_of({"number": 3, "player_id": "abc"}) == "abc" and target_of({"number": 3}) == "#3"


def test_rcon_against_fake():
    async def go():
        (port, *_), keep = await serve_fakes(tick=False)
        events, states = [], []
        rc = Rcon(f"ws://127.0.0.1:{port}", "demo", "pytest", lambda _, e: events.append(e),
                  lambda _, s, d: states.append(s))
        task = asyncio.create_task(rc.run())
        while rc.state != "online":
            await asyncio.sleep(0.01)
        st = await rc.call("status")
        assert st["ok"] and st["data"]["max_players"] == 16
        players = (await rc.call("players"))["data"]["players"]
        victim = players[0]
        r = await rc.call("kick", victim["player_id"], "spawn camping")
        assert r["ok"]
        await asyncio.sleep(0.05)
        assert any(e.get("event") == "kick" for e in events)
        assert (await rc.call("players"))["data"]["count"] == len(players) - 1
        task.cancel()

        bad = Rcon(f"ws://127.0.0.1:{port}", "wrong", "pytest", lambda *_: None, lambda *_: None)
        await asyncio.wait_for(bad.run(), 5)  # returns instead of retrying
        assert bad.state == "denied"

    asyncio.run(go())


def test_app_against_fakes():
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.press("escape")  # skip the splash
            await pilot.resize_terminal(150, 44)
            await pilot.pause()
            assert not isinstance(app.screen, Boot)  # a resize must not replay it
            for _ in range(100):
                await pilot.pause(0.05)
                if all(st.online and "players" in st.data for st in app.stations):
                    break
            assert app.stations[0].label == "Slayer"  # common "Demo Ops | " prefix stripped
            assert app.query_one("#players").row_count == len(app.stations[0].players) > 0
            assert app.query_one("#op-passvote").disabled  # no vote under way

            await pilot.click("#pl-tell")  # the player file's buttons open the same dialogs as the keys
            await until(pilot, lambda: isinstance(app.screen, Form))
            assert isinstance(app.screen, Form)
            await pilot.press("escape")
            await pilot.press("question_mark")
            await until(pilot, lambda: isinstance(app.screen, Help))
            assert isinstance(app.screen, Help)
            await pilot.press("escape")
            await pilot.pause()

            app.on_rcon_event(app.stations[1].rcon, {"type": "event", "event": "chat", "text": "admin [b]help"})
            assert app.stations[1].alerts == 1  # counted on the station not being looked at
            await pilot.press("2")
            await pilot.pause()
            assert app.stations[1].alerts == 0
            await pilot.press("1")
            await pilot.press("f5")
            app.query_one("#cmd").focus()
            app.query_one("#cmd").value = "say hello there"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert any("hello there" in str(e.get("text")) for _, e in app.feed)
    asyncio.run(go())


def test_setup_screen(tmp_path):
    from textual.widgets import Button

    from oni_rcon.wizard import SetupApp, target
    assert target("10.0.0.5", "11774") == "10.0.0.5:11774" and target("box:5", "11774") == "box:5"
    assert target("::1", "5") == "[::1]:5" and target("wss://h/s", "11774") == "wss://h/s"

    async def go():
        ports, keep = await serve_fakes(tick=False)
        cfg = tmp_path / "config.toml"
        app = SetupApp(cfg, [], "op", ask_by=True)
        async with app.run_test(size=(120, 60)) as pilot:
            # by keyboard and waiting on the test itself: clicks and polling budgets depend on the runner's speed
            status = lambda: str(app.query_one("#setup-status").render())

            async def test_and_add(password: str) -> None:
                app.query_one("#password").value = password
                app.query_one("#password").focus()
                await pilot.press("enter")
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

            assert app.query_one("#start").disabled  # nothing to start with yet
            app.query_one("#port").value = str(ports[0])
            await test_and_add("wrong")
            assert "refused the password" in status() and not app.added, status()
            await test_and_add("demo")
            assert app.added, status()
            assert app.query_one("#port").value == str(ports[0] + 1)  # the next server on the box, probably
            app.query_one("#start", Button).press()
            await pilot.pause()
        assert [s.port for s in app.return_value] == [ports[0]]
        by, servers = load_config(cfg)
        assert by == "op" and servers[0].password == "demo"
    asyncio.run(go())


def test_setup_emblem_moves_aside_when_short(tmp_path):
    from oni_rcon.wizard import SetupApp

    async def go():
        app = SetupApp(tmp_path / "config.toml", [], "op", ask_by=True)

        async def where() -> str:  # fit_emblem runs after a refresh, so wait on it rather than a fixed pause
            for _ in range(100):
                await pilot.pause(0.05)
                if app.query_one("#setup-side").display:
                    return "side"
                if str(app.query_one("#setup-emblem").content):
                    return "top"
            return "none"

        async with app.run_test(size=(120, 60)) as pilot:
            assert await where() == "top"
            await pilot.resize_terminal(120, 30)  # Windows Terminal's default: no rows spare, but columns are
            assert await where() == "side" and not app.query_one("#setup-emblem").display
    asyncio.run(go())

def test_self_update(tmp_path, monkeypatch):
    from oni_rcon import update
    exe, old, build = tmp_path / "oni-rcon.exe", tmp_path / "oni-rcon.exe.old", b"new build"
    exe.write_bytes(b"running build")
    rel = {"tag_name": "v99.0.0", "assets": [{"name": "a", "size": len(build), "browser_download_url": "dl",
                                              "digest": "sha256:" + hashlib.sha256(build).hexdigest()}]}
    monkeypatch.setattr(update, "ASSET", "a")
    monkeypatch.setattr(update, "fetch", lambda url, timeout=0: build if url == "dl" else json.dumps(rel).encode())
    monkeypatch.delenv("ONI_RCON_NO_UPDATE", raising=False)
    assert update.check(exe) == "Updated to v99.0.0. Restart oni-rcon to use it."
    assert exe.read_bytes() == build and old.read_bytes() == b"running build"

    rel["tag_name"], rel["assets"][0]["size"] = "v99.0.1", 1  # a cut-off download leaves the exe alone
    assert "update failed" in update.check(exe)
    assert exe.read_bytes() == build and not old.exists()  # last time's copy is cleared on the next start
    assert "uv tool upgrade" in update.check(None)  # not a release build: say how
    rel["tag_name"] = "v0.0.1"
    assert update.check(exe) == ""


def test_medals():
    m = Medals()
    assert m.kill("Viper", "Rook", 0) == []
    assert m.kill("Viper", "Juno", 3) == ["DOUBLE KILL"]
    assert m.kill("Viper", "Atlas", 6.5) == ["TRIPLE KILL"]  # each within 4 s of the one before
    assert m.kill("Viper", "Rook", 20) == []
    assert m.kill("Viper", "Juno", 40) == ["KILLING SPREE"]  # five without dying
    assert m.kill("Rook", "Viper", 41) == ["KILLJOY"]  # and Rook ended it
    assert "Viper" not in m.spree and m.earned["Viper"]["KILLING SPREE"] == 1
    m.kill(None, "Rook", 42)  # a fall ends a spree too, and earns nobody anything
    assert "Rook" not in m.spree
    m.kill("Juno", "Atlas", 50)
    m.new_game()
    assert not m.spree and m.earned["Viper"]["DOUBLE KILL"] == 1  # medals stay for the session


def test_art():
    glyph = biosig("9bb183e1", "#4C8DFF")
    assert glyph == biosig("9bb183e1", "#4C8DFF") and glyph.plain != biosig("9bb183e2", "#4C8DFF").plain
    lines = glyph.plain.split("\n")
    assert len(lines) == 6 and all(len(line) == 12 and line == line[::-1] for line in lines)  # mirrored
    assert spark([0, 1, 2, 4]).plain == "▁▂▄█"
    assert len(split_bar([(3, "red"), (1, "blue")], 24)) == 24 == len(split_bar([(0, "red"), (0, "blue")], 24))
    assert hbar(.5, 10, "red").cell_len == 5 and hbar(1, 10, "red").cell_len == 10
    assert decrypt("O N I", 1) == "O N I" and not any(c.isalpha() for c in decrypt("O N I", 0))
    assert emblem(30, reveal=0).plain.strip() == "" and emblem(30, reveal=1).plain == emblem(30).plain


def test_redaction():
    data = {"address": "203.0.113.9:4000", "players": [{"ip": "203.0.113.4", "name": "Rook"}],
            "note": "seen on 198.51.100.7 before", "ranges": 4}
    shown = json.dumps(redact_data(data, True))
    assert "203.0.113" not in shown and "198.51.100" not in shown and "Rook" in shown and '"ranges": 4' in shown
    assert redact_data(data, False) is data
    line = render_event("s1", {"event": "join", "name": "Rook", "player_id": "9bb183e11570266b42b38755cd37880e",
                               "address": "203.0.113.4"}, {}).plain
    assert "player_id=9bb183e1…" in line and "203.0.113.4" not in line


def test_sides():
    team = lambda t, score: {"team": t, "score": score}
    assert sides([team("blue", 3), team("red", 5), team(1, 2)]) == [("red", 1, 5), ("blue", 2, 5)]  # engine order
    assert sides([team(i, i) for i in range(4)]) == []  # a free-for-all: a team each
    assert sides([{"score": 3}]) == []


def test_engine_ids_read_when_they_arrive():
    names = {3: "Drift", 4: "Lark"}
    ev = resolve({"event": "kill", "killer": 4, "victim": 3, "weapon": "sword"}, names)
    names[3] = "Newcomer"  # the engine hands a leaver's slot to the next player to join
    assert render_event("s1", ev, names).plain.endswith("Lark ✕ Drift  [sword]")


def test_rcon_waits_for_its_tunnel():
    async def go():
        (port, *_), keep = await serve_fakes(tick=False)
        ready = asyncio.Event()
        rc = Rcon(f"ws://127.0.0.1:{port}", "demo", "pytest", lambda *_: None, lambda *_: None, ready)
        task = asyncio.create_task(rc.run())
        await asyncio.sleep(0.2)
        assert (rc.state, rc.detail) == ("connecting", "awaiting SSH tunnel")  # nothing has failed yet
        ready.set()
        while rc.state != "online":
            await asyncio.sleep(0.01)
        for ws in list(keep[0].clients):  # a message this client can't read is skipped; the link stays up
            await ws.send("[1, 2]")
        await asyncio.sleep(0.1)
        assert rc.state == "online" and (await rc.call("status"))["ok"]
        task.cancel()

    asyncio.run(go())


async def settle(app, pilot, *keys):
    for _ in range(100):
        await pilot.pause(0.05)
        if all(st.online and all(k in st.data for k in keys) for st in app.stations):
            return


async def until(pilot, ok, tries=100):  # a fixed pause is a guess on a slow runner: wait for the thing itself
    for _ in range(tries):
        await pilot.pause(0.05)
        if ok():
            return


def plain(widget) -> str:
    return str(widget.render())


def test_app_keeps_the_selection_and_redacts():
    async def go():
        ports, keep = await serve_fakes(tick=False)
        fake = keep[0]
        fake.bans["players"] += [{"id": f"{i:032x}", "name": f"Ban{i}", "reason": "x"} for i in range(4)]
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(160, 48)) as pilot:
            await settle(app, pilot, "players", "bans", "vpn")
            bans = app.query_one("#bans")
            assert str(bans.get_row_at(0)[-1]) == "Admin"  # BY reads the server's banned_by, not the group id
            bans.move_cursor(row=3)
            chosen = app.ban_rows[bans.selected]
            fake.bans["players"].insert(0, {"id": "f" * 32, "name": "New", "reason": "y"})  # someone else bans
            app.fetch(app.cur, "bans")
            await pilot.pause(0.3)
            assert bans.row_count == 7 and app.ban_rows[bans.selected] == chosen  # u still lifts the chosen ban
            assert "192.0.2.44" not in str(app.query_one("#vpn").get_row_at(0))

            await pilot.press("f5")
            app.query_one("#cmd").value = "players"
            await pilot.press("enter")
            await pilot.pause(0.3)
            log = "\n".join(line.text for line in app.query_one("#console-log").lines)
            assert "player_id" in log and "203.0.113." not in log

            other = app.stations[1]  # not the one being looked at
            app.on_rcon_event(other.rcon, {"type": "event", "event": "cheat", "name": "Rook", "text": "speed"})
            assert app.condition()[0] == "RED" and other.alerts == app.unseen == 1
            await pilot.press("f2")  # every station's alerts are in the feed: seen
            assert app.unseen == 0

    asyncio.run(go())


def test_join_ping_comes_from_the_log(tmp_path):
    line = "[S] Bob connected from 203.0.113.9 (player ID ab12cd, ping 77 ms)."
    assert JOIN_PING.search(line).groups() == ("ab12cd", "77") and not JOIN_PING.search("[S] Bob left")
    cfg = tmp_path / "c.toml"
    cfg.write_text('ping_log = "tail -f /log/{port}"\n[[server]]\nport = 5\n[[server]]\nurl = "ws://x/y"\n')
    ours, remote = load_config(cfg)[1]
    assert ours.ping_log == "tail -f /log/5" and remote.ping_log == ""

    async def go():
        ports, keep = await serve_fakes(tick=False)
        pid = keep[0].players[0]["player_id"]
        script = tmp_path / "log.py"
        script.write_text(f"print({line.replace('ab12cd', pid)!r})\n")
        servers = [Server(port=p, password="demo") for p in ports]
        servers[0].ping_log = f'"{sys.executable}" "{script}"'
        app = OniApp(servers, by="pytest", intro=False)
        async with app.run_test(size=(160, 48)) as pilot:
            await settle(app, pilot, "players")
            for _ in range(60):  # the follower starts a process: give it a moment
                await pilot.pause(0.1)
                if app.stations[0].pings:
                    break
            assert app.stations[0].pings == {pid: 77}
            app.paint_players()
            t = app.query_one("#players")
            assert "77" in [str(c) for r in range(t.row_count) for c in t.get_row_at(r)]

    asyncio.run(go())


def test_markup_in_a_name_is_just_a_name():
    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        keep[0].players[0]["name"] = "[/]"
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(160, 48)) as pilot:
            await settle(app, pilot, "players")
            t = app.query_one("#players")
            t.move_cursor(row=next(i for i, k in enumerate(t.ids) if app.row_players[k]["name"] == "[/]"))
            t.focus()
            await pilot.press("k")  # this crashed the console: the name was read as markup in the dialog title
            await pilot.pause(0.2)
            assert isinstance(app.screen, Form) and plain(app.screen.query_one(".dialog-title")) == "KICK · [/]"
            app.on_rcon_event(app.stations[0].rcon, {"type": "event", "event": "chat", "channel": "all", "name": "[/]",
                                                     "text": "admin [b]help[/b]"})
            await pilot.pause(0.2)
            assert app.is_running

    asyncio.run(go())


def test_boot_waits_for_the_tunnel(monkeypatch):
    class SlowTunnel(Tunnel):
        def __init__(self, dest, remotes, on_state):
            super().__init__(dest, remotes, on_state)
            self.local = {r: r[1] for r in remotes}  # no ssh: straight through to the fake server

        async def run(self):
            await asyncio.sleep(3)
            self._set("up")
            self.ready.set()
            await asyncio.Future()

    async def go():
        ports, keep = await serve_fakes(tick=False)
        monkeypatch.setattr(appmod, "Tunnel", SlowTunnel)
        app = OniApp([Server(port=p, password="demo", ssh="box") for p in ports], by="pytest")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(1.2)  # the log is out, the tunnel isn't
            log = plain(app.screen.query_one("#boot-log"))
            assert "AWAITING TUNNEL" in log and "NO CARRIER" not in log and "CLEARANCE" not in log
            for _ in range(60):
                await pilot.pause(0.1)
                if "CLEARANCE" in (log := plain(app.screen.query_one("#boot-log"))):
                    break
            assert "GRANTED" in log

    asyncio.run(go())


def test_reconnect_takes_the_password_again():
    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        app = OniApp([Server(port=ports[0], password="wrong")], by="pytest", intro=False)
        async with app.run_test(size=(160, 48)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                if app.stations[0].rcon.state == "denied":
                    break
            app.op("reconnect")
            await pilot.pause(0.2)
            pw = app.screen.query_one("#field-pw")
            assert isinstance(app.screen, Form) and pw.password  # typed masked
            pw.value = "demo"
            await pilot.press("enter")
            await settle(app, pilot, "players")
            assert app.stations[0].online

    asyncio.run(go())


def test_operations_tab_survives_a_narrow_terminal():
    """F3 at 80 columns used to raise measuring a button grid squeezed to nothing beside the sitrep."""
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(100):
                await pilot.pause(0.05)
                if all(st.online for st in app.stations):
                    break
            await pilot.press("f3")
            await pilot.pause(0.3)
            for size in ((80, 24), (80, 20), (100, 30), (119, 30)):
                await pilot.resize_terminal(*size)
                await pilot.pause(0.2)
                assert all(b.region.width >= 8 for b in app.query(".ops-grid Button")), size
    asyncio.run(go())


def test_feed_holds_still_and_keeps_its_width(monkeypatch):
    monkeypatch.setenv("COLUMNS", "160")  # a terminal's width, which a headless console otherwise lacks
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.press("f2")
            await pilot.pause(0.2)
            await pilot.press("f1")  # shown once and hidden again: a plain RichLog wraps what it gets at 78
            feed, st = app.query_one("#feed"), app.stations[0]
            app.on_rcon_event(st.rcon, {"type": "event", "event": "join", "name": "Wrapper", "note": "x" * 50})
            assert any("Wrapper" in line.text and "x" * 50 in line.text for line in feed.lines)  # ~97 wide, whole
            await pilot.press("f2")
            chat = lambda i: {"type": "event", "event": "chat", "channel": "all", "name": "Rook", "text": f"line {i}"}
            for i in range(120):
                app.on_rcon_event(st.rcon, chat(i))
            await pilot.pause(0.3)
            assert feed.is_vertical_scroll_end  # following the tail
            feed.scroll_to(y=10, animate=False)
            await pilot.pause(0.1)
            for i in range(20):
                app.on_rcon_event(st.rcon, chat(i))
            await pilot.pause(0.3)
            assert feed.scroll_y == 10  # reading back: new lines don't yank the view

            await pilot.press("f5")
            cmd = app.query_one("#cmd")
            for line in ("status", "status", "players"):
                cmd.value = line
                await pilot.press("enter")
            cmd.value = "half-typ"
            await pilot.press("up", "up")
            assert app.history == ["status", "players"] and cmd.value == "status"
            await pilot.press("down", "down")
            assert cmd.value == "half-typ"  # the draft comes back

    asyncio.run(go())


def test_console_lines():
    assert parse_command("say Test, test, this is a test! :D") == ["say", "Test, test, this is a test! :D"]
    assert parse_command("say don't camp") == ["say", "don't camp"]  # an apostrophe isn't an open quote
    assert parse_command('say "all of it"') == ["say", "all of it"]
    assert parse_command('tell "Big Name" it\'s you') == ["tell", "Big Name", "it's you"]
    assert parse_command('ban 9f3a 2h "team killing"') == ["ban", "9f3a", "2h", "team killing"]
    assert parse_command("status") == ["status"] and parse_command("say") == ["say"]
    with pytest.raises(ValueError):
        parse_command('kick "unclosed')


def test_community_tags():
    assert split_tag("ALPHA · Big Team Rockets") == ("ALPHA", "Big Team Rockets")
    assert split_tag("BRAVO | Throwback 4v4") == ("BRAVO", "Throwback 4v4")
    assert split_tag("Plain name") == ("", "Plain name") and split_tag("Trailing · ") == ("", "Trailing · ")
    assert same(Text("Rook", "red"), Text("Rook", "red")) and not same(Text("Rook", "red"), Text("Rook", "blue"))
    assert not same("1", 1)


def fleet(n: int):
    names = [f"{'ALPHA' if i % 2 else 'BRAVO'} · {'Big Team' if i < 2 else f'Server {i}'}" for i in range(n)]
    return [(name, 16, 0) for name in names]


def test_a_fleet():
    async def go():
        ports, keep = await serve_fakes(tick=False, specs=fleet(14))
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest")
        async with app.run_test(size=(160, 48)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, Boot) and "STATIONS 1-14" in plain(app.screen.query_one("#boot-log")))
            log = plain(app.screen.query_one("#boot-log"))  # a line every 0.22 s, and the splash goes after about 2
            assert "STATIONS 1-14" in log and "[14]" not in log  # a tally, not a line each
            await pilot.press("escape")
            await settle(app, pilot, "status")
            assert app.has_class("-fleet") and app.cards[0].size.height == 2
            labels = [st.label for st in app.stations]
            assert labels[2:4] == ["Server 2", "Server 3"] and app.stations[3].tag == "ALPHA"
            assert labels[:2] == ["BRAVO · Big Team", "ALPHA · Big Team"]  # the same name twice keeps its tag

            calls, busy, most = [], 0, 0
            for st in app.stations:  # count how many says are in flight at once: the status polls go on meanwhile
                real = st.rcon.call

                async def slow(command, *args, real=real, **kw):
                    nonlocal busy, most
                    if command != "say":
                        return await real(command, *args, **kw)
                    busy += 1
                    most = max(most, busy)
                    await asyncio.sleep(0.02)
                    busy -= 1
                    calls.append(command)
                    return await real(command, *args, **kw)
                st.rcon.call = slow
            await pilot.press("f5")
            app.query_one("#cmd").value = "@all say it's a test"
            app.query_one("#cmd").focus()
            await pilot.press("enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if calls.count("say") == 14:
                    break
            await pilot.pause(0.2)
            assert calls.count("say") == 14 and most <= appmod.FANOUT
            log = "\n".join(line.text for line in app.query_one("#console-log").lines)
            assert "@all say  ·  14 stations  ·  14 ok" in log
            assert sum("it's a test" in str(e.get("text")) for _, e in app.feed) == 14  # one argument, whole

            await pilot.press("escape")
            app.query_one("#stations").focus()
            await pilot.press("g")
            await pilot.pause(0.2)
            assert isinstance(app.screen, Pick)
            await pilot.press(*"server 9", "enter")
            await pilot.pause(0.2)
            assert app.cur.label == "Server 9"

    asyncio.run(go())


def test_a_big_fleet_only_draws_the_cards_in_view():
    """Redrawing a card Textual isn't showing re-arranges the whole screen: with 200 stations, most of the console's time."""
    async def go():
        ports, keep = await serve_fakes(tick=False, specs=fleet(60))
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot, "status")
            await pilot.pause(0.5)
            stations = app.query_one("#stations")
            assert 0 in app.seen and 59 not in app.seen and len(app.seen) < 20
            assert app.stations[0].drawn and not app.stations[59].drawn  # the last isn't drawn until it's in view
            stations.scroll_end(animate=False)
            await pilot.pause(0.3)
            assert 59 in app.seen and 0 not in app.seen
            assert app.stations[59].drawn
            for key in ("f1", "f2", "f3", "f4", "f5", "f6"):  # and every tab still lays out with a fleet
                await pilot.press(key)
                await pilot.pause(0.2)

    asyncio.run(go())


def test_a_late_reply_is_reported():
    async def go():
        (port, *_), keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        fake, real = keep[0], keep[0].command

        async def slow(cmd, args, by):
            if cmd == "endgame":
                await asyncio.sleep(0.3)
            return await real(cmd, args, by)
        fake.command = slow
        late = []
        rc = Rcon(f"ws://127.0.0.1:{port}", "demo", "pytest", lambda *_: None, lambda *_: None,
                  on_late=lambda _, line, r: late.append((line, r)))
        task = asyncio.create_task(rc.run())
        while rc.state != "online":
            await asyncio.sleep(0.01)
        with pytest.raises(TimeoutError, match="isn't resent"):
            await rc.call("endgame", timeout=0.05)
        await asyncio.sleep(0.5)
        assert late and late[0][0] == "endgame" and late[0][1]["ok"]
        task.cancel()

    asyncio.run(go())
