// The content browser, the playlist library and the playlist editor: dialogs over lib/content.ts. From ibigs/oni-rcon's
// content_ui.py and playlist_ui.py, by IBIGS.
import { useEffect, useRef, useState } from "react";
import { invoke } from "@web/bridge";
import { AMBER, DIM, GREEN, RED, WHITE } from "../art/palette";
import { oni, type Dialog } from "../engine";
import { GROUP_HELP, SORTS, arrange, catalogItems, checkPlaylist, gameLabel, itemsFor, type Game, type Group, type Item, type Playlist } from "../lib/content";
import { norm } from "../lib/forge";
import * as fuzzy from "../lib/fuzzy";
import { type Dict, type Line } from "../lib/util";
import { L, Table, type Row } from "./ui";

const Keys = ({ hints }: { hints: string[] }) => <div className="keys">{hints.join("   ·   ")}</div>;
const bar = { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", margin: "6px 0" } as const;

let lastOrder: [string, boolean] = ["name", false]; // the order last chosen, kept for the next time it opens
const BIG = 60; // more than this many and every group but the first starts closed

type R = { id: string; head?: [Group, string, number, boolean]; item?: Item };

/** Pick a map or a game type, in groups. Resolves with its reference, "" (keep the current rules), `{listing}` for a
 *  ReclaimerForge one that isn't installed yet, or null. */
export function ContentBrowser({ d }: { d: Dialog }) {
  const { title, what, index, keep } = d.props as { title: string; what: "maps" | "modes"; index: number; keep?: boolean };
  const st = oni.stations[index];
  const installed: Dict = oni.forgeState.servers?.[st.where] ?? {};
  const [text, setText] = useState("");
  const [[how, flip], setOrder] = useState<[string, boolean]>(lastOrder);
  const [at, setAt] = useState(0);
  const [toggled, setToggled] = useState<Set<string>>(new Set());
  const [cat, setCat] = useState<{ items: Item[]; note: string }>({ items: [], note: oni.forgeStatus.key ? "Fetching the ReclaimerForge catalog…" : "Load your ReclaimerForge key (F6) to see more here." });
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!oni.forgeStatus.key) return;
    let live = true;
    invoke<Dict>("forge_listings", { sort: "latest", window: "7d", q: "", more: null, fresh: false }).then((r) => {
      if (live) setCat(r.error ? { items: [], note: r.error.text } : { items: catalogItems(r.items, what === "maps" ? "map" : "gametype", new Set(Object.keys(installed))), note: "" });
    }, (e) => live && setCat({ items: [], note: String(e) }));
    return () => { live = false; };
  }, []);
  useEffect(() => setAt(0), [text]);

  const groups = arrange([...itemsFor(st.data[what]?.entries ?? [], installed), ...cat.items], what, how, text, flip);
  const total = groups.reduce((n, g) => n + g[2].length, 0);
  const rows: R[] = keep && !text ? [{ id: "keep" }] : [];
  groups.forEach(([key, name, items], gi) => {
    const open = (text !== "" || total <= BIG || gi === 0) !== toggled.has(key);
    rows.push({ id: `g:${key}`, head: [key, name, items.length, open] });
    if (open) items.forEach((item, n) => rows.push({ id: `${key}:${n}`, item }));
  });
  const cur = Math.min(at, rows.length - 1);
  useEffect(() => { list.current?.children[cur]?.scrollIntoView({ block: "nearest" }); }, [cur]);

  const act = (r?: R) => {
    if (!r) return;
    if (r.head) return setToggled((t) => { const n = new Set(t); if (!n.delete(r.head![0])) n.add(r.head![0]); return n; });
    d.resolve(!r.item ? "" : r.item.group === "catalog" ? { listing: r.item.forge } : r.item.reference);
  };
  const order = (k: string) => {
    lastOrder = how === k ? [k, !flip] : [k, false];
    setOrder(lastOrder);
  };
  const key = (e: React.KeyboardEvent) => {
    const go: Record<string, number> = { ArrowDown: 1, ArrowUp: -1, PageDown: 10, PageUp: -10 };
    if (e.key in go) { e.preventDefault(); setAt(Math.max(0, Math.min(rows.length - 1, cur + go[e.key]))); }
    else if (e.key === "Enter" && (e.target as HTMLElement).tagName !== "BUTTON") act(rows[cur]);
  };
  const playing = norm(st.data.status?.[what === "maps" ? "map" : "mode"]);
  const show = (r: R): Line => {
    if (r.head) return [[`${r.head[3] ? "▾" : "▸"} ${r.head[1]}`, AMBER], [`  ${r.head[2]}`, DIM]];
    const i = r.item;
    if (!i) return [["(keep the current rules)", DIM]];
    return [["    ", DIM], [i.name, WHITE], ...(i.folder ? [[`  ${i.folder}/`, DIM]] : []), ...(i.detail ? [[`  ${i.detail}`, DIM]] : []),
      ...(i.group !== "catalog" && playing && norm(i.name) === playing ? [["  ● playing", GREEN]] : [])] as Line;
  };
  const here = rows[cur], hit = here?.item;
  const hint = hit ? [GROUP_HELP[hit.group], hit.reference].filter(Boolean).join("  ·  ") : here?.head ? GROUP_HELP[here.head[0]] : here ? "Leaves the rules as they are." : "";
  return (
    <div className="dialog wide" onKeyDown={key}>
      <div className="dt">{title}</div>
      <input className="input" autoFocus placeholder="type to filter: every word must match a name, author, folder or file…" value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} />
      <div style={bar}>
        <span style={{ color: DIM }}>ORDER BY</span>
        {SORTS.map(([k, label]) => (
          <button key={k} className={`btn ${how === k ? "primary" : ""}`} onClick={() => order(k)}>{label}{how === k ? ((k !== "name") !== flip ? " ▼" : " ▲") : ""}</button>
        ))}
        <span style={{ color: DIM }}>{`${total} listed${cat.note ? ` · ${cat.note}` : ""}`}</span>
      </div>
      <div className="picklist" ref={list} style={{ flex: 1, maxHeight: "none" }}>
        {rows.map((r, i) => (
          <div key={r.id} className={i === cur ? "cur" : undefined} onMouseEnter={() => setAt(i)} onClick={() => act(r)}><L line={show(r)} /></div>
        ))}
        {rows.length ? null : <div style={{ color: DIM, cursor: "default" }}>{text ? "Nothing matches." : "Nothing listed yet."}</div>}
      </div>
      <div style={{ color: DIM, minHeight: 20, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{hint}</div>
      <Keys hints={["Esc cancel", "↑ ↓ move", "Enter or click to choose (or open a group)"]} />
    </div>
  );
}

/** The playlists the server folder holds, and the Forge ones installed on it. Resolves {do: "new" | "edit" | "use", path?, sel?} or null. */
export function PlaylistLibrary({ d }: { d: Dialog }) {
  const { index, sel: first } = d.props as { index: number; sel?: string };
  const st = oni.stations[index];
  const [files, setFiles] = useState<Dict[] | null>(null);
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const [sel, setSel] = useState<string | null>(first ?? null);
  useEffect(() => {
    let live = true;
    invoke<Dict[]>("playlist_library", { index }).then((r) => live && setFiles(r), (e) => live && (setError(String(e)), setFiles([])));
    return () => { live = false; };
  }, []);
  const forge = Object.values<Dict>(oni.forgeState.servers?.[st.where] ?? {}).filter((e) => e.kind === "playlist" && e.files?.[0]?.path)
    .map((e) => ({ path: `content/${e.files[0].path}`, name: e.title, forge: true }) as Dict);
  const shown = [...(files ?? []), ...forge].filter((r) => fuzzy.matches(text, r.name ?? "", r.path));
  const rows: Row[] = shown.map((r) => ({
    id: r.path,
    cells: [[[String(r.name ?? r.path.split("/").pop()), r.problem ? RED : WHITE]], [[r.problem ? "unreadable" : String(r.games ?? "—"), r.problem ? RED : DIM]],
      [[r.forge ? "Forge" : r.here ? "this console" : "your folder", DIM]], [[r.path, DIM]]],
  }));
  const row = shown.find((r) => r.path === sel);
  const go = (what: string) => row && d.resolve({ do: what, path: row.path });
  return (
    <div className="dialog wide">
      <div className="dt">{`PLAYLISTS · ${st.label}`}</div>
      <input className="input" autoFocus placeholder="type to filter…" value={text} spellCheck={false} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "ArrowDown") { e.preventDefault(); document.getElementById("playlists")?.focus(); } }} />
      <div style={{ flex: 1, minHeight: 120, display: "flex", flexDirection: "column", marginTop: 6 }}>
        {rows.length ? <Table id="playlists" cols={["PLAYLIST", "GAMES", "FROM", "FILE"]} rows={rows} selected={sel} onSelect={setSel} onEnter={() => !row?.problem && go("edit")} />
          : <div style={{ color: files ? DIM : AMBER }}>{error || (files ? "No playlists here yet: NEW makes one, or install one from F6." : "Reading the server's playlists…")}</div>}
      </div>
      <div style={{ color: DIM, minHeight: 20 }}>{row && !row.here ? "Not made by this console: EDIT opens a copy and saves it into the server folder's playlists/." : ""}</div>
      <div className="btns">
        <button className="btn primary" onClick={() => d.resolve({ do: "new" })}>NEW</button>
        <button className="btn" disabled={!row || !!row.problem} onClick={() => go("edit")}>EDIT</button>
        <button className="btn" disabled={!row || !!row.problem} onClick={() => go("use")}>USE ON THIS SERVER</button>
        <button className="btn" onClick={() => d.resolve(null)}>CLOSE</button>
      </div>
      <Keys hints={["Esc close", "↓ list", "Enter edit"]} />
    </div>
  );
}

/** Edit one playlist: its name, vote time and games. Resolves {do: "add" | "save" | "close", p, sel?}; `sink.p` always holds
 *  what is on screen, for when the dialog is dismissed from outside. */
export function PlaylistEditor({ d }: { d: Dialog }) {
  const { p, path, note, sel: first, sink } = d.props as { p: Playlist; path: string | null; note?: string; sel?: string; sink: { p?: Playlist } };
  const [name, setName] = useState(p.name);
  const [vote, setVote] = useState(Number.isFinite(p.vote_seconds) ? String(p.vote_seconds) : "");
  const [games, setGames] = useState<Game[]>(p.games);
  const [sel, setSel] = useState<string | null>(first ?? null);
  const [error, setError] = useState("");
  const now = (): Playlist => ({ version: 2, name, vote_seconds: vote.trim() === "" ? NaN : Number(vote), games });
  sink.p = now();
  useEffect(() => { if (p.games.length) document.getElementById("pl-games")?.focus(); }, []);
  const at = sel === null ? -1 : Number(sel);
  const edit = (f: (g: Game[]) => Game[], to?: number) => { setGames(f); if (to !== undefined) setSel(String(to)); setError(""); };
  const swap = (b: number) => at >= 0 && b >= 0 && b < games.length && edit((g) => { const n = [...g]; [n[at], n[b]] = [n[b], n[at]]; return n; }, b);
  const weigh = (by: number) => at >= 0 && edit((g) => g.map((x, i) => (i === at ? { ...x, weight: Math.max(1, Math.min(100, (x.weight ?? 1) + by)) } : x)));
  const drop = () => at >= 0 && edit((g) => g.filter((_, i) => i !== at), Math.min(at, games.length - 2));
  const save = () => {
    try { d.resolve({ do: "save", p: checkPlaylist(now()) }); } catch (e) { setError((e as Error).message); }
  };
  const rows: Row[] = games.map((g, i) => ({ id: String(i), cells: [[[String(i + 1), DIM]], [[gameLabel({ ...g, weight: 1 }), WHITE]], [[String(g.weight ?? 1), DIM]]] }));
  return (
    <div className="dialog wide" onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); e.stopPropagation(); save(); } }}>
      <div className="dt">{path ? `EDIT PLAYLIST · ${path}` : "NEW PLAYLIST"}</div>
      {note ? <div className="db">{note}</div> : null}
      <div style={bar}>
        <span style={{ color: DIM }}>NAME</span>
        <input className="input" style={{ flex: 1, minWidth: 200 }} autoFocus={!p.games.length} value={name} spellCheck={false} onChange={(e) => { setName(e.target.value); setError(""); }} />
        <span style={{ color: DIM }}>VOTE SECONDS</span>
        <input className="input" style={{ width: 70 }} value={vote} spellCheck={false} onChange={(e) => { setVote(e.target.value); setError(""); }} />
      </div>
      <div style={{ color: games.length < 3 ? AMBER : DIM, marginBottom: 4 }}>{`${games.length} game${games.length === 1 ? "" : "s"} · three at least, each map and game type once (a higher weight comes up more often)`}</div>
      <div style={{ flex: 1, minHeight: 120, display: "flex", flexDirection: "column" }}>
        {rows.length ? <Table id="pl-games" cols={["#", "GAME", "WEIGHT"]} rows={rows} selected={sel} onSelect={setSel} /> : <div style={{ color: DIM }}>No games yet: ADD GAME picks a map, then a game type.</div>}
      </div>
      <div style={{ color: RED, minHeight: 20 }}>{error}</div>
      <div style={bar}>
        <button className="btn" onClick={() => d.resolve({ do: "add", p: now() })}>ADD GAME</button>
        <button className="btn" disabled={at < 1} onClick={() => swap(at - 1)}>UP</button>
        <button className="btn" disabled={at < 0 || at >= games.length - 1} onClick={() => swap(at + 1)}>DOWN</button>
        <button className="btn" disabled={at < 0} onClick={() => weigh(-1)}>WEIGHT −</button>
        <button className="btn" disabled={at < 0} onClick={() => weigh(1)}>WEIGHT +</button>
        <button className="btn error" disabled={at < 0} onClick={drop}>REMOVE</button>
      </div>
      <div className="btns">
        <button className="btn" onClick={() => d.resolve({ do: "close", p: now() })}>CANCEL</button>
        <button className="btn primary" onClick={save}>SAVE</button>
      </div>
      <Keys hints={["Esc close", "Ctrl+S save", "↑ ↓ pick a game"]} />
    </div>
  );
}
