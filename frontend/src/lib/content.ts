// What a server can load, sorted into the groups people look for, and the playlist files a lobby plays. From
// ibigs/oni-rcon's content.py, by IBIGS.
//
// The server's `maps` and `modes` replies only say map, mode or variant; the reference says where each came from:
//   {kind: "map", name: "Valhalla", reference: "riverworld"}                       a base map (stock or a mod)
//   {kind: "mode", name: "Slayer", reference: "Slayer"}                            a base game type
//   {kind: "variant", reference: "Game Modes/Forge/rf-d70540eb57f22df48390.json"}  a file in the content folder
//   {kind: "variant", reference: "default_guardian_012.mvar"}                      Halo 3 / Matchmaking, built in
//   {kind: "variant", name: "MLG Guardian", reference: "MLG Guardian"}             saved in MCC, or Workshop
// Nothing here talks to a server or to Forge: it is given what they said and returns rows to show.
import * as fuzzy from "./fuzzy";
import { authorOf, kindOf, latestOf, listingId, metricsOf, norm, recentOf, refsOf, titleOf, versionLabel, votesOf } from "./forge";
import { num, pick, type Dict } from "./util";

/** Halo 3's multiplayer maps, by the engine name the server references them with. */
export const STOCK_MAPS = new Map(Object.entries({
  armory: "Rat's Nest", bunkerworld: "Standoff", chill: "Narrows", chillout: "Cold Storage",
  construct: "Construct", cyberdyne: "The Pit", deadlock: "High Ground", descent: "Assembly",
  docks: "Longshore", fortress: "Citadel", ghosttown: "Ghost Town", guardian: "Guardian",
  isolation: "Isolation", lockout: "Blackout", midship: "Heretic", riverworld: "Valhalla",
  salvation: "Epitaph", sandbox: "Sandbox", shrine: "Sandtrap", sidewinder: "Avalanche",
  snowbound: "Snowbound", spacecamp: "Orbital", warehouse: "Foundry", zanzibar: "Last Resort",
  s3d_edge: "Edge", s3d_turf: "Icebox", s3d_waterfall: "Waterfall",
}));
const STOCK_NAMES = new Set([...STOCK_MAPS.values()].map(norm));

export type Group = "stock" | "mods" | "forge" | "folder" | "builtin" | "saved" | "catalog";
export const MAP_GROUPS: [Group, string][] = [["stock", "Stock maps"], ["mods", "Mod maps"], ["forge", "ReclaimerForge downloads"],
  ["folder", "Your content folder"], ["builtin", "Halo 3 & Matchmaking variants"], ["saved", "Saved & Workshop variants"], ["catalog", "Get more from ReclaimerForge"]];
export const MODE_GROUPS: [Group, string][] = [["stock", "Base game types"], ["forge", "ReclaimerForge downloads"],
  ["folder", "Your content folder"], ["builtin", "Halo 3 & Matchmaking variants"], ["saved", "Saved & Workshop variants"], ["catalog", "Get more from ReclaimerForge"]];
export const GROUP_HELP: Record<Group, string> = {
  stock: "Halo 3's own multiplayer maps and game types.",
  mods: "Custom .map packages in the content folder's Mods, or installed from the Workshop.",
  forge: "Installed from ReclaimerForge (F6, or uploaded into a Forge folder).",
  folder: "Files you put in the content folder yourself.",
  builtin: "Variants that ship with Halo 3 and MCC Matchmaking.",
  saved: "Variants saved in MCC, and Workshop variants.",
  catalog: "Not on this server yet: choosing one downloads and installs it first.",
};

/** The browser's column buttons: click one to order by it, again to turn the order round. */
export const SORTS: [string, string][] = [["name", "NAME"], ["recent", "INSTALLED"], ["downloads", "DOWNLOADS"], ["rated", "RATING"]];
const HIGH_FIRST = new Set(["recent", "downloads", "rated"]); // first click: newest, most, best

export interface Item {
  name: string;
  reference: string;
  group: Group;
  folder: string; // a subfolder inside its group, for files: "UFM Encompass"
  kind: string; // map, mode or variant, as the server said; or the catalog's map / gametype
  detail: string; // what is shown dimmed after the name
  forge: Dict | null; // the forge-state entry, or (group "catalog") the listing
  installedAt: string;
  downloads: number;
  rating: number;
}

const item = (name: string, reference: string, group: Group, o: Partial<Item> = {}): Item =>
  ({ name, reference, group, folder: "", kind: "", detail: "", forge: null, installedAt: "", downloads: 0, rating: 0, ...o });

/** Everything the filter matches against. */
export const words = (i: Item) =>
  [i.name, i.reference, i.folder, i.detail, String(i.forge?.title ?? ""), String(i.forge?.author ?? "")].join(" ").toLowerCase();

const slashed = (ref: string) => ref.replace(/\\/g, "/");
export const isPath = (ref: string) => ["maps", "game modes", "mods", "playlists"].includes(slashed(ref).split("/")[0].toLowerCase());
export const stemOf = (ref: string) => (ref ? norm(slashed(ref).split("/").pop()!.replace(/(?<=.)\.[^.]*$/, "")) : "");

export interface ForgeIndex { files: Map<string, Dict>; names: Map<string, Dict> }

/** Every name an installed Forge listing goes by, to its forge-state entry: "files" by the names of the files it put in
 *  the content folder (rf-<sha>), "names" by its title and the manifest's reference as well. */
export function forgeIndex(installed: Record<string, Dict>): ForgeIndex {
  const out: ForgeIndex = { files: new Map(), names: new Map() };
  for (const [lid, e0] of Object.entries(installed)) {
    const e: Dict = { ...e0, listing_id: e0.listing_id || lid };
    for (const f of e.files ?? []) {
      const n = stemOf(String(f.path ?? ""));
      if (n && !out.files.has(n)) out.files.set(n, e);
    }
    for (const n of refsOf(e)) if (!out.names.has(n)) out.names.set(n, e);
  }
  return out;
}

/** The installed Forge listing a server entry is, if it is one. A file in the content folder is one only by its own
 *  file name: another file with the same title is someone else's copy. */
export function forgeEntry(e: Dict, index: ForgeIndex): Dict | null {
  const ref = String(pick(e, "reference") ?? ""), name = String(pick(e, "name") ?? "");
  if (isPath(ref)) return index.files.get(stemOf(ref)) ?? null;
  for (const n of [stemOf(ref), norm(ref), norm(name)]) {
    const hit = n ? index.files.get(n) ?? index.names.get(n) : undefined;
    if (hit) return hit;
  }
  return null;
}

/** A `maps` or `modes` entry as an Item in its group. */
export function classify(e: Dict, index: ForgeIndex): Item {
  const kind = String(e.kind ?? "");
  const ref = String(pick(e, "reference", "name") ?? "");
  const name = String(pick(e, "name", "reference") ?? "?").trim() || ref;
  const hit = forgeEntry(e, index);
  let group: Group, folder = "", detail = "";
  if (kind === "map") {
    group = STOCK_MAPS.has(ref.toLowerCase()) || STOCK_NAMES.has(norm(name)) ? "stock" : "mods";
    detail = ref;
  } else if (kind === "mode") {
    group = "stock";
  } else if (isPath(ref)) {
    const parts = slashed(ref).split("/");
    let inside = parts.slice(1, -1);
    if ((inside.length && ["forge", "reclaimerforge", "forge subscriptions"].includes(inside[0].toLowerCase())) || hit) {
      group = "forge";
      if (inside.length && /^(forge|reclaimer)/i.test(inside[0])) inside = inside.slice(1);
    } else group = "folder";
    folder = inside.filter((p) => !/^[0-9a-f-]{8,}$/.test(p)).join("/"); // not a version's own folder
    detail = parts[parts.length - 1];
  } else if (/\.(mvar|bin|json)$/i.test(ref)) {
    group = "builtin";
    detail = ref;
  } else group = hit ? "forge" : "saved";
  const out = item(name, ref, hit ? "forge" : group, { folder, kind, detail, forge: hit });
  if (hit) {
    out.installedAt = String(hit.installed_at ?? "");
    out.detail = [hit.version ? `v${hit.version}` : "", String(hit.author ?? "")].filter(Boolean).join(" · ");
  }
  return out;
}

const DOWNLOAD_KEYS = ["download_count", "downloads", "downloads_total", "total_downloads"];
export const downloadsOf = (x: Dict): number => num(pick(x, ...DOWNLOAD_KEYS)) || num(pick(metricsOf(x), ...DOWNLOAD_KEYS)) || recentOf(x) || 0;

/** Forge listings of one kind (map or gametype) that are not installed here yet. */
export function catalogItems(listings: Dict[], want: string, have: Set<string>): Item[] {
  const out: Item[] = [];
  for (const x of listings) {
    const lid = listingId(x);
    if (kindOf(x) !== want || !lid || have.has(lid) || String(x.status ?? "").toLowerCase() === "withdrawn") continue;
    const [up, down] = votesOf(x), v = latestOf(x);
    const detail = [v ? `v${versionLabel(v)}` : "", authorOf(x), up ? `▲${up}` : ""].filter(Boolean).join(" · ");
    out.push(item(titleOf(x), "", "catalog", { kind: want, detail, forge: x, downloads: downloadsOf(x), rating: (up ?? 0) - (down ?? 0) }));
  }
  return out;
}

/** What an item is ordered by, or undefined when it does not say (those go last, by name, either way round). */
function sortValue(how: string, i: Item): string | number | undefined {
  if (how === "recent") return i.installedAt || undefined;
  if (how === "downloads") return i.forge ? i.downloads : undefined;
  if (how === "rated") return i.forge ? i.rating : undefined;
  return i.name.toLowerCase();
}

const cmp = (a: string | number, b: string | number) => (a < b ? -1 : a > b ? 1 : 0);

export function ordered(rows: Item[], how = "name", flip = false): Item[] {
  const known = rows.filter((i) => sortValue(how, i) !== undefined);
  const rest = rows.filter((i) => sortValue(how, i) === undefined).sort((a, b) => cmp(a.name.toLowerCase(), b.name.toLowerCase()));
  const sign = HIGH_FIRST.has(how) !== flip ? -1 : 1;
  known.sort((a, b) => sign * (cmp(sortValue(how, a)!, sortValue(how, b)!) || cmp(a.name.toLowerCase(), b.name.toLowerCase())));
  return [...known, ...rest];
}

/** (group key, group title, items) in the order shown, filtered by text (every word must match) and ordered. Empty
 *  groups are left out. */
export function arrange(items: Item[], what: "maps" | "modes", how = "name", text = "", flip = false): [Group, string, Item[]][] {
  const ws = text.toLowerCase().split(/\s+/).filter(Boolean);
  const out: [Group, string, Item[]][] = [];
  for (const [key, title] of what === "maps" ? MAP_GROUPS : MODE_GROUPS) {
    const rows = ordered(items.filter((i) => i.group === key && (!ws.length || fuzzy.matches(text, words(i)))), how, flip);
    if (rows.length) out.push([key, title, rows]);
  }
  return out;
}

export const itemsFor = (entries: Dict[], installed: Record<string, Dict>): Item[] => {
  const index = forgeIndex(installed);
  return entries.filter((e) => e && typeof e === "object").map((e) => classify(e, index));
};

// --- playlists: the files a lobby's `playlist = "playlists/<file>.json"` names ---------------------------------------
export const MIN_GAMES = 3; // the server's ballots offer three different games each time
export const MAX_GAMES = 128; // the most the dedicated server plays in one rotation
const PLAYLIST_KEYS = new Set(["version", "name", "vote_seconds", "games"]);
const GAME_KEYS = new Set(["map", "game", "name", "weight", "options", "world", "rules", "anti_cheat", "voice_team_only"]);

export interface Game { map: string; game: string; weight?: number; name?: string; options?: unknown; world?: unknown; rules?: unknown }
export interface Playlist { version: 2; name: string; vote_seconds: number; games: Game[] }

export class PlaylistError extends Error {}

export const newPlaylist = (name = "New playlist"): Playlist => ({ version: 2, name, vote_seconds: 20, games: [] });

const whole = (v: unknown, lo: number, hi: number) => typeof v === "number" && Number.isInteger(v) && v >= lo && v <= hi;

/** A playlist in the shape the dedicated server reads (version 2), or a PlaylistError saying why not. Unknown keys are
 *  refused, the way the server's own import refuses them; whether each map and game is installed is the server's to
 *  say. The backend holds a saved playlist to the same rules (host.rs check_playlist). */
export function checkPlaylist(p: unknown): Playlist {
  if (!p || typeof p !== "object" || Array.isArray(p)) throw new PlaylistError("A playlist is a JSON object with a name and a list of games.");
  const o = p as Dict;
  const extra = Object.keys(o).filter((k) => !PLAYLIST_KEYS.has(k));
  if (extra.length) throw new PlaylistError(`Unknown playlist setting: ${extra.sort().join(", ")}.`);
  if ((o.version ?? 2) !== 2) throw new PlaylistError("Only version 2 playlists are supported.");
  const name = String(o.name ?? "").trim();
  if (!name) throw new PlaylistError("Give the playlist a name.");
  const vote = o.vote_seconds ?? 20;
  if (!whole(vote, 5, 120)) throw new PlaylistError("The vote lasts 5 to 120 seconds.");
  const games = o.games;
  if (!Array.isArray(games) || games.length < MIN_GAMES)
    throw new PlaylistError(`A playlist needs at least ${MIN_GAMES} games (it has ${Array.isArray(games) ? games.length : 0}): players vote on three different games each time.`);
  if (games.length > MAX_GAMES) throw new PlaylistError(`A playlist holds at most ${MAX_GAMES} games; this one has ${games.length}.`);
  const seen = new Set<string>();
  const out = games.map((g, i): Game => {
    const n = i + 1;
    if (!g || typeof g !== "object" || Array.isArray(g)) throw new PlaylistError(`Game ${n} isn't a map and game type.`);
    const bad = Object.keys(g).filter((k) => !GAME_KEYS.has(k));
    if (bad.length) throw new PlaylistError(`Game ${n} has unknown settings: ${bad.sort().join(", ")}.`);
    if (!String(g.map ?? "").trim() || !String(g.game ?? "").trim()) throw new PlaylistError(`Game ${n} needs both a map and a game type.`);
    if (!whole(g.weight ?? 1, 1, 100)) throw new PlaylistError(`Game ${n}'s weight must be a whole number from 1 to 100.`);
    const pair = `${g.map}\0${g.game}`;
    if (seen.has(pair)) throw new PlaylistError(`${g.game} on ${g.map} is in it twice: give it a higher weight instead.`);
    seen.add(pair);
    return Object.fromEntries(Object.entries(g).filter(([k, v]) => !(k === "weight" && v === 1) && v !== "" && v !== null && v !== undefined)) as unknown as Game;
  });
  return { version: 2, name, vote_seconds: vote as number, games: out };
}

/** "Team Slayer on Guardian ×3", the way the server's own playlist listing words it. */
export function gameLabel(g: Game): string {
  const w = g.weight ?? 1;
  const label = g.name || `${g.game ?? "?"} on ${g.map ?? "?"}`;
  return w && w !== 1 ? `${label}  ×${w}` : label;
}

export const slug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "playlist";
