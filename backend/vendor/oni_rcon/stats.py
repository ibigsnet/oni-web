"""Rounds played, per server, map and gametype, from the status polls the console already makes: one JSON line each in
rounds.jsonl, beside the config.

Groundwork for ReclaimerForge's planned "verified host feedback". Nothing is sent anywhere: each line is shaped to be
posted as it stands once Forge has somewhere to post it, with an id so a retried post can't count a round twice. No
player names, IDs or addresses go in it, only how many played; and the server goes by the public name it reports, never
by how this console reaches it (an address, or an SSH login). It counts what this console saw: a round that ended
while it was closed isn't there, and one it joined partway through says so (seen_whole).
"""
from __future__ import annotations

import contextlib
import json
import time
import uuid
from pathlib import Path
from typing import Callable

from . import __version__
from .forge import num, utc_iso
from .state import norm

SCHEMA = 1
IN_GAME = {"in_game", "ingame", "playing"}  # the demo's word is in_game; a real server's is unconfirmed


class Rounds:
    def __init__(self, path: Path | None, client: str = f"oni-rcon/{__version__}"):
        self.path, self.client = path, client
        self.open: dict[str, dict] = {}  # server -> the round under way
        self.last: dict[str, tuple] = {}  # server -> (phase, map, mode) at its last poll since it connected

    def observe(self, server: str, status: dict,
                credit: Callable[[str, str], list[dict]] = lambda m, g: []) -> dict | None:
        """One status poll from `server` (how this console tells servers apart; never written down). Returns the
        round it closed, if it closed one. `credit` names the Forge listings a map and mode came from."""
        if not isinstance(status, dict):
            return None
        phase = norm(status.get("phase"))
        mp, md, n = str(status.get("map") or ""), str(status.get("mode") or ""), num(status.get("players"))
        playing, before, done = phase in IN_GAME, self.last.get(server), None
        cur = self.open.get(server)
        if cur and (not playing or (mp, md) != (cur["map"], cur["mode"])):
            done = self.close(server, n, credit)
        if playing and server not in self.open:
            # whole when it was seen to start: this server was polled before, between rounds or in another one
            whole = before is not None and (before[0] not in IN_GAME or before[1:] != (mp, md))
            self.open[server] = {"map": mp, "mode": md, "started_at": utc_iso(), "t0": time.monotonic(),
                                 "peak": n or 0, "seen_whole": whole, "name": str(status.get("name") or "")}
        elif playing and n is not None:
            self.open[server]["peak"] = max(self.open[server]["peak"], n)
        self.last[server] = (phase, mp, md)
        return done

    def lost(self, server: str) -> None:
        """The connection dropped: how the round under way ended is unknown, so it isn't counted."""
        self.open.pop(server, None)
        self.last.pop(server, None)

    def close(self, server: str, players_end, credit) -> dict:
        r = self.open.pop(server)
        record = {"schema": SCHEMA, "id": uuid.uuid4().hex, "server": r["name"], "map": r["map"], "mode": r["mode"],
                  "forge": [{"listing_id": e.get("listing_id"), "version_id": e.get("version_id"),
                             "kind": e.get("kind")} for e in credit(r["map"], r["mode"])],
                  "started_at": r["started_at"], "ended_at": utc_iso(),
                  "seconds": round(time.monotonic() - r["t0"]), "players_peak": r["peak"],
                  "players_end": players_end,
                  "seen_whole": r["seen_whole"], "client": self.client}
        if self.path:
            with contextlib.suppress(OSError):  # a read-only profile loses the count, never the console
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, sort_keys=True) + "\n")
        return record
