import asyncio
import io
import json
import sys

import pytest
from rich.console import Console
from rich.text import Text

from oni_rcon import health as hc
from oni_rcon.app import OniApp, Station, id_forms, render_event, resolve
from oni_rcon.config import Server, health_settings, load_config
from oni_rcon.demo import FakeHost, serve_fakes
from oni_rcon.health import BARE, OOM, RESTART, SIGNATURE, HealthSetup, Monitor

T0 = hc.parse_ts("2026-10-10T08:15:00Z")
EXC = "Experimental startup exception 0xC0000005 in halo3.dll at RVA 0x14C73E"
PROBE = "Error: Engine probe worker failed: exit code: 1"
STOP = "08:15:01 [Slayer] stopped (exit code: 1); restarting in 5 s."
PLAYER_ID = "ab12" * 16  # 64 hex digits
JOIN = f"[Slayer] Bob connected from 203.0.113.9 (player ID {PLAYER_ID}, ping 77 ms)."


def at(now_clock="08:15:20", now=T0 + 20):
    return hc.clock_for({"now": now, "clock": now_clock}, now)


def host(avail=4000, oom=0, now=T0, swap=(100, 2048)):
    return (f"host now={int(now)} clock={hc.time.strftime('%H:%M:%S', hc.time.gmtime(now))} mem_available_mb={avail} "
            f"swap_used_mb={swap[0]} swap_total_mb={swap[1]} oom_kill={oom}\n")


def box(started="2026-10-10T07:00:00Z", oom="false", code=0, finished="0001-01-01T00:00:00Z"):
    return f"container started={started} finished={finished} oom_killed={oom} exit_code={code} restarts=0\n"


# --- what a crash looks like ---------------------------------------------------------------------------------------
def test_the_three_log_shapes_are_told_apart():
    (sig,) = hc.crashes_in([EXC, PROBE, STOP], at())  # the incident as the server logs it: no timestamps but the stop's
    assert (sig.cls, sig.code, sig.module, sig.rva, sig.exit) == (SIGNATURE, "0xC0000005", "halo3.dll", "0x14C73E", 1)
    assert sig.ts == T0 + 1 and sig.detail == "0xC0000005 halo3.dll+0x14C73E"

    (bare,) = hc.crashes_in([PROBE, STOP], at())  # only the engine probe line: a different fault
    assert bare.cls == BARE and bare.code == "" and "no exception" in bare.detail

    # an exception with no RVA isn't the signature
    (odd,) = hc.crashes_in(["Experimental startup exception 0xC0000005 in halo3.dll", PROBE, STOP], at())
    assert odd.cls == BARE

    # a stop with no crash behind it, and a crash that follows another, each count for what they are
    assert hc.crashes_in([STOP, "[Slayer] map loaded"], at()) == []
    got = hc.crashes_in([EXC, PROBE, STOP, PROBE, "08:15:09 [Slayer] stopped (exit code: 1); restarting in 5 s."], at())
    assert [c.cls for c in got] == [SIGNATURE, BARE] and got[1].ts == T0 + 9

    # docker --timestamps: the time on the line wins, and the bare clock needs no help
    stamped = [f"2026-10-10T08:15:01.123456789Z {EXC}", f"2026-10-10T08:15:01.2Z {PROBE}", f"2026-10-10T08:15:01.3Z {STOP}"]
    (s2,) = hc.crashes_in(stamped, hc.clock_for({}, 0))
    assert s2.cls == SIGNATURE and abs(s2.ts - (T0 + 1.3)) < 0.01

    # a crash cut off before its stop line is still a crash, if the log gave its time
    cut = hc.crashes_in([f"2026-10-10T08:15:01Z {EXC}", f"2026-10-10T08:15:01Z {PROBE}"], at())
    assert [c.cls for c in cut] == [SIGNATURE]


def test_repeats_are_one_crash():
    # lines the log says twice, inside one crash
    (one,) = hc.crashes_in([EXC, EXC, PROBE, PROBE, STOP], at())
    assert one.cls == SIGNATURE

    # the same log read by two polls, the second a second later: a bare clock time can come out a second apart
    m = Monitor(started=T0 - 1000)
    key = "0"
    log = "\n".join([EXC, PROBE, STOP])
    assert len(m.ingest(key, "", host(now=T0 + 20) + log, now=T0 + 20)) == 1  # news the once
    assert len(m.events) == 1
    assert m.ingest(key, "", host(now=T0 + 80) + log, now=T0 + 80) == []
    assert m.ingest(key, "", host(now=T0 + 141) + log, now=T0 + 141) == []
    assert len(m.events) == 1 and m.events[0].cls == SIGNATURE and m.events[0].ts == T0 + 1


def test_ids_and_addresses_never_survive():
    rep = hc.parse_report(host() + JOIN + "\n" + EXC + "\n" + f"{PLAYER_ID} 10.0.0.7\n")
    text = " ".join(rep.lines)
    assert "203.0.113.9" not in text and PLAYER_ID not in text and "10.0.0.7" not in text and "[ip]" in text
    m = Monitor(started=T0 - 1000)
    m.ingest("0", "", host() + JOIN + "\n" + "\n".join([EXC, PROBE, STOP]), now=T0 + 20)
    assert PLAYER_ID not in repr(m.__dict__) and "203.0.113.9" not in repr(m.__dict__)
    assert hc.scrub(f"ssh: connect to host 198.51.100.4 port 22 (id {PLAYER_ID})") == "ssh: connect to host [ip] port 22 (id [id])"


# --- thresholds ----------------------------------------------------------------------------------------------------
def test_threshold_colours():
    assert [hc.mem_level(v, 1024) for v in (None, 4000, 2048, 2047, 1024, 1023, 0)] == ["", "ok", "ok", "warn", "warn", "crit", "crit"]
    assert hc.mem_level(700, 500) == "warn" and hc.mem_level(499, 500) == "crit"
    assert [hc.swap_level(u, 2048) for u in (0, 511, 512, 1023, 1024)] == ["ok", "ok", "warn", "warn", "crit"]
    assert hc.swap_level(5, 0) == "" and hc.swap_level(None, 2048) == ""


def test_low_memory_alerts_once_and_rearms():
    m = Monitor(1024, started=T0)
    assert m.ingest("0", "", host(3000), now=T0) == []
    (n,) = m.ingest("0", "", host(900, now=T0 + 60), now=T0 + 60)
    assert n.level == "crit" and n.key == "" and "LOW MEMORY  900 MB" in n.text and m.short()
    assert m.ingest("0", "", host(800, now=T0 + 120), now=T0 + 120) == []  # still low: said once
    assert m.ingest("0", "", host(1050, now=T0 + 180), now=T0 + 180) == [] and m.short()  # barely over: not eased yet
    assert m.ingest("0", "", host(1500, now=T0 + 240), now=T0 + 240) == [] and not m.short()
    assert len(m.ingest("0", "", host(500, now=T0 + 300), now=T0 + 300)) == 1  # and it can alert again
    # the box it names can be an ssh destination with an address in it: not in the banner
    (n,) = Monitor(1024).ingest("0", "admin@198.51.100.4", host(100), now=T0)
    assert "198.51.100.4" not in n.text and "[ip]" in n.text


# --- news, OOM, restarts, who was on -------------------------------------------------------------------------------
def test_only_a_new_crash_is_news():
    m = Monitor(started=T0 + 100)
    old = "\n".join([f"2026-10-10T08:15:01Z {EXC}", f"2026-10-10T08:15:01Z {PROBE}", f"2026-10-10T08:15:01Z {STOP}"])
    assert m.ingest("0", "", host(now=T0 + 110) + old, now=T0 + 110) == [] and len(m.events) == 1  # before we opened: history
    new = "\n".join([f"2026-10-10T08:20:00Z {PROBE}", f"2026-10-10T08:20:00Z 08:20:00 [Slayer] stopped (exit code: 1); restarting in 5 s."])
    (n,) = m.ingest("0", "", host(now=T0 + 400) + old + "\n" + new, now=T0 + 400)
    assert "BARE CRASH" in n.text and len(m.events) == 2

    # the very first report can carry a crash from after we opened
    m2 = Monitor(started=T0)
    assert len(m2.ingest("0", "", host() + old, now=T0 + 30)) == 1


def test_oom_kills_and_restarts():
    m = Monitor(started=T0)
    m.ingest("0", "h", host(now=T0) + box(), now=T0)
    # a container the kernel killed: OOMKilled, exit 137
    dead = box(oom="true", code=137, finished="2026-10-10T08:16:00Z")
    (n,) = m.ingest("0", "h", host(now=T0 + 60) + dead, now=T0 + 60)
    (e,) = m.events
    assert (e.cls, e.exit) == (OOM, 137) and "OOM-KILL" in n.text and e.detail == "exit 137"
    assert m.ingest("0", "h", host(now=T0 + 120) + dead, now=T0 + 120) == [] and len(m.events) == 1  # same kill, seen again
    # it comes back up: that's the OOM's restart, not another event
    m.ingest("0", "h", host(now=T0 + 180) + box(started="2026-10-10T08:16:01Z"), now=T0 + 180)
    assert [e.cls for e in m.events] == [OOM]
    # a restart for no reason the log or the kernel gave
    (n,) = m.ingest("0", "h", host(now=T0 + 600) + box(started="2026-10-10T08:24:00Z"), now=T0 + 600)
    assert [e.cls for e in m.events] == [OOM, RESTART] and n.level == "warn"
    # a container that died long ago isn't news, and isn't news again when the next report says so
    old = box(oom="true", code=137, finished="2026-10-08T08:00:00Z")
    assert m.ingest("2", "h", old, now=T0 + 700) == [] and m.ingest("2", "h", old, now=T0 + 760) == []
    assert not [e for e in m.events if e.key == "2"]
    # exit 137 with no flag is read as the kill it is
    m.ingest("1", "h", box(code=137, finished="2026-10-10T08:30:00Z"), now=T0 + 900)
    assert [e.cls for e in m.recent() if e.key == "1"] == [OOM]


def test_the_hosts_counter_and_the_containers_flag_are_one_kill():
    m = Monitor(started=T0)
    m.ingest("0", "h", host(oom=2, now=T0) + box(), now=T0)
    (n,) = m.ingest("0", "h", host(oom=3, now=T0 + 60) + box(), now=T0 + 60)  # the counter first: the host
    assert n.key == "" and "OOM-KILL" in n.text and [e.key for e in m.events] == [""]
    # then another server's container shows it: it names the server, and it isn't announced twice
    assert m.ingest("1", "h", host(oom=3, now=T0 + 65) + box(oom="true", code=137, finished="2026-10-10T08:15:58Z"),
                    now=T0 + 65) == []
    assert [(e.cls, e.key) for e in m.events] == [(OOM, "1")]
    # and the other way round
    m2 = Monitor(started=T0)
    m2.ingest("1", "h", box(oom="true", code=137, finished="2026-10-10T08:15:58Z"), now=T0 + 5)
    m2.ingest("0", "h", host(oom=1, now=T0) + box(), now=T0 + 10)
    assert m2.ingest("0", "h", host(oom=2, now=T0 + 20) + box(), now=T0 + 20) == [] and [e.key for e in m2.events] == ["1"]


def test_player_count_comes_from_before_the_drop():
    m = Monitor(started=T0 - 500)
    for dt, n in ((-40, 11), (-10, 14), (30, 0)):  # the roster before, and the empty one after the restart
        m.sample("0", n, now=T0 + dt)
    m.dropped("0", now=T0 + 1)
    log = "\n".join([f"2026-10-10T08:15:01Z {EXC}", f"2026-10-10T08:15:01Z {PROBE}", f"2026-10-10T08:15:01Z {STOP}"])
    (n,) = m.ingest("0", "", host() + log, now=T0 + 40)
    (e,) = m.events
    assert (e.players, e.rcon) == (14, True) and "14 players dropped" in n.text
    # a drop that's noticed after the log has already been read still joins the two
    m.sample("1", 6, now=T0 - 5)
    m.ingest("1", "", host() + log, now=T0 + 40)
    assert [(e.players, e.rcon) for e in m.events if e.key == "1"] == [(6, False)]
    m.dropped("1", now=T0 + 2)
    assert [(e.players, e.rcon) for e in m.events if e.key == "1"] == [(6, True)]
    # no samples, or ones too old to be the crowd: unknown, not zero
    m.ingest("2", "", host() + log, now=T0 + 40)
    assert [e.players for e in m.events if e.key == "2"] == [None]


def test_reports_in_either_shape():
    rep = hc.parse_report(host(812, 2) + json.dumps({"kind": "container", "started": "2026-10-10T07:00:00Z",
                                                     "oom_killed": True, "exit_code": 137}) + "\nsome log line\n")
    assert rep.host["mem_available_mb"] == 812 and rep.host["oom_kill"] == 2 and rep.lines == ["some log line"]
    assert rep.box.oom_killed and rep.box.exit_code == 137
    rep = hc.parse_report(json.dumps({"kind": "host", "mem_available_mb": 512, "swap_total_mb": 0}) + "\n")
    assert rep.host == {"mem_available_mb": 512, "swap_total_mb": 0}
    junk = hc.parse_report("host mem_available_mb=lots oom_kill=\n{not json\n\n")
    assert junk.host == {} and junk.lines == ["{not json"]
    # a bare clock with only the host's own clock to go on counts back from it, across midnight too
    f = hc.clock_for({"now": T0 + 3600 * 15 + 60, "clock": "00:01:00"}, 0)
    assert f(23, 59, 0) == T0 + 3600 * 15 - 60


def test_service_report():
    t = hc.parse_service("active\n2026-10-10T07:00:03+0000 box blueflame[9]: moved tag latest to 0.9.11\n"
                         f"2026-10-10T08:00:00+0000 box blueflame[9]: running {PLAYER_ID}, NOT the pinned build\n", T0)
    assert t.active == "active" and t.moved == hc.parse_ts("2026-10-10T07:00:03+0000") and "NOT the pinned" in t.warn
    assert t.warn_at == hc.parse_ts("2026-10-10T08:00:00Z") and PLAYER_ID not in t.warn
    assert hc.parse_service("inactive\n", T0).moved is None and hc.parse_service("", T0).active == "unknown"
    cmd = hc.service_command("halo-blueflame")
    assert "systemctl is-active halo-blueflame" in cmd and "journalctl -u halo-blueflame" in cmd
    assert "restart" not in cmd and "stop" not in cmd.replace("--no-pager", "")  # only ever reads
    for bad in ("x; reboot", "a b", "$(id)", ""):
        with pytest.raises(ValueError):
            hc.service_command(bad)


# --- config: top-level keys only -----------------------------------------------------------------------------------
def test_config_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('health_cmd = "docker logs --since {since} box-{port}"\nhealth_min_free_mb = 750\nhealth_service = "halo-blueflame"\n'
                 '[defaults]\nssh = "box"\n[[server]]\nport = 7\n[[server]]\nurl = "ws://x/y"\n')
    _, (ours, remote) = load_config(p)
    assert ours.health_cmd == "docker logs --since {since} box-7" and remote.health_cmd == ""  # {since} is filled per run
    got = HealthSetup.from_settings(health_settings(p))
    assert (got.min_free_mb, got.service) == (750, "halo-blueflame")
    assert "health_cmd" not in health_settings(p)

    assert HealthSetup.from_settings({}) == HealthSetup(1024, "")  # the defaults
    for bad in ({"health_min_free_mb": 0}, {"health_min_free_mb": "lots"}, {"health_service": "a; b"}, {"health_service": 3}):
        with pytest.raises(ValueError):
            HealthSetup.from_settings(bad)

    # an older oni-rcon ignores the three keys out here and exits on one inside a table: so only here
    p.write_text('[[server]]\nport = 7\nhealth_cmd = "x"\n')
    with pytest.raises(SystemExit):
        load_config(p)
    p.write_text('[defaults]\nhealth_min_free_mb = 5\n[[server]]\nport = 7\n')
    with pytest.raises(SystemExit):
        load_config(p)


def test_the_documented_example_is_valid_toml():
    import re
    import tomllib
    from pathlib import Path
    text = (Path(__file__).parent.parent / "oni-rcon.example.toml").read_text(encoding="utf-8")
    block = re.search(r"# --- health example.*?\n(.*?)# --- end health example", text, re.S)[1]
    data = tomllib.loads("\n".join(line[2:] if line.startswith("# ") else line.lstrip("#") for line in block.splitlines()))
    assert {"health_cmd", "health_min_free_mb", "health_service"} <= data.keys()
    assert "{since}" in data["health_cmd"] and "{port}" in data["health_cmd"] and "docker inspect" in data["health_cmd"]


@pytest.mark.skipif(sys.platform == "win32", reason="the example is for a POSIX game box")
def test_the_examples_host_line_parses():
    import re
    import subprocess
    from pathlib import Path
    text = (Path(__file__).parent.parent / "oni-rcon.example.toml").read_text(encoding="utf-8")
    block = re.search(r"# --- health example.*?\n(.*?)# --- end health example", text, re.S)[1]
    import tomllib
    cmd = tomllib.loads("\n".join(line[2:] if line.startswith("# ") else line.lstrip("#") for line in block.splitlines()))["health_cmd"]
    echo = next(line for line in cmd.splitlines() if line.startswith('echo "host '))
    out = subprocess.run(["sh", "-c", echo.replace("/sys/fs/cgroup/memory.events", "/dev/null")], capture_output=True, text=True).stdout
    rep = hc.parse_report(out)
    assert rep.host["mem_available_mb"] > 0 and "swap_total_mb" in rep.host and rep.host["now"] > 1_700_000_000


# --- running a command ---------------------------------------------------------------------------------------------
def test_run_reads_a_local_command(tmp_path):
    script = tmp_path / "report.py"
    script.write_text(f"import sys\nprint({host(900)!r}, end='')\nprint({JOIN!r})\nprint('x', file=sys.stderr)\n")

    async def go():
        out = await hc.run(f'"{sys.executable}" "{script}"')
        assert "mem_available_mb=900" in out
        fail = tmp_path / "fail.py"
        fail.write_text("import sys\nprint('connect to host 203.0.113.9 refused', file=sys.stderr)\nsys.exit(3)\n")
        with pytest.raises(hc.HealthError) as e:
            await hc.run(f'"{sys.executable}" "{fail}"')
        assert "203.0.113.9" not in str(e.value) and "[ip]" in str(e.value)
        slow = tmp_path / "slow.py"
        slow.write_text("import time\ntime.sleep(30)\n")
        with pytest.raises(hc.HealthError, match="no answer"):
            await hc.run(f'"{sys.executable}" "{slow}"', timeout=0.5)

    asyncio.run(go())


# --- the tab -------------------------------------------------------------------------------------------------------
def plain(widget) -> str:
    return str(widget.render())


def grid_text(widget) -> str:
    """What a Static holding a rich Table (or Text) says, as plain text."""
    console = Console(width=200, file=io.StringIO(), force_terminal=False)
    console.print(widget.content)
    return console.file.getvalue()


def table_text(t) -> str:
    return "\n".join(" ".join(str(c) for c in t.get_row_at(r)) for r in range(t.row_count))


def test_unconfigured_the_tab_says_how_to_turn_it_on():
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("f7")
            await pilot.pause(0.3)
            assert app.query_one("#tabs").active == "health"
            how = plain(app.query_one("#health-setup"))
            assert "health_cmd" in how and "oni-rcon.example.toml" in how and "health_min_free_mb" in how
            assert app.query_one("#health-setup").display and not app.query_one("#crashes").display
            assert not app.query_one("#host-box").display and not app.query_one("#workaround").display  # no service named
            await pilot.press("ctrl+r")  # nothing to read: and nothing breaks
            await pilot.pause(0.3)
            assert app.is_running and not app.mon.events

    asyncio.run(go())


def test_a_service_alone_shows_its_panel():
    async def go():
        host_ = FakeHost(99, 99, 99)
        ports, keep = await serve_fakes(tick=False, host=host_)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False,
                     health=HealthSetup(service="halo-blueflame", source=host_))
        app.watched = lambda st: False  # a service named, but no health_cmd for any server
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f7")
            for _ in range(60):
                await pilot.pause(0.1)
                if app.mon.services:
                    break
            assert app.query_one("#health-setup").display and app.query_one("#workaround").display
            text = grid_text(app.query_one("#workaround"))
            assert "ACTIVE" in text and "NOT THE PINNED BUILD" in text and "MOVED TAG" in text

    asyncio.run(go())


async def ready(app, pilot):
    """Every server signed in, and its roster read: a crash from here on has a crowd to drop."""
    for _ in range(100):
        await pilot.pause(0.05)
        if all(st.online and str(st.index) in app.mon.counts and str(st.index) in app.mon.seen for st in app.stations):
            return


def test_demo_crashes_and_a_squeeze_reach_the_tab_the_feed_and_a_banner():
    async def go():
        host_ = FakeHost(0.6, 1.2, 1.8, rejoin=0.4, autorun=False)
        ports, keep = await serve_fakes(tick=False, host=host_)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False,
                     health=HealthSetup(service="halo-blueflame", source=host_))
        async with app.run_test(size=(170, 50)) as pilot:
            await ready(app, pilot)
            timeline = asyncio.create_task(host_.run())
            await timeline
            await pilot.press("f7")
            await pilot.press("ctrl+r")  # read every report now, as the key does
            for _ in range(60):
                await pilot.pause(0.1)
                if len(app.mon.events) >= 2:
                    break
            await pilot.pause(0.3)
            classes = sorted(e.cls for e in app.mon.events)
            assert classes == [BARE, SIGNATURE], classes
            sig = next(e for e in app.mon.events if e.cls == SIGNATURE)
            assert sig.rcon and sig.players  # the connection dropped with it, and there was a crowd to drop
            rows = table_text(app.query_one("#crashes"))
            assert "SIGNATURE" in rows and "BARE" in rows and "0xC0000005 halo3.dll+0x14C73E" in rows and "DROPPED" in rows
            assert app.mon.short() and "640 MB" in grid_text(app.query_one("#host")) and "1300 / 2048" in grid_text(app.query_one("#host"))
            assert "Slayer" in rows or "SLAYER" in rows.upper()
            # the feed has them, and the condition says something is wrong
            feed = " ".join(str(app.feed_line(st, ev)) for st, ev in app.feed if ev.get("event") == "health")
            assert "SIGNATURE CRASH" in feed and "BARE CRASH" in feed and "LOW MEMORY" in feed
            assert app.condition()[0] in ("RED", "AMBER")
            # the banner went up for them, and opening the tab again is looking at them: it comes down
            assert app.query_one("#banner").has_class("-on")
            await pilot.press("f1")
            await pilot.press("f7")
            await pilot.pause(0.2)
            assert not app.query_one("#banner").has_class("-on")
            # nothing from the logs that identifies a player or an address got as far as the screen or the state
            everything = rows + grid_text(app.query_one("#host")) + feed + repr(app.mon.__dict__)
            assert "203.0.113.9" not in everything and ("ab12" * 16) not in everything

    asyncio.run(go())


def test_the_banner_flashes_on_any_tab_and_a_click_clears_it():
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.health_notice(hc.Notice("crit", "1", "SIGNATURE CRASH  0xC0000005 in halo3.dll at RVA 0x14C73E  ·  7 players dropped"))
            await pilot.pause(0.3)
            banner = app.query_one("#banner")
            assert banner.has_class("-on") and "7 players dropped" in plain(banner) and app.query_one("#tabs").active == "assets"
            assert app.condition()[0] == "RED" and app.stations[1].flash > 0
            await pilot.click("#banner")
            await pilot.pause(0.2)
            assert not banner.has_class("-on")
            app.health_notice(hc.Notice("crit", "", "LOW MEMORY  500 MB available"))  # the host's own: no station
            await pilot.pause(0.2)
            assert banner.has_class("-on") and "HOST" in plain(banner)

    asyncio.run(go())


def test_the_health_tab_fits_a_small_terminal():
    async def go():
        host_ = FakeHost(0.3, 0.6, 0.9, rejoin=0.2)
        ports, keep = await serve_fakes(tick=False, host=host_)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False,
                     health=HealthSetup(service="halo-blueflame", source=host_))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f7")
            await pilot.pause(2.5)
            assert app.is_running and app.query_one("#tabs").active == "health"

    asyncio.run(go())


def test_a_report_that_fails_is_said_once_and_the_console_carries_on():
    class Broken:
        async def report(self, port, since):
            raise hc.HealthError("connect to 203.0.113.9 refused")

        async def service(self, unit):
            return "active\n"

    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False,
                     health=HealthSetup(source=Broken()))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("f7")
            for _ in range(40):
                await pilot.pause(0.1)
                if all(st.health_err for st in app.stations):
                    break
            assert all(st.health_err == "connect to [ip] refused" for st in app.stations)
            assert "[ip] refused" in grid_text(app.query_one("#host")) and "203.0.113.9" not in grid_text(app.query_one("#host"))
            said = [ev for _, ev in app.feed if "HEALTH REPORT FAILED" in str(ev.get("text"))]
            assert len(said) == 3 and app.is_running

    asyncio.run(go())


# --- kills named by an ID the engine slot doesn't answer to -----------------------------------------------------------
def station():
    return Station(Server(port=1), "Slayer")


def test_id_spellings():
    assert {"4", "4"} <= id_forms(4) and id_forms(True) == set() and id_forms({}) == set() and id_forms("") == set()
    big = 9983240802223591725
    assert id_forms(big) >= {str(big), f"{big:x}"} and id_forms(str(big)) == id_forms(big)
    h = "71a9fc60" + "0" * 8 + "d" * 48  # 64 hex digits
    assert str(int(h[:16], 16)) in id_forms(h) and str(int(h[-16:], 16)) in id_forms(h)


def test_kills_read_by_any_id_the_roster_carries():
    st = station()
    h = "71a9fc60" + "1" * 56
    st.learn([{"name": "Wombow", "engine_id": 3, "player_id": h, "xuid": "9983240802223591725"},
              {"name": "MilkyRaine", "engine_id": 4, "player_id": "bb" * 32, "number": 2}])
    ev = lambda k, v: resolve({"event": "kill", "killer": k, "victim": v, "weapon": "br"}, st.names, st.ident)
    # the engine's own slot, as ever
    assert ev(3, 4)["killer"] == "Wombow"
    # a decimal ID in a field of its own, as a string or a number
    assert ev("9983240802223591725", 4)["killer"] == "Wombow" and ev(9983240802223591725, 3)["killer"] == "Wombow"
    # the player's ID in decimal, whole or by its first 64 bits
    assert ev(str(int(h[:16], 16)), 4)["killer"] == "Wombow"
    assert ev(str(int("bb" * 8, 16)), 3)["killer"] == "MilkyRaine"
    line = render_event("s", ev("9983240802223591725", str(int("bb" * 8, 16))), {}).plain
    assert line.endswith("Wombow ✕ MilkyRaine  [br]")

    # someone who has since left is still read
    st.learn([{"name": "MilkyRaine", "engine_id": 4, "player_id": "bb" * 32}])
    assert ev("9983240802223591725", 4)["killer"] == "Wombow"
    # an ID nobody here answers to is never shown: it's PLAYER n, the same one each time
    a, b = ev("1395060783553029118", "1628542157085207465"), ev("1628542157085207465", "1395060783553029118")
    assert (a["killer"], a["victim"]) == ("PLAYER 1", "PLAYER 2") and (b["killer"], b["victim"]) == ("PLAYER 2", "PLAYER 1")
    assert "1395060783553029118" not in render_event("s", a, {}).plain
    # names, slots that left and dicts are for who() to read, as before
    assert ev("Rook", 77) == {"event": "kill", "killer": "Rook", "victim": "#77", "weapon": "br"}
    assert ev({"name": "Echo"}, 3)["killer"] == "Echo"
    assert st.ident("Kestrel") is None and st.ident(12) is None


def test_the_feed_names_players_when_kills_use_another_id():
    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        fake = keep[0]
        for p in fake.players:
            p["xuid"] = str(int(p["player_id"][:16], 16))
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(160, 48)) as pilot:
            for _ in range(60):
                await pilot.pause(0.1)
                if app.stations[0].here:
                    break
            a, b = fake.players
            app.on_rcon_event(app.stations[0].rcon, {"type": "event", "event": "kill", "killer": a["xuid"], "victim": b["xuid"],
                                                     "weapon": "sword"})
            app.on_rcon_event(app.stations[0].rcon, {"type": "event", "event": "kill", "killer": "17999231430439361766",
                                                     "victim": "1396838533438310385", "weapon": "sword"})
            await pilot.pause(0.2)
            lines = [str(app.feed_line(st, ev)) for st, ev in app.feed if ev.get("event") == "kill"]
            assert lines[0].endswith(f"{a['name']} ✕ {b['name']}  [sword]")
            assert "PLAYER 1" in lines[1] and "17999231430439361766" not in " ".join(lines)
            await pilot.press("f5")
            await pilot.pause(0.3)
            log = " ".join(line.text for line in app.query_one("#console-log").lines)
            assert "name" in log and "xuid" in log and "17999231430439361766" not in log  # field names, never values

    asyncio.run(go())
