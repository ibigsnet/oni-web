// The Superintendent's findings: what it has noticed in the last hour, with the evidence behind each. Read-only; nothing
// here moderates anyone. From ibigs/oni-rcon's finding_view.py, by IBIGS.
import { useEffect, useState } from "react";
import { AMBER, CYAN, DIM, WHITE } from "../art/palette";
import { oni, useV, type Dialog } from "../engine";
import { CHAT_BEFORE, CHAT_LIMIT, type ChatContext, type Finding, type Subject } from "../lib/findings";
import { redactText } from "../lib/util";
import { Table, type Row } from "./ui";

const clock = (at: number) => new Date(at * 1000).toTimeString().slice(0, 8);

export function chatStatus(ctx: ChatContext | null, now = Date.now() / 1000): string {
  if (!ctx) return "No surrounding chat was captured for this older finding";
  if (ctx.status === "collecting") return now < ctx.until ? `Collecting follow-up chat until ${clock(ctx.until)}` : "Follow-up window ended";
  return { complete: "Follow-up window ended", limited: "Follow-up message limit reached", interrupted: "Capture interrupted: some conversation may be missing" }[ctx.status];
}

export const stationOf = (f: Finding) => oni.stations[Number(f.server)];

/** What the dialog resolves with: the server to open, and the player to show there if one was picked. */
export interface Opened { index: number; subject?: Subject }

export function FindingsView({ d }: { d: Dialog }) {
  useV((s) => s.ui + s.cards);
  const [, tick] = useState(0);
  const [sel, setSel] = useState<string | null>(null);
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const red = (s: string) => redactText(s, oni.redact);
  const all = oni.findings.recent(), ack = oni.findings.acknowledgedAt;
  const row = all.find((f) => f.id === sel);
  const label = (f: Finding) => stationOf(f)?.label ?? `Server ${f.server}`;
  const rows: Row[] = all.map((f) => ({
    id: f.id,
    cells: [[[clock(f.at), DIM]], [[f.level === "review" ? "REVIEW NEEDED" : "HIGHLIGHT", f.level === "review" ? AMBER : CYAN]],
      [[red(label(f) + (f.at <= ack ? " · acknowledged" : "")), WHITE]], [[red(f.title), WHITE]]],
  }));
  const online = oni.stations.filter((s) => s.online).length;
  const go = (subject?: Subject) => row && d.resolve({ index: Number(row.server), subject } satisfies Opened);
  const who = (p: Subject) => red(p.name || "Unknown player");
  const ctx = row?.chat ?? null;
  return (
    <div className="dialog wide">
      <div className="dt">SUPERINTENDENT · RECENT FINDINGS</div>
      <div style={{ color: DIM, marginBottom: 6 }}>
        {`Live events: ${online}/${oni.stations.length} servers connected. Observations run while this console is open. Perfection appears only if the server supplies an explicit medal event.`}
      </div>
      <div style={{ flex: 1, minHeight: 90, display: "flex", flexDirection: "column" }}>
        {rows.length ? <Table id="findings-list" cols={["TIME", "LEVEL", "SERVER", "FINDING"]} rows={rows} selected={sel} onSelect={setSel} />
          : <div style={{ color: DIM }}>No findings observed yet. Missing telemetry does not mean all is quiet.</div>}
      </div>
      <div style={{ flex: 1, minHeight: 90, overflowY: "auto", borderTop: "1px solid var(--line)", marginTop: 8, paddingTop: 8, whiteSpace: "pre-wrap" }}>
        {row ? (
          <>
            <div style={{ color: DIM }}>{red(row.evidence)}</div>
            <div style={{ color: DIM }}>{`Source: ${row.source} · Review only; no automatic moderation.`}</div>
            <div style={{ margin: "6px 0", display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button className="btn" onClick={() => go()}>OPEN SERVER</button>
              {row.subjects.slice(0, 4).map((p, i) => <button key={i} className="btn" onClick={() => go(p)}>{`PLAYER: ${who(p)}`}</button>)}
              {row.reporter ? <button className="btn" onClick={() => go(row.reporter!)}>{`REPORTER: ${who(row.reporter)}`}</button> : null}
            </div>
            {row.level === "review" ? (
              <>
                <div style={{ color: WHITE }}>{`PLAYER CHAT · up to ${CHAT_LIMIT} before / ${CHAT_LIMIT} after · ${CHAT_BEFORE / 60} min either side`}</div>
                <div style={{ color: ctx?.status === "interrupted" ? AMBER : DIM }}>{chatStatus(ctx)}</div>
                {ctx && ctx.since > row.at - CHAT_BEFORE ? <div style={{ color: DIM }}>{`Earlier context is partial: this feed began at ${clock(ctx.since)}`}</div> : null}
                {ctx?.before_limited ? <div style={{ color: DIM }}>{`Busy chat: showing the last ${CHAT_LIMIT} messages before the finding.`}</div> : null}
                {ctx && !ctx.messages.length ? <div style={{ color: DIM }}>No player messages captured in this window.</div> : null}
                {ctx?.messages.map((m, i) => (
                  <div key={i} style={{ color: m.position === "trigger" ? AMBER : WHITE }}>
                    <span style={{ color: DIM }}>{`${clock(m.at)} `}</span>
                    <span style={{ color: m.position === "trigger" ? AMBER : DIM }}>{m.position?.toUpperCase()}</span>
                    <span style={{ color: DIM }}>{` [${red(m.channel)}] `}</span>
                    {`${who(m.speaker)}: ${red(m.text)}`}
                  </div>
                ))}
              </>
            ) : null}
          </>
        ) : null}
      </div>
      <div className="btns">
        <button className="btn" disabled={!all.length} onClick={() => { oni.acknowledgeFindings(); tick((n) => n + 1); }}>CLEAR ALL</button>
        <button className="btn primary" onClick={() => d.resolve(null)}>CLOSE</button>
      </div>
      <div className="keys">Esc close   ·   ↑ ↓ review   ·   CLEAR ALL acknowledges every alert</div>
    </div>
  );
}
