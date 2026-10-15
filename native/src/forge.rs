//! ReclaimerForge (www.reclaimerforge.net): the community catalog of forged maps, gametypes and playlists, read with
//! the operator's own API key, and published to with it when the key has the write scopes (listings:write,
//! files:upload, listings:publish, listings:delete). No key ships with oni-rcon. The key lives here, in the backend:
//! the window never sees it, and only files the operator picked in this backend's own file dialog are uploaded.
//!
//! A Bearer key over HTTPS; 120 requests per 60 s per key, reported in X-RateLimit-Limit, -Remaining and -Reset (Unix
//! seconds), and Retry-After on a 429. Field names are read tolerantly, so another spelling of the same thing works.
use crate::util::{num, parse_iso, pick, pick_str, scrub, utc_iso_at, write_atomic, Secret};
use once_cell::sync::Lazy;
use parking_lot::Mutex;
use regex::Regex;
use reqwest::Method;
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const FORGE_URL: &str = "https://www.reclaimerforge.net";
pub const SORTS: &[&str] = &["trending", "rising", "latest", "updated", "downloads", "rated", "unrated", "overlooked"];
pub const WINDOWED: &[&str] = &["trending", "rising"];
const RESERVE: i64 = 20; // of a key's requests each minute, kept back for the operator
const MAX_WAIT: f64 = 60.0;
const MAX_JSON: usize = 8 << 20;
pub const MAX_ASSET: i64 = 1 << 30;
pub const MAX_UPLOAD: u64 = 100 << 20;
pub const WITHDRAWN: &[&str] = &["withdrawn", "removed", "unlisted", "taken_down", "takedown", "deleted"];
const UNPUBLISHED: &[&str] = &["draft", "pending", "unpublished"];
type Body = Option<(&'static str, Vec<u8>)>;
static UNSAFE: Lazy<Regex> = Lazy::new(|| Regex::new("[\x00-\x08\x0b-\x1f\x7f-\u{9f}\u{202a}-\u{202e}\u{2066}-\u{2069}]").unwrap());

// --- errors, in words ------------------------------------------------------------------------------------------
#[derive(Debug, Clone, Serialize)]
pub struct ForgeError {
    pub status: u16,
    pub detail: String,
    pub retry_after: Option<f64>,
    pub short: String,
    pub text: String,
}

impl ForgeError {
    pub fn new(status: u16, detail: &str, retry_after: Option<f64>) -> Self {
        let detail: String = scrub(detail).chars().take(300).collect();
        let (short, words) = match status {
            401 => ("KEY REFUSED", "ReclaimerForge refused your API key: it's mistyped, revoked or expired. Make a new one on reclaimerforge.net and load it again (k on F6)."),
            403 => ("MISSING SCOPE", "Your key isn't allowed to do that. Browsing needs catalog:read and installing assets:download; publishing needs listings:write, files:upload and listings:publish; withdrawing and deleting need listings:delete. The account behind it also needs the Developer role and a verified email, or its keys stop working."),
            404 => ("NOT AVAILABLE", "That listing or version isn't available. It may have been withdrawn or made private."),
            409 => ("CHANGED MEANWHILE", "That listing changed state while you were looking at it. Refresh (Ctrl+R) and try again."),
            410 => ("UPLOAD EXPIRED", "That upload took too long and ReclaimerForge let it go. Publish again to start over."),
            413 => ("TOO LARGE", "ReclaimerForge won't take or serve something that large: a file can be 100 MiB at most."),
            415 => ("WRONG KIND OF FILE", "ReclaimerForge takes Reclaimer map, gametype and playlist files (.json, .mvar, .bin), and that isn't one."),
            422 => ("NOT ACCEPTED", "ReclaimerForge couldn't accept that request."),
            429 => ("RATE LIMITED", "Your key has used its requests for this minute. Wait a moment and try again."),
            503 => ("FORGE BUSY", "ReclaimerForge is down for a moment. Try again shortly."),
            502 => ("BAD REPLY", "ReclaimerForge sent back something oni-rcon can't read."),
            0 => ("NO UPLINK", "Can't reach reclaimerforge.net. Check your connection."),
            _ => ("", ""),
        };
        let short = if short.is_empty() { format!("FORGE ERROR {status}") } else { short.into() };
        let mut text = if words.is_empty() { format!("ReclaimerForge answered {status}.") } else { words.to_string() };
        if status == 429 {
            if let Some(r) = retry_after.filter(|r| *r > 0.0) {
                text = format!("Your key has used its requests for this minute. Try again in {}s.", r as i64 + 1);
            }
        } else if matches!(status, 0 | 403 | 409 | 413 | 415 | 422 | 502) && !detail.is_empty() {
            text += &format!(" ({detail})");
        }
        ForgeError { status, detail, retry_after, short, text }
    }
}

/// A download that doesn't match its manifest, in plain words.
#[derive(Debug, Clone)]
pub struct Unverified(pub String);

// --- reading what comes back -----------------------------------------------------------------------------------
pub fn tidy(v: &str, lines: bool) -> String {
    let s = UNSAFE.replace_all(v, "");
    if lines {
        s.trim().to_string()
    } else {
        s.split_whitespace().collect::<Vec<_>>().join(" ")
    }
}

pub fn items_of(reply: &Value) -> Vec<Value> {
    match pick(reply, &["items", "results", "listings", "changes", "data"]) {
        Some(Value::Array(a)) => a.iter().filter(|x| x.is_object()).cloned().collect(),
        _ => vec![],
    }
}

/// The query for the page after this one, or None at the end.
pub fn next_page(reply: &Value) -> Option<Value> {
    match pick(reply, &["next_cursor", "cursor_next", "next"]) {
        Some(Value::String(s)) if !(s.starts_with("http:") || s.starts_with("https:") || s.starts_with('/')) => {
            return Some(json!({"cursor": s}))
        }
        Some(Value::Number(n)) => return Some(json!({"cursor": n.to_string()})),
        _ => {}
    }
    let page = num(reply.get("page"))?;
    let pages = num(pick(reply, &["pages", "total_pages", "page_count"]));
    let more = pick(reply, &["has_more", "has_next"]).and_then(Value::as_bool) == Some(true);
    (more || pages.is_some_and(|p| page < p)).then(|| json!({"page": (page as i64 + 1).to_string()}))
}

pub fn id_list(v: Option<&Value>) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    if let Some(Value::Array(a)) = v {
        for i in a {
            let s = match i {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                _ => continue,
            };
            if !s.is_empty() && !out.contains(&s) {
                out.push(s);
            }
        }
    }
    out
}

fn enc(s: &str) -> String {
    url::form_urlencoded::byte_serialize(s.as_bytes()).collect()
}

/// The listing in a reply, whether or not it comes wrapped.
fn inner(reply: Value) -> Value {
    match pick(&reply, &["listing", "item", "data"]) {
        Some(inner @ Value::Object(_)) => inner.clone(),
        _ => reply,
    }
}

pub fn listing_id(x: &Value) -> String {
    pick_str(x, &["id", "listing_id"])
}
pub fn version_id(x: &Value) -> String {
    match x {
        Value::Object(_) => pick_str(x, &["id", "version_id"]),
        Value::String(s) => s.clone(),
        _ => String::new(),
    }
}
pub fn version_label(x: &Value) -> String {
    if !x.is_object() {
        return String::new();
    }
    let l = pick_str(x, &["version_label", "version", "label", "name", "number"]);
    let l = if l.is_empty() { version_id(x) } else { l };
    tidy(&l, false)
}
pub fn title_of(x: &Value) -> String {
    let t = tidy(&pick_str(x, &["title", "name"]), false);
    if t.is_empty() {
        "untitled".into()
    } else {
        t
    }
}
pub fn kind_of(x: &Value) -> String {
    let k = pick_str(x, &["kind", "type", "category", "content_type"]).to_lowercase();
    match k.as_str() {
        "game_type" | "game type" | "variant" => "gametype".into(),
        "map_variant" => "map".into(),
        _ => k,
    }
}

fn authors_of(x: &Value) -> Vec<(String, String, bool)> {
    let mut out: Vec<(String, String, bool)> = match x.get("authors") {
        Some(Value::Array(a)) => a
            .iter()
            .filter(|e| e.is_object())
            .map(|e| {
                (
                    pick_str(e, &["user_id", "id"]),
                    tidy(&pick_str(e, &["username", "display_name", "name"]), false),
                    e.get("is_owner").and_then(Value::as_bool) == Some(true),
                )
            })
            .filter(|(i, n, _)| !i.is_empty() || !n.is_empty())
            .collect(),
        _ => vec![],
    };
    out.sort_by_key(|e| !e.2);
    out
}

pub fn owner_of(x: &Value) -> String {
    let o = pick_str(x, &["owner_id", "author_id", "creator_id"]);
    if !o.is_empty() {
        return o;
    }
    if let Some(owner) = x.get("owner").filter(|o| o.is_object()) {
        let o = pick_str(owner, &["id", "owner_id"]);
        if !o.is_empty() {
            return o;
        }
    }
    authors_of(x).into_iter().find(|e| e.2 && !e.0.is_empty()).map(|e| e.0).unwrap_or_default()
}

pub fn author_of(x: &Value) -> String {
    let names: Vec<String> = authors_of(x).into_iter().map(|e| e.1).filter(|n| !n.is_empty()).collect();
    if !names.is_empty() {
        return names.join(", ");
    }
    let mut a = pick(x, &["author", "owner_name", "owner_display_name", "author_name", "creator"]).cloned();
    if let Some(Value::Object(_)) = &a {
        a = pick(a.as_ref().unwrap(), &["display_name", "name", "username", "handle"]).cloned();
    }
    if a.is_none() {
        if let Some(owner) = x.get("owner").filter(|o| o.is_object()) {
            a = pick(owner, &["display_name", "name", "username", "handle"]).cloned();
        }
    }
    tidy(a.as_ref().and_then(Value::as_str).unwrap_or_default(), false)
}

pub fn is_withdrawn(x: &Value) -> bool {
    WITHDRAWN.contains(&pick_str(x, &["status", "state"]).to_lowercase().as_str())
        || pick(x, &["withdrawn"]).and_then(Value::as_bool) == Some(true)
        || pick(x, &["withdrawn_at"]).is_some_and(|v| v.as_bool() != Some(false))
}

pub fn usable(v: &Value) -> bool {
    v.is_object() && !is_withdrawn(v) && !UNPUBLISHED.contains(&pick_str(v, &["status", "state"]).to_lowercase().as_str())
}

pub fn versions_of(x: &Value) -> Vec<Value> {
    match pick(x, &["versions", "releases"]) {
        Some(Value::Array(a)) => a.iter().filter(|e| e.is_object()).cloned().collect(),
        _ => vec![],
    }
}

pub fn when_of(v: &Value) -> Option<chrono::DateTime<chrono::Utc>> {
    parse_iso(&pick_str(v, &["published_at", "created_at", "released_at"]))
}

fn newest(vs: Vec<Value>) -> Option<Value> {
    let dated: Vec<_> = vs.iter().map(|v| (when_of(v), v)).collect();
    if !dated.is_empty() && dated.iter().all(|(d, _)| d.is_some()) {
        return dated.into_iter().max_by_key(|(d, _)| *d).map(|(_, v)| v.clone());
    }
    vs.into_iter().next()
}

/// The newest version that can be installed. A withdrawn version is never it.
pub fn latest_of(x: &Value) -> Option<Value> {
    let gone: Vec<String> = versions_of(x).iter().filter(|v| !usable(v)).map(version_id).collect();
    match pick(x, &["latest_version", "current_version", "latest_release", "latest"]) {
        Some(lv @ Value::Object(_)) if usable(lv) && !gone.contains(&version_id(lv)) => return Some(lv.clone()),
        Some(Value::String(s)) if !gone.contains(s) => return Some(json!({"id": s})),
        _ => {}
    }
    newest(versions_of(x).into_iter().filter(usable).collect())
}

pub fn change_listing(c: &Value) -> String {
    pick_str(c, &["listing_id", "id"])
}

/// What makes a changes-feed entry the same entry when the overlap reads it again.
pub fn change_key(c: &Value) -> String {
    let mut parts = vec![change_listing(c)];
    for k in ["version_id", "change", "updated_at"] {
        parts.push(pick_str(c, &[k]));
    }
    let mut key = parts.join("|");
    for (name, short) in [("published_version_ids", "pub"), ("withdrawn_version_ids", "wd")] {
        let mut ids = id_list(c.get(name));
        if !ids.is_empty() {
            ids.sort();
            key += &format!("|{short}:{}", ids.join(","));
        }
    }
    key
}

pub fn active_now(c: &Value, now: chrono::DateTime<chrono::Utc>) -> bool {
    let start = parse_iso(&pick_str(c, &["starts_at"]));
    let end = parse_iso(&pick_str(c, &["ends_at"]));
    start.is_none_or(|s| s <= now) && end.is_none_or(|e| now < e)
}

/// The listings the collections feature, in order without repeats, and a note for each: which collection, until when.
pub fn favourite_listings(collections: &[Value]) -> (Vec<Value>, serde_json::Map<String, Value>) {
    let mut out = vec![];
    let mut notes = serde_json::Map::new();
    for c in collections {
        let listings: Vec<Value> = match c.get("listings") {
            Some(Value::Array(a)) => a.iter().filter(|x| x.is_object() && !listing_id(x).is_empty()).cloned().collect(),
            _ => vec![],
        };
        let mut order: Vec<String> =
            id_list(c.get("listing_ids")).into_iter().filter(|i| listings.iter().any(|x| listing_id(x) == *i)).collect();
        for x in &listings {
            let id = listing_id(x);
            if !order.contains(&id) {
                order.push(id);
            }
        }
        for lid in order {
            if !notes.contains_key(&lid) {
                let x = listings.iter().find(|x| listing_id(x) == lid).unwrap();
                out.push(x.clone());
                let end: String = pick_str(c, &["ends_at"]).chars().take(10).collect();
                let title = tidy(&pick_str(c, &["title"]), false);
                let title = if title.is_empty() { "Favourites".to_string() } else { title };
                notes.insert(lid, Value::String(if end.is_empty() { title } else { format!("{title}, until {end}") }));
            }
        }
    }
    (out, notes)
}

pub fn sha_hex(v: &str) -> Option<String> {
    let s = v.trim();
    let s = if s.to_lowercase().starts_with("sha256:") { &s[7..] } else { s };
    (s.len() == 64 && s.chars().all(|c| c.is_ascii_hexdigit())).then(|| s.to_lowercase())
}

pub fn file_sha(path: &Path) -> std::io::Result<String> {
    use std::io::Read;
    let mut f = std::fs::File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex::encode(h.finalize()))
}

/// An ISO time moved back: the overlap a watermark keeps.
pub fn before(s: &str, seconds: i64) -> String {
    parse_iso(s).map(|d| utc_iso_at(d - chrono::Duration::seconds(seconds))).unwrap_or_else(|| s.to_string())
}

pub fn loopback(host: &str) -> bool {
    host == "localhost" || host.trim_matches(|c| c == '[' || c == ']').parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

/// https, or plain http to this machine only (the fake Forge in the demo).
pub fn secure(u: &str) -> bool {
    match url::Url::parse(u) {
        Ok(p) => p.scheme() == "https" || p.scheme() == "http" && p.host_str().is_some_and(loopback),
        Err(_) => false,
    }
}

fn origin(u: &str) -> (String, String, u16) {
    match url::Url::parse(u) {
        Ok(p) => (p.scheme().into(), p.host_str().unwrap_or_default().to_lowercase(), p.port_or_known_default().unwrap_or(0)),
        Err(_) => Default::default(),
    }
}

// --- the quota -------------------------------------------------------------------------------------------------
#[derive(Default, Clone)]
pub struct Quota {
    pub limit: Option<i64>,
    pub remaining: Option<i64>,
    pub reset_at: Option<Instant>,
}

impl Quota {
    fn read(&mut self, h: &reqwest::header::HeaderMap) {
        let get = |k: &str| h.get(k).and_then(|v| v.to_str().ok()).map(str::to_string);
        if let Some(v) = get("X-RateLimit-Limit").and_then(|v| v.parse().ok()) {
            self.limit = Some(v);
        }
        if let Some(v) = get("X-RateLimit-Remaining").and_then(|v| v.parse().ok()) {
            self.remaining = Some(v);
        }
        if let Some(r) = get("X-RateLimit-Reset").and_then(|v| v.parse::<f64>().ok()) {
            let secs = if r > 1e9 { r - crate::util::now() } else { r };
            self.reset_at = Some(Instant::now() + Duration::from_secs_f64(secs.max(0.0)));
        }
    }
    /// Seconds to hold off before the next request: background work stops RESERVE short.
    fn wait(&self, background: bool) -> f64 {
        match self.remaining {
            Some(r) if r <= if background { RESERVE } else { 0 } => {
                self.reset_at.map(|t| t.saturating_duration_since(Instant::now()).as_secs_f64()).unwrap_or(0.0)
            }
            _ => 0.0,
        }
    }
    pub fn words(&self) -> String {
        match self.remaining {
            None => String::new(),
            Some(r) => format!("{r}/{} requests left this minute", self.limit.map(|l| l.to_string()).unwrap_or("?".into())),
        }
    }
}

fn retry_after(h: &reqwest::header::HeaderMap) -> Option<f64> {
    let v = h.get("Retry-After")?.to_str().ok()?;
    if let Ok(s) = v.trim().parse::<f64>() {
        return Some(s.max(0.0));
    }
    chrono::DateTime::parse_from_rfc2822(v).ok().map(|d| (d.timestamp() as f64 - crate::util::now()).max(0.0))
}

// --- the cache -------------------------------------------------------------------------------------------------
/// Replies by URL, in a folder of the key's own (named by a hash of it). No key or header is ever written. Downloads
/// are kept by their SHA-256 and checked again each time they're used.
struct Cache {
    dir: Option<PathBuf>,
    assets: Option<PathBuf>,
}

impl Cache {
    fn new(root: Option<&Path>, key: &Secret) -> Self {
        Cache {
            dir: root.filter(|_| key.is_set()).map(|r| r.join("replies").join(&hex::encode(Sha256::digest(key.value().as_bytes()))[..16])),
            assets: root.map(|r| r.join("assets")),
        }
    }
    fn file(&self, url: &str) -> Option<PathBuf> {
        self.dir.as_ref().map(|d| d.join(format!("{}.json", &hex::encode(Sha256::digest(url.as_bytes()))[..32])))
    }
    fn get(&self, url: &str, max_age: Option<f64>) -> Option<(Value, f64)> {
        let entry: Value = serde_json::from_str(&std::fs::read_to_string(self.file(url)?).ok()?).ok()?;
        let age = crate::util::now() - num(entry.get("at")).unwrap_or(0.0);
        let body = entry.get("body")?.clone();
        (body.is_object() && max_age.is_none_or(|m| age <= m)).then_some((body, age))
    }
    fn put(&self, url: &str, body: &Value) {
        if let Some(f) = self.file(url) {
            let _ = write_atomic(&f, &json!({"url": url, "at": crate::util::now(), "body": body}).to_string(), false);
        }
    }
    fn drop_url(&self, url: &str) {
        if let Some(f) = self.file(url) {
            let _ = std::fs::remove_file(f);
        }
    }
}

// --- the client ------------------------------------------------------------------------------------------------
pub struct ForgeClient {
    key: Secret,
    base: String,
    origin: (String, String, u16),
    pub quota: Mutex<Quota>,
    cache: Cache,
    http: reqwest::Client,
    local: reqwest::Client,
    pub stop: Arc<AtomicBool>,
}

impl ForgeClient {
    pub fn new(key: Secret, base: &str, cache_dir: Option<&Path>) -> Result<Self, String> {
        if !secure(base) {
            return Err(format!("{base} isn't https: oni-rcon sends your Forge key over https only"));
        }
        let ua = format!("oni-rcon/{}", env!("CARGO_PKG_VERSION"));
        // redirects are followed by hand, so the key never follows one to another host
        let build = |proxy: bool| {
            let mut b = reqwest::Client::builder().user_agent(&ua).timeout(Duration::from_secs(20)).redirect(reqwest::redirect::Policy::none());
            if !proxy {
                b = b.no_proxy();
            }
            b.build().map_err(|e| e.to_string())
        };
        Ok(ForgeClient {
            cache: Cache::new(cache_dir, &key),
            key,
            base: base.trim_end_matches('/').into(),
            origin: origin(base),
            quota: Mutex::new(Quota::default()),
            http: build(true)?,
            local: build(false)?,
            stop: Arc::new(AtomicBool::new(false)),
        })
    }

    pub fn url(&self, path: &str, params: &[(&str, String)]) -> String {
        let q: Vec<String> = params
            .iter()
            .filter(|(_, v)| !v.is_empty())
            .map(|(k, v)| format!("{k}={}", url::form_urlencoded::byte_serialize(v.as_bytes()).collect::<String>()))
            .collect();
        format!("{}{path}{}", self.base, if q.is_empty() { String::new() } else { format!("?{}", q.join("&")) })
    }

    fn client_for(&self, u: &str) -> &reqwest::Client {
        let host = url::Url::parse(u).ok().and_then(|p| p.host_str().map(str::to_string)).unwrap_or_default();
        if loopback(&host) {
            &self.local
        } else {
            &self.http
        }
    }

    /// One request; a GET follows up to 5 redirects, and the key goes only to Forge's own origin.
    async fn send(&self, method: &Method, url: &str, accept_json: bool, body: &Body) -> Result<reqwest::Response, ForgeError> {
        let mut u = url.to_string();
        for _ in 0..6 {
            let mut req = self.client_for(&u).request(method.clone(), &u);
            if accept_json {
                req = req.header("Accept", "application/json");
            }
            if let Some((kind, bytes)) = body {
                req = req.header("Content-Type", *kind).body(bytes.clone());
            }
            if self.key.is_set() && origin(&u) == self.origin {
                req = req.header("Authorization", format!("Bearer {}", self.key.value()));
            }
            let r = req.send().await.map_err(|e| ForgeError::new(0, &e.without_url().to_string(), None))?;
            if r.status().is_redirection() && method == Method::GET {
                let loc = r.headers().get("Location").and_then(|v| v.to_str().ok()).unwrap_or_default();
                let next = url::Url::parse(&u).and_then(|b| b.join(loc)).map_err(|e| ForgeError::new(502, &e.to_string(), None))?;
                if !secure(next.as_str()) {
                    return Err(ForgeError::new(502, "redirected off https", None));
                }
                u = next.to_string();
                continue;
            }
            return Ok(r);
        }
        Err(ForgeError::new(502, "too many redirects", None))
    }

    async fn exchange(&self, method: Method, url: &str, body: Body, background: bool) -> Result<Value, ForgeError> {
        for attempt in 0..4 {
            let hold = self.quota.lock().wait(background);
            if hold > 0.0 {
                if hold > MAX_WAIT {
                    return Err(ForgeError::new(429, "", Some(hold)));
                }
                tokio::time::sleep(Duration::from_secs_f64(hold + crate::util::rnd(0.0, 1.0))).await;
            }
            let r = self.send(&method, url, true, &body).await?;
            self.quota.lock().read(r.headers());
            let status = r.status().as_u16();
            let ra = retry_after(r.headers());
            let body = r.bytes().await.map_err(|e| ForgeError::new(0, &e.without_url().to_string(), None))?;
            if (200..300).contains(&status) {
                if body.len() > MAX_JSON {
                    return Err(ForgeError::new(502, "reply too large", None));
                }
                return match serde_json::from_slice::<Value>(&body) {
                    _ if body.is_empty() => Ok(json!({})), // a 204, as a DELETE may send
                    Ok(Value::Null) => Ok(json!({})),
                    Ok(Value::Array(a)) => Ok(json!({"items": a})), // an unpaginated array, as /api/favourites sends
                    Ok(v @ Value::Object(_)) => Ok(v),
                    _ => Err(ForgeError::new(502, "not a JSON object", None)),
                };
            }
            let detail = serde_json::from_slice::<Value>(&body)
                .ok()
                .map(|d| match d.get("detail") {
                    Some(Value::Array(a)) => a.first().map(|e| pick_str(e, &["msg", "message"])).unwrap_or_default(), // a validation list
                    _ => pick_str(&d, &["message", "detail", "error", "title"]),
                })
                .unwrap_or_default();
            let err = ForgeError::new(status, &detail, ra);
            if !(status == 429 || status == 503) || attempt == 3 {
                return Err(err);
            }
            let q = self.quota.lock().wait(false);
            let wait = ra.unwrap_or(if q > 0.0 { q } else { 2f64.powi(attempt) });
            if wait > MAX_WAIT {
                return Err(err);
            }
            tokio::time::sleep(Duration::from_secs_f64(wait + crate::util::rnd(0.0, (wait / 4.0).max(0.5)))).await;
        }
        unreachable!()
    }

    /// A reply, served from the cache while fresh; when Forge can't be reached or is rate limiting, an older copy is
    /// served with "_stale" set to its age in seconds.
    pub async fn get(&self, path: &str, params: &[(&str, String)], max_age: f64, background: bool, stale_ok: bool) -> Result<Value, ForgeError> {
        if !self.key.is_set() {
            return Err(ForgeError::new(401, "no key loaded", None));
        }
        let url = self.url(path, params);
        if max_age > 0.0 {
            if let Some((hit, _)) = self.cache.get(&url, Some(max_age)) {
                return Ok(hit);
            }
        }
        match self.exchange(Method::GET, &url, None, background).await {
            Ok(body) => {
                self.cache.put(&url, &body);
                Ok(body)
            }
            Err(e) if stale_ok && matches!(e.status, 0 | 429 | 503) => match self.cache.get(&url, None) {
                Some((mut hit, age)) => {
                    hit["_stale"] = json!(age.round() as i64);
                    Ok(hit)
                }
                None => Err(e),
            },
            Err(e) => Err(e),
        }
    }

    pub async fn listings(&self, sort: &str, window: &str, q: &str, more: Option<&Value>, fresh: bool) -> Result<(Vec<Value>, Option<Value>, Value), ForgeError> {
        let mut params = vec![("page_size", "50".to_string()), ("sort", sort.to_string()), ("q", q.trim().to_string())];
        if WINDOWED.contains(&sort) {
            params.push(("window", window.to_string()));
        }
        if let Some(Value::Object(m)) = more {
            for (k, v) in m {
                let k: &'static str = if k == "page" { "page" } else { "cursor" };
                params.push((k, v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())));
            }
        }
        let reply = self.get("/api/listings", &params, if fresh { 0.0 } else { 60.0 }, false, true).await?;
        Ok((items_of(&reply), next_page(&reply), reply))
    }

    pub async fn favourites(&self, fresh: bool) -> Result<(Vec<Value>, Value), ForgeError> {
        let reply = self.get("/api/favourites", &[], if fresh { 0.0 } else { 60.0 }, false, true).await?;
        let now = chrono::Utc::now();
        Ok((items_of(&reply).into_iter().filter(|c| active_now(c, now)).collect(), reply))
    }

    fn listing_path(lid: &str) -> String {
        format!("/api/listings/{}", enc(lid))
    }

    pub fn forget(&self, lid: &str) {
        self.cache.drop_url(&self.url(&Self::listing_path(lid), &[]));
    }

    pub async fn listing(&self, lid: &str, background: bool) -> Result<Value, ForgeError> {
        let reply = self.get(&Self::listing_path(lid), &[], if background { 0.0 } else { 60.0 }, background, !background).await?;
        Ok(inner(reply))
    }

    pub async fn manifest(&self, lid: &str, vid: &str) -> Result<Value, ForgeError> {
        let path = format!("{}/versions/{}/manifest", Self::listing_path(lid), url::form_urlencoded::byte_serialize(vid.as_bytes()).collect::<String>());
        let reply = self.get(&path, &[], 0.0, false, false).await?;
        Ok(match pick(&reply, &["manifest", "data"]) {
            Some(inner @ Value::Object(_)) => inner.clone(),
            _ => reply,
        })
    }

    /// Every change since `since`, and the feed's as_of: the next watermark.
    pub async fn changes(&self, since: &str) -> Result<(Vec<Value>, String), ForgeError> {
        let first = self.get("/api/listings/changes", &[("updated_since", since.into()), ("page_size", "100".into())], 0.0, true, false).await?;
        let as_of = pick_str(&first, &["as_of"]);
        if as_of.is_empty() {
            return Err(ForgeError::new(502, "the changes feed gave no as_of to page from", None));
        }
        let mut out = items_of(&first);
        let mut reply = first;
        for _ in 0..50 {
            let Some(nxt) = next_page(&reply) else { break };
            let mut params = vec![("updated_since", since.to_string()), ("updated_before", as_of.clone()), ("page_size", "100".into())];
            for (k, v) in nxt.as_object().unwrap() {
                params.push((if k == "page" { "page" } else { "cursor" }, v.as_str().unwrap_or_default().to_string()));
            }
            reply = self.get("/api/listings/changes", &params, 0.0, true, false).await?;
            out.extend(items_of(&reply));
        }
        Ok((out, as_of))
    }

    // --- publishing -------------------------------------------------------------------------------------------
    /// One write: a JSON body (or none) in, the reply out. Never cached, never served stale.
    pub async fn write(&self, method: Method, path: &str, body: Option<Value>) -> Result<Value, ForgeError> {
        if !self.key.is_set() {
            return Err(ForgeError::new(401, "no key loaded", None));
        }
        self.exchange(method, &self.url(path, &[]), body.map(|b| ("application/json", b.to_string().into_bytes())), false).await
    }

    /// The listings this key's account owns or is an author of, drafts included.
    pub async fn mine(&self) -> Result<Vec<Value>, ForgeError> {
        Ok(items_of(&self.get("/api/listings/mine", &[], 0.0, false, false).await?))
    }

    pub async fn my_listing(&self, lid: &str) -> Result<Value, ForgeError> {
        Ok(inner(self.get(&format!("/api/listings/mine/{}", enc(lid)), &[], 0.0, false, false).await?))
    }

    /// One file onto a draft version, in the chunks Forge asks for, picking up wherever it says it got to.
    async fn upload(&self, lid: &str, vid: &str, name: &str, bytes: &[u8]) -> Result<Value, ForgeError> {
        let start = self
            .write(Method::POST, &format!("{}/versions/{}/uploads", Self::listing_path(lid), enc(vid)),
                Some(json!({"original_filename": name, "file_size": bytes.len(), "file_type": "auto"})))
            .await?;
        let id = pick_str(&start, &["id", "upload_id"]);
        if id.is_empty() {
            return Err(ForgeError::new(502, "the upload was given no id", None));
        }
        let chunk = num(start.get("chunk_size")).filter(|c| *c >= 1.0).unwrap_or(8.0 * 1048576.0) as usize;
        let mut at = num(start.get("received_size")).unwrap_or(0.0) as usize;
        let mut index = num(start.get("next_chunk_index")).unwrap_or(0.0) as u64;
        while at < bytes.len() {
            let end = (at + chunk).min(bytes.len());
            let url = self.url(&format!("/api/files/uploads/{}/chunks/{index}", enc(&id)), &[]);
            let r = self.exchange(Method::PUT, &url, Some(("application/octet-stream", bytes[at..end].to_vec())), false).await?;
            let got = num(r.get("received_size")).map(|n| n as usize).unwrap_or(end);
            if got <= at {
                return Err(ForgeError::new(502, "the upload stopped moving forward", None));
            }
            at = got;
            index = num(r.get("next_chunk_index")).map(|n| n as u64).unwrap_or(index + 1);
        }
        self.write(Method::POST, &format!("/api/listings/uploads/{}/complete", enc(&id)), None).await
    }

    /// Files onto Forge in one go: a new listing (`lid` None, `info` its fields) or a new version of `lid` (`info`
    /// its version_label and release_notes), every file uploaded, then published. A draft it made but couldn't
    /// publish is taken down again, so nothing half-done is left behind.
    pub async fn publish(&self, lid: Option<&str>, info: &Value, files: &[(String, Vec<u8>)], say: &(dyn Fn(String) + Send + Sync)) -> Result<Value, ForgeError> {
        let x = inner(match lid {
            None => self.write(Method::POST, "/api/listings", Some(info.clone())).await?,
            Some(l) => {
                let notes = json!({"version_label": info["version_label"], "release_notes": info["release_notes"]});
                self.write(Method::POST, &format!("{}/versions", Self::listing_path(l)), Some(notes)).await?
            }
        });
        let made = listing_id(&x);
        let vid = versions_of(&x).iter().find(|v| pick_str(v, &["status"]) == "draft").map(version_id).unwrap_or_default();
        if made.is_empty() || vid.is_empty() {
            return Err(ForgeError::new(502, "it made no draft version to upload to", None));
        }
        let done = async {
            for (name, bytes) in files {
                say(format!("  uploading {name}  ({} bytes)", bytes.len()));
                self.upload(&made, &vid, name, bytes).await?;
            }
            self.write(Method::POST, &format!("{}/versions/{}/publish", Self::listing_path(&made), enc(&vid)), None).await
        }
        .await;
        match done {
            Ok(x) => {
                self.forget(&made);
                Ok(inner(x))
            }
            Err(mut e) => {
                // ponytail: best effort; a key without listings:delete leaves the draft, which MINE shows
                let path = if lid.is_none() { Self::listing_path(&made) } else { format!("{}/versions/{}", Self::listing_path(&made), enc(&vid)) };
                if self.write(Method::DELETE, &path, None).await.is_ok() {
                    e.text += " Nothing was left half-done on ReclaimerForge.";
                }
                Err(e)
            }
        }
    }

    /// A change to something this key's account owns: "edit" a listing's details, "notes" for a version, "withdraw"
    /// a version, or "delete" the whole listing.
    pub async fn change(&self, what: &str, lid: &str, vid: &str, change: &Value) -> Result<Value, ForgeError> {
        let version = format!("{}/versions/{}", Self::listing_path(lid), enc(vid));
        let r = match what {
            "edit" => {
                // Forge resets what a PATCH leaves out, so everything the listing has now goes back with the change
                let x = self.my_listing(lid).await?;
                let mut body = serde_json::Map::new();
                for k in ["title", "description", "tags", "visibility", "is_modded_map", "workshop_urls", "min_players", "max_players"] {
                    if let Some(v) = change.get(k).or(x.get(k)).filter(|v| !v.is_null()) {
                        body.insert(k.into(), v.clone());
                    }
                }
                self.write(Method::PATCH, &Self::listing_path(lid), Some(Value::Object(body))).await?
            }
            "notes" => self.write(Method::PATCH, &version, Some(change.clone())).await?,
            "withdraw" => self.write(Method::DELETE, &version, None).await?,
            "delete" => self.write(Method::DELETE, &Self::listing_path(lid), None).await?,
            _ => return Err(ForgeError::new(422, what, None)),
        };
        self.forget(lid);
        Ok(inner(r))
    }

    /// The asset, fetched once into the cache and checked against the manifest's size and SHA-256 before anything can
    /// use it. Nothing that fails is kept.
    pub async fn download(&self, link: &str, size: i64, sha256: &str) -> Result<PathBuf, Unverified> {
        let digest = sha_hex(sha256).ok_or_else(|| Unverified("The manifest gives no SHA-256 for that file, so it can't be checked.".into()))?;
        if !(0..=MAX_ASSET).contains(&size) {
            return Err(Unverified("The manifest gives that file no size oni-rcon will accept.".into()));
        }
        let assets = self.cache.assets.clone().ok_or_else(|| Unverified("oni-rcon has nowhere to keep downloads.".into()))?;
        let path = assets.join(&digest);
        if path.metadata().map(|m| m.len() as i64 == size).unwrap_or(false) {
            let p = path.clone();
            if tokio::task::spawn_blocking(move || file_sha(&p).ok()).await.ok().flatten().as_deref() == Some(digest.as_str()) {
                return Ok(path);
            }
        }
        let url = url::Url::parse(&format!("{}/", self.base)).and_then(|b| b.join(link)).map_err(|e| Unverified(e.to_string()))?.to_string();
        if !secure(&url) {
            return Err(Unverified("The manifest links that file over plain http, so it isn't fetched.".into()));
        }
        for attempt in 0..3 {
            match self.stream(&url, size, &digest, &path).await {
                Ok(()) => return Ok(path),
                Err(Ok(e)) if matches!(e.status, 429 | 503) && attempt < 2 && e.retry_after.unwrap_or(0.0) <= MAX_WAIT => {
                    let wait = e.retry_after.unwrap_or(2f64.powi(attempt));
                    tokio::time::sleep(Duration::from_secs_f64(wait + crate::util::rnd(0.0, (wait / 4.0).max(0.5)))).await;
                }
                Err(Ok(e)) => return Err(Unverified(e.text)),
                Err(Err(u)) => return Err(u),
            }
        }
        unreachable!()
    }

    async fn stream(&self, url: &str, size: i64, digest: &str, path: &Path) -> Result<(), Result<ForgeError, Unverified>> {
        use futures_util::StreamExt;
        use tokio::io::AsyncWriteExt;
        if let Some(p) = path.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        let part = path.with_file_name(format!("{}.{}.{}.part", path.file_name().unwrap().to_string_lossy(), std::process::id(), crate::util::rnd(0.0, 4e9) as u32));
        let result = async {
            let r = self.send(&Method::GET, url, false, &None).await.map_err(Ok)?;
            if origin(url) == self.origin {
                self.quota.lock().read(r.headers());
            }
            if !r.status().is_success() {
                return Err(Ok(ForgeError::new(r.status().as_u16(), "", retry_after(r.headers()))));
            }
            let mut f = tokio::fs::File::create(&part).await.map_err(|e| Ok(ForgeError::new(0, &e.to_string(), None)))?;
            let mut h = Sha256::new();
            let mut got: i64 = 0;
            let mut body = r.bytes_stream();
            while let Some(chunk) = body.next().await {
                if self.stop.load(Ordering::Relaxed) {
                    return Err(Err(Unverified("Stopped before the download finished.".into())));
                }
                let chunk = chunk.map_err(|e| Ok(ForgeError::new(0, &e.without_url().to_string(), None)))?;
                got += chunk.len() as i64;
                if got > size {
                    return Err(Err(Unverified(format!("The file is bigger than the manifest says ({size} bytes): not used."))));
                }
                h.update(&chunk);
                f.write_all(&chunk).await.map_err(|e| Ok(ForgeError::new(0, &e.to_string(), None)))?;
            }
            f.flush().await.ok();
            drop(f);
            if got != size {
                return Err(Err(Unverified(format!("The file came to {got} bytes, not the {size} the manifest says: not used."))));
            }
            if hex::encode(h.finalize()) != digest {
                return Err(Err(Unverified("The file doesn't match the manifest's SHA-256: not used.".into())));
            }
            std::fs::rename(&part, path).map_err(|e| Ok(ForgeError::new(0, &e.to_string(), None)))
        }
        .await;
        let _ = std::fs::remove_file(&part);
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readers() {
        let x = json!({"id": "l1", "title": "A\u{202e}b  c", "authors": [{"user_id": "u2", "username": "Two"}, {"user_id": "u1", "username": "One", "is_owner": true}],
            "versions": [{"id": "v2", "status": "withdrawn", "created_at": "2026-01-02T00:00:00Z"}, {"id": "v1", "created_at": "2026-01-01T00:00:00Z"}],
            "latest_version": {"id": "v2"}});
        assert_eq!(title_of(&x), "Ab c");
        assert_eq!(author_of(&x), "One, Two");
        assert_eq!(owner_of(&x), "u1");
        assert_eq!(version_id(&latest_of(&x).unwrap()), "v1");
        assert!(!usable(&versions_of(&x)[0]));
    }

    #[test]
    fn change_keys_ignore_order_of_ids() {
        let a = json!({"listing_id": "l", "updated_at": "t", "published_version_ids": ["b", "a"]});
        let b = json!({"listing_id": "l", "updated_at": "t", "published_version_ids": ["a", "b"]});
        assert_eq!(change_key(&a), change_key(&b));
    }

    #[test]
    fn favourites_in_order() {
        let c = json!([{"title": "Picks", "ends_at": "2026-12-01T00:00:00Z", "listing_ids": ["b", "a"], "listings": [{"id": "a"}, {"id": "b"}]}]);
        let (out, notes) = favourite_listings(c.as_array().unwrap());
        assert_eq!(out.iter().map(listing_id).collect::<Vec<_>>(), ["b", "a"]);
        assert_eq!(notes["a"], "Picks, until 2026-12-01");
    }

    #[test]
    fn only_https_or_loopback() {
        assert!(secure("https://x.org"));
        assert!(secure("http://127.0.0.1:5"));
        assert!(!secure("http://x.org"));
        assert!(ForgeClient::new(Secret::new("rfk_testkey123"), "http://example.org", None).is_err());
    }

    #[test]
    fn errors_in_words() {
        let e = ForgeError::new(429, "", Some(3.2));
        assert!(e.text.contains("4s"));
        assert_eq!(ForgeError::new(401, "", None).short, "KEY REFUSED");
        assert!(ForgeError::new(0, "refused rfk_secret_value", None).text.contains("rfk_…"));
    }
}
