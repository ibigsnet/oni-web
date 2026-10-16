import { useEffect, useState, type FormEvent } from "react";
import { App } from "../App";
import { oni } from "../engine";
import { api, closeEvents, signOut } from "./bridge";
import { ServerUploadButton } from "./ServerUpload";
import "./web.css";

export function Session() {
  const [auth, setAuth] = useState<boolean | null>(null);
  const [required, setRequired] = useState(false);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [stream, setStream] = useState({ connected: false, detail: "" });
  const [revision, setRevision] = useState(0);
  const check = () => api<{ authenticated: boolean; login_required: boolean }>("/api/session")
    .then((s) => { setAuth(s.authenticated); setRequired(s.login_required); })
    .catch((e) => { setError(String(e)); setAuth(false); });
  useEffect(() => {
    check();
    const logout = () => { oni.stop(); closeEvents(); setAuth(false); setToken(""); };
    const status = (e: Event) => setStream((e as CustomEvent).detail);
    const reload = () => { oni.stop(); closeEvents(); setRevision((x) => x + 1); };
    window.addEventListener("oni-signout", logout);
    window.addEventListener("oni-web-status", status);
    window.addEventListener("oni-reload", reload);
    return () => {
      window.removeEventListener("oni-signout", logout);
      window.removeEventListener("oni-web-status", status);
      window.removeEventListener("oni-reload", reload);
      oni.stop();
      closeEvents();
    };
  }, []);
  const login = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      await api("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
      setToken(""); setAuth(true);
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };
  if (auth === null) return <div className="web-login"><p role="status">Establishing secure session…</p></div>;
  if (!auth) return <main className="web-login"><form className="web-login-card" onSubmit={login}>
    <span className="eyebrow">OFFICE OF NAVAL INTELLIGENCE</span><h1>Operator access</h1>
    <p>Project Reclaimer · Server Operations</p>
    {required ? <><label htmlFor="access-token">Web application access token</label>
      <input id="access-token" className="input" type="password" autoComplete="current-password" value={token}
        onChange={(e) => setToken(e.target.value)} required autoFocus maxLength={4096} />
      <button className="btn primary" disabled={busy}>{busy ? "VERIFYING…" : "SIGN IN"}</button></>
      : <button className="btn primary" type="button" onClick={check}>OPEN LOCAL CONSOLE</button>}
    {error && <p className="web-error" role="alert">{error}</p>}
    {required && <p className="web-hint">IBIGS customers: open <a href="https://app.ibigs.us/servers">app.ibigs.us</a>, pick
      your server and press <b>Web console</b> to sign in without a token. On the server itself, <code>oni web-token</code> prints it.</p>}
    <small>Credentials remain in the server gateway. Sessions expire after eight hours.</small>
  </form></main>;
  return <div className="web-shell"><div className="web-toolbar">
    <span><b>ONI</b><span className="web-divider">/</span>SERVER OPERATIONS <small>WEB CONSOLE</small></span>
    <span role="status" className={stream.connected ? "web-online" : "web-muted"}>
      <span aria-hidden="true">●</span> {stream.connected ? "GATEWAY LIVE" : stream.detail || "GATEWAY READY"}
    </span>
    <span className="web-toolbar-actions"><ServerUploadButton />
    <button onClick={() => signOut().catch((e) => setError(String(e)))}>{required ? "SIGN OUT" : "END SESSION"}</button></span>
  </div>{error && <div className="web-error" role="alert">{error}</div>}<div className="web-content"><App key={revision} /></div></div>;
}
