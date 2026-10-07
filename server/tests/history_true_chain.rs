//! Real disposable Python SQLite -> Peer TLS -> owner Unix control -> Rust HTTP.
use ares_server::{http, store::Store, text};
use serde_json::Value;
use std::{
    ffi::OsString,
    net::SocketAddr,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

struct Fixture(Child);
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn source_db(path: &Path) {
    let db = rusqlite::Connection::open(path).unwrap();
    db.execute_batch("CREATE TABLE sessions(id TEXT PRIMARY KEY,profile_name TEXT,hidden INTEGER DEFAULT 0); \
        CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,display_order INTEGER,display_identity BLOB,active INTEGER,compacted INTEGER,display_kind TEXT,tool_calls TEXT); \
        CREATE INDEX idx_messages_display_page ON messages(session_id,display_order,active DESC,id DESC) WHERE active=1 OR compacted=1; \
        INSERT INTO sessions(id,profile_name) VALUES('chat-a','default');").unwrap();
    let start = 9_007_199_254_741_000i64;
    for i in 0..19 {
        let order = start + i;
        let role = if i % 2 == 0 { "user" } else { "assistant" };
        let body = if i == 18 {
            "x".repeat(500)
        } else {
            format!("visible-{i}")
        };
        db.execute("INSERT INTO messages(session_id,role,content,display_order,display_identity,active,compacted) VALUES('chat-a',?,?,?,?,1,0)", (role, body.as_str(), order, b"id".as_slice())).unwrap();
        if i == 3 || i == 11 {
            db.execute("INSERT INTO messages(session_id,role,content,display_order,display_identity,active,compacted) VALUES('chat-a',?,?,?,?,0,1)", (role, body.as_str(), order, b"id".as_slice())).unwrap();
        }
    }
    for (role, tool) in [("system", ""), ("assistant", "private-tool")] {
        db.execute("INSERT INTO messages(session_id,role,content,display_order,display_identity,active,compacted,tool_calls) VALUES('chat-a',?,?,?,?,1,0,?)", (role, "excluded", start + 20, b"id".as_slice(), tool)).unwrap();
    }
}

fn peer_test_python(
    home: Option<&Path>,
    override_python: Option<OsString>,
) -> Result<PathBuf, String> {
    if let Some(python) = override_python {
        return Ok(PathBuf::from(python));
    }
    let home =
        home.ok_or("HOME is unset; set ARES_PEER_TEST_PYTHON to a Python with websockets")?;
    let python = home.join(".local/share/ares-peer/venv/bin/python");
    if !python.is_file() {
        return Err(format!(
            "Peer test requires a Python with websockets: install the Ares Peer venv at {} or set ARES_PEER_TEST_PYTHON",
            python.display()
        ));
    }
    Ok(python)
}

#[test]
fn peer_python_uses_existing_owner_venv_without_override() {
    let home = TempDir::new().unwrap();
    let python = home.path().join(".local/share/ares-peer/venv/bin/python");
    std::fs::create_dir_all(python.parent().unwrap()).unwrap();
    std::fs::write(&python, b"").unwrap();
    assert_eq!(peer_test_python(Some(home.path()), None).unwrap(), python);
}

#[test]
fn peer_python_explicit_override_wins() {
    let home = TempDir::new().unwrap();
    assert_eq!(
        peer_test_python(Some(home.path()), Some("/custom/python".into())).unwrap(),
        std::path::PathBuf::from("/custom/python")
    );
}

#[test]
fn peer_python_missing_venv_explains_dependency() {
    let home = TempDir::new().unwrap();
    let error = peer_test_python(Some(home.path()), None).unwrap_err();
    assert!(error.contains("websockets"));
    assert!(error.contains("ARES_PEER_TEST_PYTHON"));
}

async fn request(addr: SocketAddr, path: &str, auth: bool) -> (u16, Value, String) {
    let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    let authorization = if auth {
        "Authorization: Bearer disposable-token\r\n"
    } else {
        ""
    };
    stream.write_all(format!("GET {path} HTTP/1.1\r\nHost: localhost\r\n{authorization}Connection: close\r\n\r\n").as_bytes()).await.unwrap();
    let mut bytes = Vec::new();
    tokio::time::timeout(Duration::from_secs(15), stream.read_to_end(&mut bytes))
        .await
        .unwrap()
        .unwrap();
    let response = String::from_utf8(bytes).unwrap();
    let status = response.split_whitespace().nth(1).unwrap().parse().unwrap();
    let body = serde_json::from_str(response.split("\r\n\r\n").nth(1).unwrap()).unwrap();
    (status, body, response)
}

#[tokio::test]
async fn actual_peer_source_control_and_http_history_pages_and_denials() {
    let old_home = std::env::var_os("HOME");
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME", dir.path());
    let source_path = dir.path().join("source.db");
    source_db(&source_path);
    let store = Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let project = store
        .create_project("Remote", "/disposable/project", "node", None)
        .unwrap();
    let room = store
        .create_room(
            text(&project, "id"),
            "Fixture",
            &[serde_json::json!("default")],
        )
        .unwrap();
    let id = text(&room, "id").to_owned();
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='remote::node::default',external_session_id='remote::node::default::chat-a' WHERE id=?", [&id]).unwrap();
    let initial_events: i64 = store
        .db
        .query_row("SELECT count(*) FROM room_events", [], |r| r.get(0))
        .unwrap();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let ready = dir.path().join("ready");
    let python = peer_test_python(
        old_home.as_deref().map(Path::new),
        std::env::var_os("ARES_PEER_TEST_PYTHON"),
    )
    .unwrap_or_else(|message| panic!("{message}"));
    let child = Command::new(python)
        .arg(root.join("tests/fixture_peer_history_bridge.py"))
        .arg(dir.path())
        .arg(&source_path)
        .arg(&ready)
        .env("HOME", dir.path())
        .env("PYTHONPATH", root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut fixture = Fixture(child);
    let start = Instant::now();
    while !ready.exists() {
        assert!(
            fixture.0.try_wait().unwrap().is_none(),
            "disposable Peer exited before ready; ensure selected Python has websockets (or set ARES_PEER_TEST_PYTHON)"
        );
        assert!(
            start.elapsed() < Duration::from_secs(12),
            "disposable Peer did not become ready; ensure selected Python has websockets (or set ARES_PEER_TEST_PYTHON)"
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let web_root = dir.path().to_path_buf();
    let server = tokio::spawn(async move {
        axum::serve(listener, http::router(store, "disposable-token", web_root))
            .await
            .unwrap()
    });
    let path = format!("/v1/rooms/{id}/history");
    let (code, _, raw) = request(addr, &path, false).await;
    assert_eq!(code, 401);
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    let mut before: Option<String> = None;
    let mut seen = Vec::new();
    let mut lengths = Vec::new();
    let mut page_sizes = Vec::new();
    loop {
        let uri = before
            .as_ref()
            .map(|cursor| format!("{path}?before={cursor}"))
            .unwrap_or_else(|| path.clone());
        let (code, body, raw) = request(addr, &uri, true).await;
        assert_eq!(code, 200, "history unavailable on real bridge");
        assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
        let rows = body["messages"].as_array().unwrap();
        assert!(rows.len() <= 8);
        page_sizes.push(rows.len());
        for row in rows {
            assert!(matches!(row["role"].as_str(), Some("user" | "assistant")));
            let len = row["text"].as_str().unwrap().chars().count();
            assert!(len <= 500);
            lengths.push(len);
            let cursor = row["cursor"].as_str().unwrap().parse::<i64>().unwrap();
            assert!(cursor > 9_007_199_254_740_991);
            assert_eq!(
                row["role"].as_str(),
                Some(if (cursor - 9_007_199_254_741_000) % 2 == 0 {
                    "user"
                } else {
                    "assistant"
                })
            );
            seen.push(cursor);
        }
        if body["has_more"] == false {
            assert!(body["next_before"].is_null());
            break;
        }
        let next = body["next_before"].as_str().unwrap().to_owned();
        assert_eq!(next, rows[0]["cursor"]);
        before = Some(next);
        assert!(seen.len() < 24);
    }
    let mut sorted = seen.clone();
    sorted.sort_unstable();
    sorted.dedup();
    assert_eq!(page_sizes, [8, 8, 3]);
    assert_eq!(lengths.iter().filter(|&&len| len == 500).count(), 1);
    assert_eq!(seen.len(), 19);
    assert_eq!(
        sorted,
        (9_007_199_254_741_000..9_007_199_254_741_019).collect::<Vec<_>>()
    );
    assert_eq!(sorted.len(), seen.len());
    let (code, _, _) = request(addr, &format!("{path}?before=9223372036854775808"), true).await;
    assert_eq!(code, 400);
    // Hold the actual provider result on its way back over TLS, then commit a hide.
    std::fs::write(dir.path().join("hold-history"), b"").unwrap();
    let held_path = path.clone();
    let held = tokio::spawn(async move { request(addr, &held_path, true).await });
    let start = Instant::now();
    while !dir.path().join("history-held").exists() {
        assert!(
            start.elapsed() < Duration::from_secs(6),
            "provider never reached held response"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    rusqlite::Connection::open(&source_path)
        .unwrap()
        .execute("UPDATE sessions SET hidden=1 WHERE id='chat-a'", [])
        .unwrap();
    std::fs::write(dir.path().join("release-history"), b"").unwrap();
    let (code, body, raw) = held.await.unwrap();
    assert_eq!(code, 503);
    assert!(body["messages"].is_null());
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    let (code, body, raw) = request(addr, &path, true).await;
    assert_eq!(code, 503);
    assert!(body["messages"].is_null());
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    // Restore source visibility, then revoke the disposable pairing through the
    // real owner-only control socket (not a stand-in status response).
    rusqlite::Connection::open(&source_path)
        .unwrap()
        .execute("UPDATE sessions SET hidden=0 WHERE id='chat-a'", [])
        .unwrap();
    assert_eq!(request(addr, &path, true).await.0, 200);
    let mut control =
        tokio::net::UnixStream::connect(dir.path().join(".local/share/ares-peer/control.sock"))
            .await
            .unwrap();
    control
        .write_all(b"{\"op\":\"revoke\",\"peer\":\"node\"}\n")
        .await
        .unwrap();
    let mut reply = Vec::new();
    control.read_to_end(&mut reply).await.unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&reply).unwrap()["ok"], true);
    let (code, body, raw) = request(addr, &path, true).await;
    assert_eq!(code, 503);
    assert!(body["messages"].is_null());
    assert!(raw.to_ascii_lowercase().contains("cache-control: no-store"));
    let db = rusqlite::Connection::open(dir.path().join("ares.db")).unwrap();
    let events: i64 = db
        .query_row("SELECT count(*) FROM room_events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(events, initial_events, "history must not persist in Ares");
    server.abort();
    if let Some(home) = old_home {
        std::env::set_var("HOME", home);
    } else {
        std::env::remove_var("HOME");
    }
}
