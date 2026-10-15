//! Small things everything shares: the Forge key that never prints, scrubbing it out of text, atomic writes,
//! subprocesses that never pop a console window, and time in the shapes the rest wants.
use once_cell::sync::Lazy;
use parking_lot::RwLock;
use regex::Regex;
use serde_json::Value;
use std::fmt;
use std::path::Path;

pub static KEY_LIKE: Lazy<Regex> = Lazy::new(|| Regex::new(r"rfk_[A-Za-z0-9_\-]{6,}").unwrap());
pub const HIDDEN: &str = "rfk_…████";
static LIVE: Lazy<RwLock<Vec<String>>> = Lazy::new(|| RwLock::new(Vec::new()));

/// A key that never prints: Debug and Display both show it blanked. `.value()` goes in the Authorization header and
/// nowhere else.
#[derive(Clone, Default)]
pub struct Secret(String);

impl Secret {
    pub fn new(v: &str) -> Self {
        let v = v.trim().to_string();
        if !v.is_empty() {
            let mut live = LIVE.write();
            if !live.contains(&v) {
                live.push(v.clone());
            }
        }
        Secret(v)
    }
    pub fn value(&self) -> &str {
        &self.0
    }
    pub fn is_set(&self) -> bool {
        !self.0.is_empty()
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Secret({HIDDEN:?})")
    }
}
impl fmt::Display for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(HIDDEN)
    }
}

/// Text with every loaded key, and anything shaped like one, blanked.
pub fn scrub(text: &str) -> String {
    let live = LIVE.read();
    if !KEY_LIKE.is_match(text) && !live.iter().any(|k| text.contains(k.as_str())) {
        return text.to_string();
    }
    let mut out = text.to_string();
    for k in live.iter() {
        out = out.replace(k.as_str(), HIDDEN);
    }
    KEY_LIKE.replace_all(&out, HIDDEN).into_owned()
}

/// A reply or event with any key in it blanked, for showing.
pub fn scrub_value(v: &mut Value) {
    match v {
        Value::String(s) => {
            if KEY_LIKE.is_match(s) || LIVE.read().iter().any(|k| s.contains(k.as_str())) {
                *s = scrub(s);
            }
        }
        Value::Array(a) => a.iter_mut().for_each(scrub_value),
        Value::Object(o) => o.values_mut().for_each(scrub_value),
        _ => {}
    }
}

/// Write through a temp file, so a crash midway leaves the old file whole. `private`: owner-only on Unix.
pub fn write_atomic(path: &Path, text: &str, private: bool) -> std::io::Result<()> {
    if let Some(p) = path.parent() {
        std::fs::create_dir_all(p)?;
    }
    let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    let tmp = path.with_file_name(format!("{name}.{}.tmp", std::process::id()));
    std::fs::write(&tmp, text)?;
    #[cfg(unix)]
    if private {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    let _ = private;
    std::fs::rename(&tmp, path)
}

#[cfg(unix)]
pub fn make_private(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}
#[cfg(not(unix))]
pub fn make_private(_: &Path) {}

/// A subprocess for a GUI app: no console window flashing up on Windows, killed if dropped.
pub fn command(program: &str) -> tokio::process::Command {
    let mut c = tokio::process::Command::new(program);
    c.kill_on_drop(true);
    #[cfg(windows)]
    {
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    c
}

/// A command line through the shell: `sh -c` here, `cmd /C` on Windows.
pub fn shell(line: &str) -> tokio::process::Command {
    #[cfg(windows)]
    {
        let mut c = command("cmd");
        c.raw_arg(format!("/S /C \"{line}\"")); // as typed: arg() escapes quotes in a way cmd doesn't read
        c
    }
    #[cfg(not(windows))]
    {
        let mut c = command("sh");
        c.arg("-c").arg(line);
        c
    }
}

/// The ssh client on PATH, if there is one.
pub fn which(name: &str) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    let exts: Vec<String> = if cfg!(windows) {
        vec![".exe".into(), "".into()]
    } else {
        vec!["".into()]
    };
    for dir in std::env::split_paths(&path) {
        for e in &exts {
            let p = dir.join(format!("{name}{e}"));
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn utc_iso_at(t: chrono::DateTime<chrono::Utc>) -> String {
    t.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

pub fn utc_iso() -> String {
    utc_iso_at(chrono::Utc::now())
}

pub fn parse_iso(s: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    let s = s.trim();
    if s.is_empty() {
        return None;
    }
    if let Ok(d) = chrono::DateTime::parse_from_rfc3339(&s.replace(' ', "T")) {
        return Some(d.with_timezone(&chrono::Utc));
    }
    for f in ["%Y-%m-%dT%H:%M:%S%.f%z", "%Y-%m-%dT%H:%M:%S%z"] {
        if let Ok(d) = chrono::DateTime::parse_from_str(&s.replace(' ', "T"), f) {
            return Some(d.with_timezone(&chrono::Utc));
        }
    }
    for f in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"] {
        if let Ok(d) = chrono::NaiveDateTime::parse_from_str(&s.replace(' ', "T"), f) {
            return Some(d.and_utc());
        }
        if let Ok(d) = chrono::NaiveDate::parse_from_str(s, f) {
            return d.and_hms_opt(0, 0, 0).map(|d| d.and_utc());
        }
    }
    None
}

/// `v` in a reply as a str, an int or nothing: field names are read tolerantly, so a lookup takes several keys.
pub fn pick<'a>(d: &'a Value, keys: &[&str]) -> Option<&'a Value> {
    let o = d.as_object()?;
    keys.iter()
        .filter_map(|k| o.get(*k))
        .find(|v| !(v.is_null() || v.as_str() == Some("")))
}

pub fn pick_str(d: &Value, keys: &[&str]) -> String {
    match pick(d, keys) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        Some(v) if !v.is_null() => v.to_string(),
        _ => String::new(),
    }
}

/// A number, not a bool; None for anything else.
pub fn num(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64(),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn shell_runs_the_line_as_typed() {
        let out = shell("echo \"a  b\"").output().await.unwrap();
        assert!(String::from_utf8_lossy(&out.stdout).contains("a  b"));
    }

    #[test]
    fn secrets_never_print() {
        let s = Secret::new("rfk_live_abcdefghij");
        assert_eq!(format!("{s}"), HIDDEN);
        assert!(!format!("{s:?}").contains("abcdef"));
        assert_eq!(scrub("key rfk_live_abcdefghij here"), format!("key {HIDDEN} here"));
        assert_eq!(scrub("rfk_other_12345678"), HIDDEN);
        let mut v = serde_json::json!({"a": ["x rfk_live_abcdefghij"]});
        scrub_value(&mut v);
        assert_eq!(v["a"][0], format!("x {HIDDEN}"));
    }

    #[test]
    fn iso_times_read_and_write() {
        let d = parse_iso("2026-10-10T08:12:44Z").unwrap();
        assert_eq!(utc_iso_at(d), "2026-10-10T08:12:44Z");
        assert!(parse_iso("2026-10-10T08:12:44.123456789Z").is_some());
        assert_eq!(parse_iso("2026-10-10T08:12:44+0000"), Some(d));
        assert_eq!(parse_iso("2026-10-10 08:12:44"), Some(d));
        assert!(parse_iso("nonsense").is_none());
    }
}

/// A random number in [lo, hi): a plain f64, so nothing of the generator is held across an await.
pub fn rnd(lo: f64, hi: f64) -> f64 {
    use rand::Rng;
    if hi <= lo {
        return lo;
    }
    rand::rng().random_range(lo..hi)
}
