//! ONI Web adaptation, 2026-10-15. The native implementation is upstream code;
//! this adapter replaces Tauri with one request on stdin and JSON lines on stdout.
//! stdout is a private backend IPC pipe. It must never be exposed to a browser.
#![allow(dead_code)]
mod config;
mod forge;
mod health;
mod host;
mod install;
mod rcon;
mod util;

use config::Server;
use serde_json::{json, Value};
use std::io::{BufRead, Write};
use std::sync::Arc;

include!("probe.rs");

async fn probe(server: Server) -> Value {
    let password = server.password.clone();
    let mut reply = probe_raw(server).await;
    fn redact(value: &mut Value, password: &str) {
        match value {
            Value::String(text) if !password.is_empty() => *text = text.replace(password, "[redacted]"),
            Value::Array(values) => values.iter_mut().for_each(|v| redact(v, password)),
            Value::Object(values) => values.values_mut().for_each(|v| redact(v, password)),
            _ => {},
        }
    }
    redact(&mut reply, &password);
    reply
}

fn emit(value: Value) {
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{value}");
    let _ = out.flush();
}

fn found(servers: Vec<Server>, notes: Vec<String>, templates: Vec<(String, String)>) -> Value {
    // Upstream's public Serialize intentionally skips credentials. This pipe goes
    // only to Python, which encrypts them and returns a separate public response.
    let servers: Vec<Value> = servers.into_iter().map(|server| {
        let mut v = serde_json::to_value(&server).unwrap_or_else(|_| json!({}));
        v["password"] = json!(server.password);
        v["password_command"] = json!(server.password_command);
        v
    }).collect();
    json!({"servers": servers, "notes": notes, "templates": templates})
}

#[tokio::main]
async fn main() {
    let line = std::io::stdin().lock().lines().next();
    let result: Result<Value, String> = async {
        let line = line.ok_or("Missing request")?.map_err(|_| "Cannot read request")?;
        if line.len() > 2_000_000 { return Err("Request too large".into()); }
        let request: Value = serde_json::from_str(&line).map_err(|_| "Invalid request JSON")?;
        let say = |line: &str| emit(json!({"type": "progress", "text": line}));
        match request["action"].as_str().unwrap_or("") {
            "status" => Ok(host::status()),
            "host" => {
                let settings: host::Settings = serde_json::from_value(request["settings"].clone())
                    .map_err(|_| "Invalid hosting settings")?;
                settings.check()?;
                let (servers, notes) = host::host_here(Some(&settings), &say).await?;
                Ok(found(servers, notes, vec![]))
            }
            "find" => {
                let destination = request["dest"].as_str().unwrap_or("");
                if destination.is_empty() || destination.starts_with('-') || destination.chars().any(char::is_control) {
                    return Err("Invalid SSH destination".into());
                }
                let (servers, notes, templates) = host::connect_box(destination, &say).await?;
                Ok(found(servers, notes, templates))
            }
            _ => Err("Unknown operation".into()),
        }
    }.await;
    match result {
        Ok(data) => emit(json!({"type": "result", "ok": true, "data": data})),
        Err(error) => {
            emit(json!({"type": "result", "ok": false, "error": error}));
            std::process::exit(1);
        }
    }
}
