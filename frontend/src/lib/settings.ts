// The Settings page's data, as the backend's schema (backend/oni_web/server_settings.py) sends it, and its search.
import * as fuzzy from "./fuzzy";

export type Entry = {
  key: string; group: string; label: string; description: string; type: string; applies: string; scope: string;
  default: unknown; advanced: boolean; choices?: [unknown, string][]; min?: number; max?: number; unit?: string;
  nullable?: boolean; null_label?: string; max_len?: number; max_items?: number; suggest?: "maps" | "modes";
  needs?: string; allow_other?: boolean;
};
export type Value = { key: string; value: unknown; inherited: unknown; source: string; is_set?: boolean; inherited_set?: boolean };
export type SettingsData = { groups: { id: string; label: string }[]; settings: Entry[]; values: Value[]; plays: string; server_name: string };

/** Rows the search keeps: every word must match the label, the description, the key or the group, typos allowed
 *  ("frendly" finds Friendly Fire). An empty search keeps them all. */
export function filterEntries(query: string, entries: Entry[], groupLabel: (id: string) => string): Entry[] {
  if (!fuzzy.terms(query).length) return entries;
  return entries.filter((e) => fuzzy.matches(query, e.label, e.description, e.key.replace(/[._]/g, " "), groupLabel(e.group)));
}

/** A player ID the game can match: 64 hex digits, the player's network key or community ID (any case). The game
 *  takes anything, so other text is saved, with a warning that it will never match anyone. */
export const looksLikeId = (id: string) => /^[0-9a-f]{64}$/i.test(id.trim());

const isNaNValue = (x: unknown) => typeof x === "number" && Number.isNaN(x);
export { isNaNValue };

/** The same value (a number still being typed is never the same as anything). */
export const same = (a: unknown, b: unknown) => !isNaNValue(a) && !isNaNValue(b) && JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
