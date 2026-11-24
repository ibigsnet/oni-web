"""The server settings a customer may change from the panel: one schema, its checks and its writes.

The schema follows Project Reclaimer 0.10.9's own `dedicated.toml` reader (docs/servers.md, "dedicated.toml" and
"Changing settings while servers run"; the parser is crates/game/project-reclaimer/src/server/config.rs). Only the keys
listed here can be read or written. Anything that could cut the panel off from the server (the RCON password and port,
the game port, addresses, folders, [master], upnp) is not in it and is refused by name as well.

Where a value lives:
- "server": only in the server's own [[server]] entry.
- "defaults": the [[server]] entry over [defaults] over the game's default. Writes go into the server's entry
  (an override); clearing one (value null) removes the override so the [defaults] value applies again.
- "top": the [[server]] entry over the file's top-level value (host_name).
- "additive": a per-server list that adds to the file's top-level list of the same name (admins, moderators, ...).
- "global": a top-level key shared by every server in the file (stats_server).
"""
from __future__ import annotations

import copy
import re

import tomlkit

from oni_rcon import install

LIVE, NEXT_GAME, SERVER_RESTART, SUPERVISOR_RESTART = "live", "next_game", "server_restart", "supervisor_restart"

GROUPS = [
    ("general", "General"),
    ("admins", "Admins & roles"),
    ("gameplay", "Gameplay"),
    ("campaign", "Campaign & ODST"),
    ("forge", "Modding & Forge"),
    ("votes", "Votes & moderation"),
    ("anticheat", "Anti-cheat, films & stats"),
    ("network", "Network & performance"),
]

# Never readable or writable here, whatever a request says: the panel's own connection, the machine's addresses and
# folders, and the supervisor's networking. (The top-level `game`, the game's folder, is never written either: the
# schema's `game` is a [[server]]'s game type and only ever goes into that server's entry.)
FORBIDDEN = frozenset({
    "rcon_password", "rcon_port", "rcon", "port", "enabled", "bind_address", "public_address", "data",
    "content", "bans", "master", "upnp", "auto_reload", "auto_update", "metrics_http", "film_receivers",
    "film_upload_kbps", "social_key", "stats_copies", "vpn", "workshop", "failover",
})

MAX_PLAYERS = 128          # config.rs MAX_PLAYERS
COOP_MAX_PLAYERS = 16      # campaign.rs MAX_PLAYERS (co-op campaign, ODST, Reach)
MAX_NAME = 48              # config.rs MAX_NAME
MAX_PASSWORD = 128         # config.rs MAX_PASSWORD (bytes)
MAX_PING = 1000            # config.rs MAX_PING_LIMIT
MAX_LOAD_TIMEOUT = 600     # config.rs MAX_LOAD_TIMEOUT
MAX_THREADS = 64           # n26_channel_threads / node_threads MAX_THREADS
MAX_AUTOSAVES = 20         # state.rs MAX_AUTOSAVES
MAX_WELCOME_LINES = 5      # config.rs MAX_WELCOME_LINES
MAX_KICK_BLOCKS = 8        # config.rs MAX_KICK_BLOCKS
MAX_KICK_BLOCK = 7 * 24 * 3600
MAX_IDS = 256              # a panel limit: a list longer than this is a file job
MAX_TEXT = 200             # a panel limit for a map, mission or mod name
MAX_WELCOME_LINE = 300     # game devs: chat cuts a line at 360; welcome lines are kept to 300
MAX_BAN_MINUTES = 10080    # roles.rs: moderator_ban_minutes is 1 to seven days; else the game refuses the file
MAX_SHARE_RATE = 10000     # a page range; the game takes any u32 (0: no limit, unset: 20 Mbit/s)
PLAYER_ID = re.compile(r"[0-9a-fA-F]{64}")  # a node's network key or a community ID (SHA-256 hex), profile.rs

PLAYS_KEYS = ("playlist", "playlists", "variant", "map", "game", "base_map", "campaign", "odst", "reach")
VOTE_KINDS = ("kick", "endround", "endgame", "shuffle")
VOTE_WHO = [("everyone", "Everyone"), ("members", "Members only"), ("off", "Nobody (off)")]


def _s(key, group, label, description, typ, applies, scope="defaults", default=None, advanced=False, **extra):
    entry = {"key": key, "group": group, "label": label, "description": description, "type": typ,
             "applies": applies, "scope": scope, "default": default, "advanced": advanced}
    entry.update(extra)
    return entry


SCHEMA = [
    # --- General -------------------------------------------------------------------------------------------------
    _s("name", "general", "Server name",
       "The name players see in the server browser and at the top of the lobby. Up to 48 characters.",
       "string", LIVE, scope="server", max_len=MAX_NAME, min_len=1),
    _s("host_name", "general", "Host name",
       "Shown under the server's name in the browser, such as your community's name. Empty uses the name set for "
       "every server in the file.", "string", LIVE, scope="top", default="", max_len=MAX_NAME),
    _s("password", "general", "Join password",
       "Players must type it to join. Leave it empty to let anyone join. It is never shown here once saved.",
       "secret", LIVE, default="", max_len=MAX_PASSWORD),
    _s("max_players", "general", "Max players",
       "How many players the server takes at once. Co-op campaign and ODST servers take at most 16.",
       "int", LIVE, default=16, min=1, max=MAX_PLAYERS),
    _s("min_players", "general", "Players needed to start",
       "The lobby waits for this many players before a game starts. It can't be more than Max players.",
       "int", LIVE, default=1, min=1, max=MAX_PLAYERS),
    _s("max_ping", "general", "Ping limit",
       "Players whose ping is higher than this when they join are turned away, in milliseconds. 0 lets everyone in.",
       "int", LIVE, default=0, min=0, max=MAX_PING, unit="ms"),
    _s("hidden", "general", "Hidden from the server browser",
       "On keeps the server off the public server lists; players can still join by its address.",
       "bool", LIVE, default=False),
    _s("welcome", "general", "Welcome message",
       "Lines every player reads in chat as they join, up to 5. {name} becomes the player's name and {server} the "
       "server's. Needs text chat on.", "lines", LIVE, default=[], max_items=MAX_WELCOME_LINES, max_len=MAX_WELCOME_LINE),
    _s("text_chat", "general", "Text chat",
       "Players may type to each other in the lobby and in games.", "bool", LIVE, default=True),
    _s("voice", "general", "Voice chat",
       "Players may talk by voice in the lobby and in games.", "bool", LIVE, default=True),
    _s("voice_quality", "general", "Voice quality",
       "Standard sends voice at 16 kHz; High at 24 kHz, which sounds clearer and uses more bandwidth.",
       "enum", LIVE, default="standard", choices=[["standard", "Standard"], ["high", "High"]], advanced=True),
    _s("post_game_seconds", "general", "Post-game time",
       "Seconds the scoreboard stays up after a game before the lobby moves on.",
       "int", LIVE, default=15, min=0, max=300, unit="s", advanced=True),
    _s("start_countdown_seconds", "general", "Start countdown",
       "Seconds the lobby counts down once enough players are in.",
       "int", LIVE, default=3, min=0, max=60, unit="s", advanced=True),

    # --- Admins & roles ------------------------------------------------------------------------------------------
    _s("admins", "admins", "Admins",
       "Player IDs who hold the Admin role on this server: they can do everything and can't be vote-kicked. "
       "A player ID is the one a player's profile shows.", "ids", LIVE, scope="additive", default=[]),
    _s("moderators", "admins", "Moderators",
       "Player IDs who hold the Mod role: they can kick, give timed bans, mute, cancel votes and end rounds, and "
       "can't be vote-kicked.", "ids", LIVE, scope="additive", default=[]),
    _s("vips", "admins", "VIPs",
       "Player IDs who hold the VIP role: they can't be vote-kicked and go ahead in the join queue.",
       "ids", LIVE, scope="additive", default=[], path=("roles", "vip", "players")),
    _s("vote_kick_immune", "admins", "Can't be vote-kicked",
       "Player IDs no players' vote can kick, without giving them a role.", "ids", LIVE, scope="additive", default=[]),
    _s("moderator_ban_minutes", "admins", "Longest Mod ban",
       "The longest timed ban a Moderator may give, in minutes, up to 10,080 (seven days).", "int", LIVE,
       default=60, min=1, max=MAX_BAN_MINUTES,
       unit="min", advanced=True),

    # --- Gameplay -----------------------------------------------------------------------------------------------
    _s("playlist", "gameplay", "Playlist",
       "The playlist the server plays and votes from. Choosing one replaces a single map and game type or a "
       "campaign. Takes effect from the next vote.", "playlist", NEXT_GAME, scope="server", nullable=True,
       null_label="As the file's defaults say"),
    _s("map", "gameplay", "Map",
       "Play one map only, with the game type below, instead of a playlist. Use the map's name as the server lists "
       "it.", "string", NEXT_GAME, scope="server", nullable=True, max_len=MAX_TEXT, suggest="maps",
       null_label="Not set"),
    _s("game", "gameplay", "Game type",
       "The game type played on the map above, by its name (Slayer, Team Slayer, a saved game type's name).",
       "string", NEXT_GAME, scope="server", nullable=True, max_len=MAX_TEXT, suggest="modes", null_label="Not set"),
    _s("friendly_fire", "gameplay", "Friendly fire",
       "Off stops players from hurting their teammates and co-op partners. On follows each game type's own rules.",
       "bool", NEXT_GAME, default=True),
    _s("team_changing", "gameplay", "Team changing",
       "How far players may change teams on their own: only to a team with fewer players, as each game type "
       "says, or not at all.", "enum", NEXT_GAME, default="balanced",
       choices=[["balanced", "Only to a smaller team"], ["game_type", "As the game type says"], ["off", "Off"]]),
    _s("stock_map_defaults", "gameplay", "Bungie's extra respawn points",
       "On gives Halo 3's maps, played without a saved map, the extra respawn points Bungie's matchmaking "
       "variants add.", "bool", NEXT_GAME, default=True),
    _s("round_outlasts_leavers", "gameplay", "Keep playing when players leave",
       "On lets a round go on when a player leaving leaves too few to play. Off ends the round at once, as Halo 3 "
       "did.", "bool", NEXT_GAME, default=False),
    _s("camera", "gameplay", "Camera",
       "Which view players see their character from on foot.", "enum", NEXT_GAME, default="first_person",
       choices=[["first_person", "First person"], ["player_choice", "Player's choice"],
                ["third_person", "Third person"]], advanced=True),
    _s("force_model", "gameplay", "Everyone plays",
       "Make every player play the Spartan or the Elite in Halo 3's multiplayer games, keeping their own armour "
       "and colours. For map mods that replace one of them.", "enum", NEXT_GAME, default=None, nullable=True,
       null_label="Each player's own pick", choices=[["spartan", "Spartan"], ["elite", "Elite"]], advanced=True),
    _s("map_hud", "gameplay", "Map's own HUD",
       "On shows every player the map's own HUD, whatever character they play.", "bool", NEXT_GAME, default=False,
       advanced=True),

    # --- Campaign & ODST ----------------------------------------------------------------------------------------
    _s("campaign.mission", "campaign", "Co-op campaign mission",
       "Turn this into a co-op campaign server starting from this mission: its file name (a10, 010_jungle, sc100), "
       "its title or its map ID. Clear it to play multiplayer again. Switching restarts the server once it is "
       "empty.", "string", SERVER_RESTART, scope="server", nullable=True, max_len=MAX_TEXT,
       null_label="Not a campaign server", path=("campaign", "mission")),
    _s("campaign.difficulty", "campaign", "Campaign difficulty",
       "The difficulty the co-op campaign is played on.", "enum", SERVER_RESTART, scope="server", default="normal",
       choices=[["easy", "Easy"], ["normal", "Normal"], ["heroic", "Heroic"], ["legendary", "Legendary"]],
       path=("campaign", "difficulty"), needs="campaign.mission"),
    _s("campaign.campaign", "campaign", "Which campaign",
       "The campaign the mission is from: Halo 3, Halo 3: ODST, Halo: Combat Evolved, Halo 2, Halo: Reach, or a "
       "mod's campaign by its title. Left on automatic, the mission's own game is used.", "string", SERVER_RESTART,
       scope="server", nullable=True, max_len=MAX_TEXT, null_label="The mission's own campaign",
       choices=[["halo3", "Halo 3"], ["odst", "Halo 3: ODST"], ["halo1", "Halo: Combat Evolved"],
                ["halo2", "Halo 2"], ["reach", "Halo: Reach"]],
       path=("campaign", "campaign"), needs="campaign.mission"),
    _s("campaign_skip_vote_percent", "campaign", "Cinematic skip vote",
       "The share of co-op players who must press skip before a cutscene is skipped. 100 needs everyone.",
       "int", LIVE, default=33, min=1, max=100, unit="%"),
    _s("coop_network", "campaign", "Co-op networking",
       "How co-op missions are played over the network. Lockstep is Halo 3's own at 60 ticks; lockstep at 30 ticks "
       "is the Xbox 360's pace; host-authoritative is experimental. A change takes effect only when the server next "
       "starts.", "enum", SERVER_RESTART, default="lockstep",
       choices=[["lockstep", "Lockstep (60 ticks)"], ["lockstep30", "Lockstep (30 ticks, as the Xbox 360)"],
                ["host", "Host-authoritative (experimental)"]], advanced=True),

    # --- Modding & Forge ----------------------------------------------------------------------------------------
    _s("share_mods", "forge", "Share mods with players",
       "Players who lack the server's mods download them from the server as they join.", "bool", LIVE,
       default=False),
    _s("share_rate", "forge", "Mod download speed",
       "The most all mod downloads from this server send together, in megabits a second; 0 means no limit. It "
       "only matters with mod sharing on.", "int", LIVE, default=20, min=0, max=MAX_SHARE_RATE, unit="Mbit/s",
       advanced=True),
    _s("forge_edit", "forge", "Who edits in Forge",
       "As the game type says follows each Forge game type's own editing rule, as Halo 3 did. By role lets only "
       "players whose role allows Forge editing build.", "enum", LIVE, default="game_type",
       choices=[["game_type", "As the game type says"], ["roles", "By role"]]),
    _s("forge_autosave_minutes", "forge", "Forge autosave",
       "In a Forge game the server saves the map's state every this many minutes. 0 turns autosave off.",
       "int", LIVE, default=0, min=0, max=1440, unit="min"),
    _s("forge_autosave_keep", "forge", "Autosaves kept",
       "How many earlier autosaves the server keeps, up to 20.", "int", LIVE, default=3, min=0,
       max=MAX_AUTOSAVES, advanced=True),
    _s("restore_state", "forge", "Resume from the last save",
       "On starts the server from the state it saved last, such as a Forge map in progress.", "bool", LIVE,
       default=False, advanced=True),
    _s("odst_objects", "forge", "ODST objects in Forge",
       "Adds Halo 3: ODST's objects to Forge on Halo 3's maps. Players then need ODST to join.", "bool", NEXT_GAME,
       default=False),
    _s("reach_objects", "forge", "Reach objects in Forge",
       "Adds Halo: Reach's objects to Forge on Halo 3's and ODST's maps. Players then need Reach to join.", "bool",
       NEXT_GAME, default=False),
    _s("halo1_objects", "forge", "Halo: CE objects in Forge",
       "Adds Halo: Combat Evolved's objects to Forge. Players then need Halo: CE to join; if the server's game "
       "folder lacks it, Forge simply offers none of its objects.", "bool", NEXT_GAME, default=False, advanced=True),
    _s("halo2_objects", "forge", "Halo 2 objects in Forge",
       "Adds Halo 2's objects to Forge. Players then need Halo 2 to join; if the server's game folder lacks it, "
       "Forge simply offers none of its objects.", "bool", NEXT_GAME, default=False, advanced=True),
    _s("halo3_objects", "forge", "Halo 3 objects in Forge",
       "Halo 3's objects in Forge on ODST's maps. Automatic follows what each map offers.", "bool", NEXT_GAME,
       default=None, nullable=True, null_label="Automatic (as each map offers)", advanced=True),
    _s("forge_primitives", "forge", "Project Reclaimer primitives",
       "Project Reclaimer's building blocks in Forge. Automatic gives them on every map but Halo: CE's.", "bool",
       NEXT_GAME, default=None, nullable=True, null_label="Automatic (as each map offers)", advanced=True),
    _s("forge_mods", "forge", "Mods in the Forge palette",
       "Mods whose objects Forge offers, by title, package folder or workshop:<item>. Players download them as they "
       "join.", "list", NEXT_GAME, default=[], max_items=32, max_len=MAX_TEXT, advanced=True),

    # --- Votes & moderation -------------------------------------------------------------------------------------
    _s("votes.enabled", "votes", "Players' votes",
       "Players may call votes to kick a player, end the round or the game, or shuffle the teams.", "bool", LIVE,
       default=True, path=("votes", "enabled")),
] + [
    _s(f"votes.{kind}", "votes", label, description, "enum", LIVE, default="everyone", choices=VOTE_WHO,
       path=("votes", kind), allow_other=True)
    for kind, label, description in (
        ("kick", "Who may call a kick vote", "Who may start a vote to kick a player. Members are the players the "
                                             "file lists as members, or who played long enough."),
        ("endround", "Who may call an end-round vote", "Who may start a vote to end the round."),
        ("endgame", "Who may call an end-game vote", "Who may start a vote to end the game."),
        ("shuffle", "Who may call a shuffle vote", "Who may start a vote to shuffle the teams."),
    )
] + [
    _s("kick_blocks", "votes", "Kick ladder",
       "How long each kick keeps a player out, in rising steps such as 10m, 30m, 2h or 1d (a week at most, 8 steps "
       "at most). An empty list lets kicked players straight back.", "durations", LIVE,
       default=["10m", "30m", "60m", "120m"]),
    _s("block_vpn", "votes", "Block VPNs",
       "Turns away players who join through a known VPN or proxy, unless they are allowed.", "bool", LIVE,
       default=True),
    _s("joins", "votes", "New players",
       "Whether new players can join. Closed keeps every new player out; players already in stay. With the queue, "
       "players a full server turns away wait in line.", "enum", LIVE, default="open",
       choices=[["open", "Open"], ["queue", "Open, with a join queue"], ["closed", "Closed"]]),
    _s("queue_max", "votes", "Join queue length",
       "How many players may wait in the join queue, up to 7.", "int", LIVE, default=7, min=0, max=7,
       advanced=True),
    _s("vip_queue_max", "votes", "VIP queue length",
       "How many VIPs may wait in their own line, up to 3.", "int", LIVE, default=3, min=0, max=3, advanced=True),
    _s("vip_priority", "votes", "VIP share of free places",
       "The share of places that open which goes to the VIP line first.", "int", LIVE, default=75, min=0, max=100,
       unit="%", advanced=True),

    # --- Anti-cheat, films & stats ------------------------------------------------------------------------------
    _s("anti_cheat", "anticheat", "Anti-cheat",
       "Enforce refuses what a player's game should never send and kicks the sender. Detect refuses and reports it "
       "without kicking. Off checks nothing.", "enum", NEXT_GAME, default="enforce",
       choices=[["enforce", "Enforce (refuse and kick)"], ["detect", "Detect (refuse and report)"], ["off", "Off"]]),
    _s("anti_cheat_hits", "anticheat", "Hit check",
       "The anti-cheat's check of hits on its own. Detect reports a hit it refuses and kicks nobody. Always off when "
       "the anti-cheat is off.", "enum", NEXT_GAME, default=None, nullable=True, null_label="Detect (the default)",
       choices=[["enforce", "Enforce (refuse and kick)"], ["detect", "Detect (refuse and report)"], ["off", "Off"]],
       advanced=True),
    _s("film_recording", "anticheat", "Record films",
       "The server records every game as a film any player's Theater can play, kept in the server's folder.",
       "bool", NEXT_GAME, default=False),
    _s("film_keep", "anticheat", "Films kept",
       "How many films the server keeps; the oldest are deleted first.", "int", NEXT_GAME, default=25, min=1,
       max=10000),
    _s("film_max_mb", "anticheat", "Film space",
       "How much disk the films may take together, in MB; the oldest are deleted first.", "int", NEXT_GAME,
       default=2048, min=1, max=1048576, unit="MB", advanced=True),
    _s("stats_server", "anticheat", "Stats service",
       "Where finished games are reported, for ranks and service records. Empty uses the project's public stats "
       "service; off reports nowhere. This is shared by every server in this file.", "stats", NEXT_GAME,
       scope="global", default="", advanced=True),

    # --- Network & performance ----------------------------------------------------------------------------------
    _s("network_model", "network", "Networking",
       "Enhanced sends each player what matters most to them and smooths corrections; Classic is Halo 3's own. "
       "Every player uses the server's choice.", "enum", SERVER_RESTART, default="enhanced",
       choices=[["enhanced", "Enhanced"], ["classic", "Classic (Halo 3's)"]]),
    _s("tick_rate", "network", "Tick rate",
       "How many times a second multiplayer games update: 60, or 30 as on the Xbox 360.", "enum", NEXT_GAME,
       default=60, choices=[[60, "60 ticks"], [30, "30 ticks"]], advanced=True),
    _s("load_timeout", "network", "Slow-loading players",
       "Seconds a co-op player's slow map load may hold the others up before they are asked to join again. 0 waits "
       "as long as it takes.", "int", NEXT_GAME, default=30, min=0, max=MAX_LOAD_TIMEOUT, unit="s", advanced=True),
    _s("encode_once", "network", "Encode updates once",
       "Encodes each object's update once a tick for every player who needs it. Automatic turns it on for servers "
       "of more than 32 players.", "bool", SERVER_RESTART, default=None, nullable=True,
       null_label="Automatic (by server size)", advanced=True),
    _s("channel_threads", "network", "Network threads",
       "Threads that send players their updates each frame. 0 picks by player count and the machine; 1 uses the "
       "game's own thread only. Automatic: 1 up to 32 players, 0 above.", "int", SERVER_RESTART, default=None,
       nullable=True, null_label="Automatic (by server size)", min=0, max=MAX_THREADS, advanced=True),
    _s("node_threads", "network", "Animation threads",
       "Threads that pose characters each tick. 0 picks by how many there are and the machine; 1 uses the game's "
       "own thread only.", "int", SERVER_RESTART, default=None, nullable=True, null_label="Game's own thread (1)",
       min=0, max=MAX_THREADS, advanced=True),
]

BY_KEY = {e["key"]: e for e in SCHEMA}


def public_schema():
    return {"groups": [{"id": g, "label": label} for g, label in GROUPS], "settings": SCHEMA}


# --- reading ---------------------------------------------------------------------------------------------------

def plain(v):
    """A tomlkit value as plain Python."""
    return v.unwrap() if hasattr(v, "unwrap") else v


def path_of(entry):
    return entry.get("path") or (entry["key"],)


def get_path(table, path):
    cur = table
    for part in path:
        if not hasattr(cur, "get"):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return plain(cur)


def server_entry(doc, port):
    matches = [s for s in doc.get("server", []) if s.get("rcon_port", s.get("port")) == port and s.get("enabled", True)]
    if len(matches) != 1:
        raise ValueError("Cannot identify exactly one server by its RCON port")
    return matches[0]


def vote_who(votes, kind):
    """The `who` of one vote kind in a `votes` value (bool or table); None when it doesn't say."""
    if isinstance(votes, dict):
        v = votes.get(kind)
        if isinstance(v, dict):
            v = v.get("who")
        return v if isinstance(v, str) else None
    return None


def votes_enabled(votes):
    if isinstance(votes, bool):
        return votes
    if isinstance(votes, dict):
        enabled = votes.get("enabled")
        return enabled if isinstance(enabled, bool) else None
    return None


def welcome_lines(v):
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) for x in v]
    return None


def raw_value(entry, table):
    """The setting's own value in one table (a [[server]] entry, [defaults] or the top level), or None."""
    key = entry["key"]
    if key.startswith("votes."):
        votes = plain(table.get("votes"))
        return votes_enabled(votes) if key == "votes.enabled" else vote_who(votes, key.split(".", 1)[1])
    value = get_path(table, path_of(entry))
    if key == "welcome":
        return welcome_lines(value)
    if key == "campaign.mission" and value is not None:
        return str(value)
    return value


def plays(selected, defaults):
    for k in ("odst", "reach", "campaign"):
        if k in selected:
            return k
    if "playlists" in selected:
        return "playlists"
    if "playlist" in selected:
        return "playlist"
    if "variant" in selected:
        return "variant"
    if "map" in selected or "game" in selected:
        return "one"
    return "defaults"


def read_settings(doc, port):
    """The schema with this server's values: its own, else the inherited one, else the game's default."""
    selected = server_entry(doc, port)
    defaults = doc.get("defaults", {})
    values = []
    for entry in SCHEMA:
        scope, key = entry["scope"], entry["key"]
        own = raw_value(entry, selected) if scope != "global" else None
        inherited, source = None, "default"
        if scope == "defaults":
            # A server's own `votes` replaces [defaults]' whole: a kind it leaves out is the game's default there.
            inherited = None if key.startswith("votes.") and "votes" in selected else raw_value(entry, defaults)
        elif scope in ("top", "additive", "global"):
            inherited = raw_value(entry, doc)
        if scope == "additive":
            row = {"key": key, "value": list(own or []), "inherited": list(inherited or []),
                   "source": "server" if own else "default"}
        else:
            if own is not None:
                value, source = own, "server"
            elif inherited is not None:
                value, source = inherited, {"defaults": "defaults", "top": "all_servers", "global": "all_servers"}.get(scope, "default")
            else:
                value = entry["default"]
            row = {"key": key, "value": value, "inherited": inherited if inherited is not None else entry["default"],
                   "source": source}
        if entry["type"] == "secret":
            row = {"key": key, "value": None, "inherited": None, "source": row["source"],
                   "is_set": bool(row["value"]), "inherited_set": bool(inherited)}
        values.append(row)
    return {**public_schema(), "values": values, "plays": plays(selected, defaults),
            "server_name": str(plain(selected.get("name", "")))}


# --- checking ----------------------------------------------------------------------------------------------------

def _printable(text):
    return not any(ord(c) < 32 or ord(c) == 127 for c in text)


def check_id(value):
    """A player ID as written: trimmed, one line of text. The game doesn't check them; at join it compares each, in
    any case, with the player's 64-hex network key or community ID, so anything else is kept but never matches
    (looks_like_id says which, and the page warns)."""
    if not isinstance(value, str):
        raise ValueError("A player ID is text")
    value = value.strip()
    if not value or len(value) > 128 or not _printable(value):
        raise ValueError(f"\"{value[:40]}\" can't be a player ID (copy it from the player's profile)")
    return value


def check_legacy_admin(value):
    """The console's one-admin form (ADMIN on F1/F4) as it always was: a slot such as #3 is refused."""
    value = check_id(value)
    if value.startswith("#") or re.search(r"\s", value):
        raise ValueError("Use a player ID")
    return value


def looks_like_id(value):
    return bool(PLAYER_ID.fullmatch(value.strip()))


def id_warnings(changes):
    """Plain-words warnings for player IDs that will never match anyone (written anyway, as the game takes them)."""
    out = []
    for key, value in changes.items():
        entry = BY_KEY.get(key)
        if entry and entry["type"] == "ids" and isinstance(value, list):
            out += [f"{entry['label']}: \"{v.strip()[:40]}\" doesn't look like a player ID and will never match anyone"
                    for v in value if isinstance(v, str) and v.strip() and not looks_like_id(v)]
    return out


def duration_seconds(text):
    m = re.fullmatch(r"(\d+)([smhdw])", text)
    if not m:
        return None
    seconds = int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]]
    return seconds if 1 <= seconds <= MAX_KICK_BLOCK else None


def check_value(entry, value):
    """`value` checked against its schema entry and made ready to write; None removes the server's own value."""
    label, typ = entry["label"], entry["type"]
    if value is None:
        if entry["scope"] in ("defaults", "top") or entry.get("nullable"):
            return None
        raise ValueError(f"{label} needs a value")
    if typ == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{label} is on or off")
        return value
    if typ == "int":
        lo, hi = entry["min"], entry["max"]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"{label} must be a whole number from {lo} to {hi}")
        return value
    if typ == "enum":
        choices = [c[0] for c in entry["choices"]]
        if value in choices and not isinstance(value, bool):
            return value
        raise ValueError(f"{label} must be one of: " + ", ".join(c[1] for c in entry["choices"]))
    if typ in ("string", "secret"):
        if not isinstance(value, str) or not _printable(value):
            raise ValueError(f"{label} must be one line of text")
        if typ == "secret":
            if len(value.encode()) > entry["max_len"]:
                raise ValueError(f"{label} is up to {entry['max_len']} bytes")
            return value
        value = value.strip()
        if len(value) > entry["max_len"]:
            raise ValueError(f"{label} is up to {entry['max_len']} characters")
        if len(value) < entry.get("min_len", 0):
            raise ValueError(f"{label} can't be empty")
        if not value and (entry.get("nullable") or entry["scope"] == "top"):
            return None
        return value
    if typ == "playlist":
        # A file in the server's playlists/ folder, or one Forge installed into its content folder.
        try:
            safe = install.safe_path(value) if isinstance(value, str) else ""
        except install.InstallError:
            safe = ""
        if not safe.startswith(("playlists/", "content/")) or not safe.endswith(".json"):
            raise ValueError(f"{label}: choose one of the server's playlists")
        return safe
    if typ == "ids":
        if not isinstance(value, list) or len(value) > MAX_IDS:
            raise ValueError(f"{label} is a list of up to {MAX_IDS} player IDs")
        out = []
        for v in value:
            v = check_id(v)
            if v not in out:
                out.append(v)
        return out
    if typ in ("lines", "list"):
        if not isinstance(value, list) or len(value) > 4 * entry["max_items"]:
            raise ValueError(f"{label} takes up to {entry['max_items']} lines")
        out = []
        for v in value:
            if not isinstance(v, str) or not _printable(v) or len(v.strip()) > entry["max_len"]:
                raise ValueError(f"{label}: each line is one line of up to {entry['max_len']} characters")
            if v.strip():
                out.append(v.strip())  # empty lines are dropped, as the game drops them
        if len(out) > entry["max_items"]:
            # The game would silently drop the extra ones, so they are refused here.
            raise ValueError(f"{label} takes up to {entry['max_items']} lines")
        return out
    if typ == "durations":
        if not isinstance(value, list) or len(value) > MAX_KICK_BLOCKS:
            raise ValueError(f"{label} has at most {MAX_KICK_BLOCKS} steps")
        out, last = [], 0
        for v in value:
            seconds = duration_seconds(v.strip()) if isinstance(v, str) else None
            if seconds is None:
                raise ValueError(f"{label}: \"{str(v)[:20]}\" is not a length such as 10m, 2h or 1d (a week at most)")
            if seconds < last:
                raise ValueError(f"{label}: each step must be as long as the one before or longer")
            last = seconds
            out.append(v.strip())
        return out
    if typ == "stats":
        if not isinstance(value, str):
            raise ValueError(f"{label} is an address")
        value = value.strip()
        if value in ("", "off") or re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(:\d{1,5})?/?", value):
            return value
        raise ValueError(f"{label}: leave it empty, type off, or give an address such as http://stats.example:8792")
    raise ValueError(f"{label} cannot be changed from the console")


# --- writing -----------------------------------------------------------------------------------------------------

def set_path(table, path, value):
    cur = table
    for part in path[:-1]:
        nxt = cur.get(part)
        if nxt is None or not hasattr(nxt, "get"):
            nxt = tomlkit.inline_table() if part == "campaign" else tomlkit.table()
            cur[part] = nxt
            nxt = cur[part]
        cur = nxt
    cur[path[-1]] = value


def del_path(table, path):
    cur = table
    for part in path[:-1]:
        cur = cur.get(part)
        if cur is None or not hasattr(cur, "get"):
            return
    cur.pop(path[-1], None)


def _votes_table(selected, defaults):
    """The server's `votes` as a table to edit, starting from what applies now so nothing else changes."""
    current = plain(selected.get("votes"))
    if current is None:
        current = plain(defaults.get("votes"))
    if isinstance(current, bool):
        current = {"enabled": current}
    table = tomlkit.inline_table()
    for k, v in copy.deepcopy(current or {}).items():
        table[k] = v
    selected["votes"] = table
    return selected["votes"]


def apply_changes(doc, port, changes):
    """Every change checked, then written into this server's [[server]] entry (or the top level for a global one).
    All or nothing: the first problem raises and `doc` must then be thrown away."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Nothing to save")
    for key in changes:
        if not isinstance(key, str) or key in FORBIDDEN or key.split(".")[0] in FORBIDDEN or key not in BY_KEY:
            raise ValueError("This setting cannot be changed from the console")
    selected = server_entry(doc, port)
    defaults = doc.get("defaults", {})
    checked = {key: check_value(BY_KEY[key], value) for key, value in changes.items()}

    # What the server plays: one of a playlist, one map and game type, or a campaign.
    family = {"playlist": "playlist", "map": "one", "game": "one", "campaign.mission": "campaign",
              "campaign.difficulty": "campaign", "campaign.campaign": "campaign"}
    chosen = {family[k] for k, v in checked.items() if k in family and v is not None}
    if len(chosen) > 1:
        raise ValueError("Choose a playlist, one map and game type, or a campaign: not several at once")
    if "campaign" in chosen and "campaign.mission" not in checked and "campaign" not in selected:
        raise ValueError("Choose the campaign's mission first")
    if chosen:
        keep = {"playlist": ("playlist",), "one": ("map", "game"), "campaign": ("campaign",)}[chosen.pop()]
        for k in PLAYS_KEYS:
            if k not in keep:
                selected.pop(k, None)
    if "map" in checked:
        selected.pop("base_map", None)  # a base map belongs to the map it was given with

    for key, value in checked.items():
        entry = BY_KEY[key]
        path = path_of(entry)
        if entry["scope"] == "global":
            if value is None:
                doc.pop(key, None)
            else:
                doc[key] = value
            continue
        if key.startswith("votes."):
            if value is None:
                continue
            if key == "votes.enabled" and not isinstance(plain(selected.get("votes", plain(defaults.get("votes")))), dict):
                selected["votes"] = value
                continue
            table = _votes_table(selected, defaults)
            kind = path[1]
            if kind != "enabled" and isinstance(table.get(kind), dict):
                table[kind]["who"] = value
            else:
                table[kind] = value
            continue
        if key == "campaign.mission":
            if value is None:
                selected.pop("campaign", None)
                continue
            value = int(value) if value.isdigit() else value
        if value is None or (entry["scope"] == "additive" and value == []):
            del_path(selected, path)
            if path[0] == "roles":
                roles = selected.get("roles")
                if roles is not None and not roles.get("vip"):
                    roles.pop("vip", None)
                if roles is not None and not roles:
                    selected.pop("roles", None)
            continue
        if key == "welcome":
            value = value[0] if len(value) == 1 else value
        if key in ("campaign.difficulty", "campaign.campaign") and "campaign" not in selected:
            raise ValueError("Choose the campaign's mission first")
        set_path(selected, path, value)

    # The whole entry, as the game will check it once the file is saved.
    def effective(key):
        entry = BY_KEY[key]
        own = raw_value(entry, selected)
        if own is not None:
            return own
        inherited = raw_value(entry, defaults if entry["scope"] == "defaults" else doc)
        return inherited if inherited is not None else entry["default"]

    max_players, min_players = effective("max_players"), effective("min_players")
    if isinstance(min_players, int) and isinstance(max_players, int) and min_players > max_players:
        raise ValueError(f"Players needed to start ({min_players}) can't be more than Max players ({max_players})")
    coop = any(k in selected for k in ("campaign", "odst", "reach"))
    if coop and isinstance(max_players, int) and max_players > COOP_MAX_PLAYERS:
        raise ValueError(f"A co-op campaign server takes at most {COOP_MAX_PLAYERS} players: lower Max players first")
    if effective("welcome") and effective("text_chat") is False:
        raise ValueError("A welcome message needs text chat on")
    return doc
