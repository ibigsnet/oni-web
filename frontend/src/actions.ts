// What the buttons and keys do: each one a small dialog flow, the same as the terminal console's, ending in a
// command on the selected station.
import { invoke } from "@web/bridge";
import { DIM, TEAM_COLOR, TEAMS, WHITE, AMBER, CYAN } from "./art/palette";
import { oni, type Station } from "./engine";
import { newPlaylist, type Playlist } from "./lib/content";
import { latestOf } from "./lib/forge";
import { CATALOG, DISCORD_STEPS, MAX_RULES, ROLES, STARTERS, TEMPLATE_HELP, block, describe, describeBlock, plan, sample, stepWords, validate, type Field, type Part } from "./lib/rules";
import { num, pick, redactText, targetOf, type Dict, type Line } from "./lib/util";

export const BAN_TIMES: [string, string][] = [["Permanent", ""], ["30 minutes", "30m"], ["2 hours", "2h"], ["12 hours", "12h"], ["1 day", "1d"], ["7 days", "7d"], ["2 weeks", "2w"], ["30 days", "30d"]];
export const MUTE_TIMES: [string, string][] = [["Until restart", ""], ["10 minutes", "10m"], ["1 hour", "1h"], ["12 hours", "12h"], ["1 day", "1d"]];
export const COMMANDS = ["status", "players", "say", "tell", "kick", "ban", "banip", "unban", "bans", "vpn", "vpnallow", "vpnrevoke", "mute", "unmute",
  "endround", "endgame", "maps", "modes", "map", "mode", "load", "nextmap", "servername", "password", "maxping", "teamcount", "shuffle", "team",
  "vote", "startvote", "passvote", "cancelvote", "admin", "admins", "pardon", "help"];

export const OPS: [string, string, string][] = [["load", "LOAD MAP+MODE", "primary"], ["map", "CHANGE MAP", ""], ["mode", "CHANGE MODE", ""],
  ["nextmap", "QUEUE NEXT", ""], ["endround", "END ROUND", "warning"], ["endgame", "END GAME", "error"], ["shuffle", "SHUFFLE TEAMS", ""],
  ["teamcount", "TEAM COUNT", ""], ["startvote", "CALL VOTE", ""], ["passvote", "PASS VOTE", "success"], ["cancelvote", "CANCEL VOTE", ""],
  ["broadcast", "BROADCAST", "primary"], ["servername", "RENAME", ""], ["password", "JOIN PASSWORD", ""], ["maxping", "PING LIMIT", ""], ["capacity", "PLAYER CAPACITY", ""],
  ["playlist", "SWITCH PLAYLIST", ""], ["playlists", "PLAYLISTS", ""], ["newplaylist", "NEW PLAYLIST", ""], ["newgametype", "NEW GAME TYPE", ""], ["reconnect", "RECONNECT", ""], ["restart", "RESTART SERVER", "error"], ["remove", "REMOVE SERVER", "warning"]];
export const OP_GROUPS: [string, string[]][] = [["MATCH", ["load", "map", "mode", "nextmap", "endround", "endgame"]],
  ["TEAMS & VOTES", ["shuffle", "teamcount", "startvote", "passvote", "cancelvote"]], ["SERVER", ["broadcast", "servername", "password", "maxping", "capacity", "playlist", "playlists", "newplaylist", "newgametype", "reconnect", "restart", "remove"]]];
export const BAN_OPS: [string, string, string][] = [["newban", "NEW BAN", "error"], ["unban", "UNBAN  u", "warning"], ["vpnallow", "VPN ALLOW  a", ""],
  ["vpnrevoke", "VPN REVOKE  r", ""], ["vpncheck", "CHECK IP", ""], ["adminadd", "ADD ADMIN  p", "primary"], ["adminremove", "REMOVE ADMIN  d", "warning"]];
export const PLAYER_OPS: [string, string, string][] = [["tell", "TELL", "t"], ["kick", "KICK", "k"], ["ban", "BAN", "b"], ["mute", "MUTE", "m"],
  ["team", "TEAM", "j"], ["vpnallow", "VPN OK", "v"], ["admin", "MAKE ADMIN", "p"], ["copy", "COPY ID", "y"]];

// What each control does, in plain words: shown when the mouse rests on it.
export const TIPS: Record<string, string> = {
  "op-load": "Pick a map and a mode and switch to them now. Ends the current game.",
  "op-map": "Switch to another map, keeping the current mode. Ends the current game.",
  "op-mode": "Switch to another mode, keeping the current map. Ends the current game.",
  "op-nextmap": "Choose what plays after this game, without interrupting it.",
  "op-endround": "End this round as if its time ran out.",
  "op-endgame": "End the whole game now, with the scores as they stand.",
  "op-shuffle": "Mix the players up across the teams.",
  "op-teamcount": "Spread the players over 2 to 8 teams.",
  "op-startvote": "Ask the players to vote: end the round, shuffle, kick someone, and more.",
  "op-passvote": "Pass the vote that's under way, whatever the count.",
  "op-cancelvote": "Call off the vote that's under way.",
  "op-broadcast": "Send a [Server] message to everyone in this or every server.  Ctrl+B",
  "op-servername": "Change the name in the server browser, until the server restarts.",
  "op-password": "Make players type a password to join (or remove it).",
  "op-maxping": "Stop players with a high ping from joining.",
  "op-capacity": "How many players the server takes at once. Saved in its dedicated.toml, so it holds after a restart and needs one.",
  "op-reconnect": "Drop this connection and sign in again.",
  "op-playlist": "Pick the playlist the server plays, from its playlists folder or installed from F6. Saved in its dedicated.toml, so it needs a restart.",
  "op-playlists": "The playlists in the server's playlists folder and installed from F6: make, edit and use them.",
  "op-newplaylist": "Make a playlist from the server's own maps and modes: three games at least, that players vote between. Saved in its playlists folder.",
  "op-newgametype": "Make a game type: a base mode with its score, teams, time, rounds, lives, respawn and shields. Saved in the server's content folder, Game Modes.",
  "op-restart": "Restart the server itself: everyone is dropped and it comes back in a minute or so. Needs restart_cmd in the config (setup adds it).",
  "op-remove": "Take this server out of your list (the config file). The server itself isn't touched.",
  "bl-newban": "Ban a player ID, an IP address or a whole range.",
  "bl-unban": "Lift the selected ban.  Key: u",
  "bl-vpnallow": "Let a player or address join through a VPN.  Key: a",
  "bl-vpnrevoke": "Take back the selected VPN allowance.  Key: r",
  "bl-vpncheck": "Check whether an address looks like a VPN.",
  "bl-adminadd": "Make a player an admin on every server sharing this ban list at once, or of this server only through its dedicated.toml.  Key: p",
  "bl-adminremove": "Take admin away from the selected one. Admins written in dedicated.toml are changed there.  Key: d",
  "pl-tell": "Send a private message only they can see.  Key: t",
  "pl-kick": "Remove them from the game. They can rejoin after a short wait.  Key: k",
  "pl-ban": "Keep them out, for a while or for good.  Key: b",
  "pl-mute": "Silence their text and voice chat (or lift it).  Key: m",
  "pl-team": "Move them to another team.  Key: j",
  "pl-vpnallow": "Let them join through a VPN.  Key: v",
  "pl-admin": "Make them an admin (or take it away): on every server sharing the ban list at once, or of this server only.  Key: p",
  "pl-copy": "Copy their player ID, for a ban list or a report.  Key: y",
  "add-server": "Add another server with the setup screen.",
  "super": "The Superintendent watches every server's feed and reacts: it welcomes a join, startles at a call for an admin, scowls at a cheat flag, approves a kick or ban, is impressed by a medal and unimpressed by a mute.",
  "fg-install": "Put the selected version on this server, every file checked against Forge's manifest.  Key: i",
  "fg-load": "Load the installed map or gametype now, once the server lists it. Ends the current game.  Key: l",
  "fg-ack": "Say you've seen that it was withdrawn: CONDITION goes back to GREEN. It stays flagged.  Key: a",
  "fg-key": "Load your own ReclaimerForge API key.  Key: k",
  "fg-more": "Fetch the next page of the catalog.  Key: n",
  "fg-publish": "Put your own map, gametype or playlist files on ReclaimerForge: pick them, fill in one form, and it's uploaded and published.  Key: p",
  "fg-update": "Publish new files as the next version of this listing of yours.  Key: u",
  "fg-edit": "Change this listing's title, description, tags or who sees it.  Key: e",
  "fg-notes": "Change the selected version's label or release notes.",
  "fg-withdraw": "Withdraw the selected version, or discard it if it's a draft. Servers that have it are told.  Key: x",
  "fg-delete": "Take the whole listing off ReclaimerForge, for good.",
};
export const TAB_TIPS: Record<string, string> = {
  assets: "Players on the selected server: click one for their file and actions.",
  intercepts: "Live chat, kills, joins and moderation from every server, and a box to talk back.",
  operations: "Change the map or mode, end rounds, run votes, and server settings.",
  blacklist: "Bans, players allowed to join through a VPN, and admins.",
  console: "Type server commands directly. For when a button doesn't cover it.",
  forge: "The ReclaimerForge catalog: community maps, gametypes and playlists for your servers.",
  health: "Crashes read from each server's log, the host's memory, and whether the workaround service runs.",
};

const opt = (v: string) => (v ? [v] : []);

export function entries(st: Station, what: string): [Line, string][] {
  return (st.data[what]?.entries ?? []).map((e: Dict) => [
    [[String(pick(e, "name") ?? "?"), WHITE], [`  ${e.kind ?? ""}`, DIM], [`  ${e.reference ?? ""}`, DIM]] as Line,
    String(pick(e, "reference", "name")),
  ]);
}

export async function broadcast() {
  const f = await oni.form("BROADCAST", [["text", "Message ([Server] line in chat)", ""],
    ["scope", "Transmit to", [["Every station", "all"], [`${oni.cur.label} only`, "one"]]]], { verb: "TRANSMIT", required: ["text"] });
  if (!f) return;
  const targets = (f.scope === "all" ? oni.stations : [oni.cur]).filter((s) => s.online);
  if (targets.length) oni.fanout(targets, "say", [f.text]);
  if (targets.length < 2)
    oni.notify(targets.length ? `Transmitted to ${targets[0].label}.` : "Not connected: nothing was sent.", "BROADCAST", targets.length ? "information" : "warning");
}

export async function player(what: string, p: Dict | undefined) {
  const st = oni.cur;
  if (!p) return oni.notify("No asset selected.", "", "warning");
  const t = targetOf(p), name = String(pick(p, "name") ?? "?");
  if (what === "copy") return oni.copy(t, "PLAYER ID COPIED");
  if (what === "tell") {
    const f = await oni.form(`TELL · ${name}`, [["text", "Message (only they see it)", ""]], { verb: "SEND", required: ["text"] });
    if (f) oni.send(st, "tell", [t, f.text]);
  } else if (what === "kick") {
    const f = await oni.form(`KICK · ${name}`, [["reason", "Reason (they read it)", ""]], { verb: "KICK", danger: true, note: "Repeat kicks block rejoining for 10, 30, 60, then 120 minutes." });
    if (f) oni.send(st, "kick", [t, ...opt(f.reason)], ["players", "status"]);
  } else if (what === "ban") {
    const f = await oni.form(`BAN · ${name}`, [["time", "Duration", BAN_TIMES], ["reason", "Reason", ""]], { verb: "BAN", danger: true,
      note: "Bans their player ID, address and device together, on every server sharing this ban list." });
    if (f) oni.send(st, "ban", [t, ...opt(f.time), ...opt(f.reason)], ["players", "status", "bans"]);
  } else if (what === "mute") {
    if (p.muted) return oni.send(st, "unmute", [t], ["players"]);
    const f = await oni.form(`MUTE · ${name}`, [["time", "Duration", MUTE_TIMES], ["reason", "Reason", ""]], { verb: "MUTE", note: "Silences their text and voice chat. Rejoining keeps it." });
    if (f) oni.send(st, "mute", [t, ...opt(f.time), ...opt(f.reason)], ["players"]);
  } else if (what === "team") {
    const team = await oni.pick(`MOVE ${name} TO`, TEAMS.map((c) => [[[c.toUpperCase(), TEAM_COLOR[c]]], c]));
    if (team) oni.send(st, "team", [t, team], ["players"]);
  } else if (what === "admin") {
    const sub = p.admin ? "remove" : "add";
    if (sub === "add") return makeAdmin(st, t, name);
    if (await oni.confirm(`${p.admin ? "REMOVE ADMIN" : "MAKE ADMIN"} · ${name}`, ADMIN_NOTE[sub], p.admin ? "REMOVE" : "MAKE ADMIN", !!p.admin))
      oni.send(st, "admin", [sub, t], ["players", "admins"]);
  } else if (what === "vpnallow") {
    const f = await oni.form(`VPN ALLOW · ${name}`, [["note", "Note (why)", ""]], { verb: "ALLOW", note: "Lets them join through a VPN on every server sharing the ban list." });
    if (f) oni.send(st, "vpnallow", [t, ...opt(f.note)], ["vpn"]);
  }
}

/** An admin of every server sharing the ban list at once, or (with the server folder in reach) of this one only through
 *  its dedicated.toml. `asked`: they've already said yes once. */
async function makeAdmin(st: Station, target: string, name: string, asked = false): Promise<void> {
  if (!st.contentDir) {
    if (asked || (await oni.confirm(`MAKE ADMIN · ${name}`, ADMIN_NOTE.add, "MAKE ADMIN", false))) oni.send(st, "admin", ["add", target], ["admins", "players"]);
    return;
  }
  const scope = await oni.pick(`MAKE ${name} AN ADMIN OF`, [["EVERY SERVER sharing this ban list · at once", "all"],
    ["THIS SERVER ONLY · dedicated.toml, after a restart", "one"]]);
  if (scope === "all") oni.send(st, "admin", ["add", target], ["admins", "players"]);
  else if (scope === "one") return saveSetting(st, "admin_add", target, "ADMIN", `${name} as an admin of this server`);
}

/** A dedicated.toml setting saved, then the restart it needs offered. */
async function saveSetting(st: Station, key: string, value: unknown, what: string, saved: string): Promise<void> {
  try {
    await invoke("server_setting", { index: st.index, key, value });
  } catch (e) {
    return oni.notify(String(e), `${what} NOT SAVED · ${st.label}`, "error", 15);
  }
  if (st.canRestart) return op("restart");
  return oni.notify(`Saved: ${saved} once the server restarts.`, `${what} · ${st.label}`);
}

const BASE_MODES: [string, string][] = [["Slayer", "Slayer"], ["Capture the Flag", "CaptureTheFlag"], ["Oddball", "Oddball"], ["King of the Hill", "KingOfTheHill"],
  ["VIP", "Vip"], ["Juggernaut", "Juggernaut"], ["Territories", "Territories"], ["Assault", "Assault"], ["Infection", "Infection"], ["Escalation Slayer", "EscalationSlayer"]];
const GAME_OPTIONS: [string, string][] = [["rounds.minutes", "minutes"], ["rounds.limit", "rounds"], ["respawn.lives", "lives"], ["respawn.seconds", "respawn"]];

/** NEW GAME TYPE: a base mode and the options most changed; any left blank stay the mode's own. */
async function newGameType(st: Station): Promise<void> {
  const f = await oni.form("NEW GAME TYPE", [["name", "Name (up to 48 characters)", ""], ["mode", "Built on", BASE_MODES],
    ["teams", "Teams", [["The mode's own", ""], ["Teams", "1"], ["Free for all", "0"]]], ["score", "Score to win (0 = no limit)", ""],
    ["minutes", "Time limit in minutes, 0 to 60 (0 = none)", ""], ["rounds", "Rounds, 1 to 10", ""], ["lives", "Lives per round, 0 to 50 (0 = unlimited)", ""],
    ["respawn", "Respawn delay in seconds, 0 to 240", ""], ["shields", "Shields", [["The mode's own", ""], ["None", "1"]]]],
    { verb: "SAVE", required: ["name"], note: "Blank fields keep the mode's own. It's saved in the server's content folder, Game Modes." });
  if (!f) return;
  const options: Dict = {};
  for (const [k, field] of [["rounds.teams", "teams"], ["player.shields", "shields"], ...GAME_OPTIONS]) if (f[field] !== "") options[k] = Number(f[field]);
  const args = { index: st.index, name: f.name, mode: f.mode, scoreToWin: f.score === "" ? null : Number(f.score), options };
  let path: string;
  try {
    let r = await invoke<Dict>("save_game_type", { ...args, replace: false });
    if (r.exists) {
      if (!(await oni.confirm("REPLACE GAME TYPE", `${r.path} is there already.`, "REPLACE"))) return;
      r = await invoke<Dict>("save_game_type", { ...args, replace: true });
    }
    path = r.path;
  } catch (e) {
    return oni.notify(String(e), `GAME TYPE NOT SAVED · ${st.label}`, "error", 15);
  }
  if (st.online) await oni.fetchNow(st, ["modes"]);
  const stem = path.split("/").pop()!.replace(/\.json$/, "");
  const listed = (st.data.modes?.entries ?? []).some((e: Dict) => String(e.reference ?? "").replace(/\.json$/, "").endsWith(stem) || e.name === f.name);
  const how = listed ? "CHANGE MODE plays it, and NEW PLAYLIST can use it." : "The server picks it up when it restarts; then CHANGE MODE plays it.";
  oni.notify(`Saved as ${path}. ${how}`, `GAME TYPE · ${st.label}`, "information", 12);
}

/** Pick a map or a game type in the content browser. A ReclaimerForge one that isn't installed yet is installed instead
 *  (after a confirm), and null comes back: open the browser again once that has finished. */
async function browse(st: Station, what: "maps" | "modes", title: string, keep = false): Promise<string | null> {
  const got = await oni.ask<string | { listing: Dict } | null>("content", { title, what, index: st.index, keep });
  if (got === null || typeof got === "string") return got;
  const plan = await invoke<Dict>("forge_plan", { index: st.index, listing: got.listing, version: latestOf(got.listing) ?? {} });
  if (plan.error) oni.notify(plan.error.text, plan.error.short, "error", 15);
  else if (await oni.confirm(plan.title, plan.body, plan.replacing ? "REPLACE" : "INSTALL", plan.replacing)) {
    await invoke("forge_put", { plan: plan.plan });
    oni.notify("It is installing: open this again once it has finished.", `FORGE · ${st.label}`);
  }
  return null;
}

/** A game for the playlist: a map, then a game type, from the server's own lists (typed in when it hasn't sent them). */
async function pickGame(st: Station, n: number): Promise<[string, string] | null> {
  if (!st.data.maps?.entries?.length || !st.data.modes?.entries?.length) {
    const f = await oni.form("ADD GAME", [["map", "Map (as the server names it)", ""], ["game", "Game type", ""]], { verb: "ADD", required: ["map", "game"],
      note: `${st.label} hasn't listed its maps, so type the names. The server checks them before the playlist is used.` });
    return f ? [f.map, f.game] : null;
  }
  const mp = await browse(st, "maps", `GAME ${n} · SELECT MAP`);
  const md = mp ? await browse(st, "modes", `GAME ${n} · SELECT GAME TYPE`) : null;
  return mp && md ? [mp, md] : null;
}

/** The playlist editor until it is saved (the file's path) or dropped (null), with ADD GAME going out to the browser and
 *  back. `path`: a playlist this console made, to save over; null saves a new one in playlists/. */
async function editPlaylist(st: Station, p: Playlist, path: string | null, note = ""): Promise<string | null> {
  const start = JSON.stringify(p), sink: { p?: Playlist } = {};
  let sel: string | undefined;
  for (;;) {
    const r = await oni.ask<{ do: string; p: Playlist } | null>("playlist", { index: st.index, p, path, note, sel, sink });
    p = r?.p ?? sink.p ?? p; // outside the dialog counts as closing it
    if (!r || r.do === "close") {
      if (JSON.stringify(p) === start || (await oni.confirm("DISCARD CHANGES", `${p.name || "This playlist"} hasn't been saved.`, "DISCARD", true))) return null;
    } else if (r.do === "add") {
      const game = await pickGame(st, p.games.length + 1);
      if (!game) continue;
      if (p.games.some((g) => g.map === game[0] && g.game === game[1])) oni.notify(`${game[1]} on ${game[0]} is in it already: give it a higher weight instead.`, "", "warning");
      else p = { ...p, games: [...p.games, { map: game[0], game: game[1] }] };
      sel = String(p.games.length - 1);
    } else {
      const args = { index: st.index, playlist: p, path };
      try {
        let saved = await invoke<Dict>("save_playlist", { ...args, replace: false });
        if (saved.exists) {
          if (!(await oni.confirm("REPLACE PLAYLIST", `${saved.path} is there already.`, "REPLACE", true))) continue;
          saved = await invoke<Dict>("save_playlist", { ...args, replace: true });
        }
        return saved.path;
      } catch (e) {
        oni.notify(String(e), `PLAYLIST NOT SAVED · ${st.label}`, "error", 15);
      }
    }
  }
}

/** A new playlist, or `path` opened to edit: one this console made is rewritten, any other (a hand-made file, or one
 *  installed from Forge) is the start of a new one. Returns where it was saved. */
async function playlistEdit(st: Station, path: string | null): Promise<string | null> {
  let p = newPlaylist(), mine = path, note = "";
  if (path) {
    try {
      const raw = await invoke<Dict>("playlist_read", { index: st.index, path });
      p = { version: 2, name: String(raw.name ?? ""), vote_seconds: raw.vote_seconds ?? 20, games: Array.isArray(raw.games) ? raw.games : [] };
    } catch (e) {
      oni.notify(String(e), `PLAYLIST · ${st.label}`, "error", 15);
      return null;
    }
    if (!/^playlists\/[^/]+\.playlist\.json$/.test(path)) {
      mine = null;
      note = `A copy of ${path}: SAVE puts it in the server folder's playlists/ as its own playlist; the original stays as it is.`;
    }
  }
  return editPlaylist(st, p, mine, note);
}

/** PLAYLISTS: the library, with NEW and EDIT going into the editor and back, until one is used or it is closed. */
async function playlistsManager(st: Station): Promise<void> {
  let sel: string | undefined;
  for (;;) {
    const r = await oni.ask<{ do: string; path?: string } | null>("playlists", { index: st.index, sel });
    if (!r) return;
    sel = r.path;
    if (r.do === "use") return saveSetting(st, "playlist", r.path, "PLAYLIST", r.path!);
    sel = (await playlistEdit(st, r.do === "edit" ? r.path! : null)) ?? sel;
  }
}

export async function op(name: string) {
  const st = oni.cur, s = st.data.status ?? {};
  if (name === "broadcast") return broadcast();
  if (name === "reconnect") {
    let pw: string | undefined;
    if (st.state === "denied" || oni.waiting.includes(st.index)) {
      const f = await oni.form("RETRY SIGN-IN", [["pw", "RCON password", ""]], { verb: "RETRY", required: ["pw"], secret: ["pw"],
        note: "This station refused the password. Five wrong passwords in ten minutes lock your address out for up to ten minutes." });
      if (!f) return;
      pw = f.pw;
      oni.waiting = oni.waiting.filter((i) => i !== st.index);
    }
    return oni.reconnect(st, pw);
  }
  if (name === "restart") {
    if (!(await oni.confirm(`RESTART  ${st.label}`, "Everyone on it is dropped and the game starts over; it's back in a minute or so. Servers sharing its container or window restart with it, and what was changed from here (name, join password, ping limit) goes back to dedicated.toml's.", "RESTART", true))) return;
    oni.notify("Restarting… the console reconnects by itself.", `RESTART · ${st.label}`, "warning");
    return invoke("restart_server", { index: st.index }).then(() => oni.notify("Restarted.", `RESTART · ${st.label}`),
      (e) => oni.notify(String(e), `RESTART FAILED · ${st.label}`, "error", 15));
  }
  if (name === "remove") {
    if (!(await oni.confirm(`REMOVE  ${st.label}`, "Takes it out of your config file, so the console stops managing it. The server itself keeps running, and ADD SERVER puts it back.", "REMOVE", true))) return;
    try {
      await invoke("remove_server", { index: st.index });
    } catch (e) {
      return oni.notify(String(e), `REMOVE FAILED · ${st.label}`, "error", 15);
    }
    return location.reload(); // the console is rebuilt from the config, or the setup screen opens when none are left
  }
  if (name === "capacity") {
    const f = await oni.form("PLAYER CAPACITY", [["n", "Most players at once (1 to 63)", String(s.max_players ?? "")]], { verb: "SAVE", required: ["n"],
      note: "Saved in the server's dedicated.toml: it takes effect when the server restarts." });
    if (f) return saveSetting(st, "max_players", Number(f.n), "CAPACITY", `${f.n} players`);
    return;
  }
  if (name === "newgametype") return newGameType(st);
  if (name === "playlists") return playlistsManager(st);
  if (name === "newplaylist") {
    const path = await playlistEdit(st, null);
    if (path && (await oni.confirm("SWITCH PLAYLIST", `Saved as ${path}. Put the server on it now?`, "SWITCH", false))) return saveSetting(st, "playlist", path, "PLAYLIST", path);
    if (path) oni.notify(`Saved as ${path}: SWITCH PLAYLIST puts the server on it.`, `PLAYLIST · ${st.label}`);
    return;
  }
  if (name === "playlist") {
    let files: string[];
    try {
      files = await invoke<string[]>("playlists", { index: st.index });
    } catch (e) {
      return oni.notify(String(e), `PLAYLISTS · ${st.label}`, "error", 15);
    }
    // ponytail: Forge installs into the content folder, taken as dedicated.toml's default "content"
    for (const e of Object.values<Dict>(oni.forgeState.servers?.[st.where] ?? {}))
      if (e.kind === "playlist" && e.files?.[0]?.path) files.push(`content/${e.files[0].path}`);
    if (!files.length) return oni.notify("No playlists in the server's playlists folder yet: NEW PLAYLIST makes one, or install one from F6.", `PLAYLISTS · ${st.label}`, "warning");
    const got = await oni.pick("SWITCH PLAYLIST", files.map((f) => [f, f]));
    if (got) return saveSetting(st, "playlist", got, "PLAYLIST", got);
    return;
  }
  if (!st.online) return oni.notify(`${st.label} is not connected.`, "", "warning");
  if (["load", "map", "mode", "nextmap"].includes(name)) {
    let mp = "", md = "";
    if (name !== "mode") {
      if (!entries(st, "maps").length) return oni.notify("Map list not loaded yet.", "", "warning");
      const got = await browse(st, "maps", "SELECT MAP");
      if (!got) return;
      mp = got;
    }
    if (name !== "map") {
      const got = await browse(st, "modes", "SELECT MODE", name === "nextmap");
      if (got === null || (name !== "nextmap" && !got)) return;
      md = got;
    }
    const args = [mp, md].filter(Boolean);
    if (name !== "nextmap" && !(await oni.confirm(`${name.toUpperCase()}  ${args.join(" / ")}`, "Ends the current game for everyone on this station; the new one starts in the next lobby."))) return;
    oni.send(st, name, args, ["status", "nextmap"]);
  } else if (["endround", "endgame", "shuffle"].includes(name)) {
    const body = ({ endround: "Ends the round as if its time ran out.", endgame: "Ends the game with the scores as they stand.",
      shuffle: "Shuffles players across the teams that have players." } as Dict)[name];
    if (await oni.confirm(name.toUpperCase(), body, "EXECUTE", name !== "shuffle")) oni.send(st, name, [], ["status", "players"]);
  } else if (name === "teamcount") {
    const n = await oni.pick("SPREAD PLAYERS OVER", [2, 3, 4, 5, 6, 7, 8].map((i) => [`${i} teams`, String(i)]));
    if (n) oni.send(st, "teamcount", [n], ["players"]);
  } else if (name === "startvote") {
    const subjects: string[] = st.data.vote?.subjects ?? ["endround", "endgame", "shuffle", "kick", "playlist"];
    const subj = await oni.pick("CALL A VOTE", subjects.map((x) => [x.toUpperCase(), x]));
    if (!subj) return;
    const args = [subj];
    if (subj === "kick") {
      const w = await oni.pick("VOTE TO KICK", st.players.map((p) => [String(pick(p, "name")), targetOf(p)]));
      if (!w) return;
      args.push(w);
    }
    oni.send(st, "startvote", args, ["vote"]);
  } else if (name === "passvote" || name === "cancelvote") {
    oni.send(st, name, [], ["vote"]);
  } else if (name === "servername") {
    const f = await oni.form("RENAME STATION", [["name", "Server name (until restart)", String(s.name ?? "")]], { verb: "RENAME", required: ["name"] });
    if (f) oni.send(st, "servername", [f.name], ["status"]);
  } else if (name === "password") {
    const f = await oni.form("JOIN PASSWORD", [["pw", "Password players need (empty = open server)", ""]], { verb: "SET", note: "Lasts until restart. Doesn't touch the RCON password." });
    if (f) oni.send(st, "password", [f.pw], ["status"]);
  } else if (name === "maxping") {
    const f = await oni.form("PING LIMIT", [["ms", "Highest join ping in ms, up to 1000 (off = none)", String(s.max_ping || "off")]], { verb: "SET", required: ["ms"] });
    if (f) oni.send(st, "maxping", [f.ms], ["status"]);
  }
}

const ADMIN_NOTE = {
  add: "They can kick, ban and move players in game, and can't be vote-kicked. It applies at once on every server sharing this ban list.",
  remove: "Applies at once on every server sharing this ban list. An admin written in dedicated.toml can't be removed from here.",
};

export async function bl(name: string, ban?: string | null, vpn?: string | null, admin?: string | null, fixed?: boolean) {
  const st = oni.cur;
  if (!st.online) return oni.notify(`${st.label} is not connected.`, "", "warning");
  if (name === "newban") {
    const f = await oni.form("BAN BY ID OR ADDRESS", [["target", "Player ID, IP address or range (CIDR)", ""], ["time", "Duration", BAN_TIMES], ["reason", "Reason", ""]],
      { verb: "BAN", danger: true, required: ["target"] });
    if (f) oni.send(st, "ban", [f.target, ...opt(f.time), ...opt(f.reason)], ["bans"]);
  } else if (name === "unban" || name === "vpnrevoke") {
    const target = name === "unban" ? ban : vpn;
    if (!target) return oni.notify("Select an entry first.", "", "warning");
    if (await oni.confirm(`${name.toUpperCase()}  ${redactText(target, oni.redact)}`, "Lifts it together with its linked entries, on every server sharing the ban list.", name.toUpperCase(), false))
      oni.send(st, name, [target], ["bans", "vpn"]);
  } else if (name === "vpnallow") {
    const f = await oni.form("VPN ALLOW", [["target", "Player ID, IP address or range", ""], ["note", "Note", ""]], { verb: "ALLOW", required: ["target"] });
    if (f) oni.send(st, "vpnallow", [f.target, ...opt(f.note)], ["vpn"]);
  } else if (name === "adminadd") {
    const choices: [Line | string, string][] = st.players.filter((p) => !p.admin).map((p) => [String(pick(p, "name") ?? "?"), targetOf(p)]);
    const who = await oni.pick("MAKE ADMIN", [...choices, [[["Someone not on: type their player ID…", DIM]], "?"]]);
    if (!who) return;
    if (who !== "?") return makeAdmin(st, who, String(choices.find((c) => c[1] === who)?.[0] ?? who));
    const f = await oni.form("MAKE ADMIN", [["id", "Player ID", ""]], { verb: "MAKE ADMIN", required: ["id"], note: ADMIN_NOTE.add });
    if (f) return makeAdmin(st, f.id.trim(), f.id.trim(), true);
  } else if (name === "adminremove") {
    if (!admin) return oni.notify("Select an admin first.", "", "warning");
    if (fixed) {
      if (await oni.confirm(`REMOVE ADMIN  ${admin}`, "Written in dedicated.toml: taken out of this server's admins there, from the next restart.", "REMOVE"))
        return saveSetting(st, "admin_remove", admin, "ADMIN", `${admin} no longer an admin of this server`);
      return;
    }
    if (await oni.confirm(`REMOVE ADMIN  ${admin}`, ADMIN_NOTE.remove, "REMOVE"))
      oni.send(st, "admin", ["remove", admin], ["admins", "players"]);
  } else if (name === "vpncheck") {
    const f = await oni.form("CHECK AN ADDRESS", [["ip", "IP address", ""]], { verb: "CHECK", required: ["ip"] });
    if (f) oni.send(st, "vpn", [f.ip]);
  }
}

/** Every station in a list to type into, the ones with players first: 1 to 9 only reach so far. */
export async function goto() {
  const order = (s: Station) => [s.online ? 0 : 1, -(num(s.data.status?.players) ?? 0), s.index];
  const sorted = [...oni.stations].sort((a, b) => {
    const x = order(a), y = order(b);
    return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
  });
  const row = (s: Station): Line => {
    const st = s.data.status ?? {}, n = num(st.players), mx = num(st.max_players);
    const [g, c] = STATE[s.state] ?? STATE.connecting;
    return [[`${g} `, c], [`${String(s.index + 1).padStart(3)}  `, DIM], [s.online && n !== undefined ? `${n}/${mx}  ` : "", n ? AMBER : DIM],
      [s.label, WHITE], [s.tag ? `  ${s.tag}` : "", CYAN], [s.online ? `  ${String(st.map ?? "").replace(/_/g, " ")}` : `  ${oni.why(s)}`, DIM]];
  };
  const i = await oni.pick("GO TO STATION", sorted.map((s) => [row(s), String(s.index)]));
  if (i !== null) oni.select(Number(i));
}

export const STATE: Record<string, [string, string]> = { online: ["◉", "#5FB98A"], connecting: ["◌", "#D9A441"], offline: ["○", "#E5484D"], denied: ["⊘", "#E5484D"] };

export async function addServer(onSetup: () => void) {
  await invoke("setup_open");
  oni.stop();
  onSetup();
}

export async function askPassword() {
  if (!oni.waiting.length) return;
  const f = await oni.form("RCON PASSWORD", [["pw", "Used for every server without its own", ""]], { verb: "CONNECT", required: ["pw"], secret: ["pw"],
    note: `${oni.waiting.length} server${oni.waiting.length === 1 ? " has" : "s have"} no password in the config, $ONI_RCON_PASSWORD or a password_command.` });
  if (!f) return;
  await invoke("set_password", { password: f.pw, indices: oni.waiting });
  oni.waiting = [];
}

export function sayText(text: string) {
  text = text.trim();
  const all = text.startsWith("@all ");
  const targets = (all ? oni.stations : [oni.cur]).filter((s) => s.online);
  text = text.replace(/^@all /, "").trim();
  if (text && targets.length) oni.fanout(targets, "say", [text]);
  if (text && !targets.length) oni.notify("Not connected: nothing was sent.", "", "warning");
}

// --- automation --------------------------------------------------------------------------------------------------
const serverName = (k: string) => oni.stations.find((x) => x.where === k)?.label ?? k;
const DONE = "\0done";

/** A pick list you can tick several of; Done returns the ticked values. */
async function toggles(title: string, options: [string, string][], on: string[]): Promise<string[] | null> {
  const set = new Set(on);
  for (;;) {
    const got = await oni.pick(title, [...options.map(([l, v]) => [`${set.has(v) ? "✓" : "  "} ${l}`, v] as [string, string]), ["Done", DONE]]);
    if (got === null) return null;
    if (got === DONE) return [...set];
    if (!set.delete(got)) set.add(got);
  }
}

/** The form row for one catalog field: the current value first in a select, so it is what Enter keeps. */
function fieldRow(f: Field, now: unknown): [string, string, string | [string, string][]] | null {
  const cur = String(now ?? f.default ?? "");
  const first = (opts: [string, string][]): [string, string][] => [...opts.filter(([, v]) => v === cur), ...opts.filter(([, v]) => v !== cur)];
  if (f.kind === "choice") return [f.key, f.label, first(f.options!.map((o) => [o, o]))];
  if (f.kind === "server") return [f.key, f.label, first(oni.stations.map((x) => [x.label, x.where]))];
  if (f.kind === "webhook") {
    const names = Object.keys(oni.runner.webhooks);
    return names.length ? [f.key, f.label, first(names.map((n) => [`${n}  ${oni.runner.webhooks[n]}`, n]))] : null;
  }
  return [f.key, f.label, cur];
}

/** One WHEN, IF or THEN block of the given type, asked field by field and checked; `init` holds the values to start from. */
async function askBlock(part: Part, type: string, init: Dict = {}): Promise<Dict | null> {
  const { label, hint, fields } = CATALOG[part][type];
  if (!fields.length) return block(part, { type });
  let cur: Dict = { ...init }, note = hint + (fields.some((f) => f.kind === "message") ? `\nIn a message: ${TEMPLATE_HELP}` : "");
  for (;;) {
    const rows = fields.map((f) => fieldRow(f, cur[f.key]));
    if (rows.includes(null)) {
      oni.notify("Add a Discord webhook first (DISCORD in the automation list), then use this step.", "AUTOMATION", "warning", 10);
      return null;
    }
    const v = await oni.form(label.toUpperCase(), rows as [string, string, string | [string, string][]][], { verb: "OK", note });
    if (!v) return null;
    cur = v;
    try {
      return block(part, { type, ...v });
    } catch (e) {
      note = `${(e as Error).message}\n${hint}`;
    }
  }
}

const pickType = (part: Part) => oni.pick({ when: "WHEN", if: "ONLY IF", then: "THEN DO" }[part], Object.entries(CATALOG[part]).map(([k, e]) => [e.label, k] as [string, string]));

/** The IF or THEN list: add, edit, move and remove blocks until Done. */
async function blocks(part: "if" | "then", list: Dict[]): Promise<Dict[] | null> {
  const lo = part === "then" ? 1 : 0;
  for (;;) {
    const got = await oni.pick(part === "if" ? "ONLY IF · all of these must hold" : "THEN DO · in this order", [
      ...list.map((b, i) => [`${i + 1}. ${describeBlock(part, b)}`, `b${i}`] as [string, string]),
      ...(list.length < 8 ? [[part === "if" ? "+ Add a condition" : "+ Add an action", "add"] as [string, string]] : []),
      [list.length >= lo ? "Done" : "Done (a rule needs at least one action)", DONE],
    ]);
    if (got === null) return null;
    if (got === DONE) {
      if (list.length >= lo) return list;
      oni.notify("A rule needs something to do.", "AUTOMATION", "warning");
    } else if (got === "add") {
      const type = await pickType(part);
      const b = type && (await askBlock(part, type));
      if (b) list = [...list, b];
    } else {
      const i = Number(got.slice(1)), act = await oni.pick(describeBlock(part, list[i]), [["Edit", "edit"], ["Move up", "up"], ["Remove", "rm"]]);
      if (act === "edit") {
        const b = await askBlock(part, list[i].type, list[i]);
        if (b) list = list.map((x, j) => (j === i ? b : x));
      } else if (act === "up" && i > 0) list = list.map((x, j) => (j === i - 1 ? list[i] : j === i ? list[i - 1] : x));
      else if (act === "rm") list = list.filter((_, j) => j !== i);
    }
  }
}

/** Build a rule (or change `base`) a step at a time: name, servers, WHEN, IF, THEN, limits, then a read-back. Null: dropped. */
async function ruleFlow(base: Dict | null): Promise<Dict | null> {
  const stations = oni.stations;
  const some = !!base && base.servers !== "all";
  const f = await oni.form(base?.id ? "EDIT RULE" : "NEW RULE", [["name", "Name", base?.name ?? ""],
    ...(stations.length > 1 ? [["scope", "Runs on", some ? [["Chosen servers", "some"], ["Every server", "all"]] : [["Every server", "all"], ["Chosen servers", "some"]]] as [string, string, [string, string][]]] : [])],
  { verb: "NEXT", required: ["name"] });
  if (!f) return null;
  let servers: "all" | string[] = "all";
  if (f.scope === "some") {
    const got = await toggles("RUNS ON · tick the servers", stations.map((x) => [x.label, x.where] as [string, string]), some ? base!.servers : []);
    if (!got) return null;
    if (got.length) servers = got;
    else oni.notify("None ticked, so it runs on every server.", "AUTOMATION");
  }
  const type = await pickType("when");
  if (!type) return null;
  const when = await askBlock("when", type, base?.when?.type === type ? base.when : {});
  const conds = when && (await blocks("if", base?.if ?? []));
  const acts = conds && (await blocks("then", base?.then ?? []));
  if (!when || !conds || !acts) return null;
  const draft = validate({ name: f.name.trim(), servers, when, if: conds, then: acts });
  let limits: Dict | null = { cooldown: String(base?.cooldown ?? draft.cooldown), per_hour: String(base?.per_hour ?? draft.per_hour) };
  let note = "Cooldown: seconds before it can fire again for the same server and player. Per hour: the most times it may fire.";
  for (;;) {
    limits = await oni.form("LIMITS", [["cooldown", "Cooldown (seconds)", limits!.cooldown], ["per_hour", "Most times an hour (1–600)", limits!.per_hour]], { verb: "NEXT", note });
    if (!limits) return null;
    try {
      const rule = validate({ ...draft, id: base?.id, enabled: base?.enabled ?? true, cooldown: Number(limits.cooldown), per_hour: Number(limits.per_hour) });
      return (await oni.confirm("SAVE THIS RULE", describe(rule, serverName), "SAVE", false)) ? rule : null;
    } catch (e) {
      note = (e as Error).message;
    }
  }
}

/** TEST: what the rule would do if it fired right now on a server, with the option of really doing it. */
async function testRule(rule: Dict) {
  const up = oni.stations.filter((x) => x.online);
  if (!up.length) return oni.notify("No server is connected to test on.", "AUTOMATION", "warning");
  const where = up.length === 1 ? up[0].where : await oni.pick("TEST ON WHICH SERVER", up.map((x) => [x.label, x.where] as [string, string]));
  if (!where) return;
  const fire = sample(oni.runner.engine, rule, where), steps = plan(oni.runner.engine, fire);
  const body = steps.length ? steps.map((x) => `• ${stepWords(oni.runner.engine, x)}`).join("\n") : "Nothing: there is nobody it would tell.";
  const text = `If this fired now on ${serverName(where)} (its conditions and limits aside) it would:\n${body}`;
  if (!steps.length) return void (await oni.confirm(`TEST · ${rule.name}`, text, "OK", false));
  if (await oni.confirm(`TEST · ${rule.name}`, `${text}\n\nSEND FOR REAL does these now; ABORT only showed them.`, "SEND FOR REAL", false)) await oni.runner.steps(fire, steps);
}

/** Who has which role. ADMIN here is for rules only (who counts as an admin); the server's own admin list is under F4. */
async function rolesFlow() {
  const roles = { ...oni.roles };
  const id = (p: Dict) => String(pick(p, "player_id", "id") ?? "");
  for (;;) {
    const got = await oni.pick("ROLES · who rules treat as ADMIN, VIP or MOD", [
      ...Object.entries(roles).map(([k, p]) => [`${p.name || k.slice(0, 12)}  ${p.roles.join(", ").toUpperCase()}`, k] as [string, string]),
      ["+ Add a player", "add"], ["Done", DONE]]);
    if (got === null) return;
    if (got === DONE) return void (await oni.saveAutomation(oni.rules, roles).catch((e) => oni.notify(String(e), "AUTOMATION", "error", 10)));
    let who = got, name = roles[got]?.name ?? "";
    if (got === "add") {
      const seen = new Map<string, [string, string]>();
      for (const x of oni.stations) for (const p of x.players) if (id(p)) seen.set(id(p), [String(pick(p, "name") ?? id(p)), x.label]);
      const chosen = await oni.pick("WHICH PLAYER", [...[...seen].map(([k, [n, at]]) => [`${n}  (${at})`, k] as [string, string]), ["Type a player ID…", "type"]]);
      if (!chosen) continue;
      if (chosen === "type") {
        const t = await oni.form("PLAYER ID", [["id", "Player ID (64 characters)", ""], ["name", "Name (to recognise them by)", ""]], { verb: "OK", required: ["id"] });
        if (!t) continue;
        [who, name] = [t.id.trim(), t.name.trim()];
      } else [who, name] = [chosen, seen.get(chosen)![0]];
    } else {
      const act = await oni.pick(name || who, [["Change roles", "edit"], ["Remove", "rm"]]);
      if (act === "rm") delete roles[who];
      if (act !== "edit") continue;
    }
    const picked = await toggles(`ROLES · ${name || who.slice(0, 12)}`, ROLES.map((r) => [r.toUpperCase(), r] as [string, string]), roles[who]?.roles ?? []);
    if (!picked) continue;
    if (picked.length) roles[who] = { name, roles: picked, servers: roles[who]?.servers ?? "all" };
    else delete roles[who];
  }
}

/** Discord webhooks: add, send a test, remove. The URLs stay in the app's own storage; this window sees them hidden. */
async function discordFlow() {
  for (;;) {
    const got = await oni.pick("DISCORD WEBHOOKS", [...Object.entries(oni.runner.webhooks).map(([n, u]) => [`${n}  ${u}`, n] as [string, string]),
      ["+ Add a webhook", "add"], ["How do I get a webhook URL?", "help"], ["Done", DONE]]);
    if (got === null || got === DONE) return;
    try {
      if (got === "help") await oni.confirm("A DISCORD WEBHOOK", DISCORD_STEPS, "OK", false);
      else if (got === "add") {
        const f = await oni.form("ADD A WEBHOOK", [["name", "Name (letters, numbers, - _), like alerts", ""], ["url", "Webhook URL", ""]], { verb: "ADD", required: ["name", "url"], secret: ["url"] });
        if (f) {
          await invoke("discord_add", { name: f.name, url: f.url });
          await oni.loadAutomation();
          oni.notify("Added: pick it and SEND A TEST to see it arrive.", "DISCORD");
        }
      } else {
        const act = await oni.pick(got, [["Send a test message", "test"], ["Remove", "rm"]]);
        if (act === "test") {
          const code = await invoke<number>("discord_post", { name: got, text: "Test message from ONI RCON: this webhook works." });
          const ok = code >= 200 && code < 300;
          oni.notify(ok ? "Sent: check the channel." : `Discord answered ${code}.`, "DISCORD", ok ? "information" : "error");
        } else if (act === "rm" && (await oni.confirm("REMOVE WEBHOOK", `${got} will stop working in every rule that posts to it.`, "REMOVE", true))) {
          await invoke("discord_remove", { name: got });
          await oni.loadAutomation();
        }
      }
    } catch (e) {
      oni.notify(String(e), "DISCORD", "error", 12);
    }
  }
}

/** AUTOMATION: the rules list, with each button going out to its flow and back until it is closed. */
export async function automation() {
  let sel: string | undefined;
  for (;;) {
    const r = await oni.ask<{ do: string; id?: string } | null>("automation", { sel });
    if (!r) return;
    sel = r.id ?? sel;
    const rule = oni.rules.find((x) => x.id === r.id);
    const save = (rules: Dict[]) => oni.saveAutomation(rules);
    try {
      if (r.do === "new" || r.do === "template" || (r.do === "edit" && rule)) {
        if (r.do !== "edit" && oni.rules.length >= MAX_RULES) {
          oni.notify(`${MAX_RULES} rules is the limit.`, "AUTOMATION", "warning");
          continue;
        }
        let base: Dict | null = r.do === "edit" ? rule! : null;
        if (r.do === "template") {
          const name = await oni.pick("TEMPLATE · a starting point to change", STARTERS.map((t) => [`${t.name}  ·  ${CATALOG.when[t.when.type].label}`, t.name] as [string, string]));
          const t = STARTERS.find((x) => x.name === name);
          if (!t) continue;
          const hook = Object.keys(oni.runner.webhooks)[0];
          if (t.then.some((a: Dict) => a.type === "discord") && !hook) {
            oni.notify("This one posts to Discord: add a webhook first (DISCORD).", "AUTOMATION", "warning", 10);
            continue;
          }
          base = { ...t, then: t.then.map((a: Dict) => (a.type === "discord" ? { ...a, webhook: hook } : a)) };
        }
        const made = await ruleFlow(base);
        if (made) {
          await save(rule && r.do === "edit" ? oni.rules.map((x) => (x.id === rule.id ? made : x)) : [...oni.rules, made]);
          sel = made.id;
        }
      } else if (rule && r.do === "toggle") await save(oni.rules.map((x) => (x === rule ? { ...x, enabled: !x.enabled } : x)));
      else if (rule && r.do === "delete") {
        if (await oni.confirm("DELETE RULE", `${rule.name} is removed for good.`, "DELETE", true)) await save(oni.rules.filter((x) => x !== rule));
      } else if (rule && r.do === "test") await testRule(rule);
      else if (r.do === "roles") await rolesFlow();
      else if (r.do === "discord") await discordFlow();
    } catch (e) {
      oni.notify(String(e), "AUTOMATION", "error", 12);
    }
  }
}
