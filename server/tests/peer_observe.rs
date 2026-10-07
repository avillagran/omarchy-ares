use ares_server::{http, store::Store, text};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    net::SocketAddr,
    os::unix::{fs::PermissionsExt, net::UnixListener},
    path::Path,
    sync::{Mutex, MutexGuard},
};
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

// Each fixture mutates process-wide HOME to route the owner-local control
// socket. Rust test functions run concurrently unless we serialize them.
fn isolate_home() -> MutexGuard<'static, ()> {
    static HOME_GUARD: Mutex<()> = Mutex::new(());
    HOME_GUARD.lock().unwrap()
}

fn mock(home: &Path, replies: Vec<Value>) -> std::thread::JoinHandle<Vec<Value>> {
    let dir = home.join(".local/share/ares-peer");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    std::thread::spawn(move || {
        replies
            .into_iter()
            .map(|reply| {
                let (mut stream, _) = listener.accept().unwrap();
                let mut line = String::new();
                BufReader::new(stream.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                stream.write_all(format!("{}\n", reply).as_bytes()).unwrap();
                serde_json::from_str(&line).unwrap()
            })
            .collect()
    })
}
fn optional_mock(home: &Path, replies: Vec<Value>) -> std::thread::JoinHandle<Vec<Value>> {
    let dir = home.join(".local/share/ares-peer");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    listener.set_nonblocking(true).unwrap();
    std::thread::spawn(move || {
        let mut requests = Vec::new();
        for reply in replies {
            let start = std::time::Instant::now();
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(connection) => break connection,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        if start.elapsed() > std::time::Duration::from_millis(700) {
                            return requests;
                        }
                        std::thread::sleep(std::time::Duration::from_millis(5));
                    }
                    Err(e) => panic!("mock accept: {e}"),
                }
            };
            let mut line = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            stream.write_all(format!("{}\n", reply).as_bytes()).unwrap();
            requests.push(serde_json::from_str(&line).unwrap());
        }
        requests
    })
}
fn save_exact_binding(store: &Store, id: &str) {
    let inventory = ares_server::terminals::normalize_peer_inventory("node", &inventory()).unwrap();
    let key = text(&inventory["terminals"][0], "id");
    store.db.execute("INSERT INTO terminal_bindings(room_id,host,terminal_key,agent_kind,updated_at) VALUES(?, 'node', ?, 'hermes', 0)", rusqlite::params![id,key]).unwrap();
}

fn mutating_mock(
    home: &Path,
    replies: Vec<Value>,
    database: &Path,
    room: &str,
) -> std::thread::JoinHandle<Vec<Value>> {
    let dir = home.join(".local/share/ares-peer");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let db = database.to_owned();
    let room = room.to_owned();
    std::thread::spawn(move || {
        replies
            .into_iter()
            .enumerate()
            .map(|(index, reply)| {
                let (mut stream, _) = listener.accept().unwrap();
                let mut line = String::new();
                BufReader::new(stream.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                if index == 2 {
                    rusqlite::Connection::open(&db)
                        .unwrap()
                        .execute("UPDATE rooms SET external_archived=1 WHERE id=?", [&room])
                        .unwrap();
                }
                stream.write_all(format!("{}\n", reply).as_bytes()).unwrap();
                serde_json::from_str(&line).unwrap()
            })
            .collect()
    })
}
// Replace the saved binding while the second source status RPC is in flight.
// The mock's write is committed before that RPC can complete.
fn replacing_binding_mock(
    home: &Path,
    database: &Path,
    room: &str,
) -> std::thread::JoinHandle<Vec<Value>> {
    let dir = home.join(".local/share/ares-peer");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let db = database.to_owned();
    let room = room.to_owned();
    std::thread::spawn(move || {
        [wrapped(status()), wrapped(inventory()), wrapped(status()), wrapped(inventory())]
            .into_iter()
            .enumerate()
            .map(|(index, reply)| {
                let start = std::time::Instant::now();
                let (mut stream, _) = loop {
                    match listener.accept() {
                        Ok(connection) => break connection,
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                            if start.elapsed() > std::time::Duration::from_millis(700) {
                                return None;
                            }
                            std::thread::sleep(std::time::Duration::from_millis(5));
                        }
                        Err(e) => panic!("mock accept: {e}"),
                    }
                };
                let mut line = String::new();
                BufReader::new(stream.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                if index == 2 {
                    let changed = rusqlite::Connection::open(&db)
                        .unwrap()
                        .execute(
                            "UPDATE terminal_bindings SET terminal_key='replaced-terminal' WHERE room_id=?",
                            [&room],
                        )
                        .unwrap();
                    assert_eq!(changed, 1);
                }
                stream.write_all(format!("{}\n", reply).as_bytes()).unwrap();
                Some(serde_json::from_str(&line).unwrap())
            })
            .flatten()
            .collect()
    })
}
fn wrapped(result: Value) -> Value {
    json!({"ok":true,"result":result})
}
fn inventory() -> Value {
    json!({"complete":true,"terminals":[{"paneId":"w1:p1","tabId":"w1:t1","terminalId":"term_123","socketPath":"/home/user/.config/herdr/herdr.sock","agentKind":"hermes","sessionId":"chat-a","profile":"default","identitySource":"verified"}]})
}
fn status() -> Value {
    json!({"trusted":["node"],"trusted_bindings":{"node":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}})
}
fn frame() -> Value {
    json!({"profile":"default","session_id":"chat-a","terminal_id":"term_123","encoding":"base64","frame":"SGVsbG8=","cols":80,"rows":24})
}
fn fixture(dir: &TempDir) -> (Store, String) {
    let store = Store::open(
        &dir.path().join("db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let project = store.create_project("Peer", "/work", "node", None).unwrap();
    let room = store
        .create_room(text(&project, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id").to_owned();
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='remote::node::default',external_session_id='remote::node::default::chat-a' WHERE id=?",[&id]).unwrap();
    (store, id)
}
async fn call(addr: SocketAddr, path: &str, authorized: bool) -> (u16, Value, String) {
    let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    let auth = if authorized {
        "Authorization: Bearer test-token\r\n"
    } else {
        ""
    };
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: localhost\r\n{auth}Connection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut reply = Vec::new();
    stream.read_to_end(&mut reply).await.unwrap();
    let raw = String::from_utf8(reply).unwrap();
    let status = raw.split_whitespace().nth(1).unwrap().parse().unwrap();
    let body = serde_json::from_str(raw.split("\r\n\r\n").nth(1).unwrap()).unwrap();
    (status, body, raw)
}
async fn post_focus(addr: SocketAddr, path: &str) -> (u16, Value, String) {
    let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    stream.write_all(format!("POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-token\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}").as_bytes()).await.unwrap();
    let mut reply = Vec::new();
    stream.read_to_end(&mut reply).await.unwrap();
    let raw = String::from_utf8(reply).unwrap();
    let status = raw.split_whitespace().nth(1).unwrap().parse().unwrap();
    let body = serde_json::from_str(raw.split("\r\n\r\n").nth(1).unwrap()).unwrap();
    (status, body, raw)
}
async fn serve(store: Store, dir: &TempDir) -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let router = http::router(store, "test-token", dir.path().into());
    let task = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    (addr, task)
}
#[tokio::test]
async fn observe_exact_peer_owner_returns_bounded_read_only_frame() {
    let _home = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = mock(
        dir.path(),
        vec![
            wrapped(status()),
            wrapped(inventory()),
            wrapped(frame()),
            wrapped(status()),
            wrapped(inventory()),
        ],
    );
    let (addr, server) = serve(store, &dir).await;
    let path = format!("/v1/tty/rooms/{id}/observe?cols=80&rows=24");
    assert_eq!(call(addr, &path, false).await.0, 401);
    assert_eq!(
        call(
            addr,
            &format!("/v1/tty/rooms/{id}/observe?cols=80&cols=90&rows=24"),
            true
        )
        .await
        .0,
        400
    );
    assert_eq!(
        call(
            addr,
            &format!("/v1/tty/rooms/{id}/observe?cols=80&rows="),
            true
        )
        .await
        .0,
        400
    );
    let (code, body, raw) = call(addr, &path, true).await;
    assert_eq!(code, 200, "{raw}");
    assert_eq!(
        body,
        json!({"ok":true,"transport":"peer","encoding":"base64","frame":"SGVsbG8=","terminalId":"term_123"})
    );
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    assert!(raw
        .to_ascii_lowercase()
        .contains("x-content-type-options: nosniff"));
    let requests = worker.join().unwrap();
    assert_eq!(requests.len(), 5);
    assert_eq!(requests[0], json!({"op":"status"}));
    assert_eq!(
        requests[1],
        json!({"op":"rpc","peer":"node","request":{"op":"inventory"}})
    );
    assert_eq!(
        requests[2],
        json!({"op":"rpc","peer":"node","request":{"op":"observe","profile":"default","session_id":"chat-a","cols":80,"rows":24}})
    );
    assert_eq!(requests[3], json!({"op":"status"}));
    assert_eq!(requests[4], requests[1]);
    server.abort();
}

#[tokio::test]
async fn observe_refuses_unbound_or_ambiguous_pairing_before_inventory() {
    let _home = isolate_home();
    for (label, status_result) in [
        ("missing", json!({"trusted":["node"]})),
        (
            "duplicate",
            json!({"trusted":["node","node"],"trusted_bindings":{}}),
        ),
        (
            "malformed",
            json!({"trusted":["node"],"trusted_bindings":{"node":"not-a-binding"}}),
        ),
    ] {
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        let worker = optional_mock(
            dir.path(),
            vec![wrapped(status_result), wrapped(inventory())],
        );
        let (addr, server) = serve(store, &dir).await;
        let (code, body, raw) = call(
            addr,
            &format!("/v1/tty/rooms/{id}/observe?cols=80&rows=24"),
            true,
        )
        .await;
        assert_eq!(code, 503, "{label}: {body}");
        assert_eq!(body["error"], "Observe unavailable");
        assert!(!raw.contains("SGVsbG8="));
        assert_eq!(
            worker.join().unwrap(),
            vec![json!({"op":"status"})],
            "{label}"
        );
        server.abort();
    }
}

#[tokio::test]
async fn observe_refuses_same_alias_repair_during_rpc_before_second_inventory() {
    let _home = isolate_home();
    for (label, changed_status) in [
        (
            "repaired",
            json!({"trusted":["node"],"trusted_bindings":{"node":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}),
        ),
        ("revoked", json!({"trusted":[],"trusted_bindings":{}})),
        ("missing", json!({"trusted":["node"],"trusted_bindings":{}})),
        (
            "duplicate",
            json!({"trusted":["node","node"],"trusted_bindings":{"node":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}),
        ),
    ] {
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        let worker = optional_mock(
            dir.path(),
            vec![
                wrapped(status()),
                wrapped(inventory()),
                wrapped(frame()),
                wrapped(changed_status),
                wrapped(inventory()),
            ],
        );
        let (addr, server) = serve(store, &dir).await;
        let (code, body, raw) = call(
            addr,
            &format!("/v1/tty/rooms/{id}/observe?cols=80&rows=24"),
            true,
        )
        .await;
        assert_eq!(code, 503, "{label}: {body}");
        assert_eq!(body["error"], "Observe unavailable");
        assert!(!raw.contains("SGVsbG8="));
        let requests = worker.join().unwrap();
        assert_eq!(
            requests.len(),
            4,
            "{label}: changed pairing must stop before second inventory"
        );
        assert_eq!(requests[0], json!({"op":"status"}));
        assert_eq!(requests[1]["request"]["op"], "inventory");
        assert_eq!(requests[2]["request"]["op"], "observe");
        assert_eq!(requests[3], json!({"op":"status"}));
        server.abort();
    }
}

#[tokio::test]
async fn observe_rejects_unverified_duplicate_and_untrusted_owners_without_rpc() {
    let _home = isolate_home();
    for variant in ["unverified", "duplicate", "untrusted"] {
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        let mut inv = inventory();
        if variant == "unverified" {
            inv["terminals"][0]["identitySource"] = json!("observed-only");
        }
        if variant == "duplicate" {
            let mut other = inv["terminals"][0].clone();
            other["terminalId"] = json!("term_456");
            inv["terminals"].as_array_mut().unwrap().push(other);
        }
        let replies = if variant == "untrusted" {
            vec![wrapped(json!({"trusted":[]}))]
        } else {
            vec![wrapped(status()), wrapped(inv)]
        };
        let worker = mock(dir.path(), replies);
        let (addr, server) = serve(store, &dir).await;
        let (code, body, _) = call(
            addr,
            &format!("/v1/tty/rooms/{id}/observe?cols=80&rows=24"),
            true,
        )
        .await;
        assert_eq!(code, 503, "{variant}: {body}");
        assert_eq!(body["error"], "Observe unavailable");
        assert_eq!(
            worker.join().unwrap().len(),
            if variant == "untrusted" { 1 } else { 2 }
        );
        server.abort();
    }
}

#[tokio::test]
async fn observe_rejects_archived_malformed_and_mismatched_saved_binding_before_rpc() {
    let _home = isolate_home();
    for variant in ["archived", "mismatch", "query"] {
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        if variant == "archived" {
            store
                .db
                .execute("UPDATE rooms SET external_archived=1 WHERE id=?", [&id])
                .unwrap();
        }
        if variant == "mismatch" {
            store.db.execute("INSERT INTO terminal_bindings(room_id,host,terminal_key,agent_kind,updated_at) VALUES(?, 'node', 'wrong', 'hermes', 0)",[&id]).unwrap();
        }
        let worker = if variant == "mismatch" {
            Some(mock(
                dir.path(),
                vec![wrapped(status()), wrapped(inventory())],
            ))
        } else {
            None
        };
        let (addr, server) = serve(store, &dir).await;
        let path = format!(
            "/v1/tty/rooms/{id}/observe?cols=80&rows=24{}",
            if variant == "query" { "&extra=bad" } else { "" }
        );
        let (code, body, _) = call(addr, &path, true).await;
        assert_eq!(
            code,
            if variant == "query" { 400 } else { 503 },
            "{variant}: {body}"
        );
        assert!(!body.to_string().contains("SGVsbG8="));
        if let Some(worker) = worker {
            assert_eq!(worker.join().unwrap().len(), 2);
        }
        server.abort();
    }
}

#[tokio::test]
async fn observe_drops_frame_if_room_changes_during_rpc() {
    let _home = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = mutating_mock(
        dir.path(),
        vec![
            wrapped(status()),
            wrapped(inventory()),
            wrapped(frame()),
            wrapped(status()),
            wrapped(inventory()),
        ],
        &dir.path().join("db"),
        &id,
    );
    let (addr, server) = serve(store, &dir).await;
    let (code, body, _) = call(
        addr,
        &format!("/v1/tty/rooms/{id}/observe?cols=80&rows=24"),
        true,
    )
    .await;
    assert_eq!(code, 503, "{body}");
    assert!(!body.to_string().contains("SGVsbG8="));
    assert_eq!(worker.join().unwrap().len(), 5);
    server.abort();
}

#[tokio::test]
async fn observe_drops_frame_if_peer_revoked_or_terminal_changed_after_rpc() {
    let _home = isolate_home();
    for changed in ["revoked", "terminal"] {
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        let mut second = inventory();
        second["terminals"][0]["terminalId"] = json!("term_456");
        let replies = vec![
            wrapped(status()),
            wrapped(inventory()),
            wrapped(frame()),
            wrapped(if changed == "revoked" {
                json!({"trusted":[]})
            } else {
                status()
            }),
        ];
        let replies = if changed == "terminal" {
            [replies, vec![wrapped(second)]].concat()
        } else {
            replies
        };
        let worker = mock(dir.path(), replies);
        let (addr, server) = serve(store, &dir).await;
        let (code, body, _) = call(
            addr,
            &format!("/v1/tty/rooms/{id}/observe?cols=80&rows=24"),
            true,
        )
        .await;
        assert_eq!(code, 503, "{changed}: {body}");
        assert!(!body.to_string().contains("SGVsbG8="));
        assert_eq!(
            worker.join().unwrap().len(),
            if changed == "terminal" { 5 } else { 4 }
        );
        server.abort();
    }
}

#[tokio::test]
async fn focus_only_activates_verified_output_only_peer_descriptor() {
    let _home = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = mock(
        dir.path(),
        vec![
            wrapped(inventory()),
            wrapped(status()),
            wrapped(inventory()),
        ],
    );
    let (addr, server) = serve(store, &dir).await;
    let (code, body, _) = post_focus(addr, &format!("/v1/tty/rooms/{id}/focus")).await;
    assert_eq!(code, 200, "{body}");
    assert_eq!(body["tty"]["sessionName"], "peer-observe");
    assert_eq!(body["tty"]["agentName"], id);
    assert_eq!(body["tty"]["readOnly"], true);
    assert_eq!(body["tty"]["agentStarted"], true);
    assert!(body["tty"].get("sshTarget").is_none());
    assert_eq!(worker.join().unwrap().len(), 3);
    server.abort();
}

#[tokio::test]
async fn observe_source_denial_never_returns_a_frame() {
    let _home = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = mock(
        dir.path(),
        vec![
            wrapped(status()),
            wrapped(inventory()),
            json!({"ok":false,"error":"hidden or scope denied"}),
        ],
    );
    let (addr, server) = serve(store, &dir).await;
    let (code, body, _) = call(
        addr,
        &format!("/v1/tty/rooms/{id}/observe?cols=80&rows=24"),
        true,
    )
    .await;
    assert_eq!(code, 503);
    assert_eq!(body["error"], "Observe unavailable");
    assert_eq!(worker.join().unwrap().len(), 3);
    server.abort();
}

#[tokio::test]
async fn observe_rejects_malformed_or_oversized_source_frame() {
    let _home = isolate_home();
    for variant in ["identity", "encoding", "oversize"] {
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        let mut result = frame();
        if variant == "identity" {
            result["terminal_id"] = json!("other");
        }
        if variant == "encoding" {
            result["frame"] = json!("!!!");
        }
        if variant == "oversize" {
            result["frame"] = json!("QQ==".repeat(3000));
        }
        let worker = mock(
            dir.path(),
            vec![wrapped(status()), wrapped(inventory()), wrapped(result)],
        );
        let (addr, server) = serve(store, &dir).await;
        let (code, body, _) = call(
            addr,
            &format!("/v1/tty/rooms/{id}/observe?cols=80&rows=24"),
            true,
        )
        .await;
        assert_eq!(code, 503, "{variant}: {body}");
        assert_eq!(worker.join().unwrap().len(), 3);
        server.abort();
    }
}

#[tokio::test]
async fn control_preflight_requires_saved_binding_before_second_source_call() {
    let _home = isolate_home();
    std::env::set_var("ARES_CONTROL_PREFLIGHT", "1");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = optional_mock(
        dir.path(),
        vec![
            wrapped(status()),
            wrapped(inventory()),
            wrapped(status()),
            wrapped(inventory()),
        ],
    );
    let (addr, server) = serve(store, &dir).await;
    let (code, body, raw) =
        call(addr, &format!("/v1/tty/rooms/{id}/control/preflight"), true).await;
    assert_eq!(code, 503, "{body}");
    assert_eq!(body["error"], "Control unavailable");
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    let requests = worker.join().unwrap();
    assert_eq!(
        requests.len(),
        2,
        "missing binding must stop before recheck"
    );
    assert_eq!(requests[0]["op"], "status");
    assert_eq!(requests[1]["request"]["op"], "inventory");
    server.abort();
    std::env::remove_var("ARES_CONTROL_PREFLIGHT");
}

#[tokio::test]
async fn control_preflight_rejects_mismatched_saved_binding_before_second_source_rpc() {
    let _home = isolate_home();
    std::env::set_var("ARES_CONTROL_PREFLIGHT", "1");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    store.db.execute(
        "INSERT INTO terminal_bindings(room_id,host,terminal_key,agent_kind,updated_at) VALUES(?, 'node', 'wrong-terminal', 'hermes', 0)",
        [&id],
    ).unwrap();
    let worker = optional_mock(
        dir.path(),
        vec![
            wrapped(status()),
            wrapped(inventory()),
            wrapped(status()),
            wrapped(inventory()),
        ],
    );
    let (addr, server) = serve(store, &dir).await;
    let (code, body, raw) =
        call(addr, &format!("/v1/tty/rooms/{id}/control/preflight"), true).await;
    assert_eq!(code, 503, "{body}");
    assert_eq!(body, json!({"ok":false,"error":"Control unavailable"}));
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    assert!(!raw.contains("term_123"));
    let requests = worker.join().unwrap();
    assert_eq!(
        requests.len(),
        2,
        "mismatched binding must stop before recheck"
    );
    assert_eq!(requests[0], json!({"op":"status"}));
    assert_eq!(requests[1]["request"]["op"], "inventory");
    server.abort();
    std::env::remove_var("ARES_CONTROL_PREFLIGHT");
}

#[tokio::test]
async fn control_preflight_drops_replaced_binding_during_source_rpc() {
    let _home = isolate_home();
    std::env::set_var("ARES_CONTROL_PREFLIGHT", "1");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    save_exact_binding(&store, &id);
    let worker = replacing_binding_mock(dir.path(), &dir.path().join("db"), &id);
    let (addr, server) = serve(store, &dir).await;
    let (code, body, raw) =
        call(addr, &format!("/v1/tty/rooms/{id}/control/preflight"), true).await;
    assert_eq!(code, 503, "{body}");
    assert_eq!(body, json!({"ok":false,"error":"Control unavailable"}));
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    assert!(!raw.contains("term_123"));
    let requests = worker.join().unwrap();
    assert_eq!(
        requests.len(),
        3,
        "replaced binding must stop before second inventory"
    );
    assert_eq!(requests[2], json!({"op":"status"}));
    server.abort();
    std::env::remove_var("ARES_CONTROL_PREFLIGHT");
}

#[tokio::test]
async fn control_preflight_matching_saved_binding_rechecks_but_refuses_input() {
    let _home = isolate_home();
    std::env::set_var("ARES_CONTROL_PREFLIGHT", "1");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    save_exact_binding(&store, &id);
    let worker = mock(
        dir.path(),
        vec![
            wrapped(status()),
            wrapped(inventory()),
            wrapped(status()),
            wrapped(inventory()),
        ],
    );
    let (addr, server) = serve(store, &dir).await;
    let (code, body, raw) =
        call(addr, &format!("/v1/tty/rooms/{id}/control/preflight"), true).await;
    assert_eq!(code, 503, "{body}");
    assert_eq!(body["error"], "Control unavailable");
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    let requests = worker.join().unwrap();
    assert_eq!(requests.len(), 4);
    assert!(requests
        .iter()
        .all(|r| r["op"] == "status" || r["request"]["op"] == "inventory"));
    server.abort();
    std::env::remove_var("ARES_CONTROL_PREFLIGHT");
}

#[tokio::test]
async fn control_preflight_is_default_off_even_for_a_valid_room() {
    let _home = isolate_home();
    std::env::remove_var("ARES_CONTROL_PREFLIGHT");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let (addr, server) = serve(store, &dir).await;
    let path = format!("/v1/tty/rooms/{id}/control/preflight");
    assert_eq!(call(addr, &path, false).await.0, 401);
    let (code, body, raw) = call(addr, &path, true).await;
    assert_eq!(code, 503, "{body}");
    assert_eq!(body["error"], "Control unavailable");
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    server.abort();
}

#[tokio::test]
async fn control_preflight_refuses_ambiguous_owner_before_source_control() {
    let _home = isolate_home();
    std::env::set_var("ARES_CONTROL_PREFLIGHT", "1");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let mut inv = inventory();
    let duplicate = inv["terminals"][0].clone();
    inv["terminals"].as_array_mut().unwrap().push(duplicate);
    let worker = mock(dir.path(), vec![wrapped(status()), wrapped(inv)]);
    let (addr, server) = serve(store, &dir).await;
    let (code, body, _) = call(addr, &format!("/v1/tty/rooms/{id}/control/preflight"), true).await;
    assert_eq!(code, 503, "{body}");
    assert_eq!(worker.join().unwrap().len(), 2);
    server.abort();
    std::env::remove_var("ARES_CONTROL_PREFLIGHT");
}

#[tokio::test]
async fn control_preflight_drops_room_changed_during_source_rpc() {
    let _home = isolate_home();
    std::env::set_var("ARES_CONTROL_PREFLIGHT", "1");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    save_exact_binding(&store, &id);
    let worker = mutating_mock(
        dir.path(),
        vec![wrapped(status()), wrapped(inventory()), wrapped(status())],
        &dir.path().join("db"),
        &id,
    );
    let (addr, server) = serve(store, &dir).await;
    let (code, body, raw) =
        call(addr, &format!("/v1/tty/rooms/{id}/control/preflight"), true).await;
    assert_eq!(code, 503, "{body}");
    assert!(!raw.contains("term_123"));
    assert_eq!(worker.join().unwrap().len(), 3);
    server.abort();
    std::env::remove_var("ARES_CONTROL_PREFLIGHT");
}

#[tokio::test]
async fn control_preflight_never_promotes_inventory_or_claimed_generation_to_input() {
    let _home = isolate_home();
    std::env::set_var("ARES_CONTROL_PREFLIGHT", "1");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    save_exact_binding(&store, &id);
    let mut inv = inventory();
    inv["terminals"][0]["terminalGeneration"] = json!("claimed-source-generation");
    let worker = mock(
        dir.path(),
        vec![
            wrapped(status()),
            wrapped(inv.clone()),
            wrapped(status()),
            wrapped(inv),
        ],
    );
    let (addr, server) = serve(store, &dir).await;
    let (code, body, raw) =
        call(addr, &format!("/v1/tty/rooms/{id}/control/preflight"), true).await;
    assert_eq!(code, 503, "{body}");
    assert_eq!(body, json!({"ok":false,"error":"Control unavailable"}));
    assert!(!raw.contains("claimed-source-generation"));
    let requests = worker.join().unwrap();
    assert_eq!(requests.len(), 4);
    assert!(requests
        .iter()
        .all(|r| r["op"] == "status" || r["request"]["op"] == "inventory"));
    server.abort();
    std::env::remove_var("ARES_CONTROL_PREFLIGHT");
}
