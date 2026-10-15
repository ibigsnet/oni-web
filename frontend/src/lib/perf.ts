// Server frame rate, as F7, F3 and the station cards show it. From ibigs/oni-rcon's perf.py and perf_view.py, by
// IBIGS. The backend reads the lag reports out of each server's log (health.rs); this is what they mean.
import { AMBER, GREEN, RED } from "../art/palette";
import type { Dict } from "./util";

export const TARGET = 60; // the server's frame rate when it keeps up
export const SLOW = 54; // what the server calls falling behind
export const BAD = 40; // under this play is visibly choppy
export const TARGET_MS = 1000 / TARGET;

export const level = (fps: number): "ok" | "warn" | "crit" => (fps >= SLOW ? "ok" : fps >= BAD ? "warn" : "crit");
export const LEVEL_COLOR = { ok: GREEN, warn: AMBER, crit: RED };

/** The chart's top in ms: room for the longest frame, never under 50 (3x the target), a round number, at most 500. */
export function scaleTop(hist: Dict[]): number {
  const peak = Math.max(50, ...hist.map((b) => b.longest || (b.fps ? 1000 / b.fps : 0)));
  const step = peak <= 150 ? 25 : 50;
  return Math.min(500, Math.ceil(peak / step) * step);
}

/** [what's happening, what to do], for someone who doesn't need to know what a thread is. `r` is a server's reading and
 *  `hist` its last 30 minutes, oldest first. */
export function advice(r: Dict | undefined, hist: Dict[]): [string, string] {
  if (!r) return ["Not measured yet.", "Frame rate comes from the server's log: health_cmd has to print it (see ? help)."];
  if (!r.behind && !r.slow_minutes) return [`Running smoothly: ${TARGET} frames a second.`, "Nothing to do."];
  const crowds = hist.filter((b) => b.players && b.fps !== null && b.fps < SLOW).map((b) => b.players as number);
  const crowd = crowds.length ? ` It has fallen behind with as few as ${Math.min(...crowds)} players in the last 30 minutes.` : "";
  const head = (r.behind ? `Running at ${Math.round(r.fps)} of ${TARGET} frames a second, so players see lag and rubber-banding.`
    : `Smooth now, but it dipped to ${r.worst} frames a second in the last 5 minutes.`) + crowd;
  return [head, "The game runs almost everything on one processor core, so more cores won't help: lower the player limit, or move this server to a machine with faster cores. If the host's memory is short (below), or a heavy map or mod is in the rotation, that does it too."];
}
