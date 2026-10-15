// Finding a player on any server. From ibigs/oni-rcon's player_search.py, by IBIGS: every term must match the same
// player; names tolerate a small typo (4+ characters) and rank after exact matches; tags and IDs are never fuzzy.
import { pick, teamOf, type Dict } from "./util";

export interface Found {
  index: number; // the station
  key: string; // the player's row on F1
  name: string; tag: string; clan: string; team: string; id: string; server: string; node: string; port: string;
}

const FIELDS = ["name", "tag", "clan", "team", "id", "server", "node", "port"] as const;

export const normalized = (v: unknown) => String(v ?? "").normalize("NFKC").toLowerCase();

/** The query's terms; "quotes" keep a phrase together, and one still being typed doesn't stop the filter. */
export function filterTerms(query: string): string[] {
  const q = normalized(query);
  if ((q.match(/["']/g) ?? []).length % 2) return q.replace(/["']/g, "").split(/\s+/).filter(Boolean);
  return [...q.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)].map((m) => m[1] ?? m[2] ?? m[3]);
}

const memo = new Map<string, number | null>();

/** A small bounded edit distance (one adjacent swap counts as one) from the term to the name or to a word of it;
 *  null when it is more than 1 away (2 for terms of 8+), or the term is under 4 characters. */
export function nameDistance(term: string, name: string): number | null {
  if (term.length < 4 || term.length > 64) return null;
  const k = term + "\0" + name;
  if (memo.has(k)) return memo.get(k)!;
  const limit = term.length < 8 ? 1 : 2;
  let best = limit + 1;
  for (const c of new Set([name, ...(name.match(/[\p{L}\p{N}_]+/gu) ?? [])])) {
    if (c.length > 64 || Math.abs(term.length - c.length) > limit) continue;
    let prevprev: number[] = [], prev = Array.from({ length: c.length + 1 }, (_, j) => j);
    for (let i = 1; i <= term.length; i++) {
      const row = [i];
      for (let j = 1; j <= c.length; j++) {
        let cost = Math.min(row[j - 1] + 1, prev[j] + 1, prev[j - 1] + (term[i - 1] !== c[j - 1] ? 1 : 0));
        if (i > 1 && j > 1 && term[i - 1] === c[j - 2] && term[i - 2] === c[j - 1]) cost = Math.min(cost, prevprev[j - 2] + 1);
        row.push(cost);
      }
      [prevprev, prev] = [prev, row];
    }
    best = Math.min(best, prev[c.length]);
  }
  const out = best <= limit ? best : null;
  if (memo.size > 4096) memo.clear();
  memo.set(k, out);
  return out;
}

/** The score of one term against one player: 0 for an exact or substring match, the edit distance for an approximate
 *  name, null for no match. */
function score(term: string, f: Record<(typeof FIELDS)[number], string>): number | null {
  const i = term.indexOf(":");
  const field = i < 0 ? "" : term.slice(0, i), value = i < 0 ? "" : term.slice(i + 1);
  if (i >= 0 && (FIELDS as readonly string[]).includes(field)) {
    const mine = f[field as keyof typeof f];
    if (value && mine.includes(value)) return 0;
    return field === "name" ? nameDistance(value, f.name) : null;
  }
  if (term.startsWith("@")) return term.length > 1 && term.slice(1) === f.tag ? 0 : null;
  if (FIELDS.some((k) => f[k].includes(term))) return 0;
  return i >= 0 ? null : nameDistance(term, f.name);
}

/** Every player matching all the terms, exact matches first, then by name. `score` > 0 means an approximate name. */
export function rankedPlayers(all: Found[], query = ""): { player: Found; score: number }[] {
  const terms = filterTerms(query), q = normalized(query);
  const out: { player: Found; score: number }[] = [];
  for (const player of all) {
    const f = Object.fromEntries(FIELDS.map((k) => [k, normalized(player[k])])) as Record<(typeof FIELDS)[number], string>;
    const scores = terms.map((t) => score(t, f));
    if (scores.every((s) => s !== null)) out.push({ player, score: (scores as number[]).reduce((a, b) => a + b, 0) });
  }
  const text = (p: Found) => [normalized(p.name), normalized(p.tag), p.node, p.port, p.id];
  return out.sort((a, b) => a.score - b.score || Number(normalized(a.player.name) !== q) - Number(normalized(b.player.name) !== q)
    || (text(a.player) < text(b.player) ? -1 : text(a.player) > text(b.player) ? 1 : 0));
}

/** Everyone on the stations that are connected. */
export function everyone(stations: { index: number; label: string; host: string; port: number; online: boolean; players: Dict[] }[],
  keyOf: (p: Dict) => string): Found[] {
  return stations.filter((s) => s.online).flatMap((s) => s.players.map((p): Found => ({
    index: s.index, key: keyOf(p), name: String(pick(p, "name") ?? "?"), tag: String(pick(p, "service_tag", "tag") ?? ""),
    clan: String(pick(p, "clan_tag", "clan") ?? ""), team: teamOf(p), id: String(pick(p, "player_id", "id") ?? ""),
    server: s.label, node: s.host, port: String(s.port),
  })));
}
