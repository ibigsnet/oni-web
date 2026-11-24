"""The Settings page's schema, its checks and its writes into dedicated.toml."""
import asyncio
import json

import pytest
import tomlkit

from oni_web import server_settings as ss
from oni_web.hub import Hub
from oni_web.operations import dispatch
from oni_web.settings import Settings
from oni_web.vault import Vault

CONFIG = '''# The servers on this machine.
host_name = "Every Server"
admins = ["admin-everywhere"]
moderators = []

[rcon]
password = "rcon-secret-do-not-show"

[defaults]
max_players = 12
friendly_fire = false   # the house rule
password = "join-secret-default"
[defaults.votes]
kick = "members"
endgame = "off"

[roles.vip]
players = ["vip-everywhere"]

[[server]]
name = "First"
port = 49176
playlist = "playlists/slayer.playlist.json"

[[server]]
name = "Second"
port = 49178
rcon_port = 49179
rcon_password = "second-rcon-secret"
map = "Guardian"
game = "Slayer"
'''


def doc():
    return tomlkit.parse(CONFIG)


def value(read, key):
    return next(v for v in read["values"] if v["key"] == key)


def second(d):
    return [s for s in d["server"] if s["name"] == "Second"][0]


# --- the schema -------------------------------------------------------------------------------------------------

def test_schema_is_complete_and_exposes_nothing_dangerous():
    groups = {g for g, _ in ss.GROUPS}
    keys = [e["key"] for e in ss.SCHEMA]
    assert len(keys) == len(set(keys))
    for e in ss.SCHEMA:
        assert e["group"] in groups and e["label"] and e["description"]
        assert e["applies"] in (ss.LIVE, ss.NEXT_GAME, ss.SERVER_RESTART, ss.SUPERVISOR_RESTART)
        assert e["scope"] in ("server", "defaults", "top", "additive", "global")
        assert isinstance(e["advanced"], bool)
        assert e["key"].split(".")[0] not in ss.FORBIDDEN
        if e["type"] == "int":
            assert e["min"] <= e["max"]
            assert e["default"] is None or e["min"] <= e["default"] <= e["max"]
        if e["type"] == "enum" and e["default"] is not None:
            assert e["default"] in [c[0] for c in e["choices"]]
    for never in ("rcon_password", "rcon_port", "port", "bind_address", "public_address", "game_folder", "master", "upnp", "data"):
        assert never not in keys
    for wanted in ("name", "host_name", "password", "max_players", "min_players", "max_ping", "hidden", "welcome",
                   "admins", "moderators", "vips", "vote_kick_immune", "map", "game", "playlist", "friendly_fire",
                   "team_changing", "stock_map_defaults", "round_outlasts_leavers", "campaign.mission",
                   "campaign.difficulty", "campaign_skip_vote_percent", "coop_network", "share_mods", "share_rate",
                   "forge_edit", "forge_autosave_minutes", "odst_objects", "kick_blocks", "joins", "anti_cheat",
                   "film_recording", "film_keep", "stats_server", "network_model", "tick_rate", "encode_once",
                   "channel_threads"):
        assert wanted in keys


# --- reading ----------------------------------------------------------------------------------------------------

def test_read_shows_own_inherited_and_default_values_and_never_a_password():
    read = ss.read_settings(doc(), 49176)
    assert value(read, "name") == {"key": "name", "value": "First", "inherited": None, "source": "server"}
    assert value(read, "friendly_fire")["value"] is False and value(read, "friendly_fire")["source"] == "defaults"
    assert value(read, "max_players")["value"] == 12
    assert value(read, "round_outlasts_leavers") == {"key": "round_outlasts_leavers", "value": False, "inherited": False, "source": "default"}
    assert value(read, "host_name")["value"] == "Every Server" and value(read, "host_name")["source"] == "all_servers"
    assert value(read, "admins")["value"] == [] and value(read, "admins")["inherited"] == ["admin-everywhere"]
    assert value(read, "vips")["inherited"] == ["vip-everywhere"]
    assert value(read, "votes.kick")["value"] == "members" and value(read, "votes.shuffle")["value"] == "everyone"
    pw = value(read, "password")
    assert pw["value"] is None and pw["is_set"] and pw["source"] == "defaults"
    assert read["plays"] == "playlist"
    text = json.dumps(read)
    assert "join-secret-default" not in text and "rcon-secret" not in text
    # The second server is found by its RCON port.
    assert value(ss.read_settings(doc(), 49179), "map")["value"] == "Guardian"


# --- writing ----------------------------------------------------------------------------------------------------

def test_a_change_goes_into_that_servers_entry_only_and_keeps_the_file_as_written():
    d = doc()
    ss.apply_changes(d, 49179, {"friendly_fire": True, "max_players": 24, "hidden": True})
    out = tomlkit.dumps(d)
    assert "# the house rule" in out and "# The servers on this machine." in out
    again = tomlkit.parse(out)
    s2 = second(again)
    assert (s2["friendly_fire"], s2["max_players"], s2["hidden"]) == (True, 24, True)
    assert "friendly_fire" not in again["server"][0]
    assert again["defaults"]["friendly_fire"] is False
    assert s2["rcon_password"] == "second-rcon-secret" and s2["rcon_port"] == 49179 and s2["port"] == 49178


def test_an_override_can_be_cleared_so_defaults_apply_again():
    d = doc()
    ss.apply_changes(d, 49176, {"friendly_fire": True})
    assert value(ss.read_settings(d, 49176), "friendly_fire")["source"] == "server"
    ss.apply_changes(d, 49176, {"friendly_fire": None})
    read = ss.read_settings(d, 49176)
    assert value(read, "friendly_fire") == {"key": "friendly_fire", "value": False, "inherited": False, "source": "defaults"}
    assert "friendly_fire" not in d["server"][0]


def test_lists_roles_votes_and_global_settings_are_written_where_the_game_reads_them():
    d = doc()
    ss.apply_changes(d, 49176, {"vips": ["vip-one", "vip-one", "vip-two"], "moderators": ["mod-one"],
                                "votes.shuffle": "off", "welcome": ["Hi {name}", "", "Have fun"],
                                "kick_blocks": ["5m", "1h"], "stats_server": "off", "tick_rate": 30})
    again = tomlkit.parse(tomlkit.dumps(d))
    first = again["server"][0]
    assert first["roles"]["vip"]["players"] == ["vip-one", "vip-two"]
    assert first["moderators"] == ["mod-one"]
    # The server's votes replace [defaults]' whole, so what applied before is kept beside the change.
    assert first["votes"] == {"kick": "members", "endgame": "off", "shuffle": "off"}
    assert first["welcome"] == ["Hi {name}", "Have fun"] and first["kick_blocks"] == ["5m", "1h"]
    assert again["stats_server"] == "off" and "stats_server" not in first
    assert first["tick_rate"] == 30
    ss.apply_changes(again, 49176, {"vips": []})
    assert "roles" not in again["server"][0]


def test_what_a_server_plays_is_one_thing():
    d = doc()
    ss.apply_changes(d, 49176, {"map": "Valhalla", "game": "Slayer"})
    first = d["server"][0]
    assert "playlist" not in first and first["map"] == "Valhalla"
    ss.apply_changes(d, 49176, {"campaign.mission": "a10", "campaign.difficulty": "heroic"})
    first = tomlkit.parse(tomlkit.dumps(d))["server"][0]
    assert "map" not in first and "game" not in first
    assert first["campaign"] == {"mission": "a10", "difficulty": "heroic"}
    assert ss.read_settings(d, 49176)["plays"] == "campaign"
    ss.apply_changes(d, 49176, {"campaign.mission": "3010"})
    assert d["server"][0]["campaign"]["mission"] == 3010  # a map ID is a number
    ss.apply_changes(d, 49176, {"playlist": "playlists/swat.playlist.json"})
    assert "campaign" not in d["server"][0] and d["server"][0]["playlist"] == "playlists/swat.playlist.json"
    ss.apply_changes(d, 49176, {"playlist": "content/Playlists/Forge Night (v2).json"})  # one Forge installed
    assert d["server"][0]["playlist"] == "content/Playlists/Forge Night (v2).json"
    with pytest.raises(ValueError, match="not several"):
        ss.apply_changes(doc(), 49176, {"playlist": "playlists/swat.playlist.json", "map": "Valhalla"})
    with pytest.raises(ValueError, match="mission first"):
        ss.apply_changes(doc(), 49176, {"campaign.difficulty": "legendary"})


@pytest.mark.parametrize("changes, words", [
    ({"rcon_password": "x"}, "cannot be changed"),
    ({"rcon_port": 1}, "cannot be changed"),
    ({"port": 1}, "cannot be changed"),
    ({"bind_address": "0.0.0.0"}, "cannot be changed"),
    ({"master": {}}, "cannot be changed"),
    ({"upnp": True}, "cannot be changed"),
    ({"no_such_setting": 1}, "cannot be changed"),
    ({"max_players": 0}, "from 1 to 128"),
    ({"max_players": 129}, "from 1 to 128"),
    ({"max_players": True}, "whole number"),
    ({"max_players": "16"}, "whole number"),
    ({"max_ping": 5000}, "from 0 to 1000"),
    ({"friendly_fire": "yes"}, "on or off"),
    ({"team_changing": "random"}, "must be one of"),
    ({"tick_rate": 45}, "must be one of"),
    ({"name": ""}, "can't be empty"),
    ({"name": "x" * 49}, "up to 48"),
    ({"name": None}, "needs a value"),
    ({"name": "bad\nname"}, "one line"),
    ({"password": "p" * 129}, "128 bytes"),
    ({"admins": ["  "]}, "can't be a player ID"),
    ({"admins": ["bad\tid"]}, "can't be a player ID"),
    ({"admins": ["x" * 129]}, "can't be a player ID"),
    ({"admins": "one-id"}, "list"),
    ({"welcome": ["1", "2", "3", "4", "5", "6"]}, "up to 5"),
    ({"welcome": ["w" * 301]}, "up to 300 characters"),
    ({"moderator_ban_minutes": 0}, "from 1 to 10080"),
    ({"moderator_ban_minutes": 10081}, "from 1 to 10080"),
    ({"share_rate": 10001}, "from 0 to 10000"),
    ({"share_rate": -1}, "from 0 to 10000"),
    ({"forge_autosave_keep": 21}, "from 0 to 20"),
    ({"forge_autosave_minutes": 1441}, "from 0 to 1440"),
    ({"film_keep": 0}, "from 1 to"),
    ({"film_max_mb": 0}, "from 1 to"),
    ({"post_game_seconds": 301}, "from 0 to 300"),
    ({"start_countdown_seconds": 61}, "from 0 to 60"),
    ({"coop_network": "lockstep60"}, "must be one of"),
    ({"halo1_objects": "on"}, "on or off"),
    ({"kick_blocks": ["30m", "10m"]}, "as long as the one before"),
    ({"kick_blocks": ["2w"]}, "a week at most"),
    ({"kick_blocks": ["10x"]}, "not a length"),
    ({"playlist": "../dedicated.toml"}, "playlists"),
    ({"stats_server": "ftp://example"}, "address"),
    ({"min_players": 20}, "can't be more than Max players"),
    ({"campaign.mission": "a10", "max_players": 20}, "at most 16"),
    ({"welcome": ["Hi"], "text_chat": False}, "needs text chat"),
    ({}, "Nothing to save"),
])
def test_bad_keys_and_values_are_refused_in_plain_words(changes, words):
    with pytest.raises(ValueError, match=words):
        ss.apply_changes(doc(), 49176, changes)


# --- the operation, as the panel calls it -----------------------------------------------------------------------

def hub_with_server(tmp_path, port):
    from oni_rcon.config import Server
    root = tmp_path / "server"
    content = root / "content"
    content.mkdir(parents=True)
    (root / "dedicated.toml").write_text(CONFIG, encoding="utf-8")
    h = Hub(Settings(data_dir=tmp_path / "state"), Vault(tmp_path / "state"))
    h.clients = [object()]
    h.servers = [Server(port=port, content_dir=str(content))]
    return h, root


def test_operation_reads_writes_and_backs_up(tmp_path):
    async def scenario():
        h, root = hub_with_server(tmp_path, 49179)
        read = await dispatch(h, "server_settings", {"index": 0})
        assert value(read, "name")["value"] == "Second"
        assert "second-rcon-secret" not in json.dumps(read) and "join-secret-default" not in json.dumps(read)
        result = await dispatch(h, "server_setting", {"index": 0, "changes": {"friendly_fire": True, "network_model": "classic"}})
        assert result == {"applies": ["next_game", "server_restart"], "warnings": []}
        assert (root / "dedicated.toml.bak").read_text(encoding="utf-8") == CONFIG
        written = tomlkit.parse((root / "dedicated.toml").read_text(encoding="utf-8"))
        assert second(written)["network_model"] == "classic"
        # The one-setting form the rest of the console uses still works.
        await dispatch(h, "server_setting", {"index": 0, "key": "admin_add", "value": "new-admin"})
        assert second(tomlkit.parse((root / "dedicated.toml").read_text()))["admins"] == ["new-admin"]
        with pytest.raises(ValueError, match="already present"):
            await dispatch(h, "server_setting", {"index": 0, "key": "admin_add", "value": "new-admin"})
        await dispatch(h, "server_setting", {"index": 0, "key": "max_players", "value": 10})
        # A refused change writes nothing, not even a backup.
        before = (root / "dedicated.toml").read_text()
        (root / "dedicated.toml.bak").unlink()
        with pytest.raises(ValueError):
            await dispatch(h, "server_setting", {"index": 0, "changes": {"friendly_fire": True, "rcon_password": "x"}})
        assert (root / "dedicated.toml").read_text() == before and not (root / "dedicated.toml.bak").exists()
        with pytest.raises(ValueError, match="No such station"):
            await dispatch(h, "server_settings", {"index": 3})
    asyncio.run(scenario())


def test_operation_refuses_a_server_it_cannot_identify(tmp_path):
    async def scenario():
        h, _ = hub_with_server(tmp_path, 40000)
        with pytest.raises(ValueError, match="exactly one server"):
            await dispatch(h, "server_settings", {"index": 0})
        with pytest.raises(ValueError, match="exactly one server"):
            await dispatch(h, "server_setting", {"index": 0, "changes": {"hidden": True}})
    asyncio.run(scenario())


# --- the limits the game devs confirmed (v0.10.9) ---------------------------------------------------------------

HEX_ID = "0123456789abcdef" * 4


@pytest.mark.parametrize("changes", [
    {"moderator_ban_minutes": 1}, {"moderator_ban_minutes": 10080},
    {"share_rate": 0}, {"share_rate": 10000},
    {"forge_autosave_keep": 20}, {"forge_autosave_minutes": 0},
    {"film_keep": 1}, {"film_max_mb": 1},
    {"post_game_seconds": 0}, {"start_countdown_seconds": 0},
    {"welcome": ["w" * 300]},
    {"welcome": ["1", "", "2", "3", "  ", "4", "5"]},
    {"coop_network": "lockstep"}, {"coop_network": "lockstep30"}, {"coop_network": "host"},
    {"max_players": 128}, {"max_players": 1, "min_players": 1},
])
def test_the_edges_of_each_limit_are_accepted(changes):
    d = doc()
    ss.apply_changes(d, 49176, changes)
    tomlkit.parse(tomlkit.dumps(d))


def test_limits_and_defaults_match_the_game():
    e = ss.BY_KEY
    assert (e["share_rate"]["default"], e["share_rate"]["applies"]) == (20, ss.LIVE)
    assert (e["moderator_ban_minutes"]["min"], e["moderator_ban_minutes"]["max"]) == (1, 10080)
    assert (e["film_keep"]["default"], e["film_keep"]["applies"]) == (25, ss.NEXT_GAME)
    assert (e["film_max_mb"]["default"], e["film_max_mb"]["applies"]) == (2048, ss.NEXT_GAME)
    assert (e["post_game_seconds"]["default"], e["start_countdown_seconds"]["default"]) == (15, 3)
    assert e["post_game_seconds"]["applies"] == e["start_countdown_seconds"]["applies"] == ss.LIVE
    assert e["coop_network"]["applies"] == ss.SERVER_RESTART
    assert [c[0] for c in e["coop_network"]["choices"]] == ["lockstep", "lockstep30", "host"]
    for key in ("odst_objects", "reach_objects", "halo1_objects", "halo2_objects", "halo3_objects", "forge_primitives", "forge_mods"):
        assert e[key]["applies"] == ss.NEXT_GAME
    assert e["halo1_objects"]["default"] is False and e["halo1_objects"]["advanced"]
    assert (e["max_players"]["min"], e["max_players"]["max"], e["max_players"]["default"]) == (1, 128, 16)


def test_more_than_five_welcome_lines_are_refused_and_empty_ones_dropped():
    d = doc()
    ss.apply_changes(d, 49176, {"welcome": ["one", "", "two"]})
    assert d["server"][0]["welcome"] == ["one", "two"]
    with pytest.raises(ValueError, match="up to 5"):
        ss.apply_changes(doc(), 49176, {"welcome": ["1", "2", "3", "4", "5", "6", ""]})


def test_halo1_and_halo2_objects_are_written_for_the_server():
    d = doc()
    ss.apply_changes(d, 49176, {"halo1_objects": True, "halo2_objects": False})
    first = tomlkit.parse(tomlkit.dumps(d))["server"][0]
    assert (first["halo1_objects"], first["halo2_objects"]) == (True, False)


def test_player_ids_are_trimmed_and_odd_ones_kept_with_a_warning():
    assert ss.looks_like_id(HEX_ID) and ss.looks_like_id(HEX_ID.upper()) and ss.looks_like_id(f"  {HEX_ID} ")
    assert not ss.looks_like_id(HEX_ID[:-1]) and not ss.looks_like_id("Player One") and not ss.looks_like_id("#3")
    d = doc()
    changes = {"admins": [f"  {HEX_ID}  ", "Player One"], "vips": ["#3"]}
    ss.apply_changes(d, 49176, changes)
    first = d["server"][0]
    assert first["admins"] == [HEX_ID, "Player One"] and first["roles"]["vip"]["players"] == ["#3"]
    warnings = ss.id_warnings(changes)
    assert len(warnings) == 2 and "Player One" in warnings[0] and "will never match" in warnings[0]
    assert ss.id_warnings({"admins": [HEX_ID], "moderators": [HEX_ID.upper()]}) == []


def test_operation_saves_odd_ids_and_says_so(tmp_path):
    async def scenario():
        h, root = hub_with_server(tmp_path, 49176)
        result = await dispatch(h, "server_setting", {"index": 0, "changes": {"moderators": [HEX_ID, "nickname"]}})
        assert result["applies"] == ["live"] and len(result["warnings"]) == 1 and "nickname" in result["warnings"][0]
        assert tomlkit.parse((root / "dedicated.toml").read_text())["server"][0]["moderators"] == [HEX_ID, "nickname"]
        # The console's one-admin form still refuses a slot number, as before.
        with pytest.raises(ValueError, match="Use a player ID"):
            await dispatch(h, "server_setting", {"index": 0, "key": "admin_add", "value": "#3"})
    asyncio.run(scenario())
