import { useEffect, useState, type FormEvent } from "react";
import { api, invoke } from "@web/bridge";
import { oni } from "../engine";
import type { Dict } from "../lib/util";
import { NativeSetup } from "../web/NativeSetup";

interface Saved {
  id: string; name: string; host: string; port: number; url: string; ssh: string;
  has_password: boolean; remember: boolean; connected: boolean;
}
const empty = { name: "", host: "127.0.0.1", port: "49176", password: "", url: "", ssh: "", remember: true };

export function Setup({ info, onDone }: { info: Dict; onDone: (demo: boolean, prepared?: Dict) => void }) {
  const [profiles, setProfiles] = useState<Saved[]>([]);
  const [form, setForm] = useState(empty);
  const [editing, setEditing] = useState<string | null>(null);
  const [by, setBy] = useState(info.setup?.by || "operator");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [allowDemo, setAllowDemo] = useState(false);
  const load = () => api<{ profiles: Saved[] }>("/api/profiles").then((r) => setProfiles(r.profiles));
  useEffect(() => {
    load().catch((e) => setError(String(e)));
    api<{ demo: boolean }>("/api/session").then((s) => setAllowDemo(s.demo)).catch(() => {});
  }, []);
  const field = (key: keyof typeof empty, value: string | boolean) => setForm((f) => ({ ...f, [key]: value }));
  const save = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      if (!editing && !form.password) throw new Error("Enter the RCON password");
      const body = { ...form, port: Number(form.port), password: form.password || null };
      await api(editing ? `/api/profiles/${editing}` : "/api/profiles", {
        method: editing ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      setForm(empty); setEditing(null);
      await load();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };
  const connect = async (id?: string) => {
    setBusy(true); setError("");
    try {
      const prepared = await invoke<Dict>("start_console", { demo: false, by, ids: id ? [id] : profiles.map((p) => p.id) });
      onDone(false, prepared);
    } catch (e) { setError(String(e)); setBusy(false); }
  };
  const remove = async (p: Saved) => {
    if (!(await oni.confirm("DELETE PROFILE", `Delete ${p.name || p.host}? Active fleet connections will close. The game server is unaffected.`, "DELETE"))) return;
    setBusy(true);
    try {
      await api(`/api/profiles/${p.id}`, { method: "DELETE" });
      if (editing === p.id) { setEditing(null); setForm(empty); }
      await load();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };
  return <main className="web-setup">
    <header className="setup-heading"><div><span className="eyebrow">SECTION THREE / NETWORK CONTROL</span>
      <h1>Your fleet.<br /><span>One command center.</span></h1>
      <p>Connect Project Reclaimer servers, monitor live traffic, and keep your operations in view.</p></div>
      <div className="setup-badge"><span>◈</span><b>ONI</b><small>REMOTE CONSOLE</small></div></header>
    <div className="setup-grid"><section className="setup-card">
      <div className="setup-section"><span>01</span><h2>{editing ? "Edit connection" : "Add a connection"}</h2><small>RECLAIMER RCON</small></div>
      <form onSubmit={save}>
        <label htmlFor="profile-name">Profile name <small>OPTIONAL</small></label>
        <input className="input" id="profile-name" value={form.name} onChange={(e) => field("name", e.target.value)} placeholder="Alpha · Big Team Battle" maxLength={100} />
        <div className="setup-address"><div><label htmlFor="host">Host</label>
          <input className="input" id="host" value={form.host} onChange={(e) => field("host", e.target.value)} required maxLength={253} autoComplete="off" /></div>
          <div><label htmlFor="port">RCON port</label><input className="input" id="port" type="number" min={1} max={65535} value={form.port} onChange={(e) => field("port", e.target.value)} required /></div></div>
        <label htmlFor="rcon-password">RCON password</label>
        <input className="input" id="rcon-password" type="password" autoComplete="new-password" value={form.password} onChange={(e) => field("password", e.target.value)}
          placeholder={editing ? "Leave blank to keep the current password" : "Server authentication password"} minLength={8} maxLength={4096} required={!editing} />
        <details><summary>Advanced connection settings</summary>
          <label htmlFor="rcon-url">WebSocket URL <small>OVERRIDES HOST / PORT</small></label>
          <input id="rcon-url" className="input" value={form.url} onChange={(e) => field("url", e.target.value)} placeholder="wss://rcon.example.org/slayer" maxLength={2048} />
          <label htmlFor="ssh-dest">SSH destination <small>KEY AUTHENTICATION</small></label>
          <input id="ssh-dest" className="input" value={form.ssh} onChange={(e) => field("ssh", e.target.value)} placeholder="operator@game-box" maxLength={253} />
        </details>
        <label className="setup-remember"><input type="checkbox" checked={form.remember} onChange={(e) => field("remember", e.target.checked)} />Remember password in encrypted storage</label>
        <div className="setup-actions"><button className="btn primary" disabled={busy}>{busy ? "WORKING…" : editing ? "SAVE CHANGES" : "+ SAVE PROFILE"}</button>
          {editing && <button className="btn" type="button" disabled={busy} onClick={() => { setEditing(null); setForm(empty); }}>CANCEL</button>}</div>
        <p className="setup-footnote">Passwords are sent to the gateway once and never returned. Unremembered passwords last until the backend restarts.</p>
      </form>
    </section><section className="setup-card">
      <div className="setup-section"><span>02</span><h2>Saved servers</h2><small>{String(profiles.length).padStart(2, "0")} PROFILES</small></div>
      <div className="setup-profiles">{profiles.length ? profiles.map((p) => <article key={p.id} className="setup-profile">
        <div className="profile-symbol" aria-hidden="true">⌘</div><div className="profile-info"><b>{p.name || p.host}</b><span>{p.url || `${p.host}:${p.port}`}</span>
          <small>{p.ssh ? `SSH · ${p.ssh}` : "DIRECT CONNECTION"} · {p.has_password ? "CREDENTIAL SET" : "PASSWORD NEEDED"}</small></div>
        <div className="profile-actions"><button disabled={busy} className="btn primary" onClick={() => connect(p.id)}>CONNECT</button>
          <button disabled={busy} className="btn" aria-label={`Edit ${p.name || p.host}`} onClick={() => { setEditing(p.id); setForm({ name: p.name || "", host: p.host, port: String(p.port), password: "", url: p.url || "", ssh: p.ssh || "", remember: p.remember }); }}>EDIT</button>
          <button disabled={busy} className="btn danger" aria-label={`Delete ${p.name || p.host}`} onClick={() => remove(p)}>×</button></div>
      </article>) : <div className="setup-empty"><span>⊞</span><h3>Ready for your first server</h3><p>Add a profile to the left, then connect.<br />Or explore a simulated fleet below.</p></div>}</div>
      <div className="setup-fleet"><label htmlFor="operator">Operator name <small>RECORDED IN SERVER ADMIN LOGS</small></label>
        <input id="operator" className="input" value={by} onChange={(e) => setBy(e.target.value)} maxLength={64} />
        <button className="btn primary" disabled={busy || !profiles.length || !by.trim()} onClick={() => connect()}>CONNECT ENTIRE FLEET →</button></div>
    </section></div>
    {error && <div className="web-error setup-error" role="alert">{error}</div>}
    <NativeSetup changed={load} />
    {allowDemo && <aside className="setup-demo"><div><b>Take the console for a spin.</b><p>Three simulated servers, live players and events, a local Forge catalog. No game server required.</p></div>
      <button className="btn" disabled={busy} onClick={() => { setBusy(true); onDone(true); }}>EXPLORE DEMO ↗</button></aside>}
    <footer className="setup-bottom"><span>● BACKEND-OWNED CONNECTIONS</span><span>ENCRYPTED PROFILES</span><span>PROJECT RECLAIMER · HALO 3</span></footer>
  </main>;
}
