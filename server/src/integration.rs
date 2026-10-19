use crate::{
    broker::HermesBroker,
    digest, ident, now,
    producers::{array, content_text, first, sanitize},
    store::{truthy, Store},
    text, Result,
};
use rusqlite::params;
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
pub type SharedStore = Arc<Mutex<Store>>;
pub fn with_store<T>(store: &SharedStore, f: impl FnOnce(&Store) -> Result<T>) -> Result<T> {
    let guard = store.lock().map_err(|_| "store lock poisoned")?;
    f(&guard)
}

#[derive(Default)]
struct Stream {
    generation: String,
    segment: u64,
    text: String,
    thinking: bool,
    last_persist: Option<Instant>,
}
#[derive(Default)]
pub struct HermesLiveTranscriptMirror {
    routes: HashMap<String, (String, String)>,
    streams: HashMap<String, Stream>,
    cache: HashMap<String, (Value, Instant)>,
}
impl HermesLiveTranscriptMirror {
    pub fn update_active_sessions(&mut self, profile: &str, sessions: &[Value]) {
        self.routes.retain(|_, r| r.0 != profile);
        for s in sessions {
            let runtime = first(s, &["id", "session_id"], "").trim();
            let key = first(s, &["session_key", "stored_session_id"], "").trim();
            if !runtime.is_empty() && !key.is_empty() {
                self.routes
                    .insert(runtime.into(), (profile.into(), key.into()));
            }
        }
        self.streams.retain(|id, _| self.routes.contains_key(id));
    }
    fn event_id(runtime: &str, generation: &str, facet: &str) -> String {
        format!(
            "hermes_live_{}",
            digest(format!("{runtime}\0{generation}\0{facet}"))
        )
    }
    pub fn observe(&mut self, store: &Store, event: &Value) -> Result<()> {
        let runtime = text(event, "session_id");
        let kind = text(event, "type");
        if ![
            "message.start",
            "message.delta",
            "message.interim",
            "message.complete",
            "reasoning.delta",
            "thinking.delta",
        ]
        .contains(&kind)
        {
            return Ok(());
        }
        let Some((profile, key)) = self.routes.get(runtime) else {
            return Ok(());
        };
        let p = &event["payload"];
        if kind == "message.start" || !self.streams.contains_key(runtime) {
            let generation = first(
                p,
                &["message_id", "id"],
                &format!("{}", (now() * 1e9) as u128),
            )
            .to_owned();
            let keep = [
                Self::event_id(runtime, &format!("{generation}:0"), "message"),
                Self::event_id(runtime, &format!("{generation}:0"), "reasoning"),
            ];
            store.clear_live_hermes_events(profile, key, &keep)?;
            self.streams.insert(
                runtime.into(),
                Stream {
                    generation,
                    ..Default::default()
                },
            );
        }
        let stream = self.streams.get_mut(runtime).unwrap();
        let generation = format!("{}:{}", stream.generation, stream.segment);
        let message = Self::event_id(runtime, &generation, "message");
        let reasoning = Self::event_id(runtime, &generation, "reasoning");
        let mut remember = |id: &str, kind: &str, payload: Value, complete: bool| -> Result<()> {
            if let Some(e) = store.upsert_live_hermes_event(profile, key, id, kind, payload)? {
                self.cache.insert(
                    id.into(),
                    (
                        e,
                        Instant::now() + Duration::from_secs(if complete { 5 } else { 30 }),
                    ),
                );
            }
            Ok(())
        };
        if ["reasoning.delta", "thinking.delta"].contains(&kind) {
            stream.thinking = true;
            return remember(
                &reasoning,
                "reasoning.status",
                json!({"text":"Thinking…","streaming":true,"live":true,"externalSessionId":key,"sourceOrder":0}),
                false,
            );
        }
        let delta = first(p, &["text", "delta"], "");
        if kind == "message.delta" {
            stream.text.push_str(delta);
        } else if !delta.is_empty() {
            stream.text = delta.into();
        }
        if stream.thinking {
            remember(
                &reasoning,
                "reasoning.status",
                json!({"text":"Thought","streaming":false,"live":false,"externalSessionId":key,"sourceOrder":0}),
                true,
            )?;
            stream.thinking = false;
        }
        if !stream.text.is_empty() {
            let complete = ["message.interim", "message.complete"].contains(&kind);
            // message.delta arrives per token; persisting every delta rewrites
            // the whole accumulated row (quadratic I/O). Persist at most every
            // 200ms per stream once the text is large; interim/complete always flush.
            let throttled = kind == "message.delta"
                && stream.text.len() > 4096
                && stream
                    .last_persist
                    .is_some_and(|t| t.elapsed() < Duration::from_millis(200));
            if !throttled {
                remember(
                    &message,
                    "message.assistant",
                    json!({"text":stream.text,"streaming":!complete,"live":!complete,"externalSessionId":key,"sourceOrder":1}),
                    complete,
                )?;
                stream.last_persist = Some(Instant::now());
            }
        }
        if kind == "message.interim" {
            stream.segment += 1;
            stream.text.clear();
            stream.thinking = false;
        } else if kind == "message.complete" {
            stream.text.clear();
        }
        Ok(())
    }
    pub fn live_events(&mut self, store: &Store, room: &str) -> Result<Vec<Value>> {
        self.cache
            .retain(|_, (_, expires)| *expires > Instant::now());
        // The DB is authoritative after transcript reconciliation; retain its order.
        Ok(store
            .live_events(room)?
            .into_iter()
            .filter_map(|e| self.cache.get(text(&e, "id")).map(|c| c.0.clone()))
            .collect())
    }
}

pub struct InterventionManager;
impl InterventionManager {
    pub async fn approve(
        store: &SharedStore,
        bridge: &HermesBroker,
        id: &str,
        request: &str,
        choice: &str,
    ) -> Result<Value> {
        if !bridge.available() {
            return Err("Hermes execution bridge is unavailable".into());
        }
        let mut e = with_store(store, |s| s.execution(id))?;
        let request = crate::require(request, "approval request ID", 256)?;
        let choice = crate::require(choice, "approval choice", 32)?;
        if !["once", "always", "deny"].contains(&choice.as_str()) {
            return Err("approval choice is invalid".into());
        }
        let binding = with_store(store, |s| {
            s.one("SELECT * FROM session_bindings WHERE room_id=? AND bot_id=? ORDER BY updated_at DESC LIMIT 1",params![text(&e,"roomId"),text(&e,"botId")],"execution has no live Hermes session")
        })?;
        let runtime = text(&binding, "runtime_session_id");
        if runtime.is_empty() {
            return Err("execution has no live Hermes session".into());
        }
        let response = bridge
            .call(
                "approval.respond",
                json!({"session_id":runtime,"request_id":request,"choice":choice,"all":false}),
                30,
            )
            .await;
        if response["ok"] != true || !truthy(&response["result"]["resolved"]) {
            return Err(first(&response, &["error"], "approval was not resolved").into());
        }
        e["executionId"] = json!(id);
        with_store(store, |s| {
            s.event(&e,None,"approval.responded","user",json!({"text":format!("Approval response: {choice}"),"requestId":request,"choice":choice}),false)
        })?;
        Ok(json!({"resolved":true,"requestId":request,"choice":choice}))
    }
}
pub struct LifecycleManager;
impl LifecycleManager {
    pub async fn pause_room(
        store: &SharedStore,
        bridge: &HermesBroker,
        project: &str,
        room: &str,
        key: &str,
    ) -> Result<Value> {
        let key = if key.is_empty() {
            ident("pause")
        } else {
            key.into()
        };
        let requested = with_store(store, |s| s.lifecycle(project, room, &key, "pause"))?;
        if !bridge.available() {
            return Ok(requested);
        }
        let mut outcomes = Vec::new();
        for session in array(&requested["sessions"]) {
            if session["releaseState"] == "released" {
                outcomes.push(session.clone());
                continue;
            }
            let runtime = text(session, "runtimeSessionId");
            let node = first(session, &["nodeId"], "local");
            let (state, detail) = if node != "local" {
                (
                    "pending_remote",
                    "Remote session release awaits an authenticated peer adapter".to_owned(),
                )
            } else if runtime.is_empty() {
                ("released", "No live runtime session was allocated".into())
            } else {
                let _ = bridge
                    .call("session.interrupt", json!({"session_id":runtime}), 10)
                    .await;
                let response = bridge
                    .call("session.close", json!({"session_id":runtime}), 20)
                    .await;
                if response["ok"] == true && truthy(&response["result"]["closed"]) {
                    ("released", "Hermes confirmed session teardown".into())
                } else {
                    let active = bridge.call("session.active_list", json!({}), 10).await;
                    let present = array(&active["result"]["sessions"]).iter().any(|s| {
                        ["session_id", "runtime_session_id", "id"]
                            .iter()
                            .any(|k| text(s, k) == runtime)
                    });
                    if active["ok"] == true && !present {
                        (
                            "released",
                            "Hermes confirmed the runtime session was already absent".into(),
                        )
                    } else {
                        (
                            "failed",
                            first(&response, &["error"], "Hermes did not confirm teardown").into(),
                        )
                    }
                }
            };
            outcomes.push(json!({"botId":session["botId"],"nodeId":node,"releaseState":state,"detail":detail}));
        }
        with_store(store, |s| s.record_pause_outcomes(project, room, &outcomes))
    }
}

#[derive(Default)]
struct TurnEvents {
    text: String,
    reasoning: String,
    count: u64,
    media: HashSet<PathBuf>,
    last_persist: Option<Instant>,
}
impl TurnEvents {
    fn observe(&mut self, store: &Store, e: &Value, project: &Value, event: &Value) -> Result<()> {
        let kind = text(event, "type");
        if ![
            "message.delta",
            "reasoning.delta",
            "thinking.delta",
            "tool.start",
            "tool.generating",
            "tool.progress",
            "tool.complete",
            "tool.output_risk",
            "status",
            "approval.request",
            "clarify.request",
            "error",
        ]
        .contains(&kind)
        {
            return Ok(());
        }
        self.count += 1;
        let mut p = sanitize(if event["payload"].is_object() {
            event["payload"].clone()
        } else {
            json!({})
        });
        let id = text(e, "id");
        let bot = text(e, "botId");
        if kind == "message.delta" {
            self.text.push_str(first(&p, &["text", "delta"], ""));
            // Persist at most every 200ms; each write rewrites the whole
            // accumulated row, so per-token writes are quadratic I/O once the
            // text grows past a few KB.
            let throttled = self
                .last_persist
                .is_some_and(|t| t.elapsed() < Duration::from_millis(200));
            if !self.text.is_empty() && !(self.text.len() > 4096 && throttled) {
                store.event(
                    e,
                    Some(&format!("gw_stream_{id}_message")),
                    "message.assistant",
                    bot,
                    json!({"text":self.text,"streaming":true}),
                    true,
                )?;
                self.last_persist = Some(Instant::now());
            }
            return Ok(());
        }
        if ["reasoning.delta", "thinking.delta"].contains(&kind) {
            self.reasoning.push_str(first(&p, &["text", "delta"], ""));
            let throttled = self
                .last_persist
                .is_some_and(|t| t.elapsed() < Duration::from_millis(200));
            if !self.reasoning.trim().is_empty() && !(self.reasoning.len() > 4096 && throttled) {
                store.event(
                    e,
                    Some(&format!("gw_stream_{id}_reasoning")),
                    "reasoning.status",
                    bot,
                    json!({"text":self.reasoning,"streaming":true}),
                    true,
                )?;
                self.last_persist = Some(Instant::now());
            }
            return Ok(());
        }
        if ["tool.generating", "tool.progress"].contains(&kind) {
            let tool = first(&p, &["toolName", "name", "tool"], "tool").to_owned();
            p["streaming"] = json!(kind == "tool.progress");
            store.event(e, Some(&format!("gw_tool_{id}_{tool}")), kind, bot, p, true)?;
            return Ok(());
        }
        let stable = format!("gw_{}", digest(format!("{id}:{}:{kind}:{}", self.count, p)));
        let event = store.event(e, Some(&stable), kind, bot, p.clone(), false)?;
        if kind == "tool.complete" {
            for path in discover_media_paths(&p, text(project, "path")) {
                if !self.media.insert(path.clone()) {
                    continue;
                }
                let mime = mime_guess::from_path(&path)
                    .first_or_octet_stream()
                    .to_string();
                store.store_artifact(&json!({"projectId":e["projectId"],"roomId":e["roomId"],"name":path.file_name().unwrap().to_string_lossy(),"mime":mime,"author":bot,"eventId":event["id"]}),&std::fs::read(path)?)?;
            }
        }
        Ok(())
    }
}
fn discover_media_paths(payload: &Value, root: &str) -> Vec<PathBuf> {
    static RE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(
            r"(?i)(/[A-Za-z0-9_./+@%=-]+\.(?:png|jpe?g|gif|webp|bmp|mp4|webm|mov|mkv))\b",
        )
        .unwrap()
    });
    let Ok(root) = std::fs::canonicalize(root) else {
        return vec![];
    };
    let mut paths = Vec::new();
    fn walk(v: &Value, out: &mut Vec<String>) {
        match v {
            Value::String(s) => out.push(s.clone()),
            Value::Array(a) => {
                for v in a {
                    walk(v, out)
                }
            }
            Value::Object(m) => {
                for v in m.values() {
                    walk(v, out)
                }
            }
            _ => (),
        }
    }
    let mut strings = Vec::new();
    walk(payload, &mut strings);
    for s in strings {
        for cap in RE.captures_iter(&s) {
            if let Ok(path) = std::fs::canonicalize(&cap[1]) {
                if path.starts_with(&root)
                    && path.is_file()
                    && path
                        .metadata()
                        .is_ok_and(|m| m.len() <= crate::MAX_ARTIFACT_BYTES as u64)
                    && !paths.contains(&path)
                {
                    paths.push(path);
                }
            }
        }
    }
    paths
}
pub struct ExecutionRunner {
    pub store: SharedStore,
    pub bridge: HermesBroker,
    pub poll_interval: Duration,
    pub deadline: Duration,
}
impl ExecutionRunner {
    pub fn new(store: SharedStore, bridge: HermesBroker) -> Self {
        Self {
            store,
            bridge,
            poll_interval: Duration::from_millis(500),
            deadline: Duration::from_secs(1200),
        }
    }
    pub async fn run_execution(&self, id: &str) -> Result<Value> {
        if !with_store(&self.store, |s| s.start_execution(id))? {
            return with_store(&self.store, |s| s.execution(id));
        }
        let mut e = with_store(&self.store, |s| s.execution(id))?;
        e["executionId"] = json!(id);
        let mut events = self.bridge.add_listener();
        let mut turn = TurnEvents::default();
        let result = self.run_turn(&e, &mut events, &mut turn).await;
        let finish = with_store(&self.store, |s| match result {
            Ok(final_text) => s.finish_execution(id, "completed", &final_text, ""),
            Err(error) => s.finish_execution(id, "failed", "", &error.to_string()),
        });
        with_store(&self.store, |s| {
            s.clear_ephemeral_execution_events(text(&e, "roomId"), id)
        })?;
        finish
    }
    async fn call_observed(
        &self,
        method: &str,
        p: Value,
        e: &Value,
        project: &Value,
        runtime: &str,
        observation: (
            &mut tokio::sync::broadcast::Receiver<Value>,
            &mut TurnEvents,
        ),
    ) -> Result<Value> {
        let (events, turn) = observation;
        let call = self.bridge.call(method, p, 60);
        tokio::pin!(call);
        loop {
            tokio::select! {biased; event=events.recv()=>match event{Ok(event)=>if text(&event,"session_id")==runtime{with_store(&self.store,|s|turn.observe(s,e,project,&event))?;},Err(tokio::sync::broadcast::error::RecvError::Lagged(_))=>return Err("Hermes event stream overflow; outcome unknown".into()),Err(_)=>return Err("Hermes Desktop connection closed".into())},r=&mut call=>return Ok(r)}
        }
    }
    async fn run_turn(
        &self,
        e: &Value,
        events: &mut tokio::sync::broadcast::Receiver<Value>,
        turn: &mut TurnEvents,
    ) -> Result<String> {
        let project = with_store(&self.store, |s| {
            s.one(
                "SELECT * FROM projects WHERE id=?",
                [text(e, "projectId")],
                "project does not exist",
            )
        })?;
        let bot = with_store(&self.store, |s| {
            s.one(
                "SELECT * FROM bots WHERE id=?",
                [text(e, "botId")],
                "Bot is not configured",
            )
        })?;
        let profile = text(&bot, "profile");
        let title = format!("Ares: {} · {}", text(e, "roomId"), text(e, "botId"));
        let bindings = with_store(&self.store, |s| {
            s.rows("SELECT stored_session_id FROM session_bindings WHERE room_id=? AND bot_id=? AND node_id=?",params![text(e,"roomId"),text(e,"botId"),text(&bot,"node_id")])
        })?;
        let resume = bindings
            .first()
            .map(|b| text(b, "stored_session_id"))
            .unwrap_or(&title);
        let mut response = self
            .bridge
            .call(
                "session.resume",
                json!({"session_id":resume,"profile":profile}),
                60,
            )
            .await;
        if response["ok"] != true && response["code"] == 4007 && bindings.is_empty() {
            response=self.bridge.call("session.create",json!({"profile":profile,"title":title,"source":"ares","hidden":true,"cwd":project["path"],"room_plumbing":true,"follow_profile_config":true,"close_on_disconnect":false}),60).await;
        }
        if response["ok"] != true {
            return Err(first(&response, &["error"], "Hermes session unavailable").into());
        }
        let state = &response["result"];
        let runtime = text(state, "session_id");
        if runtime.is_empty() {
            return Err("Hermes runtime session unavailable".into());
        }
        let stored = first(state, &["session_key", "stored_session_id"], runtime);
        let before = array(&state["messages"]).len();
        with_store(&self.store, |s| {
            s.bind_session(
                text(e, "roomId"),
                text(e, "botId"),
                text(&bot, "node_id"),
                stored,
                runtime,
            )
        })?;
        let artifacts = with_store(&self.store, |s| s.execution_artifacts(text(e, "id")))?;
        let mut refs = Vec::new();
        for a in artifacts {
            let image = text(&a, "mime").starts_with("image/");
            let method = if image {
                "image.attach_bytes"
            } else {
                "file.attach"
            };
            let params = if image {
                use base64::Engine;
                let body = tokio::fs::read(text(&a, "path")).await?;
                json!({"session_id":runtime,"content_base64":base64::engine::general_purpose::STANDARD.encode(body),"filename":a["name"]})
            } else {
                json!({"session_id":runtime,"path":a["path"],"name":a["name"]})
            };
            let attached = self
                .call_observed(method, params, e, &project, runtime, (events, turn))
                .await?;
            let result = &attached["result"];
            let accepted = attached["ok"] == true && truthy(&result["attached"]);
            let detail = if accepted {
                let reference = first(result, &["ref_text", "text"], "").trim();
                if !reference.is_empty() {
                    refs.push(reference.to_owned());
                }
                format!("Hermes accepted {} via {method}", text(&a, "name"))
            } else {
                first(
                    &attached,
                    &["error"],
                    first(
                        result,
                        &["message"],
                        &format!("Hermes rejected {}", text(&a, "name")),
                    ),
                )
                .into()
            };
            with_store(&self.store, |s| {
                s.set_artifact_delivery(
                    text(e, "id"),
                    text(&a, "artifactId"),
                    if accepted { "delivered" } else { "failed" },
                    &detail,
                )
            })?;
        }
        let references = if refs.is_empty() {
            String::new()
        } else {
            format!("\nAttachments:\n{}", refs.join("\n"))
        };
        let mut group = String::new();
        if !text(&project, "group_id").is_empty() {
            let rows = with_store(&self.store, |s| {
                s.rows(
                    "SELECT instructions FROM project_groups WHERE id=?",
                    [text(&project, "group_id")],
                )
            })?;
            if let Some(r) = rows.first() {
                let instructions = text(r, "instructions").trim();
                if !instructions.is_empty() {
                    group = format!("\nShared project-group instructions:\n{instructions}\n");
                }
            }
        }
        let path = text(&project, "path");
        let cwd = if path.starts_with("hermes://") {
            String::new()
        } else {
            format!("Working project directory: {path}\n")
        };
        let prompt = format!("{cwd}{group}{references}\n\n{}", text(e, "prompt"));
        let submitted = self
            .call_observed(
                "prompt.submit",
                json!({"session_id":runtime,"text":prompt}),
                e,
                &project,
                runtime,
                (events, turn),
            )
            .await?;
        if submitted["ok"] != true {
            return Err(first(&submitted, &["error"], "Hermes prompt rejected").into());
        }
        let deadline = tokio::time::Instant::now() + self.deadline;
        loop {
            if tokio::time::Instant::now() >= deadline {
                return Err("Hermes execution timed out".into());
            }
            let sleep = tokio::time::sleep(self.poll_interval);
            tokio::pin!(sleep);
            loop {
                tokio::select! {_=&mut sleep=>break,event=events.recv()=>match event{Ok(event)=>if text(&event,"session_id")==runtime{with_store(&self.store,|s|turn.observe(s,e,&project,&event))?;},Err(_)=>break}}
            }
            let response = self
                .call_observed(
                    "session.resume",
                    json!({"session_id":stored,"profile":profile}),
                    e,
                    &project,
                    runtime,
                    (events, turn),
                )
                .await?;
            if response["ok"] != true {
                return Err(first(&response, &["error"], "Hermes session lost").into());
            }
            let state = &response["result"];
            let inflight = &state["inflight"];
            let status = text(inflight, "status").to_lowercase();
            if ["error", "failed", "cancelled", "interrupted"].contains(&status.as_str()) {
                return Err(
                    first(inflight, &["error", "message"], "Hermes provider failed").into(),
                );
            }
            if truthy(&state["running"])
                || inflight == true
                || ["queued", "running", "working", "streaming"].contains(&status.as_str())
                || truthy(&state["pending_approval"])
                || truthy(&state["pending_clarify"])
            {
                continue;
            }
            let messages = array(&state["messages"]);
            let final_text = messages
                .iter()
                .skip(before)
                .rev()
                .find(|m| m["role"] == "assistant")
                .map(|m| {
                    content_text(m.get("content").unwrap_or(&m["text"]), "", false)
                        .trim()
                        .to_owned()
                })
                .unwrap_or_default();
            if messages.len() <= before && turn.text.is_empty() {
                continue;
            }
            if !final_text.is_empty() {
                with_store(&self.store, |s| {
                    s.event(
                        e,
                        None,
                        "message.assistant",
                        text(e, "botId"),
                        json!({"text":final_text,"reconcilesDeltas":true}),
                        false,
                    )
                })?;
                return Ok(final_text);
            }
            return Ok(turn.text.clone());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn deltas_coalesce_and_reconcile() {
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(
            &tmp.path().join("db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap();
        store.sync_hermes_project("default",&json!({"path":"hermes://home","label":"Home","repos":[{"groups":[{"sessions":[{"id":"s","title":"Chat"}]}]}]}),"local",true).unwrap();
        let room = store
            .rows("SELECT id FROM rooms WHERE external_session_id='s'", [])
            .unwrap()[0]["id"]
            .as_str()
            .unwrap()
            .to_owned();
        let mut mirror = HermesLiveTranscriptMirror::default();
        mirror.update_active_sessions("default", &[json!({"id":"r","session_key":"s"})]);
        for delta in ["Hello ", "world"] {
            mirror
                .observe(
                    &store,
                    &json!({"session_id":"r","type":"message.delta","payload":{"text":delta}}),
                )
                .unwrap();
        }
        let events = mirror.live_events(&store, &room).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["text"], "Hello world");
        let cursor = events[0]["cursor"].clone();
        mirror.observe(&store,&json!({"session_id":"r","type":"message.complete","payload":{"text":"Hello world!"}})).unwrap();
        assert_eq!(
            mirror.live_events(&store, &room).unwrap()[0]["cursor"],
            cursor
        );
        store.sync_hermes_transcript("default","s",&[json!({"id":"m","role":"assistant","content":"Hello world!","timestamp":now()+1.0})],&json!({})).unwrap();
        assert!(mirror.live_events(&store, &room).unwrap().is_empty());
    }
}
