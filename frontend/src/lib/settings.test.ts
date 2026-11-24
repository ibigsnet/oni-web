import { describe, expect, it } from "vitest";
import { filterEntries, looksLikeId, same, type Entry } from "./settings";

const entry = (key: string, group: string, label: string, description: string): Entry =>
  ({ key, group, label, description, type: "bool", applies: "live", scope: "defaults", default: null, advanced: false });

// Labels and descriptions as backend/oni_web/server_settings.py has them.
const ENTRIES = [
  entry("friendly_fire", "gameplay", "Friendly fire", "Off stops players from hurting their teammates and co-op partners. On follows each game type's own rules."),
  entry("max_players", "general", "Max players", "How many players the server takes at once. Co-op campaign and ODST servers take at most 16."),
  entry("film_recording", "anticheat", "Record films", "The server records every game as a film any player's Theater can play, kept in the server's folder."),
  entry("vote_kick_immune", "admins", "Can't be vote-kicked", "Player IDs no players' vote can kick, without giving them a role."),
];
const GROUPS: Record<string, string> = { gameplay: "Gameplay", general: "General", anticheat: "Anti-cheat, films & stats", admins: "Admins & roles" };
const find = (q: string) => filterEntries(q, ENTRIES, (g) => GROUPS[g]).map((e) => e.key);

describe("settings search", () => {
  it("is fuzzy over labels, descriptions, keys and groups", () => {
    expect(find("frendly")).toEqual(["friendly_fire"]);
    expect(find("teammates")).toEqual(["friendly_fire"]);
    expect(find("theater")).toEqual(["film_recording"]);
    expect(find("vote kick")).toContain("vote_kick_immune");
    expect(find("anti-cheat")).toEqual(["film_recording"]);
    expect(find("max_players")).toEqual(["max_players"]);
  });
  it("keeps everything for an empty search and nothing for nonsense", () => {
    expect(find("  ")).toHaveLength(ENTRIES.length);
    expect(find("zzqqxx")).toEqual([]);
  });
  it("knows a player ID by its 64 hex digits, in any case, trimmed", () => {
    const id = "0123456789abcdef".repeat(4);
    expect(looksLikeId(id)).toBe(true);
    expect(looksLikeId(` ${id.toUpperCase()} `)).toBe(true);
    expect(looksLikeId(id.slice(1))).toBe(false);
    expect(looksLikeId("Player One")).toBe(false);
    expect(looksLikeId("#3")).toBe(false);
  });
  it("never counts a half-typed number as a value", () => {
    expect(same(NaN, null)).toBe(false);
    expect(same(["a"], ["a"])).toBe(true);
    expect(same(undefined, null)).toBe(true);
  });
});
