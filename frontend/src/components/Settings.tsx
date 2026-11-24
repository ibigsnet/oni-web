// F10 Settings: the selected server's dedicated.toml settings as grouped cards, each with its plain-words meaning, a
// control, where its value comes from and when a change applies. The backend owns the schema and checks every value
// again before it writes; this page only draws and collects. Every search box is fuzzy: "frendly" finds Friendly Fire.
import { useCallback, useEffect, useMemo, useState } from "react";
import { invoke } from "@web/bridge";
import { oni, useV } from "../engine";
import * as fuzzy from "../lib/fuzzy";
import type { Dict } from "../lib/util";
import { filterEntries, isNaNValue, looksLikeId, same, type Entry, type SettingsData, type Value } from "../lib/settings";
import { Btn, Panel } from "./ui";

export const APPLIES: Record<string, string> = {
  live: "applies: live", next_game: "applies: next game", server_restart: "applies: after restart (once the server is empty)",
  supervisor_restart: "applies: after the supervisor restarts",
};
const SOURCE: Record<string, string> = { server: "set for this server", defaults: "from the file's defaults", all_servers: "shared by every server", default: "game default" };
const PLAYS: Record<string, string> = {
  playlist: "a playlist", playlists: "playlists by lobby size (set in the file)", variant: "a saved game (set in the file)",
  one: "one map and game type", campaign: "a co-op campaign", odst: "an ODST setup (set in the file)", reach: "a Halo: Reach setup (set in the file)",
  defaults: "what the file's defaults say",
};
const NULL = "\u0000null";


/** A number box: typed text kept as typed; null for blank (when allowed), NaN while it isn't a whole number. */
function NumberBox({ e, value, onChange }: { e: Entry; value: unknown; onChange: (v: unknown) => void }) {
  const [text, setText] = useState(value === null || value === undefined ? "" : String(value));
  useEffect(() => { if (!isNaNValue(value)) setText(value === null || value === undefined ? "" : String(value)); }, [value]);
  const bad = typeof value === "number" && (Number.isNaN(value) || value < (e.min ?? -Infinity) || value > (e.max ?? Infinity));
  return (
    <span className="setnum">
      <input className={`input ${bad ? "missing" : ""}`} inputMode="numeric" value={text} aria-label={e.label} placeholder={e.nullable ? e.null_label : ""}
        onChange={(ev) => {
          const t = ev.target.value.trim();
          setText(ev.target.value);
          onChange(t === "" ? (e.nullable ? null : NaN) : /^-?\d+$/.test(t) ? Number(t) : NaN);
        }} />
      <small>{e.unit ? `${e.unit} · ` : ""}{e.min}–{e.max}</small>
    </span>
  );
}

/** A list of short entries (player IDs, welcome lines, kick steps, mods): chips with a remove button and a box to add. */
function ListBox({ e, value, inherited, onChange }: { e: Entry; value: string[]; inherited?: string[]; onChange: (v: string[]) => void }) {
  const [text, setText] = useState("");
  const add = () => {
    const parts = e.type === "lines" ? [text] : text.split(/[\s,]+/);
    const next = [...value, ...parts.map((p) => p.trim()).filter((p) => p && (e.type === "lines" || !value.includes(p)))];
    if (next.length !== value.length) onChange(next);
    setText("");
  };
  const odd = e.type === "ids" ? value.filter((v) => !looksLikeId(v)) : [];
  const hint = e.type === "ids" ? "paste a player ID" : e.type === "durations" ? "e.g. 10m, 2h, 1d" : e.type === "lines" ? "type a line" : "add one";
  return (
    <div className="setlist">
      {inherited?.map((v) => <span key={`i${v}`} className="chip inherited" title="Set for every server in the file">{v}</span>)}
      {value.map((v, i) => (
        <span key={`${i}${v}`} className={`chip ${odd.includes(v) ? "odd" : ""}`} title={odd.includes(v) ? "Doesn't look like a player ID" : undefined}>{v}<button aria-label={`Remove ${v}`} onClick={() => onChange(value.filter((_, j) => j !== i))}>×</button></span>
      ))}
      <span className="setadd">
        <input className="input" value={text} placeholder={hint} aria-label={`Add to ${e.label}`} onChange={(ev) => setText(ev.target.value)}
          onKeyDown={(ev) => { if (ev.key === "Enter") { ev.preventDefault(); add(); } }} />
        <Btn label="ADD" onClick={add} disabled={!text.trim()} />
      </span>
      {odd.length ? <div className="setwarn" role="status">⚠ {odd.map((v) => `"${v}"`).join(", ")} {odd.length > 1 ? "don't" : "doesn't"} look like a player ID
        (64 letters and digits, from the player's profile) and will never match anyone. {odd.length > 1 ? "They are" : "It is"} saved anyway.</div> : null}
    </div>
  );
}

function Control({ e, v, value, set, playlists, suggestions }: {
  e: Entry; v: Value; value: unknown; set: (x: unknown) => void; playlists: string[]; suggestions: string[];
}) {
  const t = e.type;
  if (t === "bool" && !e.nullable) {
    const on = !!value;
    return <button className={`toggle ${on ? "on" : ""}`} role="switch" aria-checked={on} aria-label={e.label} onClick={() => set(!on)}>{on ? "ON" : "OFF"}</button>;
  }
  if (t === "bool" || t === "enum" || t === "playlist") {
    let options: [unknown, string][] = t === "bool" ? [[true, "On"], [false, "Off"]] : t === "playlist" ? playlists.map((p) => [p, p.replace(/^playlists\//, "").replace(/^content\//, "Forge: ")]) : [...(e.choices ?? [])];
    if (value !== null && value !== undefined && !options.some(([c]) => same(c, value))) options = [...options, [value, String(value)]];
    const encoded = value === null || value === undefined ? NULL : JSON.stringify(value);
    return (
      <select className="input" value={encoded} aria-label={e.label} onChange={(ev) => set(ev.target.value === NULL ? null : JSON.parse(ev.target.value))}>
        {e.nullable || (t === "playlist") ? <option value={NULL}>{e.null_label ?? "Default"}</option> : null}
        {options.map(([c, label]) => <option key={JSON.stringify(c)} value={JSON.stringify(c)}>{label}</option>)}
      </select>
    );
  }
  if (t === "int") return <NumberBox e={e} value={value} onChange={set} />;
  if (t === "secret") {
    const changed = typeof value === "string";
    return (
      <span className="setsecret">
        <input className="input" type="password" autoComplete="new-password" aria-label={e.label} value={changed ? (value as string) : ""}
          placeholder={v.is_set ? "set (type to change)" : "none: anyone can join"} onChange={(ev) => set(ev.target.value === "" ? undefined : ev.target.value)} />
        {v.is_set && value !== "" ? <Btn label="NO PASSWORD" title="Let anyone join" onClick={() => set("")} /> : null}
        {value === "" ? <small>removed on save: anyone can join</small> : null}
      </span>
    );
  }
  if (t === "ids" || t === "lines" || t === "list" || t === "durations")
    return <ListBox e={e} value={(value as string[] | null) ?? []} inherited={e.scope === "additive" ? (v.inherited as string[]) : undefined} onChange={set} />;
  const list = e.choices?.length || suggestions.length ? `dl-${e.key}` : undefined;
  return (
    <>
      <input className="input" value={(value as string | null) ?? ""} maxLength={e.max_len} aria-label={e.label} list={list}
        placeholder={e.nullable ? e.null_label : t === "stats" ? "empty: the project's service · off: nowhere" : ""} onChange={(ev) => set(e.nullable && !ev.target.value ? null : ev.target.value)} />
      {list ? <datalist id={list}>{(e.choices ?? []).map(([c, l]) => <option key={String(c)} value={String(c)}>{l}</option>)}{suggestions.map((s) => <option key={s} value={s} />)}</datalist> : null}
    </>
  );
}

export function Settings() {
  useV((s) => s.ui);
  const st = oni.cur, index = st?.index;
  const [data, setData] = useState<SettingsData | null>(null);
  const [problem, setProblem] = useState("");
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null);
  const [playlists, setPlaylists] = useState<string[]>([]);

  const load = useCallback(async () => {
    if (index === undefined || !st?.contentDir) return;
    setProblem("");
    try {
      setData(await invoke<SettingsData>("server_settings", { index }));
      // The playlists folder's, and those Forge installed into the content folder (as SWITCH PLAYLIST lists them).
      const forge = Object.values<Dict>(oni.forgeState.servers?.[st.where] ?? {}).filter((e) => e.kind === "playlist" && e.files?.[0]?.path)
        .map((e) => `content/${e.files[0].path}`);
      invoke<string[]>("playlists", { index }).then((files) => setPlaylists([...files, ...forge]), () => setPlaylists(forge));
    } catch (e) {
      setData(null);
      setProblem(String(e instanceof Error ? e.message : e));
    }
  }, [index, st?.contentDir]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setDraft({}); setSaid(null); load(); }, [load]);

  const values = useMemo(() => new Map((data?.values ?? []).map((v) => [v.key, v])), [data]);
  const groupLabel = useCallback((id: string) => data?.groups.find((g) => g.id === id)?.label ?? id, [data]);
  const shown = useMemo(() => filterEntries(query, data?.settings ?? [], groupLabel), [query, data, groupLabel]);
  const searching = fuzzy.terms(query).length > 0;

  if (!st) return null;
  if (!st.contentDir) return <Panel className="grow" title="SETTINGS"><div className="setempty">This server's folder isn't set up in the console, so its settings can't be changed here. Your host can connect it.</div></Panel>;
  if (problem) return <Panel className="grow" title="SETTINGS"><div className="setempty">Couldn't read this server's settings: {problem}<br /><Btn label="TRY AGAIN" onClick={load} /></div></Panel>;
  if (!data) return <Panel className="grow" title="SETTINGS"><div className="setempty">Reading the server's settings…</div></Panel>;

  const current = (key: string) => {
    const v = values.get(key)!;
    if (key in draft) return draft[key] === null && v.source === "server" ? v.inherited : draft[key];
    return v.value;
  };
  const own = (key: string) => { const v = values.get(key)!; return v.source === "server" ? v.value : null; };
  const set = (e: Entry, x: unknown) => setDraft((d) => {
    const next = { ...d };
    const v = values.get(e.key)!;
    // Back to what it was (or a password box emptied again): no change at all.
    if (x === undefined || (e.type !== "secret" && same(x, v.value))) delete next[e.key];
    else next[e.key] = x;
    return next;
  });
  const reset = (e: Entry) => setDraft((d) => ({ ...d, [e.key]: null }));
  const pending = Object.keys(draft);
  const invalid = pending.filter((k) => typeof draft[k] === "number" && Number.isNaN(draft[k]));
  const suggestions = (what?: "maps" | "modes") => (what ? (st.data[what]?.entries ?? []).map((x: Dict) => String(x.name ?? x.reference ?? "")).filter(Boolean) : []);

  const save = async () => {
    setBusy(true);
    setSaid(null);
    try {
      const r = await invoke<{ applies: string[]; warnings?: string[] }>("server_setting", { index, changes: draft });
      const when = (r?.applies ?? []).map((a) => APPLIES[a]?.replace("applies: ", "")).filter(Boolean).join(", ");
      setSaid({ ok: true, text: `Saved ${pending.length} setting${pending.length === 1 ? "" : "s"}. ${when ? `They apply ${when}.` : ""} With auto_reload on (the default) the server picks the file up by itself within seconds.${r?.warnings?.length ? `\nWarning: ${r.warnings.join("; ")}.` : ""}` });
      setDraft({});
      await load();
    } catch (e) {
      setSaid({ ok: false, text: `Not saved: ${String(e instanceof Error ? e.message : e)}. Nothing was changed.` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="vsplit grow settings">
      <div className="setbar">
        <input id="settings-q" className="input" placeholder="Search settings (typos are fine)" value={query} aria-label="Search settings" onChange={(e) => setQuery(e.target.value)} />
        <span className="setplays">{data.server_name} plays {PLAYS[data.plays] ?? data.plays}</span>
        <Btn label={busy ? "SAVING…" : pending.length ? `SAVE ${pending.length}` : "SAVE"} variant="primary" disabled={!pending.length || busy || invalid.length > 0} onClick={save}
          title={invalid.length ? "Fix the numbers marked in red first" : "Write the changes to the server's dedicated.toml (a backup of the old file is kept)"} />
        <Btn label="DISCARD" disabled={!pending.length || busy} onClick={() => { setDraft({}); setSaid(null); }} />
      </div>
      {said ? <div className={`setsaid ${said.ok ? "ok" : "bad"}`} role="status">{said.text}</div> : null}
      <div className="setgroups grow">
        {data.groups.map((g) => {
          const rows = shown.filter((e) => e.group === g.id);
          if (!rows.length) return null;
          const basic = rows.filter((e) => !e.advanced || searching || open.has(g.id) || e.key in draft);
          const hidden = rows.length - basic.length;
          return (
            <Panel key={g.id} title={g.label.toUpperCase()} className="setcard">
              {basic.map((e) => {
                const v = values.get(e.key)!;
                const changed = e.key in draft;
                const disabled = !!e.needs && !current(e.needs);
                const canReset = !changed && v.source === "server" && (e.scope === "defaults" || e.scope === "top" || (e.nullable && e.scope !== "additive"));
                return (
                  <div key={e.key} className={`setrow ${changed ? "changed" : ""} ${disabled ? "disabled" : ""}`}>
                    <div className="setwhat">
                      <div className="setlabel">{e.label}{e.advanced ? <span className="settag">ADVANCED</span> : null}</div>
                      <div className="setdesc">{e.description}</div>
                      <div className="setsource">
                        {changed ? <span className="setapplies">{APPLIES[e.applies]}</span>
                          : <>{SOURCE[v.source] ?? v.source}{v.source === "server" && e.scope === "defaults" && v.inherited !== undefined && e.type !== "secret" ? ` · otherwise ${String(v.inherited ?? e.null_label ?? "default")}` : ""}</>}
                        {e.scope === "global" ? " · changes every server in this file" : ""}
                      </div>
                    </div>
                    <div className="setcontrol">
                      {disabled ? <span className="setdesc">Choose a mission first.</span>
                        : <Control e={e} v={v} value={e.scope === "additive" ? (e.key in draft ? draft[e.key] : own(e.key) ?? []) : current(e.key)} set={(x) => set(e, x)} playlists={playlists} suggestions={suggestions(e.suggest)} />}
                      <span className="setactions">
                        {canReset ? <Btn label="USE DEFAULT" title="Remove this server's own value so the shared one applies" onClick={() => reset(e)} /> : null}
                        {changed ? <Btn label="UNDO" onClick={() => setDraft((d) => { const n = { ...d }; delete n[e.key]; return n; })} /> : null}
                      </span>
                    </div>
                  </div>
                );
              })}
              {hidden ? <button className="setmore" onClick={() => setOpen((s) => new Set(s).add(g.id))}>SHOW {hidden} ADVANCED SETTING{hidden > 1 ? "S" : ""}</button>
                : open.has(g.id) && !searching ? <button className="setmore" onClick={() => setOpen((s) => { const n = new Set(s); n.delete(g.id); return n; })}>HIDE ADVANCED SETTINGS</button> : null}
            </Panel>
          );
        })}
        {!shown.length ? <div className="setempty">No setting matches “{query}”.</div> : null}
      </div>
    </div>
  );
}
