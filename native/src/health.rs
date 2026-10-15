//! Host and server health: crashes read out of each server's log, and the host's memory.
//!
//! One command per server (the top-level `health_cmd`, run where `ping_log` runs) prints a plain-text report:
//!
//! ```text
//! host now=1760083200 clock=08:15:01 mem_available_mb=812 swap_used_mb=310 swap_total_mb=2048 oom_kill=2
//! container started=2026-10-10T08:12:44Z finished=0001-01-01T00:00:00Z oom_killed=false exit_code=0 restarts=1
//! <every other line is a line of the server's log>
//! ```
//!
//! `{"kind": "host", ...}` and `{"kind": "container", ...}` as JSON lines read the same. Nothing here writes to a
//! server, restarts one or kills one: the commands only read. Player IDs and IPv4 addresses are stripped from every
//! line the moment it's read.
use once_cell::sync::Lazy;
use regex::Regex;
use serde::Serialize;
use std::collections::{HashMap, HashSet, VecDeque};
use std::time::Duration;

static EXCEPTION: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)exception\s+(0x[0-9a-f]{1,16})\s+in\s+([\w.\-]{1,40})(?:\s+at\s+RVA\s+(0x[0-9a-f]{1,16}))?").unwrap()
});
static PROBE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)Engine probe worker failed:\s*exit code:\s*(-?\d+)").unwrap());
static STOPPED: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)(?:^|\s)(\d\d):(\d\d):(\d\d)\s+\[[^\]]*\]\s+stopped \(exit code:\s*(-?\d+)\);\s*restarting in \d+\s*s").unwrap()
});
static MOVED_TAG: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)moved tag").unwrap());
static NOT_PINNED: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)NOT the pinned build").unwrap());
static CLOCK: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\d\d:\d\d:\d\d$").unwrap());
static STAMP: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d(?::?\d\d)?)?)\s+").unwrap()
});
static HEX_ID: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\b[0-9a-f]{32,}\b").unwrap());
static IPV4: Lazy<Regex> = Lazy::new(|| Regex::new(r"\b(?:\d{1,3}\.){3}\d{1,3}\b").unwrap());
pub static UNIT: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[A-Za-z0-9@._:\-]+$").unwrap());

pub const SIGNATURE: &str = "SIGNATURE";
pub const BARE: &str = "BARE";
pub const OOM: &str = "OOM-KILL";
pub const RESTART: &str = "CONTAINER-RESTART";
const DAY: f64 = 86400.0;
const DUP: f64 = 2.0;
const DROP_NEAR: f64 = 90.0;
const SAME_OOM: f64 = 180.0;
const STALE: f64 = 180.0;
pub const LAST: usize = 5;
const MAX_LINES: usize = 50_000;

fn hexed(v: Option<&str>) -> String {
    v.map(|v| format!("0x{}", v[2..].to_uppercase())).unwrap_or_default()
}

/// Text without player IDs or IPv4 addresses.
pub fn scrub(text: &str) -> String {
    IPV4.replace_all(&HEX_ID.replace_all(text, "[id]"), "[ip]").into_owned()
}

/// An RFC 3339 time as epoch seconds; UTC without a zone. None for docker's 'never' (year 1).
pub fn parse_ts(s: &str) -> Option<f64> {
    let d = crate::util::parse_iso(&s.trim().replace(',', "."))?;
    use chrono::Datelike;
    if d.year() > 2000 {
        Some(d.timestamp_millis() as f64 / 1000.0)
    } else {
        None
    }
}

fn split_stamp(line: &str) -> (Option<f64>, &str) {
    match STAMP.captures(line) {
        Some(m) => (parse_ts(&m[1]), &line[m.get(0).unwrap().end()..]),
        None => (None, line),
    }
}

fn num(v: Option<&String>) -> Option<i64> {
    v.and_then(|s| s.trim().parse::<f64>().ok()).filter(|f| f.is_finite()).map(|f| f as i64)
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct BoxState {
    pub started: Option<f64>,
    pub finished: Option<f64>,
    pub oom_killed: bool,
    pub exit_code: Option<i64>,
    pub restarts: Option<i64>,
}

#[derive(Default, Debug)]
pub struct Report {
    pub host: HashMap<String, i64>,
    pub clock: Option<String>,
    pub boxed: Option<BoxState>,
    pub lines: Vec<String>,
}

fn record(line: &str) -> Option<(String, HashMap<String, String>)> {
    if line.starts_with('{') {
        let d: serde_json::Value = serde_json::from_str(line).ok()?;
        let kind = d.get("kind")?.as_str()?.to_string();
        if kind != "host" && kind != "container" {
            return None;
        }
        let f = d.as_object()?
            .iter()
            .filter(|(k, _)| *k != "kind")
            .map(|(k, v)| (k.clone(), v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())))
            .collect();
        return Some((kind, f));
    }
    let (head, rest) = line.split_once(' ')?;
    if (head == "host" || head == "container") && rest.contains('=') {
        let f = rest
            .split_whitespace()
            .filter_map(|w| w.split_once('=').map(|(a, b)| (a.to_string(), b.to_string())))
            .collect();
        return Some((head.into(), f));
    }
    None
}

pub fn parse_report(text: &str) -> Report {
    let mut rep = Report::default();
    for raw in text.lines().take(MAX_LINES) {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        match record(line) {
            None => rep.lines.push(scrub(line)),
            Some((k, f)) if k == "host" => {
                rep.host = ["now", "mem_available_mb", "swap_used_mb", "swap_total_mb", "oom_kill"]
                    .iter()
                    .filter_map(|k| num(f.get(*k)).map(|n| (k.to_string(), n)))
                    .collect();
                let c = f.get("clock").cloned().unwrap_or_default();
                rep.clock = (c.len() == 8 && CLOCK.is_match(&c)).then_some(c);
            }
            Some((_, f)) => {
                let g = |k: &str| f.get(k).cloned().unwrap_or_default();
                rep.boxed = Some(BoxState {
                    started: parse_ts(&g("started")),
                    finished: parse_ts(&g("finished")),
                    oom_killed: matches!(g("oom_killed").trim().to_lowercase().as_str(), "true" | "1" | "yes"),
                    exit_code: num(f.get("exit_code")),
                    restarts: num(f.get("restarts")),
                });
            }
        }
    }
    rep
}

/// Turns a bare HH:MM:SS from a log into epoch seconds: the latest moment that clock face showed, counting back from
/// the host's own now and clock when the report gives them, from `now` here when it doesn't.
fn clock_for(rep: &Report, now: f64) -> impl Fn(i64, i64, i64) -> f64 {
    let reference = rep.host.get("now").map(|n| *n as f64).unwrap_or(now);
    let sod = if let Some(c) = &rep.clock {
        let p: Vec<i64> = c.split(':').map(|x| x.parse().unwrap_or(0)).collect();
        p[0] * 3600 + p[1] * 60 + p[2]
    } else if rep.host.contains_key("now") {
        (reference as i64).rem_euclid(86400)
    } else {
        use chrono::{Local, TimeZone, Timelike};
        let t = Local.timestamp_opt(reference as i64, 0).single().unwrap_or_else(Local::now);
        (t.hour() * 3600 + t.minute() * 60 + t.second()) as i64
    };
    move |h, m, s| reference - ((sod - (h * 3600 + m * 60 + s)).rem_euclid(86400)) as f64
}

#[derive(Clone, Debug, Serialize)]
pub struct Crash {
    pub ts: f64,
    pub cls: &'static str,
    pub key: String,  // the station it happened on; empty for the host as a whole
    pub host: String, // the ssh destination, or "" for this machine
    pub code: String,
    pub module: String,
    pub rva: String,
    pub exit: Option<i64>,
    pub players: Option<i64>,
    pub rcon: bool,
    pub detail: String,
}

impl Crash {
    fn new(ts: f64, cls: &'static str) -> Self {
        Crash { ts, cls, key: String::new(), host: String::new(), code: String::new(), module: String::new(), rva: String::new(), exit: None, players: None, rcon: false, detail: String::new() }
    }
    fn describe(&mut self) {
        self.detail = match self.cls {
            SIGNATURE => format!("{} {}+{}", self.code, self.module, self.rva),
            BARE => match self.exit {
                Some(e) => format!("engine probe failed, exit {e}, no exception"),
                None => "no exception".into(),
            },
            OOM => self.exit.map(|e| format!("exit {e}")).unwrap_or_else(|| "counter rose".into()),
            _ => "container started again".into(),
        };
    }
}

/// The crashes in a server's log. A crash is the engine probe's failure, with the exception before it when there is
/// one (SIGNATURE) and without when there isn't (BARE: another fault), and the 'stopped' line after it.
pub fn crashes_in(lines: &[String], at: &dyn Fn(i64, i64, i64) -> f64) -> Vec<Crash> {
    struct Cur {
        exc: Option<(String, String, String)>,
        probe: Option<i64>,
        ts: Option<f64>,
    }
    let mut out = vec![];
    let mut cur: Option<Cur> = None;
    let flush = |cur: &mut Option<Cur>, out: &mut Vec<Crash>, stop_ts: Option<f64>, code: Option<i64>| {
        if let Some(c) = cur.take() {
            if c.exc.is_some() || c.probe.is_some() {
                if let Some(ts) = stop_ts.or(c.ts) {
                    let exit = code.or(c.probe);
                    let mut cr = match &c.exc {
                        Some((code, module, rva)) if !rva.is_empty() => {
                            let mut x = Crash::new(ts, SIGNATURE);
                            x.code = code.clone();
                            x.module = module.clone();
                            x.rva = rva.clone();
                            x
                        }
                        _ => Crash::new(ts, BARE),
                    };
                    cr.exit = exit;
                    out.push(cr);
                }
            }
        }
    };
    for line in lines {
        let (stamp, body) = split_stamp(line);
        if let Some(m) = EXCEPTION.captures(body) {
            let exc = (hexed(m.get(1).map(|x| x.as_str())), m[2].to_string(), hexed(m.get(3).map(|x| x.as_str())));
            if let Some(c) = &cur {
                if c.probe.is_some() || c.exc.as_ref().is_some_and(|e| *e != exc) {
                    flush(&mut cur, &mut out, None, None);
                }
            }
            let c = cur.get_or_insert(Cur { exc: None, probe: None, ts: None });
            c.exc = Some(exc);
            c.ts = c.ts.or(stamp);
        } else if let Some(m) = PROBE.captures(body) {
            let c = cur.get_or_insert(Cur { exc: None, probe: None, ts: None });
            if c.probe.is_none() {
                c.probe = m[1].parse().ok();
            }
            c.ts = c.ts.or(stamp);
        } else if let Some(m) = STOPPED.captures(body) {
            if cur.is_some() {
                let p = |i: usize| m[i].parse::<i64>().unwrap_or(0);
                let ts = stamp.unwrap_or_else(|| at(p(1), p(2), p(3)));
                flush(&mut cur, &mut out, Some(ts), m[4].parse().ok());
            }
        }
    }
    flush(&mut cur, &mut out, None, None);
    out
}

// --- frame rate (from ibigs/oni-rcon's perf.py, by IBIGS) ---------------------------------------------------
// The server aims at 60 frames a second and logs each minute it spent under 54:
//   04:13:33 Server falling behind: under 54 frames a second for 58 of the last 58 seconds (29 on average, longest
//   frame 125 ms) with 85 players. ...
// A minute without that line held 54 or more every second, so a minute's rate is its slow seconds at their average
// and the rest at 60: a slight overestimate, never an invented slowdown. RCON and status.json carry no frame rate.
static LAG: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)Server falling behind: under \d+ frames a second for (\d+) of the last (\d+) seconds \((\d+) on average, longest frame (\d+) ms\) with (\d+) players?").unwrap()
});
static BODY_CLOCK: Lazy<Regex> = Lazy::new(|| Regex::new(r"^(\d\d):(\d\d):(\d\d)\s").unwrap());
/// Logged every minute by a server whatever its frame rate: a report with it is the raw log, so no lag line means 60.
const EVERY_MINUTE: &str = "Network upload over the last";
pub const TARGET: f64 = 60.0;
pub const SLOW: f64 = 54.0; // what the server calls falling behind
pub const BAD: f64 = 40.0; // under this play is visibly choppy, and the feed says so
const WINDOW: f64 = 300.0; // what the averages cover
const FRESH: f64 = 150.0; // a minute's report is "now" for this long
const KEEP: usize = 30;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Minute {
    pub ts: f64,
    pub slow: i64, // seconds under SLOW
    pub span: i64,
    pub avg: i64, // average fps over the slow seconds
    pub frame_ms: i64,
    pub players: i64,
}

impl Minute {
    pub fn fps(&self) -> f64 {
        ((self.slow * self.avg) as f64 + (self.span - self.slow) as f64 * TARGET) / self.span as f64
    }
}

pub fn minutes_in(lines: &[String], at: &dyn Fn(i64, i64, i64) -> f64) -> Vec<Minute> {
    let mut out = vec![];
    for line in lines.iter().filter(|l| l.contains("falling behind")) {
        let (stamp, body) = split_stamp(line);
        let Some(m) = LAG.captures(body) else { continue };
        let ts = stamp.or_else(|| BODY_CLOCK.captures(body).map(|c| at(c[1].parse().unwrap_or(0), c[2].parse().unwrap_or(0), c[3].parse().unwrap_or(0))));
        let n = |i: usize| m[i].parse::<i64>().unwrap_or(-1);
        let (slow, span) = (n(1), n(2));
        if let Some(ts) = ts.filter(|_| 0 < span && span <= 3600 && (0..=span).contains(&slow)) {
            out.push(Minute { ts, slow, span, avg: n(3).clamp(0, TARGET as i64), frame_ms: n(4).max(0), players: n(5).max(0) });
        }
    }
    out
}

/// What one server's frame rate looks like now.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Reading {
    pub fps: f64,                // the latest minute, TARGET without a recent report
    pub avg: f64,                // over WINDOW
    pub worst: Option<i64>,      // the slowest minute's slow-second average in WINDOW
    pub frame_ms: Option<i64>,   // the longest frame in WINDOW
    pub players: Option<i64>,    // as of the latest report
    pub slow_minutes: usize,     // reports in WINDOW
    pub since: Option<f64>,      // when the latest report came
    pub behind: bool,
    pub history: Vec<Bucket>,    // the last KEEP minutes, oldest first
}

/// One minute of the chart: None where it is not known.
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize)]
pub struct Bucket {
    pub fps: Option<f64>,
    pub longest: Option<i64>,
    pub players: Option<i64>,
}

#[derive(Default)]
pub struct Perf {
    minutes: VecDeque<Minute>,
    read_at: Option<f64>, // the last report, with lag lines or without
    first_read: Option<f64>, // the first: its log goes back DAY, so every minute since is known
    choppy: bool, // the feed has said it's under BAD and hasn't said it recovered
}

impl Perf {
    fn add(&mut self, m: Minute) {
        if self.minutes.iter().any(|o| (o.ts - m.ts).abs() < 2.0 && o.span == m.span && o.avg == m.avg) {
            return; // the same line read again
        }
        self.minutes.push_back(m);
        self.minutes.make_contiguous().sort_by(|a, b| a.ts.total_cmp(&b.ts));
        while self.minutes.len() > KEEP {
            self.minutes.pop_front();
        }
    }

    pub fn reading(&self, now: f64) -> Option<Reading> {
        self.read_at?;
        let recent: Vec<&Minute> = self.minutes.iter().filter(|m| now - WINDOW < m.ts && m.ts <= now + 60.0).collect();
        let last = self.minutes.back();
        let live = last.filter(|m| (0.0..=FRESH).contains(&(now - m.ts)));
        let slow_s: i64 = recent.iter().map(|m| m.slow).sum();
        let avg = (recent.iter().map(|m| (m.slow * m.avg) as f64).sum::<f64>() + (WINDOW - slow_s as f64).max(0.0) * TARGET) / WINDOW.max(slow_s as f64);
        let fps = live.map_or(TARGET, Minute::fps);
        Some(Reading {
            fps,
            avg,
            worst: recent.iter().map(|m| m.avg).min(),
            frame_ms: recent.iter().map(|m| m.frame_ms).max(),
            players: last.map(|m| m.players),
            slow_minutes: recent.len(),
            since: last.map(|m| m.ts),
            behind: fps < SLOW,
            history: self.per_minute(now),
        })
    }

    /// The last KEEP minutes, oldest first: a minute with a lag report is what it says, one without it (read after the
    /// first report, whose log goes back a day) kept up at 60, and one not read yet is unknown.
    fn per_minute(&self, now: f64) -> Vec<Bucket> {
        let (first, read) = (self.first_read.unwrap_or(now) - DAY, self.read_at.unwrap_or(now));
        (0..KEEP).rev().map(|i| {
            let (hi, lo) = (now - i as f64 * 60.0, now - i as f64 * 60.0 - 60.0);
            match self.minutes.iter().rev().find(|m| lo < m.ts && m.ts <= hi) {
                Some(m) => Bucket { fps: Some(m.fps()), longest: Some(m.frame_ms), players: Some(m.players) },
                None if lo >= first && lo <= read => Bucket { fps: Some(TARGET), ..Bucket::default() },
                None => Bucket::default(),
            }
        }).collect()
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Notice {
    pub level: &'static str,
    pub key: String,
    pub text: String,
    pub quiet: bool, // the feed only: no banner, no chime
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Host {
    pub avail: Option<i64>,
    pub swap_used: Option<i64>,
    pub swap_total: Option<i64>,
    pub oom: Option<i64>,
    pub oom_first: Option<i64>,
    pub at: f64,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Service {
    pub active: String,
    pub moved: Option<f64>,
    pub warn: String,
    pub warn_at: Option<f64>,
    pub at: f64,
}

/// What the health command's reports add up to. Crashes are kept for a day. A crash that turns up in a report is
/// news unless this console had no earlier look at that server and the crash came before it started.
pub struct Monitor {
    pub min_free: i64,
    pub started: f64,
    pub events: Vec<Crash>,
    pub hosts: HashMap<String, Host>,
    pub boxes: HashMap<String, BoxState>,
    pub services: HashMap<String, Service>,
    pub seen: HashSet<String>,
    pub perf: HashMap<String, Perf>,
    low: HashMap<String, bool>,
    counts: HashMap<String, VecDeque<(f64, i64)>>,
    drops: HashMap<String, VecDeque<f64>>,
}

impl Monitor {
    pub fn new(min_free: i64, started: f64) -> Self {
        Monitor { min_free, started, events: vec![], hosts: HashMap::new(), boxes: HashMap::new(), services: HashMap::new(), seen: HashSet::new(), perf: HashMap::new(), low: HashMap::new(), counts: HashMap::new(), drops: HashMap::new() }
    }

    pub fn sample(&mut self, key: &str, players: Option<i64>, now: f64) {
        if let Some(n) = players {
            let q = self.counts.entry(key.into()).or_default();
            q.push_back((now, n));
            while q.len() > 400 {
                q.pop_front();
            }
        }
    }

    /// The RCON connection to a server went away: the server restarted, whatever the log says.
    pub fn dropped(&mut self, key: &str, now: f64) {
        let q = self.drops.entry(key.into()).or_default();
        q.push_back(now);
        while q.len() > 50 {
            q.pop_front();
        }
        let count = self.count_at(key, now);
        for e in self.events.iter_mut() {
            if e.key == key && !e.rcon && (e.ts - now).abs() <= DROP_NEAR {
                e.rcon = true;
                e.players = count;
            }
        }
    }

    fn count_at(&self, key: &str, t: f64) -> Option<i64> {
        for (when, n) in self.counts.get(key)?.iter().rev() {
            if *when <= t + 1.0 {
                return (t - when <= STALE).then_some(*n);
            }
        }
        None
    }

    fn drop_near(&self, key: &str, t: f64) -> Option<f64> {
        self.drops
            .get(key)?
            .iter()
            .filter(|d| (*d - t).abs() <= DROP_NEAR)
            .min_by(|a, b| (*a - t).abs().total_cmp(&(*b - t).abs()))
            .copied()
    }

    /// Fold one server's report in. Returns what's news.
    pub fn ingest(&mut self, key: &str, host: &str, text: &str, now: f64) -> Vec<Notice> {
        let rep = parse_report(text);
        let mut notes = if rep.host.is_empty() { vec![] } else { self.host_in(host, &rep, now) };
        let at = clock_for(&rep, now);
        let mut found = crashes_in(&rep.lines, &at);
        let lags = minutes_in(&rep.lines, &at);
        if !lags.is_empty() || rep.lines.iter().any(|l| l.contains(EVERY_MINUTE)) {
            notes.extend(self.frames(key, &lags, now));
        }
        if let Some(b) = &rep.boxed {
            found.extend(self.box_in(key, b, now));
            self.boxes.insert(key.into(), b.clone());
        }
        for mut ev in found {
            if now - ev.ts >= DAY {
                continue;
            }
            ev.key = key.into();
            ev.host = host.into();
            let ts = ev.ts;
            if let Some(n) = self.add(ev) {
                if self.seen.contains(key) || ts >= self.started {
                    notes.push(n);
                }
            }
        }
        self.seen.insert(key.into());
        self.events.retain(|e| now - e.ts < DAY);
        self.events.sort_by(|a, b| a.ts.total_cmp(&b.ts));
        if self.events.len() > 500 {
            self.events.drain(..self.events.len() - 500);
        }
        notes
    }

    /// Fold in a server's lag reports. The feed hears once when it gets choppy (a minute under BAD) and once when it's
    /// kept up again for 3 minutes.
    fn frames(&mut self, key: &str, lags: &[Minute], now: f64) -> Option<Notice> {
        let p = self.perf.entry(key.into()).or_default();
        lags.iter().for_each(|m| p.add(*m));
        p.read_at = Some(now);
        p.first_read.get_or_insert(now);
        let r = p.reading(now)?;
        let text = if r.fps < BAD && !p.choppy {
            p.choppy = true;
            format!("FALLING BEHIND  {:.0} fps with {} players, longest frame {} ms (the target is {TARGET})", r.fps, r.players.unwrap_or(0), r.frame_ms.unwrap_or(0))
        } else if p.choppy && r.since.is_some_and(|t| now - t > 180.0) {
            p.choppy = false;
            format!("KEEPING UP  {TARGET} fps again for 3 minutes")
        } else {
            return None;
        };
        Some(Notice { level: "warn", key: key.into(), text, quiet: true })
    }

    pub fn frame_rates(&self, now: f64) -> HashMap<String, Reading> {
        self.perf.iter().filter_map(|(k, p)| Some((k.clone(), p.reading(now)?))).collect()
    }

    fn box_in(&self, key: &str, b: &BoxState, now: f64) -> Vec<Crash> {
        let mut out = vec![];
        if b.oom_killed || b.exit_code == Some(137) {
            let mut c = Crash::new(b.finished.unwrap_or(now), OOM);
            c.exit = b.exit_code;
            out.push(c);
        }
        if let (Some(prev), Some(started)) = (self.boxes.get(key).and_then(|p| p.started), b.started) {
            if started != prev
                && out.is_empty()
                && !self.events.iter().any(|e| e.cls == OOM && e.key == key && started - 300.0 <= e.ts && e.ts <= started + 5.0)
            {
                out.push(Crash::new(started, RESTART));
            }
        }
        out
    }

    fn host_in(&mut self, host: &str, rep: &Report, now: f64) -> Vec<Notice> {
        let old = self.hosts.get(host).cloned();
        let mut notes = vec![];
        let h = |k: &str| rep.host.get(k).copied();
        let snap = Host {
            avail: h("mem_available_mb"),
            swap_used: h("swap_used_mb"),
            swap_total: h("swap_total_mb"),
            oom: h("oom_kill"),
            oom_first: old.as_ref().and_then(|o| o.oom_first).or(h("oom_kill")),
            at: now,
        };
        self.hosts.insert(host.into(), snap.clone());
        if let Some(avail) = snap.avail {
            let low = avail < self.min_free;
            let was = self.low.get(host).copied().unwrap_or(false);
            if low && !was {
                let at = if host.is_empty() { String::new() } else { format!(" ({})", scrub(host)) };
                notes.push(Notice {
                    level: "crit",
                    key: String::new(),
                    text: format!("LOW MEMORY  {avail} MB available, under the {} MB limit{at}", self.min_free),
                    quiet: false,
                });
            }
            self.low.insert(host.into(), low || (was && (avail as f64) < self.min_free as f64 * 1.1));
        }
        if let (Some(o), Some(n)) = (old.and_then(|o| o.oom), snap.oom) {
            if n > o {
                let mut ev = Crash::new(now, OOM);
                ev.host = host.into();
                let k = n - o;
                if let Some(mut note) = self.add(ev) {
                    note.text = format!(
                        "OOM-KILL  the kernel killed {k} process{}, host oom_kill is {n}",
                        if k > 1 { "es" } else { "" }
                    );
                    notes.push(note);
                }
            }
        }
        notes
    }

    /// Keep ev unless it repeats one already held: the notice when it's news.
    fn add(&mut self, mut ev: Crash) -> Option<Notice> {
        if self.events.iter().any(|e| {
            (e.cls, &e.key, &e.host, &e.code, &e.rva) == (ev.cls, &ev.key, &ev.host, &ev.code, &ev.rva) && (e.ts - ev.ts).abs() <= DUP
        }) {
            return None;
        }
        let mut news = true;
        if ev.cls == OOM {
            let near: Vec<usize> = (0..self.events.len())
                .filter(|&i| {
                    let e = &self.events[i];
                    e.cls == OOM && e.host == ev.host && e.key != ev.key && (e.ts - ev.ts).abs() <= SAME_OOM
                })
                .collect();
            if !near.is_empty() && ev.key.is_empty() {
                return None; // a container's own row already says it
            }
            for i in near.into_iter().rev() {
                if self.events[i].key.is_empty() {
                    self.events.remove(i); // the counter got there first: this row, which names the server, replaces it
                    news = false;
                }
            }
        }
        if !ev.key.is_empty() {
            let drop = self.drop_near(&ev.key, ev.ts);
            ev.rcon = drop.is_some();
            ev.players = self.count_at(&ev.key, drop.unwrap_or(ev.ts));
        }
        ev.describe();
        let n = Self::notice(&ev);
        self.events.push(ev);
        news.then_some(n)
    }

    fn notice(ev: &Crash) -> Notice {
        let what = match ev.cls {
            SIGNATURE => format!("SIGNATURE CRASH  {} in {} at RVA {}", ev.code, ev.module, ev.rva),
            BARE => "BARE CRASH  engine probe failed, no exception line".into(),
            OOM => "OOM-KILL  the container was killed for memory".into(),
            _ => "CONTAINER RESTART  the container started again".into(),
        };
        let drop = match ev.players {
            Some(p) if p > 0 => format!("  ·  {p} player{} dropped", if p != 1 { "s" } else { "" }),
            _ => String::new(),
        };
        Notice { level: if ev.cls == RESTART { "warn" } else { "crit" }, key: ev.key.clone(), text: what + &drop, quiet: false }
    }

    /// The latest crashes, newest first, at most per_server for each server.
    pub fn recent(&self, per_server: usize) -> Vec<Crash> {
        let mut sorted: Vec<&Crash> = self.events.iter().collect();
        sorted.sort_by(|a, b| b.ts.total_cmp(&a.ts));
        let mut n: HashMap<&str, usize> = HashMap::new();
        let mut out = vec![];
        for e in sorted {
            let c = n.entry(e.key.as_str()).or_default();
            if *c < per_server {
                *c += 1;
                out.push(e.clone());
            }
        }
        out
    }

    pub fn short(&self) -> bool {
        self.low.values().any(|v| *v)
    }
}

/// Read-only: whether the unit is active, and what its journal said about moving a tag or the wrong build.
pub fn service_command(unit: &str) -> Result<String, String> {
    if !UNIT.is_match(unit) {
        return Err(format!("{unit:?} isn't a systemd unit name"));
    }
    let u = format!("'{unit}'");
    Ok(format!(
        "systemctl is-active {u}; journalctl -u {u} --no-pager -o short-iso --since '24 hours ago' 2>&1 | grep -iE 'moved tag|not the pinned build' | tail -n 40"
    ))
}

pub fn parse_service(text: &str, now: f64) -> Service {
    let lines: Vec<String> = text.lines().map(str::trim).filter(|x| !x.is_empty()).map(scrub).collect();
    let mut svc = Service {
        active: lines.first().and_then(|l| l.split_whitespace().next()).unwrap_or("unknown").to_lowercase(),
        at: now,
        ..Default::default()
    };
    for line in lines.iter().skip(1) {
        let (ts, body) = split_stamp(line);
        if MOVED_TAG.is_match(body) {
            svc.moved = ts.or(svc.moved);
        }
        if NOT_PINNED.is_match(body) {
            svc.warn = body.chars().take(140).collect();
            svc.warn_at = ts;
        }
    }
    svc
}

/// What a command prints: on the ssh destination as ping_log runs, or on this machine without one. Whatever it prints
/// is the report even if it exited badly; nothing printed and a bad exit is an error, with no address in it.
pub async fn run(cmd: &str, ssh: &str, timeout: f64) -> Result<String, String> {
    let mut c = if ssh.is_empty() {
        crate::util::shell(cmd)
    } else {
        let exe = crate::util::which("ssh").ok_or("no ssh client on PATH")?;
        let mut c = crate::util::command(&exe.to_string_lossy());
        c.args(crate::rcon::SSH_OPTS).arg(ssh).arg(cmd);
        c
    };
    c.stdin(std::process::Stdio::null());
    let out = tokio::time::timeout(Duration::from_secs_f64(timeout), c.output())
        .await
        .map_err(|_| format!("no answer in {timeout}s"))?
        .map_err(|e| scrub(&e.to_string()))?;
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    if !out.status.success() && text.trim().is_empty() {
        let err = String::from_utf8_lossy(&out.stderr);
        let last = err.trim().lines().last().map(str::to_string).unwrap_or_else(|| format!("exited {}", out.status.code().unwrap_or(-1)));
        return Err(scrub(&last));
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SIG: &str = "2026-10-10T08:15:01.5Z Experimental startup exception 0xc0000005 in halo3.dll at RVA 0x14c73e\n\
        2026-10-10T08:15:01.6Z Error: Engine probe worker failed: exit code: 1\n\
        2026-10-10T08:15:02Z 08:15:02 [Slayer] stopped (exit code: 1); restarting in 5 s.";

    fn t(s: &str) -> f64 {
        parse_ts(s).unwrap()
    }

    #[test]
    fn a_signature_crash_reads_as_one() {
        let at = |_, _, _| 0.0;
        let c = crashes_in(&SIG.lines().map(String::from).collect::<Vec<_>>(), &at);
        assert_eq!(c.len(), 1);
        assert_eq!((c[0].cls, c[0].code.as_str(), c[0].rva.as_str()), (SIGNATURE, "0xC0000005", "0x14C73E"));
        assert_eq!(c[0].ts, t("2026-10-10T08:15:02Z"));
    }

    #[test]
    fn a_bare_crash_takes_its_time_from_the_clock() {
        let rep = parse_report("host now=1000000 clock=10:00:00 mem_available_mb=900\nError: Engine probe worker failed: exit code: 1\n09:59:50 [x] stopped (exit code: 1); restarting in 5 s.");
        let c = crashes_in(&rep.lines, &clock_for(&rep, 0.0));
        assert_eq!(c.len(), 1);
        assert_eq!((c[0].cls, c[0].ts), (BARE, 999990.0));
    }

    #[test]
    fn ids_and_addresses_are_stripped() {
        let r = parse_report(&format!("player {} from 203.0.113.9", "ab".repeat(16)));
        assert_eq!(r.lines[0], "player [id] from [ip]");
    }

    #[test]
    fn the_monitor_says_news_once() {
        let now = t("2026-10-10T08:16:00Z");
        let mut m = Monitor::new(1024, now - 3600.0);
        m.sample("0", Some(9), now - 70.0);
        let n = m.ingest("0", "", SIG, now);
        assert_eq!(n.len(), 1);
        assert!(n[0].text.contains("SIGNATURE"));
        assert!(m.ingest("0", "", SIG, now + 60.0).is_empty());
        m.dropped("0", t("2026-10-10T08:15:03Z"));
        assert!(m.events[0].rcon);
        assert_eq!(m.events[0].players, Some(9));
    }

    #[test]
    fn old_crashes_on_a_first_look_are_not_news() {
        let now = t("2026-10-10T09:00:00Z");
        let mut m = Monitor::new(1024, now);
        assert!(m.ingest("0", "", SIG, now).is_empty());
        assert_eq!(m.recent(LAST).len(), 1);
    }

    #[test]
    fn memory_and_oom() {
        let mut m = Monitor::new(1024, 0.0);
        assert!(m.ingest("0", "", "host mem_available_mb=3000 oom_kill=1", 10.0).is_empty());
        let n = m.ingest("0", "", "host mem_available_mb=500 oom_kill=2", 20.0);
        assert_eq!(n.len(), 2);
        assert!(m.short());
        // a container's own OOM row replaces the counter's, and isn't news again
        let n = m.ingest("0", "", "container started=2026-01-01T00:00:00Z oom_killed=true exit_code=137", 25.0);
        assert!(n.is_empty());
        assert!(m.events.iter().all(|e| !e.key.is_empty()));
    }

    #[test]
    fn frame_rate_from_the_log() {
        let lag = |clock: &str, avg: i64| format!("{clock} Server falling behind: under 54 frames a second for 58 of the last 58 seconds ({avg} on average, longest frame 125 ms) with 85 players. Each player gets one update a frame");
        let rep = parse_report(&format!("host now=1000000 clock=10:00:00
{}
{}
09:59:30 not a lag line", lag("09:58:00", 35), lag("09:59:00", 29)));
        let mins = minutes_in(&rep.lines, &clock_for(&rep, 0.0));
        assert_eq!(mins.len(), 2);
        assert_eq!((mins[1].ts, mins[1].players, mins[1].frame_ms), (999940.0, 85, 125));
        assert_eq!(mins[1].fps(), 29.0);

        let mut m = Monitor::new(1024, 0.0);
        // a filtered report with no lag line and no every-minute line says nothing about frame rate
        m.ingest("0", "", "host now=999000", 999000.0);
        assert!(m.frame_rates(999000.0).is_empty());
        // the raw log with no lag line: keeping up
        m.ingest("0", "", "09:00:00 Network upload over the last minute: 1 MB", 999000.0);
        assert_eq!(m.frame_rates(999000.0)["0"].fps, TARGET);
        // choppy: said once
        let text = format!("host now=1000000 clock=10:00:00
{}
{}", lag("09:58:00", 35), lag("09:59:00", 29));
        let n = m.ingest("0", "", &text, 1000000.0);
        assert_eq!(n.len(), 1);
        assert!(n[0].quiet && n[0].text.starts_with("FALLING BEHIND  29 fps"));
        let r = &m.frame_rates(1000000.0)["0"];
        assert!(r.behind && r.worst == Some(29) && r.slow_minutes == 2);
        assert!(m.ingest("0", "", &text, 1000010.0).is_empty());
        let h = &r.history;
        assert_eq!((h.len(), h[27].fps, h[28].fps, h[28].longest, h[29].fps), (30, Some(35.0), Some(29.0), Some(125), Some(TARGET)));
        assert_eq!(m.frame_rates(1000120.0)["0"].history[29].fps, None); // a minute not read yet
        // recovered after 3 minutes without a lag line
        let n = m.ingest("0", "", "host now=1000200
10:03:00 Network upload over the last minute: 1 MB", 1000200.0);
        assert!(n[0].text.starts_with("KEEPING UP"));
        assert!(!m.frame_rates(1000200.0)["0"].behind);
    }

    #[test]
    fn service_lines() {
        let s = parse_service("active\n2026-10-10T08:00:00+0000 box u[1]: moved tag x\n2026-10-10T09:00:00+0000 box u[1]: running 1, NOT the pinned build 2", 0.0);
        assert_eq!(s.active, "active");
        assert!(s.moved.is_some());
        assert!(s.warn.contains("NOT the pinned"));
        assert!(service_command("bad; rm").is_err());
    }
}
