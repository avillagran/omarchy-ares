//! herdr terminal supervisor integration.
//!
//! Ares-created terminals share one herdr session (`ares`), one tab per room.
//! Existing Hermes chats already hosted by the default herdr session reuse
//! their original panes instead of starting a second agent. This module owns
//! the session-server lifecycle and tab/agent operations over
//! the herdr socket API. Every spawned process is scrubbed of `HERDR_*` so
//! nested-pane detection never misfires, and the server is started detached
//! (setsid) so it survives engine restarts.
use crate::{home, Result};
use serde_json::{json, Value};
use std::{
    env,
    io::{BufRead, BufReader, Read, Write},
    os::unix::fs::FileTypeExt,
    os::unix::net::UnixStream,
    os::unix::process::CommandExt,
    path::PathBuf,
    process::{Command, Stdio},
    sync::{mpsc, Mutex, OnceLock},
    thread,
    time::{Duration, Instant},
};

pub const SESSION: &str = "ares";
const HERDR: &str = "herdr";
const LABEL_MAX: usize = 32;
const AGENT_START_TIMEOUT_MS: &str = "30000";

fn herdr_config_dir() -> PathBuf {
    env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home().join(".config"))
        .join("herdr")
}

pub fn socket_path(session: &str) -> PathBuf {
    herdr_config_dir()
        .join("sessions")
        .join(session)
        .join("herdr.sock")
}

fn default_socket_path() -> PathBuf {
    herdr_config_dir().join("herdr.sock")
}

/// Room labels become herdr tab labels and agent names. Keep them printable,
/// single-line, and short enough for a tab bar.
pub fn sanitize_label(label: &str) -> String {
    let cleaned: String = label
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let trimmed = cleaned.trim();
    if trimmed.is_empty() {
        return "room".to_owned();
    }
    trimmed.chars().take(LABEL_MAX).collect()
}

// Names and labels carry an immutable room-ID digest; a title is never an identity.
fn agent_name(room_id: &str) -> String {
    format!("ares-{}", &crate::digest(room_id)[..24])
}

fn room_tab_label(room_id: &str, title: &str) -> String {
    let suffix = format!(" [{}]", &crate::digest(room_id)[..12]);
    let prefix: String = sanitize_label(title)
        .chars()
        .take(LABEL_MAX - suffix.chars().count())
        .collect();
    format!("{}{}", prefix.trim_end(), suffix)
}

pub fn validate_target(room_id: &str, profile: &str, resume: &str) -> Result<()> {
    let safe = |s: &str| {
        !s.is_empty()
            && s.len() <= 128
            && s.bytes().next().is_some_and(|b| b.is_ascii_alphanumeric())
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    };
    if !safe(room_id)
        || !safe(profile)
        || (!resume.is_empty() && (!safe(resume) || resume == "latest"))
    {
        return Err("unsafe room, profile, or stored session identifier".into());
    }
    Ok(())
}

fn command(program: &str) -> Command {
    let mut cmd = Command::new(program);
    for (key, _) in env::vars_os() {
        if key.to_string_lossy().starts_with("HERDR_") {
            cmd.env_remove(&key);
        }
    }
    cmd
}

fn run(args: &[&str]) -> Result<Value> {
    run_on_socket(args, socket_path(SESSION))
}

fn run_on_socket(args: &[&str], socket: PathBuf) -> Result<Value> {
    run_on_socket_program(HERDR, args, socket)
}

fn run_on_socket_program(program: &str, args: &[&str], socket: PathBuf) -> Result<Value> {
    let mut cmd = command(program);
    cmd.env("HERDR_SOCKET_PATH", socket);
    cmd.args(args);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = cmd.spawn()?;
    let pid = child.id();
    let (sender, receiver) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let _ = sender.send(child.wait_with_output());
    });
    let timeout = if args.starts_with(&["agent", "start"]) {
        Duration::from_secs(35)
    } else {
        Duration::from_secs(2)
    };
    let output = match receiver.recv_timeout(timeout) {
        Ok(result) => result?,
        Err(_) => {
            // Only kill the CLI child we started, never a herdr session server.
            unsafe { libc::kill(pid as i32, libc::SIGKILL) };
            let _ = receiver.recv_timeout(Duration::from_secs(1));
            return Err(format!("herdr {} timed out", args.join(" ")).into());
        }
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(format!("herdr {} failed: {detail}", args.join(" ")).into());
    }
    let value: Value = serde_json::from_str(stdout.trim())
        .map_err(|e| format!("herdr returned invalid JSON for {}: {e}", args.join(" ")))?;
    if let Some(error) = value.get("error") {
        return Err(format!("herdr {}: {error}", args.join(" ")).into());
    }
    Ok(value)
}

pub fn server_running() -> bool {
    socket_path(SESSION).exists() && run(&["workspace", "list"]).is_ok()
}

/// Starts the named-session server when absent. Returns true when it spawned.
pub fn ensure_server() -> Result<bool> {
    if server_running() {
        return Ok(false);
    }
    if socket_path(SESSION).exists() {
        return Err(
            "herdr ares socket exists but is unresponsive; refusing to spawn a second server"
                .into(),
        );
    }
    let mut cmd = command(HERDR);
    cmd.args(["--session", SESSION, "server"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    unsafe {
        cmd.pre_exec(|| {
            if libc::setsid() < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    cmd.spawn()?;
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if server_running() {
            return Ok(true);
        }
        thread::sleep(Duration::from_millis(250));
    }
    Err("herdr session server did not start".into())
}

fn snapshot() -> Result<Value> {
    snapshot_on_socket(socket_path(SESSION))
}

fn snapshot_on_socket(socket: PathBuf) -> Result<Value> {
    let value = run_on_socket(&["api", "snapshot"], socket)?;
    let snapshot = value["result"]["snapshot"].clone();
    if !snapshot["tabs"].is_array()
        || !snapshot["panes"].is_array()
        || !snapshot["agents"].is_array()
    {
        return Err("herdr snapshot is incomplete".into());
    }
    Ok(snapshot)
}

/// Observe agents already owned by the user's default Herdr session. Never start one.
pub fn default_agents() -> Vec<Value> {
    if !default_socket_path().exists() {
        return Vec::new();
    }
    snapshot_on_socket(default_socket_path())
        .ok()
        .and_then(|value| value["agents"].as_array().cloned())
        .unwrap_or_default()
}

/// Reuse a Hermes terminal already running in the default herdr session.
/// Its pane identity, not its title, identifies the process. Never focus or
/// start an agent in someone else's herdr session.
fn find_external_agent(state: &Value, resume: &str) -> Result<Option<Value>> {
    let matching: Vec<_> = state["agents"]
        .as_array()
        .ok_or("herdr agents missing")?
        .iter()
        .filter(|agent| {
            agent["agent"] == "hermes"
                && agent["agent_session"]["kind"] == "id"
                && agent["agent_session"]["value"] == resume
        })
        .collect();
    if matching.len() > 1 {
        return Err("multiple herdr agents claim the same Hermes session".into());
    }
    let Some(agent) = matching.first() else {
        return Ok(None);
    };
    let pane = agent["pane_id"].as_str().ok_or("agent pane is missing")?;
    let tab = agent["tab_id"].as_str().ok_or("agent tab is missing")?;
    let valid_id = |value: &str, kind: &str| {
        value.split_once(':').is_some_and(|(workspace, id)| {
            workspace.len() > 1
                && workspace.starts_with('w')
                && workspace[1..].bytes().all(|b| b.is_ascii_alphanumeric())
                && id.len() > kind.len()
                && id.starts_with(kind)
                && id[kind.len()..].bytes().all(|b| b.is_ascii_alphanumeric())
        })
    };
    if !valid_id(pane, "p") || !valid_id(tab, "t") {
        return Err("unsafe herdr pane or tab identifier".into());
    }
    let actual_tab = state["tabs"]
        .as_array()
        .and_then(|tabs| tabs.iter().find(|t| t["tab_id"] == tab))
        .ok_or("agent tab is missing")?;
    if !state["panes"].as_array().is_some_and(|panes| {
        panes
            .iter()
            .any(|p| p["pane_id"] == pane && p["tab_id"] == tab)
    }) {
        return Err("agent pane does not belong to its tab".into());
    }
    Ok(Some(json!({
        "tabId": tab, "paneId": pane, "agentName": pane,
        "sessionName": "default", "label": actual_tab["label"],
        "created": false, "agentStarted": true, "agentError": "",
        "awaitingBinding": false, "restoring": false,
    })))
}

/// Herdr may eventually expose a structured composer state. Until then, allow
/// submission only after the user has reviewed the unmasked terminal input.
struct PromptAgent {
    pane: String,
    terminal_id: String,
}

fn select_prompt_agent(
    state: &Value,
    resume: &str,
    user_confirmed_empty: bool,
) -> Result<PromptAgent> {
    let tty = find_external_agent(state, resume)?
        .ok_or("no existing local Herdr owner for this session")?;
    let pane_id = text_id(&tty, "paneId")?;
    let agent = state["agents"]
        .as_array()
        .ok_or("herdr agents missing")?
        .iter()
        .find(|a| a["pane_id"] == pane_id && a["agent_session"]["value"] == resume)
        .ok_or("Herdr owner disappeared")?;
    if !matches!(
        agent["agent_status"].as_str(),
        Some("idle" | "done" | "working")
    ) {
        return Err("TTY owner is blocked or has unknown status".into());
    }
    let terminal_id = text_id(agent, "terminal_id")?;
    if terminal_id.is_empty() {
        return Err("Herdr owner terminal identifier is missing".into());
    }
    let pane = state["panes"]
        .as_array()
        .ok_or("herdr panes missing")?
        .iter()
        .find(|p| p["pane_id"] == pane_id && p["tab_id"] == tty["tabId"])
        .ok_or("Herdr pane is missing")?;
    if pane["composer_empty"] != true
        && !(pane.get("composer_empty").is_none() && user_confirmed_empty)
    {
        return Err("Cannot verify empty TTY composer; prompt was not sent".into());
    }
    Ok(PromptAgent {
        pane: pane_id.to_owned(),
        terminal_id: terminal_id.to_owned(),
    })
}

fn text_id<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .ok_or_else(|| "Herdr owner identifier is missing".into())
}

fn prompt_verified(socket: PathBuf, pane: &str, terminal_id: &str, text: &str) -> Result<()> {
    const ID: &str = "ares:agent:prompt";
    const UNCERTAIN: &str = "Herdr prompt outcome is uncertain; do not retry automatically";
    let response = (|| -> Result<Value> {
        let mut stream = UnixStream::connect(socket)?;
        stream.set_read_timeout(Some(Duration::from_secs(2)))?;
        stream.set_write_timeout(Some(Duration::from_secs(2)))?;
        serde_json::to_writer(
            &mut stream,
            &json!({
                "id": ID, "method": "agent.prompt",
                "params": {"target": pane, "text": text, "wait": null}
            }),
        )?;
        stream.write_all(b"\n")?;
        let mut line = Vec::new();
        BufReader::new(stream.take(65537)).read_until(b'\n', &mut line)?;
        if line.len() > 65536 || line.last() != Some(&b'\n') {
            return Err(UNCERTAIN.into());
        }
        Ok(serde_json::from_slice(&line)?)
    })();
    let value = match response {
        Ok(value) => value,
        Err(_) => return Err(UNCERTAIN.into()),
    };
    if value["id"] != ID
        || value["result"]["type"] != "agent_prompted"
        || value["result"]["agent"]["terminal_id"] != terminal_id
        || value["ok"] == false
        || value["accepted"] == false
        || value.get("error").is_some()
        || value["result"]["ok"] == false
        || value["result"]["accepted"] == false
        || value["result"].get("error").is_some()
    {
        return Err(UNCERTAIN.into());
    }
    Ok(())
}

pub struct PreparedPrompt {
    socket: PathBuf,
    pane: String,
    terminal_id: String,
}

/// Inspect existing sockets only; never create or focus an agent.
pub fn prepare_prompt(
    room_id: &str,
    profile: &str,
    resume: &str,
    user_confirmed_empty: bool,
) -> Result<PreparedPrompt> {
    validate_target(room_id, profile, resume)?;
    if resume.is_empty() {
        return Err("TTY room has no stored Hermes session".into());
    }
    let inventory = crate::terminals::inventory("local", true)?;
    let target = crate::terminals::select_session(&inventory, "hermes", profile, resume)?
        .ok_or("TTY owner is absent or ambiguous")?;
    let state = snapshot_on_socket(PathBuf::from(text_id(&target, "socketPath")?))?;
    prepare_inventory_prompt(&inventory, &state, profile, resume, user_confirmed_empty)
}

fn prepare_inventory_prompt(
    inventory: &Value,
    state: &Value,
    profile: &str,
    resume: &str,
    confirmed: bool,
) -> Result<PreparedPrompt> {
    if inventory["host"] != "local" {
        return Err("Remote prompt transport is not verified".into());
    }
    let target = crate::terminals::select_session(inventory, "hermes", profile, resume)?
        .ok_or("TTY owner is absent or ambiguous")?;
    let agent = select_prompt_agent(state, resume, confirmed)?;
    if target["paneId"] != agent.pane || target["terminalId"] != agent.terminal_id {
        return Err("Terminal instance changed; draft was not sent".into());
    }
    let claim = state["agents"]
        .as_array()
        .and_then(|agents| {
            agents
                .iter()
                .find(|item| item["terminal_id"] == agent.terminal_id)
        })
        .ok_or("Terminal disappeared")?;
    if claim["agent_session"]["profile"]
        .as_str()
        .is_some_and(|p| !p.is_empty() && p != profile)
    {
        return Err("Terminal profile changed; draft was not sent".into());
    }
    Ok(PreparedPrompt {
        socket: PathBuf::from(text_id(&target, "socketPath")?),
        pane: agent.pane,
        terminal_id: agent.terminal_id,
    })
}

pub fn send_prepared_prompt(prepared: PreparedPrompt, text: &str) -> Result<()> {
    prompt_verified(prepared.socket, &prepared.pane, &prepared.terminal_id, text)
}

pub fn attach_live_room(
    room_id: &str,
    profile: &str,
    resume: Option<&str>,
) -> Result<Option<Value>> {
    let Some(resume) = resume.filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    validate_target(room_id, profile, resume)?;
    let socket = default_socket_path();
    if !std::fs::metadata(&socket).is_ok_and(|m| m.file_type().is_socket()) {
        return Ok(None);
    }
    let mut result = match snapshot_on_socket(socket) {
        Ok(state) => find_external_agent(&state, resume)?,
        Err(_) => None,
    };
    if let Some(tty) = &mut result {
        tty["roomId"] = json!(room_id);
    }
    Ok(result)
}

fn find_room_tab(snapshot: &Value, room_id: &str) -> Result<Option<Value>> {
    let tabs = snapshot["tabs"].as_array().ok_or("herdr tabs missing")?;
    let agents = snapshot["agents"]
        .as_array()
        .ok_or("herdr agents missing")?;
    let name = agent_name(room_id);
    if let Some(agent) = agents.iter().find(|a| a["name"].as_str() == Some(&name)) {
        if agent["agent"].as_str().is_some_and(|kind| kind != "hermes") {
            return Err("room agent is not Hermes".into());
        }
        let tab_id = agent["tab_id"].as_str().ok_or("room agent has no tab")?;
        return Ok(Some(
            tabs.iter()
                .find(|t| t["tab_id"] == tab_id)
                .ok_or("room agent tab is missing")?
                .clone(),
        ));
    }
    let suffix = format!(" [{}]", &crate::digest(room_id)[..12]);
    // The title can change; the identity suffix cannot.
    let matches: Vec<_> = tabs
        .iter()
        .filter(|t| t["label"].as_str().is_some_and(|s| s.ends_with(&suffix)))
        .collect();
    if matches.len() > 1 {
        return Err("ambiguous room tabs".into());
    }
    Ok(matches.first().map(|t| (*t).clone()))
}

fn room_agent<'a>(snapshot: &'a Value, tab_id: &str) -> Option<&'a Value> {
    snapshot["agents"]
        .as_array()?
        .iter()
        .find(|a| a["tab_id"] == tab_id)
}

/// A Desktop prompt cannot resume a session currently owned by a herdr agent.
pub fn room_agent_running(room_id: &str) -> bool {
    snapshot()
        .ok()
        .and_then(|state| {
            state["agents"].as_array().map(|agents| {
                agents
                    .iter()
                    .any(|agent| agent["name"] == agent_name(room_id))
            })
        })
        .unwrap_or(false)
}

fn restoring_agent(pane: &Value) -> bool {
    pane["agent_session"].is_object()
}

fn root_pane<'a>(snapshot: &'a Value, tab_id: &str) -> Option<&'a str> {
    snapshot["panes"]
        .as_array()?
        .iter()
        .find(|p| p["tab_id"] == tab_id)?
        .get("pane_id")?
        .as_str()
}

fn current_workspace(snapshot: &Value) -> Option<&str> {
    snapshot["workspaces"].as_array()?.first()?["workspace_id"].as_str()
}

fn start_agent(pane_id: &str, room_id: &str, profile: &str, resume: &str) -> Result<()> {
    let mut args: Vec<String> = vec![
        "agent".into(),
        "start".into(),
        agent_name(room_id),
        "--kind".into(),
        "hermes".into(),
        "--pane".into(),
        pane_id.into(),
        "--timeout".into(),
        AGENT_START_TIMEOUT_MS.into(),
        "--".into(),
    ];
    args.push("-p".into());
    args.push(profile.into());
    args.push("--resume".into());
    args.push(resume.into());
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    run(&refs)?;
    Ok(())
}

/// Serializes room opens so concurrent requests cannot create duplicate tabs or agents.
fn room_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

/// An unbound room gets a shell tab, never a new Hermes conversation.
/// Once Ares provides a stored session binding, an idle shell can start Hermes.
pub fn ensure_room_tab(
    room_id: &str,
    title: &str,
    profile: &str,
    resume: Option<&str>,
    focus: bool,
    occupied: bool,
) -> Result<Value> {
    let resume = resume.unwrap_or("");
    validate_target(room_id, profile, resume)?;
    let _guard = room_lock().lock().map_err(|_| "herdr room lock poisoned")?;
    ensure_server()?;
    let snapshot = snapshot()?;
    let label = room_tab_label(room_id, title);
    let existing = find_room_tab(&snapshot, room_id)?;
    let created = existing.is_none();
    let (tab_id, pane_id) = if let Some(tab) = existing {
        let id = tab["tab_id"].as_str().ok_or("tab has no ID")?;
        (
            id.to_owned(),
            root_pane(&snapshot, id)
                .ok_or("tab has no pane")?
                .to_owned(),
        )
    } else if let Some(workspace) = current_workspace(&snapshot) {
        let result = run(&[
            "tab",
            "create",
            "--workspace",
            workspace,
            "--label",
            &label,
            "--no-focus",
        ])?;
        (
            result["result"]["tab"]["tab_id"]
                .as_str()
                .ok_or("herdr did not return tab ID")?
                .to_owned(),
            result["result"]["root_pane"]["pane_id"]
                .as_str()
                .ok_or("herdr did not return pane ID")?
                .to_owned(),
        )
    } else {
        let result = run(&["workspace", "create", "--label", "Ares"])?;
        let id = result["result"]["tab"]["tab_id"]
            .as_str()
            .ok_or("herdr did not return tab ID")?
            .to_owned();
        let pane = result["result"]["root_pane"]["pane_id"]
            .as_str()
            .ok_or("herdr did not return pane ID")?
            .to_owned();
        run(&["tab", "rename", &id, &label])?;
        (id, pane)
    };
    let running = room_agent(&snapshot, &tab_id);
    if running.is_some_and(|a| a["name"].as_str() != Some(&agent_name(room_id))) {
        return Err("room tab contains another agent".into());
    }
    // Snapshot pane.agent can precede agent-list registration during launch.
    let pane = snapshot["panes"]
        .as_array()
        .and_then(|panes| panes.iter().find(|p| p["pane_id"] == pane_id));
    let pending =
        pane.is_some_and(|p| p["agent"].as_str().is_some() || p["agent_status"] != "unknown");
    let restoring = pane.is_some_and(restoring_agent);
    let agent_started = running.is_some() || pending;
    let should_start = !resume.is_empty() && !agent_started && !restoring;
    let agent_error = if should_start && occupied {
        "Hermes session is active in Ares; a second herdr agent cannot resume it".to_owned()
    } else if should_start {
        start_agent(&pane_id, room_id, profile, resume)
            .err()
            .map(|e| e.to_string())
            .unwrap_or_default()
    } else {
        String::new()
    };
    if focus {
        run(&["tab", "focus", &tab_id])?;
    }
    Ok(json!({
        "tabId": tab_id, "paneId": pane_id, "roomId": room_id,
        "agentName": agent_name(room_id),
        "sessionName": SESSION,
        "label": label, "created": created,
        "agentStarted": agent_started || (should_start && agent_error.is_empty()),
        "agentError": agent_error, "awaitingBinding": resume.is_empty() && !agent_started,
        "restoring": restoring && !agent_started,
    }))
}

pub fn state() -> Value {
    let running = server_running();
    let tabs = if running {
        snapshot()
            .ok()
            .and_then(|s| s["tabs"].as_array().cloned())
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    json!({
        "session": SESSION,
        "server": running,
        "tabs": tabs
            .iter()
            .map(|t| json!({
                "tabId": t["tab_id"],
                "label": t["label"],
                "focused": t["focused"],
                "agentStatus": t["agent_status"],
            }))
            .collect::<Vec<_>>(),
    })
}

pub fn ensure_and_state() -> Result<Value> {
    ensure_server()?;
    let mut value = state();
    value["ok"] = json!(true);
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prompt_selection_uses_inventory_profile_socket_and_instance() {
        let mut state = json!({"tabs":[{"tab_id":"w1:t1","label":"QA"}],
            "panes":[{"tab_id":"w1:t1","pane_id":"w1:p1","terminal_id":"term_1","composer_empty":true}],
            "agents":[{"agent":"hermes","agent_session":{"kind":"id","value":"session_1","profile":"default"},
                "tab_id":"w1:t1","pane_id":"w1:p1","terminal_id":"term_1","agent_status":"idle"}]});
        let inventory = json!({"host":"local","complete":true,"errors":[],"terminals":[{
            "agentKind":"hermes","profile":"default","sessionId":"session_1",
            "socketPath":"/home/user/.config/herdr/sessions/custom/herdr.sock","paneId":"w1:p1","terminalId":"term_1"}]});
        assert!(prepare_inventory_prompt(&inventory, &state, "coder", "session_1", true).is_err());
        let prepared =
            prepare_inventory_prompt(&inventory, &state, "default", "session_1", true).unwrap();
        assert!(prepared.socket.ends_with("sessions/custom/herdr.sock"));
        assert_eq!(prepared.terminal_id, "term_1");
        state["agents"][0]["terminal_id"] = json!("replacement");
        assert!(
            prepare_inventory_prompt(&inventory, &state, "default", "session_1", true).is_err()
        );
    }

    #[test]
    fn sanitize_label_strips_control_and_collapses_space() {
        assert_eq!(sanitize_label("  Sala\t Ares\n "), "Sala Ares");
        assert_eq!(sanitize_label(""), "room");
        assert_eq!(sanitize_label("\u{0}\u{1}"), "room");
        let long = sanitize_label(&"x".repeat(80));
        assert_eq!(long.chars().count(), LABEL_MAX);
    }

    #[test]
    fn agent_name_is_stable_and_cli_safe() {
        assert!(agent_name("room_a").starts_with("ares-"));
        assert_eq!(agent_name("room_a"), agent_name("room_a"));
        assert_ne!(agent_name("room_a"), agent_name("room_b"));
        assert!(agent_name("room_a").len() <= 32);
    }

    #[test]
    fn socket_path_lives_under_herdr_sessions() {
        let path = socket_path("ares");
        assert!(path.ends_with("sessions/ares/herdr.sock"));
        assert!(default_socket_path().ends_with("herdr/herdr.sock"));
    }

    #[test]
    fn private_prompt_uses_socket_without_spawning_a_cli_with_text_in_argv() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::{fs::PermissionsExt, net::UnixListener};
        if env::var_os("ARES_PRIVATE_PROMPT_CHILD").is_none() {
            let output = Command::new(env::current_exe().unwrap())
                .args(["--exact", "herdr::tests::private_prompt_uses_socket_without_spawning_a_cli_with_text_in_argv", "--nocapture"])
                .env("ARES_PRIVATE_PROMPT_CHILD", "1")
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let bin = tmp.path().join("bin");
        std::fs::create_dir(&bin).unwrap();
        let fake_cli = bin.join("herdr");
        let marker = tmp.path().join("cli-was-spawned");
        std::fs::write(
            &fake_cli,
            format!("#!/bin/sh\ntouch '{}'\nexit 1\n", marker.display()),
        )
        .unwrap();
        std::fs::set_permissions(&fake_cli, std::fs::Permissions::from_mode(0o700)).unwrap();
        env::set_var(
            "PATH",
            format!("{}:{}", bin.display(), env::var("PATH").unwrap_or_default()),
        );
        let socket = tmp.path().join("herdr.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        listener.set_nonblocking(true).unwrap();
        let server = thread::spawn(move || {
            let started = Instant::now();
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(e)
                        if e.kind() == std::io::ErrorKind::WouldBlock
                            && started.elapsed() < Duration::from_secs(2) =>
                    {
                        thread::sleep(Duration::from_millis(10))
                    }
                    Err(e) => panic!("prompt socket was not used: {e}"),
                }
            };
            let mut line = String::new();
            BufReader::new(&stream).read_line(&mut line).unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            let response = json!({"id":request["id"],"result":{"type":"agent_prompted","agent":{"terminal_id":"term_1"}}});
            stream
                .write_all(format!("{response}\n").as_bytes())
                .unwrap();
            request
        });
        let result = send_prepared_prompt(
            PreparedPrompt {
                socket,
                pane: "w1:p1".into(),
                terminal_id: "term_1".into(),
            },
            "private fixture prompt",
        );
        let request = server.join().unwrap();
        assert!(
            !marker.exists(),
            "prompt text must never enter process arguments"
        );
        assert!(result.is_ok());
        assert_eq!(request["method"], "agent.prompt");
        assert_eq!(request["params"]["target"], "w1:p1");
        assert_eq!(request["params"]["text"], "private fixture prompt");
    }

    fn prompt_reply(reply: Value) -> Result<()> {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::UnixListener;
        let tmp = tempfile::tempdir().unwrap();
        let socket = tmp.path().join("herdr.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            BufReader::new(&stream).read_line(&mut line).unwrap();
            stream.write_all(format!("{reply}\n").as_bytes()).unwrap();
        });
        let result = prompt_verified(socket, "w1:p1", "term_1", "private fixture prompt");
        server.join().unwrap();
        result
    }

    #[test]
    fn prompt_socket_success_requires_matching_id_and_agent() {
        prompt_reply(json!({"id":"ares:agent:prompt","result":{"type":"agent_prompted","agent":{"terminal_id":"term_1"}}})).unwrap();
    }

    #[test]
    fn prompt_without_explicit_success_is_uncertain() {
        assert!(prompt_reply(json!({}))
            .unwrap_err()
            .to_string()
            .contains("uncertain"));
        assert!(prompt_reply(json!({"id":"different","result":{"type":"agent_prompted","agent":{"terminal_id":"term_1"}}})).is_err());
        assert!(prompt_reply(json!({"id":"ares:agent:prompt","result":{"type":"agent_prompted","agent":{"terminal_id":"unrelated"}}})).is_err());
    }

    #[test]
    fn prompt_explicit_rejection_is_uncertain() {
        assert!(prompt_reply(json!({"id":"ares:agent:prompt","ok":false,"result":{"type":"agent_prompted","agent":{"terminal_id":"term_1"}}})).is_err());
    }

    #[test]
    fn prompt_nested_rejection_is_uncertain_even_with_top_level_ok() {
        assert!(prompt_reply(
            json!({"id":"ares:agent:prompt","ok":true,"result":{"error":"rejected"}})
        )
        .is_err());
    }

    #[test]
    fn prompt_socket_error_redacts_text_even_if_server_echoes_it() {
        let secret = "private fixture prompt";
        let error = prompt_reply(
            json!({"id":"ares:agent:prompt","error":{"code":"rejected","message":secret}}),
        )
        .unwrap_err()
        .to_string();
        assert!(error.contains("uncertain"));
        assert!(!error.contains(secret));
    }

    #[test]
    fn prompt_agent_rejects_busy_ambiguous_or_unproven_composer() {
        let mut state = json!({"tabs":[{"tab_id":"w1:t1","label":"Test"}],
            "panes":[{"tab_id":"w1:t1","pane_id":"w1:p1"}],
            "agents":[{"agent":"hermes","agent_session":{"kind":"id","value":"session_1"},
                        "tab_id":"w1:t1","pane_id":"w1:p1","terminal_id":"term_1","agent_status":"idle"}]});
        assert!(select_prompt_agent(&state, "session_1", false).is_err()); // no composer signal
        state["panes"][0]["composer_empty"] = json!(true);
        assert_eq!(
            select_prompt_agent(&state, "session_1", false)
                .unwrap()
                .pane,
            "w1:p1"
        );
        for status in ["blocked", "unknown"] {
            state["agents"][0]["agent_status"] = json!(status);
            assert!(select_prompt_agent(&state, "session_1", false).is_err());
        }
        state["agents"][0]["agent_status"] = json!("idle");
        let duplicate = state["agents"][0].clone();
        state["agents"].as_array_mut().unwrap().push(duplicate);
        assert!(select_prompt_agent(&state, "session_1", false).is_err());
    }

    #[test]
    fn prompt_agent_requires_terminal_identity() {
        let mut state = json!({"tabs":[{"tab_id":"w1:t1","label":"Test"}],
            "panes":[{"tab_id":"w1:t1","pane_id":"w1:p1","composer_empty":true}],
            "agents":[{"agent":"hermes","agent_session":{"kind":"id","value":"session_1"},
                        "tab_id":"w1:t1","pane_id":"w1:p1","agent_status":"idle"}]});
        assert!(select_prompt_agent(&state, "session_1", false).is_err());
        state["agents"][0]["terminal_id"] = json!("term_1");
        assert!(select_prompt_agent(&state, "session_1", false).is_ok());
    }

    #[test]
    fn explicit_review_allows_unreported_composer_but_not_known_nonempty_or_busy() {
        let mut state = json!({"tabs":[{"tab_id":"w1:t1","label":"Test"}],
            "panes":[{"tab_id":"w1:t1","pane_id":"w1:p1"}],
            "agents":[{"agent":"hermes","agent_session":{"kind":"id","value":"session_1"},
                        "tab_id":"w1:t1","pane_id":"w1:p1","terminal_id":"term_1","agent_status":"idle"}]});
        assert_eq!(
            select_prompt_agent(&state, "session_1", true).unwrap().pane,
            "w1:p1"
        );
        state["panes"][0]["composer_empty"] = json!(false);
        assert!(select_prompt_agent(&state, "session_1", true).is_err());
        state["panes"][0]
            .as_object_mut()
            .unwrap()
            .remove("composer_empty");
        state["agents"][0]["agent_status"] = json!("working");
        assert_eq!(
            select_prompt_agent(&state, "session_1", true).unwrap().pane,
            "w1:p1"
        );
        state["agents"][0]["agent_status"] = json!("blocked");
        assert!(select_prompt_agent(&state, "session_1", true).is_err());
    }

    #[test]
    fn reuse_live_external_herdr_agent_by_session_not_room_title() {
        let state = json!({
            "tabs":[{"tab_id":"wF:t1","label":"Unrelated title"}],
            "panes":[{"tab_id":"wF:t1","pane_id":"wF:p1"}],
            "agents":[{"agent":"hermes","agent_session":{"kind":"id","value":"session_1"},
                       "tab_id":"wF:t1","pane_id":"wF:p1","agent_status":"working"}]
        });
        let tty = find_external_agent(&state, "session_1").unwrap().unwrap();
        assert_eq!(tty["agentName"], "wF:p1");
        assert_eq!(tty["paneId"], "wF:p1");
        assert_eq!(tty["tabId"], "wF:t1");
        assert_eq!(tty["agentStarted"], true);
        assert!(find_external_agent(&state, "another_session")
            .unwrap()
            .is_none());
        let mut ambiguous = state.clone();
        ambiguous["agents"]
            .as_array_mut()
            .unwrap()
            .push(state["agents"][0].clone());
        assert!(find_external_agent(&ambiguous, "session_1").is_err());
    }

    #[test]
    fn tab_identity_is_room_id_not_display_label() {
        let first = room_tab_label("room_a", "Same title");
        let second = room_tab_label("room_b", "Same title");
        assert_ne!(first, second);
        assert_ne!(agent_name("room_a"), agent_name("room_b"));
        assert!(first.chars().count() <= LABEL_MAX);
    }

    #[test]
    fn snapshot_uses_agent_identity_before_tab_title() {
        let label = room_tab_label("room_a", "Same title");
        let snapshot = json!({"tabs":[
            {"tab_id":"w1:t1","label":"Same title"},
            {"tab_id":"w1:t2","label":label}],
            "agents":[{"name":agent_name("room_a"),"agent":"hermes","tab_id":"w1:t2","pane_id":"w1:p2"}],
            "panes":[{"tab_id":"w1:t2","pane_id":"w1:p2"}]});
        assert_eq!(
            find_room_tab(&snapshot, "room_a").unwrap().unwrap()["tab_id"],
            "w1:t2"
        );
    }

    #[test]
    #[ignore = "run with HOME and ARES_HERDR_TEST_HOME set to the same disposable directory"]
    fn headless_socket_deduplicates_unbound_rooms() {
        let path = std::env::var("ARES_HERDR_TEST_HOME").expect("isolated home required");
        assert_eq!(home().to_str(), Some(path.as_str()));
        assert!(!server_running());
        let first = ensure_room_tab("room_a", "Same title", "default", None, true, false).unwrap();
        let second = ensure_room_tab("room_b", "Same title", "default", None, true, false).unwrap();
        let reopened =
            ensure_room_tab("room_a", "Renamed title", "default", None, true, false).unwrap();
        assert_eq!(first["tabId"], reopened["tabId"]);
        assert_ne!(first["tabId"], second["tabId"]);
        assert_eq!(first["agentName"], agent_name("room_a"));
        assert_ne!(first["agentName"], second["agentName"]);
        assert_eq!(first["awaitingBinding"], true);
        assert_eq!(second["awaitingBinding"], true);
        assert_eq!(snapshot().unwrap()["tabs"].as_array().unwrap().len(), 2);
        let output = command(HERDR)
            .env("HERDR_SOCKET_PATH", socket_path(SESSION))
            .args(["server", "stop"])
            .output()
            .unwrap();
        assert!(output.status.success());
    }

    #[test]
    fn restored_session_reference_blocks_duplicate_launch() {
        let pane = json!({"agent":null,"agent_status":"unknown",
            "agent_session":{"kind":"id","value":"session_1","agent":"hermes","source":"hermes"}});
        assert!(restoring_agent(&pane));
    }

    #[test]
    fn rejects_unsafe_resume_and_profile_before_launch() {
        assert!(validate_target("room_a", "default", "session_1").is_ok());
        assert!(validate_target("room_a", "--profile", "session_1").is_err());
        assert!(validate_target("room_a", "default", "remote::peer::id").is_err());
        assert!(validate_target("room_a", "default", "latest").is_err());
        assert!(validate_target("room_a", "default", "").is_ok());
    }
}
