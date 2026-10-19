//! herdr-notify: real-time herdr agent state notifications.
//!
//! Watches every opted-in instance from the omarchy-link registry (local unix
//! sockets and registered SSH targets), subscribes to pane events, and on a
//! status transition notifies both the desktop (notify-send) and the phone
//! (POST to the link server's /omarchy/notify, which forwards to Ohm's
//! OmarchyNotify). Transitions INTO `blocked` (needs you) and into `done`
//! from `working` are the user-facing signals; everything else is noise.
//!
//! Notifications are deduplicated by a stable id so a repeated signal updates
//! the existing entry instead of piling up, and cooled down per pane so a
//! flapping agent cannot spam either surface.

use libherdr::{local_socket_path, Client, RemoteBridge, RemoteTarget};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const LINK_PORT: u16 = 8753;
const TOKEN_FILE: &str = "/tmp/omarchy-link-token";
const REGISTRY_RESYNC: Duration = Duration::from_secs(30);
const RECONNECT_BACKOFF: Duration = Duration::from_secs(5);
const ATTENTION_COOLDOWN: Duration = Duration::from_secs(120);
const DONE_COOLDOWN: Duration = Duration::from_secs(30);

/// Registry instance row (body JSON from the link registry).
#[derive(Debug, Clone, PartialEq, Eq)]
struct Instance {
    id: String,
    name: String,
    transport: String,
    socket: String,
    host: String,
    port: u16,
    user: String,
    session: String,
    opted_in: bool,
}

fn parse_instance(body: &str) -> Option<Instance> {
    let v: Value = serde_json::from_str(body).ok()?;
    let o = v.as_object()?;
    Some(Instance {
        id: o.get("id")?.as_str()?.to_string(),
        name: o.get("name")?.as_str()?.to_string(),
        transport: o.get("transport")?.as_str()?.to_string(),
        socket: o.get("socket")?.as_str()?.to_string(),
        host: o.get("host").and_then(Value::as_str).unwrap_or("local").to_string(),
        port: o.get("port").and_then(Value::as_u64).unwrap_or(22) as u16,
        user: o.get("user").and_then(Value::as_str).unwrap_or("").to_string(),
        session: o.get("session").and_then(Value::as_str).unwrap_or("default").to_string(),
        opted_in: o.get("opted_in").and_then(Value::as_bool).unwrap_or(false),
    })
}

fn read_instances(db: &Path) -> Vec<Instance> {
    let conn = match rusqlite::Connection::open_with_flags(
        db,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    ) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("herdr-notify: registry unreadable: {e}");
            return Vec::new();
        }
    };
    let mut stmt = match conn.prepare("SELECT body FROM instances ORDER BY id") {
        Ok(s) => s,
        Err(e) => {
            eprintln!("herdr-notify: registry query failed: {e}");
            return Vec::new();
        }
    };
    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map(|r| r.filter_map(|b| b.ok()).filter_map(|b| parse_instance(&b)).collect())
        .unwrap_or_default();
    rows
}

// --- Notification sinks -----------------------------------------------------

/// What a transition becomes; None means "stay silent".
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum Signal {
    Attention,
    Done,
}

/// The transition policy: only user-meaningful edges produce a signal.
fn transition_signal(from: Option<&str>, to: &str) -> Option<Signal> {
    match to {
        "blocked" | "attention" | "waiting" => Some(Signal::Attention),
        "done" if from == Some("working") => Some(Signal::Done),
        _ => None,
    }
}

struct Cooldowns {
    last: HashMap<(String, Signal), Instant>,
}

impl Cooldowns {
    fn allow(&mut self, pane: &str, signal: &Signal) -> bool {
        let window = match signal {
            Signal::Attention => ATTENTION_COOLDOWN,
            Signal::Done => DONE_COOLDOWN,
        };
        let key = (pane.to_string(), signal.clone());
        match self.last.get(&key) {
            Some(at) if at.elapsed() < window => false,
            _ => {
                self.last.insert(key, Instant::now());
                true
            }
        }
    }
}

fn notify_send_desktop(title: &str, body: &str, urgency: &str) {
    let _ = Command::new("notify-send")
        .args(["-u", urgency, "-a", "Omi", title, body])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
}

/// POST one notification to the link server, which forwards it to the phone.
/// Loopback only; the token authenticates exactly like the widget does.
fn notify_phone(token: &str, id: &str, title: &str, body: &str, level: &str) -> Result<(), String> {
    let payload = json!({
        "id": id,
        "title": title,
        "message": body,
        "source": "herdr-notify",
        "channel": "agent",
        "level": level,
    });
    let data = serde_json::to_vec(&payload).map_err(|e| e.to_string())?;
    let mut stream = TcpStream::connect(("127.0.0.1", LINK_PORT)).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .map_err(|e| e.to_string())?;
    let request = format!(
        "POST /omarchy/notify HTTP/1.1\r\nHost: 127.0.0.1:{LINK_PORT}\r\nX-Omarchy-Link-Token: {token}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        data.len()
    );
    stream
        .write_all(request.as_bytes())
        .and_then(|_| stream.write_all(&data))
        .map_err(|e| e.to_string())?;
    let mut reply = Vec::new();
    stream
        .take(16 * 1024)
        .read_to_end(&mut reply)
        .map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&reply);
    let status = text
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(0);
    if status == 200 && text.contains("\"delivered\": true") {
        Ok(())
    } else {
        Err(format!("link notify rejected: status {status}"))
    }
}

fn emit(token: &str, instance: &Instance, pane: &str, title: &str, signal: &Signal) {
    let (level, urgency, verb) = match signal {
        Signal::Attention => ("warning", "critical", "needs you"),
        Signal::Done => ("success", "normal", "finished"),
    };
    let headline = format!("{} · {}", instance.name, title);
    let body = format!("agent {verb}");
    notify_send_desktop(&headline, &body, urgency);
    let id = format!("herdr-{}-{}-{}", instance.id, pane, verb.replace(' ', "-"));
    if let Err(e) = notify_phone(token, &id, &headline, &body, level) {
        eprintln!("herdr-notify: phone notify failed ({}): {e}", instance.name);
    }
}

// --- Instance watchers ------------------------------------------------------

struct Watcher {
    instance: Instance,
    statuses: HashMap<String, String>,
    cooldowns: Cooldowns,
    token: String,
}

fn fresh_client(instance: &Instance, bridge: &Option<RemoteBridge>) -> libherdr::Result<Client> {
    if instance.transport == "ssh" {
        match bridge {
            Some(b) => b.connect(),
            None => Err(libherdr::Error::Remote("bridge not established".into())),
        }
    } else {
        let path = if instance.socket.is_empty() {
            local_socket_path(&instance.session)
        } else {
            PathBuf::from(&instance.socket)
        };
        Client::connect_local(&path)
    }
}

fn snapshot_statuses(client: &mut Client) -> libherdr::Result<Vec<(String, String, String)>> {
    let result = client.call("session.snapshot", json!({}))?;
    let agents = result["snapshot"]["agents"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    Ok(agents
        .iter()
        .filter_map(|a| {
            Some((
                a["pane_id"].as_str()?.to_string(),
                a["agent_status"].as_str().unwrap_or("unknown").to_string(),
                a["name"].as_str().unwrap_or("agent").to_string(),
            ))
        })
        .collect())
}

impl Watcher {
    fn run(&mut self, stop: &AtomicBool) {
        while !stop.load(Ordering::Relaxed) {
            if let Err(e) = self.watch_once(stop) {
                eprintln!("herdr-notify: {} watcher: {e}", self.instance.name);
            }
            if stop.load(Ordering::Relaxed) {
                break;
            }
            std::thread::sleep(RECONNECT_BACKOFF);
        }
    }

    fn watch_once(&mut self, stop: &AtomicBool) -> libherdr::Result<()> {
        let bridge = if self.instance.transport == "ssh" {
            Some(RemoteBridge::establish(&RemoteTarget {
                host: self.instance.host.clone(),
                port: self.instance.port,
                user: self.instance.user.clone(),
                session: self.instance.session.clone(),
            })?)
        } else {
            None
        };
        // Prime the status map so startup does not notify for steady states.
        let mut client = fresh_client(&self.instance, &bridge)?;
        for (pane, status, _title) in snapshot_statuses(&mut client)? {
            self.statuses.insert(pane, status);
        }
        // Subscribe: pane.updated fires for state/output changes; per-pane
        // agent_status_changed needs a pane_id, so add one per known pane.
        let mut entries = vec![
            json!({"type": "pane.updated"}),
            json!({"type": "pane.created"}),
            json!({"type": "pane.closed"}),
        ];
        for pane in self.statuses.keys() {
            entries.push(json!({"type": "pane.agent_status_changed", "pane_id": pane}));
        }
        let sub_client = fresh_client(&self.instance, &bridge)?;
        let mut sub = sub_client.subscribe(entries)?;
        loop {
            if stop.load(Ordering::Relaxed) {
                return Ok(());
            }
            let event = sub.next_event()?;
            if !event.name.starts_with("pane.") {
                continue;
            }
            // Any pane event triggers a fresh snapshot diff.
            let mut probe = fresh_client(&self.instance, &bridge)?;
            let current = snapshot_statuses(&mut probe)?;
            for (pane, status, title) in current {
                let previous = self.statuses.get(&pane).cloned();
                if previous.as_deref() == Some(status.as_str()) {
                    continue;
                }
                if let Some(signal) = transition_signal(previous.as_deref(), &status) {
                    if self.cooldowns.allow(&pane, &signal) {
                        emit(&self.token, &self.instance, &pane, &title, &signal);
                    }
                }
                self.statuses.insert(pane, status);
            }
        }
    }
}

fn load_token() -> Option<String> {
    std::fs::read_to_string(TOKEN_FILE)
        .ok()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty() && t.len() <= 256)
}

fn registry_path() -> PathBuf {
    std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").expect("HOME")).join(".local/share"))
        .join("omarchy-link/herdr/herdr.sqlite")
}

fn main() {
    let db = registry_path();
    let stop = Arc::new(AtomicBool::new(false));
    let watchers: Arc<Mutex<HashMap<String, std::thread::JoinHandle<()>>>> =
        Arc::new(Mutex::new(HashMap::new()));
    let Some(token) = load_token() else {
        eprintln!("herdr-notify: link token unavailable at {TOKEN_FILE}; exiting");
        std::process::exit(1);
    };
    eprintln!("herdr-notify: watching registry {}", db.display());
    loop {
        for instance in read_instances(&db) {
            if !instance.opted_in {
                continue;
            }
            let mut set = watchers.lock().unwrap();
            if set.contains_key(&instance.id) {
                continue;
            }
            let mut watcher = Watcher {
                statuses: HashMap::new(),
                cooldowns: Cooldowns { last: HashMap::new() },
                token: token.clone(),
                instance: instance.clone(),
            };
            let stop_flag = Arc::clone(&stop);
            let handle = std::thread::spawn(move || watcher.run(&stop_flag));
            set.insert(instance.id.clone(), handle);
            eprintln!("herdr-notify: watching {} ({})", instance.name, instance.transport);
        }
        // Reap finished watchers so a dead instance is re-watched next pass.
        watchers.lock().unwrap().retain(|_, h| !h.is_finished());
        std::thread::sleep(REGISTRY_RESYNC);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transitions_only_on_user_meaningful_edges() {
        assert_eq!(transition_signal(Some("working"), "blocked"), Some(Signal::Attention));
        assert_eq!(transition_signal(Some("idle"), "blocked"), Some(Signal::Attention));
        assert_eq!(transition_signal(Some("working"), "done"), Some(Signal::Done));
        assert_eq!(transition_signal(Some("idle"), "done"), None);
        assert_eq!(transition_signal(None, "done"), None);
        assert_eq!(transition_signal(Some("working"), "idle"), None);
        assert_eq!(transition_signal(Some("done"), "working"), None);
        assert_eq!(transition_signal(Some("working"), "working"), None);
    }

    #[test]
    fn cooldown_suppresses_repeats() {
        let mut cd = Cooldowns { last: HashMap::new() };
        assert!(cd.allow("wN:p1", &Signal::Attention));
        assert!(!cd.allow("wN:p1", &Signal::Attention));
        assert!(cd.allow("wN:p1", &Signal::Done));
        assert!(cd.allow("wN:p2", &Signal::Attention));
    }

    #[test]
    fn registry_row_parsing() {
        let body = r##"{"id":"abc","name":"Node","color":"#fff","transport":"ssh","socket":"ssh:abc","host":"192.168.1.177","port":22,"user":"node","session":"default","opted_in":true}"##;
        let row = parse_instance(body).expect("parses");
        assert_eq!(row.transport, "ssh");
        assert_eq!(row.port, 22);
        assert!(row.opted_in);
        assert!(parse_instance("{}").is_none());
        assert!(parse_instance("not json").is_none());
    }
}
