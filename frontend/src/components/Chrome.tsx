// The frame around the tabs: the masthead with the condition, the alert banner, the key hints along the bottom, and
// the toasts.
import { AMBER, CYAN, DIM, INK, RED, WHITE, blend } from "../art/palette";
import { oni, useV } from "../engine";
import { hms, num } from "../lib/util";

export function Masthead({ width }: { width: number }) {
  useV((s) => s.mast);
  let [cond, color] = oni.condition();
  if (cond === "RED" && Date.now() % 2000 < 1000) color = blend(RED, INK, 0.55);
  const online = oni.stations.filter((s) => s.online).length, unseen = oni.unseen;
  const assets = oni.stations.filter((s) => s.online).reduce((a, s) => a + (num(s.data.status?.players) ?? 0), 0);
  const wide = width >= 1150;
  return (
    <div className="masthead">
      <span><span style={{ color: AMBER }}>▲ {wide ? "OFFICE OF NAVAL INTELLIGENCE" : "ONI"}</span>{width >= 1300 ? <span style={{ color: DIM }}>  ·  SECTION III</span> : null}</span>
      <span>
        <span className="cond" style={{ background: color }}>CONDITION {cond}{unseen ? ` · ⚑ ${unseen}` : ""}</span>
        <span style={{ color: online ? CYAN : RED }}>  {online}/{oni.stations.length} {wide ? "STATIONS SECURE" : "SECURE"}  ·  {assets} ASSETS</span>
      </span>
      <span className="r">
        {wide ? <span style={{ color: RED }}>TOP SECRET // </span> : null}
        <span style={{ color: WHITE }}>OPERATOR {oni.by.toUpperCase()}  </span><span style={{ color: DIM }}>{hms(new Date())}</span>
      </span>
    </div>
  );
}

export function Banner() {
  useV((s) => s.ui);
  const b = oni.banner;
  if (!b) return null;
  return (
    <div className={`banner ${b.warn ? "warn" : ""} ${Date.now() % 2000 < 1000 ? "dim" : ""}`} onClick={() => { oni.banner = null; useV.setState((s) => ({ ui: s.ui + 1 })); }}>
      {b.text}
    </div>
  );
}

const KEYS: Record<string, [string, string][]> = {
  assets: [["t", "Tell"], ["k", "Kick"], ["b", "Ban"], ["m", "Mute"], ["j", "Team"], ["v", "VPN allow"], ["y", "Copy ID"]],
  blacklist: [["n", "New ban"], ["u", "Unban"], ["a", "VPN allow"], ["r", "VPN revoke"]],
  forge: [["i", "Install"], ["l", "Load now"], ["a", "Ack"], ["s", "Sort"], ["w", "Window"], ["n", "More"], ["/", "Search"], ["k", "Key"], ["y", "Copy ID"]],
  intercepts: [["/", "Type"], ["End", "Follow"]],
  console: [["/", "Type"], ["↑↓", "History"]],
  operations: [], health: [],
};

export function Footer() {
  useV((s) => s.ui);
  const keys: [string, string][] = [...KEYS[oni.tab], ["F1–F7", "Tabs"], ["g", "Go to"], ["^B", "Broadcast"], ["^R", "Refresh"], ["x", "Redact"], ["?", "Help"]];
  return (
    <div className="footer">
      {keys.map(([k, v]) => <span key={k + v}><span className="k">{k}</span>{v}</span>)}
      <span className="sp" />
      <span><span className="k">^P</span>palette</span>
    </div>
  );
}

export function Toasts() {
  useV((s) => s.toasts);
  return (
    <div className="toasts">
      {oni.toasts.map((t) => (
        <div key={t.id} className={`toast ${t.severity}`} onClick={() => { oni.toasts = oni.toasts.filter((x) => x !== t); useV.setState((s) => ({ toasts: s.toasts + 1 })); }}>
          {t.title ? <div className="t">{t.title}</div> : null}
          <div>{t.text}</div>
        </div>
      ))}
    </div>
  );
}
