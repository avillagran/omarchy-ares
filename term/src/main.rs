//! ares-term — per-room Hermes TUI terminal manager.
//!
//! Usage: ares-term <roomId>
//!
//! Ensures a private tmux session `ares-<roomId>` runs the Hermes TUI for the
//! room's bound profile/session, then opens (or focuses) the standalone Qt
//! terminal window. The tmux session and the window outlive this launcher.

use rusqlite::{Connection, OptionalExtension};
use std::{
    env, fs,
    os::unix::process::CommandExt,
    path::PathBuf,
    process::{Command, ExitCode, Stdio},
};

fn state_dir() -> PathBuf {
    if let Some(dir) = env::var_os("ARES_STATE_DIR") {
        return PathBuf::from(dir);
    }
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default()
        .join(".local/state/omarchy/ares")
}

fn scrub_herdr(cmd: &mut Command) {
    for (key, _) in env::vars_os() {
        if key.to_string_lossy().starts_with("HERDR_") {
            cmd.env_remove(&key);
        }
    }
}

fn tmux(args: &[&str]) -> bool {
    let mut cmd = Command::new("tmux");
    cmd.arg("-L")
        .arg("ares")
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    scrub_herdr(&mut cmd);
    cmd.status().map(|status| status.success()).unwrap_or(false)
}

fn room_binding(
    db_path: &PathBuf,
    room_id: &str,
) -> Result<(String, Option<String>, Option<String>), String> {
    let db = Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| error.to_string())?;
    let room: Option<(String, Option<String>)> = db
        .query_row(
            "SELECT name, alias FROM rooms WHERE id=? AND deleted_at IS NULL",
            [room_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|error| error.to_string())?;
    let (name, alias) = room.ok_or("room not found")?;
    let label = alias.filter(|value| !value.is_empty()).unwrap_or(name);
    let binding: Option<(String, Option<String>)> = db
        .query_row(
            "SELECT bot_id, stored_session_id FROM session_bindings \
             WHERE room_id=? AND COALESCE(stored_session_id,'') != '' \
             ORDER BY updated_at DESC LIMIT 1",
            [room_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|error| error.to_string())?;
    Ok(match binding {
        Some((profile, session)) => (label, Some(profile), session),
        None => (label, None, None),
    })
}

fn focus_window(pid: u32) -> bool {
    let Ok(output) = Command::new("hyprctl").args(["clients", "-j"]).output() else {
        return false;
    };
    let Ok(clients) = serde_json::from_slice::<serde_json::Value>(&output.stdout) else {
        return false;
    };
    let address = clients.as_array().and_then(|clients| {
        clients
            .iter()
            .filter(|client| client["pid"].as_u64() == Some(pid as u64))
            .find_map(|client| client["address"].as_str().map(str::to_owned))
    });
    let Some(address) = address else { return false };
    Command::new("hyprctl")
        .args(["dispatch", "focuswindow", &format!("address:{address}")])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

fn main() -> ExitCode {
    let Some(room_id) = env::args().nth(1).filter(|arg| !arg.is_empty()) else {
        eprintln!("usage: ares-term <roomId>");
        return ExitCode::from(2);
    };
    let dir = state_dir();
    let (label, profile, session) = match room_binding(&dir.join("ares.db"), &room_id) {
        Ok(binding) => binding,
        Err(error) => {
            eprintln!("ares-term: {error}");
            return ExitCode::from(1);
        }
    };
    let target = format!("ares-{room_id}");

    if !tmux(&["has-session", "-t", &target]) {
        let mut args: Vec<String> = vec![
            "new-session".into(),
            "-d".into(),
            "-s".into(),
            target.clone(),
            "-x".into(),
            "220".into(),
            "-y".into(),
            "50".into(),
            "hermes".into(),
        ];
        if let Some(profile) = profile.filter(|value| !value.is_empty()) {
            args.push("-p".into());
            args.push(profile);
        }
        if let Some(session) = session {
            args.push("--resume".into());
            args.push(session);
        }
        let refs: Vec<&str> = args.iter().map(String::as_str).collect();
        if !tmux(&refs) {
            eprintln!("ares-term: could not create tmux session {target}");
            return ExitCode::from(1);
        }
    }

    let pid_file = dir.join(format!("term-{room_id}.pid"));
    if let Ok(pid) = fs::read_to_string(&pid_file) {
        if let Ok(pid) = pid.trim().parse::<u32>() {
            if PathBuf::from(format!("/proc/{pid}")).exists() && focus_window(pid) {
                return ExitCode::SUCCESS;
            }
        }
    }

    let home = env::var("HOME").unwrap_or_default();
    let runner = env::var("ARES_TERM_RUNNER")
        .unwrap_or_else(|_| format!("{home}/Work/qmltermwidget/poc-ares/ares-term"));
    let import =
        env::var("ARES_TERM_IMPORT").unwrap_or_else(|_| format!("{home}/Work/qmltermwidget/build"));
    let qml = env::var("ARES_TERM_QML")
        .unwrap_or_else(|_| format!("{home}/Work/qmltermwidget/poc-ares/term.qml"));
    let mut cmd = Command::new(runner);
    cmd.args([
        import.as_str(),
        qml.as_str(),
        label.as_str(),
        "tmux",
        "-L",
        "ares",
        "attach-session",
        "-t",
        target.as_str(),
    ])
    .stdin(Stdio::null())
    .stdout(Stdio::null())
    .stderr(Stdio::null())
    .process_group(0);
    scrub_herdr(&mut cmd);
    match cmd.spawn() {
        Ok(child) => {
            let _ = fs::write(&pid_file, child.id().to_string());
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("ares-term: spawn failed: {error}");
            ExitCode::from(1)
        }
    }
}
