//! Which servers to manage, how to reach them, and where each password comes from. The same config file as the
//! terminal console: oni-rcon.example.toml lists every key.
use crate::util::{make_private, write_atomic};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::time::Duration;
use toml::Value;

/// A password_command or forge_api_key_command: a string runs through the shell, a list runs as-is.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Cmd {
    #[default]
    None,
    Line(String),
    Argv(Vec<String>),
}

impl Cmd {
    pub fn from_toml(v: Option<&Value>) -> Result<Cmd, String> {
        match v {
            None => Ok(Cmd::None),
            Some(Value::String(s)) if s.is_empty() => Ok(Cmd::None),
            Some(Value::String(s)) => Ok(Cmd::Line(s.clone())),
            Some(Value::Array(a)) => a
                .iter()
                .map(|x| x.as_str().map(str::to_string).ok_or_else(|| "a command list holds only strings".to_string()))
                .collect::<Result<Vec<_>, _>>()
                .map(|v| if v.is_empty() { Cmd::None } else { Cmd::Argv(v) }),
            Some(_) => Err("a command is a string or a list of strings".into()),
        }
    }
    pub fn is_set(&self) -> bool {
        !matches!(self, Cmd::None)
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Server {
    pub port: u16,
    pub host: String,
    pub name: String,
    pub ssh: String,
    pub url: String,
    #[serde(skip)]
    pub password: String,
    pub password_env: String,
    #[serde(skip)]
    pub password_command: Cmd,
    pub content_dir: String,
    pub ping_log: String,
    pub health_cmd: String,
    pub restart_cmd: String,
}

const SERVER_KEYS: &[&str] =
    &["port", "host", "name", "ssh", "url", "password", "password_env", "password_command", "content_dir", "restart_cmd"];

impl Server {
    pub fn new(host: &str, port: u16) -> Self {
        Server { host: host.into(), port, ..Default::default() }
    }

    /// How this console tells servers apart: a URL, or host:port with the SSH destination.
    pub fn where_(&self) -> String {
        if !self.url.is_empty() {
            return self.url.clone();
        }
        let hp = format!("{}:{}", self.host, self.port);
        if self.ssh.is_empty() {
            hp
        } else {
            format!("{hp} via ssh {}", self.ssh)
        }
    }

    /// The WebSocket URL for a direct connection.
    pub fn direct_url(&self) -> String {
        if !self.url.is_empty() {
            self.url.clone()
        } else if self.host.contains(':') {
            format!("ws://[{}]:{}", self.host, self.port)
        } else {
            format!("ws://{}:{}", self.host, self.port)
        }
    }

    fn from_table(t: &toml::map::Map<String, Value>) -> Result<Server, String> {
        if let Some(k) = t.keys().find(|k| !SERVER_KEYS.contains(&k.as_str())) {
            return Err(format!("unknown key {k:?} in a [[server]] block"));
        }
        let s = |k: &str| -> Result<String, String> {
            match t.get(k) {
                None => Ok(String::new()),
                Some(Value::String(v)) => Ok(v.clone()),
                Some(_) => Err(format!("{k} must be a string")),
            }
        };
        let port = match t.get("port") {
            None => 0,
            Some(Value::Integer(p)) if (1..65536).contains(p) => *p as u16,
            Some(_) => return Err("port must be a number from 1 to 65535".into()),
        };
        let mut host = s("host")?;
        if host.is_empty() {
            host = "127.0.0.1".into();
        }
        let srv = Server {
            port,
            host,
            name: s("name")?,
            ssh: s("ssh")?,
            url: s("url")?,
            password: s("password")?,
            password_env: s("password_env")?,
            password_command: Cmd::from_toml(t.get("password_command"))?,
            content_dir: s("content_dir")?,
            restart_cmd: s("restart_cmd")?.replace("{port}", &port.to_string()),
            ..Default::default()
        };
        if srv.url.is_empty() && srv.port == 0 {
            return Err("every server needs a port or a url".into());
        }
        Ok(srv)
    }
}

/// PORT, HOST:PORT, [IPv6]:PORT or a ws(s):// URL.
pub fn parse_target(target: &str, ssh: &str, name: &str) -> Result<Server, String> {
    let target = target.trim();
    if target.contains("://") {
        return Ok(Server { url: target.into(), host: "127.0.0.1".into(), ssh: ssh.into(), name: name.into(), ..Default::default() });
    }
    let (host, port) = match target.rfind(':') {
        Some(i) => (&target[..i], &target[i + 1..]),
        None => ("", target),
    };
    let bad = || format!("{target:?} isn't PORT, HOST:PORT, [IPv6]:PORT or a ws(s):// URL");
    let port: u16 = port.parse().map_err(|_| bad())?;
    if port == 0 {
        return Err(bad());
    }
    let host = host.trim_matches(|c| c == '[' || c == ']');
    Ok(Server {
        host: if host.is_empty() { "127.0.0.1".into() } else { host.into() },
        port,
        ssh: ssh.into(),
        name: name.into(),
        ..Default::default()
    })
}

pub fn user_config() -> PathBuf {
    let base = std::env::var_os("APPDATA")
        .or_else(|| std::env::var_os("XDG_CONFIG_HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join(".config"));
    base.join("oni-rcon").join("config.toml")
}

pub fn cache_root() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .or_else(|| std::env::var_os("XDG_CACHE_HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join(".cache"));
    base.join("oni-rcon")
}

/// $ONI_RCON_CONFIG, ./oni-rcon.toml (and beside the app itself, for a double-click), then the user config.
pub fn default_config() -> Option<PathBuf> {
    let mut tries: Vec<PathBuf> = vec![];
    if let Some(p) = std::env::var_os("ONI_RCON_CONFIG") {
        tries.push(p.into());
    }
    tries.push("oni-rcon.toml".into());
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            tries.push(dir.join("oni-rcon.toml"));
        }
    }
    tries.push(user_config());
    tries.into_iter().find(|p| p.is_file())
}

pub fn read_toml(path: &Path) -> Result<toml::map::Map<String, Value>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    text.parse::<toml::Table>().map_err(|e| format!("{}: {e}", path.display()))
}

/// (moderator name, servers). [defaults] applies to every [[server]] unless it sets the key itself.
pub fn load_config(path: &Path) -> Result<(String, Vec<Server>), String> {
    let data = read_toml(path)?;
    let at = |e: String| format!("{}: {e}", path.display());
    let base = data.get("defaults").and_then(Value::as_table).cloned().unwrap_or_default();
    let mut servers = vec![];
    if let Some(list) = data.get("server").and_then(Value::as_array) {
        for s in list {
            let mut merged = base.clone();
            for (k, v) in s.as_table().ok_or_else(|| at("[[server]] must be a table".into()))? {
                merged.insert(k.clone(), v.clone());
            }
            servers.push(Server::from_table(&merged).map_err(at)?);
        }
    }
    if servers.is_empty() {
        return Err(at("no [[server]] entries".into()));
    }
    if let Some(t) = data.get("ping_log").and_then(Value::as_str) {
        for s in servers.iter_mut() {
            s.ping_log = if s.url.is_empty() { t.replace("{port}", &s.port.to_string()) } else { String::new() };
        }
    }
    if let Some(t) = data.get("health_cmd").and_then(Value::as_str) {
        for s in servers.iter_mut() {
            s.health_cmd = if s.url.is_empty() { t.replace("{port}", &s.port.to_string()) } else { String::new() };
        }
    }
    for s in servers.iter_mut().filter(|s| s.restart_cmd.is_empty()) {
        s.restart_cmd = crate::host::default_restart(s); // RESTART SERVER stays greyed out without one
    }
    let by = data.get("by").and_then(Value::as_str).unwrap_or_default().to_string();
    Ok((by, servers))
}

/// password > password_env > password_command > $ONI_RCON_PASSWORD. Returns the servers still without one: the
/// console asks once for those, as the terminal one prompted.
pub async fn resolve_passwords(servers: &mut [Server]) -> Result<Vec<usize>, String> {
    let mut ran: Vec<(Cmd, String)> = vec![];
    let mut missing = vec![];
    for (i, s) in servers.iter_mut().enumerate() {
        if !s.password.is_empty() {
            continue;
        }
        if !s.password_env.is_empty() {
            if let Ok(v) = std::env::var(&s.password_env) {
                if !v.is_empty() {
                    s.password = v;
                    continue;
                }
            }
        }
        if s.password_command.is_set() {
            if let Some((_, v)) = ran.iter().find(|(c, _)| *c == s.password_command) {
                s.password = v.clone();
            } else {
                let v = run_command(&s.password_command, "password_command").await?;
                ran.push((s.password_command.clone(), v.clone()));
                s.password = v;
            }
            if s.password.is_empty() {
                return Err(format!("{}: password_command printed nothing", s.where_()));
            }
            continue;
        }
        if let Ok(v) = std::env::var("ONI_RCON_PASSWORD") {
            if !v.is_empty() {
                s.password = v;
                continue;
            }
        }
        missing.push(i);
    }
    Ok(missing)
}

/// The first line a command prints.
pub async fn run_command(cmd: &Cmd, what: &str) -> Result<String, String> {
    let mut c = match cmd {
        Cmd::None => return Ok(String::new()),
        Cmd::Line(l) => crate::util::shell(l),
        Cmd::Argv(a) => {
            let mut c = crate::util::command(&a[0]);
            c.args(&a[1..]);
            c
        }
    };
    c.stdin(std::process::Stdio::null());
    let out = tokio::time::timeout(Duration::from_secs(60), c.output())
        .await
        .map_err(|_| format!("{what} failed: no answer in 60s"))?
        .map_err(|e| format!("{what} failed: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "{what} exited {}: {}",
            out.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).lines().next().unwrap_or_default().to_string())
}

/// The config file's top-level keys starting with `prefix`: forge_* or health_*.
pub fn top_level(path: Option<&Path>, prefix: &str) -> toml::map::Map<String, Value> {
    path.and_then(|p| read_toml(p).ok())
        .map(|d| d.into_iter().filter(|(k, _)| k.starts_with(prefix)).collect())
        .unwrap_or_default()
}

/// A TOML basic string.
pub fn toml_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' | '\\' => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Save the Forge API key in the config file: only ever when the operator ticks for it.
pub fn remember_forge_key(path: &Path, key: &str) -> Result<(), String> {
    if set_top_level(path, "forge_api_key", &toml_str(key), false, true, Some(key)) {
        Ok(())
    } else {
        Err("the key couldn't be saved in that config file".into())
    }
}

/// Set a top-level key (`value` is TOML) at the top, after the opening comments, where a top-level key is always
/// valid TOML, or in place of a one-line one there. `keep`: leave one that's set. False, with the file as it was, if
/// it wouldn't read back with the value (`check`, if given).
pub fn set_top_level(path: &Path, key: &str, value: &str, keep: bool, private: bool, check: Option<&str>) -> bool {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let mut lines: Vec<String> = text.split_inclusive('\n').map(str::to_string).collect();
    if let Some(l) = lines.last_mut() {
        if !l.ends_with('\n') {
            l.push('\n');
        }
    }
    let Ok(before) = lines.concat().parse::<toml::Table>() else { return false };
    let had = before.contains_key(key);
    if had && keep {
        return true;
    }
    let line = format!("{key} = {value}\n");
    let tables = lines.iter().position(|x| x.starts_with('[')).unwrap_or(lines.len());
    let re = regex::Regex::new(&format!(r"^{}\s*=", regex::escape(key))).unwrap();
    match lines[..tables].iter().position(|x| re.is_match(x)) {
        Some(i) if had && !lines[i].contains("'''") && !lines[i].contains("\"\"\"") => lines[i] = line,
        _ if had => return false, // spread over lines, or not where it's looked for: the operator's to change
        _ => {
            let at = lines.iter().position(|x| !x.trim().is_empty() && !x.trim_start().starts_with('#')).unwrap_or(lines.len());
            lines.insert(at, line);
        }
    }
    let out: String = lines.concat();
    let ok = out.parse::<toml::Table>().ok().and_then(|t| t.get(key).cloned()).is_some_and(|v| check.is_none_or(|c| v.as_str() == Some(c)));
    ok && write_atomic(path, &out, private).is_ok() // never leave the operator a config that won't load
}

/// Append [[server]] blocks, so the comments and settings already in the file stay as they were.
pub fn add_servers(path: &Path, servers: &[Server], by: &str, remember: bool) -> std::io::Result<()> {
    let mut text = if path.is_file() {
        std::fs::read_to_string(path)?
    } else {
        let mut t = String::from(
            "# oni-rcon servers, written by the setup screen. Edit freely: oni-rcon.example.toml lists every option.\n",
        );
        if !by.is_empty() {
            t += &format!("by = {}\n", toml_str(by));
        }
        t
    };
    for s in servers {
        text += "\n[[server]]\n";
        if !s.name.is_empty() {
            text += &format!("name = {}\n", toml_str(&s.name));
        }
        if !s.url.is_empty() {
            text += &format!("url = {}\n", toml_str(&s.url));
        } else {
            text += &format!("host = {}\nport = {}\n", toml_str(&s.host), s.port);
        }
        text += &format!("ssh = {}\n", toml_str(&s.ssh)); // always set, so a [defaults] tunnel doesn't apply
        match &s.password_command {
            Cmd::Line(l) => text += &format!("password_command = {}\n", toml_str(l)), // read where the server is, each start
            Cmd::Argv(a) => text += &format!("password_command = [{}]\n", a.iter().map(|x| toml_str(x)).collect::<Vec<_>>().join(", ")),
            Cmd::None if remember && !s.password.is_empty() => text += &format!("password = {}\n", toml_str(&s.password)),
            Cmd::None => {}
        }
        if !s.content_dir.is_empty() {
            text += &format!("content_dir = {}\n", toml_str(&s.content_dir));
        }
        if !s.restart_cmd.is_empty() {
            text += &format!("restart_cmd = {}\n", toml_str(&s.restart_cmd));
        }
    }
    if !text.ends_with('\n') {
        text.push('\n');
    }
    if let Some(p) = path.parent() {
        std::fs::create_dir_all(p)?;
    }
    std::fs::write(path, text)?;
    if remember {
        make_private(path);
    }
    Ok(())
}

/// Take the `index`th [[server]] out of the config, the rest of the file as it was: how many are left.
pub fn remove_server(path: &Path, index: usize) -> Result<usize, String> {
    let at = |e: String| format!("{}: {e}", path.display());
    let mut doc = std::fs::read_to_string(path).map_err(|e| at(e.to_string()))?.parse::<toml_edit::DocumentMut>().map_err(|e| at(e.to_string()))?;
    let list = doc.get_mut("server").and_then(toml_edit::Item::as_array_of_tables_mut).filter(|l| index < l.len()).ok_or("no such server in the config")?;
    list.remove(index);
    let left = list.len();
    write_atomic(path, &doc.to_string(), true).map_err(|e| at(e.to_string()))?;
    Ok(left)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_removed_server_leaves_the_rest() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("c.toml");
        std::fs::write(&p, "# mine\nby = \"op\"\n[defaults]\nssh = \"box\"\n\n[[server]]\nport = 1\n[[server]]\nport = 2\n").unwrap();
        assert_eq!(remove_server(&p, 0), Ok(1));
        let (_, got) = load_config(&p).unwrap();
        assert_eq!((got.len(), got[0].port), (1, 2));
        assert!(std::fs::read_to_string(&p).unwrap().contains("# mine"));
        assert!(remove_server(&p, 1).is_err()); // no second one
        assert_eq!(remove_server(&p, 0), Ok(0));
    }

    #[test]
    fn targets() {
        let s = parse_target("11774", "", "").unwrap();
        assert_eq!((s.host.as_str(), s.port), ("127.0.0.1", 11774));
        let s = parse_target("[::1]:5", "", "").unwrap();
        assert_eq!(s.host, "::1");
        assert_eq!(s.direct_url(), "ws://[::1]:5");
        let s = parse_target("wss://x.org/a", "", "").unwrap();
        assert_eq!(s.where_(), "wss://x.org/a");
        assert!(parse_target("box:notaport", "", "").is_err());
        let s = parse_target("10.0.0.2:11774", "admin@box", "").unwrap();
        assert_eq!(s.where_(), "10.0.0.2:11774 via ssh admin@box");
    }

    #[test]
    fn config_merges_defaults_and_templates() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("c.toml");
        std::fs::write(
            &p,
            "by = \"me\"\nping_log = \"logs {port}\"\n[defaults]\nssh = \"a@b\"\n[[server]]\nport = 11774\n[[server]]\nport = 11775\nssh = \"\"\n",
        )
        .unwrap();
        let (by, s) = load_config(&p).unwrap();
        assert_eq!(by, "me");
        assert_eq!(s[0].ssh, "a@b");
        assert_eq!(s[1].ssh, "");
        assert_eq!(s[1].ping_log, "logs 11775");
        std::fs::write(&p, "[[server]]\nport = 1\nbogus = 2\n").unwrap();
        assert!(load_config(&p).unwrap_err().contains("bogus"));
    }

    #[test]
    fn remembering_the_key_keeps_the_file_valid() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("c.toml");
        std::fs::write(&p, "# hi\nby = \"x\"\n[[server]]\nport = 1\n").unwrap();
        remember_forge_key(&p, "rfk_a\"b").unwrap();
        remember_forge_key(&p, "rfk_second").unwrap();
        let t = read_toml(&p).unwrap();
        assert_eq!(t["forge_api_key"].as_str(), Some("rfk_second"));
        assert_eq!(std::fs::read_to_string(&p).unwrap().matches("forge_api_key").count(), 1);
    }

    #[test]
    fn added_servers_load_back() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("c.toml");
        let mut s = parse_target("127.0.0.1:11774", "", "Slayer").unwrap();
        s.password = "pw\"x".into();
        add_servers(&p, &[s], "op", true).unwrap();
        let (by, got) = load_config(&p).unwrap();
        assert_eq!((by.as_str(), got[0].name.as_str(), got[0].password.as_str()), ("op", "Slayer", "pw\"x"));
    }
}
