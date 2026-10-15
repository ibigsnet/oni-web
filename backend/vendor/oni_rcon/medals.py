"""Halo 3 medals, worked out from the kill feed: sprees (kills without dying), multi-kills (no more than 4 s
apart) and killjoys (ending someone's spree). Only kills seen since the console connected count."""
from __future__ import annotations

from collections import Counter, defaultdict

SPREES = {5: "KILLING SPREE", 10: "KILLING FRENZY", 15: "RUNNING RIOT", 20: "RAMPAGE", 25: "UNTOUCHABLE",
          30: "INVINCIBLE"}
MULTI = ["DOUBLE KILL", "TRIPLE KILL", "OVERKILL", "KILLTACULAR", "KILLTROCITY", "KILLIMANJARO", "KILLTASTROPHE",
         "KILLPOCALYPSE", "KILLIONAIRE"]
WINDOW = 4.0  # seconds between kills that still chain


class Medals:
    """One station's tally, keyed by player name."""

    def __init__(self):
        self.spree: dict[str, int] = {}
        self.chain: dict[str, tuple[int, float]] = {}  # name -> (kills chained, time of the last)
        self.earned: dict[str, Counter] = defaultdict(Counter)

    def kill(self, killer: str | None, victim: str, now: float) -> list[str]:
        """Record a death; returns the medals the killer earned with it. No killer, or the victim, is a suicide."""
        ended = self.spree.pop(victim, 0)
        self.chain.pop(victim, None)
        if not killer or killer == victim:
            return []
        n = self.spree[killer] = self.spree.get(killer, 0) + 1
        chained, last = self.chain.get(killer, (0, float("-inf")))
        chained = chained + 1 if now - last <= WINDOW else 1
        self.chain[killer] = (chained, now)
        got = [MULTI[min(chained, len(MULTI) + 1) - 2]] if chained > 1 else []
        got += [SPREES[n]] if n in SPREES else []
        got += ["KILLJOY"] if ended >= 5 else []
        self.earned[killer].update(got)
        return got

    def new_game(self) -> None:
        """Sprees and chains end with the game; what was earned stays for the session."""
        self.spree.clear()
        self.chain.clear()
