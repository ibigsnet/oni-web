// Reading tolerant: field names come from the docs, so every lookup accepts the plausible spellings. The rest is
// what the terminal console's app.py did to turn replies and events into words, unchanged in meaning.
import { AMBER, CYAN, DIM, GOLD, GREEN, GREY, INK, RED, TEAMS, TEAM_COLOR, WHITE } from "../art/palette";

export type Dict = Record<string, any>;
export type Span = [string, string?, string?]; // text, colour, background
export type Line = Span[];

export function pick(d: unknown, ...keys: string[]): any {
  if (d && typeof d === "object" && !Array.isArray(d)) {
    for (const k of keys) {
      const v = (d as Dict)[k];
      if (v !== null && v !== undefined && v !== "") return v;
    }
  }
  return undefined;
}

/** v as a number, or undefined. A count may come as the list of what it counts. */
export function num(v: unknown): number | undefined {
  if (typeof v === "number") return v;
  if (Array.isArray(v)) return v.length;
  return undefined;
}

export const plain = (l: Line) => l.map((s) => s[0]).join("");

export function teamOf(p: Dict): string {
  const t = pick(p, "team", "team_name", "color");
  if (typeof t === "number" && t >= 0 && t < TEAMS.length) return TEAMS[t];
  return t !== undefined ? String(t).toLowerCase() : "";
}

/** Player ID first: numbers shift when someone leaves between a refresh and a keypress. */
export function targetOf(p: Dict): string {
  const pid = pick(p, "player_id", "id", "xuid");
  if (pid) return String(pid);
  const n = pick(p, "number", "index");
  return n !== undefined ? `#${n}` : String(pick(p, "name") ?? "?");
}

export function who(v: unknown, names: Map<unknown, string> = new Map()): string {
  if (v && typeof v === "object") return String(pick(v, "name") ?? names.get(pick(v, "engine_id")) ?? "?");
  if (typeof v === "number") return names.get(v) ?? `#${v}`;
  return v !== undefined && v !== null && v !== "" ? String(v) : "?";
}

const ID_KEY = /(^|_)(id|xuid|uid|guid|uuid)$/i;
export const ID_LIKE = /^(\d{6,}|[0-9a-f]{16,})$/i;
const SLOT = 1n << 64n;

/** Every spelling an ID can take: as written, the same number in decimal and in hex, and for a long hex ID either
 *  64-bit half of it. A kill names its players by one of a roster entry's IDs, in whichever base the server picks. */
export function idForms(v: unknown): Set<string> {
  const out = new Set<string>();
  if (typeof v !== "number" && typeof v !== "string") return out;
  if (v === "") return out;
  const s = String(v).trim().toLowerCase();
  out.add(s);
  if (/^-?\d+$/.test(s)) {
    let n = BigInt(s) % SLOT;
    if (n < 0n) n += SLOT;
    out.add(n.toString());
    out.add(n.toString(16));
  } else if (/^[0-9a-f]{5,}$/.test(s)) {
    out.add(BigInt("0x" + s).toString());
    if (s.length > 16) for (const half of [s.slice(0, 16), s.slice(-16)]) {
      out.add(half);
      out.add(BigInt("0x" + half).toString());
    }
  }
  return out;
}

export function playerIds(p: Dict): Set<string> {
  const out = new Set<string>();
  for (const [k, v] of Object.entries(p)) if (ID_KEY.test(k)) for (const f of idForms(v)) out.add(f);
  return out;
}

const REFS = ["killer", "victim", "name", "player", "from", "sender"];

/** A copy of the event that names its players outright, read against the roster when it arrives. */
export function resolve(ev: Dict, names: Map<unknown, string>, ident?: (v: unknown) => string | null): Dict {
  const out: Dict = {};
  for (const [k, v] of Object.entries(ev)) {
    const n = (k === "killer" || k === "victim") && ident ? ident(v) : null;
    if (n !== null) out[k] = n;
    else if (REFS.includes(k) && (typeof v === "number" || (v && typeof v === "object"))) out[k] = who(v, names);
    else out[k] = v;
  }
  return out;
}

/** [team, players, total score] per team, in the engine's team order. */
export function tally(players: Dict[]): [string, number, number][] {
  const teams = new Map<string, [number, number]>();
  for (const p of players) {
    const t = teamOf(p);
    if (!t) continue;
    const e = teams.get(t) ?? [0, 0];
    e[0]++;
    e[1] += num(pick(p, "score")) ?? 0;
    teams.set(t, e);
  }
  const order = (t: string) => (TEAMS.includes(t) ? TEAMS.indexOf(t) : TEAMS.length);
  return [...teams.entries()].sort((a, b) => order(a[0]) - order(b[0])).map(([t, [n, s]]) => [t, n, s]);
}

/** The teams, or none in a free-for-all, which can put every player on a team of their own. */
export function sides(players: Dict[]) {
  const t = tally(players);
  return t.length > 2 && t.every(([, n]) => n === 1) ? [] : t;
}

export function gauge(v: number, width: number, color: string): Line {
  const n = Math.round(Math.max(0, Math.min(1, v)) * width);
  return [["▰".repeat(n), color], ["▱".repeat(width - n), DIM]];
}

export function bar(v: unknown, width = 5): Line {
  if (typeof v !== "number") return [["—", DIM]];
  return gauge(v, width, v > 0.6 ? GREEN : v > 0.3 ? AMBER : RED);
}

const BARS = "▁▂▃▄▅▆▇█";
export function spark(values: number[], color = CYAN): Line {
  const top = Math.max(0, ...values) || 1;
  return values.map((v) => [BARS[Math.max(0, Math.min(7, Math.ceil((v / top) * 8) - 1))], v > 0 ? color : "#1C242E"] as Span);
}

export function splitBar(parts: [number, string][], width: number): Line {
  let total = parts.reduce((a, [v]) => a + v, 0);
  if (!total) {
    parts = parts.map(([, c]) => [1, c]);
    total = parts.length;
  }
  let used = 0;
  return parts.map(([v, c], i) => {
    const n = i === parts.length - 1 ? width - used : Math.min(width - used, Math.round((v / total) * width));
    used += n;
    return ["━".repeat(Math.max(0, n)), c] as Span;
  });
}

export function pingCell(ms: number | undefined): Line {
  return ms === undefined ? [["—", DIM]] : [[String(ms), ms < 100 ? GREEN : ms < 200 ? AMBER : RED]];
}

export function until(ts: unknown): string {
  if (typeof ts !== "number") return "PERMANENT";
  const left = Math.floor(ts - Date.now() / 1000);
  if (left <= 0) return "expired";
  const d = Math.floor(left / 86400), h = Math.floor((left % 86400) / 3600), m = Math.floor((left % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

const pad = (n: number) => String(n).padStart(2, "0");
export const hms = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

/** HH:MM:SS for an event's time, in seconds or milliseconds; now when it has none that reads. */
export function clock(ts: unknown): string {
  if (typeof ts === "number" && isFinite(ts)) return hms(new Date(ts > 1e11 ? ts : ts * 1000));
  return hms(new Date());
}

/** The time of day of an epoch time, with the date when it wasn't today. */
export function when(ts: number): string {
  const t = new Date(ts * 1000), now = new Date();
  return t.toDateString() === now.toDateString() ? hms(t) : `${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${hms(t)}`;
}

export function age(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${pad(m)}m` : m ? `${m}m ${pad(s % 60)}s` : `${s}s`;
}

const KEY_LIKE = /rfk_[A-Za-z0-9_\-]{6,}/g;
const IPV4 = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g;
export const BLANK = "███.███.███.███";
const ADDRESS_KEYS = new Set(["address", "ip", "ip_address", "addr"]);

export const scrub = (s: string) => s.replace(KEY_LIKE, "rfk_…████");
export const redactAddr = (v: unknown, on: boolean) => (on && v ? BLANK : String(v || "—"));
export const redactText = (s: string, on: boolean) => (on ? scrub(s).replace(IPV4, BLANK) : scrub(s));

export function redactData(v: unknown, on: boolean): unknown {
  if (typeof v === "string") return redactText(v, on);
  if (Array.isArray(v)) return v.map((x) => redactData(x, on));
  if (v && typeof v === "object") {
    return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, on && ADDRESS_KEYS.has(k) && typeof x === "string" ? redactAddr(x, on) : redactData(x, on)]));
  }
  return v;
}

export function clipId(v: unknown): string {
  const s = String(v);
  return s.length > 16 && /^[0-9a-fA-F-]+$/.test(s) ? s.slice(0, 8) + "…" : s;
}

const q = (a: string) => (!a || a.includes(" ") ? `"${a}"` : a);
export const cmdLine = (command: string, args: string[]) => [command, ...args.map(q)].join(" ");

const FREE_TEXT: Record<string, number> = { say: 0, tell: 1, kick: 1, servername: 0 };

/** A console line as the command and its arguments. Quotes group words as in a shell, except in the message a say,
 *  tell, kick or rename carries: that's the rest of the line as typed. */
export function parseCommand(line: string): string[] {
  let i = 0;
  const word = (): string | null => {
    while (i < line.length && /\s/.test(line[i])) i++;
    if (i >= line.length) return null;
    let out = "", quote = "";
    while (i < line.length) {
      const c = line[i];
      if (quote) {
        if (c === quote) quote = "";
        else if (c === "\\" && quote === '"' && i + 1 < line.length && '"\\'.includes(line[i + 1])) out += line[++i];
        else out += c;
      } else if (c === '"' || c === "'") quote = c;
      else if (/\s/.test(c)) break;
      else if (c === "\\" && i + 1 < line.length) out += line[++i];
      else out += c;
      i++;
    }
    if (quote) throw new Error("No closing quotation");
    return out;
  };
  const first = word();
  if (first === null) return [];
  const words = [first];
  const lead = FREE_TEXT[first.toLowerCase()];
  if (lead === undefined) {
    for (let w = word(); w !== null; w = word()) words.push(w);
    return words;
  }
  while (words.length <= lead) {
    const w = word();
    if (w === null) break;
    words.push(w);
  }
  let rest = line.slice(i).trim();
  if (rest.length > 1 && rest[0] === rest[rest.length - 1] && "'\"".includes(rest[0]) && !rest.slice(1, -1).includes(rest[0])) rest = rest.slice(1, -1);
  return rest ? [...words, rest] : words;
}

const SEPARATORS = [" · ", " | ", " - ", " — ", ": "];

/** ["ALPHA", "Big Team Rockets"] for "ALPHA · Big Team Rockets"; ["", name] when it carries no tag. */
export function splitTag(name: string): [string, string] {
  const at = SEPARATORS.map((sep) => [name.indexOf(sep), sep] as [number, string]).filter(([i]) => i > 0 && i <= 24);
  if (!at.length) return ["", name];
  at.sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1]));
  const [i, sep] = at[0];
  const own = name.slice(i + sep.length).trim();
  return own ? [name.slice(0, i).trim(), own] : ["", name];
}

export function commonPrefix(xs: string[]): string {
  if (!xs.length) return "";
  let p = xs[0];
  for (const x of xs) while (!x.startsWith(p)) p = p.slice(0, -1);
  return p;
}

export function stripEnds(s: string, chars: string) {
  let a = 0, b = s.length;
  while (a < b && chars.includes(s[a])) a++;
  while (b > a && chars.includes(s[b - 1])) b--;
  return s.slice(a, b);
}

// A connection failure in words to act on: (what the error says, card text, the full explanation)
const EXPLAIN: [string[], string, string][] = [
  [["ssh: connect to host", "could not resolve hostname"], "SSH CAN'T REACH THE BOX", "SSH couldn't reach that machine. Check the user@host spelling, and that SSH runs there."],
  [["host key verification"], "SSH DOESN'T KNOW THE BOX", "SSH hasn't seen that machine before. Connect once with ssh in a terminal to trust it, then retry."],
  [["permission denied", "publickey"], "SSH REFUSED YOUR KEY", "SSH refused your key. Load it into your SSH agent, or name it in ~/.ssh/config."],
  [["no ssh client"], "SSH NOT INSTALLED", "There's no SSH on this computer, so the tunnel can't open."],
  [["connect call failed", "connection refused", "refused the network connection", "errno 111", "10061", "1225", "os error 111"], "NOTHING ON THAT PORT", "Nothing answered on that port. Is the server running, with an RCON password set in dedicated.toml?"],
  [["name or service not known", "getaddrinfo", "nodename nor servname", "no address associated", "11001", "failed to lookup address", "no such host"], "UNKNOWN HOST", "Can't find that host name. Check the spelling."],
  [["network is unreachable", "no route to host"], "NO ROUTE", "Can't reach that network. Check your connection or VPN."],
  [["timed out", "timeout", "10060", "semaphore"], "NO ANSWER", "No answer. Check the address and port, and any firewall in between."],
  [["invalid http", "did not receive a valid http", "rejected websocket", "invalid status", "speak rcon", "http error", "handshake not finished", "missing, duplicated or incorrect header", "protocol error"], "NOT AN RCON PORT", "Something answered, but it isn't RCON. RCON listens on the game port."],
  [["isn't a valid uri", "invalid uri", "scheme isn't", "url error", "unsupported url scheme", "relative url"], "BAD LINK", "That isn't a valid ws:// or wss:// link."],
  [["connection closed", "connection lost", "no close frame", "connection reset", "broken pipe", "already closed"], "CONNECTION DROPPED", "The connection dropped."],
  [["console full", "tools connected"], "CONSOLE FULL", "The server already has 4 tools connected (its limit). This keeps trying and gets in as soon as one leaves."],
];

/** Why a station isn't online, for someone who has never seen a socket error. */
export function explain(detail: string, state = "offline", short = false): string {
  const d = (detail || "").replace(/;?\s*retry in \d+s$/, "").trim();
  const low = d.toLowerCase();
  if (state === "denied") {
    if (["too many", "locked", "lockout"].some((k) => low.includes(k)))
      return short ? "LOCKED OUT" : "Locked out after too many wrong passwords. Wait ten minutes, then retry.";
    return short ? "PASSWORD REFUSED" : "The server refused the password. Check it against dedicated.toml [rcon]. Five wrong tries in ten minutes lock you out for a while.";
  }
  for (const [keys, card, text] of EXPLAIN) if (keys.some((k) => low.includes(k))) return short ? card : text;
  return short ? d.toUpperCase() || "NO CARRIER" : d || "Not connected.";
}

export const CATS: Record<string, string> = { chat: WHITE, combat: RED, traffic: CYAN, moderation: AMBER, ops: GREY };
export const KIND: Record<string, string> = { chat: "chat", kill: "combat", join: "traffic", leave: "traffic", refused: "traffic",
  kick: "moderation", ban: "moderation", unban: "moderation", mute: "moderation", unmute: "moderation", cheat: "moderation", admin: "moderation" };
export const GLYPH: Record<string, string> = { health: "⚠", chat: "»", kill: "✕", join: "▲", leave: "▼", refused: "⊘", kick: "◆", ban: "■",
  unban: "□", mute: "◈", unmute: "◇", admin: "✦", cheat: "!", vote: "◉", control: "•", uplink: "≡", forge: "⬢" };

/** What an event says, after its time and station. Known kinds get a layout; anything else prints its fields. */
export function describe(ev: Dict, redact = true): Line {
  const kind = String(ev.event ?? "?");
  const cat = KIND[kind] ?? "ops";
  const line: Line = [];
  if (kind === "chat") {
    const ch = String(ev.channel ?? "all");
    if (ch === "server") {
      line.push(["[SERVER] ", AMBER], [redactText(String(ev.text ?? ""), redact), AMBER]);
    } else {
      const team = String(pick(ev, "team") ?? ch.replace(/^team/, "").trim()).toLowerCase();
      line.push([`[${ch.toUpperCase()}] `, DIM], [who(pick(ev, "name", "player", "from", "sender")), TEAM_COLOR[team] ?? WHITE],
        [": " + redactText(String(ev.text ?? ""), redact)]);
    }
  } else if (kind === "kill") {
    const killer = pick(ev, "killer"), victim = pick(ev, "victim");
    const k = who(killer), v = who(victim);
    if (killer === undefined || k === v) line.push([v, WHITE], [" died", DIM]);
    else line.push([k, WHITE], [" ✕ ", RED], [v]);
    const how = pick(ev, "weapon", "damage", "cause", "how");
    if (how) line.push([`  [${how}]`, DIM]);
    for (const m of ev._medals ?? []) line.push(["  "], [` ${m} `, INK, GOLD]);
  } else if (kind === "health") {
    line.push(["HEALTH  " + String(ev.text ?? ""), ev.level === "crit" ? RED : AMBER]);
  } else {
    line.push([kind.toUpperCase(), CATS[cat]]);
    const rest: Dict = Object.fromEntries(Object.entries(ev).filter(([k]) => !["type", "event", "time"].includes(k)));
    const name = rest.name ?? rest.player;
    delete rest.name;
    delete rest.player;
    if (name !== undefined && name !== null) line.push([`  ${who(name)}`, WHITE]);
    const text = rest.text;
    delete rest.text;
    if (text) line.push([`  ${redactText(String(text), redact)}`]);
    for (const [k, v] of Object.entries(rest)) {
      const shown = ADDRESS_KEYS.has(k) ? redactAddr(v, redact) : v && typeof v === "object" ? JSON.stringify(redactData(v, redact)) : redactText(clipId(v), redact);
      line.push([`  ${k}=`, DIM], [String(shown)]);
    }
  }
  return line;
}

const UNSAFE = /[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]/g;
export const tidy = (v: unknown, lines = false) => {
  const s = String(v ?? "").replace(UNSAFE, "");
  return lines ? s.trim() : s.split(/\s+/).filter(Boolean).join(" ");
};

/** A few plain words for what the Superintendent reacted to, under its face. */
export function reactionWords(ev: Dict, redact = true): string {
  const kind = String(ev.event ?? "?"), name = who(pick(ev, "name", "player", "from", "sender"));
  if (kind === "kill" && ev._medals?.length) return tidy(`${ev._medals[ev._medals.length - 1]}: ${who(pick(ev, "killer"))}`);
  if (kind === "join") return tidy(`${name} joined`);
  if (["kick", "ban", "mute"].includes(kind)) {
    const reason = pick(ev, "reason", "text");
    return tidy(`${kind} ${name}` + (reason ? `: ${redactText(String(reason), redact)}` : ""));
  }
  if (kind === "chat" && ev.channel !== "server") return tidy(`${name}: ${redactText(String(ev.text ?? ""), redact)}`);
  return tidy(plain(describe(ev, redact)));
}

/** The vote under way: what it's on, and the tally as a tug of war when the server gives one. */
export function voteText(v: unknown): Line[] {
  if (!v) return [[["none under way", DIM]]];
  if (typeof v !== "object") return [[[JSON.stringify(v)]]];
  const head: Line = [[String(pick(v, "subject", "type", "kind") ?? "vote").toUpperCase(), AMBER]];
  const target = pick(v, "target", "player", "name");
  if (target) head.push([` ${target}`]);
  const yes = num(pick(v, "yes", "for")), no = num(pick(v, "no", "against"));
  if (yes === undefined && no === undefined) return [head];
  const y = yes ?? 0, n = no ?? 0;
  return [head, [["YES ", DIM], [`${y} `, GREEN], ...splitBar([[y, GREEN], [n, RED]], 6), [` ${n}`, RED], [" NO", DIM]]];
}
