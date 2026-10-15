import { useEffect, useState } from "react";
import { invoke, listen } from "./bridge";
import { oni } from "../engine";
import type { Dict } from "../lib/util";

/** Optional platform operations remain in the original headless Rust implementation. */
export function NativeSetup({ changed }: { changed: () => Promise<unknown> }) {
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [hostedBy, setHostedBy] = useState("");
  const [password, setPassword] = useState("");
  const [max, setMax] = useState(16);
  const [hidden, setHidden] = useState(false);
  const [internet, setInternet] = useState(false);
  const [auto, setAuto] = useState(false);
  const [dest, setDest] = useState("");
  const [playlist, setPlaylist] = useState<Dict | null>(null);
  const [remember, setRemember] = useState(true);
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    invoke<{ enabled: boolean }>("host_status").then((s) => setEnabled(s.enabled)).catch((e) => setError(String(e)));
    const off = listen<string>("setup-line", (e) => setLines((l) => [...l.slice(-99), e.payload]));
    return () => { off.then((f) => f()); };
  }, []);
  const run = async (action: "host" | "find") => {
    if (action === "host" && !(await oni.confirm("HOST A GAME SERVER", "Create or start Project Reclaimer on the gateway machine. This may download a verified server release, read the installed game, write its server folder and launch the game server. Existing server folders start as configured. No firewall rules are added.", "CREATE / START", true))) return;
    setBusy(true); setError(""); setLines([]);
    try {
      const result = await invoke<Dict>(action === "host" ? "setup_host" : "setup_find", {
        remember, ...(action === "find" ? { dest } : { settings: {
          name, host: hostedBy, password, max_players: max, hidden, internet, auto_update: auto, playlist,
        } }),
      });
      setLines((l) => [...l, `${result.added} profiles added.`, ...(result.notes ?? [])]);
      await changed();
    } catch (e) { setError(String(e)); }
    finally { setPassword(""); setBusy(false); }
  };
  return <details className="setup-native"><summary>Host a server or discover an existing game box</summary>
    {!enabled ? <p>Native hosting and discovery are not enabled on this gateway. See the README to enable the optional host integration.</p> : <>
      <p>These operations use the gateway machine's game installation, Docker and SSH credentials.</p>
      <div className="setup-grid"><section className="setup-card"><h2>Host one here</h2>
        <label htmlFor="native-name">Server name</label><input id="native-name" className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={48} />
        <label htmlFor="native-host">Hosted by <small>OPTIONAL BROWSER LABEL</small></label><input id="native-host" className="input" value={hostedBy} onChange={(e) => setHostedBy(e.target.value)} maxLength={100} />
        <label htmlFor="native-password">Game join password <small>OPTIONAL · NOT THE RCON PASSWORD</small></label>
        <input id="native-password" className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" maxLength={4096} />
        <label htmlFor="native-max">Player limit</label><input id="native-max" className="input" type="number" min={1} max={63} value={max} onChange={(e) => setMax(Number(e.target.value))} />
        <label className="setup-remember"><input type="checkbox" checked={hidden} onChange={(e) => setHidden(e.target.checked)} />Hidden from the server browser</label>
        <label className="setup-remember"><input type="checkbox" checked={internet} onChange={(e) => setInternet(e.target.checked)} />Ask the router to forward ports (UPnP)</label>
        <label className="setup-remember"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />Enable the dedicated server's auto-updater</label>
        <label htmlFor="native-playlist">Initial playlist <small>OPTIONAL JSON FILE</small></label>
        <input id="native-playlist" type="file" accept=".json" disabled={busy} onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) { setPlaylist(null); return; }
          if (f.size > 1_000_000) { setError("Playlist exceeds 1 MB"); return; }
          try { setPlaylist(JSON.parse(await f.text())); setError(""); } catch { setPlaylist(null); setError("Choose a valid playlist JSON file"); }
        }} />
        <button className="btn primary" disabled={busy || !Number.isInteger(max) || max < 1 || max > 63} onClick={() => run("host")}>{busy ? "WORKING…" : "CREATE / START"}</button>
      </section><section className="setup-card"><h2>Find servers over SSH</h2>
        <p>Search existing Reclaimer containers and server folders, authenticate each console once, and add the ones that answer.</p>
        <label htmlFor="native-dest">SSH destination</label><input id="native-dest" className="input" value={dest} onChange={(e) => setDest(e.target.value)} placeholder="operator@game-box" maxLength={253} />
        <button className="btn primary" disabled={busy || !dest.trim()} onClick={() => run("find")}>FIND SERVERS ON IT</button>
      </section></div>
      <label className="setup-remember"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />Remember generated RCON credentials in encrypted storage</label>
    </>}
    {error && <p className="web-error" role="alert">{error}</p>}
    {lines.length > 0 && <pre className="setup-progress" role="log" aria-live="polite">{lines.join("\n")}</pre>}
  </details>;
}
