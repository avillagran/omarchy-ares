use ares_server::{native_groups::NativeSnapshot, store::Store, text, Result};
use serde_json::{json, Value};
fn profiles(revision: u64, name: &str) -> Vec<Value> {
    vec![
        json!({"name":"default","ui_meta_revisions":{"hermes-bots-groups":revision},"ui_meta":{"hermes-bots-groups":{"version":3,"deleted":{},"rooms":{"id:abc":{"roomId":"abc","name":name,"revision":revision,"log":[{"text":"PRIVATE"}],"members":[{"name":"builder","connectionId":"local","connectionKind":"local"},{"name":"cto","connectionId":"local","connectionKind":"local"}]}}}}}),
    ]
}
fn store(tmp: &tempfile::TempDir) -> Store {
    Store::open(
        &tmp.path().join("db"),
        &tmp.path().join("artifacts"),
        tmp.path().join("hermes"),
    )
    .unwrap()
}
#[test]
fn two_members_are_one_identity_only_group() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s = store(&tmp);
    let snap = NativeSnapshot::from_profiles("local", &profiles(1, "Team"))?;
    s.sync_native_groups(&snap)?;
    s.sync_native_groups(&snap)?;
    let rooms = s.snapshot(None, None)?["rooms"].as_array().unwrap().clone();
    assert_eq!(rooms.len(), 1);
    let r = &rooms[0];
    assert_eq!(r["source"], "hermes-native-group");
    assert_eq!(r["controlIncluded"], false);
    assert!(s.set_control_included(text(r, "id"), true).is_err());
    assert_eq!(r["externalProfile"], "");
    assert_eq!(r["externalSessionId"], "");
    assert_eq!(r["nativeGroup"]["desktopKey"], "id:abc");
    assert_eq!(r["nativeGroup"]["members"].as_array().unwrap().len(), 2);
    assert!(!r.to_string().contains("PRIVATE"));
    assert!(s.rows("SELECT * FROM session_bindings", [])?.is_empty());
    assert!(s.rows("SELECT * FROM room_members", [])?.is_empty());
    assert!(!text(r, "id").is_empty());
    Ok(())
}

#[test]
fn tombstones_and_stale_snapshots_never_resurrect() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s = store(&tmp);
    let original = profiles(1, "Team");
    s.sync_native_groups(&NativeSnapshot::from_profiles("local", &original)?)?;
    let id = s.snapshot(None, None)?["rooms"][0]["id"]
        .as_str()
        .unwrap()
        .to_owned();
    s.db.execute("UPDATE rooms SET alias='Mine' WHERE id=?", [&id])?;
    s.sync_native_groups(&NativeSnapshot::from_profiles(
        "local",
        &profiles(2, "Renamed"),
    )?)?;
    s.sync_native_groups(&NativeSnapshot::from_profiles("local", &original)?)?;
    assert_eq!(s.room(&id, true)?["name"], "Renamed");
    assert_eq!(s.room(&id, true)?["alias"], "Mine");
    let mut deleted = profiles(3, "Deleted");
    deleted[0]["ui_meta"]["hermes-bots-groups"]["deleted"] =
        json!({"id:abc":0,"id:unseen":"corrupt"});
    s.sync_native_groups(&NativeSnapshot::from_profiles("local", &deleted)?)?;
    s.sync_native_groups(&NativeSnapshot::from_profiles(
        "local",
        &profiles(4, "Resurrect"),
    )?)?;
    assert!(s.snapshot(None, None)?["rooms"]
        .as_array()
        .unwrap()
        .is_empty());
    assert!(
        s.rows(
            "SELECT * FROM native_groups WHERE desktop_key='id:unseen'",
            []
        )?
        .len()
            == 1
    );
    Ok(())
}

use ares_server::{
    broker::HermesBroker,
    integration::{LifecycleManager, SharedStore},
    native_groups::discover_native_groups,
};
use futures_util::{SinkExt, StreamExt};
use std::sync::{Arc, Mutex};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};
use tokio_tungstenite::tungstenite::Message;
struct Desktop {
    bridge: HermesBroker,
    requests: Arc<Mutex<Vec<Value>>>,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for Desktop {
    fn drop(&mut self) {
        self.bridge.close();
        self.task.abort();
    }
}
impl Desktop {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let calls = requests.clone();
        let task = tokio::spawn(async move {
            loop {
                let (mut socket, _) = listener.accept().await.unwrap();
                let calls = calls.clone();
                tokio::spawn(async move {
                    let mut preview = [0; 4096];
                    let n = socket.peek(&mut preview).await.unwrap();
                    let line = String::from_utf8_lossy(&preview[..n]);
                    if line.starts_with("GET /api/ws?") {
                        let mut ws = tokio_tungstenite::accept_async(socket).await.unwrap();
                        while let Some(Ok(Message::Text(body))) = ws.next().await {
                            let request: Value = serde_json::from_str(&body).unwrap();
                            calls.lock().unwrap().push(request.clone());
                            let method = text(&request, "method");
                            if method == "test.disconnect" {
                                let _ = ws.close(None).await;
                                break;
                            }
                            if method == "profiles.list" {
                                let mut source = profiles(1, "Team");
                                source[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"] = json!((1..=205).map(|i| json!({"id":format!("m{i}"),"from":{"kind":"member","name":"builder","source":"local"},"text":format!("message {i}"),"thread":"t"})).collect::<Vec<_>>());
                                let result = json!({"profiles":source});
                                let _ = ws
                                    .send(Message::Text(
                                        json!({"jsonrpc":"2.0","id":request["id"],"result":result})
                                            .to_string()
                                            .into(),
                                    ))
                                    .await;
                                continue;
                            }
                            assert_eq!(method, "session.list");
                            assert_eq!(request["params"]["title"], "Group: abc");
                            let profile = text(&request["params"], "profile");
                            if profile == "timeout" {
                                continue;
                            }
                            let result = match profile {
                                "missing" => json!({"sessions":[]}),
                                "ambiguous" => {
                                    json!({"sessions":[{"id":"a","title":"Group: abc"},{"id":"b","title":"Group: abc"}]})
                                }
                                _ => {
                                    json!({"sessions":[{"id":format!("stored-{profile}"),"resolved_id":format!("tip-{profile}"),"title":"Group: abc","source":"desktop"}]})
                                }
                            };
                            if ws
                                .send(Message::Text(
                                    json!({"jsonrpc":"2.0","id":request["id"],"result":result})
                                        .to_string()
                                        .into(),
                                ))
                                .await
                                .is_err()
                            {
                                break;
                            }
                        }
                    } else {
                        let mut bytes = [0; 8192];
                        let n = socket.read(&mut bytes).await.unwrap();
                        let request = String::from_utf8_lossy(&bytes[..n]);
                        calls
                            .lock()
                            .unwrap()
                            .push(json!({"http":request.lines().next().unwrap_or("")}));
                        let body = if request.starts_with("GET / ") {
                            include_str!("fixtures/desktop.html").to_owned()
                        } else {
                            assert!(request
                                .to_ascii_lowercase()
                                .contains("authorization: bearer a+ /\"b"));
                            if request.contains("/messages?") {
                                json!({"messages":[{"id":"persisted","role":"assistant","content":"Persisted"}]}).to_string()
                            } else {
                                json!({"id":"stored","last_activity_at":10,"last_activity_description":"Ready"}).to_string()
                            }
                        };
                        let response=format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
                        socket.write_all(response.as_bytes()).await.unwrap();
                    }
                });
            }
        });
        let bridge = HermesBroker::with_endpoint(Some(
            url::Url::parse(&format!("http://{address}/")).unwrap(),
        ));

        Self {
            bridge,
            requests,
            task,
        }
    }
}

#[tokio::test]
async fn exact_group_rpc_is_read_only_and_members_never_duplicate() -> Result<()> {
    let desktop = Desktop::start().await;
    let tmp = tempfile::tempdir()?;
    let s: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let mut p = profiles(1, "Team");
    let members = p[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["members"]
        .as_array_mut()
        .unwrap();
    for name in ["missing", "ambiguous"] {
        members.push(json!({"name":name,"connectionId":"local","connectionKind":"local"}));
    }
    members.push(json!({"name":"remote","connectionId":"unknown","connectionKind":"remote"}));
    discover_native_groups(&s, &desktop.bridge, "local", &p).await?;
    let guard = s.lock().unwrap();
    let r = guard.snapshot(None, None)?["rooms"][0].clone();
    let members = r["nativeGroup"]["members"].as_array().unwrap();
    assert_eq!(members[0]["storedSessionId"], "stored-builder");
    assert_eq!(members[0]["resolvedSessionId"], "tip-builder");
    assert_eq!(members[2]["resolution"], "missing");
    assert_eq!(members[3]["resolution"], "ambiguous");
    assert_eq!(members[4]["resolution"], "unsupported");
    assert_eq!(members[4]["host"], "");
    let project = json!({"label":"Home","path":"hermes://home","repos":[{"groups":[{"sessions":[{"id":"stored-builder","title":"Anything","source":"desktop"},{"id":"tip-builder","source":"desktop"}]}]}]});
    assert_eq!(
        guard.sync_hermes_project("builder", &project, "local", true)?,
        0
    );
    assert_eq!(
        guard.snapshot(None, None)?["rooms"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert!(guard.rows("SELECT * FROM session_bindings", [])?.is_empty());
    let requests = desktop.requests.lock().unwrap();
    assert_eq!(
        requests
            .iter()
            .filter(|r| r["method"] == "session.list")
            .count(),
        4
    );
    Ok(())
}

#[tokio::test]
async fn native_groups_reject_generic_prompt_and_lifecycle_before_rpc() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let desktop = Desktop::start().await;
    let (project, room) = {
        let guard = s.lock().unwrap();
        guard.sync_native_groups(&NativeSnapshot::from_profiles(
            "local",
            &profiles(1, "Team"),
        )?)?;
        let r = guard.snapshot(None, None)?["rooms"][0].clone();
        assert!(guard
            .accept_prompt(
                text(&r, "projectId"),
                text(&r, "id"),
                "builder",
                "No send",
                "once",
                &[]
            )
            .is_err());
        assert!(guard
            .lifecycle(text(&r, "projectId"), text(&r, "id"), "resume", "resume")
            .is_err());
        (text(&r, "projectId").to_owned(), text(&r, "id").to_owned())
    };
    assert!(
        LifecycleManager::pause_room(&s, &desktop.bridge, &project, &room, "pause")
            .await
            .is_err()
    );
    assert!(desktop.requests.lock().unwrap().is_empty());
    let guard = s.lock().unwrap();
    assert!(guard.rows("SELECT * FROM executions", [])?.is_empty());
    guard.lifecycle(&project, &room, "archive", "archive")?;
    guard.sync_native_groups(&NativeSnapshot::from_profiles(
        "local",
        &profiles(2, "Renamed"),
    )?)?;
    assert!(guard.snapshot(None, None)?["rooms"]
        .as_array()
        .unwrap()
        .is_empty());
    Ok(())
}

#[tokio::test]
async fn failed_resolution_retains_verified_identity_and_parse_errors_keep_rooms() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let desktop = Desktop::start().await;
    discover_native_groups(&s, &desktop.bridge, "local", &profiles(1, "Team")).await?;
    desktop.bridge.close();
    discover_native_groups(&s, &desktop.bridge, "local", &profiles(2, "Team")).await?;
    let r = s.lock().unwrap().snapshot(None, None)?["rooms"][0].clone();
    assert_eq!(r["nativeGroup"]["members"][0]["resolution"], "error");
    assert_eq!(
        r["nativeGroup"]["members"][0]["storedSessionId"],
        "stored-builder"
    );
    for broken in [json!(null), json!({"version":3,"rooms":{},"deleted":[]})] {
        let mut p = profiles(3, "Team");
        p[0]["ui_meta"]["hermes-bots-groups"] = broken;
        assert!(discover_native_groups(&s, &desktop.bridge, "local", &p)
            .await
            .is_err());
        assert_eq!(s.lock().unwrap().snapshot(None, None)?["rooms"][0], r);
    }
    Ok(())
}

#[test]
fn legacy_tombstones_compare_revision_and_corrupt_values_fail_closed() -> Result<()> {
    for (tombstone, visible) in [(json!(1), true), (json!(2), false), (json!("bad"), false)] {
        let tmp = tempfile::tempdir()?;
        let s = store(&tmp);
        let mut p = profiles(2, "Legacy");
        let snapshot = &mut p[0]["ui_meta"]["hermes-bots-groups"];
        let room = snapshot["rooms"]
            .as_object_mut()
            .unwrap()
            .remove("id:abc")
            .unwrap();
        snapshot["rooms"]["legacy-key"] = room;
        snapshot["deleted"]["legacy-key"] = tombstone;
        s.sync_native_groups(&NativeSnapshot::from_profiles("local", &p)?)?;
        assert_eq!(
            !s.snapshot(None, None)?["rooms"]
                .as_array()
                .unwrap()
                .is_empty(),
            visible
        );
    }
    Ok(())
}

#[test]
fn corrupt_new_tombstone_overrides_old_numeric_tombstone() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s = store(&tmp);
    let mut p = profiles(2, "Legacy");
    let snapshot = &mut p[0]["ui_meta"]["hermes-bots-groups"];
    let room = snapshot["rooms"]
        .as_object_mut()
        .unwrap()
        .remove("id:abc")
        .unwrap();
    snapshot["rooms"]["legacy-key"] = room;
    snapshot["deleted"]["legacy-key"] = json!(1);
    s.sync_native_groups(&NativeSnapshot::from_profiles("local", &p)?)?;
    p[0]["ui_meta"]["hermes-bots-groups"]["deleted"]["legacy-key"] = json!({"bad":true});
    s.sync_native_groups(&NativeSnapshot::from_profiles("local", &p)?)?;
    assert!(s.snapshot(None, None)?["rooms"]
        .as_array()
        .unwrap()
        .is_empty());
    Ok(())
}

#[tokio::test]
async fn member_timeout_is_metadata_not_deletion_and_does_not_hold_store_lock() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let desktop = Desktop::start().await;
    let mut p = profiles(1, "Timeout");
    p[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["members"] =
        json!([{"name":"timeout","connectionId":"local","connectionKind":"local"}]);
    let copy = s.clone();
    let bridge = desktop.bridge.clone();
    let task =
        tokio::spawn(async move { discover_native_groups(&copy, &bridge, "local", &p).await });
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    assert!(s.try_lock().is_ok());
    tokio::time::timeout(std::time::Duration::from_secs(7), task).await???;
    let r = s.lock().unwrap().snapshot(None, None)?["rooms"][0].clone();
    assert_eq!(r["nativeGroup"]["members"][0]["resolution"], "error");
    Ok(())
}

#[tokio::test]
#[ignore = "Explicit read-only live Desktop probe; writes only a disposable Ares store"]
async fn live_native_group_catalog_read_only() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let bridge = HermesBroker::start();
    let response = bridge
        .call("profiles.list", json!({"include_sessions":false}), 15)
        .await;
    assert_eq!(
        response["ok"], true,
        "Existing Desktop service must be available; never start one in this probe"
    );
    let profiles = response["result"]["profiles"]
        .as_array()
        .ok_or("Missing profiles")?;
    let result = discover_native_groups(&store, &bridge, "local", profiles).await;
    bridge.close();
    result?;
    let store = store.lock().unwrap();
    let snapshot = store.snapshot(None, None)?;
    let rooms = snapshot["rooms"].as_array().ok_or("Missing room catalog")?;
    assert!(
        !rooms.is_empty(),
        "Expected existing native rooms for live acceptance"
    );
    for room in rooms {
        assert_eq!(room["externalSessionId"], "");
        println!(
            "{}",
            json!({"name":room["name"],"nativeGroup":room["nativeGroup"]})
        );
    }
    for table in [
        "session_bindings",
        "terminal_bindings",
        "executions",
        "room_events",
    ] {
        assert!(store
            .rows(&format!("SELECT * FROM {table}"), [])?
            .is_empty());
    }
    Ok(())
}

#[tokio::test]
async fn previously_imported_member_is_retired_without_losing_state() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let desktop = Desktop::start().await;
    let project = json!({"label":"Home","path":"hermes://home","repos":[{"groups":[{"sessions":[{"id":"stored-builder","title":"Existing","source":"desktop"}]}]}]});
    let (room, pid, queued, running, preserved) = {
        let g = s.lock().unwrap();
        g.sync_hermes_project("builder", &project, "local", true)?;
        let r = g.snapshot(None, None)?["rooms"][0].clone();
        let room = text(&r, "id").to_owned();
        let pid = text(&r, "projectId").to_owned();
        g.db.execute("UPDATE rooms SET alias='Keep' WHERE id=?", [&room])?;
        let queued = g.accept_prompt(&pid, &room, "default", "queued", "q", &[])?;
        let running = g.accept_prompt(&pid, &room, "default", "running", "r", &[])?;
        assert!(g.start_execution(text(&running, "id"))?);
        g.db.execute(
            "INSERT INTO terminal_bindings VALUES(?,'local','fake-terminal','hermes',1)",
            [&room],
        )?;
        g.db.execute("INSERT INTO artifacts(id,project_id,room_id,author,name,mime,size,sha256,path,created_at) VALUES('artifact',?,?,'user','keep','text/plain',0,'hash','fake',1)",rusqlite::params![pid,room])?;
        let preserved = [
            "room_events",
            "artifacts",
            "session_bindings",
            "terminal_bindings",
            "command_receipts",
        ]
        .map(|t| g.rows(&format!("SELECT * FROM {t}"), []).unwrap());
        (room, pid, queued, running, preserved)
    };
    discover_native_groups(&s, &desktop.bridge, "local", &profiles(1, "Team")).await?;
    let g = s.lock().unwrap();
    assert_eq!(g.room(&room, true)?["source"], "hermes-native-member");
    assert_eq!(g.room(&room, true)?["alias"], "Keep");
    assert_eq!(
        g.snapshot(None, None)?["rooms"].as_array().unwrap().len(),
        1
    );
    for (t, before) in [
        "room_events",
        "artifacts",
        "session_bindings",
        "terminal_bindings",
        "command_receipts",
    ]
    .iter()
    .zip(preserved)
    {
        assert_eq!(g.rows(&format!("SELECT * FROM {t}"), [])?, before);
    }
    assert!(g
        .accept_prompt(&pid, &room, "default", "blocked", "b", &[])
        .is_err());
    assert!(!g.start_execution(text(&queued, "id"))?);
    assert_eq!(g.execution(text(&running, "id"))?["status"], "running");
    for action in ["pause", "resume"] {
        assert!(g.lifecycle(&pid, &room, action, action).is_err());
    }
    assert_eq!(
        g.sync_hermes_project("builder", &project, "local", true)?,
        0
    );
    assert_eq!(g.room(&room, true)?["source"], "hermes-native-member");
    Ok(())
}

fn legacy_profiles(revision: u64) -> Vec<Value> {
    let mut p = profiles(revision, "Legacy");
    let snapshot = &mut p[0]["ui_meta"]["hermes-bots-groups"];
    let room = snapshot["rooms"]
        .as_object_mut()
        .unwrap()
        .remove("id:abc")
        .unwrap();
    snapshot["rooms"]["legacy-key"] = room;
    p
}

#[test]
fn legacy_revival_preserves_local_archive_and_allows_restore() -> Result<()> {
    for archived in [false, true] {
        let tmp = tempfile::tempdir()?;
        let s = store(&tmp);
        s.sync_native_groups(&NativeSnapshot::from_profiles(
            "local",
            &legacy_profiles(1),
        )?)?;
        let r = s.snapshot(None, None)?["rooms"][0].clone();
        let id = text(&r, "id");
        let project = text(&r, "projectId");
        if archived {
            s.lifecycle(project, id, "archive", "archive")?;
        }
        let mut deleted = legacy_profiles(2);
        deleted[0]["ui_meta"]["hermes-bots-groups"]["deleted"] = json!({"legacy-key":2});
        s.sync_native_groups(&NativeSnapshot::from_profiles("local", &deleted)?)?;
        assert!(s.snapshot(None, None)?["rooms"]
            .as_array()
            .unwrap()
            .is_empty());
        assert!(s.lifecycle(project, id, "bad-restore", "restore").is_err());
        s.sync_native_groups(&NativeSnapshot::from_profiles(
            "local",
            &legacy_profiles(3),
        )?)?;
        assert_eq!(
            s.snapshot(None, None)?["rooms"]
                .as_array()
                .unwrap()
                .is_empty(),
            archived
        );
        if archived {
            s.lifecycle(project, id, "restore", "restore")?;
        }
        assert_eq!(s.snapshot(None, None)?["rooms"][0]["id"], id);
    }
    Ok(())
}

#[tokio::test]
async fn group_identity_mismatch_is_rejected_before_rpc() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let desktop = Desktop::start().await;
    let mut p = profiles(1, "Invalid");
    p[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["roomId"] = json!("other");
    assert!(discover_native_groups(&s, &desktop.bridge, "local", &p)
        .await
        .is_err());
    assert!(desktop.requests.lock().unwrap().is_empty());
    Ok(())
}

#[tokio::test]
async fn legacy_identity_mutation_cannot_retain_old_session_on_rpc_error() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let desktop = Desktop::start().await;
    discover_native_groups(&s, &desktop.bridge, "local", &legacy_profiles(1)).await?;
    let before = s.lock().unwrap().snapshot(None, None)?["rooms"].clone();
    desktop.bridge.close();
    let mut p = legacy_profiles(2);
    p[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["legacy-key"]["roomId"] = json!("other");
    assert!(discover_native_groups(&s, &desktop.bridge, "local", &p)
        .await
        .is_err());
    assert_eq!(s.lock().unwrap().snapshot(None, None)?["rooms"], before);
    Ok(())
}

#[tokio::test]
async fn reconciliation_requires_exact_host_profile_and_blocks_unbound_start() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s: SharedStore = Arc::new(Mutex::new(store(&tmp)));
    let desktop = Desktop::start().await;
    let (queued, room) = {
        let g = s.lock().unwrap();
        let project = json!({"label":"Home","path":"hermes://home","repos":[{"groups":[{"sessions":[{"id":"tip-builder","source":"desktop"}]}]}]});
        g.sync_hermes_project("builder", &project, "local", true)?;
        let r = g.snapshot(None, None)?["rooms"][0].clone();
        let queued = g.accept_prompt(
            text(&r, "projectId"),
            text(&r, "id"),
            "default",
            "queued",
            "q",
            &[],
        )?;
        g.db.execute("INSERT INTO projects(id,name,node_id,path,location_key,created_at) VALUES('remote-project','Remote','peer','peer://test','remote-key',1)",[])?;
        g.db.execute("INSERT INTO rooms(id,project_id,name,status,memory_release,source,external_profile,external_session_id,created_at,updated_at) VALUES('different-host','remote-project','Other','active','not-requested','hermes','builder','stored-builder',1,1),('different-profile',?,'Other','active','not-requested','hermes','other','tip-builder',1,1)",[text(&r,"projectId")])?;
        (queued, r)
    };
    discover_native_groups(&s, &desktop.bridge, "local", &profiles(1, "Team")).await?;
    let g = s.lock().unwrap();
    assert_eq!(g.room("different-host", true)?["source"], "hermes");
    assert_eq!(g.room("different-profile", true)?["source"], "hermes");
    assert_eq!(
        g.room(text(&room, "id"), true)?["source"],
        "hermes-native-member"
    );
    assert!(!g.start_execution(text(&queued, "id"))?);
    assert!(g
        .accept_prompt(
            text(&room, "projectId"),
            text(&room, "id"),
            "default",
            "new",
            "new",
            &[]
        )
        .is_err());
    Ok(())
}

#[test]
fn old_native_deletion_migrates_without_erasing_local_archive() -> Result<()> {
    for archived in [false, true] {
        let tmp = tempfile::tempdir()?;
        let s = store(&tmp);
        s.sync_native_groups(&NativeSnapshot::from_profiles(
            "local",
            &legacy_profiles(1),
        )?)?;
        let r = s.snapshot(None, None)?["rooms"][0].clone();
        let id = text(&r, "id");
        if archived {
            s.lifecycle(text(&r, "projectId"), id, "archive", "archive")?;
        }
        s.db.execute("UPDATE native_groups SET tombstone='2'", [])?;
        s.db.execute(
            "UPDATE rooms SET deleted_at=1,status='paused' WHERE id=?",
            [id],
        )?;
        s.db.execute("ALTER TABLE rooms DROP COLUMN native_suppressed", [])?;
        drop(s);
        let s = store(&tmp);
        s.sync_native_groups(&NativeSnapshot::from_profiles(
            "local",
            &legacy_profiles(3),
        )?)?;
        assert_eq!(
            s.snapshot(None, None)?["rooms"]
                .as_array()
                .unwrap()
                .is_empty(),
            archived
        );
    }
    Ok(())
}

#[test]
fn newer_empty_snapshot_fences_unseen_rooms_from_old_snapshots() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s = store(&tmp);
    let mut empty = profiles(5, "Empty");
    empty[0]["ui_meta"]["hermes-bots-groups"]["rooms"] = json!({});
    s.sync_native_groups(&NativeSnapshot::from_profiles("local", &empty)?)?;
    s.sync_native_groups(&NativeSnapshot::from_profiles(
        "local",
        &profiles(1, "Stale"),
    )?)?;
    assert!(s.snapshot(None, None)?["rooms"]
        .as_array()
        .unwrap()
        .is_empty());
    Ok(())
}

#[test]
fn source_group_history_pages_one_existing_group_without_persisting_log() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let s = store(&tmp);
    let mut source = profiles(1, "Team");
    let log = (1..=205).map(|i| json!({"id":format!("m{i}"),"from":{"kind":if i==1 {"user"} else {"member"},"name":if i==1 {"You"} else {"builder"},"source":"local"},"text":format!("message {i}"),"at":i*1000,"thread":"t"})).collect::<Vec<_>>();
    source[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"] = json!(log);
    s.sync_native_groups(&NativeSnapshot::from_profiles("local", &source)?)?;
    let room = s.snapshot(None, None)?["rooms"][0]["id"]
        .as_str()
        .unwrap()
        .to_owned();
    let first = s.native_group_history(&room, &source, i64::MAX)?;
    assert_eq!(first["messages"].as_array().unwrap().len(), 200);
    assert!(first["oldestSeq"].as_i64().unwrap() > 6);
    assert_eq!(first["hasOlder"], true);
    let older = s.native_group_history(&room, &source, first["oldestSeq"].as_i64().unwrap())?;
    assert_eq!(older["messages"].as_array().unwrap().len(), 5);
    assert_eq!(older["messages"][0]["seq"], 1);
    assert_eq!(older["messages"][0]["role"], "user");
    assert_eq!(older["messages"][0]["thread"], "t");
    assert_eq!(older["messages"][0]["author"], "user");
    assert_eq!(older["hasOlder"], false);
    assert!(s
        .rows("SELECT * FROM room_events WHERE room_id=?", [&room])?
        .is_empty());
    source[0]["ui_meta"]["hermes-bots-groups"]["deleted"] = json!({"id:abc":0});
    assert!(s.native_group_history(&room, &source, i64::MAX).is_err());
    Ok(())
}

#[tokio::test]
async fn desktop_broker_cannot_bypass_group_history_connect_info_guard() -> Result<()> {
    let desktop = Desktop::start().await;
    let tmp = tempfile::tempdir()?;
    let s = store(&tmp);
    s.sync_native_groups(&NativeSnapshot::from_profiles(
        "local",
        &profiles(1, "Team"),
    )?)?;
    let room = s.snapshot(None, None)?["rooms"][0]["id"]
        .as_str()
        .unwrap()
        .to_owned();
    let app = ares_server::http::integrated_router(ares_server::http::App {
        store: Arc::new(Mutex::new(s)),
        token_hash: ares_server::digest("test-token"),
        web_root: tmp.path().to_path_buf(),
        bridge: Some(desktop.bridge.clone()),
        mirror: None,
    });
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let url = format!("http://{}", listener.local_addr()?);
    let task = tokio::spawn(async move {
        // Deliberately omit ConnectInfo despite the loopback listener.
        axum::serve(listener, app.into_make_service())
            .await
            .unwrap();
    });
    let client = reqwest::Client::builder().no_proxy().build()?;
    let endpoint = format!("{url}/v1/groups/{room}/history");
    assert_eq!(client.get(&endpoint).send().await?.status(), 401);
    let response = client
        .get(&endpoint)
        .bearer_auth("test-token")
        .send()
        .await?;
    assert_eq!(response.status(), 403);
    let body = response.text().await?;
    assert!(!body.contains("messages"));
    assert!(!body.contains("message 205"));
    assert!(!body.contains("PRIVATE"));
    assert_eq!(
        client
            .get(format!("{endpoint}?before=6"))
            .bearer_auth("test-token")
            .send()
            .await?
            .status(),
        403
    );
    assert!(!desktop
        .requests
        .lock()
        .unwrap()
        .iter()
        .any(|request| request["method"] == "profiles.list"));
    assert_eq!(
        client
            .get(format!("{endpoint}?before=0"))
            .bearer_auth("test-token")
            .send()
            .await?
            .status(),
        400
    );
    // POST without a body is rejected by the common request-size guard first.
    assert_eq!(
        client
            .post(&endpoint)
            .bearer_auth("test-token")
            .send()
            .await?
            .status(),
        400
    );
    assert_eq!(
        client
            .put(&endpoint)
            .bearer_auth("test-token")
            .send()
            .await?
            .status(),
        405
    );
    task.abort();
    Ok(())
}

fn history_fixture() -> Result<(tempfile::TempDir, Store, Vec<Value>, String)> {
    let tmp = tempfile::tempdir()?;
    let s = store(&tmp);
    let mut source = profiles(1, "Team");
    source[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"] = json!((1..=205).map(|i| json!({"id":format!("m{i}"),"from":{"kind":"member","name":"builder","source":"local"},"text":format!("message {i}"),"thread":"t","at":i})).collect::<Vec<_>>());
    s.sync_native_groups(&NativeSnapshot::from_profiles("local", &source)?)?;
    let room = text(&s.snapshot(None, None)?["rooms"][0], "id").to_owned();
    Ok((tmp, s, source, room))
}

#[test]
fn group_history_rejects_unknown_assistant_actor() -> Result<()> {
    let (_tmp, s, mut source, room) = history_fixture()?;
    source[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"][204]["from"]["name"] =
        json!("intruder");
    assert!(s.native_group_history(&room, &source, i64::MAX).is_err());
    Ok(())
}

#[test]
fn group_history_refuses_index_cursor_after_late_insertion() -> Result<()> {
    let (_tmp, s, mut source, room) = history_fixture()?;
    let first = s.native_group_history(&room, &source, i64::MAX)?;
    let before = first["oldestSeq"].as_i64().unwrap();
    let inserted = json!({"id":"late","from":{"kind":"member","name":"builder","source":"local"},"text":"late","thread":"t","at":0});
    source[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"]
        .as_array_mut()
        .unwrap()
        .insert(0, inserted);
    assert!(s.native_group_history(&room, &source, before).is_err());
    Ok(())
}

#[test]
fn group_history_rejects_malformed_event_metadata_and_wrong_source() -> Result<()> {
    for (field, value) in [
        ("id", json!(null)),
        ("id", json!("x".repeat(257))),
        ("id", json!("bad\nline")),
        ("thread", json!("x".repeat(257))),
        ("thread", json!({"invalid":true})),
        ("from.source", json!("elsewhere")),
        ("from.source", json!("x".repeat(257))),
    ] {
        let (_tmp, s, mut source, room) = history_fixture()?;
        let event = &mut source[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"][204];
        if field == "from.source" {
            event["from"]["source"] = value;
        } else {
            event[field] = value;
        }
        assert!(
            s.native_group_history(&room, &source, i64::MAX).is_err(),
            "{field}"
        );
    }
    Ok(())
}

#[test]
fn group_history_caps_the_serialized_page_and_rejects_legacy_index() -> Result<()> {
    let (_tmp, s, mut source, room) = history_fixture()?;
    for event in source[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"]
        .as_array_mut()
        .unwrap()
    {
        event["text"] = json!("s".repeat(16_000));
    }
    let first = s.native_group_history(&room, &source, i64::MAX)?;
    assert!(first["messages"].as_array().unwrap().len() < 200);
    assert!(serde_json::to_vec(&first)?.len() <= 512 * 1024);
    assert!(s.native_group_history(&room, &source, 6).is_err());
    let older = s.native_group_history(&room, &source, first["oldestSeq"].as_i64().unwrap())?;
    assert!(!older["messages"].as_array().unwrap().is_empty());
    Ok(())
}

#[test]
fn group_history_accepts_source_local_member_event_without_source_field() -> Result<()> {
    let (_tmp, s, mut source, room) = history_fixture()?;
    source[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"][204]["from"]
        .as_object_mut()
        .unwrap()
        .remove("source");
    let page = s.native_group_history(&room, &source, i64::MAX)?;
    assert_eq!(
        page["messages"].as_array().unwrap().last().unwrap()["author"],
        "builder"
    );
    assert_eq!(
        page["messages"].as_array().unwrap().last().unwrap()["source"],
        ""
    );
    Ok(())
}
