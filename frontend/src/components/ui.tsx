// The pieces every tab is built from: coloured text, a titled panel, a table that keeps its cursor on the row the
// operator chose, a canvas for pixel art, a checkbox and a JSON view.
import { memo, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type KeyboardEvent } from "react";
import { paint, type Pic } from "../art/pixels";
import type { Line } from "../lib/util";

/** One line of coloured spans. */
export const L = memo(function L({ line }: { line: Line }) {
  return (
    <>
      {line.map(([t, c, b], i) =>
        b ? <span key={i} className={b === "#FFD27A" ? "medal" : undefined} style={b === "#FFD27A" ? undefined : { color: c, background: b }}>{t}</span>
          : c ? <span key={i} style={{ color: c }}>{t}</span> : <span key={i}>{t}</span>)}
    </>
  );
});

export function Panel(p: { title?: ReactNode; sub?: ReactNode; className?: string; children: ReactNode; style?: React.CSSProperties; tabIndex?: number; onKeyDown?: (e: KeyboardEvent) => void; col?: boolean }) {
  return (
    <div className={`panel ${p.className ?? ""}`} style={p.style} tabIndex={p.tabIndex} onKeyDown={p.onKeyDown}>
      {p.title ? <div className="ptitle">{p.title}</div> : null}
      <div className={p.col ? "pbody col" : "pbody"}>{p.children}</div>
      {p.sub ? <div className="psub">{p.sub}</div> : null}
    </div>
  );
}

export type Cell = Line | string;
export interface Row { id: string; cells: Cell[] }

/** A table refreshed in place. The cursor stays with the selected row's id when rows come, go or reorder: an unban
 *  must hit the entry the operator chose. ↑ ↓ PageUp PageDown Home End move it while the table has focus. */
export function Table(p: { cols: string[]; rows: Row[]; selected?: string | null; onSelect?: (id: string) => void; cursor?: boolean; id?: string;
  onEnter?: (id: string) => void }) {
  const { rows, selected, onSelect, cursor = true } = p;
  const ref = useRef<HTMLTableElement>(null);
  const idx = rows.findIndex((r) => r.id === selected);
  useEffect(() => {
    if (cursor && onSelect && rows.length && idx < 0) onSelect(rows[0].id);
  }, [cursor, onSelect, rows, idx]);
  useLayoutEffect(() => {
    if (idx < 0) return;
    const tr = ref.current?.tBodies[0]?.rows[idx];
    tr?.scrollIntoView({ block: "nearest" });
  }, [idx]);
  const move = (d: number) => {
    if (!rows.length || !onSelect) return;
    const i = Math.max(0, Math.min(rows.length - 1, (idx < 0 ? 0 : idx) + d));
    onSelect(rows[i].id);
  };
  const key = (e: KeyboardEvent) => {
    const page = Math.max(1, Math.floor((ref.current?.parentElement?.clientHeight ?? 200) / 20) - 2);
    const m: Record<string, number> = { ArrowDown: 1, ArrowUp: -1, PageDown: page, PageUp: -page, Home: -1e9, End: 1e9 };
    if (e.key in m) {
      e.preventDefault();
      e.stopPropagation();
      move(m[e.key]);
    } else if (e.key === "Enter" && selected && p.onEnter) p.onEnter(selected);
  };
  return (
    <div className="tblwrap">
      <table className={`tbl ${cursor ? "" : "nocursor"}`} ref={ref} tabIndex={cursor ? 0 : -1} onKeyDown={key} id={p.id}>
        <thead><tr>{p.cols.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.map((r) => (
            <TRow key={r.id} row={r} cur={cursor && r.id === selected} onClick={() => onSelect?.(r.id)} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

const sameRow = (a: Row, b: Row) => a.cells.length === b.cells.length && a.cells.every((c, i) => JSON.stringify(c) === JSON.stringify(b.cells[i]));

/** A row repaints only when what it shows changed: a poll that finds the same numbers redraws nothing. */
const TRow = memo(function TRow({ row, cur, onClick }: { row: Row; cur: boolean; onClick: () => void }) {
  return (
    <tr className={cur ? "cur" : undefined} onMouseDown={onClick}>
      {row.cells.map((c, i) => <td key={i}>{typeof c === "string" ? c : <L line={c} />}</td>)}
    </tr>
  );
}, (a, b) => a.cur === b.cur && sameRow(a.row, b.row));

/** A picture drawn at its own resolution, scaled up crisp to `width` CSS pixels. */
export const Pixel = memo(function Pixel({ pic, width, title }: { pic: Pic | null; width?: number; title?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useLayoutEffect(() => {
    if (ref.current) paint(ref.current, pic);
  }, [pic]);
  const w = width ?? pic?.w ?? 0, h = pic ? (w * pic.h) / pic.w : 0;
  return <canvas ref={ref} className="px" style={{ width: w, height: h, visibility: pic ? "visible" : "hidden" }} title={title} />;
});

export function Check({ label, on, onChange, title }: { label: string; on: boolean; onChange: (v: boolean) => void; title?: string }) {
  return (
    <span className={`check ${on ? "" : "off"}`} role="checkbox" aria-checked={on} onClick={() => onChange(!on)} title={title} tabIndex={0}
      onKeyDown={(e) => (e.key === " " || e.key === "Enter") && (e.preventDefault(), onChange(!on))}>
      <span className="box">{on ? "▣" : "□"}</span>{label}
    </span>
  );
}

/** JSON as the terminal's rich JSON drew it: keys cyan, strings green, numbers amber. */
export const Json = memo(function Json({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2) ?? "null";
  const parts: ReactNode[] = [];
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\b|\b(true|false|null)\b/g;
  let last = 0, m: RegExpExecArray | null, k = 0;
  while ((m = re.exec(text))) {
    parts.push(text.slice(last, m.index));
    if (m[1]) parts.push(<span key={k++} className={m[2] ? "k" : "s"}>{m[1]}</span>, m[2] ?? "");
    else if (m[3]) parts.push(<span key={k++} className="n">{m[3]}</span>);
    else parts.push(<span key={k++} className="b">{m[4]}</span>);
    last = re.lastIndex;
  }
  parts.push(text.slice(last));
  return <pre className="json sel-text">{parts}</pre>;
});

/** The size of an element, as it lays out. */
export function useSize<T extends HTMLElement>(): [React.RefObject<T | null>, { w: number; h: number }] {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setSize({ w: Math.round(e.contentRect.width), h: Math.round(e.contentRect.height) }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size];
}

export function Btn(p: { label: string; variant?: string; onClick: () => void; disabled?: boolean; title?: string; id?: string }) {
  return <button id={p.id} className={`btn ${p.variant ?? ""}`} onClick={p.onClick} disabled={p.disabled} title={p.title}>{p.label}</button>;
}
