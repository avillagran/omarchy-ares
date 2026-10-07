//! Real loopback HTTP/WS protocol fixtures; never discover or start an installed Desktop.
use ares_server::{
    broker::HermesBroker,
    integration::{
        with_store, ExecutionRunner, InterventionManager, LifecycleManager, SharedStore,
    },
    store::Store,
    sync::{sync_remote_hermes_chat, sync_remote_hermes_rooms},
    text, Result,
};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
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
                        let mut submitted = false;
                        while let Some(Ok(Message::Text(body))) = ws.next().await {
                            let request: Value = serde_json::from_str(&body).unwrap();
                            calls.lock().unwrap().push(request.clone());
                            let method = text(&request, "method");
                            if method == "test.disconnect" {
                                let _ = ws.close(None).await;
                                break;
                            }
                            let result = match method {
                                "session.resume" => {
                                    if submitted {
                                        json!({"session_id":"runtime","session_key":"stored","messages":[{"role":"user","content":"question"},{"role":"assistant","content":[{"text":"Final answer"}]}]})
                                    } else {
                                        json!({"session_id":"runtime","session_key":"stored","messages":[]})
                                    }
                                }
                                "image.attach_bytes" | "file.attach" => {
                                    json!({"attached":true,"ref_text":"fixture-attachment"})
                                }
                                "prompt.submit" => {
                                    submitted = true;
                                    for (kind, payload) in [
                                        ("message.delta", json!({"text":"Hello "})),
                                        ("message.delta", json!({"text":"world"})),
                                        ("reasoning.delta", json!({"text":"Plan"})),
                                        (
                                            "tool.progress",
                                            json!({"name":"terminal","text":"Running"}),
                                        ),
                                        (
                                            "approval.request",
                                            json!({"request_id":"approval-1","text":"Permission?","token":"sensitive"}),
                                        ),
                                    ] {
                                        ws.send(Message::Text(json!({"jsonrpc":"2.0","method":"event","params":{"session_id":"runtime","type":kind,"payload":payload}}).to_string().into())).await.unwrap();
                                    }
                                    json!({"submitted":true})
                                }
                                "approval.respond" => json!({"resolved":true}),
                                "session.close" => json!({"closed":false}),
                                "session.active_list" => json!({"sessions":[]}),
                                _ => json!({}),
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
        assert_eq!(bridge.call("profiles.list", json!({}), 3).await["ok"], true);
        Self {
            bridge,
            requests,
            task,
        }
    }
}
fn store(tmp: &tempfile::TempDir) -> SharedStore {
    Arc::new(Mutex::new(
        Store::open(
            &tmp.path().join("ares.db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap(),
    ))
}

#[tokio::test]
async fn paired_peer_sync_does_not_implicitly_read_remote_chat_history() -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let desktop = Desktop::start().await;
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    let helper = tmp.path().join("runtime-helper.sh");
    let log = tmp.path().join("calls.txt");
    std::fs::write(&helper, format!(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{}'\nprintf '%s\\n' '{{\"sessions\":[{{\"id\":\"remote::host::default::s1\"}}]}}'\n",
        log.display()
    ))?;
    std::fs::set_permissions(&helper, std::fs::Permissions::from_mode(0o700))?;
    ares_server::sync::sync_runtime(&store, &desktop.bridge, &helper).await?;
    let args = std::fs::read_to_string(&log)?;
    assert!(!args.lines().any(|line| line == "--session"),
        "Peer room sync must not fetch legacy gateway chat transcripts");
    Ok(())
}

#[tokio::test]
async fn execution_delivery_approval_pause_and_replay() -> Result<()> {
    let desktop = Desktop::start().await;
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    let (project, room, execution) = with_store(&store, |s| {
        let group = s.create_group("Shared", "Follow these instructions")?;
        let project = s.create_project(
            "Fixture",
            tmp.path().to_str().unwrap(),
            "local",
            Some(text(&group, "id")),
        )?;
        let room = s.create_room(text(&project, "id"), "Chat", &[json!("default")])?;
        let image=s.store_artifact(&json!({"projectId":project["id"],"roomId":room["id"],"name":"image.png","mime":"image/png","author":"user"}),b"fixture-image")?;
        let execution = s.accept_prompt(
            text(&project, "id"),
            text(&room, "id"),
            "default",
            "question",
            "once",
            &[image["id"].clone()],
        )?;
        Ok((project, room, execution))
    })?;
    let mut runner = ExecutionRunner::new(store.clone(), desktop.bridge.clone());
    runner.poll_interval = Duration::from_millis(1);
    runner.deadline = Duration::from_secs(2);
    let result = runner.run_execution(text(&execution, "id")).await?;
    assert_eq!(result["status"], "completed");
    assert_eq!(result["finalText"], "Final answer");
    runner.run_execution(text(&execution, "id")).await?;
    let approved = InterventionManager::approve(
        &store,
        &desktop.bridge,
        text(&execution, "id"),
        "approval-1",
        "once",
    )
    .await?;
    assert_eq!(approved["resolved"], true);
    let paused = LifecycleManager::pause_room(
        &store,
        &desktop.bridge,
        text(&project, "id"),
        text(&room, "id"),
        "pause",
    )
    .await?;
    assert_eq!(paused["memoryRelease"], "confirmed");
    assert_eq!(paused["sessions"][0]["releaseState"], "released");
    with_store(&store, |s| {
        assert!(s.live_events(text(&room, "id"))?.is_empty());
        let events = s.dtos(
            "room_events",
            "SELECT * FROM room_events WHERE room_id=? ORDER BY sequence",
            [text(&room, "id")],
        )?;
        assert!(events
            .iter()
            .any(|e| e["kind"] == "approval.request" && e["payload"]["token"] == "[redacted]"));
        assert!(events
            .iter()
            .any(|e| e["kind"] == "artifact.delivery.accepted"));
        assert!(events
            .iter()
            .any(|e| e["kind"] == "message.assistant" && e["payload"]["reconcilesDeltas"] == true));
        Ok(())
    })?;
    let requests = desktop.requests.lock().unwrap();
    let prompts: Vec<_> = requests
        .iter()
        .filter(|r| r["method"] == "prompt.submit")
        .collect();
    assert_eq!(prompts.len(), 1);
    let prompt = text(&prompts[0]["params"], "text");
    assert!(prompt.contains("Shared project-group instructions:\nFollow these instructions"));
    assert!(prompt.contains("Attachments:\nfixture-attachment"));
    assert!(requests.iter().any(|r| r["method"] == "image.attach_bytes"
        && r["params"]["content_base64"] == "Zml4dHVyZS1pbWFnZQ=="));
    Ok(())
}

#[tokio::test]
async fn persisted_http_and_reconnect_do_not_replay_mutations() {
    let desktop = Desktop::start().await;
    let response = desktop
        .bridge
        .call(
            "session.messages",
            json!({"session_id":"stored","profile":"default","limit":100,"since":0}),
            3,
        )
        .await;
    assert_eq!(response["result"]["messages"][0]["id"], "persisted");
    assert_eq!(response["result"]["session"]["last_activity_at"], 10);
    let unchanged = desktop
        .bridge
        .call(
            "session.messages",
            json!({"session_id":"stored","since":10}),
            3,
        )
        .await;
    assert_eq!(unchanged["result"]["messages"], json!([]));
    let lost = desktop.bridge.call("test.disconnect", json!({}), 3).await;
    assert_eq!(lost["ok"], false);
    tokio::time::timeout(Duration::from_secs(3), async {
        while !desktop.bridge.available() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        desktop.bridge.call("profiles.list", json!({}), 3).await["ok"],
        true
    );
    assert_eq!(
        desktop
            .requests
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r["method"] == "test.disconnect")
            .count(),
        1
    );
}

#[test]
fn remote_sessions_group_by_workspace_on_each_peer() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    let sessions = json!([
        {"id":"remote::host::default::a","peer":"host","profile":"host/default","title":"A","workspacePath":"/home/agent/work/alpha","lastActivityTs":10},
        {"id":"remote::host::default::b","peer":"host","profile":"host/default","title":"B","workspacePath":"/home/agent/work/beta","lastActivityTs":11},
        {"id":"remote::host::default::c","peer":"host","profile":"host/default","title":"C","workspacePath":"/home/agent/work/alpha","lastActivityTs":12},
        {"id":"remote::other::default::d","peer":"other","profile":"other/default","title":"D","workspacePath":"/home/agent/work/alpha","lastActivityTs":13},
        {"id":"remote::host::default::legacy","peer":"host","profile":"host/default","title":"No path","lastActivityTs":14}
    ]);
    sync_remote_hermes_rooms(&store, sessions.as_array().unwrap())?;
    with_store(&store, |s| {
        let rows = s.rows(
            "SELECT r.external_session_id AS session, p.name AS name, p.path AS path, p.node_id AS node FROM rooms r JOIN projects p ON p.id=r.project_id WHERE r.external_profile LIKE 'remote::%' ORDER BY r.external_session_id",
            [],
        )?;
        assert_eq!(rows.len(), 5);
        assert_eq!(rows[0]["name"], "alpha");
        assert_eq!(rows[0]["path"], "peer://host/home/agent/work/alpha");
        assert_eq!(rows[1]["name"], "beta");
        assert_eq!(rows[2]["path"], rows[0]["path"]);
        assert_eq!(rows[3]["name"], "Hermes");
        assert_eq!(rows[3]["path"], "peer://host/default");
        assert_eq!(rows[4]["path"], "peer://other/home/agent/work/alpha");
        assert_ne!(rows[0]["path"], rows[4]["path"]);
        Ok(())
    })?;
    Ok(())
}

#[test]
fn remote_room_keeps_verified_workspace_when_peer_temporarily_omits_it() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    let identified = json!([{"id":"remote::host::default::s1","peer":"host",
        "profile":"host/default","title":"Chat","workspacePath":"/home/agent/work/alpha",
        "lastActivityTs":10}]);
    sync_remote_hermes_rooms(&store, identified.as_array().unwrap())?;
    let missing = json!([{"id":"remote::host::default::s1","peer":"host",
        "profile":"host/default","title":"Chat","lastActivityTs":11}]);
    sync_remote_hermes_rooms(&store, missing.as_array().unwrap())?;
    with_store(&store, |s| {
        let rows = s.rows("SELECT p.path FROM rooms r JOIN projects p ON p.id=r.project_id WHERE r.external_session_id='remote::host::default::s1'", [])?;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["path"], "peer://host/home/agent/work/alpha");
        Ok(())
    })?;
    Ok(())
}

#[test]
fn remote_missing_workspace_uses_neutral_fallback_not_agent_title() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    let sessions = json!([{"id":"remote::host::default::s1","peer":"host",
        "profile":"host/default","agentTitle":"NodeBot","title":"Chat",
        "lastActivityTs":10}]);
    sync_remote_hermes_rooms(&store, sessions.as_array().unwrap())?;
    with_store(&store, |s| {
        let rows = s.rows("SELECT p.name,p.path FROM rooms r JOIN projects p ON p.id=r.project_id WHERE r.external_session_id=?", ["remote::host::default::s1"])?;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["name"], "Hermes");
        assert_eq!(rows[0]["path"], "peer://host/default");
        Ok(())
    })?;
    Ok(())
}

#[test]
fn remote_workspace_reassignment_keeps_room_and_project_scoped_history() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    let old = json!([{"id":"remote::host::default::s1","peer":"host",
        "profile":"host/default","title":"Chat","lastActivityTs":10}]);
    sync_remote_hermes_rooms(&store, old.as_array().unwrap())?;
    let (room_id, fallback_id, event_id) = with_store(&store, |s| {
        let room = s.rows("SELECT id,project_id FROM rooms WHERE external_session_id='remote::host::default::s1'", [])?.remove(0);
        let rid = text(&room, "id").to_owned();
        let pid = text(&room, "project_id").to_owned();
        s.update_room_alias(&rid, "Pinned display name")?;
        let eid = text(&s.rows("SELECT id FROM room_events WHERE room_id=? LIMIT 1", [&rid])?.remove(0), "id").to_owned();
        s.db.execute("INSERT INTO executions(id,project_id,room_id,bot_id,prompt,status,created_at) VALUES('e1',?,?,'default','p','completed',1)", rusqlite::params![pid,rid])?;
        s.db.execute("INSERT INTO artifacts(id,project_id,room_id,author,name,mime,size,sha256,path,created_at) VALUES('a1',?,?,'user','x','text/plain',0,'hash','/x',1)", rusqlite::params![pid,rid])?;
        Ok((rid,pid,eid))
    })?;
    let identified = json!([{"id":"remote::host::default::s1","peer":"host",
        "profile":"host/default","title":"Chat","workspacePath":"/home/agent/work/alpha",
        "lastActivityTs":11}]);
    sync_remote_hermes_rooms(&store, identified.as_array().unwrap())?;
    with_store(&store, |s| {
        let room = s.rows("SELECT r.id,r.alias,r.project_id,p.path FROM rooms r JOIN projects p ON p.id=r.project_id WHERE r.id=?", [&room_id])?.remove(0);
        let target = text(&room, "project_id");
        assert_ne!(target, fallback_id);
        assert_eq!(room["path"], "peer://host/home/agent/work/alpha");
        assert_eq!(room["alias"], "Pinned display name");
        let event = s.rows("SELECT id,project_id FROM room_events WHERE id=?", [&event_id])?.remove(0);
        assert_eq!(event["id"], event_id);
        assert_eq!(text(&event,"project_id"), target);
        for table in ["executions", "artifacts"] {
            let rows = s.rows(&format!("SELECT project_id FROM {table} WHERE room_id=?"), [&room_id])?;
            assert_eq!(rows.len(), 1);
            assert_eq!(text(&rows[0],"project_id"), target);
        }
        Ok(())
    })?;
    Ok(())
}

#[test]
fn source_archive_transitions_preserve_room_and_project_history() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    let reference = "remote::host::default::s1";
    let row = |archived| json!({"id":reference,"peer":"host","profile":"host/default",
        "workspacePath":"/source/project","title":"Chat","lastActivityTs":10,
        "externalArchived":archived});
    sync_remote_hermes_rooms(&store, &[row(false)])?;
    let (rid, pid, eid) = with_store(&store, |s| {
        let room = s.rows("SELECT id,project_id FROM rooms WHERE external_session_id=?", [reference])?.remove(0);
        let rid = text(&room,"id").to_owned();
        let pid = text(&room,"project_id").to_owned();
        s.update_room_alias(&rid,"Keep alias")?;
        s.db.execute("UPDATE rooms SET status='paused',deleted_at=123,control_included=0 WHERE id=?", [&rid])?;
        let eid = text(&s.rows("SELECT id FROM room_events WHERE room_id=? LIMIT 1", [&rid])?.remove(0),"id").to_owned();
        s.db.execute("INSERT INTO executions(id,project_id,room_id,bot_id,prompt,status,created_at) VALUES('e1',?,?,'default','p','completed',1)", rusqlite::params![pid,rid])?;
        s.db.execute("INSERT INTO artifacts(id,project_id,room_id,author,name,mime,size,sha256,path,created_at) VALUES('a1',?,?,'user','x','text/plain',0,'hash','/x',1)", rusqlite::params![pid,rid])?;
        Ok((rid,pid,eid))
    })?;
    for archived in [true,false] {
        sync_remote_hermes_rooms(&store, &[row(archived)])?;
        with_store(&store, |s| {
            let room = s.dtos("rooms","SELECT * FROM rooms WHERE id=?", [&rid])?.remove(0);
            assert_eq!(room["externalArchived"], archived);
            assert_eq!(room["projectId"], pid);
            assert_eq!(room["alias"], "Keep alias");
            assert_eq!(room["status"], "paused");
            assert_eq!(room["deletedAt"], 123.0);
            assert_eq!(room["controlIncluded"], false);
            for (table,id) in [("room_events",eid.as_str()),("executions","e1"),("artifacts","a1")] {
                let rows = s.rows(&format!("SELECT id,project_id,room_id FROM {table} WHERE id=?"), [id])?;
                assert_eq!(rows.len(),1);
                assert_eq!(rows[0]["project_id"],pid);
                assert_eq!(rows[0]["room_id"],rid);
            }
            assert_eq!(s.rows("SELECT id FROM rooms WHERE external_session_id=?", [reference])?.len(),1);
            Ok(())
        })?;
    }
    // An older catalog can only make the safe compatibility claim (false).
    let mut old = row(true);
    old.as_object_mut().unwrap().remove("externalArchived");
    sync_remote_hermes_rooms(&store, &[old])?;
    with_store(&store, |s| {
        let room = s.dtos("rooms","SELECT * FROM rooms WHERE id=?", [&rid])?.remove(0);
        assert_eq!(room["externalArchived"],false);
        Ok(())
    })
}

#[test]
fn remote_transcript_facets_are_idempotent_and_keep_dto_shapes() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    let reference = "remote::peer::default::session";
    sync_remote_hermes_rooms(
        &store,
        &[
            json!({"id":reference,"peer":"peer","profile":"peer/default","title":"Remote chat","lastActivityTs":10}),
        ],
    )?;
    let chat = json!({"id":reference,"lastActivityTs":12,"activity":"Done","messages":[{"id":"a","role":"assistant","content":"Answer","thinking":"Plan","timestamp":10,"tool_calls":[{"id":"call","function":{"name":"terminal","arguments":"{\"command\":\"pwd\"}"}}]},{"id":"t","role":"tool","toolName":"terminal","tool_call_id":"call","content":"{\"output\":\"fixture\",\"exit_code\":0,\"success\":true}","timestamp":12}]});
    assert_eq!(sync_remote_hermes_chat(&store, &chat)?, 4);
    assert_eq!(sync_remote_hermes_chat(&store, &chat)?, 0);
    with_store(&store, |s| {
        let events = s.dtos(
            "room_events",
            "SELECT * FROM room_events ORDER BY sequence",
            [],
        )?;
        assert_eq!(events.len(), 4);
        assert_eq!(events[0]["kind"], "reasoning.status");
        assert_eq!(events[1]["html"], "<p>Answer</p>");
        assert_eq!(events[2]["payload"]["text"], "$ pwd");
        assert_eq!(events[3]["payload"]["durationSeconds"], 2.0);
        assert_eq!(events[3]["payload"]["toolName"], "terminal");
        Ok(())
    })
}

#[test]
#[ignore = "optional Python reference conformance; requires python3"]
fn python_transcript_conformance() -> Result<()> {
    use std::io::Write;
    use std::process::{Command, Stdio};
    let tmp = tempfile::tempdir_in(concat!(env!("CARGO_MANIFEST_DIR"), "/test-work"))?;
    let store = store(&tmp);
    let sessions = json!([{"id":"remote::peer::default::session","peer":"peer","profile":"peer/default","title":"Chat","lastActivityTs":10}]);
    let chat = json!({"id":"remote::peer::default::session","lastActivityTs":12,"activity":"Done","messages":[
        {"id":101,"role":"user","content":[{"type":"text","text":"Question"}],"timestamp":9},
        {"id":102,"role":"assistant","content":"**Answer**","thinking":"Plan","token_count":20,"timestamp":10,"tool_calls":[{"id":"call","function":{"name":"terminal","arguments":"{\"command\":\"pwd\"}"}}]},
        {"id":103,"role":"tool","toolName":"terminal","tool_call_id":"call","content":"{\"output\":\"fixture\",\"exit_code\":0,\"success\":true}","timestamp":12}
    ]});
    sync_remote_hermes_rooms(&store, sessions.as_array().unwrap())?;
    sync_remote_hermes_chat(&store, &chat)?;
    let mut rust = with_store(&store, |s| {
        s.dtos(
            "room_events",
            "SELECT * FROM room_events ORDER BY sequence",
            [],
        )
    })?;
    for e in &mut rust {
        e.as_object_mut().unwrap().remove("projectId");
    }
    let script = r#"
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ares_service import AresStore, sync_remote_hermes_rooms, sync_remote_hermes_chat
root = Path(sys.argv[2])
data = json.load(sys.stdin)
store = AresStore(root / 'python.db', root / 'python-artifacts')
sync_remote_hermes_rooms(store, data['sessions'])
sync_remote_hermes_chat(store, data['chat'])
events = [store._event_dto(row) for row in store._db.execute('SELECT * FROM room_events ORDER BY sequence')]
for event in events:
    event.pop('projectId')
print(json.dumps(events))
store.close()
"#;
    let mut child = Command::new("python3")
        .args(["-I", "-B", "-c", script])
        .arg(concat!(env!("CARGO_MANIFEST_DIR"), "/../bin"))
        .arg(tmp.path())
        .env("HERMES_HOME", tmp.path().join("python-hermes"))
        .env("XDG_STATE_HOME", tmp.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    child.stdin.take().unwrap().write_all(
        json!({"sessions":sessions,"chat":chat})
            .to_string()
            .as_bytes(),
    )?;
    let output = child.wait_with_output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let python: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(json!(rust), python);
    Ok(())
}

#[test]
fn runtime_producers_preserve_preferences_and_replace_quotas() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store = store(&tmp);
    with_store(&store, |s| {
        s.sync_profiles(&[json!({"name":"default","display_name":"Desktop title","model":"model-a","provider":"provider"})])?;
        assert!(s.hermes_home.join("profiles/default/ares.json").is_file());
        assert!(!s.hermes_home.join("ares.json").exists());
        s.update_bot(
            "default",
            &json!({"name":"My bot","model":"model-b","provider":"provider"}),
        )?;
        s.sync_profiles(&[
            json!({"name":"default","display_name":"Changed upstream","model":"model-c"}),
        ])?;
        let bot = s.one("SELECT * FROM bots WHERE id='default'", [], "missing")?;
        assert_eq!(bot["name"], "My bot");
        assert_eq!(bot["model"], "model-b");
        s.sync_peers(&[
            json!({"name":"peer","title":"Upstream","status":"ok","authenticated":true}),
        ])?;
        s.update_peer_preferences("peer", "My peer", "info")?;
        s.sync_peers(&[
            json!({"name":"peer","title":"New upstream","status":"degraded","authenticated":true}),
        ])?;
        let peer = s.one("SELECT * FROM peers WHERE id='peer'", [], "missing")?;
        assert_eq!(peer["name"], "My peer");
        assert_eq!(peer["source_name"], "New upstream");
        assert_eq!(peer["color_role"], "info");
        s.sync_quotas(&json!({"updatedAt":10,"providers":[{"provider":"p","hourTokens":42,"quota":{"status":"ok","windows":[{"label":"Week","availablePercent":80,"usedPercent":20,"windowSeconds":604800,"resetAt":100}]}}]}))?;
        let quota = s.dtos("quota_providers", "SELECT * FROM quota_providers", [])?;
        assert_eq!(quota[0]["hourTokens"], 42);
        assert_eq!(quota[0]["windows"][0]["dailySeparators"], 6);
        assert_eq!(quota[0]["windows"][0]["resetAt"], 100.0);
        s.sync_quotas(&json!({"providers":[]}))?;
        assert!(s.rows("SELECT * FROM quota_windows", [])?.is_empty());
        assert!(s.rows("SELECT * FROM quota_providers", [])?.is_empty());
        Ok(())
    })
}
