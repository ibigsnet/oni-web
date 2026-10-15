// The automation hub: the rules, what the picked one does in words, and what they did lately. Building, testing and the
// roles and webhooks go out to actions.ts and back. From ibigs/oni-rcon's automation_ui.py, by IBIGS.
import { useState } from "react";
import { AMBER, CYAN, DIM, GREEN, RED, WHITE } from "../art/palette";
import { oni, useV, type Dialog } from "../engine";
import { CATALOG, describe } from "../lib/rules";
import type { Dict } from "../lib/util";
import { Table, type Row } from "./ui";

const Keys = ({ hints }: { hints: string[] }) => <div className="keys">{hints.join("   ·   ")}</div>;
const clock = (t: number) => new Date(t * 1000).toTimeString().slice(0, 8);

/** Resolves {do: "new" | "template" | "edit" | "test" | "toggle" | "delete" | "roles" | "discord", id?} or null. */
export function AutomationHub({ d }: { d: Dialog }) {
  useV((s) => s.auto);
  const [sel, setSel] = useState<string | null>((d.props.sel as string | undefined) ?? null);
  const label = (k: string) => oni.stations.find((s) => s.where === k)?.label ?? k;
  const rules: Dict[] = oni.rules;
  const rule = rules.find((r) => r.id === sel);
  const rows: Row[] = rules.map((r) => ({
    id: r.id,
    cells: [[[r.enabled ? "ON" : "OFF", r.enabled ? GREEN : DIM]], [[r.name, r.enabled ? WHITE : DIM]], [[CATALOG.when[r.when.type]?.label ?? r.when.type, DIM]],
      [[r.servers === "all" ? "every server" : r.servers.map(label).join(", "), DIM]]],
  }));
  const go = (what: string) => d.resolve({ do: what, id: rule?.id });
  const vip = Object.values(oni.roles).some((p) => p.roles.includes("vip"));
  const recent = oni.runner.history.slice(-8).reverse();
  return (
    <div className="dialog wide">
      <div className="dt">AUTOMATION</div>
      <div style={{ color: DIM, marginBottom: 4 }}>
        {`Rules act through this console, so they run while it is open. ${rules.filter((r) => r.enabled).length} of ${rules.length} on`}
        {vip ? " · VIP kick protection is on" : ""}
        {Object.keys(oni.runner.webhooks).length ? "" : " · no Discord webhook yet"}
      </div>
      <div style={{ flex: 1, minHeight: 110, display: "flex", flexDirection: "column" }}>
        {rows.length ? <Table id="rules" cols={["", "RULE", "WHEN", "RUNS ON"]} rows={rows} selected={sel} onSelect={setSel} onEnter={() => go("edit")} />
          : <div style={{ color: AMBER }}>No rules yet: TEMPLATE starts from a ready-made one, NEW builds your own.</div>}
      </div>
      <div className="db" style={{ whiteSpace: "pre-wrap", minHeight: 78 }}>{rule ? describe(rule, label) : ""}</div>
      <div style={{ color: DIM }}>RECENT</div>
      <div style={{ minHeight: 60, fontSize: "0.92em" }}>
        {recent.length ? recent.map((n, i) => (
          <div key={i} style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            <span style={{ color: DIM }}>{clock(n.at)} </span><span style={{ color: CYAN }}>{n.name || n.rule}</span>
            <span style={{ color: DIM }}>{n.server ? ` · ${n.server}` : ""}  </span><span style={{ color: /fail|refus|could not|skipped/.test(n.result) ? RED : WHITE }}>{n.result}</span>
          </div>
        )) : <div style={{ color: DIM }}>Nothing has fired since the console opened.</div>}
      </div>
      <div className="btns">
        <button className="btn primary" onClick={() => go("new")}>NEW</button>
        <button className="btn" onClick={() => go("template")}>TEMPLATE</button>
        <button className="btn" disabled={!rule} onClick={() => go("edit")}>EDIT</button>
        <button className="btn" disabled={!rule} onClick={() => go("test")}>TEST</button>
        <button className="btn" disabled={!rule} onClick={() => go("toggle")}>{rule?.enabled ? "TURN OFF" : "TURN ON"}</button>
        <button className="btn error" disabled={!rule} onClick={() => go("delete")}>DELETE</button>
        <button className="btn" onClick={() => go("roles")}>ROLES</button>
        <button className="btn" onClick={() => go("discord")}>DISCORD</button>
        <button className="btn" onClick={() => d.resolve(null)}>CLOSE</button>
      </div>
      <Keys hints={["Esc close", "↑ ↓ pick a rule", "Enter edit"]} />
    </div>
  );
}
