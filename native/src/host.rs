//! Host a server on this computer, or find the servers on a game box: the setup screen's HOST ONE HERE and FIND
//! SERVERS ON IT, following Project Reclaimer's own guide (projectreclaimer.dev/host). No firewall rule, no service: only the server
//! folder, its dedicated.toml and the config file are written.
use crate::config::{self, toml_str, Cmd, Server};
use crate::rcon::SSH_OPTS;
use crate::util::{command, which, write_atomic};
use once_cell::sync::Lazy;
use regex::Regex;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

const RELEASES: &str = "https://api.github.com/repos/ProjectReclaimer/project-reclaimer-releases/releases/latest";
const COMPOSE: &str = "https://projectreclaimer.dev/docker/compose.yaml";
const GAME: &str = "Halo The Master Chief Collection";
// the shell that runs on the game box
const DISCOVER: &str = include_str!("discover.sh");
const HEALTH: &str = include_str!("docker-health.sh");
const NO_GAME: &str = "Halo: The Master Chief Collection wasn't found in your Steam libraries. Install it (with Halo 3) from Steam, then try again.";
const NO_SYSTEM: &str = "Project Reclaimer servers run on Windows, or on Linux with Docker; not on this system";
const NO_DOCKER: &str = "Docker isn't installed. On Ubuntu: sudo apt install docker.io docker-compose-v2, then add yourself to the docker group (sudo usermod -aG docker $USER) and log in again";

/// What to tell the operator about letting players in: the ports the servers use, and the commands for them.
fn open_ports_note(ports: &[u16], master: u16, upnp: bool) -> String {
    let list = ports.iter().map(u16::to_string).collect::<Vec<_>>().join(",");
    let how = if cfg!(windows) {
        format!("Paste this into any PowerShell (it asks for administrator, which the rules need): Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','New-NetFirewallRule -DisplayName ''Reclaimer games'' -Direction Inbound -Protocol UDP -LocalPort {list} -Action Allow; New-NetFirewallRule -DisplayName ''Reclaimer browser'' -Direction Inbound -Protocol TCP -LocalPort {master} -Action Allow'")
    } else {
        format!("sudo ufw allow {}/udp && sudo ufw allow {master}/tcp", list.replace(',', "/udp && sudo ufw allow "))
    };
    let router = if upnp { "UPnP is on, so the router is asked to forward them; if friends still can't join, forward them yourself." } else { "Friends outside your home also need them forwarded on your router." };
    format!("Players on other machines need UDP {list} and TCP {master} open. {how}. {router}")
}

pub type Say<'a> = &'a (dyn Fn(&str) + Send + Sync);
/// (the servers, what to tell the operator about the rest, top-level config keys to add: ping_log and health_cmd)
pub type Hosted = (Vec<Server>, Vec<String>, Vec<(String, String)>);

// --- finding the game ----------------------------------------------------------------------------------------------
fn steam_roots() -> Vec<PathBuf> {
    if cfg!(windows) {
        let mut roots = vec![];
        let mut reg = std::process::Command::new("reg");
        reg.args(["query", r"HKCU\Software\Valve\Steam", "/v", "SteamPath"]);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            reg.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: a console flashed up while hosting
        }
        if let Ok(out) = reg.output() {
            let text = String::from_utf8_lossy(&out.stdout);
            if let Some(p) = text.lines().find_map(|l| l.split_once("REG_SZ").map(|(_, p)| p.trim().to_string())) {
                roots.push(PathBuf::from(p));
            }
        }
        roots.push(PathBuf::from(std::env::var("ProgramFiles(x86)").unwrap_or_else(|_| r"C:\Program Files (x86)".into())).join("Steam"));
        roots
    } else {
        let h = dirs::home_dir().unwrap_or_default();
        vec![h.join(".local/share/Steam"), h.join(".steam/steam"), h.join(".var/app/com.valvesoftware.Steam/.local/share/Steam")]
    }
}

/// The Steam library folders listed in libraryfolders.vdf.
fn library_paths(vdf: &str) -> Vec<String> {
    static PATH: Lazy<Regex> = Lazy::new(|| Regex::new(r#""path"\s+"((?:[^"\\]|\\.)*)""#).unwrap());
    static ESC: Lazy<Regex> = Lazy::new(|| Regex::new(r"\\(.)").unwrap());
    PATH.captures_iter(vdf).map(|c| ESC.replace_all(&c[1], "$1").to_string()).collect()
}

/// The MCC folder with Halo 3 in it, from any Steam library on this machine.
pub fn find_game() -> Option<PathBuf> {
    for root in steam_roots() {
        let mut libs = vec![root.clone()];
        if let Ok(vdf) = std::fs::read_to_string(root.join("steamapps/libraryfolders.vdf")) {
            libs.extend(library_paths(&vdf).into_iter().map(PathBuf::from));
        }
        for lib in libs {
            let game = lib.join("steamapps/common").join(GAME);
            if game.join("halo3/halo3.dll").is_file() {
                return Some(game);
            }
        }
    }
    None
}

// --- dedicated.toml ------------------------------------------------------------------------------------------------
fn rcon_password(text: &str) -> Option<String> {
    let t = text.parse::<toml::Table>().ok()?;
    t.get("rcon")?.get("password")?.as_str().filter(|p| !p.is_empty()).map(str::to_string)
}

/// dedicated.toml with the remote console on: (the text, its password). A password it has already stays.
pub fn ensure_rcon_password(text: &str, new: &str) -> Result<(String, String), String> {
    if let Some(have) = rcon_password(text) {
        return Ok((text.to_string(), have));
    }
    static HEAD: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s*\[rcon\]\s*(#.*)?\s*$").unwrap());
    static PW: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s*password\s*=").unwrap());
    let mut lines: Vec<String> = text.split_inclusive('\n').map(str::to_string).collect();
    if let Some(l) = lines.last_mut() {
        if !l.ends_with('\n') {
            l.push('\n');
        }
    }
    let line = format!("password = {}\n", toml_str(new));
    match lines.iter().position(|x| HEAD.is_match(x)) {
        None => lines.extend(["\n[rcon]\n".to_string(), line]),
        Some(head) => {
            let end = (head + 1..lines.len()).find(|&i| lines[i].trim_start().starts_with('[')).unwrap_or(lines.len());
            match (head + 1..end).find(|&i| PW.is_match(&lines[i])) {
                Some(i) => lines[i] = line,
                None => lines.insert(head + 1, line),
            }
        }
    }
    let out = lines.concat();
    if rcon_password(&out).as_deref() != Some(new) {
        // never leave the servers a file they won't read
        return Err("couldn't set the remote-console password in dedicated.toml: set [rcon] password by hand".into());
    }
    Ok((out, new.to_string()))
}

#[derive(Debug, PartialEq)]
pub struct Found {
    pub name: String,
    pub port: u16, // RCON: rcon_port, else the game port
    pub game_port: u16,
    pub password: String, // its own rcon_password, if it has one
    pub hidden: bool,     // not listed in the server browser
}

/// The enabled [[server]]s of a dedicated.toml.
pub fn servers_of(text: &str) -> Vec<Found> {
    let Ok(t) = text.parse::<toml::Table>() else { return vec![] };
    let hidden = t.get("defaults").and_then(|d| d.get("hidden")).and_then(toml::Value::as_bool).unwrap_or(false);
    let port = |v: Option<&toml::Value>| v.and_then(toml::Value::as_integer).filter(|p| (1..65536).contains(p)).map(|p| p as u16);
    let s = |v: Option<&toml::Value>| v.and_then(toml::Value::as_str).unwrap_or_default().to_string();
    t.get("server")
        .and_then(toml::Value::as_array)
        .into_iter()
        .flatten()
        .filter(|s| s.get("enabled").and_then(toml::Value::as_bool).unwrap_or(true))
        .filter_map(|x| {
            let game = port(x.get("port"))?;
            Some(Found { name: s(x.get("name")), port: port(x.get("rcon_port")).unwrap_or(game), game_port: game, password: s(x.get("rcon_password")),
                hidden: x.get("hidden").and_then(toml::Value::as_bool).unwrap_or(hidden) })
        })
        .collect()
}

/// What the operator chose on the setup screen for a server this app is about to create. Applied to a fresh
/// dedicated.toml only: a server folder that's already there is started as it is.
#[derive(Deserialize, Default, Clone, Debug)]
pub struct Settings {
    pub name: String,
    pub host: String,     // "hosted by", shown under the name in the server browser
    pub password: String, // to join; empty lets anyone in
    pub max_players: u32, // 0 keeps the default
    pub hidden: bool,     // not listed: players join by address
    pub internet: bool,   // UPnP: ask the router to forward the ports
    pub auto_update: bool,
    pub playlist: String, // a playlist .json picked on this PC; empty keeps the server's own default rotation
}

impl Settings {
    /// Checked before anything is downloaded, so a mistake costs nothing.
    pub fn check(&self) -> Result<(), String> {
        let plain = |s: &str| !s.chars().any(char::is_control);
        if self.name.trim().chars().count() > 48 {
            return Err("A server name can be at most 48 characters.".into());
        }
        if self.max_players > 63 {
            return Err("A server holds at most 63 players.".into());
        }
        if ![&self.name, &self.host, &self.password].into_iter().all(|s| plain(s)) {
            return Err("Names and passwords can't contain control characters.".into());
        }
        self.chosen_playlist().map(drop)
    }

    /// The picked playlist as the server folder keeps it: its path there and its JSON, held to Reclaimer's rules.
    pub fn chosen_playlist(&self) -> Result<Option<(String, String)>, String> {
        if self.playlist.trim().is_empty() {
            return Ok(None);
        }
        let text = std::fs::read_to_string(&self.playlist).map_err(|e| format!("The playlist file didn't open: {e}"))?;
        let json = serde_json::from_str(&text).map_err(|e| format!("{} isn't a playlist file: {e}", self.playlist))?;
        playlist_file(&json).map(Some)
    }

    /// Puts the picked playlist in the server folder's playlists/.
    pub fn place_playlist(&self, folder: &Path) -> Result<(), String> {
        let Some((path, text)) = self.chosen_playlist()? else { return Ok(()) };
        let dest = folder.join(&path);
        std::fs::create_dir_all(dest.parent().unwrap_or(folder)).map_err(|e| e.to_string())?;
        write_atomic(&dest, &text, false).map_err(|e| format!("couldn't save the playlist in {}: {e}", dest.display()))
    }
}

/// dedicated.toml with the settings in it and the comments left alone. `dedicated init` writes two servers; a custom
/// one is a single server, so the first enabled [[server]] is kept and any other turned off (not deleted).
pub fn apply_settings(text: &str, s: &Settings) -> Result<String, String> {
    use toml_edit::{value, DocumentMut, Item};
    s.check()?;
    let mut doc: DocumentMut = text.parse().map_err(|e| format!("dedicated.toml doesn't read: {e}"))?;
    if s.internet {
        doc["upnp"] = value(true);
    }
    if s.auto_update {
        doc["auto_update"] = value(true);
    }
    if !s.host.trim().is_empty() {
        doc["host_name"] = value(s.host.trim());
    }
    let list = doc.get_mut("server").and_then(Item::as_array_of_tables_mut).ok_or("dedicated.toml has no [[server]]")?;
    let mut kept = false;
    for t in list.iter_mut() {
        if t.get("enabled").and_then(Item::as_bool) == Some(false) {
            continue;
        }
        if kept {
            t["enabled"] = value(false);
            continue;
        }
        kept = true;
        if !s.name.trim().is_empty() {
            t["name"] = value(s.name.trim());
        }
        if !s.password.is_empty() {
            t["password"] = value(&s.password);
        }
        if s.max_players > 0 {
            t["max_players"] = value(i64::from(s.max_players));
        }
        if s.hidden {
            t["hidden"] = value(true);
        }
        if let Some((path, _)) = s.chosen_playlist()? {
            for other in ["map", "game", "base_map", "variant", "campaign"] {
                t.remove(other); // a server plays from one source
            }
            t["playlist"] = value(path);
        }
    }
    if !kept {
        return Err("dedicated.toml has no enabled [[server]]".into());
    }
    Ok(doc.to_string())
}

/// A playlist made in the console: its path from the server folder and its JSON, held to Reclaimer's rules.
fn slug(name: &str) -> Result<String, String> {
    let s = name.trim().to_lowercase().split(|c: char| !c.is_ascii_alphanumeric()).filter(|w| !w.is_empty()).collect::<Vec<_>>().join("-");
    if s.is_empty() { Err("Name it with a letter or number in it.".into()) } else { Ok(s) }
}

const PLAYLIST_KEYS: &[&str] = &["version", "name", "vote_seconds", "games"];
const GAME_KEYS: &[&str] = &["map", "game", "name", "weight", "options", "world", "rules", "anti_cheat", "voice_team_only"];

/// A playlist in the shape the dedicated server reads (version 2), or why it can't be saved. Unknown keys are refused,
/// the way the server's own import refuses them; a weight of 1 and empty values are left out. Whether each map and game
/// is installed is the server's to say. From ibigs/oni-rcon's check_playlist, by IBIGS.
pub fn check_playlist(p: &serde_json::Value) -> Result<serde_json::Value, String> {
    use serde_json::{json, Value};
    let o = p.as_object().ok_or("A playlist is a JSON object with a name and a list of games.")?;
    if let Some(k) = o.keys().find(|k| !PLAYLIST_KEYS.contains(&k.as_str())) {
        return Err(format!("Unknown playlist setting: {k}."));
    }
    if o.get("version").is_some_and(|v| v.as_i64() != Some(2)) {
        return Err("Only version 2 playlists are supported.".into());
    }
    let name = o.get("name").and_then(Value::as_str).unwrap_or("").trim();
    if name.is_empty() {
        return Err("Give the playlist a name.".into());
    }
    let vote = o.get("vote_seconds").map_or(Some(20), Value::as_i64).filter(|n| (5..=120).contains(n)).ok_or("The vote lasts 5 to 120 seconds.")?;
    let games = o.get("games").and_then(Value::as_array).map_or(&[][..], Vec::as_slice);
    if games.len() < 3 {
        return Err(format!("A playlist needs at least 3 games (it has {}): players vote on three different games each time.", games.len()));
    }
    if games.len() > 128 {
        return Err(format!("A playlist holds at most 128 games; this one has {}.", games.len()));
    }
    let mut seen = std::collections::HashSet::new();
    let mut out = vec![];
    for (i, g) in games.iter().enumerate() {
        let n = i + 1;
        let g = g.as_object().ok_or_else(|| format!("Game {n} isn't a map and game type."))?;
        if let Some(k) = g.keys().find(|k| !GAME_KEYS.contains(&k.as_str())) {
            return Err(format!("Game {n} has an unknown setting: {k}."));
        }
        let text = |k: &str| g.get(k).and_then(Value::as_str).unwrap_or("").trim().to_string();
        let (map, game) = (text("map"), text("game"));
        if map.is_empty() || game.is_empty() {
            return Err(format!("Game {n} needs both a map and a game type."));
        }
        if !g.get("weight").map_or(Some(1), Value::as_i64).is_some_and(|w| (1..=100).contains(&w)) {
            return Err(format!("Game {n}'s weight must be a whole number from 1 to 100."));
        }
        if !seen.insert((map.clone(), game.clone())) {
            return Err(format!("{game} on {map} is in it twice: give it a higher weight instead."));
        }
        let kept: serde_json::Map<String, Value> =
            g.iter().filter(|(k, v)| !(k.as_str() == "weight" && v.as_i64() == Some(1)) && !v.is_null() && v.as_str() != Some("")).map(|(k, v)| (k.clone(), v.clone())).collect();
        out.push(Value::Object(kept));
    }
    Ok(json!({"version": 2, "name": name, "vote_seconds": vote, "games": out}))
}

/// A playlist made in the console: its path from the server folder and its JSON, held to Reclaimer's rules.
pub fn playlist_file(p: &serde_json::Value) -> Result<(String, String), String> {
    let checked = check_playlist(p)?;
    let slug = slug(checked["name"].as_str().unwrap_or_default())?;
    Ok((format!("playlists/{slug}.playlist.json"), serde_json::to_string_pretty(&checked).unwrap() + "\n"))
}

/// A playlist file the console made, and so may rewrite: playlists/<name>.playlist.json, nothing deeper.
pub fn console_playlist(path: &str) -> Result<String, String> {
    let p = crate::install::safe_path(path)?;
    match p.strip_prefix("playlists/") {
        Some(f) if !f.contains('/') && f.ends_with(".playlist.json") => Ok(p),
        _ => Err(format!("{path} isn't a playlist made in this console.")),
    }
}

/// The native game types a game type file builds on, as it names them.
pub const BASE_MODES: &[&str] = &["Slayer", "CaptureTheFlag", "Oddball", "KingOfTheHill", "Vip", "Juggernaut", "Territories", "Assault", "Infection", "EscalationSlayer"];
/// The game options NEW GAME TYPE sets, in the ranges Reclaimer 0.9.13's mode-check holds them to.
const GAME_OPTIONS: &[(&str, i64, i64)] = &[("rounds.teams", 0, 1), ("rounds.minutes", 0, 60), ("rounds.limit", 1, 10),
    ("respawn.lives", 0, 50), ("respawn.seconds", 0, 240), ("player.shields", 0, 5)];

/// A game type made in the console: its path in the content folder and its JSON. Options left out are the mode's own.
pub fn new_game_type(name: &str, mode: &str, score_to_win: Option<i64>, options: &serde_json::Map<String, serde_json::Value>) -> Result<(String, String), String> {
    let name = name.trim();
    if name.chars().count() > 48 || name.chars().any(char::is_control) {
        return Err("A game type's name is up to 48 characters.".into());
    }
    let slug = slug(name)?;
    if !BASE_MODES.contains(&mode) {
        return Err(format!("{mode} isn't a game type one can be built on."));
    }
    let mut rules = serde_json::Map::new();
    if let Some(n) = score_to_win {
        if !(0..=32767).contains(&n) {
            return Err("Score to win is 0 to 32767.".into());
        }
        rules.insert("score_to_win".into(), n.into());
    }
    for (k, v) in options {
        let (_, lo, hi) = GAME_OPTIONS.iter().find(|(o, ..)| o == k).ok_or_else(|| format!("{k} can't be set from here"))?;
        if !v.as_i64().is_some_and(|n| (*lo..=*hi).contains(&n)) {
            return Err(format!("{k} is {lo} to {hi}."));
        }
    }
    // an empty script: the file is then a custom mode Reclaimer's mode-check passes
    let json = serde_json::json!({"version": 1, "name": name, "mode": mode, "rules": rules, "options": options,
        "script": {"version": 1, "name": name, "initial": []}});
    Ok((format!("Game Modes/{slug}.json"), serde_json::to_string_pretty(&json).unwrap() + "
"))
}

/// `key` set on the [[server]] of a dedicated.toml that answers RCON on `rcon_port`, the rest left as it was.
pub fn set_server_key(text: &str, rcon_port: u16, key: &str, v: toml_edit::Value) -> Result<String, String> {
    edit_server(text, rcon_port, |t| {
        if key == "playlist" {
            for other in ["map", "game", "base_map", "variant", "campaign"] {
                t.remove(other); // a server plays from one source
            }
        }
        t[key] = toml_edit::Item::Value(v);
        Ok(())
    })
}

/// `id` added to, or taken off, the admins of the [[server]] on `rcon_port` only: Reclaimer has no other tiers.
pub fn set_server_admin(text: &str, rcon_port: u16, id: &str, add: bool) -> Result<String, String> {
    let id = id.trim();
    if id.is_empty() || id.starts_with('#') || id.chars().any(char::is_control) {
        return Err("That's not a player ID.".into());
    }
    edit_server(text, rcon_port, |t| {
        let a = t.entry("admins").or_insert(toml_edit::value(toml_edit::Array::new())).as_array_mut().ok_or("admins in dedicated.toml isn't a list")?;
        let at = a.iter().position(|v| v.as_str() == Some(id));
        match (add, at) {
            (true, Some(_)) => Err(format!("{id} is already an admin of this server.")),
            (true, None) => Ok(a.push(id)),
            (false, Some(i)) => Ok(a.remove(i)).map(drop),
            (false, None) => Err(format!("{id} isn't in this server's admins: it may be in the top-level admins of dedicated.toml, for every server.")),
        }
    })
}

fn edit_server(text: &str, rcon_port: u16, edit: impl FnOnce(&mut toml_edit::Table) -> Result<(), String>) -> Result<String, String> {
    use toml_edit::{DocumentMut, Item, Table};
    let mut doc: DocumentMut = text.parse().map_err(|e| format!("dedicated.toml doesn't read: {e}"))?;
    let list = doc.get_mut("server").and_then(Item::as_array_of_tables_mut).ok_or("dedicated.toml has no [[server]]")?;
    let port = |t: &Table, k: &str| t.get(k).and_then(Item::as_integer);
    let t = list
        .iter_mut()
        .find(|t| port(t, "rcon_port").or(port(t, "port")) == Some(i64::from(rcon_port)))
        .ok_or_else(|| format!("dedicated.toml has no [[server]] on RCON port {rcon_port}"))?;
    edit(t)?;
    Ok(doc.to_string())
}

fn new_password() -> String {
    use rand::Rng;
    rand::rng().sample_iter(rand::distr::Alphanumeric).take(24).map(char::from).collect()
}

// --- local help ----------------------------------------------------------------------------------------------------
async fn listening(port: u16) -> bool {
    matches!(tokio::time::timeout(Duration::from_millis(500), tokio::net::TcpStream::connect(("127.0.0.1", port))).await, Ok(Ok(_)))
}

/// Run a command, passing each line it prints on as it comes; an error with the last few if it fails.
async fn stream(mut c: tokio::process::Command, say: Say<'_>, what: &str) -> Result<(), String> {
    use std::process::Stdio;
    c.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = c.spawn().map_err(|e| format!("couldn't run {what}: {e}"))?;
    let mut out = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut err = BufReader::new(child.stderr.take().unwrap()).lines();
    let (mut out_done, mut err_done, mut tail) = (false, false, Vec::<String>::new());
    while !(out_done && err_done) {
        let line = tokio::select! {
            l = out.next_line(), if !out_done => l.ok().flatten().or_else(|| { out_done = true; None }),
            l = err.next_line(), if !err_done => l.ok().flatten().or_else(|| { err_done = true; None }),
        };
        if let Some(l) = line.map(|l| l.trim_end().to_string()).filter(|l| !l.is_empty()) {
            say(&l);
            tail.push(l);
            if tail.len() > 25 { // `dedicated check` lists every problem at once, so keep room for them all
                tail.remove(0);
            }
        }
    }
    let st = child.wait().await.map_err(|e| format!("{what} failed: {e}"))?;
    if st.success() {
        Ok(())
    } else {
        Err(format!("{what} failed (exit {}){}", st.code().unwrap_or(-1), if tail.is_empty() { String::new() } else { format!(":\n{}", tail.join("\n")) }))
    }
}

fn client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder().user_agent(format!("oni-rcon/{}", env!("CARGO_PKG_VERSION"))).build().map_err(|e| e.to_string())
}

async fn fetch(c: &reqwest::Client, url: &str, secs: u64) -> Result<Vec<u8>, String> {
    let r = c.get(url).timeout(Duration::from_secs(secs)).send().await.and_then(|r| r.error_for_status()).map_err(|e| e.to_string())?;
    Ok(r.bytes().await.map_err(|e| e.to_string())?.to_vec())
}

/// The SHA-256 SHA256SUMS.txt gives for `name`.
fn sum_for(sums: &str, name: &str) -> Option<String> {
    sums.lines().find_map(|l| {
        let (h, n) = l.trim().split_once(' ')?;
        (n.trim().trim_start_matches('*') == name).then(|| h.to_lowercase())
    })
}

/// The newest dedicated server program from the official releases, checked against SHA256SUMS.txt. Its version.
async fn download_server(exe: &Path, say: Say<'_>) -> Result<String, String> {
    static EXE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^project-reclaimer-dedicated-v[\d.]+\.exe$").unwrap());
    let c = client()?;
    let rel: serde_json::Value = serde_json::from_slice(&fetch(&c, RELEASES, 15).await.map_err(|e| format!("couldn't reach GitHub for Project Reclaimer's releases: {e}"))?)
        .map_err(|e| format!("couldn't read Project Reclaimer's releases: {e}"))?;
    let assets = rel["assets"].as_array().cloned().unwrap_or_default();
    let asset = |pick: &dyn Fn(&str) -> bool| assets.iter().find(|a| pick(a["name"].as_str().unwrap_or_default())).cloned();
    let a = asset(&|n| EXE.is_match(n)).ok_or("the newest Project Reclaimer release has no dedicated server download")?;
    let (name, tag) = (a["name"].as_str().unwrap_or_default(), rel["tag_name"].as_str().unwrap_or_default());
    say(&format!("Downloading Project Reclaimer {tag}, the dedicated server ({:.0} MB)…", a["size"].as_f64().unwrap_or(0.0) / 1e6));
    let data = fetch(&c, a["browser_download_url"].as_str().unwrap_or_default(), 600).await.map_err(|e| format!("the download failed: {e}"))?;
    let sums = match asset(&|n| n == "SHA256SUMS.txt") {
        Some(s) => String::from_utf8_lossy(&fetch(&c, s["browser_download_url"].as_str().unwrap_or_default(), 15).await.map_err(|e| format!("the download failed: {e}"))?).to_string(),
        None => String::new(),
    };
    let want = sum_for(&sums, name).or_else(|| crate::forge::sha_hex(a["digest"].as_str().unwrap_or_default()));
    if want.is_none() || want != Some(hex::encode(Sha256::digest(&data))) {
        return Err("the download didn't match its published SHA-256, so it wasn't kept. Try again.".into());
    }
    std::fs::create_dir_all(exe.parent().unwrap()).map_err(|e| e.to_string())?;
    let part = exe.with_extension("exe.part");
    std::fs::write(&part, &data).and_then(|_| std::fs::rename(&part, exe)).map_err(|e| format!("couldn't save {}: {e}", exe.display()))?;
    let _ = std::fs::write(exe.with_file_name("project-reclaimer.version"), format!("{tag}\n"));
    say(&format!("Checked against the published SHA-256 and saved as {}.", exe.display()));
    Ok(tag.to_string())
}

async fn wait_for(found: &[Found], say: Say<'_>, secs: u64, mut alive: impl FnMut() -> bool) -> Result<(), String> {
    let mut left: Vec<u16> = found.iter().map(|f| f.port).collect();
    say(&format!("Waiting for {} remote console{} to open…", left.len(), if left.len() > 1 { "s" } else { "" }));
    let end = Instant::now() + Duration::from_secs(secs);
    while !left.is_empty() && Instant::now() < end {
        let mut still = vec![];
        for p in left {
            if !listening(p).await {
                still.push(p);
            }
        }
        left = still;
        if !left.is_empty() && !alive() {
            return Err("the servers stopped as they started: run `project-reclaimer dedicated check` in the server folder for what's wrong".into());
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    if left.is_empty() {
        Ok(())
    } else {
        Err(format!("no remote console on port {} after {secs}s: the servers' window says why", left.iter().map(u16::to_string).collect::<Vec<_>>().join(", ")))
    }
}

// --- hosting -------------------------------------------------------------------------------------------------------
/// Where the server lives: $ONI_RCON_HOST_DIR, else `reclaimer` at the root of the drive MCC is on (so init
/// hard-links the maps instead of copying gigabytes), or in your home folder on Linux.
fn host_folder(game: Option<&Path>) -> PathBuf {
    if let Some(d) = std::env::var_os("ONI_RCON_HOST_DIR") {
        return d.into();
    }
    if cfg!(windows) {
        game.and_then(|g| g.ancestors().last().map(Path::to_path_buf)).unwrap_or_else(|| "C:\\".into()).join("reclaimer")
    } else {
        dirs::home_dir().unwrap_or_default().join("reclaimer")
    }
}

fn toml_of(folder: &Path) -> PathBuf {
    folder.join(if cfg!(windows) { "dedicated.toml" } else { "server/dedicated.toml" })
}

/// What the setup screen shows before anything runs: where the server goes, whether one is there already (it's
/// started as it is, and its settings are the file's), and what's missing for making a new one.
pub fn status() -> serde_json::Value {
    let game = find_game();
    let folder = host_folder(game.as_deref());
    let text = std::fs::read_to_string(toml_of(&folder)).ok();
    let problem = if cfg!(windows) {
        if text.is_none() && game.is_none() { NO_GAME } else { "" }
    } else if cfg!(target_os = "linux") {
        if which("docker").is_none() { NO_DOCKER } else { "" }
    } else {
        NO_SYSTEM
    };
    let servers: Vec<String> = text.as_deref().map(servers_of).unwrap_or_default().into_iter().map(|f| f.name).collect();
    serde_json::json!({"folder": folder.display().to_string(), "exists": text.is_some(), "servers": servers, "problem": problem})
}

/// Set up and start dedicated servers on this computer: the servers, with what to tell the operator.
pub async fn host_here(settings: Option<&Settings>, say: Say<'_>) -> Result<(Vec<Server>, Vec<String>), String> {
    if let Some(s) = settings {
        s.check()?; // before anything is downloaded
    }
    let game = find_game();
    let folder = host_folder(game.as_deref());
    if cfg!(windows) {
        host_windows(&folder, game.as_deref(), settings, say).await
    } else if cfg!(target_os = "linux") {
        host_docker(&folder, game.as_deref(), settings, say).await
    } else {
        Err(NO_SYSTEM.into())
    }
}

/// How players get in, once the servers are up: found in the browser, or by address when hidden.
fn join_note(text: &str, found: &[Found]) -> String {
    let t = text.parse::<toml::Table>().unwrap_or_default();
    let upnp = t.get("upnp").and_then(toml::Value::as_bool).unwrap_or(false);
    let master = t.get("master").and_then(|m| m.get("port")).and_then(toml::Value::as_integer).and_then(|p| u16::try_from(p).ok()).unwrap_or(49175);
    let ports: Vec<u16> = found.iter().map(|f| f.game_port).collect();
    let names = found.iter().map(|f| format!("\"{}\"", f.name)).collect::<Vec<_>>().join(", ");
    let how = if found.iter().any(|f| f.hidden) {
        let addr = t.get("public_address").and_then(toml::Value::as_str).filter(|a| !a.is_empty()).unwrap_or("your public IP");
        format!("{names} is hidden: players join by address, {addr}:{}.", ports.first().copied().unwrap_or_default())
    } else {
        format!("Players find {names} in Halo 3's server browser.")
    };
    format!("{how} {}", open_ports_note(&ports, master, upnp))
}

fn read(p: &Path) -> Result<String, String> {
    std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))
}

fn found_servers(found: &[Found], pw: &str, content: &Path, restart: &str) -> Vec<Server> {
    found
        .iter()
        .map(|f| {
            let mut s = Server::new("127.0.0.1", f.port);
            s.name = f.name.clone();
            s.password = if f.password.is_empty() { pw.into() } else { f.password.clone() };
            s.content_dir = content.display().to_string();
            s.restart_cmd = restart.into();
            s
        })
        .collect()
}

pub async fn host_windows(folder: &Path, game: Option<&Path>, settings: Option<&Settings>, say: Say<'_>) -> Result<(Vec<Server>, Vec<String>), String> {
    let (exe, toml) = (folder.join("project-reclaimer.exe"), folder.join("dedicated.toml"));
    if !toml.is_file() && game.is_none() {
        return Err(format!("{NO_GAME} Or make the server folder by hand (docs/hosting.md)."));
    }
    let fresh = !toml.is_file(); // settings are for a server made now; one that's there is started as it is
    if !exe.is_file() {
        download_server(&exe, say).await?;
    }
    if fresh {
        let game = game.unwrap();
        say(&format!("Making the server folder {} from {}…", folder.display(), game.display()));
        let mut c = command(&exe.display().to_string());
        c.args(["dedicated", "init"]).arg(folder).arg("--from").arg(game);
        stream(c, say, "dedicated init").await?;
    }
    let disk = read(&toml)?;
    let base = match settings {
        Some(s) if fresh => {
            s.place_playlist(folder)?;
            apply_settings(&disk, s)?
        }
        _ => disk.clone(),
    };
    let (new, pw) = ensure_rcon_password(&base, &new_password())?;
    if new != disk {
        write_atomic(&toml, &new, false).map_err(|e| e.to_string())?;
        if base != disk {
            say("Saved your settings in dedicated.toml.");
        }
        if new != base {
            say("Turned the remote console on, with a new password in dedicated.toml under [rcon].");
        }
    }
    let found = servers_of(&new);
    if found.is_empty() {
        return Err(format!("{} has no enabled [[server]]", toml.display()));
    }
    let mut up = false;
    for f in &found {
        up |= listening(f.port).await;
    }
    if up && new != disk {
        return Err("the servers are running from before the password was set: stop them (Ctrl+C in their window) and try again".into());
    }
    if !up {
        say("Checking the game files, settings and ports (dedicated check)…");
        let mut c = command(&exe.display().to_string());
        c.args(["dedicated", "check"]).current_dir(folder);
        stream(c, say, "dedicated check").await.map_err(|e| {
            format!("{e}\nYour settings are saved. Fix what it names (a map or game type the server doesn't have goes in the server's content folder; F6 in the console installs them), then press START IT.")
        })?;
        say("Starting the servers in a window of their own: Ctrl+C there stops them. If Windows asks, allow Project Reclaimer through the firewall.");
        let mut c = std::process::Command::new(&exe);
        c.arg("dedicated").current_dir(folder);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            c.creation_flags(0x10); // CREATE_NEW_CONSOLE: theirs to watch and stop, and it outlives this window
        }
        let mut child = c.spawn().map_err(|e| format!("couldn't start the servers: {e}"))?;
        wait_for(&found, say, 180, || matches!(child.try_wait(), Ok(None))).await?;
    }
    Ok((found_servers(&found, &pw, &folder.join("content"), &windows_restart(&exe, folder)), vec![join_note(&new, &found)]))
}

// ponytail: one process runs every server in the folder, so restarting one restarts them all
fn windows_restart(exe: &Path, folder: &Path) -> String {
    let q = |p: &Path| p.display().to_string().replace('\'', "''");
    format!(
        "cd /d \"{f}\" && \"{e}\" dedicated check >nul 2>&1 || ( echo NOT RESTARTED: dedicated check found problems in the server folder, fix them in dedicated.toml and the playlists: & \"{e}\" dedicated check 2>&1 & exit /b 1 ) & taskkill /F /T /IM project-reclaimer.exe >nul 2>&1 & powershell -NoProfile -Command \"Start-Sleep 2; Start-Process -FilePath '{}' -ArgumentList dedicated -WorkingDirectory '{}'\"",
        q(exe),
        q(folder),
        e = exe.display(),
        f = folder.display(),
    )
}

/// The restart for a server hosted on this computer whose config has no restart_cmd: one added by hand, or before
/// HOST ONE HERE wrote it. Found only when the server's folder lists that port; empty otherwise.
pub fn default_restart(s: &Server) -> String {
    if !cfg!(windows) || !s.ssh.is_empty() || !s.url.is_empty() || !["127.0.0.1", "localhost", "::1"].contains(&s.host.as_str()) {
        return String::new();
    }
    let folder = match Path::new(&s.content_dir).parent().filter(|p| !p.as_os_str().is_empty()) {
        Some(p) => p.to_path_buf(),
        None => host_folder(find_game().as_deref()),
    };
    let exe = folder.join("project-reclaimer.exe");
    let listed = std::fs::read_to_string(folder.join("dedicated.toml")).is_ok_and(|t| servers_of(&t).iter().any(|f| f.port == s.port));
    if listed && exe.is_file() { windows_restart(&exe, &folder) } else { String::new() }
}

fn read_env(text: &str, key: &str) -> String {
    text.lines()
        .find_map(|l| l.split_once('=').filter(|(k, _)| k.trim() == key).map(|(_, v)| v.trim().trim_matches(['"', '\'']).to_string()))
        .unwrap_or_default()
}

pub async fn host_docker(folder: &Path, game: Option<&Path>, settings: Option<&Settings>, say: Say<'_>) -> Result<(Vec<Server>, Vec<String>), String> {
    let docker = which("docker").ok_or(NO_DOCKER)?;
    let server = folder.join("server");
    let toml = server.join("dedicated.toml");
    let custom = settings.filter(|_| !toml.is_file()); // settings are for a server made now
    for sub in ["", "game", "content", "playlists"] {
        // made here, as you: a folder Docker makes belongs to root
        std::fs::create_dir_all(server.join(sub)).map_err(|e| e.to_string())?;
    }
    if !folder.join("compose.yaml").is_file() {
        let data = fetch(&client()?, COMPOSE, 30).await.map_err(|e| format!("couldn't download the official compose.yaml: {e}"))?;
        std::fs::write(folder.join("compose.yaml"), data).map_err(|e| e.to_string())?;
        say(&format!("Saved the official compose.yaml in {}.", folder.display()));
    }
    let env_path = folder.join(".env");
    let env = std::fs::read_to_string(&env_path).unwrap_or_default();
    let mut add = vec![];
    let mut pw = read_env(&env, "RECLAIMER_DEDICATED_RCON_PASSWORD");
    if pw.is_empty() {
        pw = new_password();
        add.push(format!("RECLAIMER_DEDICATED_RCON_PASSWORD={pw}"));
    }
    if !server.join("game/halo3/halo3.dll").is_file() && read_env(&env, "GAME_DIR").is_empty() {
        let game = game.ok_or_else(|| format!("no game files: copy Halo 3's from your MCC install into {} (see docs/hosting.md)", server.join("game").display()))?;
        add.push(format!("GAME_DIR={}", game.display()));
    }
    if !add.is_empty() {
        let sep = if !env.is_empty() && !env.ends_with('\n') { "\n" } else { "" };
        write_atomic(&env_path, &format!("{env}{sep}{}\n", add.join("\n")), true).map_err(|e| e.to_string())?;
        say(&format!("Wrote {} to {}.", add.iter().map(|a| a.split('=').next().unwrap()).collect::<Vec<_>>().join(", "), env_path.display()));
    }
    say("Starting the containers (the first start downloads the image)…");
    let mut c = command(&docker.display().to_string());
    c.args(["compose", "up", "-d"]).current_dir(folder);
    if custom.is_some() {
        c.arg("server-1"); // one custom server: the official compose file has a service for each of its two
    }
    stream(c, say, "docker compose up").await?;
    let end = Instant::now() + Duration::from_secs(600);
    while !toml.is_file() {
        if Instant::now() > end {
            return Err(format!("no {} after ten minutes: docker compose logs (in {}) says why", toml.display(), folder.display()));
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    tokio::time::sleep(Duration::from_secs(1)).await; // written whole by now
    let mut text = read(&toml)?;
    if let Some(s) = custom {
        s.place_playlist(&server)?;
        text = apply_settings(&text, s)?;
        write_atomic(&toml, &text, false).map_err(|e| format!("couldn't save your settings in {}: {e}", toml.display()))?;
        say("Saved your settings; restarting the server with them…");
        let mut c = command(&docker.display().to_string());
        c.args(["compose", "restart", "server-1"]).current_dir(folder);
        stream(c, say, "docker compose restart").await?;
    }
    let found = servers_of(&text);
    if found.is_empty() {
        return Err(format!("{} has no enabled [[server]]", toml.display()));
    }
    wait_for(&found, say, 300, || true).await?;
    let restart = format!("cd {} && {} compose restart", sh_quote(&folder.display().to_string()), sh_quote(&docker.display().to_string()));
    Ok((found_servers(&found, &pw, &server.join("content"), &restart), vec![join_note(&text, &found)]))
}

// --- finding the servers on a game box -----------------------------------------------------------------------------
#[derive(Default, Debug)]
pub struct Container {
    pub name: String,
    pub mount: String,
    pub only: String,
    pub address: String,
    pub envpassword: bool,
}

/// What DISCOVER saw: the docker command, each Reclaimer container, and each server folder's dedicated.toml.
#[derive(Default, Debug)]
pub struct Box_ {
    pub docker: String,
    pub no_docker: bool,
    pub containers: Vec<Container>,
    pub folders: Vec<(String, String)>,
}

impl Box_ {
    fn folder(&self, dir: &str) -> Option<&str> {
        self.folders.iter().find(|(d, _)| d == dir).map(|(_, t)| t.as_str())
    }
}

pub fn read_discovery(out: &str) -> Box_ {
    let mut b = Box_::default();
    let mut toml: Option<(String, Vec<&str>)> = None;
    for line in out.lines() {
        if let Some((dir, lines)) = toml.as_mut() {
            if line == "@@end" {
                let text = lines.join("\n") + "\n";
                if b.folder(dir).is_none() {
                    b.folders.push((dir.clone(), text));
                }
                toml = None;
            } else {
                lines.push(line);
            }
            continue;
        }
        let (tag, rest) = line.split_once(' ').unwrap_or((line, ""));
        let cur = b.containers.last_mut();
        match (tag, cur) {
            ("@@docker", _) => b.docker = rest.into(),
            ("@@nodocker", _) => b.no_docker = true,
            ("@@container", _) => b.containers.push(Container { name: rest.into(), ..Default::default() }),
            ("@@mount", Some(c)) => c.mount = rest.into(),
            ("@@only", Some(c)) => c.only = rest.into(),
            ("@@address", Some(c)) => c.address = rest.into(),
            ("@@envpassword", Some(c)) => c.envpassword = true,
            ("@@toml", _) => toml = Some((rest.into(), vec![])),
            _ => {}
        }
    }
    b
}

/// shlex.quote: quoted only when it has to be.
fn sh_quote(s: &str) -> String {
    static SAFE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[\w@%+=:,./-]+$").unwrap());
    if SAFE.is_match(s) {
        s.into()
    } else {
        format!("'{}'", s.replace('\'', "'\"'\"'"))
    }
}

/// Reads the RCON password on the box at each start, so it's never kept on this machine.
pub fn password_command(dest: &str, folder: &str, container: &str, docker: &str) -> Vec<String> {
    let mut cmd = format!(
        r#"sed -n '/^\[rcon\]/,/^\[/s/^[[:space:]]*password[[:space:]]*=[[:space:]]*["'"'"']\(.*\)["'"'"'].*/\1/p' {}"#,
        sh_quote(&format!("{folder}/dedicated.toml"))
    );
    if !container.is_empty() {
        cmd = format!(
            "{docker} inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {} | sed -n 's/^RECLAIMER_DEDICATED_RCON_PASSWORD=//p' | grep . || {cmd}",
            sh_quote(container)
        );
    }
    let mut v: Vec<String> = vec!["ssh".into()];
    v.extend(SSH_OPTS.iter().map(|s| s.to_string()));
    v.extend([dest.to_string(), cmd]);
    v
}

/// A server found on the box, and what it takes to reach it.
pub struct Spot {
    pub server: Server,
    pub container: String,
    pub problem: String, // why it can't be added as it is
}

/// The servers to add: each container's, then those of any server folder no container runs.
pub fn spots(b: &Box_, dest: &str) -> Vec<Spot> {
    let mut out: Vec<Spot> = vec![];
    let add = |out: &mut Vec<Spot>, folder: &str, f: &Found, rcon: Option<&toml::Value>, container: &str, env_pw: bool, address: &str| {
        let addr = if address.is_empty() { rcon.and_then(|r| r.get("address")).and_then(toml::Value::as_str).unwrap_or_default() } else { address };
        let host = if ["", "0.0.0.0", "::", "127.0.0.1", "localhost"].contains(&addr) { "127.0.0.1" } else { addr };
        let mut s = Server::new(host, f.port);
        s.name = f.name.clone();
        s.ssh = dest.into();
        s.content_dir = format!("{folder}/content");
        let has_pw = rcon.and_then(|r| r.get("password")).and_then(toml::Value::as_str).is_some_and(|p| !p.is_empty());
        let problem = if !f.password.is_empty() {
            "has its own rcon_password: put it in the config as password_env or password by hand"
        } else if !(env_pw || has_pw) {
            "has its remote console off: set [rcon] password in dedicated.toml and restart it"
        } else {
            s.password_command = Cmd::Argv(password_command(dest, folder, container, &b.docker));
            ""
        };
        if !out.iter().any(|x| x.server.where_() == s.where_()) {
            // the same folder can turn up twice: as a mount and by search
            out.push(Spot { server: s, container: container.into(), problem: problem.into() });
        }
    };
    let table = |text: &str| text.parse::<toml::Table>().ok();
    for c in &b.containers {
        let Some(text) = b.folder(&c.mount) else { continue };
        let t = table(text);
        for (i, f) in servers_of(text).iter().enumerate() {
            // RECLAIMER_DEDICATED_ONLY: its position in dedicated.toml, its name, or its game port
            if c.only.is_empty() || [(i + 1).to_string(), f.name.clone(), f.game_port.to_string()].contains(&c.only) {
                add(&mut out, &c.mount, f, t.as_ref().and_then(|t| t.get("rcon")), &c.name, c.envpassword, &c.address);
            }
        }
    }
    for (folder, text) in &b.folders {
        if !b.containers.iter().any(|c| &c.mount == folder) {
            let t = table(text);
            for f in servers_of(text) {
                add(&mut out, folder, &f, t.as_ref().and_then(|t| t.get("rcon")), "", false, "");
            }
        }
    }
    out
}

async fn discover(dest: &str) -> Result<Box_, String> {
    use std::process::Stdio;
    let ssh = which("ssh").ok_or("no ssh client on PATH (Windows: Settings > System > Optional features > OpenSSH Client)")?;
    let mut c = command(&ssh.display().to_string());
    c.args(SSH_OPTS).arg(dest).arg("sh -s").stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = c.spawn().map_err(|e| format!("couldn't run ssh: {e}"))?;
    let mut stdin = child.stdin.take().unwrap();
    let script = DISCOVER.replace('\r', ""); // a Windows checkout's CRLF would break the box's sh
    tokio::spawn(async move {
        let _ = stdin.write_all(script.as_bytes()).await;
    });
    let out = tokio::time::timeout(Duration::from_secs(60), child.wait_with_output())
        .await
        .map_err(|_| format!("{dest} didn't answer in 60s"))?
        .map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&out.stdout);
    if !text.contains("@@done") {
        let err = String::from_utf8_lossy(&out.stderr);
        let why = err.trim().lines().last().map(str::to_string).unwrap_or_else(|| format!("exit {}", out.status.code().unwrap_or(-1)));
        return Err(format!("couldn't look around on {dest}: {why}. It needs SSH key sign-in (ssh-copy-id {dest})"));
    }
    Ok(read_discovery(&text))
}

/// ping_log and health_cmd for servers in containers: {port} picks its container.
pub fn docker_templates(containers: &[(u16, String)], docker: &str) -> (String, String) {
    let mut sorted = containers.to_vec();
    sorted.sort();
    let arms: String = sorted.iter().map(|(p, c)| format!("{p}) echo {};; ", sh_quote(c))).collect();
    let pick = format!("$(case {{port}} in {arms}esac)");
    let ping = format!("{docker} logs -f --since 12h \"{pick}\" 2>&1");
    let health = HEALTH.replace('\r', "").replace("{c}", &pick).replace("{docker}", docker);
    (ping, health)
}

/// Find the servers on `dest` and test each: (those that answered, what to tell the operator about the rest, the
/// ping_log and health_cmd for them when they're all in containers).
pub async fn connect_box(dest: &str, say: Say<'_>) -> Result<Hosted, String> {
    say(&format!("Looking for Project Reclaimer servers on {dest}…"));
    let b = discover(dest).await?;
    let found = spots(&b, dest);
    let mut notes = vec![];
    if b.no_docker {
        notes.push("Docker is there, but you can't use it as this user: add it to the docker group (sudo usermod -aG docker $USER) to find containers".to_string());
    }
    for c in b.containers.iter().filter(|c| b.folder(&c.mount).is_none()) {
        notes.push(format!("{}: couldn't read its server folder {}", c.name, if c.mount.is_empty() { "(no /server mount)" } else { &c.mount }));
    }
    if found.is_empty() {
        return Err(format!("no Project Reclaimer servers found on {dest}. {} Server folders are looked for under your home, /opt and /srv.", notes.join(" ")));
    }
    let mut ok: Vec<Spot> = vec![];
    for mut sp in found {
        let label = if sp.server.name.is_empty() { format!("port {}", sp.server.port) } else { sp.server.name.clone() };
        if !sp.problem.is_empty() {
            notes.push(format!("{label} {}", sp.problem));
            continue;
        }
        match config::run_command(&sp.server.password_command, "reading its RCON password").await {
            Ok(pw) if !pw.is_empty() => sp.server.password = pw,
            Ok(_) => {
                notes.push(format!("{label}: its RCON password is empty"));
                continue;
            }
            Err(e) => {
                notes.push(format!("{label}: {e}"));
                continue;
            }
        }
        let r = crate::probe(sp.server.clone()).await;
        let text = r["text"].as_str().or(r["detail"].as_str()).unwrap_or("no answer").to_string();
        let kind = if sp.container.is_empty() { "server folder".to_string() } else { format!("container {}", sp.container) };
        if r["ok"] == true {
            say(&format!("✓ {label} ({kind}): {text}"));
            if !sp.container.is_empty() {
                sp.server.restart_cmd = format!("{} restart {}", b.docker, sh_quote(&sp.container));
            }
            ok.push(sp);
        } else {
            say(&format!("✗ {label} ({kind}): {text}"));
            notes.push(format!("{label} wasn't added: {text}"));
        }
    }
    let mut templates = vec![];
    if !ok.is_empty() && ok.iter().all(|sp| !sp.container.is_empty()) {
        let (ping, health) = docker_templates(&ok.iter().map(|sp| (sp.server.port, sp.container.clone())).collect::<Vec<_>>(), &b.docker);
        templates = vec![("ping_log".into(), toml_str(&ping)), ("health_cmd".into(), format!("'''{health}'''"))];
    }
    Ok((ok.into_iter().map(|sp| sp.server).collect(), notes, templates))
}

/// Add the servers not in the config already, and ping_log and health_cmd if it has none: the ones added.
pub fn save_found(path: &Path, servers: Vec<Server>, templates: &[(String, String)], by: &str, remember: bool) -> Result<Vec<Server>, String> {
    let have: Vec<String> = config::load_config(path).map(|(_, s)| s.iter().map(Server::where_).collect()).unwrap_or_default();
    let new: Vec<Server> = servers.into_iter().filter(|s| !have.contains(&s.where_())).collect();
    if !new.is_empty() {
        config::add_servers(path, &new, by, remember).map_err(|e| format!("Couldn't save {}: {e}", path.display()))?;
    }
    for (k, v) in templates {
        config::set_top_level(path, k, v, true, false, None);
    }
    Ok(new)
}

#[cfg(test)]
mod tests {
    use super::*;

    // dedicated init's file, cut down: what the guide shows
    const TOML: &str = "game = \"game\"\n[master]\nport = 49175\n\n[rcon]\npassword = \"\"             # set one to turn on the remote console\naddress = \"127.0.0.1\"\n\n[[server]]\nname = \"Community Rotation\"\nport = 49176\npassword = \"join me\"\n\n[[server]]\nname = \"Snipers Only\"\nport = 49177\nrcon_port = 50177\n\n[[server]]\nname = \"Off\"\nport = 49178\nenabled = false\n";

    #[test]
    fn a_new_game_type_keeps_to_the_ranges() {
        let o = |v: serde_json::Value| v.as_object().unwrap().clone();
        let (path, json) = new_game_type("Team Swords", "Slayer", Some(50), &o(serde_json::json!({"rounds.teams": 1, "rounds.minutes": 12}))).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!((path.as_str(), &v["rules"]["score_to_win"], &v["options"]["rounds.minutes"], &v["script"]["initial"]),
            ("Game Modes/team-swords.json", &50.into(), &12.into(), &serde_json::json!([])));
        assert!(new_game_type("x", "Slayer", None, &o(serde_json::json!({"rounds.minutes": 61}))).is_err());
        assert!(new_game_type("x", "Slayer", None, &o(serde_json::json!({"player.health": 1}))).is_err());
        assert!(new_game_type("x", "Forge", None, &serde_json::Map::new()).is_err());
        assert!(new_game_type(&"y".repeat(49), "Slayer", None, &serde_json::Map::new()).is_err());
    }

    #[test]
    fn a_new_playlist_keeps_the_rules() {
        let pl = |n: &str, games: serde_json::Value| serde_json::json!({"name": n, "vote_seconds": 25, "games": games});
        let g = |m: &str| serde_json::json!({"map": m, "game": "Slayer"});
        let (path, json) = playlist_file(&pl(" Friday Night! ", serde_json::json!([g("Guardian"), g("Valhalla"), g("The Pit")]))).unwrap();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!((path.as_str(), &v["name"], &v["games"][2]["map"]), ("playlists/friday-night.playlist.json", &"Friday Night!".into(), &"The Pit".into()));
        assert!(playlist_file(&pl("x", serde_json::json!([g("Guardian"), g("Valhalla")]))).is_err(), "three at least");
        assert!(playlist_file(&pl("x", serde_json::json!([g("Guardian"), g("Valhalla"), g("Guardian")]))).is_err(), "no game twice");
        assert!(playlist_file(&pl("!!", serde_json::json!([g("a"), g("b"), g("c")]))).is_err());
    }

    #[test]
    fn a_playlist_is_checked_in_the_servers_own_shape() {
        use serde_json::json;
        let games = json!([{"map": "Guardian", "game": "Team Slayer", "weight": 3}, {"map": "Valhalla", "game": "Slayer", "weight": 1, "name": ""}, {"map": "The Pit", "game": "Slayer"}]);
        let out = check_playlist(&json!({"name": "Team Mix", "games": games})).unwrap();
        assert_eq!(out, json!({"version": 2, "name": "Team Mix", "vote_seconds": 20, "games": [
            {"map": "Guardian", "game": "Team Slayer", "weight": 3}, {"map": "Valhalla", "game": "Slayer"}, {"map": "The Pit", "game": "Slayer"}]}));
        let many = |n: usize| (0..n).map(|i| json!({"map": format!("m{i}"), "game": "b"})).collect::<Vec<_>>();
        for (bad, words) in [
            (json!({"name": "", "games": games}), "name"),
            (json!({"name": "x", "games": []}), "at least 3"),
            (json!({"name": "x", "games": many(129)}), "at most 128"),
            (json!({"name": "x", "games": [{"map": "a"}, {"map": "b", "game": "c"}, {"map": "d", "game": "e"}]}), "both a map and a game type"),
            (json!({"name": "x", "games": [{"map": "a", "game": "b", "weight": 0}, {"map": "c", "game": "d"}, {"map": "e", "game": "f"}]}), "weight"),
            (json!({"name": "x", "games": [{"map": "a", "game": "b", "script": 1}, {"map": "c", "game": "d"}, {"map": "e", "game": "f"}]}), "unknown setting"),
            (json!({"name": "x", "games": games, "shuffle": true}), "Unknown playlist setting"),
            (json!({"name": "x", "vote_seconds": 4, "games": games}), "5 to 120"),
        ] {
            let e = check_playlist(&bad).unwrap_err();
            assert!(e.contains(words), "{e:?} should mention {words:?}");
        }
        assert!(console_playlist("playlists/a.playlist.json").is_ok());
        for bad in ["playlists/a.json", "playlists/../a.playlist.json", "Maps/a.playlist.json", "playlists/x/a.playlist.json"] {
            assert!(console_playlist(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn a_setting_lands_on_its_own_server() {
        let text = set_server_key(TOML, 50177, "max_players", 12.into()).unwrap();
        let t: toml::Table = text.parse().unwrap();
        assert_eq!((t["server"][0].get("max_players"), t["server"][1]["max_players"].as_integer()), (None, Some(12)));
        assert!(text.contains("# set one to turn on the remote console"), "comments kept");
        assert!(set_server_key(&text, 49176, "max_players", 4.into()).unwrap().contains("max_players = 4"));
        assert!(set_server_key(TOML, 1234, "max_players", 4.into()).is_err());
        let text = set_server_key(&text.replace("rcon_port = 50177", "rcon_port = 50177
map = \"Valhalla\"
game = \"Slayer\""), 50177, "playlist", "playlists/a.playlist.json".into()).unwrap();
        let t: toml::Table = text.parse().unwrap();
        assert_eq!((t["server"][1].get("map"), t["server"][1]["playlist"].as_str()), (None, Some("playlists/a.playlist.json")), "a playlist replaces map + game");
    }

    #[test]
    fn an_admin_of_one_server() {
        let text = set_server_admin(TOML, 50177, "abc123", true).unwrap();
        let text = set_server_admin(&text, 50177, "def456", true).unwrap();
        let t: toml::Table = text.parse().unwrap();
        assert_eq!((t["server"][0].get("admins"), t["server"][1]["admins"].as_array().unwrap().len()), (None, 2));
        assert!(set_server_admin(&text, 50177, "abc123", true).is_err(), "not twice");
        let text = set_server_admin(&text, 50177, "abc123", false).unwrap();
        assert!(!text.contains("abc123") && text.contains("def456"));
        assert!(set_server_admin(&text, 50177, "abc123", false).is_err());
        assert!(set_server_admin(TOML, 50177, "#3", true).is_err(), "a slot number isn't an ID");
    }

    #[test]
    fn steam_libraries() {
        let vdf = "\"libraryfolders\"\n{\n\t\"0\"\n\t{\n\t\t\"path\"\t\t\"C:\\\\Program Files (x86)\\\\Steam\"\n\t}\n\t\"1\"\n\t{\n\t\t\"path\"\t\t\"F:\\\\SteamLibrary\"\n\t}\n}\n";
        assert_eq!(library_paths(vdf), ["C:\\Program Files (x86)\\Steam", "F:\\SteamLibrary"]);
    }

    #[test]
    fn rcon_password_set_once() {
        let (text, pw) = ensure_rcon_password(TOML, "a-new-password").unwrap();
        let t: toml::Table = text.parse().unwrap();
        assert_eq!((pw.as_str(), t["rcon"]["address"].as_str()), ("a-new-password", Some("127.0.0.1")));
        assert_eq!(t["server"][0]["password"].as_str(), Some("join me")); // a server's join password is another thing
        assert_eq!(ensure_rcon_password(&text, "other").unwrap(), (text.clone(), pw));
        for without in ["game = \"game\"\n", "[rcon]\naddress = \"127.0.0.1\"\n[[server]]\nport = 1\n"] {
            assert_eq!(rcon_password(&ensure_rcon_password(without, "pw-12345678").unwrap().0).as_deref(), Some("pw-12345678"));
        }
        assert!(ensure_rcon_password("rcon.address = \"127.0.0.1\"\n", "pw-12345678").is_err());
        let f = servers_of(TOML);
        assert_eq!(f.iter().map(|f| (f.port, f.game_port)).collect::<Vec<_>>(), [(49176, 49176), (50177, 49177)]);
    }

    #[test]
    fn discovery_and_templates() {
        let masked = TOML.replace("password = \"\"", "password = \"hidden\"");
        let out = format!("@@docker docker\n@@container reclaimer-server-2-1\n@@mount /srv/rec/server\n@@only 2\n@@toml /srv/rec/server\n{masked}@@end\n@@toml /home/me/old\n{TOML}@@end\n@@toml /srv/rec/server\n{masked}@@end\n@@done\n");
        let b = read_discovery(&out);
        assert_eq!((b.docker.as_str(), b.containers[0].only.as_str(), b.folders.len()), ("docker", "2", 2));
        let found = spots(&b, "me@box");
        let got: Vec<_> = found.iter().map(|sp| (sp.server.port, sp.container.as_str(), !sp.problem.is_empty())).collect();
        assert_eq!(got, [(50177, "reclaimer-server-2-1", false), (49176, "", true)]);
        let s = &found[0].server;
        assert_eq!((s.ssh.as_str(), s.host.as_str(), s.content_dir.as_str()), ("me@box", "127.0.0.1", "/srv/rec/server/content"));
        let Cmd::Argv(cmd) = &s.password_command else { panic!() };
        assert!(cmd[0] == "ssh" && cmd.last().unwrap().contains("reclaimer-server-2-1"));
        // reads [rcon] password out of dedicated.toml
        assert!(cmd.last().unwrap().ends_with(r#"["'"'"']\(.*\)["'"'"'].*/\1/p' /srv/rec/server/dedicated.toml"#));
        assert_eq!(sh_quote("it's"), r#"'it'"'"'s'"#);
        let (ping, health) = docker_templates(&[(50177, "reclaimer-server-2-1".into())], "docker");
        assert!(ping.contains("case {port} in 50177) echo reclaimer-server-2-1;; esac") && health.contains("--since {since}"));
        assert!(!health.contains('\r') && !DISCOVER.replace('\r', "").contains('\r'));
    }

    #[test]
    fn found_servers_save_without_their_password() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("c.toml");
        let mut s = Server::new("127.0.0.1", 50177);
        s.ssh = "me@box".into();
        s.content_dir = "/srv/rec/server/content".into();
        s.password_command = Cmd::Argv(password_command("me@box", "/srv/rec/server", "reclaimer-server-2-1", "docker"));
        s.password = "read on the box".into();
        s.restart_cmd = "docker restart 'reclaimer-server-2-1'".into();
        let (ping, health) = docker_templates(&[(50177, "reclaimer-server-2-1".into())], "docker");
        let t = vec![("ping_log".to_string(), toml_str(&ping)), ("health_cmd".to_string(), format!("'''{health}'''"))];
        assert_eq!(save_found(&p, vec![s.clone()], &t, "op", true).unwrap().len(), 1);
        assert!(!std::fs::read_to_string(&p).unwrap().contains("read on the box"));
        let (by, got) = config::load_config(&p).unwrap();
        assert_eq!((by.as_str(), &got[0].password_command, got[0].content_dir.as_str()), ("op", &s.password_command, "/srv/rec/server/content"));
        assert_eq!(got[0].restart_cmd, s.restart_cmd);
        assert!(got[0].ping_log.contains("case 50177 in") && got[0].health_cmd.contains("--since {since}"));
        assert!(save_found(&p, vec![s], &[("ping_log".into(), "\"mine\"".into())], "", true).unwrap().is_empty());
        assert!(config::load_config(&p).unwrap().1[0].ping_log.contains("reclaimer-server-2-1"));
    }

    #[cfg(windows)]
    #[test]
    fn a_local_server_without_restart_cmd_gets_one_from_its_folder() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("project-reclaimer.exe"), "").unwrap();
        std::fs::write(dir.path().join("dedicated.toml"), "[[server]]\nport = 49176\n").unwrap();
        let mut s = Server::new("127.0.0.1", 49176);
        s.content_dir = dir.path().join("content").display().to_string();
        assert!(default_restart(&s).contains("taskkill") && default_restart(&s).contains("dedicated check")); // a bad toml must not take the running servers down
        s.port = 49177; // not one the folder runs
        assert!(default_restart(&s).is_empty());
        s.port = 49176;
        s.ssh = "me@box".into(); // not on this computer
        assert!(default_restart(&s).is_empty());
    }

    #[test]
    fn a_picked_playlist_replaces_the_default_rotation() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("mine.json");
        let g = |m: &str| serde_json::json!({"map": m, "game": "Slayer"});
        std::fs::write(&file, serde_json::json!({"name": "Friday Mix", "games": [g("Guardian"), g("Valhalla"), g("The Pit")]}).to_string()).unwrap();
        let s = Settings { playlist: file.display().to_string(), ..Default::default() };
        let out = apply_settings("[[server]]\nport = 1\nmap = \"Guardian\"\ngame = \"Slayer\"\n", &s).unwrap();
        let t: toml::Table = out.parse().unwrap();
        assert_eq!(t["server"][0]["playlist"].as_str(), Some("playlists/friday-mix.playlist.json"));
        assert!(t["server"][0].get("map").is_none() && t["server"][0].get("game").is_none());
        s.place_playlist(dir.path()).unwrap();
        assert!(dir.path().join("playlists/friday-mix.playlist.json").is_file());
        std::fs::write(&file, r#"{"name": "Two", "games": []}"#).unwrap();
        assert!(s.check().is_err(), "a playlist short of games is refused before anything downloads");
    }

    #[test]
    fn settings_make_one_custom_server() {
        let s = Settings { name: "Friday \"Slayer\"".into(), host: "Finn".into(), password: "letmein".into(), max_players: 8, hidden: true, internet: true, auto_update: true, ..Default::default() };
        let out = apply_settings(TOML, &s).unwrap();
        assert!(out.contains("# set one to turn on the remote console")); // the file's comments stay
        let t: toml::Table = out.parse().unwrap();
        assert_eq!((t["upnp"].as_bool(), t["auto_update"].as_bool(), t["host_name"].as_str()), (Some(true), Some(true), Some("Finn")));
        assert_eq!(servers_of(&out), [Found { name: "Friday \"Slayer\"".into(), port: 49176, game_port: 49176, password: String::new(), hidden: true }]);
        assert_eq!((t["server"][0]["password"].as_str(), t["server"][0]["max_players"].as_integer()), (Some("letmein"), Some(8)));
        assert_eq!(t["server"][1]["enabled"].as_bool(), Some(false)); // the other default server is off, not deleted
        // nothing chosen: still one server, as dedicated init named it, and nothing else changed
        let plain = apply_settings(TOML, &Settings::default()).unwrap();
        let (f, t) = (servers_of(&plain), plain.parse::<toml::Table>().unwrap());
        assert_eq!((f.len(), f[0].name.as_str(), t.contains_key("upnp"), t.contains_key("host_name")), (1, "Community Rotation", false, false));
    }

    #[test]
    fn settings_are_checked() {
        let bad = |s: Settings| s.check().is_err();
        assert!(bad(Settings { name: "x".repeat(49), ..Default::default() }));
        assert!(bad(Settings { max_players: 64, ..Default::default() }));
        assert!(bad(Settings { password: "a\nb".into(), ..Default::default() }));
        assert!(Settings { name: "x".repeat(48), max_players: 63, ..Default::default() }.check().is_ok());
        assert!(apply_settings("game = \"game\"\n", &Settings::default()).is_err());
        assert!(apply_settings("[[server]]\nport = 1\nenabled = false\n", &Settings::default()).is_err());
    }

    #[test]
    fn join_note_says_how_to_get_in() {
        let hidden = apply_settings(TOML, &Settings { hidden: true, internet: true, ..Default::default() }).unwrap();
        let n = join_note(&hidden, &servers_of(&hidden));
        assert!(n.contains("hidden") && n.contains(":49176.") && n.contains("UPnP is on"), "{n}");
        let n = join_note(TOML, &servers_of(TOML));
        assert!(n.contains("server browser") && n.contains("UDP 49176,49177 and TCP 49175") && n.contains("forwarded on your router"), "{n}");
    }

    #[test]
    fn status_sees_a_server_folder() {
        let dir = tempfile::tempdir().unwrap();
        std::env::set_var("ONI_RCON_HOST_DIR", dir.path());
        assert_eq!(status()["exists"], false);
        let toml = toml_of(dir.path());
        std::fs::create_dir_all(toml.parent().unwrap()).unwrap();
        std::fs::write(&toml, TOML).unwrap();
        let s = status();
        assert_eq!((&s["exists"], &s["servers"]), (&true.into(), &serde_json::json!(["Community Rotation", "Snipers Only"])));
        std::env::remove_var("ONI_RCON_HOST_DIR");
    }
}
