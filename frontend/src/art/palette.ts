// The console's colours: charcoal, amber primary, holo cyan, red for anything that bites. The same as the terminal.
export const AMBER = "#D9A441", CYAN = "#4FC3D9", RED = "#E5484D", GREEN = "#5FB98A", DIM = "#5C6773", WHITE = "#D6DCE4";
export const GREY = "#8B95A1", GOLD = "#FFD27A", INK = "#06080B";

export type RGB = [number, number, number];

export function rgb(hex: string): RGB {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function hex([r, g, b]: RGB): string {
  return "#" + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("").toUpperCase();
}

/** The colour t of the way from b to a: blend(a, b, 1) is a. */
export function blend(a: string, b: string, t: number): string {
  const ca = rgb(a), cb = rgb(b);
  return hex([0, 1, 2].map((i) => cb[i] + (ca[i] - cb[i]) * t) as RGB);
}

/** A fixed pseudo-random number in [0, 1) for a pixel: the same pixel always gets the same one. */
export function noise(x: number, y: number): number {
  let h = (Math.imul(x, 374761393) + Math.imul(y, 668265263)) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177) >>> 0;
  return ((h ^ (h >>> 16)) >>> 0) / 2 ** 32;
}

export const NOISE = "▓▒░▚▞▙▟#%&@$*+=<>/\\";

/** The text t (0 to 1) of the way through decrypting: characters resolve left to right out of noise. */
export function decrypt(text: string, t: number, frame = 0): string {
  const n = Math.max(text.length, 1);
  let out = "";
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    out += c === " " || t >= 1 || i / n + 0.3 * noise(i, 99) < t * 1.3 ? c : NOISE[Math.floor(noise(i, frame) * NOISE.length)];
  }
  return out;
}

export const TEAMS = ["red", "blue", "green", "orange", "purple", "gold", "brown", "pink"];
export const TEAM_COLOR: Record<string, string> = Object.fromEntries(
  TEAMS.map((t, i) => [t, ["#E5484D", "#4C8DFF", "#4CC27A", "#F08A24", "#A066E0", "#E8C547", "#A0714A", "#F07FB8"][i]]),
);
