// The Halo ASCII Archive: the boot sequence's pixel art and timeline, from the design project (halo-archive.jsx), as
// the terminal console drew it, now at any resolution. Every picture is a function of the clock: the ONI emblem, Section
// Three's lattice, Installation 04 turning, 343 Guilty Spark and the Superintendent.
import emblemText from "./emblem.txt?raw";
import { AMBER, CYAN, DIM, GOLD, GREEN, GREY, RED, WHITE, blend, decrypt, noise, rgb, type RGB } from "./palette";
import { Pic, smoothCached, tones } from "./pixels";

// The ONI emblem as traced pixel grids, largest first; tones 1 dark face, 2 rays, 3 bright face.
export const EMBLEMS: string[][] = emblemText
  .replace(/^#.*\r?\n/gm, "") // CRLF on a Windows checkout: without the \r the comments stay and become a 1 pixel wide "grid"
  .trim()
  .split(/\n\s*\n/)
  .map((b) => b.split(/\s+/).filter(Boolean));
const EMBLEM_TONES = tones({ "1": "#2C2415", "2": "#6F5626", "3": AMBER });
const SCAN_GOLD = rgb(GOLD);

const GUNGNIR = [
  "0000000000000000003333000000000000000000", "0000330000000000033333300000000000330000",
  "0003333000000000333333330000000003333000", "0033333300000003333333333000000033333300",
  "0033333330000033333333333300000333333300", "0003333333000333333333333330003333333000",
  "0000333333303333333003333333033333330000", "0000033333333333330000333333333333300000",
  "0000003333333333300000033333333333000000", "0000000333333333000000003333333330000000",
  "0000000033333330000000000333333300000000", "0000000333333333000000003333333330000000",
  "0000003333333333300000033333333333000000", "0000033333333333330000333333333333300000",
  "0000333333303333333003333333033333330000", "0003333333000333333333333330003333333000",
  "0033333330000033333333333300000333333300", "0333333300000003333333333000000033333330",
  "0333333000000000333333330000000003333330", "0333333000000000333333330000000003333330",
  "0333333300000003333333333000000033333330", "0033333330000033333333333300000333333300",
  "0003333333000333333333333330003333333000", "0000333333303333333003333333033333330000",
  "0000033333333333330000333333333333300000", "0000003333333333300000033333333333000000",
  "0000000333333333000000003333333330000000", "0000000033333330000000000333333300000000",
  "0000000333333333000000003333333330000000", "0000003333333333300000033333333333000000",
  "0000033333333333330000333333333333300000", "0000333333303333333003333333033333330000",
  "0003333333000333333333333330003333333000", "0033333330000033333333333300000333333300",
  "0033333300000003333333333000000033333300", "0003333000000000333333330000000003333000",
  "0000330000000000033333300000000000330000", "0000000000000000003333000000000000000000"];
// the lattice with its dark edge marked as tone 2, as the design draws it
const GUNGNIR_EDGED = GUNGNIR.map((row, y) =>
  [...row].map((d, x) => {
    if (d !== "3") return "0";
    const on = (x: number, y: number) => y >= 0 && y < GUNGNIR.length && x >= 0 && x < row.length && GUNGNIR[y][x] === "3";
    return on(x - 1, y) && on(x + 1, y) && on(x, y - 1) && on(x, y + 1) ? "3" : "2";
  }).join(""));
const GUNGNIR_TONES = tones({ "2": "#6F5626", "3": AMBER });

const METAL = ["#161C23", "#252D37", "#3E4752", "#5C6773", "#8B95A1", "#C5CCD5"].map(rgb);
export const FACE_EYE = "#EEFFF2";
const BRIGHT = rgb("#BFF3FF"); // the scan on the ring and the spark

export const cl = (v: number, a = 0, b = 1) => Math.max(a, Math.min(b, v));
export const easeOutCubic = (p: number) => 1 - (1 - cl(p)) ** 3;
export const easeInOutSine = (p: number) => -(Math.cos(Math.PI * cl(p)) - 1) / 2;

/** Pixels dissolve in out of noise, from the centre or along a diagonal; a bright scan sweeps down once. `k` is how
 *  many of these pixels make one of the design's, so the scan keeps its thickness at any size. */
export function materialise(src: Pic, reveal: number, scan: number | null, scanColor: RGB | null, mode: "radial" | "diag", k: number): Pic | null {
  if (reveal <= 0) return null;
  const { w, h } = src;
  const out = src.clone();
  const far = Math.hypot(w / 2, h / 2);
  const line = scan === null ? null : scan * (h + 4 * k) - 2 * k;
  for (let y = 0; y < h; y++) {
    const onScan = line !== null && Math.abs(y - line) < 1.5 * k;
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      if (!out.data[i + 3]) continue;
      if (reveal < 1) {
        const m = mode === "diag"
          ? 0.45 * noise(x, y) + 0.55 * (x + y) / (w + h)
          : 0.55 * noise(x, y) + 0.45 * Math.hypot(x - w / 2, y - h / 2) / far;
        if (m >= reveal) {
          out.data[i + 3] = 0;
          continue;
        }
      }
      if (onScan && scanColor) {
        out.data[i] = scanColor[0];
        out.data[i + 1] = scanColor[1];
        out.data[i + 2] = scanColor[2];
      }
    }
  }
  return out;
}

/** The ONI emblem, `w` pixels across. */
export function emblem(w: number, reveal = 1, scan: number | null = null): Pic | null {
  const pic = smoothCached("emblem", EMBLEMS[0], EMBLEM_TONES, Math.max(12, Math.round(w)));
  if (reveal >= 1 && scan === null) return pic;
  return materialise(pic, reveal, scan, SCAN_GOLD, "radial", pic.w / 48);
}

/** Section Three's lattice, with an optional glint along a diagonal (in the design's pixels). */
export function gungnir(w: number, glint: number | null): Pic {
  const base = smoothCached("gungnir", GUNGNIR_EDGED, GUNGNIR_TONES, w);
  if (glint === null) return base;
  const out = base.clone();
  const k = out.w / GUNGNIR[0].length;
  const gold = rgb(GOLD);
  for (let y = 0; y < out.h; y++) for (let x = 0; x < out.w; x++) {
    if (out.alpha(x, y) && Math.abs(x / k - y / k - glint) < 2.2) {
      const i = (y * out.w + x) * 4;
      out.data[i] = gold[0]; out.data[i + 1] = gold[1]; out.data[i + 2] = gold[2];
    }
  }
  return out;
}

function vnoise(u: number, v: number): number {
  const x0 = Math.floor(u), y0 = Math.floor(v), fx = u - x0, fy = v - y0;
  const s = (t: number) => t * t * (3 - 2 * t);
  const n = (x: number, y: number) => noise(((x % 48) + 48) % 48, y + 7);
  const a = n(x0, y0), b = n(x0 + 1, y0), c = n(x0, y0 + 1), d = n(x0 + 1, y0 + 1);
  return a + (b - a) * s(fx) + (c - a) * s(fy) + (a - b - c + d) * s(fx) * s(fy);
}

const C = (h: string) => rgb(h);
const RING = { sea: C("#2E7F95"), seaDark: C("#1F5664"), land: C(GREEN), landDark: C("#3C7A5A"), cloud: C(WHITE),
  rim: C(GREY), rimDark: C(DIM), hull: C("#3A434E"), hullDark: C("#262E37"), stripe: C(DIM) };

/** Installation 04: a thin band tilted toward us, spinning about its axis, land, sea and cloud on its inner face.
 *  `k` scales the design's 80 x 48. */
export function ring(spin: number, tilt: number, k: number): Pic {
  const W = Math.round(80 * k), H = Math.round(48 * k), cx = 40 * k, cy = 24 * k, R = 35 * k, b = 4.5 * k;
  const pic = new Pic(W, H);
  const zb = new Float32Array(W * H).fill(-1e9);
  const ct = Math.cos(tilt), st = Math.sin(tilt);
  const steps = Math.round(1100 * k), dv = 0.45;
  for (let i = 0; i < steps; i++) {
    const th = (i / steps) * Math.PI * 2;
    const c = Math.cos(th), s = Math.sin(th);
    const inner = -s * ct > 0;
    const lon = (((th + spin) / (Math.PI * 2)) * 48) % 48;
    const lit = Math.abs(s) > 0.35;
    for (let v = -b; v <= b; v += dv) {
      const x = R * c, z = R * s;
      const yp = v * ct + z * st, zp = -v * st + z * ct;
      const X = Math.floor(cx + x), Y = Math.floor(cy + yp);
      if (X < 0 || Y < 0 || X >= W || Y >= H || zp < zb[Y * W + X]) continue;
      zb[Y * W + X] = zp;
      let col: RGB;
      if (Math.abs(v) > b - 0.9 * k) col = lit ? RING.rim : RING.rimDark;
      else if (inner) {
        const n = vnoise(((lon + 48) % 48) / 1.6, (v / k + 4.5) / 3);
        col = n < 0.42 ? (lit ? RING.sea : RING.seaDark) : n < 0.8 ? (lit ? RING.land : RING.landDark) : RING.cloud;
      } else col = Math.floor(lon * 2) % 7 === 0 ? RING.stripe : lit ? RING.hull : RING.hullDark;
      pic.set(X, Y, col);
    }
  }
  return pic;
}

/** 343 Guilty Spark: a lit metal sphere with seams, one blue eye, turning and bobbing. `k` scales the design's 48. */
export function spark(yaw: number, bob: number, glow: number, k: number): Pic {
  const N = Math.round(48 * k), cx = 24 * k, cy = (24 + bob) * k, R = 15.5 * k;
  const pic = new Pic(N, N);
  const L = [-0.5, -0.62, 0.6], ln = Math.hypot(...L);
  const [lx, ly, lz] = L.map((v) => v / ln);
  const ex = Math.sin(yaw), ez = Math.cos(yaw);
  const eyeMid = rgb(blend(CYAN, "#2E7F95", glow)), core = rgb(blend("#E6FBFF", CYAN, glow));
  const halo = rgb("#12303A"), socket = rgb("#0E1318");
  for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) {
    const nx = (x + 0.5 - cx) / R, ny = (y + 0.5 - cy) / R;
    const r2 = nx * nx + ny * ny;
    if (r2 > 1) {
      const d = Math.sqrt(r2), eyeX = cx + ex * R * 0.8;
      if (ez > 0.2 && d < 1.12 && Math.hypot(x + 0.5 - eyeX, y + 0.5 - cy) < 7 * k && glow > 0.55) pic.set(x, y, halo);
      continue;
    }
    const nz = Math.sqrt(1 - r2);
    const a = Math.acos(cl(nx * ex + nz * ez, -1, 1));
    if (a < 0.14) pic.set(x, y, core);
    else if (a < 0.33) pic.set(x, y, eyeMid);
    else if (a < 0.42) pic.set(x, y, socket);
    else {
      const lon = Math.atan2(nx * Math.cos(yaw) - nz * Math.sin(yaw), nx * Math.sin(yaw) + nz * Math.cos(yaw));
      const lam = cl(nx * lx + ny * ly + nz * lz);
      let q = Math.min(5, Math.floor(lam * 5.2) + (r2 > 0.86 ? 0 : 1));
      if (Math.abs(ny) < 0.06 || Math.abs(Math.sin(2 * lon)) < 0.09) q = Math.max(0, q - 2);
      pic.set(x, y, METAL[q]);
    }
  }
  return pic;
}

// --- the Superintendent's face ----------------------------------------------------------------------------------
export interface Expr {
  size: number; // eye size: 1.26 is wide-eyed alarm
  ex: number; // eyes glance right, toward the feed
  ey: number;
  lid: number; // the top lid coming down (0 open, 0.5 half)
  tilt: number; // lids slanted in: a frown
  happy: boolean; // the eyes become smiling arcs
  openL: number; // 1 open, 0 closed: a blink
  openR: number;
  alarm: number; // above half, the face flushes red
}

export const BASE: Expr = { size: 1, ex: 2.5, ey: 0, lid: 0, tilt: 0, happy: false, openL: 1, openR: 1, alarm: 0 };

function faceTones(e: Expr): Record<string, RGB> {
  const al = e.alarm;
  const rim = (c: string) => rgb(al > 0 ? blend(c, al > 0.5 ? RED : c, 1 - al * 0.6) : c);
  return { a: rim("#7FD18F"), b: rim("#A8E8B2"), c: rim("#4E8F5E"), d: rim("#24482D"), e: rim("#2D5A38"), w: rgb(FACE_EYE) };
}

/** What the design's face is at a point in its 48 pixel space: null outside the disc, "w" an eye, "a" "b" "c" the
 *  rim from the edge in, "d" or "e" the scanlines. */
export function faceClass(e: Expr, px: number, py: number, apart = 0): string | null {
  const R = 22.5, d = Math.hypot(px - 24, py - 24);
  if (d > R) return null;
  for (const side of [-1, 1]) {
    if (Math.abs(px - 24 - e.ex) < apart) break;
    const r = 6.4 * e.size;
    const dx = (px - (24 + side * 8.6 + e.ex)) / r, dy = (py - (24 + e.ey)) / r;
    if (e.happy) {
      const dd = Math.hypot(dx, dy * 1.1);
      if (dd <= 1 && dd >= 0.52 && dy <= 0.08) return "w";
    } else if (
      Math.hypot(dx, dy / Math.max(side < 0 ? e.openL : e.openR, 0.1)) <= 1 &&
      dy >= -1 + 2 * e.lid + e.tilt * -side * dx
    ) return "w";
  }
  return d > R - 1.3 ? "a" : d > R - 2.8 ? "b" : d > R - 4.4 ? "c" : Math.floor(py) % 2 ? "d" : "e";
}

const CLASSES = ["a", "b", "c", "d", "e", "w"];

/** How much of each pixel of an n-pixel face is each of its classes (rim tones, scanlines, eye), from ss x ss
 *  points of the design: the face's shape, which the alarm's colour doesn't change. */
function faceShape(e: Expr, n: number, ss: number): Float32Array {
  const k = 45 / n; // the design's disc is 45 of its 48: here it fills the n exactly
  // a gap kept between wide eyes only where the design's sliver between them is under a pixel: at this size it isn't
  const gap = k < 0.75 ? 0 : e.size > 1.15 ? Math.max(2.2, k) : 2.2;
  const out = new Float32Array(n * n * 6);
  const w = 1 / (ss * ss);
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    for (let sb = 0; sb < ss; sb++) for (let sa = 0; sa < ss; sa++) {
      const c = faceClass(e, 24 + (i + (sa + 0.5) / ss - n / 2) * k, 24 + (j + (sb + 0.5) / ss - n / 2) * k, gap);
      if (c !== null) out[(j * n + i) * 6 + CLASSES.indexOf(c)] += w;
    }
  }
  return out;
}

const shapes = new Map<string, Float32Array>();
const q2 = (v: number) => v.toFixed(2);

/** The face `n` pixels across, each pixel the average of ss x ss points of the design: crisp eyes and rim, smooth
 *  edges, the scanlines kept at the design's own pitch whatever the size. Shapes are kept, so an alarm pulsing the
 *  rim's colour only recolours. */
export function face(e: Expr, n: number, ss = 4): Pic {
  const key = [n, ss, q2(e.size), q2(e.ex), q2(e.ey), q2(e.lid), q2(e.tilt), e.happy, q2(e.openL), q2(e.openR)].join("|");
  let shape = shapes.get(key);
  if (!shape) {
    shape = faceShape(e, n, ss);
    if (shapes.size > 400) shapes.delete(shapes.keys().next().value!);
    shapes.set(key, shape);
  }
  const t = faceTones(e), cols = CLASSES.map((c) => t[c]);
  const pic = new Pic(n, n);
  for (let p = 0; p < n * n; p++) {
    let r = 0, g = 0, b = 0, lit = 0;
    for (let c = 0; c < 6; c++) {
      const v = shape[p * 6 + c];
      if (!v) continue;
      r += cols[c][0] * v; g += cols[c][1] * v; b += cols[c][2] * v; lit += v;
    }
    if (lit) {
      const i = p * 4;
      pic.data[i] = r / lit; pic.data[i + 1] = g / lit; pic.data[i + 2] = b / lit;
      pic.data[i + 3] = Math.min(1, lit * 1.15) * 255;
    }
  }
  return pic;
}

const faces = new Map<string, Pic>();

/** face(), kept: a blink or a glance takes a few dozen distinct pictures, and each is drawn once. */
export function faceCached(e: Expr, n: number): Pic {
  const key = `${n}|${Object.values(e).map((v) => (typeof v === "number" ? v.toFixed(2) : v)).join("|")}`;
  let p = faces.get(key);
  if (!p) {
    p = face(e, n);
    if (faces.size > 600) faces.delete(faces.keys().next().value!);
    faces.set(key, p);
  }
  return p;
}

// --- the sequence -----------------------------------------------------------------------------------------------
export const SCENES: [string, number][] = [["ONI", 4.5], ["SectionThree", 5.0], ["Installation04", 6.5], ["GuiltySpark", 5.5], ["Superintendent", 7.5]];
export const TOTAL = SCENES.reduce((a, [, d]) => a + d, 0);
export const FILES: [string, string][] = [["FILE 01  ONI EMBLEM", "ONI"], ["FILE 02  SECTION THREE", "SectionThree"],
  ["FILE 03  INSTALLATION 04", "Installation04"], ["FILE 04  343 GUILTY SPARK", "GuiltySpark"], ["FILE 05  SUPERINTENDENT", "Superintendent"]];
const FILE_COUNT = FILES.length + 1;
const DECRYPT_FOR = 1.4;

const TITLES: Record<string, [string, string, string, string, string]> = {
  ONI: ["OFFICE OF NAVAL INTELLIGENCE", AMBER, "SECTION THREE  ·  REMOTE CONSOLE TERMINAL  ·  ", "TOP SECRET", RED],
  SectionThree: ["SECTION THREE", AMBER, "OFFICE OF NAVAL INTELLIGENCE  ·  GUNGNIR  ·  ", "BLACK OPERATIONS", RED],
  Installation04: ["INSTALLATION 04", CYAN, "FORERUNNER ARRAY  ·  10,000 KM  ·  ", "CLASSIFIED", RED],
  GuiltySpark: ["343 GUILTY SPARK", CYAN, "MONITOR  ·  INSTALLATION 04  ·  ", "RECLAIMER DETECTED", AMBER],
  Superintendent: ["SUPERINTENDENT", GREEN, "NEW MOMBASA MUNICIPAL AI  ·  MOOD  ", "ONLINE", GREEN],
};

export const spaced = (s: string) => s.split(" ").map((w) => [...w].join(" ")).join("   ");

function superMood(lt: number): [string, string] {
  return lt < 2.25 ? ["ONLINE", GREEN] : lt < 3.75 ? ["WATCHFUL", CYAN] : lt < 5.2 ? ["PLEASED", GREEN] : lt < 6.1 ? ["ALARMED", RED] : ["ONLINE", GREEN];
}

function superExpr(lt: number): Expr {
  const k = (a: number, b: number) => easeOutCubic((lt - a) / (b - a));
  const blink = (at: number) => cl(1 - Math.abs(lt - at - 0.11) / 0.11);
  const op = 1 - blink(1.8) - blink(6.35);
  return { ...BASE, size: 1 + 0.26 * k(5.25, 5.4) - 0.26 * k(6.0, 6.2), ex: -3 * k(2.25, 2.5) + 6 * k(2.85, 3.1) - 3 * k(3.4, 3.6),
    ey: -1 * k(5.25, 5.4) + 1 * k(6.0, 6.2), happy: lt > 3.85 && lt < 5.05, openL: op, openR: op };
}

export interface Frame {
  scene: string;
  art: Pic | null;
  title: string;
  sub: string;
  tag: string;
  color: string;
  tagColor: string;
  files: [string, boolean][];
  progress: number;
}

function locate(t: number): [string, number, number, number] {
  let start = 0;
  for (const [name, dur] of SCENES) {
    if (t < start + dur) return [name, t - start, start, start + dur];
    start += dur;
  }
  const [name, dur] = SCENES[SCENES.length - 1];
  return [name, dur, start - dur, start];
}

/** The whole screen at t seconds, its art `px` pixels tall: a pure function of the clock, like the design's Piece. */
export function frame(t: number, px: number): Frame {
  const [scene, lt, , end] = locate(t);
  const rev = (inDur = 1.2) => easeInOutSine(lt / inDur) * (1 - easeInOutSine((t - (end - 0.6)) / 0.6));
  const scanAt = (a = 1) => { const p = lt - a; return p > 0 && p < 1 ? p : null; };
  let art: Pic | null = null;
  if (scene === "ONI") art = emblem(px, rev(1.3), scanAt(0.9));
  else if (scene === "SectionThree") {
    const glint = lt > 2.3 && lt < 3.5 ? -44 + ((lt - 2.3) / 1.2) * 88 : null;
    const g = gungnir(Math.round(px * 40 / 38), glint);
    art = materialise(g, rev(1.6), null, null, "diag", g.w / 40);
  } else if (scene === "Installation04") {
    const k = px / 48;
    art = materialise(ring(t * 0.55, ((6 + 30 * easeInOutSine(lt / 4.5)) * Math.PI) / 180, k), rev(), scanAt(1.2), BRIGHT, "radial", k);
  } else if (scene === "GuiltySpark") {
    const k = px / 48;
    art = materialise(spark(0.55 * Math.sin(lt * 1.25), 1.4 * Math.sin(lt * 2.1), 0.5 + 0.5 * Math.sin(lt * 5), k), rev(), scanAt(), BRIGHT, "radial", k);
  } else {
    const n = Math.round(px * 45 / 48);
    art = materialise(face(superExpr(lt), n, 3), rev(1.2), scanAt(), rgb("#D9FFE0"), "radial", n / 45);
  }
  let [title, color, sub, tag, tagColor] = TITLES[scene];
  if (scene === "Superintendent") [tag, tagColor] = superMood(lt);
  const starts: Record<string, number> = {};
  SCENES.reduce((s, [n, d]) => ((starts[n] = s), s + d), 0);
  const files = FILES.filter(([, n]) => t >= starts[n]).map(([l, n]) => [l, t >= starts[n] + DECRYPT_FOR] as [string, boolean]);
  return { scene, art, title: decrypt(spaced(title), lt / 0.8, Math.floor(t / 0.07)), sub, tag, color, tagColor, files,
    progress: cl(files.filter(([, d]) => d).length / FILE_COUNT) };
}

/** A biometric signature: a mirrored pixel glyph drawn from a hash of a player ID, in their colour, smoothed. */
export async function biosig(seed: string, color: string, size = 12, px = 96): Promise<Pic> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-512", new TextEncoder().encode(seed)));
  // python's int.from_bytes(big) & 7, then >> 3: read the bits from the least significant end
  let bits = 0n;
  for (const b of digest) bits = (bits << 8n) | BigInt(b);
  const rows: string[] = [];
  for (let r = 0; r < size; r++) {
    let half = "";
    for (let c = 0; c < (size + 1) >> 1; c++) {
      half += "00001123"[Number(bits & 7n)];
      bits >>= 3n;
    }
    rows.push(half + [...half].reverse().slice(size % 2).join(""));
  }
  const t = tones({ "1": blend(color, "#06080B", 0.3), "2": blend(color, "#06080B", 0.6), "3": color });
  // a glyph is meant to look like a glyph: drawn blocky (nearest), not smoothed into blobs
  const k = Math.floor(px / size);
  const pic = new Pic(size * k, size * k);
  for (let y = 0; y < size * k; y++) for (let x = 0; x < size * k; x++) {
    const ch = rows[Math.floor(y / k)][Math.floor(x / k)];
    if (ch !== "0") pic.set(x, y, t[ch]);
  }
  return pic;
}
