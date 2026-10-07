use ares_server::{http, store::Store, text};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::net::SocketAddr;
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn fixture(dir: &TempDir) -> (Store, String) {
    let home = dir.path().join("hermes");
    std::fs::create_dir_all(&home).unwrap();
    let db = Connection::open(home.join("state.db")).unwrap();
    db.execute_batch("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,profile_name TEXT,hidden INTEGER,archived INTEGER); CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,display_order INTEGER,display_identity BLOB,active INTEGER,compacted INTEGER,display_kind TEXT,tool_calls TEXT,reasoning TEXT);CREATE INDEX idx_messages_display_page ON messages(session_id,display_order) WHERE active=1 OR compacted=1;").unwrap();
    db.execute(
        "INSERT INTO sessions VALUES('sess_1','desktop','default',0,0)",
        [],
    )
    .unwrap();
    for i in 1..=12_i64 {
        db.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,X'01',1,0,NULL,NULL,NULL)",
            params![
                i,
                "sess_1",
                if i % 2 == 0 { "assistant" } else { "user" },
                format!("message-{i}-{}", "x".repeat(550)),
                9_007_199_254_740_990_i64 + i
            ],
        )
        .unwrap();
    }
    let s = Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        home,
    )
    .unwrap();
    let p = s
        .create_project("Local", dir.path().to_str().unwrap(), "local", None)
        .unwrap();
    let r = s
        .create_room(text(&p, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&r, "id").to_owned();
    s.db.execute("UPDATE rooms SET source='hermes',external_profile='default',external_session_id='sess_1' WHERE id=?",[&id]).unwrap();
    (s, id)
}
async fn server(s: Store, dir: &TempDir) -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = http::router(s, "test-token", dir.path().into());
    (
        addr,
        tokio::spawn(async move {
            axum::serve(
                listener,
                app.into_make_service_with_connect_info::<SocketAddr>(),
            )
            .await
            .unwrap()
        }),
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
    let raw = String::from_utf8(bytes).unwrap();
    let status = raw.split_whitespace().nth(1).unwrap().parse().unwrap();
    let body = serde_json::from_str(raw.split("\r\n\r\n").nth(1).unwrap()).unwrap();
    (status, body, raw)
}
fn source(dir: &TempDir) -> Connection {
    Connection::open(dir.path().join("hermes/state.db")).unwrap()
}
#[tokio::test]
async fn exact_local_history_pages_large_cursors_without_persisting() {
    let dir = TempDir::new().unwrap();
    let (s, id) = fixture(&dir);
    let ares = dir.path().join("ares.db");
    let (addr, task) = server(s, &dir).await;
    assert_eq!(
        call(addr, &format!("/v1/rooms/{id}/history"), false)
            .await
            .0,
        401
    );
    let (status, page, raw) = call(addr, &format!("/v1/rooms/{id}/history"), true).await;
    assert_eq!(status, 200, "{page}");
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    assert_eq!(page["messages"].as_array().unwrap().len(), 8);
    assert_eq!(page["next_before"], "9007199254740995");
    assert!(page["messages"]
        .as_array()
        .unwrap()
        .iter()
        .all(|row| row["text"].as_str().unwrap().chars().count() <= 500));
    let (status, older, _) = call(
        addr,
        &format!(
            "/v1/rooms/{id}/history?before={}",
            page["next_before"].as_str().unwrap()
        ),
        true,
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(older["messages"].as_array().unwrap().len(), 4);
    assert_eq!(older["has_more"], false);
    let db = Connection::open(ares).unwrap();
    let count: i64 = db
        .query_row(
            "SELECT count(*) FROM room_events WHERE room_id=?",
            [id],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
    task.abort();
}
#[tokio::test]
async fn local_history_refuses_hidden_archived_reassigned_and_changed_room() {
    let dir = TempDir::new().unwrap();
    let (s, id) = fixture(&dir);
    let ares = dir.path().join("ares.db");
    let (addr, task) = server(s, &dir).await;
    for sql in [
        "UPDATE sessions SET hidden=1",
        "UPDATE sessions SET hidden=0,archived=1",
        "UPDATE sessions SET archived=0,profile_name='other'",
        "UPDATE sessions SET profile_name='default',source=''",
    ] {
        source(&dir).execute(sql, []).unwrap();
        assert_eq!(
            call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
            503
        );
    }
    source(&dir)
        .execute("UPDATE sessions SET source='desktop'", [])
        .unwrap();
    Connection::open(&ares)
        .unwrap()
        .execute(
            "UPDATE rooms SET external_session_id='other' WHERE id=?",
            [&id],
        )
        .unwrap();
    assert_eq!(
        call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
        503
    );
    task.abort();
}
#[tokio::test]
async fn local_history_filters_nonconversation_rows_and_rejects_invalid_cursor() {
    let dir = TempDir::new().unwrap();
    let (s, id) = fixture(&dir);
    let db = source(&dir);
    db.execute("UPDATE messages SET tool_calls='secret' WHERE id=12", [])
        .unwrap();
    db.execute("UPDATE messages SET reasoning='secret' WHERE id=11", [])
        .unwrap();
    db.execute("UPDATE messages SET display_kind='tool' WHERE id=10", [])
        .unwrap();
    let (addr, task) = server(s, &dir).await;
    let (status, page, raw) = call(addr, &format!("/v1/rooms/{id}/history"), true).await;
    assert_eq!(status, 200);
    assert!(!raw.contains("secret"));
    assert_eq!(page["messages"].as_array().unwrap().len(), 8);
    for c in ["0", "01", "900719925474099312345", "-1", "1.0"] {
        assert_eq!(
            call(addr, &format!("/v1/rooms/{id}/history?before={c}"), true)
                .await
                .0,
            400
        );
    }
    task.abort();
}

#[tokio::test]
async fn local_history_prefers_active_compaction_copy_and_redacts_secrets() {
    let dir = TempDir::new().unwrap();
    let (store, id) = fixture(&dir);
    let db = source(&dir);
    db.execute(
        "UPDATE messages SET active=0,compacted=1,content='obsolete copy' WHERE id=12",
        [],
    )
    .unwrap();
    db.execute("INSERT INTO messages(id,session_id,role,content,display_order,display_identity,active,compacted) VALUES(13,'sess_1','assistant','Authorization: Bearer secret-string',9007199254741002,X'01',1,0)",[]).unwrap();
    let (addr, task) = server(store, &dir).await;
    let (code, page, raw) = call(addr, &format!("/v1/rooms/{id}/history"), true).await;
    assert_eq!(code, 200, "{page}");
    assert_eq!(page["messages"].as_array().unwrap().len(), 8);
    assert_eq!(page["messages"][7]["text"], "[redacted]");
    assert!(!raw.contains("secret-string"));
    assert!(!raw.contains("obsolete copy"));
    task.abort();
}

async fn impersonate_remote(
    mut req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    req.extensions_mut()
        .insert(axum::extract::ConnectInfo(SocketAddr::from((
            [192, 0, 2, 1],
            30000,
        ))));
    next.run(req).await
}
#[tokio::test]
async fn local_history_requires_direct_loopback_connect_info() {
    let dir = TempDir::new().unwrap();
    let (store, id) = fixture(&dir);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = http::router(store, "test-token", dir.path().into());
    let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    assert_eq!(
        call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
        403
    );
    task.abort();
    let store = Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = http::router(store, "test-token", dir.path().into())
        .layer(axum::middleware::from_fn(impersonate_remote));
    let task = tokio::spawn(async move {
        axum::serve(
            listener,
            app.into_make_service_with_connect_info::<SocketAddr>(),
        )
        .await
        .unwrap()
    });
    assert_eq!(
        call(addr, &format!("/v1/rooms/{id}/history"), true).await.0,
        403
    );
    task.abort();
}
