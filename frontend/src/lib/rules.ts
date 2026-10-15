// Automation rules: WHEN something happens on a server, IF the conditions hold, THEN do things. From ibigs/oni-rcon's
// rules.py and rules_run.py, by IBIGS. A rule is plain data built from the menus in CATALOG; the engine is pure:
// it is fed events, status and roster readings and hands back Firings, and `plan` turns one into Steps. Nothing here
// sends anything, so TEST can show a rule's steps. A rule can talk (say, tell, Discord), cancel a vote or shuffle
// teams. It can't kick, ban, mute, restart or run commands, and nothing reacts to what people say except "!commands"
// typed on purpose.
import { idForms, pick, type Dict } from "./util";

export const ROLES = ["admin", "vip", "mod"] as const;
export const SPREES: Record<number, string> = { 5: "Killing Spree", 10: "Killing Frenzy", 15: "Running Riot", 20: "Rampage", 25: "Untouchable", 30: "Invincible" };
export const MULTIS: Record<number, string> = { 2: "Double Kill", 3: "Triple Kill", 4: "Overkill", 5: "Killtacular", 6: "Killtrocity", 7: "Killimanjaro", 8: "Killtastrophe", 9: "Killpocalypse", 10: "Killionaire" };
export const CHAIN = 4; // seconds between kills that still chain into a multi-kill (Halo 3's window)
export const MEDALS = [...Object.values(SPREES), ...Object.values(MULTIS), "Killjoy", "Perfection"];
export const MAX_RULES = 128;
export const MAX_LINE = 240;
export const SAY_PER_MINUTE = 20; // per server, across every rule: a misjudged rule can't flood the chat
export const DISCORD_GAP = 2; // seconds between Discord posts (Discord allows about 30 a minute per webhook)
export const HISTORY = 200;
export const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
// moments (a medal, a join) aren't held back by default; states and alerts get a breather
const INSTANT = new Set(["medal", "spree", "multikill", "join", "leave", "match_start", "match_end", "before_match", "every", "at", "vote", "weapon_kill", "first_blood", "kills_reached"]);

export type Part = "when" | "if" | "then";
export interface Field { key: string; label: string; kind: "int" | "number" | "text" | "choice" | "message" | "server" | "webhook"; default: any; min?: number; max?: number; options?: string[] }
export interface Entry { label: string; hint: string; fields: Field[] }
const F = (key: string, label: string, kind: Field["kind"], dflt: any, o: Partial<Field> = {}): Field => ({ key, label, kind, default: dflt, ...o });
const TZ = "America/New_York";

export const CATALOG: Record<Part, Record<string, Entry>> = {
  when: {
    medal: { label: "A player earns a medal", hint: "Sprees, multi-kills, Killjoy, and Perfection or any other medal the server reports.", fields: [F("medal", "Which medal", "choice", "any", { options: ["any", ...MEDALS] })] },
    spree: { label: "A player reaches a killing spree", hint: "Kills without dying: 5 is a Killing Spree, 10 a Frenzy, and so on.", fields: [F("counts", "At these counts (comma list)", "text", "5,10,15,20,25,30", { max: 60 })] },
    multikill: { label: "A player gets a multi-kill", hint: "Kills less than 4 seconds apart: 2 is a Double Kill, 3 a Triple, 4 an Overkill.", fields: [F("min", "At least this many kills", "int", 3, { min: 2, max: 10 })] },
    weapon_kill: { label: "A player gets a kill with a weapon", hint: "Part of the weapon's name is enough: sniper, sword, rocket, melee, grenade…", fields: [F("weapon", "Weapon", "text", "sniper", { max: 40 })] },
    first_blood: { label: "First blood", hint: "The first kill of a match.", fields: [] },
    kills_reached: { label: "A player reaches N kills in a match", hint: "Counted from the kill feed this match.", fields: [F("count", "N kills", "int", 25, { min: 1, max: 500 })] },
    betrayal: { label: "A player betrays a teammate", hint: "A teammate kill. Rules can only talk about it: no punishment.", fields: [] },
    fps_below: { label: "A server's frame rate drops under N", hint: "The server's own minute-by-minute report: 60 is perfect, under 54 players notice, under 40 is laggy.", fields: [F("fps", "Frames a second", "int", 40, { min: 5, max: 59 })] },
    fps_recovered: { label: "A server's frame rate is back to N or more", hint: "After it had dropped under N.", fields: [F("fps", "Frames a second", "int", 54, { min: 5, max: 60 })] },
    join: { label: "A player joins", hint: "Someone finishes connecting.", fields: [] },
    leave: { label: "A player leaves", hint: "Someone disconnects or is removed.", fields: [] },
    command: { label: "A player types a !command", hint: "Only messages that START with the command count, e.g. !admin. Ordinary chat never triggers anything.", fields: [F("command", "Command", "text", "!admin", { max: 24 })] },
    vote: { label: "A vote starts", hint: "A player calls a vote.", fields: [F("kind", "Kind of vote", "choice", "any", { options: ["any", "kick", "endround", "endgame", "shuffle", "playlist"] })] },
    match_start: { label: "A match starts", hint: "The game goes live.", fields: [] },
    match_end: { label: "A match ends", hint: "The game finishes (not a round break).", fields: [] },
    before_match: { label: "Voting or loading begins", hint: "Between matches, before the next one starts.", fields: [] },
    server_down: { label: "A server goes down", hint: "This console loses it for this long; a quick reconnect doesn't count.", fields: [F("seconds", "Down for at least (seconds)", "int", 60, { min: 15, max: 3600 })] },
    server_up: { label: "A server comes back", hint: "It answers again after being reported down.", fields: [] },
    full: { label: "A server fills up", hint: "Every slot is taken.", fields: [] },
    empty: { label: "A server empties", hint: "The last player leaves.", fields: [] },
    players_above: { label: "Players rise to N", hint: "The count reaches N from below.", fields: [F("count", "N players", "int", 8, { min: 1, max: 128 })] },
    players_below: { label: "Players drop under N", hint: "The count falls below N.", fields: [F("count", "N players", "int", 4, { min: 1, max: 128 })] },
    every: { label: "Every N minutes", hint: "On a timer, lined up with the clock.", fields: [F("minutes", "Minutes", "int", 30, { min: 1, max: 10080 })] },
    at: { label: "At a time of day", hint: "Cron-style: minute hour day month weekday.", fields: [F("cron", "When (minute hour day month weekday)", "text", "0 20 * * 5", { max: 60 }), F("timezone", "Timezone", "text", TZ, { max: 48 })] },
  },
  if: {
    players_between: { label: "Player count is between", hint: "On the server where it happened.", fields: [F("min", "At least", "int", 0, { min: 0, max: 128 }), F("max", "At most", "int", 128, { min: 0, max: 128 })] },
    admin_online: { label: "An admin is online", hint: "Someone with the ADMIN role, or the server's own admin flag.", fields: [F("where", "Where", "choice", "any", { options: ["any", "this", "other"] })] },
    no_admin_online: { label: "No admin is online", hint: "Nobody with ADMIN is playing on any server this console watches.", fields: [] },
    player_role: { label: "The player has a role", hint: "The player who triggered the rule.", fields: [F("role", "Role", "choice", "vip", { options: [...ROLES, "none"] })] },
    player_kd: { label: "The player's kills/deaths this match", hint: "Kills divided by deaths (deaths of 0 count as 1), with at least some kills so one lucky kill doesn't count.",
      fields: [F("min_kd", "At least this K/D (e.g. 2.5)", "number", "2.0", { min: 0, max: 100 }), F("min_kills", "And at least this many kills", "int", 5, { min: 0, max: 500 })] },
    day_of_week: { label: "The day is", hint: "Comma list: mon,tue,wed,thu,fri,sat,sun.", fields: [F("days", "Days", "text", "fri,sat,sun", { max: 40 }), F("timezone", "Timezone", "text", TZ, { max: 48 })] },
    fps_under: { label: "The server's frame rate is under N", hint: "Right now, from its latest report.", fields: [F("fps", "Frames a second", "int", 54, { min: 5, max: 60 })] },
    server_players: { label: "Another server's player count is between", hint: "", fields: [F("server", "Server", "server", ""), F("min", "At least", "int", 0, { min: 0, max: 128 }), F("max", "At most", "int", 128, { min: 0, max: 128 })] },
    map_is: { label: "The map is", hint: "Part of the map's name is enough.", fields: [F("text", "Map", "text", "", { max: 40 })] },
    mode_is: { label: "The game mode is", hint: "Part of the mode's name is enough.", fields: [F("text", "Mode", "text", "", { max: 40 })] },
    time_between: { label: "The time is between", hint: "Local time, e.g. 18:00 to 23:59. Wraps past midnight.", fields: [F("start", "From (HH:MM)", "text", "18:00", { max: 5 }), F("end", "To (HH:MM)", "text", "23:59", { max: 5 }), F("timezone", "Timezone", "text", TZ, { max: 48 })] },
    server_state: { label: "Another server is", hint: "Up, down, full or empty right now.", fields: [F("server", "Server", "server", ""), F("state", "State", "choice", "up", { options: ["up", "down", "full", "empty"] })] },
  },
  then: {
    say: { label: "Say in chat", hint: "A message everyone on the server sees.", fields: [F("to", "On", "choice", "this", { options: ["this", "all", "other servers"] }), F("message", "Message", "message", "")] },
    say_on: { label: "Say on a specific server", hint: "", fields: [F("server", "Server", "server", ""), F("message", "Message", "message", "")] },
    tell_player: { label: "Message the player privately", hint: "Only the player who triggered the rule sees it.", fields: [F("message", "Message", "message", "")] },
    tell_admins: { label: "Message the admins privately", hint: "Every online ADMIN, on whichever server they're playing.", fields: [F("where", "Admins on", "choice", "any", { options: ["any", "this", "other"] }), F("message", "Message", "message", "")] },
    tell_role: { label: "Message everyone with a role", hint: "Privately, wherever they're playing.", fields: [F("role", "Role", "choice", "vip", { options: [...ROLES] }), F("message", "Message", "message", "")] },
    cancel_vote: { label: "Cancel the vote", hint: "Stops the vote under way on that server.", fields: [] },
    shuffle: { label: "Shuffle the teams", hint: "Only while a match is being played.", fields: [] },
    wait: { label: "Wait a few seconds", hint: "Pause before the next step (1–300 seconds).", fields: [F("seconds", "Seconds", "int", 5, { min: 1, max: 300 })] },
    discord: { label: "Post to Discord", hint: "Sends the message to a Discord channel through a webhook you've added.", fields: [F("webhook", "Webhook", "webhook", ""), F("message", "Message", "message", "")] },
  },
};

export const TEMPLATE_HELP = "{player} the player · {server} this server · {count} players on it · {max} its slots · {medal} the medal · {kills} kill count · {weapon} · {victim} · {kd} K/D · {fps} frame rate · {map} · {mode} · {admins} admins online · {target} a vote's target · {args} what followed a !command · {down} the server that went down · {time}";
export const PLACEHOLDERS = new Set(["player", "server", "count", "max", "medal", "kills", "map", "mode", "admins", "target", "args", "down", "time", "weapon", "victim", "fps", "kd"]);

/** Starting points: copy one, change the words. Kept small and obviously useful. */
export const STARTERS: Dict[] = [
  { name: "Spree announcer", when: { type: "spree", counts: "5,10,15,20,25,30" }, then: [{ type: "say", to: "this", message: "{player} is on a {medal}! ({kills} kills)" }], cooldown: 0, per_hour: 240 },
  { name: "Multi-kill shout-out", when: { type: "multikill", min: 3 }, then: [{ type: "say", to: "this", message: "{medal} by {player}!" }], cooldown: 0, per_hour: 240 },
  { name: "Perfection", when: { type: "medal", medal: "Perfection" }, then: [{ type: "say", to: "all", message: "PERFECTION! {player} won on {server} without dying." }], cooldown: 0, per_hour: 60 },
  { name: "First blood", when: { type: "first_blood" }, then: [{ type: "say", to: "this", message: "First blood: {player} took out {victim}!" }] },
  { name: "Sniper shout-out", when: { type: "weapon_kill", weapon: "sniper" }, if: [{ type: "player_kd", min_kd: "3", min_kills: 10 }], then: [{ type: "say", to: "this", message: "{player} is sniping at a {kd} K/D!" }], cooldown: 300 },
  { name: "Lag alert", when: { type: "fps_below", fps: 40 }, then: [{ type: "tell_admins", where: "any", message: "{server} has dropped to {fps} fps ({count} players)." },
    { type: "discord", webhook: "", message: "{server} is lagging: {fps} fps with {count} players." }], cooldown: 900 },
  { name: "Welcome", when: { type: "join" }, then: [{ type: "tell_player", message: "Welcome to {server}, {player}! Type !admin if you need help." }], cooldown: 0, per_hour: 600 },
  { name: "!admin call", when: { type: "command", command: "!admin" }, if: [{ type: "admin_online", where: "any" }],
    then: [{ type: "tell_admins", where: "any", message: "{player} on {server} needs an admin: {args}" }, { type: "tell_player", message: "An admin has been told. Hang tight." }], cooldown: 60 },
  { name: "!admin with nobody on", when: { type: "command", command: "!admin" }, if: [{ type: "no_admin_online" }],
    then: [{ type: "discord", webhook: "", message: "{player} on {server} asked for an admin: {args}" }, { type: "tell_player", message: "No admin is online; we've pinged Discord." }], cooldown: 120 },
  { name: "Server down: tell the admins", when: { type: "server_down", seconds: 60 },
    then: [{ type: "tell_admins", where: "other", message: "Heads up: {down} is down." }, { type: "discord", webhook: "", message: "{down} has been down for a minute." }], cooldown: 600 },
  { name: "Full server alert", when: { type: "full" }, then: [{ type: "tell_admins", where: "any", message: "{server} is full ({count}/{max})." }], cooldown: 900 },
  { name: "Empty server: point people elsewhere", when: { type: "players_below", count: 2 }, then: [{ type: "say", to: "this", message: "Quiet here! Try our other servers." }], cooldown: 1800 },
  { name: "VIP kick protection", when: { type: "vote", kind: "kick" }, if: [{ type: "player_role", role: "vip" }],
    then: [{ type: "cancel_vote" }, { type: "say", to: "this", message: "{target} is a VIP and can't be vote-kicked." }], cooldown: 0 },
  // the votes switch: the dedicated server has one setting for all voting, so the console cancels a vote the moment it starts
  { name: "Votes off", when: { type: "vote", kind: "any" }, then: [{ type: "cancel_vote" }, { type: "say", to: "this", message: "Player votes are off on this server." }], cooldown: 0 },
];

/** Always on while anyone has the VIP role: VIPs can't be vote-kicked. */
export const VIP_GUARD = () => validate({ id: "builtin-vip-guard", name: "VIP kick protection (built in)", enabled: true, servers: "all", when: { type: "vote", kind: "kick" },
  if: [{ type: "player_role", role: "vip" }], then: [{ type: "cancel_vote" }, { type: "say", to: "this", message: "{target} is a VIP and can't be vote-kicked." }], cooldown: 0, per_hour: 120 });

// --- small helpers ----------------------------------------------------------------------------------------------
export const clean = (s: string) => s.replace(/\p{C}/gu, "");
const fail = (m: string): never => { throw new Error(m); };

/** The webhook shape Discord hands out. */
export const WEBHOOK = /^https:\/\/(?:(?:ptb|canary)\.)?discord(?:app)?\.com\/api\/webhooks\/\d{5,30}\/[A-Za-z0-9_-]{20,100}$/;
export const webhookOk = (url: unknown) => typeof url === "string" && WEBHOOK.test(url.trim());
/** A webhook URL with its secret part hidden, for the screen. */
export const masked = (url: string) => url.slice(0, url.lastIndexOf("/")) + "/••••••";

export const DISCORD_STEPS = [
  "To let rules post in a Discord channel:",
  "  1. In Discord, open your server and go to the channel you want.",
  "  2. Click the gear (Edit Channel) → Integrations → Webhooks → New Webhook.",
  "  3. Give it a name (for example ONI Alerts), then click Copy Webhook URL.",
  "  4. Paste the URL here and give it a short name, like alerts.",
  "You need the Manage Webhooks permission in that Discord server. Anyone with the URL can post in that channel, so keep it private. Delete the webhook in Discord to revoke it.",
].join("\n");

// --- time zones and cron ----------------------------------------------------------------------------------------
export interface Zoned { date: string; hm: string; minute: number; hour: number; day: number; month: number; dow: number; key: string }
const formats = new Map<string, Intl.DateTimeFormat>();
export function validZone(tz: string): boolean {
  try {
    zoned(0, tz);
    return true;
  } catch {
    return false;
  }
}
/** The wall clock in `tz` at `ms`: dow counts Sunday as 0. */
export function zoned(ms: number, tz: string): Zoned {
  let f = formats.get(tz);
  if (!f) formats.set(tz, (f = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", weekday: "short" })));
  const p: Dict = {};
  for (const x of f.formatToParts(new Date(ms))) p[x.type] = x.value;
  const hm = `${p.hour}:${p.minute}`;
  return { date: `${p.year}${p.month}${p.day}`, hm, minute: Number(p.minute), hour: Number(p.hour), day: Number(p.day), month: Number(p.month),
    dow: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(p.weekday), key: `${p.year}${p.month}${p.day}${hm}` };
}

/** Five cron fields as the sets of values they allow. Numbers, *, ranges, comma lists and /steps; 0 or 7 is Sunday. */
export function cronFields(expression: string): [string[], Set<number>[]] {
  const fields = String(expression).trim().split(/\s+/);
  if (fields.length !== 5) fail("Use five cron fields: minute hour day month weekday (0 or 7 = Sunday).");
  const bounds: [number, number][] = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]];
  const sets = fields.map((field, i) => {
    const [low, high] = bounds[i], values = new Set<number>();
    for (const part of field.split(",")) {
      if (!/^(\*|\d+(-\d+)?)(\/\d+)?$/.test(part)) fail("Cron accepts numbers, *, ranges, comma lists, and /steps.");
      const [base, stepText] = part.split("/");
      const step = Number(stepText ?? 1);
      if (!(step >= 1 && step <= high + 1)) fail("Cron step is outside its field range.");
      let start: number, end: number;
      if (base === "*") [start, end] = [low, high];
      else if (base.includes("-")) [start, end] = base.split("-").map(Number);
      else [start, end] = [Number(base), stepText !== undefined ? high : Number(base)];
      if (!(low <= start && start <= end && end <= high)) fail("Cron value is outside its field range.");
      for (let v = start; v <= end; v += step) values.add(v);
    }
    return values;
  });
  sets[4] = new Set([...sets[4]].map((v) => v % 7));
  return [fields, sets];
}

/** Whether the minute `z` falls on the cron expression. Standard cron: when both day fields are restricted, either may match. */
export function cronDue(expression: string, z: Zoned): boolean {
  const [fields, [minute, hour, day, month, weekday]] = cronFields(expression);
  const dom = day.has(z.day), dow = weekday.has(z.dow);
  const dayOk = fields[2].startsWith("*") || fields[4].startsWith("*") ? dom && dow : dom || dow;
  return minute.has(z.minute) && hour.has(z.hour) && month.has(z.month) && dayOk;
}

// --- checking a rule --------------------------------------------------------------------------------------------
function line(v: unknown, what: string, required = true): string {
  if (typeof v !== "string" || v.length > MAX_LINE || clean(v) !== v) fail(`${what} must be one printable line of at most ${MAX_LINE} characters.`);
  const s = v as string;
  if (required && !s.trim()) fail(`${what} is empty.`);
  const bad = [...s.matchAll(/\{(\w*)\}/g)].map((m) => m[1]).filter((n) => !PLACEHOLDERS.has(n));
  if (bad.length) fail(`${what}: unknown placeholder {${bad.sort()[0]}}. You can use: ${TEMPLATE_HELP}`);
  return s;
}

/** One WHEN/IF/THEN block, checked against the catalog. Returns it normalized. */
export function block(part: Part, b: unknown): Dict {
  const type = b && typeof b === "object" ? (b as Dict).type : undefined;
  if (typeof type !== "string" || !Object.hasOwn(CATALOG[part], type)) return fail(`Unknown ${part.toUpperCase()} choice: ${type ?? JSON.stringify(b)}.`);
  const { label, fields } = CATALOG[part][type];
  const out: Dict = { type };
  for (const f of fields) {
    let v = (b as Dict)[f.key] ?? f.default;
    if (f.kind === "int") {
      const n = typeof v === "number" ? v : /^\s*-?\d+\s*$/.test(String(v)) ? Number(v) : NaN;
      if (!Number.isInteger(n)) fail(`${label}: ${f.label} must be a whole number.`);
      if (n < f.min! || n > f.max!) fail(`${label}: ${f.label} must be ${f.min}–${f.max}.`);
      v = n;
    } else if (f.kind === "number") {
      const n = typeof v === "number" ? v : /^\s*-?\d+(\.\d+)?\s*$/.test(String(v)) ? Number(v) : NaN;
      if (!Number.isFinite(n)) fail(`${label}: ${f.label} must be a number like 2.5.`);
      if (n < f.min! || n > f.max!) fail(`${label}: ${f.label} must be ${f.min}–${f.max}.`);
      v = String(Math.round(n * 100) / 100);
    } else if (f.kind === "choice") {
      if (!f.options!.includes(v)) fail(`${label}: choose one of ${f.options!.join(", ")}.`);
    } else if (f.kind === "message") {
      v = line(v, `${label}: the message`);
    } else {
      if (typeof v !== "string" || v.length > (f.max ?? 200) || clean(v) !== v) fail(`${label}: ${f.label} is too long or has odd characters.`);
      v = (v as string).trim();
      if (f.kind === "server" && !v) fail(`${label}: choose a server.`);
      if (f.kind === "webhook" && !v) fail(`${label}: choose a Discord webhook (add one under DISCORD first).`);
    }
    out[f.key] = v;
  }
  if (type === "spree") {
    const counts = [...new Set<number>((String(out.counts).match(/\d+/g) ?? []).map(Number))].sort((a, b) => a - b);
    if (!counts.length || counts.some((c) => c < 2 || c > 100)) fail("Killing spree: give counts between 2 and 100, like 5,10,15.");
    out.counts = counts.join(",");
  } else if (type === "command") {
    if (!/^![A-Za-z0-9_-]{1,23}$/.test(out.command)) fail("A command starts with ! and has letters or numbers, like !admin.");
    out.command = out.command.toLowerCase();
  } else if (type === "day_of_week") {
    const days = out.days.split(",").map((d: string) => d.trim().toLowerCase().slice(0, 3)).filter(Boolean);
    if (!days.length || days.some((d: string) => !DAYS.includes(d))) fail("Days: use mon,tue,wed,thu,fri,sat,sun.");
    out.days = [...new Set<string>(days)].sort((a, b) => DAYS.indexOf(a) - DAYS.indexOf(b)).join(",");
    if (!validZone(out.timezone)) fail("Use a timezone such as America/New_York or UTC.");
  } else if (type === "at" || type === "time_between") {
    if (!validZone(out.timezone)) fail("Use a timezone such as America/New_York or UTC.");
    if (type === "at") cronFields(out.cron);
    else for (const k of ["start", "end"]) if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(out[k])) fail("Times look like 18:00 or 07:30.");
  } else if ((type === "players_between" || type === "server_players") && out.min > out.max) {
    fail("Player count: 'at least' is more than 'at most'.");
  }
  return out;
}

export function validate(rule: Dict): Dict {
  if (!rule || typeof rule !== "object" || Array.isArray(rule)) fail("A rule must be an object.");
  const id = String(rule.id || crypto.randomUUID().replace(/-/g, "").slice(0, 12));
  if (!/^[a-zA-Z0-9_-]{1,64}$/.test(id)) fail("Invalid rule ID.");
  const name = rule.name;
  if (typeof name !== "string" || name.length < 1 || name.length > 80 || clean(name) !== name) fail("Give the rule a name (1–80 characters).");
  const enabled = rule.enabled ?? true;
  if (typeof enabled !== "boolean") fail("enabled must be true or false.");
  let servers = rule.servers ?? "all";
  if (servers !== "all") {
    if (!Array.isArray(servers) || !servers.length || servers.length > 64 || !servers.every((s) => typeof s === "string" && s.length > 0 && s.length <= 200 && clean(s) === s))
      fail("Servers: choose all, or a list of the servers this console watches.");
    servers = [...new Set<string>(servers)].sort();
  }
  const conds = rule.if ?? [], acts = rule.then ?? [];
  if (!Array.isArray(conds) || conds.length > 8) fail("Up to 8 conditions.");
  if (!Array.isArray(acts) || acts.length < 1 || acts.length > 8) fail("A rule needs 1 to 8 things to do.");
  const when = block("when", rule.when);
  const out: Dict = { id, name, enabled, servers, when, if: (conds as unknown[]).map((c) => block("if", c)), then: (acts as unknown[]).map((a) => block("then", a)) };
  const instant = INSTANT.has(when.type);
  for (const [k, dflt, lo, hi] of [["cooldown", instant ? 0 : 30, 0, 86400], ["per_hour", instant ? 120 : 60, 1, 600]] as const) {
    const v = rule[k] ?? dflt;
    if (!Number.isInteger(v) || v < lo || v > hi) fail(`${k === "cooldown" ? "Cooldown" : "Per-hour limit"} must be ${lo}–${hi}.`);
    out[k] = v;
  }
  return out;
}

/** One block in words, for lists and the confirmation. */
export function describeBlock(part: Part, b: Dict): string {
  const { label, fields } = CATALOG[part][b.type];
  const bits: string[] = [];
  for (const f of fields) {
    const v = b[f.key];
    if (f.kind === "message") bits.push(`"${v}"`);
    else if ((v !== undefined && v !== null && v !== "" && v !== "any") || f.kind === "int") bits.push(String(v));
  }
  return label + (bits.length ? ": " + bits.join(", ") : "");
}

/** A rule in words. `name` shows a server's key (what the rule stores) as its label. */
export function describe(rule: Dict, name: (server: string) => string = (s) => s): string {
  const where = rule.servers === "all" ? "every server" : rule.servers.map(name).join(", ");
  const sub = (b: Dict) => (b.server ? { ...b, server: name(b.server) } : b);
  const lines = [`WHEN  ${describeBlock("when", rule.when)}  (${where})`,
    ...rule.if.map((c: Dict) => `IF    ${describeBlock("if", sub(c))}`), ...rule.then.map((a: Dict) => `THEN  ${describeBlock("then", sub(a))}`)];
  const limits = [...(rule.cooldown ? [`at most once every ${rule.cooldown} s`] : []), `at most ${rule.per_hour} times an hour`];
  return [...lines, "      " + limits.join(", ")].join("\n");
}

// --- match boundaries -------------------------------------------------------------------------------------------
const PHASES = new Set(["no_game", "loading", "starting", "playing", "round_over", "between_rounds", "finished"]);

/** Only witnessed native boundaries count. Reconnects and round breaks never finish a match. */
export class MatchEvents {
  phase: string | null = null;
  vote: boolean | null = null;
  played = false;
  beforeSent = false;
  observe(status: Dict): string[] {
    const phase = status.phase;
    if (!PHASES.has(phase)) {
      Object.assign(this, new MatchEvents());
      return [];
    }
    const before = this.phase, events: string[] = [];
    const ballot = status.playlist_vote;
    const vote = ballot && typeof ballot === "object" ? ballot.active : null;
    if (phase === "no_game" && before !== null && before !== "no_game") this.beforeSent = false;
    if (before !== null) {
      const voting = phase === "no_game" && vote === true && this.vote === false;
      const loading = (phase === "loading" || phase === "starting") && ["no_game", "loading", "starting"].includes(before);
      if ((voting || loading) && !this.beforeSent) {
        events.push("before_match");
        this.beforeSent = true;
      }
      if (phase === "playing" && !["playing", "round_over", "between_rounds"].includes(before)) events.push("match_start");
      if (phase === "finished" && before !== "finished" && this.played) events.push("after_match");
    }
    if (phase === "playing") this.played = true;
    else if (["finished", "no_game", "loading", "starting"].includes(phase)) this.played = false;
    this.phase = phase;
    if (vote !== null && vote !== undefined) this.vote = Boolean(vote);
    else if (phase !== "no_game") this.vote = false;
    return events;
  }
}

// --- the engine -------------------------------------------------------------------------------------------------
export interface Who { name: string; id: string; ids: Set<string>; team: unknown; admin: unknown }
export interface Lobby {
  key: string; label: string; up: boolean; downSince: number | null; downTold: boolean; count: number | null; max: number | null;
  map: string; mode: string; phase: string; roster: Map<string, Who>; sprees: Map<string, number>; chains: Map<string, [number, number]>;
  matches: MatchEvents; voteId: unknown; tally: Map<string, [number, number]>; blooded: boolean; fps: number | null;
}
export interface Firing { rule: Dict; server: string; ctx: Dict }
export type Roles = Record<string, { name: string; roles: string[]; servers: "all" | string[] }>;

/** The message with its {placeholders} filled; unknown or missing ones become empty. One printable line. */
export function fill(template: string, ctx: Dict): string {
  return template.replace(/\{(\w+)\}/g, (_, k) => clean(String(ctx[k] ?? "")).replace(/\n/g, " ")).slice(0, MAX_LINE);
}

const text = (v: unknown, n = 40) => clean(String(v ?? "")).slice(0, n);
const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;

export class Engine {
  rules: Dict[];
  roles: Roles;
  lobbies = new Map<string, Lobby>();
  fired = new Map<string, number>();
  hourly = new Map<string, number[]>();
  lastTick: number | null = null;
  constructor(rules: Dict[] = [], roles: Roles = {}, public clock: () => number = () => Date.now() / 1000) {
    this.rules = rules;
    this.roles = roles;
  }

  // facts ------------------------------------------------------------------------------------------------------
  lobby(key: string, label?: string): Lobby {
    let l = this.lobbies.get(key);
    if (!l) this.lobbies.set(key, (l = { key, label: label ?? key, up: false, downSince: null, downTold: false, count: null, max: null, map: "", mode: "", phase: "", roster: new Map(),
      sprees: new Map(), chains: new Map(), matches: new MatchEvents(), voteId: null, tally: new Map(), blooded: false, fps: null }));
    if (label) l.label = label;
    return l;
  }
  name = (key: string) => this.lobbies.get(key)?.label ?? key;
  roleOf(pid: unknown): string[] { return this.roles[String(pid)]?.roles ?? []; }

  /** [server, player] for every online admin: the ADMIN role or the server's own admin flag. */
  admins(where?: Set<string>): [string, Who][] {
    const out: [string, Who][] = [];
    for (const [key, lob] of this.lobbies) {
      if ((where && !where.has(key)) || !lob.up) continue;
      for (const p of lob.roster.values()) if (p.admin === true || this.roleOf(p.id).includes("admin")) out.push([key, p]);
    }
    return out;
  }
  withRole(role: string): [string, Who][] {
    const out: [string, Who][] = [];
    for (const [key, lob] of this.lobbies) if (lob.up) for (const p of lob.roster.values()) if (this.roleOf(p.id).includes(role)) out.push([key, p]);
    return out;
  }
  private others(key: string) { return new Set([...this.lobbies.keys()].filter((k) => k !== key)); }
  private scope(where: string, key: string): Set<string> | undefined { return where === "any" ? undefined : where === "this" ? new Set([key]) : this.others(key); }

  context(server: string, extra: Dict = {}): Dict {
    const lob = this.lobby(server), now = new Date();
    return { server: lob.label, count: lob.count ?? "", max: lob.max || "", map: lob.map, mode: lob.mode,
      admins: this.admins().map(([, p]) => p.name).join(", ") || "none", time: `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`, ...extra };
  }

  // inputs -----------------------------------------------------------------------------------------------------
  connection(server: string, up: boolean, label?: string): Firing[] {
    const lob = this.lobby(server, label), now = this.clock();
    let out: Firing[] = [];
    if (up && !lob.up) {
      if (lob.downTold) out = this.fire("server_up", server, this.context(server, { down: lob.label }));
      lob.downSince = null;
      lob.downTold = false;
    } else if (!up && lob.up) {
      lob.downSince = now;
      lob.roster = new Map();
      lob.sprees = new Map();
      lob.chains = new Map();
      lob.matches = new MatchEvents();
    }
    lob.up = up;
    return out;
  }

  status(server: string, s: Dict): Firing[] {
    const lob = this.lobby(server);
    let out: Firing[] = [];
    if (!s || typeof s !== "object") return out;
    const before = lob.count;
    const n = s.players, mx = s.max_players;
    lob.count = isCount(n) ? n : lob.count;
    lob.max = isCount(mx) && mx > 0 ? mx : lob.max;
    lob.map = text(s.map);
    lob.mode = text(s.mode);
    const phase = s.phase;
    if (typeof phase === "string" && phase !== lob.phase) {
      if (phase === "playing" || lob.phase === "playing") {
        lob.sprees = new Map(); // sprees end with the game
        lob.chains = new Map();
      }
      if (phase === "playing" && !["round_over", "between_rounds", "playing"].includes(lob.phase)) {
        lob.tally = new Map(); // a new match
        lob.blooded = false;
      }
      lob.phase = phase;
    }
    for (const ev of lob.matches.observe(s)) out = out.concat(this.fire(ev === "after_match" ? "match_end" : ev, server, this.context(server)));
    if (before !== null && lob.count !== null && lob.count !== before) {
      const ctx = this.context(server);
      if (lob.max && lob.count >= lob.max && lob.max > before) out = out.concat(this.fire("full", server, ctx));
      if (lob.count === 0 && before > 0) out = out.concat(this.fire("empty", server, ctx));
      out = out.concat(this.fire("players_above", server, ctx, { crossed: [before, lob.count] }), this.fire("players_below", server, ctx, { crossed: [before, lob.count] }));
    }
    const vote = s.vote;
    if (vote && typeof vote === "object" && vote.id !== undefined && vote.id !== null && vote.id !== lob.voteId) {
      const seen = Array.isArray(lob.voteId) && this.clock() - lob.voteId[1] < 60;
      lob.voteId = vote.id;
      if (seen) return out; // the event already announced this ballot
      out = out.concat(this.voteStarted(server, String(vote.kind ?? vote.subject ?? ""), vote.target, vote.target_id, ""));
    } else if (!vote) {
      lob.voteId = null;
    }
    return out;
  }

  players(server: string, players: unknown): void {
    const lob = this.lobby(server);
    if (!Array.isArray(players)) return;
    const roster = new Map<string, Who>();
    for (const p of players.slice(0, 128)) {
      if (!p || typeof p !== "object") continue;
      const ident = pick(p, "engine_id", "number"), name = text(pick(p, "name"));
      if (ident === undefined || !name) continue;
      const id = String(pick(p, "player_id", "id") ?? "");
      const ids = new Set<string>();
      for (const [k, v] of Object.entries(p)) if (/(^|_)(id|xuid|uid|guid|uuid)$/i.test(k)) for (const f of idForms(v)) ids.add(f);
      roster.set(String(ident), { name, id, ids, team: pick(p, "team"), admin: (p as Dict).admin });
    }
    for (const gone of lob.roster.keys()) if (!roster.has(gone)) {
      lob.sprees.delete(gone);
      lob.chains.delete(gone);
    }
    lob.roster = roster;
    if (lob.count === null) lob.count = roster.size;
  }

  /** A roster entry by engine id, player ID or name. */
  find(lob: Lobby, value: unknown): [string, Who] | null {
    if (value === undefined || value === null || value === "") return null;
    const v = String(value);
    const direct = lob.roster.get(v);
    if (direct) return [v, direct];
    for (const [k, p] of lob.roster) if (v === p.id || v === p.name) return [k, p];
    const forms = idForms(value);
    if (forms.size) for (const [k, p] of lob.roster) for (const f of forms) if (p.ids.has(f)) return [k, p];
    return null;
  }

  event(server: string, ev: Dict): Firing[] {
    const lob = this.lobby(server), kind = ev.event;
    if (kind === "lagged" || ev.type === "lagged") {
      lob.sprees = new Map();
      lob.chains = new Map();
      return [];
    }
    if (kind === "kill") return this.kill(server, ev);
    const pidOf = () => String(pick(ev, "player_id", "id") ?? "");
    if (kind === "join") return this.fire("join", server, this.context(server, { player: text(ev.name) }), { player_id: pidOf() });
    if (kind === "leave") {
      const pid = pidOf(), name = text(ev.name);
      const hit = this.find(lob, pid || name);
      if (hit) {
        lob.sprees.delete(hit[0]);
        lob.chains.delete(hit[0]);
      }
      return this.fire("leave", server, this.context(server, { player: name }), { player_id: pid });
    }
    if (kind === "chat") {
      const msg = String(ev.text ?? "").trim();
      if (ev.channel === "server" || !msg.startsWith("!")) return []; // ordinary chat, and the server's own lines, never trigger anything
      const [word, ...rest] = msg.split(/\s+/);
      const name = text(ev.name);
      let pid = pidOf();
      if (!pid) pid = this.find(lob, name)?.[1].id ?? "";
      return this.fire("command", server, this.context(server, { player: name, args: clean(rest.join(" ")).slice(0, 120) }), { player_id: pid, command: word.toLowerCase() });
    }
    if (kind === "medal" || kind === "award") {
      const medal = text(ev.medal ?? ev.award);
      const hit = this.find(lob, pick(ev, "player_id", "id", "name"));
      if (!medal) return [];
      const name = hit ? hit[1].name : text(ev.name);
      const title = medal.replace(/\w\S*/g, (w) => w[0].toUpperCase() + w.slice(1).toLowerCase());
      return this.fire("medal", server, this.context(server, { player: name, medal: title, kills: "" }), { player_id: hit?.[1].id ?? "", medal });
    }
    if (kind === "vote") {
      const msg = String(ev.text ?? "");
      const m = /called a vote to (\w+)(?: (.+))?/.exec(msg);
      if (ev.action === "start" || m) {
        return this.voteStarted(server, String(ev.vote_type ?? m?.[1] ?? ""), ev.target ?? m?.[2] ?? null, ev.target_id, text(ev.name ?? ev.caller), true);
      }
    }
    return [];
  }

  voteStarted(server: string, kind: string, target: unknown, targetId: unknown, caller: string, viaEvent = false): Firing[] {
    const lob = this.lobby(server);
    if (viaEvent) lob.voteId = ["event", this.clock()]; // the status poll's copy of this ballot must not fire it again
    kind = kind.toLowerCase().replace(/_/g, "");
    kind = ({ endmatch: "endgame", endmap: "endgame" } as Dict)[kind] ?? kind;
    const hit = this.find(lob, targetId) ?? this.find(lob, target);
    const tname = hit ? hit[1].name : text(target);
    return this.fire("vote", server, this.context(server, { player: caller, target: tname }), { vote: kind, player_id: hit?.[1].id ?? "", role_subject: "target" });
  }

  kill(server: string, ev: Dict): Firing[] {
    const lob = this.lobby(server), now = this.clock();
    let out: Firing[] = [];
    if (!["", "playing", "in_game"].includes(lob.phase)) return out;
    const killer = this.find(lob, ev.killer), victim = this.find(lob, ev.victim);
    let ended = 0;
    if (victim) {
      ended = lob.sprees.get(victim[0]) ?? 0;
      lob.sprees.set(victim[0], 0);
      lob.chains.delete(victim[0]);
      const t = lob.tally.get(victim[0]) ?? [0, 0];
      t[1]++;
      lob.tally.set(victim[0], t);
    }
    if (!killer || (victim && killer[0] === victim[0]) || ev.suicide === true) return out;
    const weapon = text(ev.weapon);
    const [k, p] = killer;
    if (ev.betrayal === true || ev.teamkill === true || (victim && p.team !== undefined && p.team !== null && p.team === victim[1].team))
      return this.fire("betrayal", server, this.context(server, { player: p.name, weapon, victim: victim ? victim[1].name : "" }), { player_id: p.id }); // betrayals earn nothing else
    const t = lob.tally.get(k) ?? [0, 0];
    lob.tally.set(k, t);
    t[0]++;
    const kd = t[0] / Math.max(1, t[1]);
    const common = { player: p.name, weapon, victim: victim ? victim[1].name : "", kd: kd.toFixed(2) };
    if (weapon) out = out.concat(this.fire("weapon_kill", server, this.context(server, { kills: t[0], ...common }), { player_id: p.id, weapon }));
    if (!lob.blooded) {
      lob.blooded = true;
      out = out.concat(this.fire("first_blood", server, this.context(server, { kills: 1, ...common }), { player_id: p.id }));
    }
    out = out.concat(this.fire("kills_reached", server, this.context(server, { kills: t[0], ...common }), { player_id: p.id, count: t[0] }));
    const n = (lob.sprees.get(k) ?? 0) + 1;
    lob.sprees.set(k, n);
    const [prevChain, last] = lob.chains.get(k) ?? [0, -Infinity];
    const chained = now - last <= CHAIN ? prevChain + 1 : 1;
    lob.chains.set(k, [chained, now]);
    if (chained >= 2) {
      const medal = MULTIS[Math.min(chained, 10)];
      const ctx = this.context(server, { medal, kills: chained, ...common });
      out = out.concat(this.fire("multikill", server, ctx, { player_id: p.id, count: chained }), this.fire("medal", server, ctx, { player_id: p.id, medal }));
    }
    if (n >= 2) { // each rule picks its own counts (whenMatches)
      const medal = SPREES[n] ?? `${n}-kill spree`;
      const ctx = this.context(server, { medal, kills: n, ...common });
      out = out.concat(this.fire("spree", server, ctx, { player_id: p.id, count: n }));
      if (SPREES[n]) out = out.concat(this.fire("medal", server, ctx, { player_id: p.id, medal }));
    }
    if (ended >= 5) out = out.concat(this.fire("medal", server, this.context(server, { medal: "Killjoy", kills: ended, ...common }), { player_id: p.id, medal: "Killjoy" }));
    return out;
  }

  /** The server's latest minute (health.frames): fires the frame-rate triggers on a crossing. */
  frameRate(server: string, fps: number | null | undefined): Firing[] {
    const lob = this.lobby(server);
    if (fps === null || fps === undefined || !Number.isFinite(fps)) return [];
    const before = lob.fps;
    lob.fps = Number(fps);
    const ctx = this.context(server, { fps: lob.fps.toFixed(0) });
    return this.fire("fps_below", server, ctx, { fps: [before, lob.fps] }).concat(this.fire("fps_recovered", server, ctx, { fps: [before, lob.fps] }));
  }

  /** Timers and outages: call every couple of seconds. */
  tick(): Firing[] {
    const now = this.clock();
    let out: Firing[] = [];
    for (const [key, lob] of this.lobbies) {
      if (lob.up || lob.downSince === null || lob.downTold) continue;
      for (const rule of this.rules) {
        if (rule.enabled && rule.when.type === "server_down" && now - lob.downSince >= rule.when.seconds) {
          lob.downTold = true;
          out = out.concat(this.fire("server_down", key, this.context(key, { down: lob.label }), { only: rule }));
        }
      }
    }
    const prev = this.lastTick;
    this.lastTick = now;
    if (prev !== null) {
      for (const rule of this.rules) {
        const w = rule.when;
        if (!rule.enabled || (w.type !== "every" && w.type !== "at")) continue;
        let due: boolean;
        if (w.type === "every") {
          const period = w.minutes * 60;
          due = Math.floor(now / period) !== Math.floor(prev / period);
        } else {
          const a = zoned(now * 1000, w.timezone), b = zoned(prev * 1000, w.timezone);
          due = a.key !== b.key && cronDue(w.cron, a);
        }
        if (!due) continue;
        const targets = rule.servers === "all" ? [...this.lobbies].filter(([, l]) => l.up).map(([k]) => k) : rule.servers.filter((k: string) => this.lobbies.get(k)?.up);
        for (const server of targets) out = out.concat(this.fire(w.type, server, this.context(server), { only: rule }));
      }
    }
    return out;
  }

  // matching ---------------------------------------------------------------------------------------------------
  whenMatches(w: Dict, f: Dict): boolean {
    switch (w.type) {
      case "medal": return w.medal === "any" || String(f.medal ?? "").toLowerCase() === w.medal.toLowerCase();
      case "spree": return w.counts.split(",").map(Number).includes(f.count);
      case "multikill": return (f.count ?? 0) >= w.min;
      case "command": return f.command === w.command;
      case "weapon_kill": return String(f.weapon ?? "").toLowerCase().includes(w.weapon.toLowerCase());
      case "kills_reached": return f.count === w.count;
      case "fps_below": case "fps_recovered": {
        const [before, now] = f.fps ?? [null, null];
        if (now === null || now === undefined) return false;
        if (w.type === "fps_below") return now < w.fps && (before === null || before >= w.fps);
        return before !== null && before < w.fps && w.fps <= now;
      }
      case "vote": return w.kind === "any" || f.vote === w.kind;
      case "players_above": case "players_below": {
        const [before, now] = f.crossed ?? [null, null];
        if (before === null || now === null) return false;
        return w.type === "players_above" ? before < w.count && w.count <= now : before >= w.count && w.count > now;
      }
      default: return true;
    }
  }

  condition(c: Dict, server: string, f: Dict): boolean {
    const lob = this.lobby(server);
    switch (c.type) {
      case "players_between": return lob.count !== null && c.min <= lob.count && lob.count <= c.max;
      case "admin_online": return this.admins(this.scope(c.where, server)).length > 0;
      case "no_admin_online": return this.admins().length === 0;
      case "player_role": {
        const roles = this.roleOf(f.player_id);
        return c.role === "none" ? roles.length === 0 : roles.includes(c.role);
      }
      case "player_kd": {
        const hit = this.find(lob, f.player_id);
        const [k, d] = (hit && lob.tally.get(hit[0])) || [0, 0];
        return k >= c.min_kills && k / Math.max(1, d) >= Number(c.min_kd);
      }
      case "day_of_week": return c.days.split(",").includes(DAYS[(zoned(this.clock() * 1000, c.timezone).dow + 6) % 7]);
      case "fps_under": return lob.fps !== null && lob.fps < c.fps;
      case "server_players": {
        const o = this.lobby(c.server);
        return o.up && o.count !== null && c.min <= o.count && o.count <= c.max;
      }
      case "map_is": return lob.map.toLowerCase().includes(c.text.toLowerCase());
      case "mode_is": return lob.mode.toLowerCase().includes(c.text.toLowerCase());
      case "time_between": {
        const now = zoned(this.clock() * 1000, c.timezone).hm;
        return c.start <= c.end ? c.start <= now && now <= c.end : now >= c.start || now <= c.end;
      }
      case "server_state": {
        const o = this.lobby(c.server);
        return { up: o.up, down: !o.up, full: Boolean(o.up && o.max && o.count !== null && o.count >= o.max), empty: o.up && o.count === 0 }[c.state as "up"] ?? false;
      }
    }
    return false;
  }

  fire(trigger: string, server: string, ctx: Dict, facts: Dict = {}): Firing[] {
    const now = this.clock(), out: Firing[] = [];
    for (const rule of facts.only ? [facts.only as Dict] : this.rules) {
      if (!rule.enabled || rule.when.type !== trigger) continue;
      if (rule.servers !== "all" && !rule.servers.includes(server)) continue;
      if (!this.whenMatches(rule.when, facts)) continue;
      if (!rule.if.every((c: Dict) => this.condition(c, server, facts))) continue;
      const key = `${rule.id}\u0000${server}\u0000${facts.player_id ?? ""}`;
      if (rule.cooldown && now - (this.fired.get(key) ?? -Infinity) < rule.cooldown) continue;
      const hour = this.hourly.get(rule.id) ?? [];
      this.hourly.set(rule.id, hour);
      while (hour.length && now - hour[0] > 3600) hour.shift();
      if (hour.length >= rule.per_hour) continue;
      this.fired.set(key, now);
      hour.push(now);
      out.push({ rule, server, ctx: { ...ctx, _player_id: facts.player_id ?? "" } });
    }
    if (this.fired.size > 5000) for (const [k, t] of this.fired) if (now - t >= 86400) this.fired.delete(k);
    return out;
  }
}

// --- what a firing does -----------------------------------------------------------------------------------------
/** One concrete thing to do: an RCON command on a server, a wait, or a Discord post. */
export interface Step { kind: "rcon" | "discord" | "wait"; server?: string; args?: string[]; webhook?: string; text?: string; seconds?: number }

/** What a step says it does, in words. */
export function stepWords(e: Engine, s: Step): string {
  if (s.kind === "discord") return `Post to Discord (${s.webhook}): ${s.text}`;
  if (s.kind === "wait") return `Wait ${s.seconds} s`;
  const at = e.name(s.server!), [cmd, ...rest] = s.args!;
  if (cmd === "say") return `Say on ${at}: ${rest[0]}`;
  if (cmd === "tell") return `Tell ${s.text} on ${at}: ${rest[1]}`;
  return ({ cancelvote: `Cancel the vote on ${at}`, shuffle: `Shuffle teams on ${at}` } as Dict)[cmd] ?? `${cmd} on ${at}`;
}

/** The steps a firing turns into, given who's where right now. Nothing is sent here: TEST shows this list. */
export function plan(e: Engine, f: Firing): Step[] {
  const steps: Step[] = [], here = f.server;
  for (const a of f.rule.then) {
    const msg = "message" in a ? fill(a.message, f.ctx) : "";
    switch (a.type) {
      case "say": {
        const targets = a.to === "this" ? [here] : [...e.lobbies].filter(([n, l]) => l.up && (a.to === "all" || n !== here)).map(([n]) => n);
        for (const n of targets) steps.push({ kind: "rcon", server: n, args: ["say", msg] });
        break;
      }
      case "say_on": steps.push({ kind: "rcon", server: a.server, args: ["say", msg] }); break;
      case "tell_player": if (f.ctx._player_id) steps.push({ kind: "rcon", server: here, args: ["tell", f.ctx._player_id, msg], text: String(f.ctx.player ?? "") }); break;
      case "tell_admins":
        for (const [server, p] of e.admins(a.where === "any" ? undefined : a.where === "this" ? new Set([here]) : new Set([...e.lobbies.keys()].filter((k) => k !== here))))
          if (p.id) steps.push({ kind: "rcon", server, args: ["tell", p.id, msg], text: p.name });
        break;
      case "tell_role": for (const [server, p] of e.withRole(a.role)) if (p.id) steps.push({ kind: "rcon", server, args: ["tell", p.id, msg], text: p.name }); break;
      case "cancel_vote": steps.push({ kind: "rcon", server: here, args: ["cancelvote"] }); break;
      case "shuffle": steps.push({ kind: "rcon", server: here, args: ["shuffle"] }); break;
      case "discord": steps.push({ kind: "discord", webhook: a.webhook, text: msg }); break;
      case "wait": steps.push({ kind: "wait", seconds: a.seconds }); break;
    }
  }
  return steps;
}

/** A made-up firing for TEST: the rule's trigger with example facts on a real server, ignoring its conditions, cooldown and limits. */
export function sample(e: Engine, rule: Dict, server: string): Firing {
  const w = rule.when;
  const ctx = e.context(server, { player: "ExamplePlayer", args: "help please", target: "ExampleTarget", down: e.name(server) });
  if (w.type === "spree") {
    const n = Number(w.counts.split(",")[0]);
    Object.assign(ctx, { medal: SPREES[n] ?? `${n}-kill spree`, kills: n });
  } else if (w.type === "multikill") Object.assign(ctx, { medal: MULTIS[w.min], kills: w.min });
  else if (w.type === "medal") Object.assign(ctx, { medal: w.medal === "any" ? "Perfection" : w.medal, kills: "" });
  else if (["weapon_kill", "first_blood", "kills_reached", "betrayal"].includes(w.type)) Object.assign(ctx, { weapon: w.weapon ?? "sniper rifle", victim: "ExampleVictim", kills: w.count ?? 1, kd: "2.50" });
  else if (w.type === "fps_below" || w.type === "fps_recovered") ctx.fps = w.type === "fps_below" ? w.fps - 1 : w.fps;
  return { rule, server, ctx: { ...ctx, _player_id: "0".repeat(64) } };
}
