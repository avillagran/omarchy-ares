//! Desktop discovery/persisted sync and the intentionally retained runtime helper boundary.
use crate::{
    broker::HermesBroker,
    digest,
    integration::{with_store, HermesLiveTranscriptMirror, SharedStore},
    producers::{array, first},
    text, Result,
};
use rusqlite::params;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet, HashSet},
    path::{Path, PathBuf},
    process::Stdio,
    sync::{Arc, Mutex},
    time::Duration,
};

pub async fn sync_hermes_rooms(
    store: &SharedStore,
    bridge: &HermesBroker,
    profiles: &[Value],
) -> Result<()> {
    let mut names: BTreeSet<_> = profiles
        .iter()
        .map(|p| first(p, &["name", "profile"], "").trim())
        .filter(|s| !s.is_empty())
        .collect();
    if names.is_empty() {
        names.insert("default");
    }
    for profile in names {
        let tree = bridge
            .call(
                "projects.tree",
                json!({"profile":profile,"preview_limit":0,"session_limit":5000}),
                30,
            )
            .await;
        if tree["ok"] != true {
            eprintln!("sync: projects.tree failed for {profile}: {}", tree["error"]);
            continue;
        }
        for project in array(&tree["result"]["projects"]) {
            if project["sessionCount"].as_u64().unwrap_or(0) < 1 {
                continue;
            }
            let hydrated = bridge
                .call(
                    "projects.project_sessions",
                    json!({"profile":profile,"project_id":text(project,"id"),"session_limit":5000}),
                    30,
                )
                .await;
            if hydrated["ok"] != true {
                eprintln!("sync: project_sessions failed for profile={profile} id={} err={}",
                    text(project, "id"), hydrated["error"]);
                continue;
            }
            // A profile-scoped hydration returns no project for ids the profile does not
            // own (the tree is global); that is an expected skip, not an error.
            if !hydrated["result"]["project"].is_object() {
                continue;
            }
            let mut p = hydrated["result"]["project"].clone();
            let virtual_project = crate::store::truthy(&project["isNoProject"]);
            if virtual_project {
                p["label"] = json!(crate::truncate(
                    first(project, &["label"], first(&p, &["label"], "Home")),
                    256
                ));
                p["path"] = json!("hermes://home");
            }
            if let Err(e) = with_store(store, |s| {
                s.sync_hermes_project(profile, &p, "local", virtual_project)
            }) {
                eprintln!("sync: sync_hermes_project failed for {profile} {}: {e}", text(&p, "path"));
                return Err(e);
            }
        }
    }
    Ok(())
}
pub fn sync_remote_hermes_rooms(store: &SharedStore, sessions: &[Value]) -> Result<()> {
    let mut groups: BTreeMap<(String, String, String, String, String), Vec<Value>> =
        BTreeMap::new();
    // A peer may temporarily omit workspace metadata (e.g. an older gateway).
    // Do not demote a room whose project path was previously verified.
    let existing = with_store(store, |s| {
        s.rows(
        "SELECT r.external_profile,r.external_session_id,p.name,p.path,p.node_id FROM rooms r JOIN projects p ON p.id=r.project_id WHERE r.external_profile LIKE 'remote::%' AND r.deleted_at IS NULL",
        [],
    )
    })?;
    let mut known = BTreeMap::new();
    for row in existing {
        known.insert(
            (
                text(&row, "external_profile").to_owned(),
                text(&row, "external_session_id").to_owned(),
            ),
            row,
        );
    }
    for s in sessions {
        let peer = text(s, "peer").trim();
        let id = text(s, "id").trim();
        if peer.is_empty() || !id.starts_with(&format!("remote::{peer}::")) {
            continue;
        }
        let identity = first(s, &["profile"], &format!("{peer}/default")).to_owned();
        let profile = identity
            .strip_prefix(&format!("{peer}/"))
            .unwrap_or("default");
        let mut word_start = true;
        let title_case: String = profile
            .chars()
            .flat_map(|c| {
                let s = if word_start {
                    c.to_uppercase().collect::<String>()
                } else {
                    c.to_lowercase().collect()
                };
                word_start = !c.is_alphabetic();
                s.chars().collect::<Vec<_>>()
            })
            .collect();
        let key = format!("remote::{peer}::{profile}");
        let workspace = text(s, "workspacePath").trim().trim_end_matches('/');
        let workspace_path = std::path::Path::new(workspace);
        let valid_workspace = workspace_path.is_absolute()
            && !workspace.is_empty()
            && !workspace.contains('\0')
            && workspace.len() <= 4096
            && workspace_path.components().all(|part| {
                !matches!(
                    part,
                    std::path::Component::ParentDir | std::path::Component::CurDir
                )
            });
        let previous = known.get(&(key.clone(), id.to_owned())).filter(|row| {
            let path = text(row, "path");
            text(row, "node_id") == peer
                && path.starts_with(&format!("peer://{peer}/"))
                && path != format!("peer://{peer}/{profile}")
        });
        let (label, path) = if valid_workspace {
            (
                crate::truncate(
                    workspace_path
                        .file_name()
                        .and_then(|s| s.to_str())
                        .unwrap_or(profile),
                    256,
                ),
                format!("peer://{peer}{workspace}"),
            )
        } else if let Some(previous) = previous {
            (
                text(previous, "name").to_owned(),
                text(previous, "path").to_owned(),
            )
        } else {
            (
                if profile == "default" { "Hermes" } else { &title_case }.to_owned(),
                format!("peer://{peer}/{profile}"),
            )
        };
        groups.entry((peer.into(),profile.into(),label,key,path)).or_default().push(json!({"id":id,"title":first(s,&["title"],id),"source":"peer","last_active":s["lastActivityTs"].as_f64().unwrap_or(0.0),"externalArchived":s["externalArchived"].as_bool().unwrap_or(false)}));
    }
    for ((peer, profile, title, key, path), sessions) in groups {
        with_store(store, |s| {
            if profile == "default" && path == format!("peer://{peer}/default") {
                s.transaction(|s| {
                    let legacy = s.rows(
                        "SELECT id FROM projects WHERE location_key=?",
                        [digest(format!("{peer}\0peer://{peer}"))],
                    )?;
                    let target = s.rows(
                        "SELECT id FROM projects WHERE location_key=?",
                        [digest(format!("{peer}\0{path}"))],
                    )?;
                    if let Some(legacy) = legacy.first() {
                        let old = text(legacy, "id");
                        if let Some(target) = target.first() {
                            let target = text(target, "id");
                            for table in ["rooms", "executions", "room_events", "artifacts"] {
                                s.db.execute(
                                    &format!("UPDATE {table} SET project_id=? WHERE project_id=?"),
                                    params![target, old],
                                )?;
                            }
                            s.db.execute("DELETE FROM projects WHERE id=?", [old])?;
                        } else {
                            s.db.execute(
                                "UPDATE projects SET name=?,path=?,location_key=? WHERE id=?",
                                params![title, path, digest(format!("{peer}\0{path}")), old],
                            )?;
                        }
                    }
                    Ok(())
                })?;
            }
            s.sync_hermes_project(
                &key,
                &json!({"label":title,"path":path,"repos":[{"groups":[{"sessions":sessions}]}]}),
                &peer,
                false,
            )
        })?;
    }
    Ok(())
}
pub fn sync_remote_hermes_chat(store: &SharedStore, chat: &Value) -> Result<usize> {
    let reference = text(chat, "id").trim();
    let parts: Vec<_> = reference.splitn(4, "::").collect();
    if parts.len() != 4 || parts[0] != "remote" || parts[1..].iter().any(|s| s.is_empty()) {
        return Ok(0);
    }
    let messages: Vec<_> = array(&chat["messages"])
        .iter()
        .filter(|m| m.is_object())
        .map(|m| {
            let mut m = m.clone();
            if !crate::store::truthy(&m["tool_name"]) && crate::store::truthy(&m["toolName"]) {
                m["tool_name"] = m["toolName"].clone();
            }
            if !crate::store::truthy(&m["reasoning_content"])
                && crate::store::truthy(&m["thinking"])
            {
                m["reasoning_content"] = m["thinking"].clone();
            }
            m
        })
        .collect();
    with_store(store, |s| {
        s.sync_hermes_transcript(&format!("remote::{}::{}",parts[1],parts[2]),reference,&messages,&json!({"last_activity_at":chat["lastActivityTs"],"last_activity_description":text(chat,"activity")}))
    })
}
pub async fn sync_recent_hermes_transcripts(
    store: &SharedStore,
    bridge: &HermesBroker,
    limit: usize,
) -> Result<()> {
    let mut rows = with_store(store, |s| {
        s.rows("SELECT r.external_profile,r.external_session_id,r.external_transcript_version,EXISTS(SELECT 1 FROM room_events e WHERE e.room_id=r.id AND e.kind IN ('message.user','message.assistant')) AS has_messages,COALESCE((SELECT MAX(e.created_at) FROM room_events e WHERE e.room_id=r.id AND json_extract(e.payload,'$.externalMessageId') IS NOT NULL),0) AS transcript_at FROM rooms r WHERE r.source='hermes' AND r.deleted_at IS NULL AND r.external_session_id<>'' AND r.external_profile NOT LIKE 'remote::%' ORDER BY COALESCE(r.external_updated_at,r.updated_at) DESC LIMIT 128",[])
    })?;
    let profiles: BTreeSet<_> = rows
        .iter()
        .map(|r| first(r, &["external_profile"], "default").to_owned())
        .collect();
    let mut active = HashSet::new();
    for p in profiles {
        let response = bridge
            .call("session.active_list", json!({"profile":p}), 10)
            .await;
        if response["ok"] == true {
            for s in array(&response["result"]["sessions"]) {
                let key = text(s, "session_key");
                if !key.is_empty() {
                    active.insert((p.clone(), key.to_owned()));
                }
            }
        }
    }
    rows.sort_by_key(|r| {
        !active.contains(&(
            first(r, &["external_profile"], "default").into(),
            text(r, "external_session_id").into(),
        ))
    });
    for row in rows.iter().take(limit.clamp(1, 32)) {
        let profile = first(row, &["external_profile"], "default");
        let sid = text(row, "external_session_id");
        let since = if row["has_messages"] == 1
            && row["external_transcript_version"].as_i64().unwrap_or(0) >= 2
        {
            row["transcript_at"].as_f64().unwrap_or(0.0)
        } else {
            0.0
        };
        let response = bridge
            .call(
                "session.messages",
                json!({"profile":profile,"session_id":sid,"limit":500,"since":since}),
                20,
            )
            .await;
        if response["ok"] != true || !response["result"]["messages"].is_array() {
            continue;
        }
        let result = &response["result"];
        with_store(store, |s| {
            s.sync_hermes_transcript(profile, sid, array(&result["messages"]), &result["session"])
        })?;
    }
    Ok(())
}
pub async fn runtime_json(helper: &Path, args: &[&str], seconds: u64) -> Result<Value> {
    use tokio::io::AsyncReadExt;
    let mut child = tokio::process::Command::new(helper)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()?;
    let mut stdout = child.stdout.take().ok_or("runtime stdout unavailable")?;
    let operation = async {
        let mut bytes = Vec::new();
        (&mut stdout)
            .take(40 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .await?;
        if bytes.len() > 40 * 1024 * 1024 {
            return Err("Runtime response too large".into());
        }
        if !child.wait().await?.success() {
            return Ok(json!({}));
        }
        let value: Value = serde_json::from_slice(&bytes)?;
        Ok(if value.is_object() { value } else { json!({}) })
    };
    match tokio::time::timeout(Duration::from_secs(seconds), operation).await {
        Ok(v) => v,
        Err(_) => {
            let _ = child.kill().await;
            Err("Runtime helper timed out".into())
        }
    }
}
pub async fn sync_runtime(store: &SharedStore, bridge: &HermesBroker, helper: &Path) -> Result<()> {
    let profiles = bridge
        .call("profiles.list", json!({"include_sessions":false}), 10)
        .await;
    let items = if profiles["result"]["profiles"].is_array() {
        array(&profiles["result"]["profiles"])
    } else {
        array(&profiles["result"]["items"])
    };
    if profiles["ok"] == true {
        let _ = with_store(store, |s| s.sync_profiles(items));
        let _ = crate::native_groups::discover_native_groups(store, bridge, "local", items).await;
    }
    let _ = sync_hermes_rooms(store, bridge, items).await;
    let _ = sync_recent_hermes_transcripts(store, bridge, 16).await;
    if let Ok(payload) = runtime_json(helper, &[], 25).await {
        let sessions: Vec<_> = array(&payload["sessions"])
            .iter()
            .filter(|r| text(r, "id").starts_with("remote::"))
            .cloned()
            .collect();
        let _ = sync_remote_hermes_rooms(store, &sessions);
        // Peer sync imports identity/metadata only. Historical chat fetches need
        // an explicit legacy gateway opt-in and are never part of pairing.
        if std::env::var("ARES_LEGACY_GATEWAY_IMPORT").as_deref() == Ok("1") {
            use futures_util::{stream, StreamExt};
            let chats = stream::iter(sessions.into_iter().take(16).map(|s| async move {
                runtime_json(helper, &["--session", text(&s, "id"), "--limit", "100"], 20).await
            }))
            .buffered(3);
            tokio::pin!(chats);
            while let Some(result) = chats.next().await {
                if let Ok(value) = result {
                    if let Some(chat) = array(&value["chats"]).first() {
                        let _ = sync_remote_hermes_chat(store, chat);
                    }
                }
            }
        }
    }
    // Legacy mesh enrollment is never automatic: discovery is read-only.
    let _ = runtime_json(helper, &["--discover"], 25).await;
    if let Ok(peers) = runtime_json(helper, &["--peers"], 20).await {
        let _ = with_store(store, |s| s.sync_peers(array(&peers["peers"])));
    }
    if let Ok(usage) = runtime_json(helper, &["--usage"], 30).await {
        let _ = with_store(store, |s| s.sync_quotas(&usage));
    }
    Ok(())
}
pub struct Runtime {
    pub bridge: HermesBroker,
    pub mirror: Arc<Mutex<HermesLiveTranscriptMirror>>,
    tasks: Vec<tokio::task::JoinHandle<()>>,
    child: Option<tokio::process::Child>,
}
impl Runtime {
    pub async fn start(store: SharedStore, log: &Path) -> Result<Self> {
        use std::os::unix::fs::OpenOptionsExt;
        let bridge = HermesBroker::start();
        let mut profiles = bridge
            .call("profiles.list", json!({"include_sessions":false}), 10)
            .await;
        let mut child = None;
        if profiles["ok"] != true {
            let output = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .mode(0o600)
                .open(log)?;
            child = tokio::process::Command::new("hermes")
                .args(["serve", "--port", "0", "--skip-build"])
                .stdin(Stdio::null())
                .stdout(output.try_clone()?)
                .stderr(output)
                .process_group(0)
                .kill_on_drop(true)
                .spawn()
                .ok();
            let deadline = tokio::time::Instant::now() + Duration::from_secs(12);
            if child.is_some() {
                while tokio::time::Instant::now() < deadline {
                    tokio::time::sleep(Duration::from_millis(250)).await;
                    profiles = bridge
                        .call("profiles.list", json!({"include_sessions":false}), 3)
                        .await;
                    if profiles["ok"] == true {
                        break;
                    }
                }
            }
        }
        if profiles["ok"] == true {
            let items = profiles["result"]
                .get("profiles")
                .unwrap_or(&profiles["result"]["items"]);
            let _ = with_store(&store, |s| s.sync_profiles(array(items)));
        }
        let mirror = Arc::new(Mutex::new(HermesLiveTranscriptMirror::default()));
        let mut tasks = Vec::new();
        let mut events = bridge.add_listener();
        let s = store.clone();
        let m = mirror.clone();
        tasks.push(tokio::spawn(async move {
            loop {
                match events.recv().await {
                    Ok(e) => {
                        let _ = with_store(&s, |s| {
                            m.lock().map_err(|_| "mirror lock poisoned")?.observe(s, &e)
                        });
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(_) => break,
                }
            }
        }));
        let s = store.clone();
        let b = bridge.clone();
        let helper = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../bin/ares-hermes-runtime");
        tasks.push(tokio::spawn(async move {
            loop {
                let _ = sync_runtime(&s, &b, &helper).await;
                tokio::time::sleep(Duration::from_secs(60)).await;
            }
        }));
        let s = store;
        let b = bridge.clone();
        let m = mirror.clone();
        tasks.push(tokio::spawn(async move{let mut persisted=tokio::time::Instant::now()-Duration::from_secs(2);loop{
            if let Ok(profiles)=with_store(&s,|s|s.rows("SELECT DISTINCT external_profile FROM rooms WHERE source='hermes' AND deleted_at IS NULL AND external_profile NOT LIKE 'remote::%'",[])){for row in profiles{let profile=first(&row,&["external_profile"],"default");let active=b.call("session.active_list",json!({"profile":profile}),10).await;let sessions=if active["ok"]==true{array(&active["result"]["sessions"])}else{&[]};if let Ok(mut m)=m.lock(){m.update_active_sessions(profile,sessions);}}}
            if persisted.elapsed()>=Duration::from_secs(2){let _=sync_recent_hermes_transcripts(&s,&b,8).await;persisted=tokio::time::Instant::now();}tokio::time::sleep(Duration::from_millis(500)).await;
        }}));
        Ok(Self {
            bridge,
            mirror,
            tasks,
            child,
        })
    }
    pub async fn shutdown(&mut self) {
        for t in &self.tasks {
            t.abort();
        }
        for t in self.tasks.drain(..) {
            let _ = t.await;
        }
        self.bridge.close();
        if let Some(child) = &mut self.child {
            if let Some(pid) = child.id() {
                unsafe {
                    libc::kill(pid as i32, libc::SIGTERM);
                }
            }
            if tokio::time::timeout(Duration::from_secs(5), child.wait())
                .await
                .is_err()
            {
                let _ = child.kill().await;
            }
        }
    }
}
impl Drop for Runtime {
    fn drop(&mut self) {
        for t in &self.tasks {
            t.abort();
        }
        self.bridge.close();
    }
}
