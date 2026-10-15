import asyncio
import io

import pytest

from oni_rcon import archive
from oni_rcon import superintendent as sp
from oni_rcon.superintendent import BELOW, BIG, HUGE, LARGE, MINI, SMALL, XL, Expr, Superintendent, card, classify, face

CHEAT = {"event": "cheat", "name": "Lark", "text": "speed out of range"}
JOIN = {"event": "join", "name": "Kestrel"}


def clock(start: float = 100.0):
    t = [start]
    sup = Superintendent(now=lambda: t[0])
    return sup, t


def colours(text) -> set[str]:
    return {str(s.style) for s in text.spans} | {c for s in text.spans for c in str(s.style).replace("on ", "").split()}


@pytest.mark.parametrize("ev,key", [
    ({"event": "join", "name": "A"}, "welcome"),
    ({"event": "chat", "channel": "all", "text": "admins?? red is hacking"}, "call"),
    ({"event": "chat", "channel": "team red", "text": "any mod around"}, "call"),
    ({"event": "chat", "channel": "all", "text": "gg everyone"}, None),  # ordinary chat earns nothing
    ({"event": "chat", "channel": "server", "text": "next map loading"}, "cheer"),
    ({"event": "chat", "channel": "server", "text": "admins have been called"}, "cheer"),  # the server isn't a caller
    ({"event": "cheat", "name": "A"}, "cheat"),
    ({"event": "kick", "player": "A"}, "satisfied"),
    ({"event": "ban", "player": "A"}, "satisfied"),
    ({"event": "mute", "player": "A"}, "unimpressed"),
    ({"event": "kill", "killer": 1, "victim": 2, "_medals": ["DOUBLE KILL"]}, "medal"),
    ({"event": "kill", "killer": 1, "victim": 2, "_medals": []}, None),  # most kills earn no medal
    ({"event": "leave", "name": "A"}, None), ({"event": "unmute"}, None), ({"event": "vote"}, None), ({}, None),
])
def test_what_earns_a_reaction(ev, key):
    assert classify(ev) == key


def test_the_face_is_drawn_the_way_the_design_draws_it():
    for n in (SMALL, BIG, LARGE):
        lines = face(Expr(), n).plain.split("\n")
        assert len(lines) == n // 2 and all(len(r) == n for r in lines)  # n cells wide, half as many rows
    looks = (face(e) for e in (Expr(), Expr(openL=0, openR=0), Expr(happy=True), Expr(size=1.28)))
    assert len({(t.plain, tuple((s.start, s.end, str(s.style)) for s in t.spans)) for t in looks}) == 4  # all differ
    assert archive.FACE_EYE in colours(face(Expr(), BIG))  # open eyes are white all the way through,
    assert archive.FACE_EYE not in colours(face(Expr(openL=0, openR=0), BIG))  # and a blink leaves only a slit

    def redness(text) -> int:  # the most red over green any colour in the face has
        return max(int(c[1:3], 16) - int(c[3:5], 16) for c in colours(text) if c.startswith("#"))
    assert redness(face(Expr(alarm=0.9), BIG)) > 0 > redness(face(Expr(alarm=0), BIG))  # the rim flushes red in alarm


def test_the_face_is_the_designs_at_48_and_crisp_below():
    """1:1 at the design's 48 pixels. Smaller faces are drawn from the same shapes: under 24 pixels with only its
    tones, above that with the rim blended but the eyes and edge still hard, never averaged into a halo."""
    full = face(Expr(), 48)
    assert len(full.plain.split("\n")) == 24 and archive.FACE_EYE in colours(full)
    tones = set(archive.face_tones(Expr()).values())
    for n in (MINI, SMALL, BIG):
        used = {c for c in colours(face(Expr(), n)) if len(c) == 7 and c.startswith("#")}
        assert used <= tones and archive.FACE_EYE in used, n
    for n in (LARGE, XL, HUGE):
        grid = archive.superintendent_at(Expr(), n)
        eyes = {c for row in grid for c in row if c is not None and sum(int(c[i:i + 2], 16) for i in (1, 3, 5)) > 650}
        assert eyes == {archive.FACE_EYE}, n  # the only near-white is the eye itself: no halo of blends round it


@pytest.mark.parametrize("n", [MINI, SMALL, BIG, LARGE, XL, HUGE])
def test_small_eyes_stay_apart_and_a_smile_stays_a_smile(n):
    def eyes(e):  # the columns that have an eye pixel in them
        g = archive.superintendent_at(e, n)
        return sorted({x for row in g for x, c in enumerate(row) if c == archive.FACE_EYE})
    wide, smile = eyes(Expr(size=1.28, ey=-1.5, ex=0)), eyes(Expr(happy=True))
    assert any(b - a > 1 for a, b in zip(wide, wide[1:])) and wide[0] < n // 2 - 1 and wide[-1] > n // 2  # two eyes
    assert smile and (n >= XL or not eyes(Expr(openL=0, openR=0)))  # a smile still shows; a blink has none (a slit, big)
    rows = archive.superintendent_at(Expr(), n)
    assert rows[0][n // 2] is not None or rows[1][n // 2] is not None  # the disc reaches the top
    assert rows[0][0] is None and rows[0][-1] is None and rows[-1][0] is None  # and is round, not square


def test_a_reaction_eases_in_holds_and_relaxes():
    sup, t = clock()
    assert sup.look_at(t[0])[0].name == "idle" and sup.look()[1] == "WATCHING"
    assert sup.react(CHEAT, "Lark: speed out of range").name == "cheat"
    cur, e = sup.look_at(t[0])
    assert cur.mood == "HOSTILE" and e.tilt == 0  # the moment it happens the expression hasn't started to move
    t[0] += 0.1
    mid = sup.look_at(t[0])[1].tilt
    t[0] += 0.3
    done = sup.look_at(t[0])[1].tilt
    assert 0 < mid < done and abs(done - 0.6) < 0.01  # eased in, settled on the design's frown
    t[0] += 5.0
    assert sup.look()[1:4] == ("HOSTILE", sp.RED, "Lark: speed out of range")  # still held
    t[0] += 1.0  # the hold is over: the mood is back, and the face eases to rest
    assert sup.look()[1] == "WATCHING" and sup.look_at(t[0] + 0.1)[1].tilt < done
    assert sup.look_at(t[0] + 1.0)[1].tilt == 0 and not sup.look_at(t[0] + 1.0)[1].alarm
    assert sup.look()[3] == "Lark: speed out of range"  # what it last reacted to stays until the next


def test_an_alert_pulses_the_rim_red():
    sup, t = clock()
    sup.react({"event": "chat", "channel": "all", "text": "admin!"}, "call")
    seen = set()
    for _ in range(40):
        t[0] += 0.05
        seen.add(sup.look_at(t[0])[1].alarm > 0.5)
    assert seen == {True, False}  # it flashes between red and green, as the design's alert does
    sup.motion = False
    assert sup.look_at(t[0])[1].alarm == 1.0  # with animations off it simply stays red while held


def test_weight_and_debounce():
    sup, t = clock()
    assert sup.react(CHEAT, "a").name == "cheat"
    t[0] += 1.0
    assert sup.react(JOIN, "b") is None and sup.look()[3] == "a"  # a join doesn't cut an alert short
    assert sup.react({"event": "kick", "player": "x"}, "c") is None  # nor does a kick
    assert sup.react(CHEAT, "d") is None  # one of the same weight waits a moment, so a busy fleet doesn't flicker
    t[0] += 0.5
    assert sup.react({"event": "chat", "channel": "x", "text": "admin?"}, "e").name == "call" and sup.look()[3] == "e"
    t[0] += 10
    assert sup.react(JOIN, "f").name == "welcome"  # once the alert has run its hold, anything goes
    t[0] += 0.3
    assert sup.react(CHEAT, "g").name == "cheat"  # and something heavier takes over at once
    assert not sup.wants({"event": "leave"}) and sup.wants(JOIN)


def test_blinks_come_and_go_and_stop_with_animations_off():
    sup, _ = clock()
    levels = [sup.blinking(i * 0.02) for i in range(1, 2000)]  # 40 seconds
    assert all(0 <= v <= 1 for v in levels) and max(levels) == pytest.approx(1, abs=0.1)
    blinks = sum(1 for a, b in zip(levels, levels[1:]) if a == 0 < b)
    assert 5 <= blinks <= 9  # about one in each 5.5 s
    assert sup.blinking(12345.6) == sup.blinking(12345.6)  # a moment always looks the same
    sup.motion = False
    assert not any(sup.blinking(i * 0.02) for i in range(2000))
    sup.react(CHEAT, "x")
    assert sup.look_at(sup.since + 0.001)[1].tilt == pytest.approx(0.6)  # and expressions snap


def test_a_blink_closes_both_eyes_and_a_wink_one():
    sup, t = clock()
    shut = next(x * 0.01 for x in range(1, 2000) if sup.blinking(x * 0.01) > 0.99)
    e = sup.look_at(shut)[1]
    assert e.openL < 0.05 and e.openR < 0.1
    sup.react({"event": "chat", "channel": "server", "text": "gg"}, "gg")
    t[0] = sup.since + 0.25
    e = sup.look_at(t[0])[1]
    assert e.openR < 0.2 and e.openL > 0.9 or sup.blinking(t[0]) > 0  # a wink: the right eye only


@pytest.mark.parametrize("n,width", [(MINI, 23), (SMALL, 27), (SMALL, 31), (BIG, 31), (BIG, 41)])
def test_the_card_fits_its_room(n, width):
    sup, t = clock()
    sup.react(CHEAT, "Lark: speed out of range at the red base ramp again, third time this round " * 2)
    t[0] += 1
    out, key = card(sup, width, n)
    lines = out.plain.split("\n")
    assert len(lines) == n // 2 and all(len(line) <= width for line in lines)  # fits, and no taller than the face
    assert lines[0].rstrip().endswith("SUPERINTENDENT") and "HOSTILE" in lines[1]
    assert lines[-1].rstrip().endswith("…") or any(line.rstrip().endswith("…") for line in lines)  # long words cut
    again, key2 = card(sup, width, n)
    assert key == key2  # nothing changed: nothing to repaint
    sup.react({"event": "chat", "channel": "all", "text": "admins"}, "x")
    t[0] += 2
    assert card(sup, width, n)[1] != key


@pytest.mark.parametrize("n,width", [(LARGE, 28), (XL, 33), (HUGE, 43)])
def test_a_large_face_puts_its_words_underneath(n, width):
    sup, t = clock()
    sup.react(CHEAT, "Lark: speed out of range at the red base ramp again, third time this round " * 2)
    t[0] += 1
    out, key = card(sup, width, n)
    lines = out.plain.split("\n")
    below = lines[n // 2:]
    assert len(lines) == n // 2 + BELOW and all(len(line) <= width for line in lines)  # one steady height
    assert below[0].strip() == "SUPERINTENDENT" and below[1].strip() == "HOSTILE" and below[3].strip().endswith("…")
    assert abs(below[0].index("S") - (width - 14) / 2) <= 1  # centred under the face
    assert card(sup, width, n)[1] == key


def test_quiet_says_so():
    sup, _ = clock()
    out, _ = card(sup, 27, SMALL)
    assert "WATCHING" in out.plain and "all quiet" in out.plain


# --- in the console ----------------------------------------------------------------------------------------------
async def until(pilot, cond, tries: int = 120) -> bool:
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return True
    return False


def plain(widget) -> str:
    from rich.console import Console
    console = Console(width=80, file=io.StringIO(), record=True, color_system=None)
    console.print(widget.content)
    return console.export_text()


def console_app(ports):
    from oni_rcon.app import OniApp
    from oni_rcon.config import Server
    return OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)


def check_there(app, where: str, rows: int) -> None:
    box, side = app.query_one("#super"), app.query_one("#sidebar")
    assert box.display and box.region.height >= rows, where  # all of the face's rows
    assert side.region.contains_region(box.region), where  # inside the sidebar
    assert app.query_one("#uplink").region.bottom <= side.region.bottom, where  # and the uplink still is too
    assert "SUPERINTENDENT" in plain(box) and "WATCHING" in plain(box), where


def test_the_superintendent_is_always_there():
    """On every tab and at every size: the face is drawn whole, inside the sidebar, and the uplink with it."""
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False)
        for size in [(100, 20), (100, 24), (120, 30), (120, 36), (170, 50), (210, 60)]:
            app = console_app(ports)
            async with app.run_test(size=size) as pilot:
                assert await until(pilot, lambda: all(st.online for st in app.stations))
                await pilot.pause(0.3)  # the strip fits itself once the sidebar has its width
                for tab in ("f1", "f2", "f3", "f4", "f5", "f6"):
                    await pilot.press(tab)
                    await pilot.pause(0.1)
                    check_there(app, f"{size} on {tab}", app.sup_rows() - 2)
                assert app.sup_size() == {(100, 20): MINI, (100, 24): MINI, (120, 30): SMALL, (120, 36): SMALL, (170, 50): XL,
                                          (210, 60): HUGE}[size], size  # the biggest faces need height; the largest, width
    asyncio.run(go())


def test_it_stays_even_in_a_fleet():
    """Past 12 servers the cards slim to two lines; the list scrolls rather than push the strip or the uplink away."""
    from oni_rcon.app import OniApp
    from oni_rcon.config import Server
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[(f"S{i}", 16, 2) for i in range(14)])
        for size in [(120, 24), (120, 30), (170, 50)]:
            app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
            async with app.run_test(size=size) as pilot:
                assert await until(pilot, lambda: sum(st.online for st in app.stations) >= 12)
                await pilot.pause(0.3)
                check_there(app, f"fleet {size}", app.sup_rows() - 2)
    asyncio.run(go())


def test_it_reacts_to_what_the_servers_send():
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = console_app(ports)
        async with app.run_test(size=(120, 36)) as pilot:
            assert await until(pilot, lambda: all(st.online for st in app.stations))
            st, box = app.stations[0], app.query_one("#super")
            t = [1000.0]
            app.sup.now = lambda: t[0]
            for ev, mood, words in [
                ({"event": "join", "name": "Kestrel", "player_id": "f9d36cae", "address": "203.0.113.9:1"}, "WELCOMING",
                 "Kestrel joined"),
                ({"event": "cheat", "name": "Lark", "text": "speed out of range"}, "HOSTILE", "speed out of range"),
                ({"event": "chat", "channel": "all", "name": "Drift", "text": "admins?? red is hacking"}, "ALARMED",
                 "Drift: admins"),
                ({"event": "kick", "player": "Lark", "reason": "speed hack"}, "SATISFIED", "kick Lark: speed hack"),
                ({"event": "mute", "player": "Viper", "reason": "spam"}, "UNIMPRESSED", "mute Viper: spam"),
                ({"event": "chat", "channel": "server", "text": "gg all"}, "CHEERFUL", "gg all"),
            ]:
                t[0] += 30  # past any hold
                app.on_rcon_event(st.rcon, dict(ev))
                t[0] += 0.4
                assert await until(pilot, lambda: mood in plain(box)), mood
                assert words in app.sup.look()[3], (words, app.sup.look()[3])  # what it says it reacted to
            # an ordinary line and a plain death leave the face as it was
            t[0] += 30
            app.on_rcon_event(st.rcon, {"event": "chat", "channel": "all", "name": "Rook", "text": "nice shot"})
            assert app.sup.look()[1] == "WATCHING" and "nice shot" not in plain(box)
    asyncio.run(go())


def test_what_it_says_obeys_redaction_and_strangers():
    from oni_rcon.app import reaction_words
    ev = {"event": "chat", "channel": "all", "name": "Drift", "text": "admin! 203.0.113.9 is hacking\x1b[31m"}
    assert "203.0.113.9" not in reaction_words(ev, {}, True) and "███" in reaction_words(ev, {}, True)
    assert "203.0.113.9" in reaction_words(ev, {}, False) and "\x1b" not in reaction_words(ev, {}, False)
    assert reaction_words({"event": "join", "name": "Kestrel", "address": "203.0.113.9:5"}, {}, True) == "Kestrel joined"
    assert reaction_words({"event": "kill", "killer": "Rook", "victim": "Juno", "_medals": ["DOUBLE KILL", "KILLJOY"]},
                          {}, True) == "KILLJOY: Rook"
    assert reaction_words({"event": "ban", "player": "Lark"}, {}, True) == "ban Lark"


def test_turning_redaction_on_clears_what_it_last_said():
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = console_app(ports)
        async with app.run_test(size=(120, 36)) as pilot:
            assert await until(pilot, lambda: all(st.online for st in app.stations))
            app.redact = False  # addresses visible: it may have shown one
            app.on_rcon_event(app.stations[0].rcon, {"event": "chat", "channel": "all", "name": "Drift",
                                                    "text": "admin 203.0.113.9"})
            assert "203.0.113.9" in app.sup.last
            app.action_redact()
            assert app.redact and app.sup.last == ""
    asyncio.run(go())


def test_animations_off_means_no_blinking_in_the_console():
    """TEXTUAL_ANIMATIONS=none sets the app's animation_level: the face then snaps and never blinks."""
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = console_app(ports)
        app.animation_level = "none"  # what the environment variable sets, once the app has read it
        async with app.run_test(size=(120, 36)) as pilot:
            assert await until(pilot, lambda: all(st.online for st in app.stations))
            await pilot.pause(0.3)
            assert app.sup.motion is False
            assert not any(app.sup.blinking(i * 0.05) for i in range(400))
            app.animation_level = "full"
            await pilot.pause(0.3)
            assert app.sup.motion is True  # and it follows the setting
    asyncio.run(go())
