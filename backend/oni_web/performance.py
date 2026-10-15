"""Desktop health.rs frame-rate calculations, translated without changing their sampling rules."""
from __future__ import annotations

import re
from dataclasses import dataclass

from oni_rcon.health import clock_for, parse_report, split_stamp

LAG = re.compile(r"Server falling behind: under \d+ frames a second for (\d+) of the last (\d+) seconds \((\d+) on average, longest frame (\d+) ms\) with (\d+) players?", re.I)
CLOCK = re.compile(r"^(\d\d):(\d\d):(\d\d)\s")


@dataclass
class Minute:
    ts: float
    slow: int
    span: int
    avg: int
    frame: int
    players: int

    @property
    def fps(self):
        return (self.slow * self.avg + (self.span - self.slow) * 60) / self.span


class Performance:
    def __init__(self):
        self.minutes: list[Minute] = []
        self.read_at: float | None = None
        self.first_read: float | None = None
        self.choppy = False

    def ingest(self, text: str, now: float):
        report = parse_report(text)
        clock = clock_for(report.host, now)
        observed = False
        for line in report.lines:
            stamp, body = split_stamp(line)
            match = LAG.search(body)
            if not match:
                continue
            if stamp is None and (m := CLOCK.match(body)):
                stamp = clock(*map(int, m.groups()))
            slow, span, avg, frame, players = map(int, match.groups())
            if stamp is None or not 0 < span <= 3600 or not 0 <= slow <= span:
                continue
            observed = True
            if not any(abs(old.ts - stamp) < 2 and old.span == span and old.avg == avg for old in self.minutes):
                self.minutes.append(Minute(stamp, slow, span, min(60, avg), frame, players))
        if not observed and not any("Network upload over the last" in x for x in report.lines):
            return None
        self.minutes = sorted(self.minutes, key=lambda x: x.ts)[-30:]
        self.read_at = now
        if self.first_read is None:
            self.first_read = now
        r = self.reading(now)
        if r["fps"] < 40 and not self.choppy:
            self.choppy = True
            return f"FALLING BEHIND  {r['fps']:.0f} fps with {r['players'] or 0} players, longest frame {r['frame_ms'] or 0} ms (the target is 60)"
        if self.choppy and r["since"] is not None and now - r["since"] > 180:
            self.choppy = False
            return "KEEPING UP  60 fps again for 3 minutes"
        return None

    def reading(self, now: float):
        if self.read_at is None:
            return None
        recent = [m for m in self.minutes if now - 300 < m.ts <= now + 60]
        last = self.minutes[-1] if self.minutes else None
        live = last if last and 0 <= now - last.ts <= 150 else None
        slow = sum(m.slow for m in recent)
        average = (sum(m.slow * m.avg for m in recent) + max(0, 300 - slow) * 60) / max(300, slow)
        history = []
        for i in reversed(range(30)):
            hi, lo = now - i * 60, now - i * 60 - 60
            minute = next((m for m in reversed(self.minutes) if lo < m.ts <= hi), None)
            known = lo >= self.first_read - 86400 and lo <= self.read_at
            history.append({"fps": minute.fps if minute else 60 if known else None,
                            "longest": minute.frame if minute else None, "players": minute.players if minute else None})
        fps = live.fps if live else 60
        return {"fps": fps, "avg": average, "worst": min((m.avg for m in recent), default=None),
                "frame_ms": max((m.frame for m in recent), default=None), "players": last.players if last else None,
                "slow_minutes": len(recent), "since": last.ts if last else None, "behind": fps < 54, "history": history}
