// Carries out what the rules engine decides: the rcon commands, the Discord posts and the waits, with the limits that
// keep a misjudged rule from flooding a server. The console feeds it events; it hands every command to the IO it was
// given, so it runs the same under test. From ibigs/oni-rcon's rules_run.py, by IBIGS.
import { DISCORD_GAP, Engine, HISTORY, SAY_PER_MINUTE, VIP_GUARD, plan, stepWords, validate, type Firing, type Roles, type Step } from "./rules";
import type { Dict } from "./util";

export interface IO {
  /** Run `command` on the server called `server` (a station's `where`). null: not connected. */
  call(server: string, command: string, args: string[]): Promise<{ ok: boolean | null; data?: any } | null>;
  /** Post `text` to the webhook called `name`: the HTTP status. */
  discord(name: string, text: string): Promise<number>;
  changed(): void;
  /** Each finished firing, for the command log. */
  noted?(n: Note): void;
}

export interface Note { at: number; rule: string; name: string; server: string; result: string }

export class Runner {
  private epoch = 0;
  engine: Engine;
  history: Note[] = [];
  /** Webhook name -> its URL with the secret hidden: the window never holds the real ones. */
  webhooks: Record<string, string> = {};
  private said = new Map<string, number[]>();
  private discordAt = -Infinity;
  private discordLine: Promise<unknown> = Promise.resolve();

  constructor(private io: IO, private clock: () => number = () => Date.now() / 1000, private sleep = (s: number) => new Promise<void>((r) => setTimeout(r, s * 1000))) {
    this.engine = new Engine([], {}, clock);
  }

  get active() { return this.engine.rules.some((r) => r.enabled); }
  /** Invalidate delayed actions when this tab stops or loses the automation lease. */
  stop() { this.epoch++; this.engine = new Engine([], {}, this.clock); }
  /** The frame-rate triggers only matter when a rule uses them. */
  get wantsFps() { return this.engine.rules.some((r) => r.enabled && (["fps_below", "fps_recovered"].includes(r.when.type) || r.if.some((c: Dict) => c.type === "fps_under"))); }

  /** The saved rules and roles. While anyone has the VIP role, VIPs can't be vote-kicked. */
  load(rules: Dict[], roles: Roles) {
    const checked: Dict[] = [];
    for (const r of rules) {
      try {
        checked.push(validate(r));
      } catch (e) {
        this.note("", r?.id ?? "", "", `Rule "${r?.name ?? "?"}" skipped: ${e instanceof Error ? e.message : e}`);
      }
    }
    this.engine.rules = Object.values(roles).some((p) => p.roles.includes("vip")) ? [VIP_GUARD(), ...checked] : checked;
    this.engine.roles = roles;
    this.io.changed();
  }

  // inputs ---------------------------------------------------------------------------------------------------------
  event(server: string, ev: Dict) { this.run(this.engine.event(server, ev)); }
  connection(server: string, up: boolean, label?: string) { this.run(this.engine.connection(server, up, label)); }
  status(server: string, s: Dict) { this.run(this.engine.status(server, s)); }
  players(server: string, p: unknown) { this.engine.players(server, p); }
  fps(server: string, fps: number | null | undefined) { this.run(this.engine.frameRate(server, fps)); }
  tick() { this.run(this.engine.tick()); }

  private run(firings: Firing[]) {
    for (const f of firings) void this.steps(f, plan(this.engine, f));
  }

  /** A rule's steps in order. One failing never stops the others, or the console. */
  async steps(f: Firing, steps: Step[]) {
    const epoch = this.epoch;
    const results: string[] = [];
    for (const s of steps) {
      if (epoch !== this.epoch) return;
      try {
        results.push(await this.step(s, epoch));
      } catch (e) {
        results.push(`${stepWords(this.engine, s)}: failed (${e instanceof Error ? e.name : "error"})`);
      }
    }
    if (epoch === this.epoch) this.note(f.rule.id, f.server, results.join("; ") || "Nothing to do (nobody to tell).", f.rule.name);
  }

  private async step(s: Step, epoch: number): Promise<string> {
    const words = stepWords(this.engine, s);
    if (s.kind === "wait") {
      await this.sleep(s.seconds!);
      return words;
    }
    if (s.kind === "discord") {
      if (!this.webhooks[s.webhook!]) return `${words}: no webhook called ${s.webhook}`;
      const code = (await (this.discordLine = this.discordLine.then(async () => {
        const wait = DISCORD_GAP - (this.clock() - this.discordAt);
        if (wait > 0) await this.sleep(wait);
        if (epoch !== this.epoch) return 0;
        this.discordAt = this.clock();
        return this.io.discord(s.webhook!, s.text!).catch(() => 0);
      }))) as number;
      return words + (code >= 200 && code < 300 ? ": done" : code ? `: Discord answered ${code}` : ": Discord could not be reached");
    }
    const [cmd, ...rest] = s.args!;
    if (cmd === "say" || cmd === "tell") {
      const now = this.clock(), window = (this.said.get(s.server!) ?? []).filter((t) => now - t <= 60);
      this.said.set(s.server!, window);
      if (window.length >= SAY_PER_MINUTE) return `${words}: skipped, ${SAY_PER_MINUTE} messages a minute is the limit`;
      window.push(now);
    }
    if (cmd === "shuffle") {
      const fresh = await this.io.call(s.server!, "status", []);
      if (epoch !== this.epoch) return `${words}: stopped`;
      if (!["playing", "in_game"].includes(fresh?.data?.phase)) return `${words}: skipped, no match is being played`;
    }
    const reply = await this.io.call(s.server!, cmd, rest);
    if (!reply) return `${words}: skipped, server offline`;
    return words + (reply.ok ? ": done" : ": the server refused");
  }

  note(rule: string, server: string, result: string, name = "") {
    const n = { at: this.clock(), rule, name, server: this.engine.name(server), result: result.slice(0, 600) };
    this.history.push(n);
    if (this.history.length > HISTORY) this.history.splice(0, this.history.length - HISTORY);
    this.io.noted?.(n);
    this.io.changed();
  }
}
