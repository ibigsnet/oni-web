async fn probe_raw(s: Server) -> Value {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let mut tasks = vec![];
    let (url, ready) = if !s.ssh.is_empty() && s.url.is_empty() {
        let t = Arc::new(rcon::Tunnel::new(&s.ssh, &[(s.host.clone(), s.port)]));
        let (ttx, mut trx) = tokio::sync::mpsc::unbounded_channel();
        tasks.push(tokio::spawn(t.clone().run(ttx)));
        let tx2 = tx.clone();
        tasks.push(tokio::spawn(async move {
            while let Some((_, state, detail)) = trx.recv().await {
                if state == "down" {
                    let _ = tx2.send(rcon::LinkNote::State { state: "tunnel", detail, retry_in: 0, info: Value::Null });
                }
            }
        }));
        (format!("ws://127.0.0.1:{}", t.local_port(&s.host, s.port)), Some(t.ready.subscribe()))
    } else {
        (s.direct_url(), None)
    };
    let rc = rcon::Rcon::new(&s.password, "setup");
    tasks.push(tokio::spawn(rc.run(url, ready, Arc::new(tokio::sync::Semaphore::new(1)), tx)));
    let wait = async {
        while let Some(n) = rx.recv().await {
            if let rcon::LinkNote::State { state, detail, info, .. } = n {
                match state {
                    "online" => {
                        let name = util::pick_str(&info, &["server"]);
                        return json!({"ok": true, "text": format!("Connected to {}.", if name.is_empty() { s.where_() } else { name })});
                    }
                    "denied" | "offline" | "tunnel" => return json!({"ok": false, "detail": detail, "state": if state == "tunnel" { "offline" } else { state }}),
                    _ => {}
                }
            }
        }
        json!({"ok": false, "text": "No answer."})
    };
    let out = tokio::time::timeout(std::time::Duration::from_secs(12), wait)
        .await
        .unwrap_or_else(|_| json!({"ok": false, "text": "No answer in time. Check the address and port, and any firewall in between."}));
    for t in tasks {
        t.abort();
    }
    out
}

