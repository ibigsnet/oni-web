// The sidebar: a card per station, the ONI crest in whatever room is left, the Superintendent, and the uplink.
import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AMBER, CYAN, DIM, GREEN, RED, WHITE, INK, blend } from "../art/palette";
import { emblem, faceCached } from "../art/archive";
import { paint } from "../art/pixels";
import { oni, useV, type Station } from "../engine";
import { STATE, TIPS, addServer } from "../actions";
import { LEVEL_COLOR, level } from "../lib/perf";
import { gauge, num, spark, type Line } from "../lib/util";
import { L, Pixel, useSize } from "./ui";

const QUIET = new Set(["", "unknown", "none"]);
/** The server's frame rate when it is falling behind, so the card says so; undefined while it keeps up. */
const slowFps = (st: Station) => { const r = oni.health.frames?.[String(st.index)]; return r?.behind ? Math.round(r.fps) : undefined; };

function cardLines(st: Station, cols: number): [Line, Line, Line[]] {
  const s = st.data.status ?? {};
  const [g0, c0] = STATE[st.state] ?? STATE.connecting;
  const glyph: [string, string] = [st.state === "connecting" ? "◐" : g0, c0];
  const n = num(s.players), mx = num(s.max_players);
  const count: Line = st.online && n !== undefined ? [[`${n}/${mx}`, n ? AMBER : DIM]] : [];
  const fr = st.online ? slowFps(st) : undefined;
  const lag: Line = fr ? [[`${fr}fps  `, LEVEL_COLOR[level(fr)]]] : [];
  const right: Line = st.alerts ? [[`⚑ ${st.alerts}  `, RED], ...lag, ...count] : [...lag, ...count];
  const top: Line = [[glyph[0] + " ", glyph[1]], [`${st.index + 1}  ${st.label.toUpperCase()}`, WHITE]];
  if (!st.online) {
    const left = st.retryAt - performance.now();
    const r: Line = left > 0 ? [[`↻ ${Math.ceil(left / 1000)}s`, DIM]] : st.state === "denied" ? [["F3 ↻", DIM]] : [];
    return [top, right, [[["   " + oni.why(st), st.state !== "connecting" ? c0 : AMBER]], r]];
  }
  const where = [st.tag, s.map, s.mode].filter(Boolean).map((x) => String(x).replace(/_/g, " "));
  const place: Line = [["   "]];
  where.forEach((x, i) => place.push(...(i ? [[" · ", DIM]] as Line : []), [x, x === st.tag ? CYAN : DIM]));
  if (oni.fleet) return [top, right, [place, spark(st.rate(10, 5000, true))]];
  const phase = String(s.phase ?? "");
  const full = n !== undefined && mx ? n / mx : 0;
  return [top, right, [place, QUIET.has(phase.toLowerCase()) ? [] : [[phase.replace(/_/g, " ").toUpperCase(), phase === "in_game" ? GREEN : DIM]],
    [["   "], ...gauge(full, 8, full >= 1 ? RED : full >= 0.75 ? AMBER : GREEN), ["  "], ...spark(st.rate(Math.max(4, cols - 13), 5000))]]];
}

const Card = memo(function Card({ st, on, cols }: { st: Station; on: boolean; cols: number; v: string }) {
  const [top, right, rest] = cardLines(st, cols);
  const pulse = st.flash > performance.now();
  const beat = st.online && !oni.fleet;
  return (
    <div className={`card ${oni.fleet ? "fleet" : ""} ${on ? "on" : ""} ${pulse ? "pulse" : ""}`} role="button" tabIndex={0}
      aria-pressed={on} aria-label={`${st.label}, ${st.state}`} onClick={() => oni.select(st.index)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); oni.select(st.index); } }}>
      <div className="row2">
        <span>
          <span className={st.state === "connecting" ? "spin" : beat ? "beat" : ""} style={{ color: top[0][1], animationDelay: `-${((st.index * 7) % 20) * 0.15}s` }}>{top[0][0].trim()}</span>{" "}
          <L line={top.slice(1)} />
        </span>
        <span><L line={right} /></span>
      </div>
      {oni.fleet || !st.online ? (
        <div className="row2"><span><L line={rest[0]} /></span><span><L line={rest[1]} /></span></div>
      ) : (
        <>
          <div className="row2"><span><L line={rest[0]} /></span><span><L line={rest[1]} /></span></div>
          <div><L line={rest[2]} /></div>
        </>
      )}
    </div>
  );
});

function cardKey(st: Station, cols: number) {
  const s = st.data.status ?? {};
  const left = st.retryAt > performance.now() ? Math.ceil((st.retryAt - performance.now()) / 1000) : 0;
  const trace = st.online ? st.rate(oni.fleet ? 10 : Math.max(4, cols - 13), 5000, oni.fleet).join(",") : "";
  return [st.state, st.label, st.tag, st.alerts, s.players, s.max_players, s.map, s.mode, s.phase, left, trace, slowFps(st), st.flash > performance.now(), oni.why(st)].join("|");
}

export function Stations({ cols }: { cols: number }) {
  useV((s) => s.cards);
  return (
    <div className="stations">
      {oni.stations.map((st) => <Card key={st.index} st={st} on={st.index === oni.sel} cols={cols} v={cardKey(st, cols)} />)}
    </div>
  );
}

/** The ONI emblem in whatever room the cards and the Superintendent leave, gone when that's too little. */
function Crest() {
  const [ref, { w, h }] = useSize<HTMLDivElement>();
  const size = Math.floor(Math.min(w - 16, h - 12, 260) / 2) * 2;
  return <div className="crest" ref={ref}>{size >= 56 ? <Pixel pic={emblem(size / 2)} width={size} /> : null}</div>;
}

/** The Superintendent's card: the face, its name, its mood and what it last reacted to. It paints itself at up to
 *  30 frames a second while it moves, and not at all while it's still. */
export function Super({ size, width }: { size: number; width: number }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [words, setWords] = useState(["WATCHING", CYAN, ""]);
  useV((s) => s.ui);
  const below = size >= 160 && width < size + 140;
  const n = Math.round(size / 2); // two screen pixels to one of its: four times the terminal's finest
  useEffect(() => {
    let raf = 0, lastKey = "", lastT = 0;
    const loop = (t: number) => {
      raf = requestAnimationFrame(loop);
      if (t - lastT < 33) return;
      lastT = t;
      oni.sup.motion = !matchMedia("(prefers-reduced-motion: reduce)").matches;
      const [cur, e, key] = oni.sup.look();
      if (key + n === lastKey) return;
      lastKey = key + n;
      if (canvas.current) paint(canvas.current, faceCached(e, n));
      setWords((w) => (w[0] === cur.mood && w[1] === cur.color && w[2] === oni.sup.last ? w : [cur.mood, cur.color, oni.sup.last]));
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [n]);
  const [mood, color, last] = words;
  const alarm = mood === "ALARMED" || mood === "HOSTILE";
  return (
    <div className={`super ${below ? "below" : ""}`} role="button" tabIndex={0} aria-label="Open Superintendent findings"
      title={`${TIPS.super} Click, or press F8, for its findings.`} onClick={() => oni.onFindings?.()} style={{ cursor: "pointer" }}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); oni.onFindings?.(); } }}>
      <canvas ref={canvas} className="px" style={{ width: size, height: size, flex: "none", borderRadius: "50%", boxShadow: alarm ? `0 0 10px ${blend(RED, INK, 0.6)}` : undefined }} />
      <div className="words">
        <div style={{ color: DIM }}>SUPERINTENDENT</div>
        <div style={{ color }}>{mood}</div>
        {size >= 96 && !below ? <div>&nbsp;</div> : null}
        <div className="said" style={{ color: last ? WHITE : DIM }}>{last || "all quiet"}</div>
      </div>
    </div>
  );
}

function Uplink() {
  useV((s) => s.ui);
  return (
    <div className="uplink">
      <div><span style={{ color: DIM }}>OPERATOR  </span><span style={{ color: AMBER }}>{oni.by}</span></div>
      {[...oni.tunnels].map(([dest, t]) => {
        const [g, c] = t.state === "up" ? ["◉", GREEN] : t.state === "opening" ? ["◌", AMBER] : ["○", RED];
        return <div key={dest} style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}><span style={{ color: DIM }}>SSH {dest}  </span><span style={{ color: c }}>{g} {t.state.toUpperCase()}</span></div>;
      })}
      <div><span style={{ color: DIM }}>ADDRESSES </span><span style={{ color: oni.redact ? RED : GREEN }}>{oni.redact ? "REDACTED" : "VISIBLE"}</span></div>
    </div>
  );
}

/** The biggest face that fits with every station card showing; failing that the list gives up rows (it scrolls) to
 *  two cards, then one; failing that the smallest face. */
function faceSize(h: number, cards: number, card: number) {
  const fixed = 40 + 70 + 30; // the add button, the uplink, the strip's own padding
  for (const [size, keep] of [[192, cards], [160, cards], [128, cards], [96, cards], [64, cards], [128, 2], [96, 2], [64, 2], [48, 1]] as const) {
    const words = size >= 192 ? 84 : 0;
    if (h - fixed - Math.min(cards, keep) * card - size - words >= 0) return size;
  }
  return 48;
}

export function Sidebar({ onSetup }: { onSetup: () => void }) {
  const [ref, { w, h }] = useSize<HTMLDivElement>();
  useV((s) => s.cards);
  const [cw, setCw] = useState(8.1);
  useLayoutEffect(() => {
    const c = document.createElement("canvas").getContext("2d")!;
    c.font = getComputedStyle(document.body).font;
    setCw(c.measureText("M").width || 8.1);
  }, []);
  const card = oni.fleet ? 42 : 83;
  const size = faceSize(h, oni.stations.length, card);
  return (
    <div className="sidebar" ref={ref}>
      <Stations cols={Math.floor((w - 20) / cw)} />
      <button className="add" onClick={() => addServer(onSetup)} title={TIPS["add-server"]}>+ ADD SERVER</button>
      <Crest />
      <Super size={size} width={w - 20} />
      <Uplink />
    </div>
  );
}
