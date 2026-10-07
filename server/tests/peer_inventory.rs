use ares_server::{peer, terminals};
use serde_json::{json, Value};
use std::os::unix::fs::PermissionsExt;
use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixListener,
    time::Duration,
};
use tempfile::TempDir;

fn payload() -> Value {
    json!({"complete":true,"terminals":[{
        "paneId":"w1:p1","tabId":"w1:t1","terminalId":"term_123",
        "socketPath":"/home/user/.config/herdr/herdr.sock",
        "agentKind":"hermes","sessionId":"chat-a","profile":"default",
        "identitySource":"verified","status":"idle"}]})
}

fn exchange(response: Value) -> (TempDir, std::path::PathBuf, std::thread::JoinHandle<Value>) {
    let dir = TempDir::new().unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let worker = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut line = String::new();
        BufReader::new(stream.try_clone().unwrap())
            .read_line(&mut line)
            .unwrap();
        stream
            .write_all(format!("{}\n", response).as_bytes())
            .unwrap();
        serde_json::from_str(&line).unwrap()
    });
    (dir, path, worker)
}

#[test]
fn peer_inventory_uses_exact_alias_and_no_ssh_credentials() {
    let (_dir, path, worker) = exchange(json!({"ok":true,"result":payload()}));
    let result = peer::inventory_at(&path, "node", Duration::from_secs(2)).unwrap();
    assert_eq!(
        worker.join().unwrap(),
        json!({"op":"rpc","peer":"node","request":{"op":"inventory"}})
    );
    assert_eq!(result["host"], "node");
    assert_eq!(result["terminals"][0]["transport"], "peer");
    assert!(result["terminals"][0].get("sshTarget").is_none());
    assert!(result["terminals"][0].get("socketPath").is_none());
    assert!(result["terminals"][0].get("herdrBin").is_none());
    assert_eq!(result["terminals"][0]["peer"], "node");
    assert_eq!(
        terminals::descriptor("r", &result["terminals"][0])["transport"],
        "peer"
    );
    assert!(peer::inventory_at(&path, "node; rm -rf /", Duration::from_secs(1)).is_err());
}

#[test]
fn peer_denial_incomplete_and_missing_socket_fail_closed() {
    let (_dir, path, worker) = exchange(json!({"ok":false,"error":"denied"}));
    assert!(peer::inventory_at(&path, "node", Duration::from_secs(2)).is_err());
    worker.join().unwrap();
    let (_dir, path, worker) = exchange(json!({"ok":true,"result":{
        "complete":false,"terminals":[]}}));
    assert!(peer::inventory_at(&path, "node", Duration::from_secs(2)).is_err());
    worker.join().unwrap();
    let mut partial = terminals::normalize_peer_inventory("node", &payload()).unwrap();
    partial["complete"] = json!(false);
    assert!(terminals::select_session(&partial, "hermes", "default", "chat-a").is_err());
    assert!(peer::inventory_at(
        std::path::Path::new("/nonexistent/ares-peer/control.sock"),
        "node",
        Duration::from_millis(50)
    )
    .is_err());
}

#[test]
fn peer_oversize_and_timeout_are_bounded() {
    let dir = TempDir::new().unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let worker = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut line = String::new();
        BufReader::new(stream.try_clone().unwrap())
            .read_line(&mut line)
            .unwrap();
        let _ = stream.write_all(&vec![b'x'; 2 * 1024 * 1024 + 1]);
    });
    assert!(peer::inventory_at(&path, "node", Duration::from_secs(2)).is_err());
    worker.join().unwrap();
    let path = dir.path().join("wait.sock");
    let listener = UnixListener::bind(&path).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let worker = std::thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        std::thread::sleep(Duration::from_millis(200));
        drop(stream);
    });
    assert!(peer::inventory_at(&path, "node", Duration::from_millis(40)).is_err());
    worker.join().unwrap();
}

#[test]
fn peer_normalization_never_accepts_a_fake_ssh_target() {
    assert!(terminals::normalize_inventory("node", "", &payload()).is_err());
    let result = terminals::normalize_peer_inventory("node", &payload()).unwrap();
    assert_eq!(result["complete"], true);
    let mut legacy = payload();
    legacy["ok"] = json!(true);
    legacy["errors"] = json!([]);
    legacy["terminals"][0]["herdrBin"] = json!("/usr/bin/herdr");
    assert_ne!(
        result["terminals"][0]["id"],
        terminals::normalize_inventory("node", "user@node", &legacy).unwrap()["terminals"][0]["id"]
    );
    let mut observed = payload();
    observed["terminals"][0]["sessionId"] = json!("");
    observed["terminals"][0]["identitySource"] = json!("unverified");
    let normalized = terminals::normalize_peer_inventory("node", &observed).unwrap();
    assert_eq!(
        normalized["terminals"][0]["identitySource"],
        "observed-only"
    );
    assert!(
        terminals::select_session(&normalized, "hermes", "default", "chat-a")
            .unwrap()
            .is_none()
    );
}

#[test]
fn local_control_returns_only_successful_bounded_result() {
    let (_dir, path, worker) = exchange(json!({"ok":true,"result":{"peers":["node"]}}));
    let result =
        peer::local_control_at(&path, &json!({"op":"status"}), Duration::from_secs(2)).unwrap();
    assert_eq!(worker.join().unwrap(), json!({"op":"status"}));
    assert_eq!(result, json!({"peers":["node"]}));
    assert!(peer::local_control_at(
        &path,
        &json!({"op":"pair","code":"x".repeat(20_000)}),
        Duration::from_secs(1)
    )
    .is_err());
}

#[test]
fn incomplete_peer_inventory_cannot_reuse_a_saved_terminal_binding() {
    use ares_server::{store::Store, text};
    let dir = TempDir::new().unwrap();
    let store = Store::open(
        &dir.path().join("db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let project = store
        .create_project("Peer", "/peer/work", "node", None)
        .unwrap();
    let room = store
        .create_room(text(&project, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id");
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='remote::node::default',external_session_id='remote::node::default::chat-a' WHERE id=?", [id]).unwrap();
    let complete = terminals::normalize_peer_inventory("node", &payload()).unwrap();
    let candidate = complete["terminals"][0]["id"].as_str().unwrap();
    terminals::bind(&store, id, &complete, candidate, true).unwrap();
    let mut partial = complete.clone();
    partial["complete"] = json!(false);
    assert!(terminals::resolve_bound(&store, id, &partial).is_err());
    assert!(terminals::resolve_room(&store, id, &partial).is_err());
    assert!(terminals::bind(&store, id, &partial, candidate, true).is_err());
}
