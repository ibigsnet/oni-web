// Pictures as RGBA pixels, and the smooth upscaler that draws the archive's hand-traced grids at any size.
//
// The terminal drew two pixels per cell in half blocks, so a 48 pixel emblem was all it had. Here each traced grid is
// sampled through a bilinear blend of its tones (one per tone, the strongest wins) at 4 x 4 points per output pixel:
// staircases become clean diagonals and curves, the edge is antialiased, and every colour is still one of the grid's
// own. The pixels stay visible on screen, just several times finer.
import { rgb, type RGB } from "./palette";

export class Pic {
  data: Uint8ClampedArray;
  constructor(public w: number, public h: number) {
    this.data = new Uint8ClampedArray(w * h * 4);
  }
  set(x: number, y: number, c: RGB, a = 1) {
    const i = (y * this.w + x) * 4;
    this.data[i] = c[0];
    this.data[i + 1] = c[1];
    this.data[i + 2] = c[2];
    this.data[i + 3] = a * 255;
  }
  alpha(x: number, y: number) {
    return this.data[(y * this.w + x) * 4 + 3];
  }
  clone() {
    const p = new Pic(this.w, this.h);
    p.data.set(this.data);
    return p;
  }
}

export type Tones = Record<string, RGB>;

export function tones(map: Record<string, string>): Tones {
  return Object.fromEntries(Object.entries(map).map(([k, v]) => [k, rgb(v)]));
}

/** A tone grid ("0" empty, any other character a tone) drawn `w` pixels wide, smoothed and antialiased. */
export function smooth(rows: string[], t: Tones, w: number, ss = 4): Pic {
  const gh = rows.length, gw = rows[0].length;
  const scale = w / gw;
  const W = Math.max(1, Math.round(gw * scale)), H = Math.max(1, Math.round(gh * scale));
  const pic = new Pic(W, H);
  const keys = Object.keys(t);
  const idx: Record<string, number> = Object.fromEntries(keys.map((k, i) => [k, i + 1]));
  const cells = new Uint8Array(gw * gh);
  for (let y = 0; y < gh; y++) for (let x = 0; x < gw; x++) cells[y * gw + x] = idx[rows[y][x]] ?? 0;
  const at = (x: number, y: number) => (x < 0 || y < 0 || x >= gw || y >= gh ? 0 : cells[y * gw + x]);
  const n = keys.length + 1;
  const weight = new Float32Array(n);
  const cols = keys.map((k) => t[k]);
  for (let Y = 0; Y < H; Y++) {
    for (let X = 0; X < W; X++) {
      let r = 0, g = 0, b = 0, lit = 0;
      for (let sb = 0; sb < ss; sb++) {
        for (let sa = 0; sa < ss; sa++) {
          const u = (X + (sa + 0.5) / ss) / scale - 0.5, v = (Y + (sb + 0.5) / ss) / scale - 0.5;
          const x0 = Math.floor(u), y0 = Math.floor(v), fx = u - x0, fy = v - y0;
          weight.fill(0);
          weight[at(x0, y0)] += (1 - fx) * (1 - fy);
          weight[at(x0 + 1, y0)] += fx * (1 - fy);
          weight[at(x0, y0 + 1)] += (1 - fx) * fy;
          weight[at(x0 + 1, y0 + 1)] += fx * fy;
          let best = 0;
          for (let i = 1; i < n; i++) if (weight[i] > weight[best]) best = i;
          if (best) {
            const c = cols[best - 1];
            r += c[0]; g += c[1]; b += c[2]; lit++;
          }
        }
      }
      if (lit) pic.set(X, Y, [r / lit, g / lit, b / lit], lit / (ss * ss));
    }
  }
  return pic;
}

const cache = new Map<string, Pic>();

/** smooth(), kept: the same grid at the same size is drawn once. */
export function smoothCached(key: string, rows: string[], t: Tones, w: number): Pic {
  const k = `${key}:${w}`;
  let p = cache.get(k);
  if (!p) {
    p = smooth(rows, t, w);
    if (cache.size > 64) cache.delete(cache.keys().next().value!);
    cache.set(k, p);
  }
  return p;
}

/** Paint a picture into a canvas at its own resolution; CSS scales it up crisp (image-rendering: pixelated). */
export function paint(canvas: HTMLCanvasElement, pic: Pic | null) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  if (!pic) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  if (canvas.width !== pic.w || canvas.height !== pic.h) {
    canvas.width = pic.w;
    canvas.height = pic.h;
  }
  ctx.putImageData(new ImageData(pic.data as unknown as Uint8ClampedArray<ArrayBuffer>, pic.w, pic.h), 0, 0);
}
