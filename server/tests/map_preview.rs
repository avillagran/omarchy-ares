use ares_server::{http, store::Store, text};
use serde_json::{json, Value};
use std::{io::{BufRead, BufReader, Write}, net::SocketAddr, os::unix::{fs::PermissionsExt, net::UnixListener}};
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

async fn call(addr: SocketAddr, room: &str, authorized: bool) -> (u16, Value) {
    let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    let auth = if authorized { "Authorization: Bearer test-token\r\n" } else { "" };
    let request = format!("GET /v1/rooms/{room}/preview HTTP/1.1\r\nHost: localhost\r\n{auth}Connection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut reply = Vec::new();
    stream.read_to_end(&mut reply).await.unwrap();
    let text = String::from_utf8(reply).unwrap();
    let status = text.split_whitespace().nth(1).unwrap().parse().unwrap();
    let value = serde_json::from_str(text.split("\r\n\r\n").nth(1).unwrap()).unwrap();
    (status, value)
}

async fn server(store: Store, dir: &TempDir) -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = http::router(store, "test-token", dir.path().into());
    let task = tokio::spawn(async move { axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>()).await.unwrap(); });
    (addr, task)
}

fn fixture(dir: &TempDir) -> (Store, String, String) {
    let s = Store::open(&dir.path().join("ares.db"), &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap();
    let project = s.create_project("Fixture", dir.path().to_str().unwrap(), "local", None).unwrap();
    let pid = text(&project, "id").to_owned();
    let room = s.create_room(&pid, "Local", &[json!("default")]).unwrap();
    (s, pid, text(&room, "id").to_owned())
}

fn event(s: &Store, project: &str, room: &str, seq: i64, kind: &str, value: &str) {
    s.db.execute("INSERT INTO room_events(id,project_id,room_id,sequence,kind,author,payload,created_at) VALUES(?,?,?,?,?,'fixture',?,?)",
        rusqlite::params![format!("evt_preview_{seq}"),project,room,seq,kind,json!({"text":value}).to_string(),seq]).unwrap();
}

#[tokio::test]
async fn peek_local_room_is_bounded_plaintext_and_read_only_even_when_archived() {
    let dir = TempDir::new().unwrap();
    let (store, project, room) = fixture(&dir);
    for i in 2..14 {
        let kind = if i == 7 { "tool.result" } else if i == 8 { "reasoning.status" }
            else if i % 2 == 0 { "message.user" } else { "message.assistant" };
        event(&store, &project, &room, i, kind, &format!("message-{i}-{}", "x".repeat(700)));
    }
    store.db.execute("UPDATE rooms SET deleted_at=1,status='deleted' WHERE id=?", [&room]).unwrap();
    let before: i64 = store.db.query_row("SELECT COUNT(*) FROM room_events WHERE room_id=?", [&room], |r| r.get(0)).unwrap();
    let (addr, task) = server(store, &dir).await;
    assert_eq!(call(addr, &room, false).await.0, 401);
    assert_eq!(call(addr, "invalid-room", true).await.0, 404);
    let (status, peek) = call(addr, &room, true).await;
    assert_eq!(status, 200);
    let messages = peek["messages"].as_array().unwrap();
    assert_eq!(messages.len(), 8);
    assert!(messages.iter().all(|m| (m["role"] == "user" || m["role"] == "assistant")
        && m["text"].as_str().is_some_and(|t| t.chars().count() <= 500)));
    assert!(!peek.to_string().contains("tool.result"));
    assert!(!peek.to_string().contains("reasoning.status"));
    task.abort();
    let db = rusqlite::Connection::open(dir.path().join("ares.db")).unwrap();
    let after: i64 = db.query_row("SELECT COUNT(*) FROM room_events WHERE room_id=?", [&room], |r| r.get(0)).unwrap();
    assert_eq!(before, after);
}

#[tokio::test]
async fn paired_remote_preview_requires_exact_stored_identity_and_only_returns_messages() {
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let (store, project, room) = fixture(&dir);
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='remote::node::default',external_session_id='remote::node::default::session_1' WHERE id=?", [&room]).unwrap();
    event(&store, &project, &room, 42, "message.user", "cached-private-history");
    let peer_dir=dir.path().join(".local/share/ares-peer");
    std::fs::create_dir_all(&peer_dir).unwrap();
    std::fs::set_permissions(&peer_dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let socket=peer_dir.join("control.sock");
    let listener=UnixListener::bind(&socket).unwrap();
    std::fs::set_permissions(&socket,std::fs::Permissions::from_mode(0o600)).unwrap();
    let worker=std::thread::spawn(move || {
        let (mut stream,_) = listener.accept().unwrap();
        let mut line=String::new();
        BufReader::new(stream.try_clone().unwrap()).read_line(&mut line).unwrap();
        stream.write_all(format!("{}\n",json!({"ok":true,"result":{"messages":[{"role":"assistant","text":"answer"}],"token":"never-echo"}})).as_bytes()).unwrap();
        serde_json::from_str::<Value>(&line).unwrap()
    });
    let (addr, task) = server(store, &dir).await;
    let (status, preview)=call(addr,&room,true).await;
    assert_eq!(status,200);
    assert_eq!(preview["messages"],json!([{"role":"assistant","text":"answer"}]));
    assert!(!preview.to_string().contains("never-echo"));
    assert_eq!(worker.join().unwrap(),json!({"op":"rpc","peer":"node","request":{"op":"preview","profile":"default","session_id":"session_1","limit":8}}));
    let (status, unavailable) = call(addr, &room, true).await;
    assert_eq!(status, 502);
    assert!(!unavailable.to_string().contains("cached-private-history"));
    task.abort();
}
