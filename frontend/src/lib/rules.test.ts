import { describe, expect, it } from "vitest";
import { Engine, MatchEvents, STARTERS, VIP_GUARD, cronDue, cronFields, fill, plan, sample, validate, zoned } from "./rules";

const rule = (o: Record<string, any>) => validate({ name: "t", ...o });
const lobby = (e: Engine, key = "s1") => {
  e.connection(key, true, "Server One");
  e.status(key, { players: 4, max_players: 16, map: "Guardian", mode: "Slayer", phase: "playing" });
  e.players(key, [{ engine_id: 1, name: "Ann", player_id: "a".repeat(64), team: "red", admin: true }, { engine_id: 2, name: "Bob", player_id: "b".repeat(64), team: "blue" }]);
};
const kill = (e: Engine, killer: number, victim: number, extra = {}) => e.event("s1", { event: "kill", killer, victim, weapon: "Sniper Rifle", ...extra });

describe("rules", () => {
  it("every starter validates and plans", () => {
    for (const s of STARTERS) {
      const r = validate({ ...s, then: s.then.map((a: any) => (a.type === "discord" ? { ...a, webhook: "hook" } : a)) });
      const e = new Engine([r]);
      lobby(e);
      expect(plan(e, sample(e, r, "s1")).length).toBeGreaterThan(0);
    }
  });

  it("rejects bad rules", () => {
    expect(() => rule({ when: { type: "nope" }, then: [{ type: "cancel_vote" }] })).toThrow(/Unknown WHEN/);
    expect(() => rule({ when: { type: "join" }, then: [{ type: "say", to: "this", message: "hi {nobody}" }] })).toThrow(/unknown placeholder/);
    expect(() => rule({ when: { type: "join" }, then: [] })).toThrow(/1 to 8/);
    expect(() => rule({ when: { type: "at", cron: "61 * * * *" }, then: [{ type: "cancel_vote" }] })).toThrow();
  });

  it("fills placeholders on one line", () => {
    expect(fill("{player} did {nope}ok", { player: "A\u0007\nB" })).toBe("AB did ok");
  });

  it("spree and multikill fire from the kill feed, once per count", () => {
    const e = new Engine([rule({ when: { type: "spree", counts: "2,3" }, then: [{ type: "say", to: "this", message: "{player} {medal}" }] }),
      rule({ when: { type: "multikill", min: 2 }, then: [{ type: "say", to: "this", message: "{medal}" }] })], {}, () => 100);
    lobby(e);
    expect(kill(e, 1, 2)).toHaveLength(0);
    const f = kill(e, 1, 2); // second kill inside the chain window
    expect(f.map((x) => x.rule.when.type).sort()).toEqual(["multikill", "spree"]);
    expect(plan(e, f.find((x) => x.rule.when.type === "spree")!)[0].args).toEqual(["say", "Ann Killing Spree"].map((x, i) => (i ? "Ann 2-kill spree" : x)));
  });

  it("a vote cancels for a VIP target, and the guard is off without a VIP", () => {
    const e = new Engine([VIP_GUARD()], { ["b".repeat(64)]: { name: "Bob", roles: ["vip"], servers: "all" } });
    lobby(e);
    const f = e.event("s1", { event: "vote", action: "start", vote_type: "kick", target: "Bob", target_id: "b".repeat(64) });
    expect(f).toHaveLength(1);
    expect(plan(e, f[0]).map((s) => s.args![0])).toEqual(["cancelvote", "say"]);
    expect(new Engine([VIP_GUARD()]).event("s1", { event: "vote", action: "start", vote_type: "kick", target: "Bob" })).toHaveLength(0);
  });

  it("cooldown and the hourly limit hold a rule back", () => {
    let t = 0;
    const e = new Engine([rule({ when: { type: "join" }, then: [{ type: "tell_player", message: "hi" }], cooldown: 10, per_hour: 2 })], {}, () => t);
    lobby(e);
    const join = (n: string, id: string) => e.event("s1", { event: "join", name: n, player_id: id }).length;
    expect([join("A", "1"), join("A", "1")]).toEqual([1, 0]); // same player inside the cooldown
    expect(join("B", "2")).toBe(1);
    expect(join("C", "3")).toBe(0); // third firing in an hour
    t = 4000;
    expect(join("C", "3")).toBe(1);
  });

  it("players_above fires on the crossing only", () => {
    const e = new Engine([rule({ when: { type: "players_above", count: 5 }, then: [{ type: "say", to: "this", message: "x" }] })]);
    lobby(e);
    expect(e.status("s1", { players: 5, phase: "playing" })).toHaveLength(1);
    expect(e.status("s1", { players: 6, phase: "playing" })).toHaveLength(0);
  });

  it("frame rate crossings", () => {
    const e = new Engine([rule({ when: { type: "fps_below", fps: 40 }, then: [{ type: "say", to: "this", message: "{fps}" }] }),
      rule({ when: { type: "fps_recovered", fps: 54 }, then: [{ type: "say", to: "this", message: "ok" }] })]);
    lobby(e);
    expect(e.frameRate("s1", 60)).toHaveLength(0);
    expect(e.frameRate("s1", 30)).toHaveLength(1);
    expect(e.frameRate("s1", 20)).toHaveLength(0);
    expect(e.frameRate("s1", 58).map((f) => f.rule.when.type)).toEqual(["fps_recovered"]);
  });

  it("server_down waits, then server_up reports once", () => {
    let t = 0;
    const e = new Engine([rule({ when: { type: "server_down", seconds: 30 }, then: [{ type: "discord", webhook: "h", message: "{down}" }] }),
      rule({ when: { type: "server_up" }, then: [{ type: "discord", webhook: "h", message: "up" }] })], {}, () => t);
    lobby(e);
    e.connection("s1", false);
    t = 10;
    expect(e.tick()).toHaveLength(0);
    t = 40;
    expect(e.tick()).toHaveLength(1);
    t = 50;
    expect(e.tick()).toHaveLength(0);
    expect(e.connection("s1", true)).toHaveLength(1);
  });

  it("ordinary chat never fires, !commands do", () => {
    const e = new Engine([rule({ when: { type: "command", command: "!admin" }, then: [{ type: "tell_player", message: "{args}" }] })]);
    lobby(e);
    expect(e.event("s1", { event: "chat", name: "Ann", text: "need an admin" })).toHaveLength(0);
    const f = e.event("s1", { event: "chat", name: "Ann", text: "!Admin cheater here" });
    expect(f).toHaveLength(1);
    expect(plan(e, f[0])[0].args).toEqual(["tell", "a".repeat(64), "cheater here"]);
  });

  it("match boundaries come from phase changes", () => {
    const m = new MatchEvents();
    expect(m.observe({ phase: "no_game" })).toEqual([]);
    expect(m.observe({ phase: "loading" })).toEqual(["before_match"]);
    expect(m.observe({ phase: "playing" })).toEqual(["match_start"]);
    expect(m.observe({ phase: "round_over" })).toEqual([]);
    expect(m.observe({ phase: "playing" })).toEqual([]);
    expect(m.observe({ phase: "finished" })).toEqual(["after_match"]);
  });

  it("cron: lists, ranges, steps and the day-field OR rule", () => {
    expect([...cronFields("*/15 9-10 * * 7")[1][0]]).toEqual([0, 15, 30, 45]);
    const z = (ms: number) => zoned(ms, "UTC");
    const fri20 = Date.UTC(2026, 9, 16, 20, 0); // Fri 2026-10-16
    expect(cronDue("0 20 * * 5", z(fri20))).toBe(true);
    expect(cronDue("0 20 * * 6", z(fri20))).toBe(false);
    expect(cronDue("0 20 1 * 5", z(fri20))).toBe(true); // either day field may match
    expect(z(Date.UTC(2026, 9, 18, 0, 0)).dow).toBe(0); // Sunday
  });
});
