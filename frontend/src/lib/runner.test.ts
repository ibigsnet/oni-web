import { describe, expect, it } from "vitest";
import { Runner } from "./runner";
import { validate } from "./rules";

const setup = (phase = "playing") => {
  const calls: string[] = [], posts: string[] = [];
  const r = new Runner({
    call: async (_s, c, a) => { calls.push([c, ...a].join(" ")); return { ok: true, data: { phase } }; },
    discord: async (n, t) => { posts.push(`${n}:${t}`); return 204; },
    changed() {},
  }, () => 0, async () => {});
  r.webhooks = { hook: "https://discord.com/api/webhooks/1/••••••" };
  r.engine.connection("s", true, "One");
  return { r, calls, posts };
};
const rule = (then: object[], when: object = { type: "join" }) => validate({ name: "t", when, then, cooldown: 0, per_hour: 600 });

describe("runner", () => {
  it("says at most 20 a minute per server, and shuffles only mid-match", async () => {
    const a = setup();
    a.r.load([rule([{ type: "say", to: "this", message: "hi" }])], {});
    for (let i = 0; i < 25; i++) a.r.event("s", { event: "join", name: "A", player_id: String(i) });
    await new Promise((r) => setTimeout(r, 10));
    expect(a.calls.filter((c) => c.startsWith("say")).length).toBe(20);

    for (const [phase, runs] of [["playing", 1], ["lobby", 0]] as const) {
      const b = setup(phase);
      b.r.load([rule([{ type: "shuffle" }])], {});
      b.r.event("s", { event: "join", name: "A", player_id: "1" });
      await new Promise((r) => setTimeout(r, 10));
      expect(b.calls.filter((c) => c === "shuffle").length).toBe(runs);
    }
  });

  it("posts to Discord by name, and notes an unknown webhook", async () => {
    const a = setup();
    a.r.load([rule([{ type: "discord", webhook: "hook", message: "hello" }]), rule([{ type: "discord", webhook: "nope", message: "x" }])], {});
    a.r.event("s", { event: "join", name: "A", player_id: "1" });
    await new Promise((r) => setTimeout(r, 10));
    expect(a.posts).toEqual(["hook:hello"]);
    expect(a.r.history.some((n) => /no webhook called nope/.test(n.result))).toBe(true);
  });
});
