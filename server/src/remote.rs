//! Read-only SSH discovery of a peer's already-running Hermes terminal.
use crate::{home, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::Write,
    path::{Component, Path},
    process::{Command, Stdio},
    sync::{mpsc, Mutex, OnceLock},
    thread,
    time::{Duration, Instant},
};

const PROBE: &str = include_str!("../../bin/ares_remote_probe.py");
const ACTIVITY_PROBE: &str = include_str!("../../bin/ares_remote_activity.py");

#[derive(Default)]
struct ActivityCache {
    peers: HashMap<String, ActivityEntry>,
}

#[derive(Default)]
struct ActivityEntry {
    last_attempt: Option<Instant>,
    last_success: Option<Instant>,
    items: Vec<Value>,
}

impl ActivityCache {
    fn schedule(&mut self, peer: &str, at: Instant) -> bool {
        let entry = self.peers.entry(peer.to_owned()).or_default();
        if entry.last_attempt.is_some_and(|previous| {
            at.saturating_duration_since(previous) < Duration::from_secs(15)
        }) {
            return false;
        }
        entry.last_attempt = Some(at);
        true
    }

    fn complete(&mut self, peer: &str, at: Instant, result: Option<Vec<Value>>) {
        if let Some(items) = result {
            let entry = self.peers.entry(peer.to_owned()).or_default();
            entry.last_success = Some(at);
            entry.items = items;
        }
    }

    fn snapshot(&self, peer: &str, at: Instant) -> Vec<Value> {
        self.peers
            .get(peer)
            .filter(|entry| {
                entry.last_success.is_some_and(|last| {
                    at.saturating_duration_since(last) < Duration::from_secs(30)
                })
            })
            .map(|entry| entry.items.clone())
            .unwrap_or_default()
    }
}

static ACTIVITY_CACHE: OnceLock<Mutex<ActivityCache>> = OnceLock::new();

fn safe_identity(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

pub fn parse_remote_session<'a>(reference: &'a str, peer: &str, profile: &str) -> Option<&'a str> {
    if !safe_identity(peer) || !safe_identity(profile) {
        return None;
    }
    let session = reference.strip_prefix(&format!("remote::{peer}::{profile}::"))?;
    safe_identity(session).then_some(session)
}

pub fn validate_ssh_target(target: &str) -> Result<()> {
    let (user, host) = target
        .split_once('@')
        .ok_or("SSH target must be user@host")?;
    let safe_user = !user.is_empty()
        && user.len() <= 64
        && user.as_bytes()[0].is_ascii_alphanumeric()
        && user
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-');
    let safe_host = !host.is_empty()
        && host.len() <= 253
        && host.as_bytes()[0].is_ascii_alphanumeric()
        && host
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'.' || byte == b'-');
    if !safe_user || !safe_host {
        return Err("unsafe SSH target".into());
    }
    Ok(())
}

pub fn ssh_target_for_peer<'a>(config: &'a Value, peer: &str) -> Result<&'a str> {
    if !safe_identity(peer) {
        return Err("unsafe peer identity".into());
    }
    let target = config[peer]
        .as_str()
        .ok_or("remote peer has no configured SSH target")?;
    validate_ssh_target(target)?;
    Ok(target)
}

fn safe_remote_path(value: &str) -> bool {
    let path = Path::new(value);
    path.is_absolute()
        && value.len() <= 240
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'/' | b'.' | b'_' | b'-'))
        && path
            .components()
            .all(|component| matches!(component, Component::RootDir | Component::Normal(_)))
}

pub fn probe_live_room(room_id: &str, peer: &str, profile: &str, session: &str) -> Result<Value> {
    crate::herdr::validate_target(room_id, profile, session)?;
    if !safe_identity(peer) {
        return Err("unsafe remote peer identity".into());
    }
    let config_path = home().join(".config/omarchy-ares/ssh-peers.json");
    let config: Value = serde_json::from_slice(&fs::read(&config_path).map_err(|_| {
        format!(
            "remote SSH target is not configured ({})",
            config_path.display()
        )
    })?)?;
    let ssh_target = ssh_target_for_peer(&config, peer)?.to_owned();
    let mut command = Command::new("ssh");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("HERDR_") {
            command.env_remove(key);
        }
    }
    let mut child = command
        .args([
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=yes",
            "--",
            &ssh_target,
            &format!("python3 - {session}"),
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    child
        .stdin
        .take()
        .ok_or("SSH stdin unavailable")?
        .write_all(PROBE.as_bytes())?;
    let pid = child.id();
    let (sender, receiver) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let _ = sender.send(child.wait_with_output());
    });
    let output = match receiver.recv_timeout(Duration::from_secs(12)) {
        Ok(output) => output?,
        Err(_) => {
            unsafe { libc::kill(pid as i32, libc::SIGKILL) };
            return Err("remote herdr probe timed out".into());
        }
    };
    let value: Value = serde_json::from_slice(&output.stdout).map_err(|_| {
        let detail = String::from_utf8_lossy(&output.stderr);
        format!(
            "remote herdr probe returned no identity: {}",
            detail.chars().take(180).collect::<String>()
        )
    })?;
    if !output.status.success() || value["ok"] != true {
        return Err(value["error"]
            .as_str()
            .unwrap_or("remote Hermes terminal is not available")
            .to_owned()
            .into());
    }
    let pane = value["paneId"].as_str().ok_or("remote pane ID missing")?;
    let tab = value["tabId"].as_str().ok_or("remote tab ID missing")?;
    let id_ok = |value: &str, kind: char| {
        value.split_once(':').is_some_and(|(workspace, item)| {
            workspace.starts_with('w')
                && workspace.len() > 1
                && workspace[1..]
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric())
                && item.starts_with(kind)
                && item.len() > 1
                && item[1..].bytes().all(|byte| byte.is_ascii_alphanumeric())
        })
    };
    if !id_ok(pane, 'p') || !id_ok(tab, 't') {
        return Err("unsafe remote pane identity".into());
    }
    let socket = value["socketPath"]
        .as_str()
        .ok_or("remote socket missing")?;
    let binary = value["herdrBin"]
        .as_str()
        .ok_or("remote herdr binary missing")?;
    if !safe_remote_path(socket) || !socket.ends_with("/herdr.sock") || !safe_remote_path(binary) {
        return Err("unsafe remote herdr path".into());
    }
    Ok(json!({
        "roomId":room_id, "tabId":tab, "paneId":pane, "agentName":pane,
        "sessionName":"remote", "sshTarget":ssh_target,
        "remoteSocket":socket, "remoteBin":binary,
        "created":false, "agentStarted":true, "agentError":"", "awaitingBinding":false,
    }))
}

fn parse_activity_report(peer: &str, payload: &Value) -> Vec<Value> {
    if !safe_identity(peer) || payload["ok"] != true {
        return Vec::new();
    }
    let mut seen = std::collections::HashSet::new();
    payload["sessions"]
        .as_array()
        .into_iter()
        .flatten()
        .take(512)
        .filter_map(|item| {
            let profile = item["profile"].as_str()?;
            let session = item["sessionId"].as_str()?;
            let units = item["runningDelegations"].as_u64()?;
            if !safe_identity(profile)
                || !safe_identity(session)
                || !(1..=100).contains(&units)
                || !seen.insert((profile, session))
            {
                return None;
            }
            Some(json!({"peer":peer,"profile":profile,"sessionId":session,"runningDelegations":units}))
        })
        .collect()
}

fn probe_remote_activity(peer: &str, target: &str) -> Option<Vec<Value>> {
    let mut command = Command::new("ssh");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("HERDR_") {
            command.env_remove(key);
        }
    }
    let mut child = command
        .args([
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=4",
            "-o",
            "StrictHostKeyChecking=yes",
            "--",
            target,
            "python3 -",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    child
        .stdin
        .take()?
        .write_all(ACTIVITY_PROBE.as_bytes())
        .ok()?;
    let pid = child.id();
    let (sender, receiver) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let _ = sender.send(child.wait_with_output());
    });
    let output = match receiver.recv_timeout(Duration::from_secs(8)) {
        Ok(Ok(output)) => output,
        _ => {
            unsafe { libc::kill(pid as i32, libc::SIGKILL) };
            return None;
        }
    };
    if !output.status.success() || output.stdout.len() > 128 * 1024 {
        return None;
    }
    let payload: Value = serde_json::from_slice(&output.stdout).ok()?;
    if payload["ok"] != true || !payload["sessions"].is_array() {
        return None;
    }
    Some(parse_activity_report(peer, &payload))
}

/// Non-blocking, bounded read-only observation; HTTP never waits for an SSH peer.
pub fn observed_remote_activity(peers: &[String]) -> Vec<Value> {
    if peers.is_empty() {
        return Vec::new();
    }
    let config_path = home().join(".config/omarchy-ares/ssh-peers.json");
    let Ok(config) = fs::read(&config_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .ok_or(())
    else {
        return Vec::new();
    };
    let cache = ACTIVITY_CACHE.get_or_init(|| Mutex::new(ActivityCache::default()));
    let mut observations = Vec::new();
    for peer in peers.iter().take(8) {
        let Ok(target) = ssh_target_for_peer(&config, peer) else {
            continue;
        };
        let now = Instant::now();
        let (schedule, recent) = match cache.lock() {
            Ok(mut state) => (state.schedule(peer, now), state.snapshot(peer, now)),
            Err(_) => continue,
        };
        observations.extend(recent);
        if schedule {
            let peer = peer.clone();
            let target = target.to_owned();
            thread::spawn(move || {
                let result = probe_remote_activity(&peer, &target);
                if let Some(cache) = ACTIVITY_CACHE.get() {
                    if let Ok(mut state) = cache.lock() {
                        state.complete(&peer, Instant::now(), result);
                    }
                }
            });
        }
    }
    observations
}

#[cfg(test)]
mod activity_tests {
    use super::*;

    #[test]
    fn remote_activity_payload_is_bounded_and_never_trusts_a_claimed_peer() {
        let payload = json!({"ok":true,"sessions":[
            {"profile":"default","sessionId":"session-a","runningDelegations":2,"peer":"forged"},
            {"profile":"default","sessionId":"../bad","runningDelegations":1},
            {"profile":"default","sessionId":"session-b","runningDelegations":0},
        ]});
        let items = parse_activity_report("host", &payload);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0]["peer"], "host");
        assert_eq!(items[0]["sessionId"], "session-a");
        assert_eq!(items[0]["runningDelegations"], 2);
        assert!(
            parse_activity_report("host", &json!({"ok":false,"sessions":payload["sessions"]}))
                .is_empty()
        );
    }

    #[test]
    fn remote_activity_cache_throttles_probes_and_expires_stale_work() {
        let mut cache = ActivityCache::default();
        let start = std::time::Instant::now();
        assert!(cache.schedule("host", start));
        assert!(!cache.schedule("host", start + Duration::from_secs(5)));
        cache.complete(
            "host",
            start + Duration::from_secs(1),
            Some(vec![json!({"sessionId":"one"})]),
        );
        assert_eq!(
            cache
                .snapshot("host", start + Duration::from_secs(25))
                .len(),
            1
        );
        assert!(cache.schedule("host", start + Duration::from_secs(16)));
        assert!(cache
            .snapshot("host", start + Duration::from_secs(32))
            .is_empty());
    }
}
