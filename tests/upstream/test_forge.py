import asyncio
import hashlib
import json
import sys

import pytest

from oni_rcon import forge
from oni_rcon.config import forge_settings, load_config, remember_forge_key
from oni_rcon.forge import (ForgeClient, ForgeError, Secret, Unverified, compatible, load_key, next_page, scrub,
                            scrub_data)
from oni_rcon.forgefake import FakeForge, serve

KEY = "rfk_test1234_abcdefghijklmnopqrstuvwxyz0123"


@pytest.fixture
def fake():
    url, fk, httpd = serve(FakeForge(key=KEY))
    yield fk
    httpd.shutdown()


def client(fk, tmp_path, key=KEY, **kw) -> tuple[ForgeClient, list]:
    naps = []

    async def nap(s):
        naps.append(s)
    return ForgeClient(Secret(key), fk.base, cache_dir=tmp_path / "cache", sleep=nap, **kw), naps


def test_a_key_never_prints():
    s = Secret(f"  {KEY}\n")
    assert s.value == KEY and KEY not in f"{s} {s!r} {[s]} {s:>40}" and str(s) == forge.HIDDEN
    assert scrub(f"Authorization: Bearer {KEY}") == f"Authorization: Bearer {forge.HIDDEN}"
    assert scrub("rfk_someoneelses_KEY99") == forge.HIDDEN  # shaped like a key: blanked even if it isn't ours
    data = {"a": [1, {"b": f"x {KEY} y"}], "c": "clean"}
    assert KEY not in json.dumps(scrub_data(data)) and data["a"][1]["b"].startswith("x rfk_test")  # a copy
    clean = {"a": [1, {"b": "nothing here"}]}
    assert scrub_data(clean) is clean  # untouched when there's nothing to blank


def test_where_the_key_comes_from(tmp_path, monkeypatch):
    monkeypatch.delenv("ONI_RCON_FORGE_KEY", raising=False)
    env = {"MY_KEY": "rfk_fromenv_123456", "ONI_RCON_FORGE_KEY": "rfk_fallback_123456"}
    cmd = [sys.executable, "-c", "print('rfk_fromcmd_123456')"]
    assert load_key({"forge_api_key": "rfk_saved_123456", "forge_api_key_env": "MY_KEY"}, env).source == "the config file"
    found = load_key({"forge_api_key_env": "MY_KEY", "forge_api_key_command": cmd}, env)
    assert found.key.value == "rfk_fromenv_123456" and found.source == "$MY_KEY"
    assert load_key({"forge_api_key_env": "UNSET", "forge_api_key_command": cmd}, env).key.value == "rfk_fromcmd_123456"
    assert load_key({}, env).source == "$ONI_RCON_FORGE_KEY"
    assert load_key({}, {}).key is None and not load_key({}, {}).error  # no key: Forge is off, nothing's wrong
    assert "empty or not set" in load_key({"forge_api_key_env": "UNSET"}, {}).error
    bad = load_key({"forge_api_key_command": [sys.executable, "-c", "import sys; print('rfk_leaky_99999999', "
                                                                    "file=sys.stderr); sys.exit(3)"]}, {})
    assert bad.key is None and "exited 3" in bad.error and "rfk_leaky" not in bad.error  # never a SystemExit

    p = tmp_path / "c.toml"
    p.write_text('# mine\nby = "op"\nforge_api_key_env = "MY_KEY"\n[defaults]\nssh = "box"\n[[server]]\nport = 1\n'
                 'content_dir = "~/reclaimer/content"\n')
    assert forge_settings(p) == {"forge_api_key_env": "MY_KEY"}
    assert load_config(p)[1][0].content_dir == "~/reclaimer/content"


def test_the_key_is_saved_only_when_asked(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('# mine\n\nby = "op"\n[[server]]\nport = 1\n')
    remember_forge_key(p, KEY)
    text = p.read_text()
    assert text.startswith("# mine\n\nforge_api_key = ") and text.count(KEY) == 1
    assert forge_settings(p)["forge_api_key"] == KEY and load_config(p)[1][0].port == 1
    remember_forge_key(p, "rfk_another_1234567")  # a new key replaces the old one
    assert KEY not in p.read_text() and forge_settings(p)["forge_api_key"] == "rfk_another_1234567"
    if sys.platform != "win32":
        assert p.stat().st_mode & 0o777 == 0o600


def test_the_catalog(fake, tmp_path):
    async def go():
        fc, _ = client(fake, tmp_path)
        items, nxt, _ = await fc.listings("trending", "7d", page_size=10)
        assert len(items) == 10 and nxt == {"cursor": "10"} and fc.quota.remaining == 119 and fc.quota.limit == 120
        more, _, _ = await fc.listings("trending", "7d", page_size=10, more=nxt)
        assert not {x["id"] for x in items} & {x["id"] for x in more}
        path, q, _ = fake.seen[0]
        assert path == "/api/listings" and q == {"page_size": "10", "sort": "trending", "window": "7d"}
        await fc.listings("latest", "24h")
        assert "window" not in fake.seen[-1][1]  # a window only goes with the sorts it means something for
        await fc.listings("downloads", "24h")
        assert "window" not in fake.seen[-1][1]  # downloads is lifetime; only trending and rising take one
        await fc.listings("rising", "24h")
        assert fake.seen[-1][1]["window"] == "24h"
        found, _, _ = await fc.listings("latest", query="grif")
        assert [x["title"] for x in found] == ["Grifball"]
        detail = await fc.listing(found[0]["id"])
        assert detail["versions"] and forge.latest_of(detail)["id"] == detail["versions"][0]["id"]
        sent = fc.sent
        await fc.listing(found[0]["id"])
        assert fc.sent == sent  # fresh in the cache: not asked again
    asyncio.run(go())


@pytest.mark.parametrize("status,words", [(401, "refused your API key"), (403, "catalog:read and assets:download"),
                                          (404, "isn't available"), (409, "changed state"), (413, "that large"),
                                          (422, "couldn't accept"), (503, "down for a moment")])
def test_every_error_in_words(fake, tmp_path, status, words):
    async def go():
        fc, naps = client(fake, tmp_path)
        fake.fail["/api/listings"] = [status] * 4
        with pytest.raises(ForgeError) as e:
            await fc.listings()
        assert e.value.status == status and words in e.value.text and KEY not in str(e.value)
        assert len(naps) == (3 if status == 503 else 0)  # only a 429 or a 503 is tried again
    asyncio.run(go())


def test_a_refused_key_and_a_missing_scope(fake, tmp_path):
    async def go():
        fc, _ = client(fake, tmp_path, key="rfk_wrong_000000000")
        with pytest.raises(ForgeError) as e:
            await fc.listings()
        assert e.value.short == "KEY REFUSED" and "rfk_wrong" not in e.value.text
        fake.scopes = {"catalog:read"}
        fc, _ = client(fake, tmp_path)
        x = (await fc.listings())[0][0]
        with pytest.raises(ForgeError) as e:
            await fc.manifest(x["id"], forge.latest_of(x)["id"])
        assert e.value.status == 403
    asyncio.run(go())


def test_rate_limits_are_honoured(fake, tmp_path):
    async def go():
        fc, naps = client(fake, tmp_path)
        fake.fail["/api/listings"] = [429, 429]
        items, _, _ = await fc.listings()
        assert items and len(naps) == 2 and all(1 <= n <= 1.5 for n in naps)  # Retry-After: 1, with jitter

        fake.limit, fake.window = 3, 30  # spent: the next request waits for the reset rather than drawing a 429
        fake.hits.clear()
        fc, naps = client(fake, tmp_path)
        for _ in range(3):
            await fc.get("/api/listings", {"page_size": 1})
        assert fc.quota.remaining == 0 and fc.quota.wait() > 0
        fake.hits.clear()
        await fc.get("/api/listings", {"page_size": 2})
        assert len(naps) == 1 and 25 < naps[0] <= 31
        fake.window = 600  # a reset further off than anyone should wait: reported, not waited out
        fc.quota.remaining, fc.quota.reset_at = 0, forge.time.monotonic() + 600
        with pytest.raises(ForgeError) as e:
            await fc.get("/api/listings", {"page_size": 3})
        assert e.value.status == 429 and "Try again in" in e.value.text

        q = forge.Quota(limit=120, remaining=forge.RESERVE, reset_at=forge.time.monotonic() + 20)
        assert q.wait() == 0 and q.wait(background=True) > 0  # the watcher leaves the operator room
    asyncio.run(go())


def test_an_old_copy_when_forge_is_down(fake, tmp_path):
    async def go():
        fc, _ = client(fake, tmp_path)
        x = (await fc.listings())[0][0]
        await fc.listing(x["id"])
        fc.cache.get = lambda url, max_age, real=fc.cache.get: real(url, None if max_age is None else 0)  # all stale
        fake.fail["/api/listings/"] = [503] * 4
        old = await fc.listing(x["id"])
        assert old["id"] == x["id"] and old["_stale"] >= 0
        assert not list((tmp_path / "cache").rglob("*.json"))[0].read_text().count(KEY)  # never the key on disk
    asyncio.run(go())


def test_the_key_stays_on_forge(fake, tmp_path):
    cdn_url, cdn, cdn_httpd = serve(FakeForge(key="rfk_cdn_00000000"))
    try:
        cdn.blobs = fake.blobs
        fake.asset_base = cdn_url.replace("127.0.0.1", "localhost")  # another origin, as a CDN would be

        async def go():
            fc, _ = client(fake, tmp_path)
            x = next(x for x in (await fc.listings())[0])
            man = await fc.manifest(x["id"], forge.latest_of(x)["id"])
            a = man["assets"][0]
            path = await fc.download(a["url"], a["size"], a["sha256"])
            assert hashlib.sha256(path.read_bytes()).hexdigest() == a["sha256"]
            assert cdn.seen[-1][2] is False and fake.seen[-1][2] is True  # no key to the CDN; Forge got it
        asyncio.run(go())
    finally:
        cdn_httpd.shutdown()
    with pytest.raises(ValueError, match="https"):
        ForgeClient(Secret(KEY), "http://forge.example.org")


def test_downloads_are_checked(fake, tmp_path):
    async def go():
        fc, _ = client(fake, tmp_path)
        x = (await fc.listings())[0][0]
        a = (await fc.manifest(x["id"], forge.latest_of(x)["id"]))["assets"][0]
        fake.tamper.add(a["sha256"])
        with pytest.raises(Unverified, match="SHA-256"):
            await fc.download(a["url"], a["size"], a["sha256"])
        fake.tamper.clear()
        fake.pad.add(a["sha256"])
        with pytest.raises(Unverified, match="bigger than the manifest"):
            await fc.download(a["url"], a["size"], a["sha256"])
        fake.pad.clear()
        assert not list((tmp_path / "cache" / "assets").iterdir())  # nothing that failed is kept
        with pytest.raises(Unverified, match="no SHA-256"):
            await fc.download(a["url"], a["size"], "md5:abc")
        with pytest.raises(Unverified, match="plain http"):
            await fc.download("http://cdn.example.org/x", a["size"], a["sha256"])
        path = await fc.download(a["url"], a["size"], "sha256:" + a["sha256"].upper())
        sent = fc.sent
        assert await fc.download(a["url"], a["size"], a["sha256"]) == path and fc.sent == sent  # kept, rechecked
    asyncio.run(go())


def test_the_changes_feed_pages_from_as_of(fake, tmp_path):
    async def go():
        fc, _ = client(fake, tmp_path)
        lids = list(fake.listings)[:5]
        for lid in lids:
            fake.publish(lid)
        fake.withdraw(lids[0])
        since = forge.before(forge.utc_iso(), 60)
        changes, as_of = await fc.changes(since, page_size=2)
        assert len(changes) == 6 and changes[-1]["status"] == "withdrawn"
        assert changes[0]["published_version_ids"] and changes[-1]["withdrawn_version_ids"]  # the documented fields
        pages = [q for path, q, _ in fake.seen if path == "/api/listings/changes"]
        assert len(pages) == 3 and "updated_before" not in pages[0]
        assert all(q["updated_before"] == as_of and q["updated_since"] == since for q in pages[1:])
        fake.fail["/api/listings/changes"] = [200]  # a page without as_of differs from the docs: say so
        with pytest.raises(ForgeError, match="no as_of"):
            await fc.changes(since)
    asyncio.run(go())


def test_reading_listings_tolerantly():
    assert next_page({"next_cursor": "abc"}) == {"cursor": "abc"} and next_page({"next_cursor": None}) is None
    assert next_page({"page": 2, "total_pages": 3}) == {"page": 3} and next_page({"page": 3, "pages": 3}) is None
    x = {"name": "Pit", "type": "Game_Type", "owner": {"id": "u1", "display_name": "Juno"},
         "ratings": {"positive": 9, "negative": 1}, "recent_downloads": {"7d": 40}, "state": "Withdrawn"}
    assert (forge.title_of(x), forge.kind_of(x), forge.owner_of(x), forge.author_of(x)) == ("Pit", "gametype", "u1", "Juno")
    assert forge.votes_of(x) == (9, 1, 10) and forge.recent_of(x, "7d") == 40 and forge.is_withdrawn(x)


def days_ago(n: int) -> str:
    return forge.utc_iso(forge.datetime.now(forge.timezone.utc) - forge.timedelta(days=n))


def test_reading_the_documented_listing():
    x = {"id": "l1", "title": "Pit Stop", "owner_id": "u1", "upvote_count": 12, "downvote_count": 3, "vote_count": 15,
         "view_count": 900, "rating_average": 4.5, "rating_count": 99,
         "authors": [{"user_id": "u2", "username": "lowgrav", "avatar_url": "/a.png", "is_owner": False},
                     {"user_id": "u1", "username": "Kestrel", "avatar_url": "/b.png", "is_owner": True}],
         "metrics": {"recent_downloads": {"24h": 2, "7d": 30}, "download_history_started_at": days_ago(10)}}
    assert forge.votes_of(x) == (12, 3, 15)  # thumbs; the archived stars are ignored
    assert forge.votes_of({"rating_average": 4.5, "rating_count": 9}) == (None, None, None)
    assert forge.votes_of({"upvote_count": 4, "downvote_count": 1}) == (4, 1, 5)
    assert forge.votes_of({"upvote_count": 0, "downvote_count": 0, "vote_count": 0}) == (0, 0, 0)  # unrated
    assert forge.author_of(x) == "Kestrel, lowgrav" and forge.owner_of(x) == "u1"  # the owner first
    assert [a["user_id"] for a in forge.authors_of(x)] == ["u1", "u2"]
    assert forge.owner_of({"authors": x["authors"]}) == "u1" and forge.author_of({"author": "Juno"}) == "Juno"
    assert forge.views_of(x) == 900 and forge.views_of({"metrics": {"view_count": 4}}) == 4 and forge.views_of({}) is None
    assert forge.recent_of(x, "24h") == 2 and forge.recent_of(x, "7d") == 30  # in the metrics object
    assert forge.recent_of({"metrics": {"downloads_30d": 7}}, "30d") == 7 and forge.recent_of({}, "7d") is None

    # recent windows fill from deployment onward: a window that reaches back past it is only partly counted
    began = {"metrics": {"download_history_started_at": days_ago(3)}}
    assert forge.history_since(began, "30d") and forge.history_since(began, "7d")
    assert forge.history_since(began, "24h") is None  # history covers all of the last day
    assert forge.history_since(x, "30d") and forge.history_since(x, "7d") is None  # began ten days ago
    assert forge.history_since({}, "7d") is None and forge.history_since(began, "") is None


def test_versions_by_their_documented_fields():
    old = {"id": "v1", "version_label": "1.0", "created_at": "2026-09-01T00:00:00Z", "release_notes": "First **cut**."}
    new = {"id": "v2", "version_label": "1.1", "created_at": "2026-10-01T00:00:00Z", "status": "published"}
    gone = {"id": "v3", "version_label": "1.2", "created_at": "2026-10-05T00:00:00Z", "status": "withdrawn"}
    draft = {"id": "v4", "version_label": "1.3", "created_at": "2026-10-06T00:00:00Z", "status": "draft"}
    assert forge.version_label(old) == "1.0" and forge.version_label({"id": "v9", "version": "0.5"}) == "0.5"
    assert forge.notes_of(old) == "First **cut**." and forge.notes_of({"notes": "older spelling"}) == "older spelling"
    assert [forge.usable(v) for v in (old, new, gone, draft)] == [True, True, False, False]
    assert not forge.usable({"id": "v5", "withdrawn_at": "2026-10-02T00:00:00Z"}) and not forge.usable("v1")

    # a withdrawn version is never the latest, even when the listing's own latest_version still names it
    x = {"versions": [gone, draft, new, old], "latest_version": {"id": "v3", "version_label": "1.2"}}
    assert forge.latest_of(x)["id"] == "v2"
    assert forge.latest_of({**x, "latest_version": "v3"})["id"] == "v2"
    assert forge.latest_of({**x, "latest_version": "v1"})["id"] == "v1"  # one that is out stays as the listing says
    assert forge.latest_of({"versions": [gone]}) is None and forge.latest_of({}) is None
    assert forge.latest_of({"versions": [old, new]})["id"] == "v2"  # oldest first, as Forge may list them: by date
    assert forge.latest_of({"versions": [{"id": "a"}, {"id": "b"}]})["id"] == "a"  # undated: as listed

    # compatibility is set per version: the listing's own wins, else the newest version's
    assert forge.compat_of({"versions": [{**new, "compatibility": ">=0.9.6"}, old]}) == ">=0.9.6"
    assert forge.compat_of({"compatibility": "0.9.5", "versions": [{**new, "compatibility": ">=0.9.6"}]}) == "0.9.5"
    assert forge.compat_of({"versions": [new]}) is None and compatible(forge.compat_of({}), "0.9.7") is None


def test_text_from_strangers_is_safe_on_a_terminal():
    nasty = "Pit\x1b]0;owned\x07 Stop\x1b[2J\u202eGnp.exe\x9b31m\x00"
    assert forge.tidy(nasty) == "Pit]0;owned Stop[2JGnp.exe31m"  # no escape, bell, C1, null or bidi override left
    assert forge.tidy("a\n\n  b\tc\r\nd") == "a b c d" and forge.tidy(None) == "" and forge.tidy(7) == "7"
    assert forge.tidy("**one**\r\n\r\n- two\x1b[0m\n", lines=True) == "**one**\n\n- two[0m"  # Markdown's breaks stay
    assert forge.title_of({"title": nasty}) == "Pit]0;owned Stop[2JGnp.exe31m" and forge.title_of({}) == "untitled"
    assert "\x1b" not in forge.author_of({"authors": [{"user_id": "u", "username": "Ev\x1bil", "is_owner": True}]})
    assert "\x1b" not in forge.blurb_of({"description": "x\x1b[31m red"}) and "\x1b" not in forge.version_label(
        {"id": "v", "version_label": "1\x1b[0m"})
    assert forge.shorten("abcdef", 4) == "abc…" and forge.shorten("abc", 4) == "abc"
    # the Markdown itself is left as written: shown as text, never rendered
    assert forge.blurb_of({"description": "[x](javascript:alert(1)) <b>hi</b>"}) == "[x](javascript:alert(1)) <b>hi</b>"


def test_favourites_feed(fake, tmp_path):
    async def go():
        fc, _ = client(fake, tmp_path)
        cols, reply = await fc.favourites()
        assert [c["title"] for c in cols] == ["Staff picks"]  # the scheduled and the expired ones are off
        assert fake.seen[-1][0] == "/api/favourites" and reply["items"] and fc.quota.remaining == 119
        listings, notes = forge.favourite_listings(cols)
        assert [x["id"] for x in listings] == cols[0]["listing_ids"] and len(listings) == 4
        assert notes[listings[0]["id"]].startswith("Staff picks, until 20")  # which collection, and until when
        sent = fc.sent
        await fc.favourites()
        assert fc.sent == sent  # fresh in the cache
        await fc.favourites(fresh=True)
        assert fc.sent == sent + 1

        fake.fail["/api/favourites"] = [503] * 4  # Forge down: the last copy, flagged, and still only what is on now
        old, reply = await fc.favourites(fresh=True)
        assert old and reply["_stale"] >= 0
        fake.collections[0]["ends_at"] = forge.utc_iso(forge.datetime.now(forge.timezone.utc)
                                                       + forge.timedelta(seconds=2))
        assert (await fc.favourites(fresh=True))[0]  # still on, and now cached with that end
        await asyncio.sleep(2.2)
        fake.fail["/api/favourites"] = [503] * 4
        cols, reply = await fc.favourites(fresh=True)
        assert cols == [] and reply["_stale"] >= 0  # the old copy's collection has ended since: not shown
    asyncio.run(go())


def test_a_favourites_collection_is_on_from_its_start_until_its_end():
    t = forge.parse_iso("2026-10-09T12:00:00Z")
    at = lambda starts, ends: forge.active_now({"starts_at": starts, "ends_at": ends}, t)
    assert at("2026-10-09T12:00:00Z", "2026-10-10T00:00:00Z")  # the start is inclusive
    assert not at("2026-10-09T00:00:00Z", "2026-10-09T12:00:00Z")  # the end is exclusive
    assert not at("2026-10-09T12:00:01Z", None) and at(None, "2026-10-09T12:00:01Z") and at(None, None)
    a = {"id": "a", "title": "Picks", "ends_at": "2026-10-12T00:00:00Z", "listing_ids": ["2", "1", "9"],
         "listings": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
    b = {"id": "b", "title": "More", "listing_ids": ["3", "2"], "listings": [{"id": "2"}, {"id": "3"}]}
    got, notes = forge.favourite_listings([a, b])
    assert [x["id"] for x in got] == ["2", "1", "3"]  # each collection's order, the unlisted last, no repeats
    assert notes == {"2": "Picks, until 2026-10-12", "1": "Picks, until 2026-10-12", "3": "Picks, until 2026-10-12"}
    assert forge.favourite_listings([]) == ([], {})


def test_changes_are_told_apart_by_ids():
    legacy = {"listing_id": "l1", "version_id": "v1", "change": "version_published", "updated_at": "T1"}
    assert forge.change_key(legacy) == "l1|v1|version_published|T1"  # the form already kept in forge-state.json
    new = {"listing_id": "l1", "updated_at": "T1", "published_version_ids": ["v2", "v1"], "withdrawn_version_ids": ["v0"]}
    assert forge.change_key(new) == "l1|||T1|pub:v1,v2|wd:v0" and forge.change_listing({"id": "l9"}) == "l9"
    assert forge.change_key({**new, "published_version_ids": ["v1", "v2", "v2"]}) == forge.change_key(new)
    assert forge.change_key({**new, "withdrawn_version_ids": []}) != forge.change_key(new)
    assert forge.id_list(["a", 2, True, "", None, "a", {"x": 1}]) == ["a", "2"] and forge.id_list("a") == []


def test_compatibility():
    assert compatible(">=0.9.5", "0.9.7-demo") and not compatible("0.9.6", "0.9.7")
    assert compatible("0.9.x", "0.9.7") and compatible(">=0.9.5, <0.10", "0.9.7") and compatible("<=0.9", "0.9.7")
    assert not compatible(">0.9.7", "0.9.7") and compatible(["0.9.6", "0.9.7"], "0.9.7")
    assert compatible({"min": "0.9.5"}, "0.9.7") and not compatible({"max": "0.9.6"}, "0.9.7")
    assert compatible({"reclaimer": ">=0.9.8"}, "0.9.7") is False
    assert compatible(None, "0.9.7") is None and compatible(">=0.9", "") is None and compatible("soon", "0.9.7") is None


# --- the F6 tab ---------------------------------------------------------------------------------------------------
async def until(pilot, cond, tries: int = 120) -> bool:
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return True
    return False


def plain(widget) -> str:
    """What a Static shows, as text: rendered the way Rich would, Groups and tables included."""
    import io

    from rich.console import Console
    console = Console(width=120, file=io.StringIO(), record=True, color_system=None)
    console.print(widget.content)
    return console.export_text()


def forge_app(ports, fk, tmp_path, key=KEY, servers=None, **kw):
    from oni_rcon.app import OniApp
    from oni_rcon.config import Server
    from oni_rcon.forge import ForgeSetup
    setup = ForgeSetup(Secret(key) if key else None, "pytest" if key else "", url=fk.base, poll=3600,
                       state_dir=tmp_path / "state", cache_dir=tmp_path / "cache", **kw)
    return OniApp(servers or [Server(port=p, password="demo") for p in ports], by="pytest", intro=False, forge=setup)


def test_the_forge_tab(fake, tmp_path):
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = forge_app(ports, fake, tmp_path)
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: all(st.online for st in app.stations))
            assert not any(path.startswith("/api") for path, *_ in fake.seen)  # nothing asked until F6 is opened
            await pilot.press("f6")
            t = app.query_one("#listings")
            assert await until(pilot, lambda: t.row_count == 25)
            assert fake.seen[0][1]["sort"] == "trending"
            assert "requests left this minute" in t.border_subtitle
            assert await until(pilot, lambda: app.query_one("#versions").row_count > 0)  # the file brings versions
            file = plain(app.query_one("#listing"))
            x = app.cur_listing()
            assert "CATALOG ENTRY" in file and x["title"] in file and forge.author_of(x) in file and "on Slayer" in file
            assert "VIEWS" in file and "▲" in file or "no votes" in file  # thumbs, not stars
            assert "Section Three" in plain(app.query_one("#forge-credit"))

            app.query_one("#listings").focus()
            await pilot.press("s")  # trending -> rising, fetched again
            assert await until(pilot, lambda: any(q.get("sort") == "rising" for _, q, _ in fake.seen))
            await pilot.press("s")  # rising -> latest: a window means nothing to it
            assert await until(pilot, lambda: app.query_one("#forge-window").disabled)

            await pilot.press("slash")
            assert app.focused is app.query_one("#forge-q")
            await pilot.press(*"grif")
            assert t.row_count == 1  # narrows what's loaded as it's typed
            await pilot.press("enter")
            assert await until(pilot, lambda: any(q.get("q") == "grif" for _, q, _ in fake.seen))

            fake.fail["/api/listings"] = [503] * 8
            app.query_one("#forge-q").value = ""
            await pilot.press("ctrl+r")  # fresh, past the cache: Forge is down, so the last copy shows, flagged
            assert await until(pilot, lambda: "OFFLINE COPY" in str(t.border_subtitle), 200)
            seen = "\n".join(line.text for line in app.query_one("#console-log").lines)
            assert KEY not in seen and KEY not in plain(app.query_one("#listing-raw"))
    asyncio.run(go())


def test_the_favourites_view(fake, tmp_path):
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        app = forge_app(ports, fake, tmp_path)
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: app.cur.online)
            await pilot.press("f6")
            t = app.query_one("#listings")
            assert await until(pilot, lambda: t.row_count == 25)
            app.query_one("#forge-sort").value = "favourites"
            assert await until(pilot, lambda: t.row_count == 4)  # the one collection that is on
            assert "FAVOURITES" in str(t.border_title) and app.query_one("#forge-window").disabled
            assert [lid for lid in t.ids] == fake.collections[0]["listing_ids"]  # in the collection's order
            assert await until(pilot, lambda: "FEATURED" in plain(app.query_one("#listing")))
            assert "Staff picks, until" in plain(app.query_one("#listing"))
            assert app.query_one("#fg-more").disabled  # one unpaginated feed: no next page

            app.query_one("#forge-sort").value = "latest"  # an ordinary order forgets what was featured
            assert await until(pilot, lambda: t.row_count == 25 and "FEATURED" not in plain(app.query_one("#listing")))

            fake.collections.clear()
            app.query_one("#forge-sort").value = "favourites"
            await pilot.press("ctrl+r")  # past the cache: nothing is featured now, and it says so
            assert await until(pilot, lambda: t.row_count == 0
                               and "isn't featuring anything" in plain(app.query_one("#listing")))
    asyncio.run(go())


def test_a_withdrawn_version_flags_only_the_install_it_was(fake, tmp_path):
    from oni_rcon.app import Confirm
    from oni_rcon.config import Server
    from oni_rcon.demo import serve_fakes

    async def go():
        shared = tmp_path / "content"
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)], content_dirs=[shared])
        app = forge_app(ports, fake, tmp_path, servers=[Server(port=ports[0], password="demo", content_dir=str(shared))])
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: app.cur.online and "nextmap" in app.cur.data)
            st = app.cur
            x = next(x for x in fake.listings.values() if x["kind"] == "map")
            lid = x["id"]
            fake.publish(lid)  # at least two versions, so the newest can be withdrawn and the listing stay up
            first = fake.listings[lid]["latest_version"]
            entry = {"listing_id": lid, "version_id": first["id"], "version": first["version_label"], "title": x["title"],
                     "kind": "map", "reference": "r", "files": [], "installed_at": forge.utc_iso()}
            app.fstate.record([st.server.where], entry)
            toasts = lambda title: [n for n in app._notifications if n.title == title]

            await app._forge_watch()
            await pilot.pause()  # it published what is installed already: not news
            assert not toasts("FORGE · UPDATE") and app.condition()[0] == "GREEN"

            fake.withdraw_version(lid, first["id"])  # the installed one, by its author; the listing stays up
            older = fake.listings[lid]["latest_version"]["id"]
            assert older != first["id"]
            await app._forge_watch()
            await pilot.pause()
            assert len(toasts("FORGE · VERSION WITHDRAWN")) == 1 and not toasts("FORGE · UPDATE")  # a step back isn't news
            assert not toasts("FORGE · WITHDRAWN") and app.condition()[0] == "AMBER"
            assert app.fstate.is_withdrawn(lid) and not app.fstate.blocks(lid) and app.fstate.alarms() == [lid]
            await app._forge_watch()
            await pilot.pause()  # the feed is read back over the overlap: the same change isn't news twice
            assert len(toasts("FORGE · VERSION WITHDRAWN")) == 1

            await pilot.press("f6")  # the listing: ⚠ in the catalog, the version struck, the install still open
            q = app.query_one("#forge-q")
            q.value = x["title"]
            await pilot.press("slash")
            await pilot.press("enter")
            t, vt = app.query_one("#listings"), app.query_one("#versions")
            assert await until(pilot, lambda: lid in t.ids and lid in app.details and vt.row_count > 1)
            t.move_cursor(row=t.ids.index(lid))
            assert await until(pilot, lambda: first["id"] in vt.ids)
            assert "⚠" in str(t.get_row_at(t.ids.index(lid))[0])
            assert "⚠" in str(vt.get_row_at(vt.ids.index(first["id"]))[0])  # withdrawn, and it's the one here
            assert vt.selected == older  # the cursor starts on the newest version that can be installed
            assert not app.query_one("#fg-install").disabled and "installed version withdrawn" in plain(
                app.query_one("#listing"))

            vt.move_cursor(row=vt.ids.index(first["id"]))  # but the withdrawn one itself is never installed
            vt.focus()
            await pilot.press("i")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, Confirm)
            assert any("withdrawn that version" in n.message for n in app._notifications)
            vt.move_cursor(row=vt.ids.index(older))
            await pilot.press("i")
            # the screen is pushed before its buttons are mounted: a slow runner got there first
            assert await until(pilot, lambda: isinstance(app.screen, Confirm) and len(app.screen.query("#no")) > 0)
            app.screen.query_one("#no").press()

            new = fake.publish(lid)
            await app._forge_watch()
            await pilot.pause()
            assert len(toasts("FORGE · UPDATE")) == 1 and app.fstate.latest(lid)["id"] == new
            app.fstate.record([st.server.where], {**entry, "version_id": new, "version": "9.9"})  # installed: it's clear
            assert not app.fstate.is_withdrawn(lid) and app.condition()[0] == "GREEN"
    asyncio.run(go())


def test_the_older_change_spelling_still_reads(fake, tmp_path):
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        app = forge_app(ports, fake, tmp_path)
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: app.cur.online and "nextmap" in app.cur.data)
            x = next(x for x in fake.listings.values() if x["kind"] == "map")
            lid, v = x["id"], x["latest_version"]
            app.fstate.record([app.cur.server.where], {
                "listing_id": lid, "version_id": v["id"], "version": v["version_label"], "title": x["title"],
                "kind": "map", "reference": "r", "files": [], "installed_at": forge.utc_iso()})
            new = fake.publish(lid)
            fake.changes[-1] = {"listing_id": lid, "version_id": new, "version": "1.9", "change": "version_published",
                                "updated_at": fake.changes[-1]["updated_at"]}
            await app._forge_watch()
            await pilot.pause()
            assert [n for n in app._notifications if n.title == "FORGE · UPDATE"] and app.fstate.latest(lid)["id"] == new

            fake.fail["/api/listings/" + lid] = [503] * 8  # the listing can't be fetched: the feed's word is enough
            newer = fake.publish(lid)
            fake.changes[-1] = {"listing_id": lid, "published_version_ids": [newer], "updated_at": fake.changes[-1]["updated_at"]}
            await app._forge_watch()
            await pilot.pause()
            assert app.fstate.latest(lid)["id"] == newer
    asyncio.run(go())


def test_late_forge_events_while_quitting_are_ignored(fake, tmp_path):
    """A RowHighlighted, Select.Changed or Input event still queued as the app quits is handled with its widgets
    already gone. That raised NoMatches('#listing') on CI, after the test itself had passed."""
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        app = forge_app(ports, fake, tmp_path)
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: app.cur.online)
            await pilot.press("f6")
            assert await until(pilot, lambda: app.query_one("#listings").row_count == 25)
        assert not app.is_running
        for handler in (app._listing_row, app._forge_order, app._forge_filter):
            handler(None)  # the widgets are gone: nothing to paint, and nothing raised
        app._forge_search(type("Late", (), {"stop": lambda self: None})())
    asyncio.run(go())


def test_late_row_station_and_tab_events_while_quitting_are_ignored(tmp_path):
    """The same race in the handlers every tab shares: a highlight or tab event still queued as the app quits was
    handled with its widgets gone. CI hit it in _row (NoMatches '#dossier') after a test's last repaint."""
    from types import SimpleNamespace as NS

    from oni_rcon.app import OniApp
    from oni_rcon.config import Server
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("A", 16, 2), ("B", 16, 2)])
        app = OniApp([Server(port=p, password="demo") for p in ports], by="pytest", intro=False)
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: all(st.online for st in app.stations))
            app.stations[0].alerts = 1  # something unseen, so opening the feed would repaint
        assert not app.is_running
        app._row(None)
        app._station(NS(list_view=NS(index=1)))  # another station chosen: it would repaint everything
        app._tab(NS(pane=NS(id="intercepts")))
    asyncio.run(go())


def test_the_forge_tab_without_a_key(fake, tmp_path):
    from oni_rcon.app import Form
    from oni_rcon.demo import serve_fakes

    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        cfg = tmp_path / "c.toml"
        cfg.write_text("[[server]]\nport = 1\n")
        app = forge_app(ports, fake, tmp_path, key=None, config=cfg)
        async with app.run_test(size=(170, 50)) as pilot:
            await pilot.press("f6")
            await pilot.pause()
            assert "NO KEY" in plain(app.query_one("#listing")) and not fake.seen
            app.query_one("#listings").focus()
            await pilot.press("k")
            assert await until(pilot, lambda: isinstance(app.screen, Form))
            assert app.screen.query_one("#field-remember").value == "no"  # remembering is opted into
            app.screen.query_one("#field-key").value = KEY
            app.screen.query_one("#field-key").focus()
            await pilot.press("enter")
            assert await until(pilot, lambda: app.query_one("#listings").row_count == 25)
            assert fake.seen[-1][2] and KEY not in cfg.read_text()  # sent with the key; not saved
            assert "TYPED THIS SESSION" in str(app.query_one("#listing-box").border_subtitle)
    asyncio.run(go())


# --- installing ---------------------------------------------------------------------------------------------------
def test_only_safe_files_are_written():
    from oni_rcon.install import InstallError, plan, safe_path
    assert safe_path("guardian_rebuilt.map") == "guardian_rebuilt.map" and safe_path("maps\\a b.map") == "maps/a b.map"
    for bad in ("../evil.map", "/etc/passwd", "C:\\x.map", ".ssh/authorized_keys", "a/../../b", "", "x/./y",
                "trailing.", "a/b/c/d/e.map"):
        with pytest.raises(InstallError):
            safe_path(bad)
    good = {"path": "a.map", "url": "/assets/x", "size": 3, "sha256": "ab" * 32}
    assert plan({"assets": [good]})[0].sha256 == "ab" * 32
    for broken, words in [({**good, "sha256": "md5:1"}, "no SHA-256"), ({**good, "size": -1}, "no size"),
                          ({**good, "url": ""}, "no link")]:
        with pytest.raises(InstallError, match=words):
            plan({"assets": [broken]})
    with pytest.raises(InstallError, match="twice"):
        plan({"assets": [good, {**good, "path": "A.map"}]})
    with pytest.raises(InstallError, match="no files"):
        plan({"assets": []})


def test_the_state_file(tmp_path):
    from oni_rcon.state import ForgeState, refs_of
    p = tmp_path / "forge-state.json"
    st = ForgeState(p)
    entry = {"listing_id": "lst_1", "version_id": "ver_1", "title": "Pit Stop", "reference": "pit_stop",
             "files": [{"path": "maps/pit-stop.map"}]}
    st.record(["a:1", "b:2"], entry)
    again = ForgeState(p)
    assert again.ids() == {"lst_1"} and again.where("lst_1") == ["a:1", "b:2"] and again.installed("a:1")["lst_1"]
    assert refs_of(entry) == {"pit_stop"}
    p.write_text("{not json")
    assert not ForgeState(p).ids() and (tmp_path / "forge-state.json.unreadable").exists()  # kept aside, not lost


def test_a_withdrawn_version_in_the_state_file(tmp_path):
    from oni_rcon.state import ForgeState
    p = tmp_path / "forge-state.json"
    st = ForgeState(p)
    one = {"listing_id": "l1", "version_id": "v1", "version": "1.0", "title": "Pit", "files": []}
    st.record(["a:1", "b:2"], one)
    st.record(["c:3"], {**one, "version_id": "v0", "version": "0.9"})  # an older one elsewhere

    assert st.withdraw("l1", "v1") and not st.withdraw("l1", "v1")  # news once
    assert st.is_withdrawn("l1") and not st.blocks("l1")  # the listing is up: a newer version can be installed
    assert st.pulled("l1", one) and not st.pulled("l1", st.installed("c:3")["l1"])  # only the install that was it
    assert st.alarms() == ["l1"] and st.where("l1", "v1") == ["a:1", "b:2"] and st.where("l1") == ["a:1", "b:2", "c:3"]
    st.restore("l1")  # the listing coming back says nothing about one withdrawn version
    assert st.is_withdrawn("l1") and ForgeState(p).is_withdrawn("l1")
    st.acknowledge("l1")
    assert st.alarms() == []

    st.record(["a:1"], {**one, "version_id": "v2", "version": "1.1"})  # one server moves on: b:2 still has it
    assert st.is_withdrawn("l1")
    st.record(["b:2"], {**one, "version_id": "v2", "version": "1.1"})  # the last one: the flag clears
    assert not st.is_withdrawn("l1") and not ForgeState(p).is_withdrawn("l1")

    assert st.withdraw("l1", "v2") and st.withdraw("l1")  # then the whole listing goes: that replaces it, as news
    assert st.blocks("l1") and st.pulled("l1", st.installed("c:3")["l1"]) and not st.withdraw("l1")
    st.restore("l1")
    assert not st.is_withdrawn("l1")


def install_files(fake, tmp_path, target):
    """Download a listing's latest version through the client, then put it with `target`: (files, listing)."""
    from oni_rcon.install import plan

    async def go():
        fc, _ = client(fake, tmp_path)
        x = next(x for x in (await fc.listings("latest"))[0] if x["kind"] == "map")
        files = plan(await fc.manifest(x["id"], forge.latest_of(x)["id"]))
        assert await target.existing([f.path for f in files]) == set()
        for f in files:
            await target.put(await fc.download(f.url, f.size, f.sha256), f)
        assert await target.existing([f.path for f in files] + ["nope.map"]) == {f.path for f in files}
        return files, x
    return asyncio.run(go())


def test_install_on_this_machine(fake, tmp_path):
    from oni_rcon.install import LocalTarget
    files, _ = install_files(fake, tmp_path, LocalTarget(str(tmp_path / "content")))
    f = files[0]
    assert hashlib.sha256((tmp_path / "content" / f.path).read_bytes()).hexdigest() == f.sha256
    assert not list((tmp_path / "content").rglob("*.part"))


@pytest.mark.skipif(sys.platform == "win32", reason="stands in for ssh with a POSIX shell script")
def test_install_over_ssh(fake, tmp_path, monkeypatch):
    from oni_rcon.install import File, InstallError, SshTarget, rpath
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    ssh = bin_ / "ssh"  # runs the remote command here, as the game box's shell would
    ssh.write_text('#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\nshift\nexec sh -c "$1"\n')
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert rpath("~/content", "a b.map") == '"$HOME"/\'content/a b.map\''
    files, _ = install_files(fake, tmp_path, SshTarget("admin@game-box", "~/reclaimer/content"))
    landed = tmp_path / "home" / "reclaimer" / "content" / files[0].path
    assert hashlib.sha256(landed.read_bytes()).hexdigest() == files[0].sha256

    bogus = tmp_path / "bogus.map"
    bogus.write_bytes(b"not what the manifest says")
    target = SshTarget("admin@game-box", "~/reclaimer/content")
    with pytest.raises(InstallError, match="doesn't match the manifest's SHA-256"):
        asyncio.run(target.put(bogus, File("odd name's.map", "", 26, "ab" * 32)))
    assert not list(landed.parent.glob("*.part")) and not (landed.parent / "odd name's.map").exists()


def test_install_and_load_from_f6(fake, tmp_path):
    from oni_rcon.app import Confirm, Pick
    from oni_rcon.config import Server
    from oni_rcon.demo import serve_fakes
    from oni_rcon.forge import ForgeSetup

    async def go():
        shared = tmp_path / "shared"
        ports, keep = await serve_fakes(tick=False, content_dirs=[shared, shared, tmp_path / "other"])
        servers = [Server(port=ports[0], password="demo", content_dir=str(shared)),
                   Server(port=ports[1], password="demo", content_dir=str(shared)),
                   Server(port=ports[2], password="demo")]  # no content_dir: says so
        app = forge_app(ports, fake, tmp_path, servers=servers)
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: all(st.online and "maps" in st.data for st in app.stations))
            await pilot.press("f6")
            t = app.query_one("#listings")
            assert await until(pilot, lambda: t.row_count == 25)
            row = next(i for i, lid in enumerate(t.ids) if app.listing_rows[lid]["kind"] == "map")
            t.focus()
            t.move_cursor(row=row)
            lid = t.selected
            assert await until(pilot, lambda: lid in app.details)
            await pilot.press("i")
            assert await until(pilot, lambda: isinstance(app.screen, Confirm))
            assert app.screen.verb == "INSTALL" and "Big Team Battle" in app.screen.b  # it shares the folder
            assert app.screen.focused.id == "no"  # ABORT by default
            app.screen.query_one("#yes").press()
            assert await until(pilot, lambda: lid in app.fstate.ids(), 200)
            entry = app.fstate.installed(app.stations[0].server.where)[lid]
            assert app.fstate.installed(app.stations[1].server.where)[lid] == entry  # theirs too
            assert (shared / entry["files"][0]["path"]).is_file() and "◉" in str(t.get_row_at(t.ids.index(lid))[0])
            assert await until(pilot, lambda: app.listed(app.stations[0], entry))  # the server lists it now

            await pilot.press("i")  # again: it's there, so it asks to replace, and Enter means ABORT
            assert await until(pilot, lambda: isinstance(app.screen, Confirm))
            assert app.screen.verb == "REPLACE" and app.screen.danger
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, Confirm)

            await pilot.press("l")  # load now: its map, a mode to go with it, a confirm
            assert await until(pilot, lambda: isinstance(app.screen, Pick))
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, Confirm))
            app.screen.query_one("#yes").press()
            fake_rcon = keep[0]
            assert await until(pilot, lambda: fake_rcon.status["map"] == entry["reference"])

            await pilot.press("3")
            await pilot.pause(0.2)
            await pilot.press("f6")
            app.query_one("#listings").focus()
            await pilot.press("i")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, Confirm)  # no content_dir: nothing to confirm, a toast says why
            assert any("content_dir" in n.message for n in app._notifications)
    asyncio.run(go())


# --- the watcher --------------------------------------------------------------------------------------------------
def test_updates_and_withdrawals(fake, tmp_path):
    from oni_rcon.app import Confirm
    from oni_rcon.demo import serve_fakes
    from oni_rcon.forgefake import slug

    async def go():
        ports, keep = await serve_fakes(tick=False)
        app = forge_app(ports, fake, tmp_path)
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: all(st.online and "nextmap" in st.data for st in app.stations))
            st = app.stations[0]
            x = next(x for x in fake.listings.values() if x["kind"] == "map")
            lid, v = x["id"], x["latest_version"]
            entry = {"listing_id": lid, "version_id": v["id"], "version": v["version_label"], "title": x["title"],
                     "kind": "map", "reference": slug(x["title"]), "files": [], "installed_at": forge.utc_iso()}
            app.fstate.record([st.server.where], entry)
            toasts = lambda title: [n for n in app._notifications if n.title == title]

            await app._forge_watch()
            await pilot.pause()  # nothing new: no toast, and the installed listings were fetched once whole
            assert not toasts("FORGE · UPDATE") and app.fstate.data.get("reconciled")
            new = fake.publish(lid)
            await app._forge_watch()
            await pilot.pause()
            assert len(toasts("FORGE · UPDATE")) == 1 and "Slayer" in toasts("FORGE · UPDATE")[0].message
            await app._forge_watch()
            await pilot.pause()  # the feed is read back over the overlap: the same change isn't news twice
            assert len(toasts("FORGE · UPDATE")) == 1 and app.fstate.latest(lid)["id"] == new
            pages = [q for path, q, _ in fake.seen if path == "/api/listings/changes"]
            assert forge.parse_iso(pages[-1]["updated_since"]) < forge.parse_iso(app.fstate.data["since"])

            assert app.condition()[0] == "GREEN"
            fake.withdraw(lid)
            await app._forge_watch()
            await pilot.pause()
            assert app.condition()[0] == "AMBER" and app.fstate.alarms() == [lid]
            assert len(toasts("FORGE · WITHDRAWN")) == 1
            st.data["nextmap"] = {"rotation": [{"map": "guardian", "mode": "Slayer"},
                                               {"map": entry["reference"], "mode": "Slayer"}]}
            app.paint_ops()
            rot = app.query_one("#rotation")
            assert "WITHDRAWN" in str(rot.get_row_at(1)[1]) and "WITHDRAWN" not in str(rot.get_row_at(0)[1])

            await pilot.press("f6")
            app.query_one("#forge-sort").value = "installed"  # what's on this server, from forge-state.json
            t = app.query_one("#listings")
            assert await until(pilot, lambda: t.row_count == 1 and "⚠" in str(t.get_row_at(0)[0]))
            t.focus()
            await pilot.press("a")
            assert await until(pilot, lambda: isinstance(app.screen, Confirm))
            app.screen.query_one("#yes").press()
            assert await until(pilot, lambda: not app.fstate.alarms())
            assert app.condition()[0] == "GREEN" and app.fstate.is_withdrawn(lid)  # still flagged, just seen
    asyncio.run(go())


# --- now playing --------------------------------------------------------------------------------------------------
def test_now_playing_credit(fake, tmp_path):
    from oni_rcon.demo import serve_fakes
    from oni_rcon.state import ForgeState

    st = ForgeState(None)
    st.record(["s"], {"listing_id": "l1", "kind": "map", "title": "Pit Stop", "reference": "pit_stop", "files": []})
    st.record(["s"], {"listing_id": "l2", "kind": "gametype", "title": "Grifball", "reference": "grifball", "files": []})
    assert [e["listing_id"] for e in st.playing("s", "Pit Stop", "Grifball")] == ["l1", "l2"]
    assert st.playing("s", "grifball", "pit_stop") == []  # a map's name as a mode isn't it
    assert st.playing("s", "the_pit", "Slayer") == [] and st.playing("other", "pit_stop", "") == []

    async def go():
        ports, keep = await serve_fakes(tick=False, specs=[("Probe", 16, 2)])
        app = forge_app(ports, fake, tmp_path)
        async with app.run_test(size=(170, 50)) as pilot:
            assert await until(pilot, lambda: app.cur.online and "status" in app.cur.data)
            app.fstate.record([app.cur.server.where], {"listing_id": "l1", "kind": "map", "title": "Pit Stop",
                                                       "author": "Kestrel", "version": "1.2", "reference": "pit_stop",
                                                       "files": []})
            await app.cur.rcon.call("load", "pit_stop", "slayer")
            await app._fetch(app.cur, ("status",))
            await pilot.pause()
            text = plain(app.query_one("#sitrep"))
            assert "FORGE" in text and "Pit Stop  by Kestrel  v1.2" in text
    asyncio.run(go())


# --- rounds played ------------------------------------------------------------------------------------------------
def test_rounds_are_counted_locally(tmp_path, monkeypatch):
    from oni_rcon import stats
    from oni_rcon.stats import Rounds
    clock = [1000.0]
    monkeypatch.setattr(stats.time, "monotonic", lambda: clock[0])
    r = Rounds(tmp_path / "rounds.jsonl", client="oni-rcon/test")
    s1 = "127.0.0.1:11774 via ssh admin@game-box"
    credit = lambda m, g: [{"listing_id": "l1", "version_id": "v1", "kind": "map"}] if m == "pit_stop" else []
    status = lambda phase, mp="pit_stop", md="Slayer", n=8: {"phase": phase, "map": mp, "mode": md, "players": n,
                                                              "address": "203.0.113.7:1", "name": "Probe | Slayer"}

    assert r.observe(s1, status("in_game"), credit) is None  # joined partway through: not seen whole
    clock[0] += 300
    r.observe(s1, status("in_game", n=12), credit)
    done = r.observe(s1, status("post_game", n=10), credit)
    assert done["seconds"] == 300 and done["players_peak"] == 12 and done["players_end"] == 10
    assert done["seen_whole"] is False and done["forge"] == [{"listing_id": "l1", "version_id": "v1", "kind": "map"}]

    r.observe(s1, status("in_game", mp="guardian"), credit)  # seen to start this time
    done = r.observe(s1, status("in_game", mp="narrows"), credit)  # a map change ends one round, starts the next
    assert done["map"] == "guardian" and done["seen_whole"] is True and done["forge"] == []
    r.lost(s1)  # the link dropped: the narrows round's end is unknown, so it isn't counted
    assert r.observe(s1, status("post_game"), credit) is None

    lines = [json.loads(x) for x in (tmp_path / "rounds.jsonl").read_text().splitlines()]
    assert len(lines) == 2 and len({x["id"] for x in lines}) == 2 and lines[0]["schema"] == 1
    assert lines[0]["client"] == "oni-rcon/test" and lines[0]["server"] == "Probe | Slayer"  # its public name
    assert not any(k in json.dumps(lines) for k in ("203.0.113", "admin@", "11774"))  # never how it's reached
    assert Rounds(None).observe("s", status("in_game")) is None  # nowhere to keep them: counted in memory only
