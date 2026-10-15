//! Project Reclaimer RCON over its WebSocket protocol, and the SSH tunnels that reach it.
//!
//! Protocol (server 0.9.x): send {"type":"auth","password"}, get {"type":"auth","ok",...,"server","version"}; then
//! {"type":"command","command","args":[...],"id","by"} -> {"type":"reply","id","ok","text","data"?}. The server also
//! pushes {"type":"event","event":"chat"|"kill"|"join"|...} to every signed-in tool.
use futures_util::{SinkExt, StreamExt};
use parking_lot::Mutex;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, oneshot, watch, Semaphore};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message;

/// What a connection tells whoever owns it.
#[derive(Debug, Clone)]
pub enum LinkNote {
    State { state: &'static str, detail: String, retry_in: u64, info: Value },
    Event(Value),
    Late { line: String, reply: Value },
}

#[derive(Debug)]
pub enum CallError {
    NotConnected(String),
    Timeout(f64),
    Lost,
}

impl std::fmt::Display for CallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CallError::NotConnected(d) => f.write_str(d),
            CallError::Timeout(t) => write!(f, "no reply in {t}s; it may still run, and isn't resent"),
            CallError::Lost => f.write_str("connection lost"),
        }
    }
}

struct Inner {
    state: &'static str,
    detail: String,
    info: Value,
    out: Option<mpsc::UnboundedSender<String>>,
    pending: HashMap<u64, oneshot::Sender<Value>>,
    late: VecDeque<(u64, String)>, // commands that timed out, by id: their reply may still come
}

/// One signed-in connection per server, shared by commands and the event stream: servers admit only 4 tools.
#[derive(Clone)]
pub struct Rcon {
    inner: Arc<Mutex<Inner>>,
    ids: Arc<AtomicU64>,
    pub password: Arc<Mutex<String>>,
    pub by: String,
}

impl Rcon {
    pub fn new(password: &str, by: &str) -> Self {
        Rcon {
            inner: Arc::new(Mutex::new(Inner {
                state: "connecting",
                detail: String::new(),
                info: json!({}),
                out: None,
                pending: HashMap::new(),
                late: VecDeque::new(),
            })),
            ids: Arc::new(AtomicU64::new(1)),
            password: Arc::new(Mutex::new(password.to_string())),
            by: by.to_string(),
        }
    }

    fn set(&self, tx: &mpsc::UnboundedSender<LinkNote>, state: &'static str, detail: String, retry_in: u64) {
        let info = {
            let mut i = self.inner.lock();
            i.state = state;
            i.detail = detail.clone();
            i.info.clone()
        };
        let _ = tx.send(LinkNote::State { state, detail, retry_in, info });
    }

    /// Connect, sign in and pump messages; reconnect with backoff. Returns only when the sign-in is refused.
    pub async fn run(
        self,
        url: String,
        mut ready: Option<watch::Receiver<bool>>,
        gate: Arc<Semaphore>,
        tx: mpsc::UnboundedSender<LinkNote>,
    ) {
        let mut delay: u64 = 2;
        loop {
            if let Some(r) = ready.as_mut() {
                if !*r.borrow() {
                    self.set(&tx, "connecting", "awaiting SSH tunnel".into(), 0);
                    if r.wait_for(|up| *up).await.is_err() {
                        return;
                    }
                }
            }
            self.set(&tx, "connecting", String::new(), 0);
            let detail = match self.session(&url, &gate, &tx).await {
                Ok(Some(denied)) => {
                    // Never retry a refused sign-in: 5 wrong passwords in 10 min lock the address out.
                    self.set(&tx, "denied", denied, 0);
                    return;
                }
                Ok(None) => {
                    delay = 2;
                    "connection closed".to_string()
                }
                Err(e) => e,
            };
            {
                let mut i = self.inner.lock();
                i.out = None;
                i.pending.clear(); // dropping the senders fails every waiting call with "connection lost"
                i.late.clear(); // a reply only comes back on the connection that asked
            }
            // jittered, so a fleet that lost its link together doesn't come back in lockstep
            let wait = delay + crate::util::rnd(0.0, (delay / 2 + 1) as f64) as u64;
            self.set(&tx, "offline", format!("{detail}; retry in {wait}s"), wait);
            tokio::time::sleep(Duration::from_secs(wait)).await;
            delay = (delay * 2).min(60);
        }
    }

    /// One connection: Ok(Some(why)) when the password was refused, Ok(None) when it closed after signing in.
    async fn session(
        &self,
        url: &str,
        gate: &Semaphore,
        tx: &mpsc::UnboundedSender<LinkNote>,
    ) -> Result<Option<String>, String> {
        let (ws, reply) = {
            let _permit = gate.acquire().await.map_err(|e| e.to_string())?; // only the sign-in: a fleet connects a few at a time
            let mut cfg = WebSocketConfig::default();
            cfg.max_message_size = None;
            cfg.max_frame_size = None;
            let (mut ws, _) = tokio::time::timeout(
                Duration::from_secs(10),
                tokio_tungstenite::connect_async_with_config(url, Some(cfg), false),
            )
            .await
            .map_err(|_| "timed out during opening handshake".to_string())?
            .map_err(|e| e.to_string())?;
            let pw = self.password.lock().clone();
            ws.send(Message::text(json!({"type": "auth", "password": pw}).to_string()))
                .await
                .map_err(|e| e.to_string())?;
            let first = tokio::time::timeout(Duration::from_secs(10), ws.next())
                .await
                .map_err(|_| "timed out waiting for the sign-in reply".to_string())?;
            let text = match first {
                Some(Ok(Message::Text(t))) => t.to_string(),
                Some(Ok(_)) => return Err("that port doesn't speak RCON".into()),
                Some(Err(e)) => return Err(e.to_string()),
                None => return Err("connection closed".into()),
            };
            let reply: Value = serde_json::from_str(&text).map_err(|_| "that port doesn't speak RCON".to_string())?;
            if !reply.is_object() {
                return Err("that port doesn't speak RCON".into());
            }
            (ws, reply)
        };
        if reply.get("ok").and_then(Value::as_bool) != Some(true) {
            let why = crate::util::pick_str(&reply, &["error", "text"]);
            return Ok(Some(if why.is_empty() { "sign-in refused".into() } else { why }));
        }
        let (mut sink, mut stream) = ws.split();
        let (out_tx, mut out_rx) = mpsc::unbounded_channel::<String>();
        {
            let mut i = self.inner.lock();
            i.info = reply.clone();
            i.out = Some(out_tx);
        }
        let version = crate::util::pick_str(&reply, &["version"]);
        self.set(tx, "online", format!("v{}", if version.is_empty() { "?" } else { &version }), 0);
        loop {
            tokio::select! {
                msg = stream.next() => match msg {
                    Some(Ok(Message::Text(t))) => {
                        if let Ok(v) = serde_json::from_str::<Value>(&t) {
                            if v.is_object() {
                                self.dispatch(v, tx);
                            }
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => return Ok(None),
                    Some(Ok(_)) => {}
                    Some(Err(e)) => return Err(e.to_string()),
                },
                out = out_rx.recv() => match out {
                    Some(text) => sink.send(Message::text(text)).await.map_err(|e| e.to_string())?,
                    None => return Ok(None),
                },
            }
        }
    }

    fn dispatch(&self, msg: Value, tx: &mpsc::UnboundedSender<LinkNote>) {
        match msg.get("type").and_then(Value::as_str) {
            Some("reply") => {
                let id = msg.get("id").and_then(Value::as_u64).unwrap_or(0);
                let mut i = self.inner.lock();
                if let Some(f) = i.pending.remove(&id) {
                    let _ = f.send(msg);
                } else if let Some(pos) = i.late.iter().position(|(x, _)| *x == id) {
                    let (_, line) = i.late.remove(pos).unwrap();
                    let _ = tx.send(LinkNote::Late { line, reply: msg });
                }
            }
            Some("event") => {
                let _ = tx.send(LinkNote::Event(msg));
            }
            other => {
                // {"type":"error"} for a malformed message, or anything newer than this client
                let mut m = msg.clone();
                let kind = other.unwrap_or("unknown").to_string();
                if let Some(o) = m.as_object_mut() {
                    o.insert("type".into(), "event".into());
                    o.insert("event".into(), kind.into());
                }
                let _ = tx.send(LinkNote::Event(m));
            }
        }
    }

    /// Run one command; the reply ({"ok","text","data"?}). Never retried: commands aren't idempotent.
    pub async fn call(&self, command: &str, args: &[String], timeout: f64) -> Result<Value, CallError> {
        let id = self.ids.fetch_add(1, Ordering::Relaxed);
        let (ftx, frx) = oneshot::channel();
        {
            let mut i = self.inner.lock();
            let Some(out) = i.out.clone() else {
                let why = if i.detail.is_empty() { i.state.to_string() } else { i.detail.clone() };
                return Err(CallError::NotConnected(why));
            };
            i.pending.insert(id, ftx);
            let msg = json!({"type": "command", "command": command, "args": args, "id": id, "by": self.by});
            if out.send(msg.to_string()).is_err() {
                i.pending.remove(&id);
                return Err(CallError::Lost);
            }
        }
        match tokio::time::timeout(Duration::from_secs_f64(timeout), frx).await {
            Ok(Ok(v)) => Ok(v),
            Ok(Err(_)) => Err(CallError::Lost),
            Err(_) => {
                let mut i = self.inner.lock();
                i.pending.remove(&id);
                i.late.push_back((id, std::iter::once(command.to_string()).chain(args.iter().cloned()).collect::<Vec<_>>().join(" ")));
                while i.late.len() > 200 {
                    i.late.pop_front(); // replies that never come shouldn't pile up for ever
                }
                Err(CallError::Timeout(timeout))
            }
        }
    }
}

pub fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(0)
}

pub const SSH_OPTS: &[&str] = &["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"];

/// One `ssh -N -L ...` per destination, forwarding every server behind it; restarted if ssh exits.
pub struct Tunnel {
    pub dest: String,
    pub local: Vec<((String, u16), u16)>, // (host, port) -> local port
    pub ready: watch::Sender<bool>,
    pub state: Arc<Mutex<(String, String)>>,
}

impl Tunnel {
    pub fn new(dest: &str, remotes: &[(String, u16)]) -> Self {
        let mut local: Vec<((String, u16), u16)> = vec![];
        for r in remotes {
            if !local.iter().any(|(x, _)| x == r) {
                local.push((r.clone(), free_port()));
            }
        }
        Tunnel {
            dest: dest.into(),
            local,
            ready: watch::channel(false).0,
            state: Arc::new(Mutex::new(("opening".into(), String::new()))),
        }
    }

    pub fn local_port(&self, host: &str, port: u16) -> u16 {
        self.local.iter().find(|((h, p), _)| h == host && *p == port).map(|(_, l)| *l).unwrap_or(0)
    }

    pub async fn run(self: Arc<Self>, tx: mpsc::UnboundedSender<(String, String, String)>) {
        let set = |s: &str, d: String| {
            *self.state.lock() = (s.to_string(), d.clone());
            let _ = tx.send((self.dest.clone(), s.to_string(), d));
        };
        let Some(ssh) = crate::util::which("ssh") else {
            set("down", "no ssh client on PATH".into());
            return;
        };
        let mut delay: u64 = 2;
        loop {
            set("opening", String::new());
            let mut c = crate::util::command(&ssh.to_string_lossy());
            c.args(["-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]);
            for ((host, port), local) in &self.local {
                c.arg("-L").arg(format!("127.0.0.1:{local}:{host}:{port}"));
            }
            c.arg(&self.dest)
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::piped());
            let (err, code, was_up) = match c.spawn() {
                Err(e) => (e.to_string(), None, false),
                Ok(mut child) => {
                    let mut stderr = child.stderr.take();
                    let port = self.local.first().map(|(_, l)| *l).unwrap_or(0);
                    let me = self.clone();
                    let set2 = {
                        let tx = tx.clone();
                        let state = self.state.clone();
                        let dest = self.dest.clone();
                        move || {
                            *state.lock() = ("up".into(), String::new());
                            let _ = tx.send((dest.clone(), "up".into(), String::new()));
                        }
                    };
                    // ssh listens locally only once it has signed in, so a local port accepting means it's up
                    let probe = tokio::spawn(async move {
                        loop {
                            if tokio::net::TcpStream::connect(("127.0.0.1", port)).await.is_ok() {
                                set2();
                                let _ = me.ready.send(true);
                                return;
                            }
                            tokio::time::sleep(Duration::from_millis(300)).await;
                        }
                    });
                    let mut buf = Vec::new();
                    if let Some(s) = stderr.as_mut() {
                        use tokio::io::AsyncReadExt;
                        let _ = s.read_to_end(&mut buf).await;
                    }
                    let status = child.wait().await.ok();
                    probe.abort();
                    let up = self.state.lock().0 == "up";
                    (String::from_utf8_lossy(&buf).trim().to_string(), status.and_then(|s| s.code()), up)
                }
            };
            let _ = self.ready.send(false);
            if was_up {
                delay = 2;
            }
            let last = err.lines().last().map(str::to_string).unwrap_or_else(|| format!("ssh exited {}", code.unwrap_or(-1)));
            set("down", format!("{last}; retry in {delay}s"));
            tokio::time::sleep(Duration::from_secs(delay)).await;
            delay = (delay * 2).min(60);
        }
    }
}
