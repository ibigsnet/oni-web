//! Forge content onto a server. A version's manifest names its files; each is downloaded and checked against the
//! manifest's size and SHA-256 before any is copied, then copied into the server's content_dir under a temporary
//! name, checked again where it landed, and only then moved into place.
//!
//! A server behind SSH gets its files over its own ssh process to the same destination (key auth only). That takes a
//! POSIX shell and sha256sum on the game box. A server on this machine gets a plain copy. A server reached by a ws://
//! or wss:// link has no files oni-rcon can reach. Installing never loads anything: that's a separate step.
use crate::config::Server;
use crate::forge::{file_sha, loopback, sha_hex, MAX_ASSET};
use crate::util::{pick, pick_str};
use once_cell::sync::Lazy;
use regex::Regex;
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

static SAFE_PART: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9 ._()+\-\[\]]*$").unwrap());

#[derive(Clone, Debug, Serialize)]
pub struct File {
    pub path: String,
    pub url: String,
    pub size: i64,
    pub sha256: String,
}

/// A manifest's file name as a path inside content_dir: nothing absolute, hidden, or climbing out.
pub fn safe_path(raw: &str) -> Result<String, String> {
    let norm = raw.replace('\\', "/");
    let parts: Vec<&str> = norm.split('/').collect();
    let drive = Regex::new(r"^[A-Za-z]:").unwrap();
    if raw.is_empty() || parts.len() > 4 || drive.is_match(raw)
        || !parts.iter().all(|p| SAFE_PART.is_match(p) && !p.ends_with('.') && !p.ends_with(' '))
    {
        return Err(format!("The manifest names a file oni-rcon won't write: {raw:?}. Nothing was installed."));
    }
    Ok(parts.join("/"))
}

pub fn plan(manifest: &Value) -> Result<Vec<File>, String> {
    let assets = match pick(manifest, &["assets", "files"]) {
        Some(Value::Array(a)) if !a.is_empty() => a,
        _ => return Err("That version's manifest lists no files.".into()),
    };
    let mut out: Vec<File> = vec![];
    for a in assets {
        if !a.is_object() {
            return Err("That version's manifest isn't in a shape oni-rcon can read.".into());
        }
        let path = safe_path(&pick_str(a, &["path", "name", "filename", "file"]))?;
        let url = pick_str(a, &["url", "download_url", "href"]);
        let sha = sha_hex(&pick_str(a, &["sha256", "hash", "digest", "checksum"]))
            .ok_or_else(|| format!("The manifest gives no SHA-256 for {path}, so it can't be checked. Nothing was installed."))?;
        let size = match pick(a, &["size", "bytes", "length"]) {
            Some(Value::Number(n)) if n.is_i64() && (0..=MAX_ASSET).contains(&n.as_i64().unwrap()) => n.as_i64().unwrap(),
            _ => return Err(format!("The manifest gives {path} no size oni-rcon will accept. Nothing was installed.")),
        };
        if url.is_empty() {
            return Err(format!("The manifest gives no link for {path}."));
        }
        if out.iter().any(|f| f.path.to_lowercase() == path.to_lowercase()) {
            return Err(format!("The manifest lists {path} twice."));
        }
        out.push(File { path, url, size, sha256: sha });
    }
    Ok(out)
}

pub fn size_words(n: i64) -> String {
    if n >= 1_048_576 {
        format!("{:.1} MB", n as f64 / 1_048_576.0)
    } else {
        format!("{} KB", ((n as f64 / 1024.0).round() as i64).max(1))
    }
}

pub enum Target {
    Local(PathBuf),
    Ssh { dest: String, root: String },
}

fn expand(p: &str) -> PathBuf {
    if p == "~" {
        return dirs::home_dir().unwrap_or_default();
    }
    if let Some(rest) = p.strip_prefix("~/") {
        return dirs::home_dir().unwrap_or_default().join(rest);
    }
    PathBuf::from(p)
}

fn sh_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

/// A path on the game box for its shell: quoted, with a leading ~ left for that shell to expand.
pub fn rpath(root: &str, rel: &str) -> String {
    let full = if rel.is_empty() {
        let t = root.trim_end_matches('/');
        if t.is_empty() { "/".to_string() } else { t.to_string() }
    } else {
        format!("{}/{}", root.trim_end_matches('/'), rel)
    };
    if full == "~" {
        return "\"$HOME\"".into();
    }
    if let Some(rest) = full.strip_prefix("~/") {
        return format!("\"$HOME\"/{}", sh_quote(rest));
    }
    sh_quote(&full)
}

impl Target {
    pub fn where_(&self) -> String {
        match self {
            Target::Local(_) => "this computer".into(),
            Target::Ssh { dest, .. } => dest.clone(),
        }
    }

    async fn ssh(dest: &str, command: &str, stdin: Option<&Path>, timeout: u64) -> Result<(i32, String), String> {
        let exe = crate::util::which("ssh").ok_or("no ssh client on PATH")?;
        let mut c = crate::util::command(&exe.to_string_lossy());
        c.args(crate::rcon::SSH_OPTS).arg(dest).arg(command).stdout(Stdio::piped()).stderr(Stdio::piped());
        match stdin {
            Some(p) => c.stdin(std::fs::File::open(p).map_err(|e| e.to_string())?),
            None => c.stdin(Stdio::null()),
        };
        let out = tokio::time::timeout(Duration::from_secs(timeout), c.output())
            .await
            .map_err(|_| "The game box stopped answering partway through.".to_string())?
            .map_err(|e| e.to_string())?;
        let code = out.status.code().unwrap_or(-1);
        let err = String::from_utf8_lossy(&out.stderr).to_string();
        if code == 255 {
            return Err(err.trim().lines().last().unwrap_or("ssh failed").to_string());
        }
        Ok((code, String::from_utf8_lossy(&out.stdout).to_string() + &err))
    }

    /// A text file of ours near the content: `rel` may climb out of it, unlike a manifest's paths.
    pub async fn read_text(&self, rel: &str) -> Result<String, String> {
        match self {
            Target::Local(root) => std::fs::read_to_string(root.join(rel)).map_err(|e| format!("{}: {e}", root.join(rel).display())),
            Target::Ssh { dest, root } => crate::health::run(&format!("cat -- {}", rpath(root, rel)), dest, 30.0).await,
        }
    }

    pub async fn write_text(&self, rel: &str, text: &str) -> Result<(), String> {
        match self {
            Target::Local(root) => crate::util::write_atomic(&root.join(rel), text, false).map_err(|e| format!("{}: {e}", root.join(rel).display())),
            Target::Ssh { dest, root } => {
                let tmp = tempfile::NamedTempFile::new().map_err(|e| e.to_string())?;
                std::fs::write(tmp.path(), text).map_err(|e| e.to_string())?;
                let (part, to) = (rpath(root, &format!("{rel}.part")), rpath(root, rel));
                let (code, out) = Self::ssh(dest, &format!("cat > {part} && mv -f -- {part} {to}"), Some(tmp.path()), 30).await?;
                if code == 0 {
                    return Ok(());
                }
                Err(format!("Couldn't write {rel} on {dest}: {}", out.trim().lines().last().map(str::to_string).unwrap_or_else(|| format!("exit {code}"))))
            }
        }
    }

    /// The names in a folder near the content; none when it isn't there.
    pub async fn list(&self, rel: &str) -> Result<Vec<String>, String> {
        match self {
            Target::Local(root) => Ok(std::fs::read_dir(root.join(rel)).into_iter().flatten().flatten().map(|e| e.file_name().to_string_lossy().to_string()).collect()),
            Target::Ssh { dest, root } => {
                let out = crate::health::run(&format!("ls -1A -- {} 2>/dev/null; true", rpath(root, rel)), dest, 30.0).await?;
                Ok(out.lines().map(str::to_string).collect())
            }
        }
    }

    pub async fn existing(&self, paths: &[String]) -> Result<Vec<String>, String> {
        match self {
            Target::Local(root) => Ok(paths.iter().filter(|p| root.join(p).exists()).cloned().collect()),
            Target::Ssh { dest, root } => {
                let tests: Vec<String> = paths.iter().map(|p| format!("[ -e {} ] && echo {}", rpath(root, p), sh_quote(p))).collect();
                let (_, out) = Self::ssh(dest, &format!("{}; true", tests.join("; ")), None, 60).await?;
                Ok(out.lines().filter(|l| paths.iter().any(|p| p == l)).map(str::to_string).collect())
            }
        }
    }

    pub async fn put(&self, local: &Path, f: &File) -> Result<(), String> {
        match self {
            Target::Local(root) => {
                let (root, local, f) = (root.clone(), local.to_path_buf(), f.clone());
                tokio::task::spawn_blocking(move || {
                    let dest = root.join(&f.path);
                    let part = dest.with_file_name(format!("{}.part", dest.file_name().unwrap().to_string_lossy()));
                    let r = (|| {
                        std::fs::create_dir_all(dest.parent().unwrap()).map_err(|e| e.to_string())?;
                        std::fs::copy(&local, &part).map_err(|e| e.to_string())?;
                        if file_sha(&part).map_err(|e| e.to_string())? != f.sha256 {
                            return Err(format!("The copy of {} doesn't match the manifest's SHA-256: removed.", f.path));
                        }
                        std::fs::rename(&part, &dest).map_err(|e| e.to_string())
                    })();
                    let _ = std::fs::remove_file(&part);
                    r.map_err(|e| if e.starts_with("The copy") { e } else { format!("Couldn't write {} into {}: {e}", f.path, root.display()) })
                })
                .await
                .map_err(|e| e.to_string())?
            }
            Target::Ssh { dest, root } => {
                let part = rpath(root, &format!("{}.part", f.path));
                let to = rpath(root, &f.path);
                let folder = match f.path.rfind('/') {
                    Some(i) => rpath(root, &f.path[..i]),
                    None => rpath(root, ""),
                };
                let command = format!(
                    "command -v sha256sum >/dev/null 2>&1 || exit 4; mkdir -p -- {folder} || exit 5; cat > {part} || {{ rm -f -- {part}; exit 6; }}; \
                     if [ \"$(sha256sum < {part} | cut -c1-64)\" = {} ]; then mv -f -- {part} {to}; else rm -f -- {part}; exit 3; fi",
                    f.sha256
                );
                let (code, out) = Self::ssh(dest, &command, Some(local), 600).await?;
                if code == 0 {
                    return Ok(());
                }
                let said = out.trim().lines().last().unwrap_or_default().to_string();
                Err(match code {
                    3 => format!("The copy of {} on {dest} doesn't match the manifest's SHA-256, so it was removed.", f.path),
                    4 => format!("{dest} has no sha256sum, so a copy there can't be checked. Nothing was installed."),
                    5 => format!("Couldn't make the folder for {} on {dest}: {said}", f.path),
                    6 => format!("Couldn't write {} on {dest}: {said}", f.path),
                    _ => format!("Copying {} to {dest} failed: {}", f.path, if said.is_empty() { format!("exit {code}") } else { said }),
                })
            }
        }
    }
}

/// Where a server's Forge content goes, or why it can't have any, in words.
pub fn target_for(s: &Server) -> Result<Target, String> {
    if s.content_dir.is_empty() {
        return Err("This server has no content_dir: add content_dir = \"...\" to its [[server]] block in the config file, naming the folder the dedicated server loads content from (on the game box, with ssh).".into());
    }
    if !s.url.is_empty() {
        return Err("This server is reached by a ws:// or wss:// link, so oni-rcon can't put files on it. Reach it with ssh instead.".into());
    }
    if !s.ssh.is_empty() {
        return Ok(Target::Ssh { dest: s.ssh.clone(), root: s.content_dir.clone() });
    }
    if loopback(&s.host) {
        return Ok(Target::Local(expand(&s.content_dir)));
    }
    Err("This server is on another machine with no ssh set, so oni-rcon can't put files on it. Set ssh for it.".into())
}

/// Servers with the same place share one content folder, and so one install.
pub fn place(s: &Server) -> (String, String) {
    let d = s.content_dir.trim_end_matches('/').replace("/./", "/");
    (s.ssh.clone(), d)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn paths_stay_inside() {
        assert!(safe_path("maps/guardian.map").is_ok());
        for bad in ["../x", "/etc/passwd", "C:\\x", ".hidden", "a/b/c/d/e", "x.", ""] {
            assert!(safe_path(bad).is_err(), "{bad}");
        }
        assert_eq!(rpath("~/content", "a b.map"), "\"$HOME\"/'content/a b.map'");
    }

    #[test]
    fn plans_check_everything() {
        let sha = "a".repeat(64);
        let ok = json!({"assets": [{"path": "x.map", "url": "/a", "size": 3, "sha256": sha}]});
        assert_eq!(plan(&ok).unwrap().len(), 1);
        assert!(plan(&json!({"assets": [{"path": "x.map", "url": "/a", "size": 3, "sha256": "nope"}]})).is_err());
        assert!(plan(&json!({"assets": [{"path": "x.map", "url": "/a", "size": 3, "sha256": sha}, {"path": "X.map", "url": "/b", "size": 3, "sha256": sha}]})).is_err());
    }
}
