// Superintendent findings: bounded, evidence-based observations that are never an input to moderation. From
// ibigs/oni-rcon's findings.py, by IBIGS. Event time, not arrival time, drives the windows; a gap in the feed resets
// the pattern state, not the report history. Plain functions and one class, so it is testable without the window.
import type { Dict } from "./util";

export const HISTORY = 200;
export const RETENTION = 3600;
export const VOTE_WINDOW = 300;
export const CHAT_REPEAT = 120;
export const CHAT_BEFORE = 120;
export const CHAT_AFTER = 120;
export const CHAT_LIMIT = 20;
const FAST_MODES = /\b(?:infection|infected|swat|fat[ _-]?kid)\b/i;

/** Text that can travel with a finding: no control characters, API keys blanked, one line, at most `limit` long. */
export function words(value: unknown, limit = 240): string {
  const text = String(value || "").replace(/\p{C}/gu, "").replace(/\brfk_[A-Za-z0-9_-]+/g, "[API KEY]");
  return text.split(/\s+/).filter(Boolean).join(" ").slice(0, limit);
}

/** SWAT often reports its engine mode as Slayer, so the lobby's name and tags count too. */
export function quietKillMode(context: Dict = {}): boolean {
  const tags = context.tags ?? [];
  return FAST_MODES.test([context.mode, context.name, ...(Array.isArray(tags) ? tags : [tags])].map((v) => String(v ?? "")).join(" "));
}

export interface ChatRequest { kind: "admin_help" | "moderation_request" | "player_report"; topic: string; target?: string }

const strip = (s: string, chars: string) => {
  let a = 0, b = s.length;
  while (a < b && chars.includes(s[a])) a++;
  while (b > a && chars.includes(s[b - 1])) b--;
  return s.slice(a, b);
};
const ROLE = "(?:admins?|moderators?|mods?)";
const BAD = "(?:hack(?:ing|er|s)?|cheat(?:ing|er|s)?|aimbot(?:ting|ter)?|wallhack(?:ing)?|speedhack(?:ing)?)";
const NEGATED = /\b(?:don't|dont|do not|doesn't|isn't|aren't|not|never|stop|no need)\b/;
const ROLE_ALONE = new RegExp(`^(?:hey |please |pls |plz )?${ROLE}(?: please| pls| plz)?$`);
const NEED_ROLE = /\b(?:need|want|looking for|paging|calling) (?:an? |the )?(?:admin|moderator)s?\b/;
const ANY_ROLE = new RegExp(`\\b(?:any|is there an?|are there any) ${ROLE}(?:\\s+(?:online|around|here|available))?$`);
const ROLE_HERE = new RegExp(`\\b${ROLE}\\s+(?:online|around|here|available|help)\\b`);
const NEED_MOD = /^(?:i |we )?need a mod(?: please| pls| plz)?$/;
const NEED_HELP = /\b(?:need|looking for|could use) (?:some )?(?:help|assistance)\b/;
const HELP_ALONE = /^(?:please |pls |plz )?help(?: me| us)?(?: please| pls| plz)?$/;
const CAN_HELP = /\b(?:can|could|would) (?:someone|anyone|you|an admin|a mod) (?:please )?help\b/;
const REQUEST = new RegExp(`^(?:${ROLE}[, :]+)?(?:(?:can|could|would) (?:someone|anyone|you|we|an admin|a mod) (?:please )?|(?:please|pls|plz) )?(?:vote[ -]?)?(kick|ban|mute)\\s+(.+)`);
const NOT_A_TARGET = /^(?:is|was|are|the (?:command|button|option|system|feature)|command|button)\b/;
const ALLEGATION = new RegExp(`^(.{1,80}?)\\s+(?:is|are|keeps|was|has been)\\s+(?:(?:clearly|obviously|definitely|a|an)\\s+)*(?:${BAD}|using (?:an? )?(?:aimbot|wallhack))\\b`);
const REMOVAL = /^(.{1,80}?)\s+(?:should|needs? to) be (?:kicked|banned|muted)\b/;
const THERE_IS = /\b(?:there(?:'s| is)|we (?:have|got)) (?:a |an )?(?:hacker|cheater|aimbotter)\b/;

/** Explicit requests and allegations in chat, not isolated game-mod or cheat words. A conservative English-language
 *  triage rule, never a verdict. A topic groups repeat requests from different speakers on one server. */
export function chatRequest(value: unknown): ChatRequest | null {
  const text = words(value, 700).normalize("NFKC").toLowerCase().replace(/’/g, "'");
  let help: ChatRequest | null = null;
  for (let clause of text.split(/[.!?;]+\s*|\bbut\b/)) {
    clause = strip(clause, " ,:");
    if (!clause) continue;
    if (NEGATED.test(clause)) continue; // denials and instructions about commands are not reports
    if (ROLE_ALONE.test(clause) || NEED_ROLE.test(clause) || ANY_ROLE.test(clause) || ROLE_HERE.test(clause) || NEED_MOD.test(clause))
      help = { kind: "admin_help", topic: "admin" };
    if (NEED_HELP.test(clause) || HELP_ALONE.test(clause) || CAN_HELP.test(clause)) help = help ?? { kind: "admin_help", topic: "help" };
    const request = REQUEST.exec(clause);
    if (request) {
      const target = strip(request[2].split(/\b(?:because|for|please|pls|plz)\b/)[0], " ,:");
      if (target && !NOT_A_TARGET.test(target)) return { kind: "moderation_request", topic: "player:" + target.slice(0, 80), target: target.slice(0, 80) };
    }
    const allegation = ALLEGATION.exec(clause);
    if (allegation) {
      const target = allegation[1].trim().slice(0, 80);
      return { kind: "player_report", topic: "player:" + target, target };
    }
    const removal = REMOVAL.exec(clause);
    if (removal) {
      const target = removal[1].trim().slice(0, 80);
      return { kind: "moderation_request", topic: "player:" + target, target };
    }
    if (THERE_IS.test(clause)) return { kind: "player_report", topic: "unidentified-player" };
  }
  return help;
}

export interface Who { id?: string; player_id?: string; engine_id?: string; name?: string }
export interface Subject { id: string; name: string }

/** Only public identity fields can travel with a finding; never addresses or fingerprints. */
export function publicRoster(players: unknown): Who[] {
  const out: Who[] = [];
  for (const player of (Array.isArray(players) ? players : []).slice(0, 64)) {
    if (!player || typeof player !== "object") continue;
    const guests = (player as Dict).guest_players;
    for (const p of [player as Dict, ...(Array.isArray(guests) ? guests.slice(0, 4) : [])]) {
      if (p && typeof p === "object") out.push({ id: words(p.id, 128), player_id: words(p.player_id, 128), engine_id: words(p.engine_id, 128), name: words(p.name, 128) });
    }
  }
  return out;
}

/** Who a slot or name is, at event time. Navigation later revalidates the full public ID. */
export function subject(value: unknown, names: (id: string) => string | undefined = () => undefined, roster: Who[] = []): Subject {
  let ident: string, name: string;
  if (value && typeof value === "object") {
    const v = value as Dict;
    ident = words(v.player_id || v.id, 128);
    name = words(v.name || names(ident), 80);
  } else {
    ident = words(value, 128);
    name = words(names(ident) ?? value, 80);
  }
  let matches = roster.filter((p) => ident && [p.id, p.player_id, p.engine_id].includes(ident));
  if (!matches.length && name) matches = roster.filter((p) => words(p.name).toLowerCase() === name.toLowerCase());
  if (matches.length === 1) return { id: matches[0].player_id || matches[0].id || "", name: matches[0].name || name };
  // Do not retain reusable engine slots, a guessed ID, or select duplicate names.
  const stable = /^[0-9a-fA-F]{32,128}$/.test(ident) && !matches.length ? ident : "";
  return { id: stable, name: name || "Unknown player" };
}

export interface ChatLine { at: number; speaker: Subject; channel: string; text: string; position?: "before" | "trigger" | "after" }
export interface ChatContext {
  messages: ChatLine[]; since: number; until: number; before_limited: boolean; revision: number;
  status: "collecting" | "complete" | "limited" | "interrupted";
}
export interface Finding {
  id: string; server: string; at: number; kind: string; level: "review" | "highlight"; title: string; evidence: string;
  source: string; subjects: Subject[]; reporter: Subject | null; chat: ChatContext | null;
}

/** A short hash of a string, 24 hex digits: two 53-bit hashes (cyrb53) with different seeds. */
function hash24(s: string): string {
  const one = (seed: number) => {
    let a = 0xdeadbeef ^ seed, b = 0x41c6ce57 ^ seed;
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      a = Math.imul(a ^ c, 2654435761);
      b = Math.imul(b ^ c, 1597334677);
    }
    a = Math.imul(a ^ (a >>> 16), 2246822507) ^ Math.imul(b ^ (b >>> 13), 3266489909);
    b = Math.imul(b ^ (b >>> 16), 2246822507) ^ Math.imul(a ^ (a >>> 13), 3266489909);
    return (4294967296 * (2097151 & b) + (a >>> 0)).toString(16).padStart(14, "0");
  };
  return (one(0) + one(0x9e3779b9)).slice(0, 24);
}
const stable = (v: unknown): string =>
  Array.isArray(v) ? `[${v.map(stable).join(",")}]` : v && typeof v === "object"
    ? `{${Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + stable((v as Dict)[k])).join(",")}}` : JSON.stringify(v) ?? "null";
const key = (...parts: unknown[]) => JSON.stringify(parts);

interface Observations {
  votes: [number, string, string, string, string][]; // when, caller, target, and their names
  kills: [number, string, string][]; // when, killer, victim
  recent: Map<string, number>; // event signatures: a relay that repeats an event is not two events
  cooldowns: Map<string, number>;
  last: number;
  combat: string;
  chat: ChatLine[];
  chatSince: number | null;
}
const fresh = (cooldowns = new Map<string, number>()): Observations =>
  ({ votes: [], kills: [], recent: new Map(), cooldowns, last: 0, combat: "", chat: [], chatSince: null });
const drop = <T>(m: Map<string, T>) => m.delete(m.keys().next().value!);

export class Findings {
  history: Finding[] = [];
  states = new Map<string, Observations>();
  acknowledgedAt = 0;
  constructor(public now: () => number = () => Date.now() / 1000) {}

  /** A round change or a reconnect breaks evidence windows, not alert suppression. */
  reset(server: string, keepChat = false) {
    const old = this.states.get(server);
    if (!old) return;
    const next = fresh(old.cooldowns);
    this.states.set(server, next);
    if (keepChat) Object.assign(next, { chat: old.chat, chatSince: old.chatSince, last: old.last, recent: old.recent });
    else this.interruptChat(server);
  }

  /** Never join conversations across an unobserved connection or process gap. */
  interruptChat(server?: string) {
    this.history = this.history.map((r) => r.chat && r.chat.status === "collecting" && (server === undefined || r.server === server)
      ? { ...r, chat: { ...r.chat, status: this.now() >= r.chat.until ? "complete" : "interrupted", revision: r.chat.revision + 1 } } : r);
  }

  acknowledge() {
    this.acknowledgedAt = this.now();
  }

  /** Newest first. */
  recent(): Finding[] {
    const now = this.now();
    return this.history.filter((r) => 0 <= now - r.at && now - r.at <= RETENTION).reverse();
  }

  clear() {
    this.history = [];
    this.states.clear();
  }

  add(f: Finding): boolean {
    if (!(0 <= this.now() - f.at && this.now() - f.at <= RETENTION)) return false;
    const i = this.history.findIndex((r) => r.id === f.id);
    if (i >= 0) {
      // New context updates the existing finding without a second notification; a delayed poll can't roll evidence back.
      const row = this.history[i];
      if (row.server === f.server && row.at === f.at && row.kind === f.kind && row.evidence === f.evidence && (f.chat?.revision ?? -1) > (row.chat?.revision ?? -1))
        this.history[i] = { ...row, chat: f.chat };
      return false;
    }
    this.history.push(f);
    if (this.history.length > HISTORY) this.history.shift();
    return true;
  }

  appendChat(server: string, message: ChatLine) {
    this.history = this.history.map((row) => {
      const ctx = row.chat;
      if (row.server !== server || !ctx || (ctx.status !== "collecting" && ctx.status !== "complete")) return row;
      if (message.at > ctx.until) return ctx.status === "collecting" ? { ...row, chat: { ...ctx, status: "complete", revision: ctx.revision + 1 } } : row;
      if (message.at < row.at) return row;
      const messages = [...ctx.messages, { ...message, position: "after" as const }];
      const limited = messages.filter((m) => m.position === "after").length >= CHAT_LIMIT;
      return { ...row, chat: { ...ctx, messages, revision: ctx.revision + 1, status: limited ? "limited" : ctx.status } };
    });
  }

  /** Take one event from a station. `names` reads a callsign from an ID; `roster` is `publicRoster` of its players.
   *  Returns the findings it earned. */
  ingest(server: string, event: Dict, names: (id: string) => string | undefined = () => undefined, context: Dict = {}, roster: Who[] = []): Finding[] {
    const now = this.now();
    let stamp = "time" in event ? event.time : now;
    if (typeof stamp !== "number" || !Number.isFinite(stamp) || now - stamp > 30) return [];
    if (stamp > now) {
      if (stamp - now > 30) return [];
      stamp = now; // a server clock a little ahead of this one
    }
    const kind = event.event;
    if (kind === "lagged" || kind === "phase" || (kind === "control" && event.action === "kill_feed" && !event.ok)) {
      this.reset(server, kind !== "lagged");
      return [];
    }
    let state = this.states.get(server);
    if (!state) {
      if (this.states.size >= 256) {
        const evicted = this.states.keys().next().value!;
        this.interruptChat(evicted);
        this.states.delete(evicted);
      }
      this.states.set(server, (state = fresh()));
    }
    if (state.chatSince === null) state.chatSince = stamp;
    const combat = JSON.stringify([context.map, context.mode, quietKillMode(context)]);
    if (combat !== state.combat) {
      state.kills = [];
      state.combat = combat;
    }
    if (stamp < state.last) return [];
    state.last = stamp;
    if ("time" in event) {
      const signature = hash24(stable(event));
      for (const [k, t] of state.recent) if (stamp - t > 30) state.recent.delete(k);
      if (state.recent.has(signature)) return [];
      if (state.recent.size >= 1024) drop(state.recent);
      state.recent.set(signature, stamp);
    }
    for (const [k, t] of state.cooldowns) if (stamp - t >= VOTE_WINDOW) state.cooldowns.delete(k);
    const found: Finding[] = [];
    while (state.chat.length && stamp - state.chat[0].at > CHAT_BEFORE) state.chat.shift();
    const channel = String(event.channel ?? "").toLowerCase();
    let chat: ChatLine | null = null;
    if (kind === "chat" && channel !== "server" && words(event.text)) {
      chat = { at: stamp, speaker: subject({ id: event.player_id || event.id || event.player, name: event.name }, names, roster),
        channel: words(event.channel || "all", 24), text: words(event.text, 500) };
      this.appendChat(server, chat);
    }
    const player = (value: unknown): [string, string] => {
      let k: unknown, label: unknown;
      if (value && typeof value === "object") {
        const v = value as Dict;
        k = v.id || v.player_id || v.name;
        label = v.name || names(String(k)) || k;
      } else [k, label] = [value, names(String(value)) ?? value];
      return [words(k, 100), words(label, 80) || "Unknown player"];
    };
    const report = (k: string, category: string, level: "review" | "highlight", title: string, evidence: string, interval = 60,
      subjects: unknown[] = [], reporter?: unknown): boolean => {
      if (stamp - (state!.cooldowns.get(k) ?? -Infinity) < interval) return false;
      if (state!.cooldowns.size >= 512) drop(state!.cooldowns);
      state!.cooldowns.set(k, stamp);
      const before = state!.chat.slice(-CHAT_LIMIT);
      const row: Finding = {
        id: hash24(JSON.stringify([server, stamp, k, evidence])), server, at: stamp, kind: category, level, title: words(title),
        evidence: words(evidence, 700), source: "Native RCON events", subjects: subjects.map((v) => subject(v, names, roster)),
        reporter: reporter !== undefined ? subject(reporter, names, roster) : null,
        chat: level === "review" ? {
          messages: [...before.map((m) => ({ ...m, position: "before" as const })), ...(chat ? [{ ...chat, position: "trigger" as const }] : [])],
          since: Math.max(state!.chatSince!, stamp - CHAT_BEFORE), until: stamp + CHAT_AFTER, before_limited: state!.chat.length > CHAT_LIMIT,
          status: "collecting", revision: 1,
        } : null,
      };
      if (!this.add(row)) return false;
      found.push(row);
      return true;
    };

    if (kind === "vote") {
      const text = words(event.text);
      const caller = event.caller || event.name || event.player;
      let target = event.target || event.target_id;
      let started = (event.action === "start" || event.action === "started") && event.vote_type === "kick";
      const match = /^called a vote to kick (.+)$/i.exec(text);
      if (match) {
        target = strip(match[1], '"');
        started = true;
      }
      const [actor, actorName] = player(caller), [victim, victimName] = player(target);
      // Results, yes/no votes and chat about votes do not count as starts.
      if (started && actor && victim && actor !== victim) {
        state.votes.push([stamp, actor, victim, actorName, victimName]);
        if (state.votes.length > 128) state.votes.shift();
        while (state.votes.length && stamp - state.votes[0][0] > VOTE_WINDOW) state.votes.shift();
        const attempts = state.votes.filter((v) => v[2] === victim);
        const callers = [...new Set(attempts.map((v) => v[3]))].sort();
        if (attempts.length >= 3)
          report(key("target", victim), "repeated_votes", "review", `Repeated kick votes against ${victimName}`,
            `${attempts.length} vote starts in 5 minutes from ${callers.length} caller(s): ${callers.join(", ")}. Repeated targeting warrants review; it does not establish misconduct.`, 120, [target]);
        const forward = state.votes.filter((v) => v[1] === actor && v[2] === victim), reverse = state.votes.filter((v) => v[1] === victim && v[2] === actor);
        if (forward.length >= 2 && reverse.length >= 2)
          report(key("fight", ...[actor, victim].sort()), "vote_fight", "review", `Back-and-forth kick votes: ${actorName} / ${victimName}`,
            `${forward.length} starts in one direction and ${reverse.length} in the other within 5 minutes. Check the vote and chat history before acting.`, 120, [target, caller]);
      }
    } else if (kind === "kill") {
      if (quietKillMode(context)) return []; // streaks and kill counts mean little in these modes
      const [killer, name] = player(event.killer), [victim] = player(event.victim);
      if (killer && victim && killer !== victim && !["suicide", "betrayal", "teamkill"].some((k) => event[k] === true)) {
        state.kills.push([stamp, killer, victim]);
        if (state.kills.length > 512) state.kills.shift();
        while (state.kills.length && stamp - state.kills[0][0] > 10) state.kills.shift();
        const kills = state.kills.filter((k) => k[1] === killer), opponents = new Set(kills.map((k) => k[2])).size;
        if (kills.length >= 5 && opponents >= 3 && stamp - (state.cooldowns.get(key("burst-server")) ?? -Infinity) >= 60
          && report(key("burst", killer), "kill_burst", "highlight", `${name}: ${kills.length} kills in 10 seconds`,
            `Observed ${kills.length} kill events against ${opponents} opponents. A highlight, not evidence of cheating.`, 300, [event.killer]))
          state.cooldowns.set(key("burst-server"), stamp);
        const fast = kills.filter((k) => stamp - k[0] <= 3), fastOpponents = new Set(fast.map((k) => k[2])).size;
        if ("time" in event && fast.length >= 12 && fastOpponents >= 6)
          report(key("rate", killer), "unusual_rate", "review", `Unusual kill burst: ${name}`,
            `${fast.length} native-timestamped kills against ${fastOpponents} opponents in 3 seconds. Explosives, custom modes or delayed server telemetry can explain this. Review gameplay; this is not a cheat verdict.`, 120, [event.killer]);
      }
    } else if (kind === "cheat") {
      const [ident, name] = player(event.player_id || event.id || event.name || event.player);
      report(key("native", ident), "native_flag", "review", `Server anti-cheat flag: ${name}`,
        words(event.text || event.reason || "No reason supplied") + ". Native server report; investigate before deciding on moderation.", 60,
        [{ id: ident, name: event.name || name }]);
    } else if (kind === "chat" && channel !== "server") {
      const request = chatRequest(event.text);
      if (request) {
        const reporter = { id: event.player_id || event.id || event.player, name: event.name };
        const [ident, name] = player(reporter);
        // Pronouns are conversation references, not an identified roster player.
        const target = request.target && !["him", "her", "them", "someone", "somebody", "that guy", "this guy"].includes(request.target) ? request.target : "";
        const targets = target ? [target] : request.kind === "admin_help" ? [reporter] : [];
        const actorKey = key("chat-actor", ident, request.kind);
        if (stamp - (state.cooldowns.get(actorKey) ?? -Infinity) >= 60) {
          const title = { admin_help: "Player needs help", moderation_request: "Player requests moderation", player_report: "Player reported possible misconduct" }[request.kind];
          const heading = request.target ? `${title}: ${subject(request.target, names, roster).name} · from ${name}` : `${title}: ${name}`;
          if (report(key("chat-topic", request.topic), request.kind, "review", heading,
            "Unverified player chat: " + words(event.text, 500) + " — Review the conversation before acting.", CHAT_REPEAT, targets, reporter))
            state.cooldowns.set(actorKey, stamp);
        }
      }
    } else if (kind === "medal" || kind === "award") {
      // A kill/death ratio is insufficient: it takes an explicit native award.
      if (words(event.medal || event.award).toLowerCase() === "perfection") {
        const [ident, name] = player(event.player_id || event.player || event.name);
        report(key("perfection", ident), "perfection", "highlight", `Server reported Perfection: ${name}`,
          "Explicit Perfection award in the native event feed. Never inferred from a partial match.", 60, [{ id: ident, name }]);
      }
    }
    if (chat) {
      state.chat.push(chat);
      if (state.chat.length > CHAT_LIMIT + 1) state.chat.shift();
    }
    return found;
  }
}
