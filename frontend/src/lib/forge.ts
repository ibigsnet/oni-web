// Reading ReclaimerForge listings for display. The key and every request stay in the backend; these only read the
// listings it hands over, tolerantly, as the terminal console's forge.py did.
import { pick, num, tidy, type Dict } from "./util";

export const SORTS = ["trending", "rising", "latest", "updated", "downloads", "rated", "unrated", "overlooked"];
export const VIEWS = [...SORTS, "favourites", "installed", "mine"];
export const WINDOWS = ["24h", "7d", "30d"];
export const WINDOWED = new Set(["trending", "rising"]);
const WINDOW_SECONDS: Record<string, number> = { "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400 };
export const WITHDRAWN = new Set(["withdrawn", "removed", "unlisted", "taken_down", "takedown", "deleted"]);
const UNPUBLISHED = new Set(["draft", "pending", "unpublished"]);
export const SITE = "RECLAIMERFORGE.NET";
export const CREDIT = "ReclaimerForge is built and kept running by one of the Reclaimer community, on their own time, and they signed off on this link-up. Every map, gametype and playlist in F6 is theirs to host and ours to play. Section Three is in their debt.";

const str = (v: unknown) => (v === undefined || v === null ? "" : String(v));
export const listingId = (x: Dict) => str(pick(x, "id", "listing_id"));
export const versionId = (x: unknown) => (x && typeof x === "object" ? str(pick(x, "id", "version_id")) : str(x));
export const versionLabel = (x: unknown) => (x && typeof x === "object" ? tidy(pick(x, "version_label", "version", "label", "name", "number") ?? versionId(x)) : "");
export const titleOf = (x: Dict) => tidy(pick(x, "title", "name")) || "untitled";
export const notesOf = (v: Dict) => tidy(pick(v, "release_notes", "notes", "changelog", "summary") ?? "");
export const blurbOf = (x: Dict) => tidy(pick(x, "description", "summary", "short_description") ?? "", true);
export const shorten = (s: string, n: number) => (s.length <= n ? s : s.slice(0, n - 1).trimEnd() + "…");

export function kindOf(x: Dict): string {
  const k = str(pick(x, "kind", "type", "category", "content_type")).toLowerCase();
  return ({ game_type: "gametype", "game type": "gametype", variant: "gametype", map_variant: "map" } as Dict)[k] ?? k;
}

function authorsOf(x: Dict) {
  const a = x?.authors;
  if (!Array.isArray(a)) return [];
  return a.filter((e) => e && typeof e === "object")
    .map((e) => ({ user_id: str(pick(e, "user_id", "id")), username: tidy(pick(e, "username", "display_name", "name") ?? ""), is_owner: e.is_owner === true }))
    .filter((e) => e.username || e.user_id)
    .sort((a, b) => Number(b.is_owner) - Number(a.is_owner));
}
export const authorCount = (x: Dict) => authorsOf(x).length;

export function ownerOf(x: Dict): string {
  const o = pick(x, "owner_id", "author_id", "creator_id") ?? (x.owner && typeof x.owner === "object" ? pick(x.owner, "id", "owner_id") : undefined);
  return str(o ?? authorsOf(x).find((e) => e.is_owner && e.user_id)?.user_id);
}

export function authorOf(x: Dict): string {
  const names = authorsOf(x).map((e) => e.username).filter(Boolean);
  if (names.length) return names.join(", ");
  let a = pick(x, "author", "owner_name", "owner_display_name", "author_name", "creator");
  if (a && typeof a === "object") a = pick(a, "display_name", "name", "username", "handle");
  if (!a && x.owner && typeof x.owner === "object") a = pick(x.owner, "display_name", "name", "username", "handle");
  return tidy(a ?? "");
}

export const isWithdrawn = (x: Dict) =>
  WITHDRAWN.has(str(pick(x, "status", "state")).toLowerCase()) || pick(x, "withdrawn") === true || !!pick(x, "withdrawn_at");
export const usable = (v: unknown) =>
  !!v && typeof v === "object" && !isWithdrawn(v as Dict) && !UNPUBLISHED.has(str(pick(v, "status", "state")).toLowerCase());
export function versionsOf(x: Dict): Dict[] {
  const v = pick(x, "versions", "releases");
  return Array.isArray(v) ? v.filter((e) => e && typeof e === "object") : [];
}
export const whenOf = (v: Dict) => {
  const s = pick(v, "published_at", "created_at", "released_at");
  const t = s ? Date.parse(String(s)) : NaN;
  return isNaN(t) ? null : t;
};

export function latestOf(x: Dict): Dict | null {
  const gone = new Set(versionsOf(x).filter((v) => !usable(v)).map(versionId));
  const lv = pick(x, "latest_version", "current_version", "latest_release", "latest");
  if (lv && typeof lv === "object" && usable(lv) && !gone.has(versionId(lv))) return lv;
  if (typeof lv === "string" && !gone.has(lv)) return { id: lv };
  const vs = versionsOf(x).filter(usable);
  if (vs.length && vs.every((v) => whenOf(v) !== null)) return vs.reduce((a, b) => (whenOf(b)! > whenOf(a)! ? b : a));
  return vs[0] ?? null;
}

export function votesOf(x: Dict): [number | undefined, number | undefined, number | undefined] {
  let up = num(pick(x, "upvote_count")), down = num(pick(x, "downvote_count"));
  let total = num(pick(x, "vote_count"));
  if (up === undefined && down === undefined) {
    const r = pick(x, "ratings", "rating_counts", "votes");
    if (r && typeof r === "object" && !Array.isArray(r)) {
      up = num(pick(r, "up", "positive", "likes", "thumbs_up"));
      down = num(pick(r, "down", "negative", "dislikes", "thumbs_down"));
    }
  }
  if (total === undefined && (up !== undefined || down !== undefined)) total = (up ?? 0) + (down ?? 0);
  return [up, down, total];
}

export const metricsOf = (x: Dict): Dict => (x?.metrics && typeof x.metrics === "object" ? x.metrics : {});

export function recentOf(x: Dict, window = ""): number | undefined {
  for (const src of [x, metricsOf(x)]) {
    let r = pick(src, "recent_downloads", "downloads_recent", "window_downloads", "downloads_window", `recent_downloads_${window}`, `downloads_${window}`);
    if (r && typeof r === "object") r = pick(r, window, "7d", "30d", "24h");
    if (typeof r === "number") return r;
  }
  return undefined;
}

export function viewsOf(x: Dict): number | undefined {
  for (const src of [x, metricsOf(x)]) {
    const n = pick(src, "view_count", "views");
    if (typeof n === "number") return n;
  }
  return undefined;
}

export function historySince(x: Dict, window = ""): Date | null {
  const s = pick(metricsOf(x), "download_history_started_at");
  const began = s ? new Date(String(s)) : null;
  const span = WINDOW_SECONDS[window];
  return began && !isNaN(+began) && span && +began > Date.now() - span * 1000 ? began : null;
}

const COMPAT = ["compatibility", "compatible_versions", "reclaimer_versions", "compatible_with", "requires", "game_version"];
export function compatOf(x: Dict): unknown {
  const spec = pick(x, ...COMPAT);
  if (spec !== undefined) return spec;
  const v = latestOf(x);
  return v ? pick(v, ...COMPAT) : undefined;
}

const versionTuple = (v: unknown) => (String(v).split("-")[0].split("+")[0].match(/\d+/g) ?? []).slice(0, 3).map(Number);
const cmp = (a: number[], b: number[]) => {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (i >= a.length) return -1;
    if (i >= b.length) return 1;
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
};
const pad3 = (t: number[]) => [...t, 0, 0].slice(0, 3);

/** Whether a listing says it runs on a server of this version: null when either side doesn't say. */
export function compatible(spec: unknown, server: unknown): boolean | null {
  const have = server ? versionTuple(server) : [];
  if (!have.length || spec === undefined || spec === null || spec === "" || (Array.isArray(spec) && !spec.length)) return null;
  if (Array.isArray(spec)) {
    const said = spec.map((s) => compatible(s, server));
    return said.some((s) => s) ? true : said.every((s) => s === null) ? null : false;
  }
  if (typeof spec === "object") {
    if (!Object.keys(spec as Dict).length) return null;
    const inner = pick(spec, "reclaimer", "server", "dedicated", "versions", "range");
    if (inner !== undefined) return compatible(inner, server);
    const lo = pick(spec, "min", "from", "minimum"), hi = pick(spec, "max", "to", "maximum");
    if (lo === undefined && hi === undefined) return null;
    return (lo === undefined || cmp(have, versionTuple(lo)) >= 0) && (hi === undefined || cmp(have.slice(0, versionTuple(hi).length), versionTuple(hi)) <= 0);
  }
  let ok = true;
  for (const term of String(spec).split(/[,\s]+/).filter(Boolean)) {
    if (term === "x" || term === "*") continue;
    const m = term.match(/^(>=|<=|>|<|==|=|\^|~)?v?([\d.]*\d)(?:\.[x*])?$/);
    if (!m) return null;
    const want = versionTuple(m[2]);
    const part = have.slice(0, want.length), h = pad3(have), w = pad3(want);
    const r = ({ ">=": cmp(h, w) >= 0, ">": cmp(h, w) > 0, "<=": cmp(part, want) <= 0, "<": cmp(h, w) < 0 } as Record<string, boolean>)[m[1] ?? ""];
    ok &&= r ?? cmp(part, want) === 0;
  }
  return ok;
}

export function compatWords(spec: unknown): string {
  if (Array.isArray(spec)) return spec.map(String).join(", ");
  if (spec && typeof spec === "object") return Object.entries(spec).map(([k, v]) => `${k} ${v}`).join(" ");
  return spec !== undefined && spec !== null && spec !== "" ? String(spec) : "not stated";
}

/** A name as servers and catalogs both might spell it. */
export const norm = (s: unknown) => String(s ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

/** Every name an installed listing might go by on a server. */
export function refsOf(e: Dict): Set<string> {
  const names = [e.reference, e.title, ...(e.files ?? []).map((f: Dict) => String(f.path ?? "").split("/").pop()!.replace(/\.[^.]*$/, ""))];
  return new Set(names.filter(Boolean).map(norm).filter(Boolean));
}

export const day = (ts: unknown) => String(ts ?? "").slice(0, 10) || "—";
