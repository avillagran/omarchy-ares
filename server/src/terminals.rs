//! Shared local/SSH terminal identity and explicit guarded historical opening.
use crate::{digest, text, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{Read, Write},
    process::{Command, Stdio},
    sync::{mpsc, Arc, Mutex, OnceLock},
    time::{Duration, Instant},
};

type CachedInventory = Arc<Mutex<Option<(Instant, Value)>>>;
static INVENTORIES: OnceLock<Mutex<HashMap<String, CachedInventory>>> = OnceLock::new();
const HISTORICAL_OPEN_TIMEOUT: Duration = Duration::from_secs(120);

/// Short single-flight cache. A failed refresh never serves stale ownership.
pub fn inventory(host: &str, fresh: bool) -> Result<Value> {
    if !safe_id(host) {
        return Err("Invalid terminal host".into());
    }
    if host != "local" {
        // Discovery of a remote owner requires the paired service. An SSH
        // configuration is not evidence of pairing or peer authorization.
        return crate::peer::inventory(host);
    }
    inventory_ssh(host, fresh)
}

/// Explicit legacy/development SSH transport; never selected as a fallback.
pub fn inventory_ssh(host: &str, fresh: bool) -> Result<Value> {
    if !safe_id(host) {
        return Err("Invalid terminal host".into());
    }
    let ssh = if host == "local" {
        String::new()
    } else {
        let config: Value = serde_json::from_slice(
            &std::fs::read(crate::home().join(".config/omarchy-ares/ssh-peers.json"))
                .map_err(|_| "Configure key-based SSH access for this host first")?,
        )?;
        crate::remote::ssh_target_for_peer(&config, host)?.to_owned()
    };
    let cache_key = format!("{host}:{ssh}");
    let slot = {
        let mut cache = INVENTORIES
            .get_or_init(Default::default)
            .lock()
            .map_err(|_| "Terminal cache lock poisoned")?;
        if cache.len() >= 64 && !cache.contains_key(&cache_key) {
            return Err("Too many terminal hosts".into());
        }
        cache.entry(cache_key).or_default().clone()
    };
    let mut cached = slot.lock().map_err(|_| "Terminal cache lock poisoned")?;
    if !fresh {
        if let Some((at, value)) = cached.as_ref() {
            if at.elapsed() < Duration::from_secs(3) {
                return Ok(value.clone());
            }
        }
    }
    *cached = None;
    let mut cmd = if ssh.is_empty() {
        let mut c = Command::new("python3");
        c.args(["-", "--inventory"]);
        c
    } else {
        let mut c = Command::new("ssh");
        c.args([
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=5",
            "--",
            &ssh,
            "python3 - --inventory",
        ]);
        c
    };
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("HERDR_") {
            cmd.env_remove(key);
        }
    }
    let payload = run_python(
        cmd,
        include_str!("../../bin/ares_remote_probe.py"),
        Duration::from_secs(20),
    )?;
    let result = normalize_inventory(host, &ssh, &payload)?;
    *cached = Some((Instant::now(), result.clone()));
    Ok(result)
}

fn opener_source(request: &Value, probe: &str, opener: &str) -> String {
    format!(
        "import json, sys, types\nprobe = types.ModuleType('ares_remote_probe')\nsys.modules['ares_remote_probe'] = probe\nexec(compile({probe}, 'ares_remote_probe.py', 'exec'), probe.__dict__)\nnamespace = {{'__name__': 'ares_terminal_open', 'TERMINAL_SOURCES': {sources}}}\nexec(compile({opener}, 'ares_terminal_open.py', 'exec'), namespace)\nprint(json.dumps(namespace['open_session'](json.loads({request}))))\n",
        probe = json!(probe), opener = json!(opener), request = json!(request.to_string()),
        sources = json!({"ares_remote_probe.py":probe,"ares_terminal_open.py":opener,"ares_terminal_resume.py":include_str!("../../bin/ares_terminal_resume.py")}),
    )
}

pub fn open_history(host: &str, request: &Value) -> Result<Value> {
    if !safe_id(host) {
        return Err("Invalid terminal host".into());
    }
    // `open_history_ssh` uses the exact-resume child acknowledgment and readback
    // contract on the owning host. It is distinct from peer observation/control.
    open_history_ssh(host, request)
}

/// Explicit owner-host SSH transport; gated by the route above.
pub fn open_history_ssh(host: &str, request: &Value) -> Result<Value> {
    if !safe_id(host) {
        return Err("Invalid terminal host".into());
    }
    crate::herdr::validate_target(
        text(request, "roomId"),
        text(request, "profile"),
        text(request, "sessionId"),
    )?;
    let ssh = if host == "local" {
        String::new()
    } else {
        let config: Value = serde_json::from_slice(
            &std::fs::read(crate::home().join(".config/omarchy-ares/ssh-peers.json"))
                .map_err(|_| "Configure key-based SSH access for this host first")?,
        )?;
        crate::remote::ssh_target_for_peer(&config, host)?.to_owned()
    };
    let mut cmd = if ssh.is_empty() {
        let mut c = Command::new("python3");
        c.arg("-");
        c
    } else {
        let mut c = Command::new("ssh");
        c.args([
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=5",
            "--",
            &ssh,
            "python3 -",
        ]);
        c
    };
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("HERDR_") {
            cmd.env_remove(key);
        }
    }
    let source = opener_source(
        request,
        include_str!("../../bin/ares_remote_probe.py"),
        include_str!("../../bin/ares_terminal_open.py"),
    );
    let payload = run_python(cmd, &source, HISTORICAL_OPEN_TIMEOUT).map_err(|_| {
        "Historical opening outcome is unconfirmed; inspect the host before retrying"
    })?;
    if payload["ok"] != true {
        let message = text(&payload, "error");
        return Err(if message.is_empty() {
            "Historical session could not be opened safely".to_owned()
        } else {
            crate::truncate(message, 800)
        }
        .into());
    }
    normalize_inventory(host, &ssh, &payload)
}

fn run_python(mut cmd: Command, source: &str, timeout: Duration) -> Result<Value> {
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let pid = child.id();

    let write_result = child
        .stdin
        .take()
        .ok_or("Probe stdin unavailable")?
        .write_all(source.as_bytes());
    if write_result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return Err("Herdr inventory transport failed".into());
    }
    let output = child.stdout.take().ok_or("Probe stdout unavailable")?;
    let (tx, rx) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let read = output.take(2 * 1024 * 1024 + 1).read_to_end(&mut bytes);
        if bytes.len() > 2 * 1024 * 1024 {
            let _ = child.kill();
        }
        let status = child.wait();
        let _ = tx.send((read, status, bytes));
    });
    let (read, status, bytes) = match rx.recv_timeout(timeout) {
        Ok(value) => value,
        Err(_) => {
            unsafe {
                libc::kill(pid as i32, libc::SIGKILL);
            }
            return Err("Herdr inventory transport timed out".into());
        }
    };
    read?;
    if !status?.success() || bytes.len() > 2 * 1024 * 1024 {
        return Err("Herdr inventory transport failed".into());
    }
    Ok(serde_json::from_slice(&bytes)?)
}

fn safe_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
fn safe_path(value: &str) -> bool {
    value.starts_with('/')
        && value.len() <= 240
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b))
        && !value.split('/').any(|part| part == ".." || part == ".")
}
fn pane_id(value: &str, kind: char) -> bool {
    value.split_once(':').is_some_and(|(ws, id)| {
        ws.starts_with('w')
            && ws.len() > 1
            && ws[1..].bytes().all(|b| b.is_ascii_alphanumeric())
            && id.starts_with(kind)
            && id.len() > 1
            && id[1..].bytes().all(|b| b.is_ascii_alphanumeric())
    })
}

pub fn normalize_inventory(host: &str, ssh_target: &str, payload: &Value) -> Result<Value> {
    let transport = if host == "local" {
        if !ssh_target.is_empty() {
            return Err("Local inventory cannot have an SSH target".into());
        }
        InventoryTransport::Local
    } else {
        crate::remote::validate_ssh_target(ssh_target)?;
        InventoryTransport::Ssh(ssh_target)
    };
    normalize_with_transport(host, transport, payload)
}

enum InventoryTransport<'a> {
    Local,
    Ssh(&'a str),
    Peer,
}

/// A peer's authenticated alias identifies the source; no SSH identity exists.
pub fn normalize_peer_inventory(host: &str, payload: &Value) -> Result<Value> {
    if host == "local" {
        return Err("Peer inventory requires a remote alias".into());
    }
    normalize_with_transport(host, InventoryTransport::Peer, payload)
}

fn normalize_with_transport(
    host: &str,
    transport: InventoryTransport<'_>,
    payload: &Value,
) -> Result<Value> {
    if !safe_id(host)
        || (matches!(transport, InventoryTransport::Peer)
            && (payload["complete"] != true
                || payload.get("ok") == Some(&json!(false))
                || payload["errors"]
                    .as_array()
                    .is_some_and(|errors| !errors.is_empty())))
        || (!matches!(transport, InventoryTransport::Peer) && payload["ok"] != true)
    {
        return Err("Herdr inventory unavailable".into());
    }
    let raw = payload["terminals"]
        .as_array()
        .ok_or("Herdr inventory incomplete")?;
    if raw.len() > 8192 {
        return Err("Herdr inventory exceeds limit".into());
    }
    let mut terminals = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for item in raw {
        if !pane_id(text(item, "paneId"), 'p')
            || !pane_id(text(item, "tabId"), 't')
            || !safe_id(text(item, "terminalId"))
            || !safe_id(text(item, "agentKind"))
            || !safe_path(text(item, "socketPath"))
            || (!matches!(transport, InventoryTransport::Peer)
                && (!text(item, "socketPath").ends_with("/herdr.sock")
                    || !safe_path(text(item, "herdrBin"))))
        {
            return Err("Invalid Herdr terminal identity".into());
        }
        let transport_id = match transport {
            InventoryTransport::Local => "".to_owned(),
            InventoryTransport::Ssh(target) => target.to_owned(),
            InventoryTransport::Peer => format!("peer:{host}"),
        };
        let id = digest(
            json!([
                host,
                transport_id,
                item["socketPath"],
                item["terminalId"],
                item["agentKind"]
            ])
            .to_string(),
        );
        if !seen.insert(id.clone()) {
            return Err("Duplicate Herdr terminal identity".into());
        }
        // Copy only the public discovery contract, never arbitrary remote fields.
        let mut terminal = match transport {
            InventoryTransport::Local => {
                json!({"id":id,"host":host,"transport":"local","sshTarget":""})
            }
            InventoryTransport::Ssh(target) => {
                json!({"id":id,"host":host,"transport":"ssh","sshTarget":target})
            }
            InventoryTransport::Peer => {
                json!({"id":id,"host":host,"transport":"peer","peer":host,"readOnly":true})
            }
        };
        for key in [
            "paneId",
            "tabId",
            "terminalId",
            "agentKind",
            "sessionId",
            "profile",
            "identitySource",
            "status",
            "workspace",
            "label",
            "herdrSession",
        ] {
            terminal[key] = json!(crate::truncate(text(item, key), 240));
        }
        if !matches!(transport, InventoryTransport::Peer) {
            terminal["socketPath"] = item["socketPath"].clone();
            terminal["herdrBin"] = item["herdrBin"].clone();
        } else if text(item, "identitySource") != "verified"
            || text(item, "sessionId").is_empty()
            || text(item, "profile").is_empty()
        {
            // The source may observe a pane without verifying a session owner.
            terminal["sessionId"] = json!("");
            terminal["profile"] = json!("");
            terminal["identitySource"] = json!("observed-only");
        }
        terminals.push(terminal);
    }
    Ok(
        json!({"host":host,"terminals":terminals,"errors":if matches!(transport, InventoryTransport::Peer) { json!([]) } else { payload["errors"].clone() },"complete":payload["complete"] != false && (matches!(transport, InventoryTransport::Peer) || payload["errors"].as_array().is_some_and(|errors| errors.is_empty()))}),
    )
}

pub fn select_session(
    inventory: &Value,
    kind: &str,
    profile: &str,
    session: &str,
) -> Result<Option<Value>> {
    if session.is_empty() {
        return Ok(None);
    }
    if inventory["complete"] == false
        || !inventory["errors"]
            .as_array()
            .is_some_and(|errors| errors.is_empty())
    {
        return Err("Herdr inventory is incomplete; choose an observed terminal explicitly".into());
    }
    let matches: Vec<_> = inventory["terminals"]
        .as_array()
        .ok_or("Herdr inventory incomplete")?
        .iter()
        .filter(|t| t["agentKind"] == kind && t["profile"] == profile && t["sessionId"] == session)
        .collect();
    if matches.len() > 1 {
        return Err("Ambiguous Herdr session owner; choose its terminal explicitly".into());
    }
    Ok(matches.first().map(|t| (*t).clone()))
}

/// A group is not one agent. Only a separately identified, live member can
/// expose a terminal; unknown connections and stale catalog rows fail closed.
pub fn group_member_identity(
    store: &crate::store::Store,
    room: &str,
    member_key: &str,
) -> Result<Value> {
    let row = store.room(room, false)?;
    if text(&row, "source") != "hermes-native-group" || text(&row, "status") != "active" {
        return Err("Active native bot group required".into());
    }
    let group = store.native_group_dto(room)?;
    let members: Vec<_> = group["members"]
        .as_array()
        .ok_or("Group members unavailable")?
        .iter()
        .filter(|m| text(m, "memberKey") == member_key)
        .collect();
    if member_key.is_empty() || members.len() != 1 {
        return Err("Group member identity is missing or ambiguous".into());
    }
    if text(&group, "authorityHost") != "local" {
        return Err("Group authority is not local".into());
    }
    let member = members[0];
    // Native discovery currently verifies only the local authority. No
    // connectionId-to-host inference or remote Desktop fallback is permitted.
    if member["resolution"] != "resolved"
        || member["connectionKind"] != "local"
        || member["connectionId"] != "local"
        || member["host"] != "local"
        || text(member, "profile").is_empty()
        || text(member, "storedSessionId").is_empty()
        || text(member, "resolvedSessionId").is_empty()
    {
        return Err("Group member has no verified local session identity".into());
    }
    Ok(member.clone())
}

pub fn resolve_group_member(room: &str, member: &Value, inventory: &Value) -> Result<Value> {
    if inventory["host"] != "local" {
        return Err("Group member terminal host mismatch".into());
    }
    let profile = text(member, "profile");
    let stored = text(member, "storedSessionId");
    let resolved = text(member, "resolvedSessionId");
    if text(member, "host") != "local"
        || member["resolution"] != "resolved"
        || profile.is_empty()
        || stored.is_empty()
        || resolved.is_empty()
    {
        return Err("Group member identity is unresolved".into());
    }
    // A compressed session may use its stored or resolved tip ID. Both IDs
    // come from the exact member catalog, and two live owners are ambiguous.
    let mut matches = Vec::new();
    for (index, session) in [stored, resolved].into_iter().enumerate() {
        if index == 1 && stored == resolved {
            continue;
        }
        if let Some(target) = select_session(inventory, "hermes", profile, session)? {
            if !matches
                .iter()
                .any(|item: &Value| item["id"] == target["id"])
            {
                matches.push(target);
            }
        }
    }
    if matches.len() != 1 {
        return Err(if matches.is_empty() {
            "No verified Herdr terminal for this group member"
        } else {
            "Ambiguous Herdr terminals for this group member"
        }
        .into());
    }
    // Never treat guessed pane metadata as a verified member association.
    if text(&matches[0], "identitySource") != "registry" {
        return Err("Group member terminal identity was not observed in the registry".into());
    }
    let mut tty = descriptor(room, &matches[0]);
    tty["memberKey"] = member["memberKey"].clone();
    tty["readOnly"] = json!(true);
    Ok(tty)
}

pub fn room_host(store: &crate::store::Store, room: &str) -> Result<String> {
    if matches!(
        text(&store.room(room, false)?, "source"),
        "hermes-native-group" | "hermes-native-member"
    ) {
        return Err("Native bot groups have separate member sessions, not one terminal".into());
    }
    let row = store.one("SELECT p.node_id FROM rooms r JOIN projects p ON p.id=r.project_id WHERE r.id=? AND r.deleted_at IS NULL AND r.source!='control'", [room], "room not found")?;
    Ok(text(&row, "node_id").to_owned())
}

pub fn descriptor(room: &str, target: &Value) -> Value {
    let mut tty = json!({"roomId":room,"sessionName":"terminal","agentName":target["terminalId"],
        "paneId":target["paneId"],"tabId":target["tabId"],"terminalId":target["terminalId"],
        "agentKind":target["agentKind"],"host":target["host"],"sshTarget":target["sshTarget"],
        "remoteBin":target["herdrBin"],"remoteSocket":target["socketPath"],
        "agentStarted":true,"created":false,"awaitingBinding":false,"agentError":"",
        "connectionState":"resolved","transport":target["transport"]});
    if target["transport"] == "peer" {
        // Inventory cannot grant control or silently become an SSH attachment.
        tty.as_object_mut().unwrap().remove("sshTarget");
        tty.as_object_mut().unwrap().remove("remoteBin");
        tty.as_object_mut().unwrap().remove("remoteSocket");
        tty["transport"] = json!("peer");
        tty["peer"] = target["peer"].clone();
        tty["readOnly"] = json!(true);
        tty["agentStarted"] = json!(false);
        tty["connectionState"] = json!("inventory-only");
    }
    tty
}

/// Opening a historical chat is distinct from read-only inventory/attachment.
pub fn historical_open_request(store: &crate::store::Store, room: &str) -> Result<Value> {
    let host = room_host(store, room)?;
    let row = store.room(room, false)?;
    if text(&row, "status") != "active" || text(&row, "source") != "hermes" {
        return Err("Only an active stored Hermes chat can be opened in a new terminal".into());
    }
    if binding(store, room)?.is_some() {
        return Err("This room already has an explicit terminal association".into());
    }
    let pending: bool = store.db.query_row(
        "SELECT EXISTS(SELECT 1 FROM executions WHERE room_id=? AND status IN ('queued','running'))",
        [room], |row| row.get(0),
    )?;
    if pending {
        return Err("Wait for pending room executions before opening its terminal".into());
    }
    let profile_ref = text(&row, "external_profile");
    let session_ref = text(&row, "external_session_id");
    let (profile, session) = if host == "local" {
        (profile_ref, session_ref)
    } else {
        let profile = profile_ref
            .strip_prefix(&format!("remote::{host}::"))
            .ok_or("Stored profile does not belong to this host")?;
        (
            profile,
            crate::remote::parse_remote_session(session_ref, &host, profile)
                .ok_or("Stored session does not belong to this host")?,
        )
    };
    if session.is_empty() {
        return Err("This room has no stored session to resume".into());
    }
    crate::herdr::validate_target(room, profile, session)?;
    let title = if text(&row, "alias").is_empty() {
        text(&row, "name")
    } else {
        text(&row, "alias")
    };
    Ok(json!({"roomId":room,"profile":profile,"sessionId":session,"title":title}))
}

pub enum RoomResolution {
    Attached(Value),
    Open { host: String, request: Value },
}

/// Prepare under the store lock; slow host work must run after releasing it.
pub fn prepare_room(
    store: &crate::store::Store,
    room: &str,
    inventory: &Value,
    open_if_missing: bool,
) -> Result<RoomResolution> {
    if let Some(target) = resolve_room(store, room, inventory)? {
        return Ok(RoomResolution::Attached(
            json!({"tty":descriptor(room, &target),"focus":null}),
        ));
    }
    let host = room_host(store, room)?;
    let row = store.room(room, false)?;
    let has_session: bool = store.db.query_row(
        "SELECT EXISTS(SELECT 1 FROM session_bindings WHERE room_id=? AND stored_session_id!='')",
        [room],
        |row| row.get(0),
    )?;
    if host == "local"
        && text(&row, "source") == "ares"
        && !has_session
        && inventory["complete"] == true
    {
        return Ok(RoomResolution::Attached(
            json!({"tty":{"roomId":room,"awaitingBinding":true,"agentStarted":false,"created":false},"focus":null}),
        ));
    }
    if !open_if_missing {
        return Err("No verified Herdr terminal is attached. Historical opening is disabled; choose an existing terminal.".into());
    }
    let request = historical_open_request(store, room)?;
    Ok(RoomResolution::Open { host, request })
}

pub fn verify_opened_room(
    store: &crate::store::Store,
    room: &str,
    host: &str,
    request: &Value,
    opened: &Value,
) -> Result<Value> {
    if room_host(store, room)? != host || historical_open_request(store, room)? != *request {
        return Err("Room changed during opening; inspect its terminal before retrying".into());
    }
    let target = resolve_room(store, room, opened)?
        .ok_or("Session start is unconfirmed; no verified terminal identity was returned. Do not retry automatically.")?;
    Ok(json!({"tty":descriptor(room, &target),"focus":null}))
}

pub fn resolve_or_open(
    store: &crate::store::Store,
    room: &str,
    inventory: &Value,
    open_if_missing: bool,
    open: impl FnOnce(&str, &Value) -> Result<Value>,
) -> Result<Value> {
    match prepare_room(store, room, inventory, open_if_missing)? {
        RoomResolution::Attached(value) => Ok(value),
        RoomResolution::Open { host, request } => {
            let opened = open(&host, &request)?;
            verify_opened_room(store, room, &host, &request, &opened)
        }
    }
}

pub fn binding(store: &crate::store::Store, room: &str) -> Result<Option<Value>> {
    room_host(store, room)?;
    Ok(store
        .rows("SELECT * FROM terminal_bindings WHERE room_id=?", [room])?
        .into_iter()
        .next())
}

pub fn resolve_room(
    store: &crate::store::Store,
    room: &str,
    inventory: &Value,
) -> Result<Option<Value>> {
    let host = room_host(store, room)?;
    if inventory["host"] != host {
        return Err("Terminal inventory host mismatch".into());
    }
    if let Some(target) = resolve_bound(store, room, inventory)? {
        return Ok(Some(target));
    }
    let row = store.room(room, false)?;
    if text(&row, "source") == "ares" {
        let identities = store.rows("SELECT DISTINCT s.stored_session_id,b.profile FROM session_bindings s JOIN bots b ON b.id=s.bot_id AND b.node_id=s.node_id WHERE s.room_id=? AND s.node_id=? AND s.stored_session_id!=''", rusqlite::params![room,host])?;
        if identities.len() > 1 {
            return Err("Room has several stored owners; choose its terminal explicitly".into());
        }
        return match identities.first() {
            Some(identity) => select_session(
                inventory,
                "hermes",
                text(identity, "profile"),
                text(identity, "stored_session_id"),
            ),
            None => Ok(None),
        };
    }
    if text(&row, "source") != "hermes" {
        return Ok(None);
    }
    let profile_ref = text(&row, "external_profile");
    let session_ref = text(&row, "external_session_id");
    let (profile, session) = if host == "local" {
        if profile_ref.starts_with("remote::") {
            return Err("Remote chat cannot attach on the local host".into());
        }
        (profile_ref, session_ref)
    } else {
        let profile = profile_ref
            .strip_prefix(&format!("remote::{host}::"))
            .ok_or("Remote profile does not match this host")?;
        (
            profile,
            crate::remote::parse_remote_session(session_ref, &host, profile)
                .ok_or("Remote session does not match this host")?,
        )
    };
    select_session(inventory, "hermes", profile, session)
}

pub fn resolve_bound(
    store: &crate::store::Store,
    room: &str,
    inventory: &Value,
) -> Result<Option<Value>> {
    if inventory["complete"] != true
        || !inventory["errors"]
            .as_array()
            .is_some_and(|errors| errors.is_empty())
    {
        return Err("Herdr inventory is incomplete; cannot verify terminal association".into());
    }
    let Some(saved) = binding(store, room)? else {
        return Ok(None);
    };
    let host = room_host(store, room)?;
    if saved["host"] != host || inventory["host"] != host {
        return Err("Terminal binding belongs to a different host".into());
    }
    let target = inventory["terminals"]
        .as_array()
        .ok_or("Herdr inventory incomplete")?
        .iter()
        .find(|t| t["id"] == saved["terminal_key"])
        .ok_or("The associated terminal is unavailable; choose its current Herdr terminal")?;
    verify_room_owner(store, room, inventory, target, &host)?;
    Ok(Some(target.clone()))
}

fn verify_room_owner(
    store: &crate::store::Store,
    room: &str,
    inventory: &Value,
    target: &Value,
    host: &str,
) -> Result<()> {
    if room_host(store, room)? != host
        || target["host"] != host
        || !(target["identitySource"] == "verified"
            || (host == "local" && target["identitySource"] == "registry"))
    {
        return Err("Terminal has no verified owner on the room host".into());
    }
    let row = store.room(room, false)?;
    let (profile, session) = if text(&row, "source") == "hermes" {
        let profile_ref = text(&row, "external_profile");
        let session_ref = text(&row, "external_session_id");
        if host == "local" {
            if profile_ref.starts_with("remote::") {
                return Err("Remote profile does not belong to local room".into());
            }
            (profile_ref.to_owned(), session_ref.to_owned())
        } else {
            let profile = profile_ref
                .strip_prefix(&format!("remote::{host}::"))
                .ok_or("Stored profile does not belong to room host")?;
            let session = crate::remote::parse_remote_session(session_ref, host, profile)
                .ok_or("Stored session does not belong to room host")?;
            (profile.to_owned(), session.to_owned())
        }
    } else if text(&row, "source") == "ares" {
        let identities = store.rows("SELECT DISTINCT s.stored_session_id,b.profile FROM session_bindings s JOIN bots b ON b.id=s.bot_id AND b.node_id=s.node_id WHERE s.room_id=? AND s.node_id=? AND s.stored_session_id!=''", rusqlite::params![room,host])?;
        if identities.len() != 1 {
            return Err("Room needs exactly one stored session owner".into());
        }
        (
            text(&identities[0], "profile").to_owned(),
            text(&identities[0], "stored_session_id").to_owned(),
        )
    } else {
        return Err("Room has no stored session owner".into());
    };
    if profile.is_empty()
        || session.is_empty()
        || target["agentKind"] != "hermes"
        || target["profile"] != profile
        || target["sessionId"] != session
        || inventory["terminals"]
            .as_array()
            .ok_or("Herdr inventory incomplete")?
            .iter()
            .filter(|t| {
                t["host"] == host
                    && t["agentKind"] == "hermes"
                    && t["profile"] == profile
                    && t["sessionId"] == session
            })
            .count()
            != 1
    {
        return Err("Terminal does not uniquely match the room's verified session owner".into());
    }
    Ok(())
}

pub fn bind(
    store: &crate::store::Store,
    room: &str,
    inventory: &Value,
    candidate: &str,
    confirmed: bool,
) -> Result<Value> {
    if !confirmed {
        return Err("Explicit terminal association confirmation required".into());
    }
    if inventory["complete"] != true
        || !inventory["errors"]
            .as_array()
            .is_some_and(|errors| errors.is_empty())
    {
        return Err("Herdr inventory is incomplete; cannot associate terminal".into());
    }
    let host = room_host(store, room)?;
    if inventory["host"] != host {
        return Err("Terminal belongs to a different host".into());
    }
    let matches: Vec<_> = inventory["terminals"]
        .as_array()
        .ok_or("Herdr inventory incomplete")?
        .iter()
        .filter(|t| t["id"] == candidate)
        .collect();
    if candidate.is_empty() || matches.len() != 1 {
        return Err("Terminal changed or ambiguous; refresh the Herdr inventory".into());
    }
    let target = matches[0];
    store.transaction(|s| {
        verify_room_owner(s, room, inventory, target, &host)?;
        if s.db.query_row("SELECT EXISTS(SELECT 1 FROM executions WHERE room_id=? AND status IN ('queued','running'))",[room],|row| row.get::<_,bool>(0))? {
            return Err("Wait for pending room executions before associating a terminal".into());
        }
        s.db.execute("INSERT INTO terminal_bindings(room_id,host,terminal_key,agent_kind,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET host=excluded.host,terminal_key=excluded.terminal_key,agent_kind=excluded.agent_kind,updated_at=excluded.updated_at",
            rusqlite::params![room,host,candidate,text(target,"agentKind"),crate::now()])?;
        let r = s.room(room, false)?;
        s.append_event(text(&r,"project_id"), room, None, "terminal.associated", "ares", json!({"host":host,"terminalId":target["terminalId"],"agentKind":target["agentKind"]}))?;
        Ok(())
    })?;
    // Return the persisted row, not the submitted form.
    binding(store, room)?.ok_or_else(|| "Terminal binding did not persist".into())
}

#[cfg(test)]
mod opening_tests {
    use super::*;

    #[test]
    fn historical_transport_outlives_the_child_ack_deadline() {
        assert!(HISTORICAL_OPEN_TIMEOUT > Duration::from_secs(70));
    }

    #[test]
    fn owner_bootstrap_roundtrips_request_as_data_without_shell_interpolation() {
        let request = json!({"roomId":"r","profile":"default","sessionId":"s","title":"quote '\"\n$(false); None true café"});
        let source = opener_source(&request, "marker = 7\n", "import ares_remote_probe\ndef open_session(request):\n    assert ares_remote_probe.marker == 7\n    assert set(TERMINAL_SOURCES) == {'ares_remote_probe.py','ares_terminal_open.py','ares_terminal_resume.py'}\n    return request\n");
        let mut cmd = Command::new("python3");
        cmd.arg("-");
        let actual = run_python(cmd, &source, Duration::from_secs(3)).unwrap();
        assert_eq!(actual, request);
    }
}
