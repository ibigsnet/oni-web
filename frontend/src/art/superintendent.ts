// The Superintendent: New Mombasa's municipal AI, a small green face that lives in the sidebar on every tab, watches
// what comes in from every station, and reacts to what an admin would care about. The app feeds it events (react) and
// asks it, every frame, what face to wear (look). Plain functions and one small state machine, so it's testable.
import { AMBER, CYAN, GREEN, GREY, RED, noise } from "./palette";
import { BASE, type Expr } from "./archive";
import { chatRequest } from "../lib/findings";

const EASE = 0.28; // seconds an expression takes to settle after an event
const DEBOUNCE = 1.2; // seconds a reaction holds against another of the same weight: a busy fleet doesn't make it flicker
const BLINK_FOR = 0.22;
const BLINK_EVERY = 5.5;

export interface Reaction {
  name: string;
  mood: string;
  color: string;
  expr: Partial<Expr>;
  hold: number;
  prio: number;
  alert?: boolean;
  wink?: boolean;
}

export const REACTIONS: Record<string, Reaction> = {
  review: { name: "review", mood: "REVIEW NEEDED", color: AMBER, expr: { size: 1.18, ex: 0 }, hold: 8.0, prio: 3 },
  welcome: { name: "welcome", mood: "WELCOMING", color: GREEN, expr: { happy: true }, hold: 4.0, prio: 1 },
  call: { name: "call", mood: "ALARMED", color: RED, expr: { size: 1.28, ey: -1.5, ex: 0 }, hold: 6.0, prio: 3, alert: true },
  cheat: { name: "cheat", mood: "HOSTILE", color: RED, expr: { tilt: 0.6, lid: 0.1 }, hold: 6.0, prio: 3, alert: true },
  satisfied: { name: "satisfied", mood: "SATISFIED", color: GREEN, expr: { happy: true }, hold: 4.5, prio: 2 },
  medal: { name: "medal", mood: "IMPRESSED", color: AMBER, expr: { size: 1.14 }, hold: 4.0, prio: 2 },
  unimpressed: { name: "unimpressed", mood: "UNIMPRESSED", color: GREY, expr: { lid: 0.48, ex: 1.0 }, hold: 4.0, prio: 1 },
  cheer: { name: "cheer", mood: "CHEERFUL", color: GREEN, expr: {}, hold: 3.5, prio: 1, wink: true },
};
export const IDLE: Reaction = { name: "idle", mood: "WATCHING", color: CYAN, expr: {}, hold: 0, prio: 0 };

/** Which reaction an event earns, or null for the many that earn none. A chat line the findings have already judged
 *  carries `_review_alert`; a kill in a mode where streaks mean little carries `_quiet_kills`. */
export function classify(ev: Record<string, unknown>): string | null {
  const kind = ev.event;
  if (kind === "chat") {
    if (ev.channel === "server") return "cheer";
    if ("_review_alert" in ev) return ev._review_alert ? "review" : null;
    return chatRequest(ev.text) ? "call" : null;
  }
  if (kind === "kill") return (ev._medals as unknown[] | undefined)?.length && !ev._quiet_kills ? "medal" : null;
  return ({ join: "welcome", cheat: "cheat", kick: "satisfied", ban: "satisfied", mute: "unimpressed" } as Record<string, string>)[String(kind)] ?? null;
}

const easeOut = (p: number) => 1 - (1 - Math.max(0, Math.min(1, p))) ** 3;
function easePop(p: number) {
  p = Math.max(0, Math.min(1, p));
  return 1 + 2.70158 * (p - 1) ** 3 + 1.70158 * (p - 1) ** 2;
}

export class Superintendent {
  current: Reaction = IDLE;
  before: Reaction = IDLE;
  since = -Infinity;
  last = ""; // a few words on what it reacted to
  constructor(public motion = true, public now: () => number = () => performance.now() / 1000) {}

  /** Take an event. Returns the reaction it earned if the face took it up. */
  react(ev: Record<string, unknown>, subject = ""): Reaction | null {
    const key = classify(ev);
    if (!key) return null;
    const next = REACTIONS[key], t = this.now();
    if (this.held(t) && (next.prio < this.current.prio || (next.prio === this.current.prio && t - this.since < DEBOUNCE))) return null;
    this.before = this.lookAt(t)[0];
    this.current = next;
    this.since = t;
    this.last = subject || this.last;
    return next;
  }

  /** A finding: the face wears REVIEW NEEDED for a review, IMPRESSED for a highlight. */
  report(subject: string, level: string) {
    const t = this.now();
    this.before = this.lookAt(t)[0];
    this.current = REACTIONS[level === "review" ? "review" : "medal"];
    this.since = t;
    this.last = subject;
  }

  clear() {
    this.current = this.before = IDLE;
    this.since = -Infinity;
    this.last = "";
  }

  held(t: number) {
    return t - this.since < this.current.hold;
  }

  /** 0 to 1: how closed the eyes are, a quick dip once in each stretch of BLINK_EVERY seconds. */
  blinking(t: number) {
    if (!this.motion) return 0;
    const k = Math.floor(t / BLINK_EVERY);
    const at = k * BLINK_EVERY + 0.4 + (BLINK_EVERY - BLINK_FOR - 0.8) * noise(k % 100000, 41);
    return Math.max(0, 1 - Math.abs(t - at - BLINK_FOR / 2) / (BLINK_FOR / 2));
  }

  /** [the reaction in force, the face for it]. An expression eases in from the one before it; once its hold is over
   *  the face eases back to idle. */
  lookAt(t: number): [Reaction, Expr] {
    const cur = this.held(t) ? this.current : IDLE;
    const start = cur === this.current ? this.since : this.since + this.current.hold;
    const p = this.motion ? (t - start) / EASE : 1;
    const into = { ...BASE, ...cur.expr } as Expr;
    const out = { ...BASE, ...(cur === this.current ? this.before.expr : this.current.expr) } as Expr;
    const mix = easeOut(p);
    const e = { ...into } as Expr;
    for (const k of ["size", "ex", "ey", "lid", "tilt", "openL", "openR", "alarm"] as const) {
      e[k] = out[k] + (into[k] - out[k]) * (k === "size" ? easePop(p) : mix);
    }
    e.happy = mix > 0.5 ? into.happy : out.happy;
    const blink = e.happy ? 0 : this.blinking(t);
    const wink = cur.wink && t - start >= 0 && t - start < 0.5 ? 1 - Math.abs(2 * ((t - start) / 0.5) - 1) : 0;
    e.openL = 1 - blink;
    e.openR = Math.max(0.08, 1 - blink - 0.92 * Math.max(0, wink));
    if (cur.alert) e.alarm = this.motion ? 0.55 + 0.45 * Math.cos((t - start) * 9) : 1;
    return [cur, e];
  }

  /** [the reaction, the face, a key that changes only when the picture does]. */
  look(): [Reaction, Expr, string] {
    const [cur, e] = this.lookAt(this.now());
    const key = Object.values(e).map((v) => (typeof v === "number" ? v.toFixed(2) : v)).join("|") + cur.mood + this.last;
    return [cur, e, key];
  }
}
