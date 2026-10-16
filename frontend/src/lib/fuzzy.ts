// Typo-tolerant search shared by every search box (IBIGS 2026-10-16; same rules as oni-rcon's fuzzy.py).
// Every term must match: as a substring, or a word within one slip (4-7 letters) or two (8+), a swap counting as
// one, so "gluch" finds "Gulch". A term also matches the start of a longer word ("valhal"). Terms under 4 letters
// must match exactly.

export const norm = (v: unknown) => String(v ?? "").normalize("NFKC").toLowerCase();

export function terms(query: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  for (const m of norm(query).matchAll(re)) {
    const t = (m[1] ?? m[2] ?? m[3] ?? "").replace(/["']/g, "").trim();
    if (t) out.push(t);
  }
  return out;
}

const limitFor = (t: string) => (t.length < 4 ? 0 : t.length < 8 ? 1 : 2);
const memo = new Map<string, number>();

export function distance(a: string, b: string, limit: number): number {
  if (Math.abs(a.length - b.length) > limit) return limit + 1;
  const k = `${a}\u0000${b}\u0000${limit}`;
  const hit = memo.get(k);
  if (hit !== undefined) return hit;
  let prevprev: number[] = [], prev = Array.from({ length: b.length + 1 }, (_, j) => j), out = -1;
  for (let i = 1; i <= a.length && out < 0; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) {
      let cost = Math.min(row[j - 1] + 1, prev[j] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) cost = Math.min(cost, prevprev[j - 2] + 1);
      row.push(cost);
    }
    if (Math.min(...row) > limit) out = limit + 1;
    [prevprev, prev] = [prev, row];
  }
  if (out < 0) out = prev[b.length];
  if (memo.size > 16384) memo.clear();
  memo.set(k, out);
  return out;
}

/** 0 = substring, 1-2 = slips, null = no match. `text` must already be norm()ed. */
export function termScore(term: string, text: string): number | null {
  if (text.includes(term)) return 0;
  const limit = limitFor(term);
  if (!limit) return null;
  const words = text.match(/[\p{L}\p{N}_]+/gu) ?? [];
  const cands = new Set([...words, ...words.slice(1).map((w, i) => words[i] + w)]);
  let best = limit + 1;
  for (const w of cands) {
    best = Math.min(best, distance(term, w, limit));
    if (w.length > term.length) best = Math.min(best, distance(term, w.slice(0, term.length), limit));
    if (best === 1 && limit === 1) break;
  }
  return best <= limit ? best : null;
}

export function score(query: string, ...texts: unknown[]): number | null {
  const hay = norm(texts.map((t) => String(t ?? "")).join(" "));
  let total = 0;
  for (const t of terms(query)) {
    const s = termScore(t, hay);
    if (s === null) return null;
    total += s;
  }
  return total;
}

export const matches = (query: string, ...texts: unknown[]) => score(query, ...texts) !== null;

/** Matching items, closest first (stable among equals). */
export function rank<T>(query: string, items: T[], textOf: (x: T) => unknown[]): T[] {
  return items.map((x, i) => ({ x, i, s: score(query, ...textOf(x)) }))
    .filter((r) => r.s !== null).sort((a, b) => (a.s as number) - (b.s as number) || a.i - b.i).map((r) => r.x);
}
