// The boot: the Halo ASCII Archive (the ONI emblem, Section Three, Installation 04, 343 Guilty Spark and the
// Superintendent decrypt one file at a time), then the emblem returns for the clearance check while the stations
// sign in. Any key skips; F1 to F7 also open that tab.
import { useEffect, useRef, useState } from "react";
import { AMBER, DIM, GREEN, RED, WHITE } from "../art/palette";
import { FILES, TOTAL, emblem, frame } from "../art/archive";
import { paint } from "../art/pixels";
import { decrypt } from "../art/palette";
import { oni, TABS } from "../engine";
import { explain, gauge, type Line } from "../lib/util";
import { L } from "./ui";

const SPIN = "◐◓◑◒";
const PITCH = 2; // screen pixels to one of the art's

function dotted(k: string, v: string, color: string): Line {
  const dots = Math.max(3, Math.min(46 - k.length, 59 - k.length - v.length));
  return [[`> ${k} `, WHITE], [".".repeat(dots), DIM], [` ${v}`, color]];
}

function useSkip(onDone: () => void) {
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      e.preventDefault();
      const fn = /^F([1-7])$/.exec(e.key);
      if (fn) oni.setTab(TABS[Number(fn[1]) - 1]); // straight to that tab
      onDone();
    };
    const t = setTimeout(() => window.addEventListener("keydown", key), 150);
    window.addEventListener("mousedown", onDone);
    return () => {
      clearTimeout(t);
      window.removeEventListener("keydown", key);
      window.removeEventListener("mousedown", onDone);
    };
  }, [onDone]);
}

function useClock(fps = 30) {
  const [t, setT] = useState(0);
  useEffect(() => {
    const t0 = performance.now();
    let raf = 0, last = 0;
    const loop = (now: number) => {
      raf = requestAnimationFrame(loop);
      if (now - last < 1000 / fps) return;
      last = now;
      setT((now - t0) / 1000);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [fps]);
  return t;
}

const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

export function Archive({ onDone, onSkip }: { onDone: () => void; onSkip: () => void }) {
  useSkip(onSkip);
  const t = useClock(30);
  const canvas = useRef<HTMLCanvasElement>(null);
  const cssH = Math.max(160, Math.min(520, Math.floor((window.innerHeight - 330) / 4) * 4));
  const f = frame(Math.min(t, TOTAL - 0.001), cssH / PITCH);
  useEffect(() => {
    if (t >= TOTAL) onDone();
  }, [t, onDone]);
  useEffect(() => {
    if (canvas.current) paint(canvas.current, f.art);
  });
  const w = f.art ? f.art.w * PITCH : 0;
  return (
    <div className="boot">
      <div className="art" style={{ height: cssH }}>
        <canvas ref={canvas} className="px" style={{ width: w, height: f.art ? f.art.h * PITCH : 0 }} />
      </div>
      <div className="title">
        <div style={{ color: f.color }}>{f.title}</div>
        <div><span style={{ color: DIM }}>{f.sub}</span><span style={{ color: f.tagColor }}>{f.tag}</span></div>
      </div>
      <div className="bootlog" style={{ marginTop: 20 }}>
        {f.files.map(([label, done]) => <div key={label}><L line={dotted(label, done ? "DECRYPTED" : "DECRYPTING", done ? GREEN : AMBER)} /></div>)}
        {Array.from({ length: FILES.length - f.files.length }, (_, i) => <div key={i}>&nbsp;</div>)}
        <div><span style={{ color: WHITE }}>{"> "}</span><span style={{ color: AMBER }}>{Math.floor(t * 3) % 2 ? " " : "█"}</span></div>
        <br />
        <div><L line={[...gauge(f.progress, 56, AMBER), [` ${String(Math.round(f.progress * 100)).padStart(3)}%`, DIM]]} /></div>
      </div>
    </div>
  );
}

const HEADING = "O F F I C E   O F   N A V A L   I N T E L L I G E N C E";
const SHOW_DOWN = 5;

export function Boot({ onDone }: { onDone: () => void }) {
  useSkip(onDone);
  const el = useClock(20);
  const t = reduced() ? 9 : el;
  const done = useRef<number | null>(null);
  const cssH = Math.max(120, Math.min(480, Math.floor((window.innerHeight - 290 - 20 * (oni.stations.length + oni.tunnels.size)) / 4) * 4));
  const pic = emblem(cssH / PITCH, t / 0.9, t > 0.8 && t < 1.7 ? (t - 0.8) / 0.9 : null);
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (canvas.current) paint(canvas.current, pic);
  });
  const spin = SPIN[Math.floor(el * 8) % 4];
  const log: [string, string, string, boolean][] = [
    ["AUTHENTICATING OPERATOR", oni.by.toUpperCase(), AMBER, true],
    ["OPENING SECURE CHANNELS", `${oni.stations.length} STATION${oni.stations.length !== 1 ? "S" : ""}`, AMBER, true],
  ];
  for (const [dest, tun] of oni.tunnels) {
    const [v, c] = tun.state === "opening" ? [`${spin} OPENING`, AMBER] : tun.state === "up" ? ["ESTABLISHED", GREEN] : [explain(tun.detail, "offline", true), RED];
    log.push([`SSH TUNNEL ${dest.toUpperCase().slice(0, 34)}`, v, c, tun.state !== "opening"]);
  }
  const links = oni.stations.map((st, i) => [`[${i + 1}] ${st.label.toUpperCase().slice(0, 34)}`, ...oni.link(st, spin)] as [string, string, string, boolean]);
  if (oni.fleet) {
    const up = oni.stations.filter((s) => s.online).length, settled = links.every((l) => l[3]);
    log.push([`STATIONS 1-${links.length}`, `${settled ? "" : spin + " "}${up}/${links.length} SECURE`, up === links.length ? GREEN : !settled || up ? AMBER : RED, settled]);
    const down = links.filter((l) => l[3] && l[2] === RED);
    log.push(...down.slice(0, SHOW_DOWN));
    if (down.length > SHOW_DOWN) log.push([`AND ${down.length - SHOW_DOWN} MORE`, "SEE THE SIDEBAR", RED, true]);
  } else log.push(...links);
  if (oni.forgeStatus.key) log.push(["FORGE CATALOG", "RECLAIMERFORGE.NET", GREEN, true]);
  const steps = Math.floor(el / 0.22);
  const settled = log.every((l) => l[3]);
  const finished = (steps > log.length && settled) || el > 15;
  let clearance: Line | null = null;
  if (finished) {
    const ok = oni.stations.some((s) => s.online);
    clearance = dotted("CLEARANCE", ...((ok ? ["GRANTED", GREEN] : !settled ? ["PENDING", AMBER] : ["NO STATION REACHABLE", RED]) as [string, string]));
    done.current ??= el;
  }
  useEffect(() => {
    if (done.current !== null && el - done.current > 1.1) onDone();
  }, [el, onDone]);
  const progress = (log.slice(0, steps).filter((l) => l[3]).length + (finished ? 1 : 0)) / (log.length + 1);
  return (
    <div className="boot">
      <div className="art" style={{ height: cssH }}>
        <canvas ref={canvas} className="px" style={{ width: pic ? pic.w * PITCH : 0, height: pic ? pic.h * PITCH : 0 }} />
      </div>
      <div className="title">
        <div style={{ color: AMBER }}>{decrypt(HEADING, t / 0.8, Math.floor(t / 0.07))}</div>
        <div><span style={{ color: DIM }}>SECTION THREE  ·  REMOTE CONSOLE TERMINAL  ·  </span><span style={{ color: RED }}>TOP SECRET</span></div>
      </div>
      <div className="bootlog" style={{ marginTop: 20, minHeight: 20 * (log.length + 3) }}>
        {log.slice(0, steps).map(([k, v, c]) => <div key={k}><L line={dotted(k, v, c)} /></div>)}
        {clearance ? <div><L line={clearance} /></div>
          : <div><span style={{ color: WHITE }}>{"> "}</span><span style={{ color: AMBER }}>{Math.floor(el * 3) % 2 ? " " : "█"}</span></div>}
        <br />
        <div><L line={[...gauge(progress, 56, AMBER), [` ${String(Math.round(progress * 100)).padStart(3)}%`, DIM]]} /></div>
      </div>
    </div>
  );
}
