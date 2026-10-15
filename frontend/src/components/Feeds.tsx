// F2 Intercepts and F5 Console: the feed from every server and the command log. Each follows new lines only while
// it's scrolled to the bottom, so reading back isn't yanked away, and counts what came in meanwhile.
import { memo, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { AMBER, CYAN, DIM, RED, INK, blend } from "../art/palette";
import { oni, useV, type FeedEntry } from "../engine";
import { COMMANDS, sayText } from "../actions";
import { CATS, GLYPH, KIND, clock, describe, parseCommand, type Line } from "../lib/util";
import { Check, Json, L, Panel } from "./ui";

/** Follows new lines while at the bottom. Only a person scrolling up stops it (a wheel, a key, the scrollbar):
 *  rows settling their heights as they come into view move the scroll position too, and that isn't reading back. */
export function useFollow(dep: number) {
  const ref = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const touched = useRef(0);
  const [unread, setUnread] = useState(0);
  const seen = useRef(dep);
  const pin = () => {
    const el = ref.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  };
  useLayoutEffect(() => {
    if (!ref.current) return;
    if (follow.current) {
      pin();
      requestAnimationFrame(pin); // again once the rows in view have their real heights
      seen.current = dep;
    } else if (dep !== seen.current) setUnread((u) => u + 1);
  }, [dep]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const mark = () => (touched.current = performance.now());
    for (const e of ["wheel", "mousedown", "keydown", "touchmove"]) el.addEventListener(e, mark, { passive: true });
    const ro = new ResizeObserver(pin);
    ro.observe(el);
    return () => {
      for (const e of ["wheel", "mousedown", "keydown", "touchmove"]) el.removeEventListener(e, mark);
      ro.disconnect();
    };
  }, []);
  const onScroll = () => {
    const el = ref.current!;
    const end = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (end) {
      follow.current = true;
      if (unread) setUnread(0);
    } else if (performance.now() - touched.current < 800) follow.current = false;
    else pin();
  };
  const toEnd = () => {
    const el = ref.current!;
    el.scrollTop = el.scrollHeight;
    follow.current = true;
    setUnread(0);
  };
  return { ref, onScroll, unread, toEnd };
}

const FeedRow = memo(function FeedRow({ e, redact, width }: { e: FeedEntry; redact: boolean; width: number; label: string }) {
  const kind = String(e.ev.event ?? "?");
  const label = e.st !== null ? oni.stations[e.st].label : kind === "health" ? "HOST" : "LINK";
  const head: Line = [[clock(e.ev.time), DIM], [` ${label.slice(0, width).padEnd(width)} `, CYAN], [`${GLYPH[kind] ?? "·"} `, CATS[KIND[kind] ?? "ops"]]];
  return <div><L line={[...head, ...describe(e.ev, redact)]} /></div>;
});

export function Intercepts() {
  const v = useV((s) => s.feed);
  useV((s) => s.ui);
  const shown = oni.feed.filter((e) => oni.shown(e)).slice(-800);
  const last = shown.length ? shown[shown.length - 1].id : 0;
  const f = useFollow(last);
  const live = oni.stations.some((s) => s.online);
  const rate = oni.stations.reduce((a, s) => a + s.rate(1, 60_000)[0], 0);
  const blink = Date.now() % 2000 < 1000;
  const [say, setSay] = useState("");
  const title: ReactNode = <>
    <span style={{ color: AMBER }}>SIGINT FEED  </span>
    <span style={{ color: live ? (blink ? RED : blend(RED, INK, 0.4)) : DIM }}>{live ? "●" : "○"}</span>
    <span style={{ color: DIM }}>{live ? ` LIVE · ${rate}/min` : " NO SIGNAL"}</span>
  </>;
  void v;
  return (
    <>
      <div className="bar" style={{ gap: 18 }}>
        {Object.keys(CATS).map((c) => (
          <Check key={c} label={c.toUpperCase()} on={oni.filters.has(c)} onChange={(on) => { on ? oni.filters.add(c) : oni.filters.delete(c); useV.setState((s) => ({ feed: s.feed + 1 })); }} />
        ))}
        <Check label="THIS STATION ONLY" on={oni.localOnly} title="Show only what happens on the selected server."
          onChange={(on) => { oni.localOnly = on; useV.setState((s) => ({ feed: s.feed + 1 })); }} />
      </div>
      <Panel title={title} sub={f.unread ? <span onClick={f.toEnd} style={{ cursor: "pointer" }}>▼ {f.unread} NEW  ·  End to follow</span> : null} className="grow" col>
        <div className="log" id="feed" ref={f.ref} onScroll={f.onScroll} tabIndex={0} onKeyDown={(e) => e.key === "End" && f.toEnd()}>
          {shown.map((e) => <FeedRow key={e.id} e={e} redact={oni.redact} width={oni.labelWidth} label={e.st !== null ? oni.stations[e.st].label : ""} />)}
        </div>
      </Panel>
      <input id="say" aria-label="Server chat message" className="input" value={say} onChange={(e) => setSay(e.target.value)} spellCheck={false}
        placeholder="» type to chat as [Server] on this server   ·   start with @all to reach every server"
        onKeyDown={(e) => { if (e.key === "Enter") { sayText(say); setSay(""); } }} />
    </>
  );
}

let draft = "", hpos = 0;

export function ConsoleTab() {
  const v = useV((s) => s.log);
  const [line, setLine] = useState("");
  const [raw, setRaw] = useState(oni.rawEvents);
  const f = useFollow(v);
  const run = () => {
    const l = line.trim();
    setLine("");
    if (!l) return;
    if (oni.history[oni.history.length - 1] !== l) oni.history.push(l);
    hpos = oni.history.length;
    draft = "";
    f.toEnd();
    if (l === "clear") return oni.clearLog();
    const fleet = l.startsWith("@all ");
    let parts: string[];
    try {
      parts = parseCommand(l.replace(/^@all /, ""));
    } catch (err) {
      return oni.logLine([[`  ${(err as Error).message}`, RED]]);
    }
    if (!parts.length) return;
    if (fleet) {
      const live = oni.stations.filter((s) => s.online);
      if (live.length < oni.stations.length) oni.logLine([[`  ${oni.stations.length - live.length} station(s) offline: skipped`, AMBER]]);
      oni.fanout(live, parts[0], parts.slice(1), true);
    } else oni.send(oni.cur, parts[0], parts.slice(1), [], { toast: false, showData: true });
  };
  const history = (step: number) => {
    const h = oni.history;
    if (!h.length) return;
    if (hpos === h.length) draft = line;
    hpos = Math.max(0, Math.min(h.length, hpos + step));
    setLine(hpos < h.length ? h[hpos] : draft);
  };
  const suggestion = line && !line.includes(" ") ? COMMANDS.find((c) => c.startsWith(line.toLowerCase()) && c !== line.toLowerCase()) : undefined;
  return (
    <>
      <Panel title="COMMAND LOG" sub={f.unread ? <span onClick={f.toEnd} style={{ cursor: "pointer" }}>▼ NEW  ·  End to follow</span> : null} className="grow" col>
        <div className="log" id="console-log" role="log" aria-label="Command responses" aria-live="polite" ref={f.ref} onScroll={f.onScroll} tabIndex={0} onKeyDown={(e) => e.key === "End" && f.toEnd()}>
          {oni.log.slice(-1500).map((e) => (e.json !== undefined ? <Json key={e.id} value={e.json} /> : <div key={e.id}><L line={e.line!} /></div>))}
        </div>
      </Panel>
      <div className="bar" style={{ alignItems: "center", flexWrap: "nowrap" }}>
        <div style={{ position: "relative", flex: 1 }}>
          <input id="cmd" aria-label="RCON command" className="input" value={line} onChange={(e) => setLine(e.target.value)} spellCheck={false} autoComplete="off"
            placeholder="command   ·   @all <command> runs on every station   ·   ↑↓ history"
            onKeyDown={(e) => {
              if (e.key === "Enter") run();
              else if (e.key === "ArrowUp") { e.preventDefault(); history(-1); }
              else if (e.key === "ArrowDown") { e.preventDefault(); history(1); }
              else if ((e.key === "Tab" || e.key === "ArrowRight") && suggestion) { e.preventDefault(); setLine(suggestion); }
            }} />
          {suggestion ? <span style={{ position: "absolute", left: 9, top: 4, pointerEvents: "none", color: DIM, whiteSpace: "pre" }}><span style={{ color: "transparent" }}>{line}</span><span>{suggestion.slice(line.length)}</span></span> : null}
        </div>
        <Check label="RAW EVENTS" on={raw} title="Also print every event the servers push, as raw data." onChange={(on) => { oni.rawEvents = on; setRaw(on); }} />
      </div>
    </>
  );
}
