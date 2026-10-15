// Halo 3 medals, worked out from the kill feed: sprees (kills without dying), multi-kills (no more than 4 s apart)
// and killjoys (ending someone's spree). Only kills seen since the console connected count.
const SPREES: Record<number, string> = { 5: "KILLING SPREE", 10: "KILLING FRENZY", 15: "RUNNING RIOT", 20: "RAMPAGE", 25: "UNTOUCHABLE", 30: "INVINCIBLE" };
const MULTI = ["DOUBLE KILL", "TRIPLE KILL", "OVERKILL", "KILLTACULAR", "KILLTROCITY", "KILLIMANJARO", "KILLTASTROPHE", "KILLPOCALYPSE", "KILLIONAIRE"];
const WINDOW = 4.0;

export class Medals {
  spree = new Map<string, number>();
  chain = new Map<string, [number, number]>();
  earned = new Map<string, Map<string, number>>();

  /** Record a death; returns the medals the killer earned with it. No killer, or the victim, is a suicide. */
  kill(killer: string | null, victim: string, now: number): string[] {
    const ended = this.spree.get(victim) ?? 0;
    this.spree.delete(victim);
    this.chain.delete(victim);
    if (!killer || killer === victim) return [];
    const n = (this.spree.get(killer) ?? 0) + 1;
    this.spree.set(killer, n);
    const [c0, last] = this.chain.get(killer) ?? [0, -Infinity];
    const chained = now - last <= WINDOW ? c0 + 1 : 1;
    this.chain.set(killer, [chained, now]);
    const got: string[] = [];
    if (chained > 1) got.push(MULTI[Math.min(chained, MULTI.length + 1) - 2]);
    if (SPREES[n]) got.push(SPREES[n]);
    if (ended >= 5) got.push("KILLJOY");
    const e = this.earned.get(killer) ?? new Map();
    for (const m of got) e.set(m, (e.get(m) ?? 0) + 1);
    this.earned.set(killer, e);
    return got;
  }

  /** Sprees and chains end with the game; what was earned stays for the session. */
  newGame() {
    this.spree.clear();
    this.chain.clear();
  }
}
