// F1: the players on the selected server, and the file of the one picked.
import { useEffect, useState } from "react";
import { AMBER, DIM, GOLD, GREEN, RED, TEAM_COLOR, TEAMS, WHITE } from "../art/palette";
import { biosig } from "../art/archive";
import type { Pic } from "../art/pixels";
import { oni, SPREE, useV } from "../engine";
import { PLAYER_OPS, TIPS, player } from "../actions";
import { bar, num, pick, pingCell, redactAddr, redactData, sides, splitBar, targetOf, teamOf, type Dict, type Line } from "../lib/util";
import { Btn, Json, L, Panel, Pixel, Table, type Row } from "./ui";

export const assetsUi = { selected: null as string | null, want: null as string | null, rows: new Map<string, Dict>() };

function teamStrip(): Line | null {
  const teams = sides(oni.cur.players);
  if (teams.length < 2) return null;
  const parts: [number, string][] = teams.map(([t, , s]) => [Math.max(s, 0), TEAM_COLOR[t] ?? WHITE]);
  if (teams.length === 2) {
    const [[a, , sa], [b, , sb]] = teams;
    return [[` ${a.toUpperCase()} ${sa} `, parts[0][1]], ...splitBar(parts, 24), [` ${sb} ${b.toUpperCase()} `, parts[1][1]]];
  }
  const out: Line = [[" "]];
  teams.forEach(([t, , s], i) => out.push(...(i ? [["  "]] as Line : []), [`${t.toUpperCase()} ${s}`, parts[i][1]]));
  return [...out, [" "], ...splitBar(parts, 24), [" "]];
}

export function playerRows(): Row[] {
  const st = oni.cur, now = performance.now();
  const order = (p: Dict) => { const t = teamOf(p); return TEAMS.includes(t) ? TEAMS.indexOf(t) : TEAMS.length; };
  const players = [...st.players].sort((a, b) => order(a) - order(b) || teamOf(a).localeCompare(teamOf(b)) || (num(pick(b, "score")) ?? 0) - (num(pick(a, "score")) ?? 0));
  assetsUi.rows = new Map();
  const rows = players.map((p, i) => {
    const team = teamOf(p), name = String(pick(p, "name") ?? "?");
    const k = pick(p, "kills"), d = pick(p, "deaths");
    const kd = typeof k === "number" && typeof d === "number" ? (k / Math.max(d, 1)).toFixed(2) : "—";
    const flags: Line = [];
    const spree = st.medals.spree.get(name) ?? 0;
    if (spree >= SPREE) flags.push([`★${spree} `, GOLD]);
    if (p.admin) flags.push(["ADM ", AMBER]);
    if (p.muted) flags.push(["MUT ", RED]);
    if (p.alive === false) flags.push(["KIA ", DIM]);
    let key = targetOf(p);
    if (assetsUi.rows.has(key)) key = `${key}~${i}`;
    assetsUi.rows.set(key, p);
    if (!st.firstSeen.has(key)) st.firstSeen.set(key, st.painted ? now : -1e9);
    if (now - st.firstSeen.get(key)! < 12_000) flags.push(["NEW", GREEN]);
    return { id: key, cells: [String(pick(p, "number") ?? ""), [[name, TEAM_COLOR[team] ?? WHITE]], [[team.toUpperCase() || "—", TEAM_COLOR[team] ?? DIM]],
      String(pick(p, "score") ?? "—"), String(k ?? "—"), String(d ?? "—"), kd, pingCell(st.pings.get(String(pick(p, "player_id", "id")))),
      bar(p.health), bar(p.shields), flags] as Row["cells"] };
  });
  st.painted ||= "players" in st.data; // everyone here at the first look isn't news
  return rows;
}

function Glyph({ seed, color, size }: { seed: string; color: string; size: number }) {
  const [pic, setPic] = useState<Pic | null>(null);
  useEffect(() => {
    let live = true;
    biosig(seed, color, 12, size).then((p) => live && setPic(p));
    return () => { live = false; };
  }, [seed, color, size]);
  return <Pixel pic={pic} width={size} />;
}

function Dossier({ p }: { p: Dict | undefined }) {
  const st = oni.cur;
  if (!p) {
    return (
      <div style={{ textAlign: "center", marginTop: 40 }}>
        {st.online ? (
          <><div style={{ color: DIM }}>NO ASSETS IN THEATRE</div><br /><div style={{ color: DIM }}>Nobody is playing on this server right now.<br />Players appear here as they join.</div></>
        ) : (
          <><div style={{ color: RED }}>STATION OFFLINE</div><br /><div>{oni.why(st, false)}</div><br /><div style={{ color: DIM }}>It retries by itself. To retry now: F3, then RECONNECT.</div></>
        )}
      </div>
    );
  }
  const team = teamOf(p), name = String(pick(p, "name") ?? "?"), color = TEAM_COLOR[team] ?? WHITE;
  const k = pick(p, "kills"), d = pick(p, "deaths");
  const spree = st.medals.spree.get(name) ?? 0, earned = st.medals.earned.get(name);
  const ms = st.pings.get(String(pick(p, "player_id", "id")));
  const guests = pick(p, "guest_players", "guests");
  const last = pick(p, "seconds_since_last_death");
  const head: [string, Line | string][] = [
    ["CALLSIGN", [[name, color]]], ["SERVICE TAG", String(pick(p, "service_tag", "tag") ?? "—")], ["TEAM", [[team.toUpperCase() || "—", TEAM_COLOR[team] ?? DIM]]],
    ["STATUS", [p.alive ? "ALIVE" : p.alive === false ? "KIA" : "—", ...(p.admin ? ["ADMIN"] : []), ...(p.muted ? ["MUTED"] : [])].join(" · ")],
    ["SCORE", `${pick(p, "score") ?? "—"}    K ${k ?? "—"} / D ${d ?? "—"}`],
    ["STREAK", spree >= SPREE ? [[`★ ${spree} without dying`, GOLD]] : spree ? `${spree} without dying` : [["—", DIM]]],
  ];
  const rows: [string, Line | string][] = [
    ["PLAYER ID", String(pick(p, "player_id", "id") ?? "—")],
    ["ADDRESS", [[redactAddr(pick(p, "address", "ip"), oni.redact), oni.redact ? RED : WHITE]]],
    ["JOIN PING", ms !== undefined ? [...pingCell(ms), [" ms", DIM]] : [["—", DIM]]],
    ["HEALTH", bar(p.health, 14)], ["SHIELDS", bar(p.shields, 14)],
    ["LAST DEATH", typeof last === "number" ? `${last}s ago` : "—"],
    ...(guests ? [["GUESTS", String(Array.isArray(guests) ? guests.length : guests)] as [string, string]] : []),
    ...(earned?.size ? [["MEDALS", [[[...earned].sort((a, b) => b[1] - a[1]).map(([m, n]) => (n > 1 ? `${m} ×${n}` : m)).join(" · "), GOLD]]] as [string, Line]] : []),
  ];
  const facts = (rs: [string, Line | string][]) => (
    <div className="facts">{rs.flatMap(([a, b]) => [<span key={a}>{a}</span>, <span key={a + "v"} className="sel-text">{typeof b === "string" ? b : <L line={b} />}</span>])}</div>
  );
  return (
    <>
      <div><span style={{ color: AMBER }}>PERSONNEL FILE</span><span style={{ color: RED }}>  //  CLASSIFIED</span></div><br />
      <div style={{ display: "flex", gap: 22, alignItems: "center", flexWrap: "wrap" }}>
        <Glyph seed={String(pick(p, "player_id", "id") ?? name)} color={color} size={120} />
        {facts(head)}
      </div><br />
      {facts(rows)}
      <div className="grid4" style={{ margin: "16px 0" }}>
        {PLAYER_OPS.map(([op, label]) => (
          <Btn key={op} label={op === "mute" && p.muted ? "UNMUTE" : op === "admin" && p.admin ? "REMOVE ADMIN" : label} variant={op === "kick" || op === "ban" ? "error" : ""} title={TIPS[`pl-${op}`]} onClick={() => player(op, p)} />
        ))}
      </div>
      <details style={{ borderTop: "1px solid var(--line)", paddingTop: 6 }}>
        <summary style={{ color: DIM, cursor: "pointer" }}>RAW DATA</summary>
        <Json value={redactData(p, oni.redact)} />
      </details>
    </>
  );
}

export function Assets() {
  useV((s) => s.roster);
  const [sel, setSel] = useState<string | null>(assetsUi.want ?? assetsUi.selected);
  const rows = playerRows();
  useEffect(() => { // a player picked from FIND PLAYER, once their server's roster is showing
    if (assetsUi.want && rows.some((r) => r.id === assetsUi.want)) { setSel(assetsUi.want); assetsUi.want = null; }
  });
  const st = oni.cur;
  assetsUi.selected = rows.some((r) => r.id === sel) ? sel : rows[0]?.id ?? null;
  const mx = st.data.players?.max_players ?? st.data.status?.max_players;
  const strip = teamStrip();
  const select = (id: string) => { assetsUi.selected = id; setSel(id); };
  return (
    <div className="hsplit grow">
      <Panel title={`ASSETS IN THEATRE · ${st.players.length}/${mx ?? "?"}`} sub={strip ? <L line={strip} /> : null} className="grow" style={{ flex: 3 }}>
        <Table id="players" cols={["#", "CALLSIGN", "TEAM", "SCORE", "K", "D", "K/D", "PING", "HEALTH", "SHIELD", "FLAGS"]} rows={rows}
          selected={assetsUi.selected} onSelect={select} />
      </Panel>
      <Panel title="DOSSIER" style={{ flex: 2, minWidth: 380 }}>
        <Dossier p={assetsUi.selected ? assetsUi.rows.get(assetsUi.selected) : undefined} />
      </Panel>
    </div>
  );
}
