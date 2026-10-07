use ares_server::{http, store::Store, text};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    net::SocketAddr,
    os::unix::{fs::PermissionsExt, net::UnixListener},
    path::Path,
    time::{Duration, Instant},
};
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn isolate_home() -> std::sync::MutexGuard<'static, ()> {
    static GUARD: std::sync::Mutex<()> = std::sync::Mutex::new(());
    GUARD.lock().unwrap()
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
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='remote::node::default',external_session_id='remote::node::default::chat-a' WHERE id=?", [&id]).unwrap();
    (store, id)
}
fn peer(
    home: &Path,
    replies: Vec<Value>,
    change: Option<(&Path, &str, &str)>,
) -> std::thread::JoinHandle<Vec<Value>> {
    peer_with_hold(home, replies, change, None)
}
fn peer_with_hold(
    home: &Path,
    replies: Vec<Value>,
    change: Option<(&Path, &str, &str)>,
    hold: Option<(std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>)>,
) -> std::thread::JoinHandle<Vec<Value>> {
    let parent = home.join(".local/share/ares-peer");
    std::fs::create_dir_all(&parent).unwrap();
    std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o700)).unwrap();
    let socket = parent.join("control.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let change = change.map(|(db, id, sql)| (db.to_owned(), id.to_owned(), sql.to_owned()));
    std::thread::spawn(move || {
        let mut seen = Vec::new();
        for (index, reply) in replies.into_iter().enumerate() {
            let start = Instant::now();
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(v) => break v,
                    Err(e)
                        if e.kind() == std::io::ErrorKind::WouldBlock
                            && start.elapsed() < Duration::from_millis(350) =>
                    {
                        std::thread::sleep(Duration::from_millis(3))
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return seen,
                    Err(e) => panic!("accept: {e}"),
                }
            };
            let mut line = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            seen.push(serde_json::from_str(&line).unwrap());
            if index == 1 {
                if let Some((ref ready, ref release)) = hold {
                    ready.send(()).unwrap();
                    release.recv_timeout(Duration::from_secs(3)).unwrap();
                }
                if let Some((ref db, ref id, ref sql)) = change {
                    rusqlite::Connection::open(db)
                        .unwrap()
                        .execute(sql, [id])
                        .unwrap();
                }
            }
            stream
                .write_all(format!("{}\n", json!({"ok":true,"result":reply})).as_bytes())
                .unwrap();
        }
        seen
    })
}
async fn server(store: Store, dir: &TempDir) -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = http::router(store, "test-token", dir.path().into());
    (
        addr,
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() }),
    )
}
async fn call(addr: SocketAddr, path: &str, auth: bool) -> (u16, Value, String) {
    let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    let header = if auth {
        "Authorization: Bearer test-token\r\n"
    } else {
        ""
    };
    stream
        .write_all(
            format!("GET {path} HTTP/1.1\r\nHost: localhost\r\n{header}Connection: close\r\n\r\n")
                .as_bytes(),
        )
        .await
        .unwrap();
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).await.unwrap();
    let reply = String::from_utf8(bytes).unwrap();
    let status = reply.split_whitespace().nth(1).unwrap().parse().unwrap();
    let body = serde_json::from_str(reply.split("\r\n\r\n").nth(1).unwrap()).unwrap();
    (status, body, reply)
}
fn status() -> Value {
    status_binding("a".repeat(64))
}
fn status_binding(binding: String) -> Value {
    json!({"trusted":["node"],"trusted_bindings":{"node":binding}})
}
fn page() -> Value {
    json!({"messages":[{"role":"assistant","text":"answer","cursor":2}],"next_before":2,"has_more":true})
}
fn sequence() -> Vec<Value> {
    vec![status(), page(), status()]
}
#[tokio::test]
async fn room_history_accepts_peer_integer_ascending_page_and_numeric_request() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let expected = json!({"messages":[
        {"role":"user","text":"older","cursor":"2"},
        {"role":"assistant","text":"newer","cursor":"5"}
    ],"next_before":"2","has_more":true});
    let source = json!({"messages":[
        {"role":"user","text":"older","cursor":2},
        {"role":"assistant","text":"newer","cursor":5}
    ],"next_before":2,"has_more":true});
    let worker = peer(dir.path(), vec![status(), source, status()], None);
    let (addr, task) = server(store, &dir).await;
    let (code, body, raw) = call(addr, &format!("/v1/rooms/{id}/history?before=9"), true).await;
    assert_eq!(code, 200, "{body}");
    assert_eq!(body["messages"], expected["messages"]);
    assert_eq!(body["next_before"], expected["next_before"]);
    assert_eq!(body["has_more"], expected["has_more"]);
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    assert_eq!(
        worker.join().unwrap(),
        vec![
            json!({"op":"status"}),
            json!({"op":"rpc","peer":"node","request":{"op":"history","profile":"default","session_id":"chat-a","before":9,"limit":8}}),
            json!({"op":"status"}),
        ]
    );
    task.abort();
}
#[tokio::test]
async fn room_history_initial_page_uses_null_before_and_accepts_maximum_i64_cursor() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let newest = json!({"messages":[{"role":"assistant","text":"latest","cursor":i64::MAX}],"next_before":null,"has_more":false});
    let worker = peer(dir.path(), vec![status(), newest.clone(), status()], None);
    let (addr, task) = server(store, &dir).await;
    let (code, body, _) = call(addr, &format!("/v1/rooms/{id}/history"), true).await;
    assert_eq!(code, 200, "{body}");
    assert_eq!(body["messages"][0]["cursor"], i64::MAX.to_string());
    assert_eq!(body["next_before"], Value::Null);
    assert_eq!(worker.join().unwrap()[1]["request"]["before"], Value::Null);
    task.abort();
}
#[tokio::test]
async fn room_history_pages_above_js_safe_limit_without_precision_loss() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let upper = i64::MAX;
    let oldest = 9_007_199_254_740_993_i64;
    let source = json!({"messages":[
        {"role":"user","text":"private older","cursor":oldest},
        {"role":"assistant","text":"private newer","cursor":oldest+1}
    ],"next_before":oldest,"has_more":true});
    let worker = peer(dir.path(), vec![status(), source, status()], None);
    let (addr, task) = server(store, &dir).await;
    let (code, body, raw) = call(
        addr,
        &format!("/v1/rooms/{id}/history?before={upper}"),
        true,
    )
    .await;
    assert_eq!(code, 200, "{body}");
    assert_eq!(body["messages"][0]["cursor"], oldest.to_string());
    assert_eq!(body["messages"][1]["cursor"], (oldest + 1).to_string());
    assert_eq!(body["next_before"], oldest.to_string());
    assert!(!raw.contains("\"cursor\":9007199254740993"));
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    let requests = worker.join().unwrap();
    assert_eq!(requests[1]["request"]["before"], json!(upper));
    assert!(requests[1]["request"]["before"].is_number());
    task.abort();
}

#[tokio::test]
async fn room_history_authenticates_pages_and_rechecks_trust() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = peer(dir.path(), sequence(), None);
    let (addr, task) = server(store, &dir).await;
    let path = format!("/v1/rooms/{id}/history?before=9");
    assert_eq!(call(addr, &path, false).await.0, 401);
    let (code, body, raw) = call(addr, &path, true).await;
    assert_eq!(code, 200, "{body}");
    assert_eq!(body["messages"][0]["cursor"], "2");
    assert_eq!(body["next_before"], "2");
    assert_eq!(body["has_more"], true);
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    let seen = worker.join().unwrap();
    assert_eq!(
        seen,
        vec![
            json!({"op":"status"}),
            json!({"op":"rpc","peer":"node","request":{"op":"history","profile":"default","session_id":"chat-a","before":9,"limit":8}}),
            json!({"op":"status"})
        ]
    );
    task.abort();
}
#[tokio::test]
async fn room_history_refuses_same_alias_repair_while_history_rpc_is_held() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let (ready_tx, ready_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let worker = peer_with_hold(
        dir.path(),
        vec![status(), page(), status_binding("b".repeat(64))],
        None,
        Some((ready_tx, release_rx)),
    );
    let (addr, task) = server(store, &dir).await;
    let request =
        tokio::spawn(async move { call(addr, &format!("/v1/rooms/{id}/history"), true).await });
    tokio::task::spawn_blocking(move || ready_rx.recv_timeout(Duration::from_secs(3)).unwrap())
        .await
        .unwrap();
    assert!(!request.is_finished(), "history RPC must still be held");
    release_tx.send(()).unwrap();
    let (code, body, raw) = request.await.unwrap();
    assert_eq!(code, 503, "{body}");
    assert!(
        !raw.contains("answer"),
        "old pairing history must not be released"
    );
    assert_eq!(
        worker
            .join()
            .unwrap()
            .iter()
            .map(|r| r["op"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["status", "rpc", "status"]
    );
    task.abort();
}

#[tokio::test]
async fn room_history_refuses_missing_or_ambiguous_pairing_before_rpc() {
    for first in [
        json!({"trusted":["node"]}),
        json!({"trusted":["node"],"trusted_bindings":{}}),
        json!({"trusted":["node","node"],"trusted_bindings":{"node":"a".repeat(64)}}),
        json!({"trusted":["node"],"trusted_bindings":{"node":"A".repeat(64)}}),
        json!({"trusted":["node"],"trusted_bindings":{"node":"a".repeat(63)}}),
    ] {
        let _guard = isolate_home();
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        let worker = peer(dir.path(), vec![first, page()], None);
        let (addr, task) = server(store, &dir).await;
        let (code, body, raw) = call(addr, &format!("/v1/rooms/{id}/history"), true).await;
        assert_eq!(code, 503, "{body}");
        assert!(!raw.contains("answer"));
        assert_eq!(worker.join().unwrap(), vec![json!({"op":"status"})]);
        task.abort();
    }
}

#[tokio::test]
async fn room_history_refuses_revoked_peer_before_remote_rpc() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = peer(dir.path(), vec![json!({"trusted":[]}), page()], None);
    let (addr, task) = server(store, &dir).await;
    assert_eq!(
        call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
        503
    );
    assert_eq!(worker.join().unwrap(), vec![json!({"op":"status"})]);
    task.abort();
}
#[tokio::test]
async fn room_history_refuses_mid_rpc_room_replacement() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = peer(
        dir.path(),
        sequence(),
        Some((
            &dir.path().join("db"),
            &id,
            "UPDATE rooms SET external_session_id='remote::node::default::other' WHERE id=?",
        )),
    );
    let (addr, task) = server(store, &dir).await;
    assert_eq!(
        call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
        503
    );
    assert_eq!(worker.join().unwrap().len(), 3);
    task.abort();
}
#[tokio::test]
async fn room_history_rejects_malformed_cursor_and_source_frame() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let (addr, task) = server(store, &dir).await;
    assert_eq!(
        call(
            addr,
            &format!("/v1/rooms/{id}/history?before=bad%3Avalue"),
            true
        )
        .await
        .0,
        400
    );
    assert_eq!(
        call(
            addr,
            &format!("/v1/rooms/{id}/history?before=a&before=b"),
            true
        )
        .await
        .0,
        400
    );
    let worker = peer(
        dir.path(),
        vec![
            status(),
            json!({"messages":[{"role":"tool","text":"secret","cursor":1}],"next_before":null,"has_more":false}),
            status(),
        ],
        None,
    );
    assert_eq!(
        call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
        503
    );
    assert_eq!(worker.join().unwrap().len(), 2);
    task.abort();
}

#[tokio::test]
async fn room_history_refuses_trust_revoked_during_rpc() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let worker = peer(
        dir.path(),
        vec![status(), page(), json!({"trusted":[]})],
        None,
    );
    let (addr, task) = server(store, &dir).await;
    assert_eq!(
        call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
        503
    );
    assert_eq!(
        worker
            .join()
            .unwrap()
            .iter()
            .map(|r| r["op"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["status", "rpc", "status"]
    );
    task.abort();
}

#[tokio::test]
async fn room_history_refuses_source_archived_and_changed_identity_without_rpc() {
    for statement in [
        "UPDATE rooms SET external_archived=1 WHERE id=?",
        "UPDATE rooms SET external_session_id='remote::other::default::chat-a' WHERE id=?",
        "UPDATE rooms SET status='deleted' WHERE id=?",
    ] {
        let _guard = isolate_home();
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        store.db.execute(statement, [&id]).unwrap();
        let (addr, task) = server(store, &dir).await;
        assert_eq!(
            call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
            503
        );
        task.abort();
    }
}

#[tokio::test]
async fn room_history_refuses_offline_peer_and_never_releases_cached_events() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let project: String = store
        .db
        .query_row("SELECT project_id FROM rooms WHERE id=?", [&id], |r| {
            r.get(0)
        })
        .unwrap();
    store.db.execute("INSERT INTO room_events(id,project_id,room_id,sequence,kind,author,payload,created_at) VALUES('private',?,?,400,'message.user','fixture','{\"text\":\"cached-secret\"}',1)",rusqlite::params![project,id]).unwrap();
    let (addr, task) = server(store, &dir).await;
    let (code, body, _) = call(addr, &format!("/v1/rooms/{id}/history"), true).await;
    assert_eq!(code, 503);
    assert!(!body.to_string().contains("cached-secret"));
    task.abort();
}

#[tokio::test]
async fn room_history_refuses_overlong_or_invalid_pagination_frames() {
    for bad in [
        json!({"messages":[{"role":"user","text":"x".repeat(501),"cursor":1}],"next_before":null,"has_more":false}),
        json!({"messages":[{"role":"assistant","text":"x","cursor":"bad:cursor"}],"next_before":null,"has_more":false}),
        json!({"messages":[],"next_before":1,"has_more":true}),
    ] {
        let _guard = isolate_home();
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        let worker = peer(dir.path(), vec![status(), bad, status()], None);
        let (addr, task) = server(store, &dir).await;
        assert_eq!(
            call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
            503
        );
        assert_eq!(worker.join().unwrap().len(), 2);
        task.abort();
    }
}

#[tokio::test]
async fn room_history_rejects_non_positive_or_non_decimal_uri_before_peer_contact() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let (addr, task) = server(store, &dir).await;
    for before in [
        "0",
        "-1",
        "+1",
        "1.0",
        "true",
        "9223372036854775808",
        "",
        "1a",
        "01",
        "0001",
    ] {
        let (code, _, _) = call(
            addr,
            &format!("/v1/rooms/{id}/history?before={before}"),
            true,
        )
        .await;
        assert_eq!(code, 400, "before={before}");
    }
    task.abort();
}

#[tokio::test]
async fn room_history_rejects_invalid_peer_cursor_types_and_order() {
    let row = |cursor: Value| json!({"role":"user","text":"x","cursor":cursor});
    let cases = [
        json!({"messages":[row(json!(true))],"next_before":null,"has_more":false}),
        json!({"messages":[row(json!(1.0))],"next_before":null,"has_more":false}),
        json!({"messages":[row(json!(0))],"next_before":null,"has_more":false}),
        json!({"messages":[row(json!(9223372036854775808u64))],"next_before":null,"has_more":false}),
        json!({"messages":[row(json!(4)),row(json!(3))],"next_before":null,"has_more":false}),
        json!({"messages":[row(json!(4)),row(json!(4))],"next_before":null,"has_more":false}),
        json!({"messages":[row(json!(4)),row(json!(9))],"next_before":null,"has_more":false}),
        json!({"messages":[row(json!(4)),row(json!(5))],"next_before":5,"has_more":true}),
        json!({"messages":[row(json!(4)),row(json!(5))],"next_before":true,"has_more":true}),
        json!({"messages":[row(json!(4)),row(json!(5))],"next_before":1.0,"has_more":true}),
        json!({"messages":[row(json!(4)),row(json!(5))],"next_before":9223372036854775808u64,"has_more":true}),
    ];
    for bad in cases {
        let _guard = isolate_home();
        let dir = TempDir::new().unwrap();
        std::env::set_var("HOME", dir.path());
        let (store, id) = fixture(&dir);
        let worker = peer(dir.path(), vec![status(), bad.clone(), status()], None);
        let (addr, task) = server(store, &dir).await;
        let (code, body, _) = call(addr, &format!("/v1/rooms/{id}/history?before=9"), true).await;
        assert_eq!(code, 503, "bad frame: {bad}, response: {body}");
        assert_eq!(worker.join().unwrap().len(), 2);
        task.abort();
    }
}
#[tokio::test]
async fn room_history_rejects_delayed_trailing_bytes_after_valid_peer_frame() {
    let _guard = isolate_home();
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, id) = fixture(&dir);
    let parent = dir.path().join(".local/share/ares-peer");
    std::fs::create_dir_all(&parent).unwrap();
    std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o700)).unwrap();
    let socket = parent.join("control.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let worker = std::thread::spawn(move || {
        let mut seen = Vec::new();
        for index in 0..3 {
            let start = Instant::now();
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(v) => break v,
                    Err(e)
                        if e.kind() == std::io::ErrorKind::WouldBlock
                            && start.elapsed() < Duration::from_millis(350) =>
                    {
                        std::thread::sleep(Duration::from_millis(3))
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return seen,
                    Err(e) => panic!("accept: {e}"),
                }
            };
            let mut line = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            seen.push(serde_json::from_str::<Value>(&line).unwrap());
            let reply = if index == 1 { page() } else { status() };
            stream
                .write_all(format!("{}\n", json!({"ok":true,"result":reply})).as_bytes())
                .unwrap();
            if index == 1 {
                std::thread::sleep(Duration::from_millis(40));
                let _ = stream.write_all(b"trailing bytes after valid frame");
            }
        }
        seen
    });
    let (addr, task) = server(store, &dir).await;
    let (code, body, _) = call(addr, &format!("/v1/rooms/{id}/history"), true).await;
    assert_eq!(code, 503, "{body}");
    assert!(!body.to_string().contains("answer"));
    assert_eq!(
        worker
            .join()
            .unwrap()
            .iter()
            .map(|r| r["op"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>(),
        vec!["status", "rpc"],
        "must not recheck trust or release peer content after malformed RPC"
    );
    task.abort();
}
