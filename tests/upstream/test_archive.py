import asyncio
import json

import pytest

from oni_rcon import app as appmod
from oni_rcon import archive
from oni_rcon.app import Archive, Boot, OniApp
from oni_rcon.art import fit, pixels
from oni_rcon.config import Server
from oni_rcon.demo import serve_fakes
from oni_rcon.prefs import Prefs


def plain(w) -> str:
    return w.content.plain if hasattr(w, "content") and hasattr(w.content, "plain") else str(w.render())


# --- the sequence: a pure function of the clock ------------------------------------------------------------------
def test_the_scenes_play_in_the_designs_order_and_length():
    assert [n for n, _ in archive.SCENES] == ["ONI", "SectionThree", "Installation04", "GuiltySpark", "Superintendent"]
    assert archive.TOTAL == 29.0
    starts = [0.1, 4.6, 9.6, 16.1, 21.6]
    assert [archive.frame(t).scene for t in starts] == [n for n, _ in archive.SCENES]
    assert archive.frame(archive.TOTAL + 5).scene == "Superintendent"  # holds on its last picture


def test_each_file_decrypts_in_turn_and_the_gauge_follows():
    assert archive.frame(0.1).files == [("FILE 01  ONI EMBLEM", False)] and archive.frame(0.1).progress == 0
    f = archive.frame(archive.TOTAL)
    assert [d for _, d in f.files] == [True] * 5 and [label for label, _ in f.files] == [k for k, _ in archive.FILES]
    assert round(f.progress, 3) == round(5 / 6, 3)  # the sixth step is the boot screen's clearance check
    seen = [archive.frame(t / 10).progress for t in range(0, 300, 7)]
    assert seen == sorted(seen)


def test_titles_decrypt_and_say_what_the_design_says():
    assert archive.frame(0).title != archive.frame(3).title  # noise at first, the words later
    assert "".join(archive.frame(3).title.split()) == "OFFICEOFNAVALINTELLIGENCE"
    f = archive.frame(12)
    assert (f.tag, f.sub.split("  ·  ")[:2]) == ("CLASSIFIED", ["FORERUNNER ARRAY", "10,000 KM"])
    assert archive.frame(22).tag == "ONLINE" and archive.frame(26.5).tag == "PLEASED"  # the mood follows the face


@pytest.mark.parametrize("t,size", [(7, (38, 40)), (12.8, (48, 80)), (18, (48, 48)), (26, (48, 48))])
def test_the_pictures_are_the_designs_size(t, size):
    art = archive.frame(t).art
    assert (len(art), len(art[0])) == size and any(c for row in art for c in row)


def test_a_picture_materialises_from_nothing_and_dissolves_out():
    assert archive.frame(4.5).art is None  # the scene has only begun
    lit = lambda t: sum(c is not None for row in archive.frame(t).art for c in row)
    assert lit(5.0) < lit(5.4) < lit(7.5) and lit(9.45) < lit(7.5)  # in, whole, out again


def test_the_glint_and_the_spin_move():
    assert archive.gungnir() != archive.gungnir(0.0)
    assert archive.ring(0.0, 0.3) != archive.ring(1.0, 0.3)
    assert archive.spark(0.0, 0.0, 1.0) != archive.spark(0.5, 1.0, 0.0)


def test_pixels_and_fit():
    grid = [["#FF0000", None], ["#00FF00", "#00FF00"]]
    text = pixels(grid)
    assert text.plain == "▀▄"[:0] + text.plain and len(text.plain.split("\n")) == 1 and len(text.plain) == 2
    small = fit(archive.frame(12.8).art, 10, 40)
    assert len(small) <= 20 and len(small[0]) <= 40 and any(c for row in small for c in row)
    assert fit(grid, 5, 5) is grid  # fits already: untouched


# --- preferences -----------------------------------------------------------------------------------------------
def test_the_first_run_is_full_then_quick_unless_asked(tmp_path):
    path = tmp_path / "sub" / "prefs.json"
    prefs = Prefs.load(path)
    assert prefs.intro_mode() == "full"  # never seen it
    prefs.intro_seen = True
    prefs.save()
    again = Prefs.load(path)
    assert again.intro_seen and again.intro_mode() == "quick"
    again.full_intro = True
    again.save()
    assert Prefs.load(path).intro_mode() == "full" and json.loads(path.read_text())["full_intro"] is True


@pytest.mark.parametrize("junk", ["", "not json", "[]", '{"intro_seen": "yes", "full_intro": 1}'])
def test_unreadable_prefs_read_as_defaults(tmp_path, junk):
    path = tmp_path / "prefs.json"
    path.write_text(junk)
    assert not Prefs.load(path).intro_seen and not Prefs.load(path).full_intro
    assert not Prefs.load(tmp_path / "missing.json").intro_seen and Prefs.load(None).intro_mode() == "full"


# --- in the console ----------------------------------------------------------------------------------------------
def full_app(ports, tmp_path, **kw):
    return OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro="full",
                  prefs=Prefs(tmp_path / "prefs.json"), **kw)


def test_full_plays_the_archive_then_the_clearance_check(tmp_path, monkeypatch):
    async def go():
        ports, keep = await serve_fakes(tick=False)
        monkeypatch.setattr(appmod, "TOTAL", 600)  # held open until it's been looked at: a slow runner can't miss it
        app = full_app(ports, tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            for _ in range(100):
                await pilot.pause(0.05)
                if isinstance(app.screen, Archive) and "FILE 01" in plain(app.screen.query_one("#boot-log")):
                    break
            assert isinstance(app.screen, Archive)
            assert "FILE 01" in plain(app.screen.query_one("#boot-log")) and app.prefs.intro_seen
            assert json.loads((tmp_path / "prefs.json").read_text())["intro_seen"] is True
            monkeypatch.setattr(appmod, "TOTAL", 0)  # and now it's over
            for _ in range(40):
                await pilot.pause(0.1)
                if isinstance(app.screen, Boot):
                    break
            assert isinstance(app.screen, Boot)  # the last act
            await pilot.press("escape")
            assert not isinstance(app.screen, (Archive, Boot))

    asyncio.run(go())


def test_a_key_skips_the_whole_sequence(tmp_path):
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = full_app(ports, tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            assert isinstance(app.screen, Archive)
            await pilot.press("space")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, (Archive, Boot))

    asyncio.run(go())


def test_it_fits_a_small_terminal(tmp_path):
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = full_app(ports, tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(6):  # through the emblem into the ring, the widest picture
                await pilot.pause(0.1)
            assert isinstance(app.screen, Archive)
            app.screen.t0 -= 12  # jump to the ring
            await pilot.pause(0.3)
            art = app.screen.query_one("#boot-emblem")
            assert art.size.width <= 100 and art.size.height <= 30

    asyncio.run(go())


def test_quick_and_off_skip_the_archive(tmp_path):
    async def go():
        ports, keep = await serve_fakes(tick=False)
        for mode, boot in (("quick", True), ("off", False)):
            app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=mode)
            async with app.run_test(size=(160, 48)) as pilot:
                await pilot.pause(0.3)
                assert isinstance(app.screen, Boot) == boot and not isinstance(app.screen, Archive)

    asyncio.run(go())


def test_no_animations_means_no_archive(tmp_path):
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = full_app(ports, tmp_path)
        app.animation_level = "none"  # what TEXTUAL_ANIMATIONS=none sets, once the app has read it
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            assert isinstance(app.screen, Boot) and not app.prefs.intro_seen  # nothing was shown: not seen

    asyncio.run(go())


def test_the_palette_toggle_is_remembered(tmp_path):
    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro="off",
                     prefs=Prefs(tmp_path / "prefs.json"))
        async with app.run_test(size=(160, 48)) as pilot:
            titles = lambda: [c.title for c in app.get_system_commands(app.screen) if c.title.startswith("Boot")]
            assert titles() == ["Boot sequence: full every time"]
            app.action_full_intro()
            assert Prefs.load(tmp_path / "prefs.json").full_intro and Prefs.load(tmp_path / "prefs.json").intro_mode() == "full"
            assert titles() == ["Boot sequence: quick after the first run"]
            app.action_full_intro()
            assert not Prefs.load(tmp_path / "prefs.json").full_intro

    asyncio.run(go())
