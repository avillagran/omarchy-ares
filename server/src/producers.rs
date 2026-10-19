//! Python-compatible durable runtime producers. Call with the Store lock held.
use crate::{
    digest, ident, now,
    store::{truthy, Store},
    text, truncate, Result,
};
use rusqlite::params;
use serde_json::{json, Value};
use std::{collections::HashMap, sync::LazyLock};
pub fn array(v: &Value) -> &[Value] {
    v.as_array().map(Vec::as_slice).unwrap_or(&[])
}
pub fn number(v: &Value) -> Option<f64> {
    v.as_f64()
        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        .filter(|n| n.is_finite())
}
fn scalar_string(v: &Value) -> String {
    match v {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        v => v.to_string(),
    }
}
pub fn first<'a>(v: &'a Value, keys: &[&str], default: &'a str) -> &'a str {
    keys.iter()
        .find_map(|k| v[*k].as_str().filter(|s| !s.is_empty()))
        .unwrap_or(default)
}
pub fn sanitize(v: Value) -> Value {
    static KEY: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new("(?i)(password|secret|token|api.?key|authorization|cookie)").unwrap()
    });
    static AUTH: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"(?i)\b(?:Bearer|Basic)\s+\S+").unwrap());
    static SECRET: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(r"\b(?:sk-|gh[pousr]_|github_pat_|AKIA|AIza|xox[baprs]-)[A-Za-z0-9_-]+")
            .unwrap()
    });
    match v {
        Value::Object(m) => Value::Object(
            m.into_iter()
                .map(|(k, v)| {
                    let v = if KEY.is_match(&k) {
                        json!("[redacted]")
                    } else {
                        sanitize(v)
                    };
                    (k, v)
                })
                .collect(),
        ),
        Value::Array(a) => json!(a.into_iter().map(sanitize).collect::<Vec<_>>()),
        Value::String(s) => {
            let s = AUTH.replace_all(&s, "[redacted]");
            let s = SECRET.replace_all(&s, "[redacted]");
            json!(s
                .chars()
                .skip(s.chars().count().saturating_sub(100000))
                .collect::<String>())
        }
        v => v,
    }
}
impl Store {
    pub fn execution(&self, id: &str) -> Result<Value> {
        self.dto(
            "executions",
            &self.one(
                "SELECT * FROM executions WHERE id=?",
                [id],
                "execution does not exist",
            )?,
        )
    }
    /// Atomic claim prevents an idempotency replay from submitting a second turn.
    pub fn start_execution(&self, id: &str) -> Result<bool> {
        self.transaction(|s|{let e=s.execution(id)?;let changed=s.db.execute("UPDATE executions SET status='running',started_at=? WHERE id=? AND status='queued' AND NOT EXISTS (SELECT 1 FROM rooms r WHERE r.id=executions.room_id AND r.source IN ('hermes-native-group','hermes-native-member')) AND NOT EXISTS (SELECT 1 FROM terminal_bindings t WHERE t.room_id=executions.room_id)",params![now(),id])?;if changed==1{s.append_event(text(&e,"projectId"),text(&e,"roomId"),Some(id),"execution.started",text(&e,"botId"),json!({"text":"Started"}))?;}Ok(changed==1)})
    }
    pub fn finish_execution(
        &self,
        id: &str,
        status: &str,
        final_text: &str,
        error: &str,
    ) -> Result<Value> {
        if !["completed", "failed", "cancelled", "interrupted"].contains(&status) {
            return Err("invalid execution status".into());
        }
        self.transaction(|s|{let e=s.execution(id)?;s.db.execute("UPDATE executions SET status=?,final_text=?,error=?,finished_at=? WHERE id=?",params![status,final_text,error,now(),id])?;s.append_event(text(&e,"projectId"),text(&e,"roomId"),Some(id),&format!("execution.{status}"),text(&e,"botId"),json!({"text":if !final_text.is_empty(){final_text}else if !error.is_empty(){error}else{status}}))?;s.execution(id)})
    }
    pub fn bind_session(
        &self,
        room: &str,
        bot: &str,
        node: &str,
        stored: &str,
        runtime: &str,
    ) -> Result<()> {
        self.room(room, true)?;
        self.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,runtime_session_id,lifecycle_state,updated_at) VALUES(?,?,?,?,?,'resident',?) ON CONFLICT(room_id,bot_id,node_id) DO UPDATE SET stored_session_id=excluded.stored_session_id,runtime_session_id=excluded.runtime_session_id,lifecycle_state='resident',updated_at=excluded.updated_at",params![room,bot,node,stored,runtime,now()])?;
        Ok(())
    }
    pub fn execution_artifacts(&self, id: &str) -> Result<Vec<Value>> {
        self.rows("SELECT x.artifact_id AS artifactId,a.name,a.mime,a.path FROM execution_artifacts x JOIN artifacts a ON a.id=x.artifact_id WHERE x.execution_id=? ORDER BY a.created_at",[id])
    }
    pub fn set_artifact_delivery(
        &self,
        id: &str,
        artifact: &str,
        state: &str,
        detail: &str,
    ) -> Result<()> {
        if !["delivered", "failed", "unsupported"].contains(&state) {
            return Err("artifact delivery state is invalid".into());
        }
        self.transaction(|s|{let e=s.execution(id)?;let detail=truncate(detail,1000);if s.db.execute("UPDATE execution_artifacts SET delivery_state=?,detail=?,updated_at=? WHERE execution_id=? AND artifact_id=?",params![state,detail,now(),id,artifact])?!=1{return Err("execution artifact does not exist".into());}s.append_event(text(&e,"projectId"),text(&e,"roomId"),Some(id),&format!("artifact.delivery.{}",if state=="delivered"{"accepted"}else{state}),"ares",json!({"text":detail,"artifactId":artifact,"deliveryState":state}))})
    }
    pub fn event(
        &self,
        e: &Value,
        id: Option<&str>,
        kind: &str,
        author: &str,
        payload: Value,
        ephemeral: bool,
    ) -> Result<Value> {
        self.transaction(|s|{let stable=id.map(str::to_owned).unwrap_or_else(||ident("evt"));let room=first(e,&["roomId","room_id"],"");let project=first(e,&["projectId","project_id"],"");let owned=s.room(room,true)?;
            if text(&owned,"project_id")!=project {return Err("room does not belong to the project".into());}
            let mut payload=payload;if ephemeral{payload["ephemeral"]=json!(true);payload=sanitize(payload);}
            let existing=s.rows("SELECT * FROM room_events WHERE id=?",[&stable])?;
            if existing.is_empty(){s.db.execute("INSERT INTO room_events(id,project_id,room_id,execution_id,sequence,kind,author,payload,created_at) VALUES(?,?,?,?,(SELECT COALESCE(MAX(sequence),0)+1 FROM room_events WHERE room_id=?),?,?,?,?)",params![stable,project,room,e["executionId"].as_str(),room,kind,author,payload.to_string(),now()])?;}else if ephemeral{s.db.execute("UPDATE room_events SET kind=?,author=?,payload=? WHERE id=? AND room_id=?",params![kind,author,payload.to_string(),stable,room])?;}else{return s.dto("room_events",&existing[0]);}
            s.db.execute("UPDATE rooms SET updated_at=?,revision=revision+1 WHERE id=?",params![now(),room])?;
            let row=s.one("SELECT * FROM room_events WHERE id=?",[stable],"event missing")?;
            if existing.is_empty() {
                // Python's insertion DTO is deliberately raw; subsequent upserts
                // use the rendered historical DTO (including assistant HTML).
                Ok(json!({"cursor":row["cursor"],"id":row["id"],"projectId":project,"roomId":room,"executionId":e["executionId"],"sequence":row["sequence"],"kind":kind,"author":author,"payload":payload,"text":first(&payload,&["text","delta"],""),"createdAt":row["created_at"]}))
            } else {s.dto("room_events",&row)}
        })
    }
    pub fn upsert_live_hermes_event(
        &self,
        profile: &str,
        session: &str,
        id: &str,
        kind: &str,
        payload: Value,
    ) -> Result<Option<Value>> {
        let rooms=self.rows("SELECT id AS roomId,project_id AS projectId FROM rooms WHERE source='hermes' AND external_profile=? AND external_session_id=? AND deleted_at IS NULL",params![profile,session])?;
        rooms
            .first()
            .map(|r| self.event(r, Some(id), kind, "assistant", payload, true))
            .transpose()
    }
    pub fn clear_live_hermes_events(
        &self,
        profile: &str,
        session: &str,
        keep: &[String],
    ) -> Result<usize> {
        // Ephemeral deltas are transient; bound the scan to the recent cursor
        // window per room so pruning never re-reads a full (possibly huge) room.
        Ok(self.db.execute("DELETE FROM room_events WHERE room_id IN (SELECT id FROM rooms WHERE source='hermes' AND external_profile=? AND external_session_id=? AND deleted_at IS NULL) AND json_extract(payload,'$.ephemeral')=1 AND id NOT IN (SELECT value FROM json_each(?)) AND cursor>(SELECT COALESCE(MAX(cursor),0)-400 FROM room_events)",params![profile,session,json!(keep).to_string()])?)
    }
    pub fn clear_ephemeral_execution_events(&self, room: &str, id: &str) -> Result<usize> {
        Ok(self.db.execute("DELETE FROM room_events WHERE room_id=? AND execution_id=? AND json_extract(payload,'$.ephemeral')=1 AND cursor>(SELECT COALESCE(MAX(cursor),0)-400 FROM room_events WHERE room_id=?)",params![room,id,room])?)
    }
    pub fn record_pause_outcomes(
        &self,
        project: &str,
        room: &str,
        outcomes: &[Value],
    ) -> Result<Value> {
        self.transaction(|s|{let r=s.room(room,false)?;if text(&r,"project_id")!=project{return Err("room does not belong to the project".into());}let mut released=0;for o in outcomes{let state=text(o,"releaseState");let bot=text(o,"botId");let node=text(o,"nodeId");released+=usize::from(state=="released");s.db.execute("UPDATE session_bindings SET lifecycle_state=?,updated_at=? WHERE room_id=? AND bot_id=? AND node_id=?",params![state,now(),room,bot,node])?;s.append_event(project,room,None,if state=="released"{"session.release.confirmed"}else{"session.release.pending"},"ares",json!({"text":format!("{bot}@{node}: {state}"),"botId":bot,"nodeId":node,"releaseState":state,"detail":truncate(text(o,"detail"),500)}))?;}
        let memory=if released>0 && released==outcomes.len(){"confirmed"}else if released>0{"partial"}else{"unconfirmed"};s.db.execute("UPDATE rooms SET memory_release=?,revision=revision+1,updated_at=? WHERE id=?",params![memory,now(),room])?;s.append_event(project,room,None,"room.pause.completed","ares",json!({"text":format!("Pause completed: {memory}"),"memoryRelease":memory}))?;Ok(json!({"roomId":room,"status":"paused","memoryRelease":memory,"sessions":s.dtos("session_bindings","SELECT * FROM session_bindings WHERE room_id=? ORDER BY bot_id,node_id",[room])?}))})
    }
    pub fn sync_profiles(&self, profiles: &[Value]) -> Result<usize> {
        self.transaction(|s|{let mut count=0;for item in profiles{let profile=first(item,&["name","profile"],"").trim();if profile.is_empty() || profile.len()>128 || profile.contains('/') || profile==".." {continue;}
        let path=s.hermes_home.join("profiles").join(profile).join("ares.json");let mut side=crate::sidecar::json_read(&path);
        let display=truncate(first(item,&["display_name","title"],profile).trim(),160);let old=s.rows("SELECT * FROM bots WHERE id=?",[profile])?;
        if side.as_object().is_some_and(|m|m.is_empty()){if let Some(old)=old.first(){if text(old,"name")!=display || old["description"]!=item["description"].as_str().unwrap_or("") || old["model"]!=item["model"].as_str().unwrap_or(""){for k in ["name","description","model","provider","face_shape","color_role"]{side[k]=old[k].clone();}crate::sidecar::json_write(&path,&side);}}}
        let name=truncate(first(&side,&["name"],&display),160);let description=truncate(first(&side,&["description"],text(item,"description")),500);let model=truncate(first(&side,&["model"],text(item,"model")),160);let provider=truncate(first(&side,&["provider"],text(item,"provider")),160);let shape=["diamond","circle","square","hexagon"][u8::from_str_radix(&digest(profile)[..2],16).unwrap() as usize%4];
        s.db.execute("INSERT INTO bots(id,name,description,profile,node_id,model,provider,face_shape,color_role,enabled) VALUES(?,?,?,?,'local',?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,profile=excluded.profile,model=excluded.model,provider=excluded.provider,enabled=1",params![profile,name,description,profile,model,provider,first(&side,&["face_shape"],shape),first(&side,&["color_role"],"accent")])?;if !model.trim().is_empty(){s.db.execute("INSERT OR IGNORE INTO known_models(provider,model) VALUES(?,?)",params![provider.trim(),model.trim()])?;}count+=1;}Ok(count)})
    }
    pub fn sync_peers(&self, peers: &[Value]) -> Result<usize> {
        let path = self.hermes_home.join("ares-peers.json");
        let mut side = crate::sidecar::json_read(&path);
        self.transaction(|s|{for item in peers{let id=first(item,&["name","id"],"").trim();if id.is_empty() || id.len()>128 {continue;}let source=truncate(first(item,&["title"],id).trim(),160);if side.get(id).is_none(){if let Some(old)=s.rows("SELECT name,color_role FROM peers WHERE id=?",[id])?.first(){if text(old,"name")!=source || text(old,"color_role")!="accent"{side[id]=old.clone();}}}let status=truncate(first(item,&["status"],"unknown"),64);let cap=json!({"authenticated":truthy(&item["authenticated"]),"lanAccess":truthy(&item["lanAccess"]),"version":text(item,"version"),"agents":array(&item["agents"])});let t=now();s.db.execute("INSERT INTO peers(id,name,endpoint,status,capabilities,last_seen_at,updated_at,source_name,color_role) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_name=excluded.source_name,endpoint=excluded.endpoint,status=excluded.status,capabilities=excluded.capabilities,last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at",params![id,truncate(first(&side[id],&["name"],&source),160),truncate(text(item,"url"),500),status,cap.to_string(),if ["ok","degraded"].contains(&status.as_str()){Some(t)}else{None},t,source,first(&side[id],&["color_role"],"accent")])?;}Ok(())})?;
        if side.as_object().is_some_and(|m| !m.is_empty()) {
            crate::sidecar::json_write(&path, &side);
        }
        Ok(peers.len())
    }
    pub fn sync_quotas(&self, usage: &Value) -> Result<usize> {
        self.transaction(|s| {
            let updated=number(&usage["updatedAt"]).unwrap_or_else(now);
            s.db.execute("DELETE FROM quota_windows",[])?;
            s.db.execute("DELETE FROM quota_providers",[])?;
            let mut count=0;
            for p in array(&usage["providers"]) {
                let id=truncate(text(p,"provider"),128);
                if id.is_empty(){continue;}
                let q=&p["quota"];
                let status=truncate(first(q,&["status"],"unknown"),64);
                s.db.execute("INSERT INTO quota_providers(provider,hour_tokens,week_tokens,hour_calls,week_calls,plan,details,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",params![id,p["hourTokens"].as_i64().unwrap_or(0),p["weekTokens"].as_i64().unwrap_or(0),p["hourCalls"].as_i64().unwrap_or(0),p["weekCalls"].as_i64().unwrap_or(0),truncate(text(q,"plan"),160),json!(array(&q["details"])).to_string(),status,updated])?;
                for w in array(&q["windows"]) {
                    let reset=match &w["resetAt"] {Value::String(s)=>rusqlite::types::Value::Text(s.clone()),v=>number(v).map(rusqlite::types::Value::Real).unwrap_or(rusqlite::types::Value::Null)};
                    s.db.execute("INSERT INTO quota_windows(provider,label,available_percent,used_percent,reset_at,window_seconds,status,updated_at) VALUES(?,?,?,?,?,?,?,?)",params![id,truncate(first(w,&["label"],"Window"),160),number(&w["availablePercent"]).unwrap_or(0.0),number(&w["usedPercent"]).unwrap_or(0.0),reset,number(&w["windowSeconds"]),status,updated])?;
                    count+=1;
                }
            }
            Ok(count)
        })
    }
    pub fn sync_hermes_project(
        &self,
        profile: &str,
        project: &Value,
        node: &str,
        virtual_project: bool,
    ) -> Result<usize> {
        let mut path = text(project, "path").to_owned();
        if path.is_empty() {
            path = array(&project["repos"])
                .iter()
                .map(|r| text(r, "path"))
                .find(|s| !s.is_empty())
                .unwrap_or("")
                .into();
        }
        if node == "local" && !virtual_project {
            let Ok(p) = crate::expand_path(&path).canonicalize() else {
                return Ok(0);
            };
            if !p.is_dir() {
                return Ok(0);
            }
            path = p.to_string_lossy().into_owned();
        }
        let fallback = std::path::Path::new(&path)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or(node);
        let label = truncate(first(project, &["label"], fallback), 256);
        let target = if virtual_project {
            let key = digest(format!("{node}\0{path}"));
            if self
                .rows("SELECT id FROM projects WHERE location_key=?", [&key])?
                .is_empty()
            {
                self.db.execute("INSERT INTO projects(id,name,node_id,path,location_key,repo_identity,created_at) VALUES(?,?,?,?,?,'',?)",params![ident("prj"),label,node,path,key,now()])?;
            }
            self.one(
                "SELECT id FROM projects WHERE location_key=?",
                [key],
                "project missing",
            )?
        } else {
            self.create_project(&label, &path, node, None)?
        };
        let mut sessions = std::collections::BTreeMap::new();
        for repo in array(&project["repos"]) {
            for group in array(&repo["groups"]) {
                for session in array(&group["sessions"]) {
                    let id = text(session, "id").trim();
                    if !id.is_empty() && text(session, "source") != "ares"
                        && self.rows("SELECT 1 FROM native_group_members WHERE host=? AND profile=? AND stored_session_id<>'' AND (stored_session_id=? OR resolved_session_id=?) LIMIT 1",params![node,profile,id,id])?.is_empty() {
                        sessions.insert(id, session);
                    }
                }
            }
        }
        let bot = if self
            .rows("SELECT id FROM bots WHERE id=? AND enabled=1", [profile])?
            .is_empty()
        {
            "default"
        } else {
            profile
        };
        self.transaction(|s|{let mut count=0;for (id,session) in sessions{let title=truncate(first(session,&["title"],id).trim(),256);let archived=session["externalArchived"].as_bool().unwrap_or(false);let activity=session["last_active"].as_f64().or(session["started_at"].as_f64()).filter(|v|*v!=0.0).unwrap_or_else(now);let old=s.rows("SELECT id,project_id,source FROM rooms WHERE external_profile=? AND external_session_id=?",params![profile,id])?;let room=if let Some(old)=old.first(){if old["source"] == "hermes-native-member" { continue; } if text(old,"project_id") != text(&target,"id") { for table in ["room_events","executions","artifacts"] { s.db.execute(&format!("UPDATE {table} SET project_id=? WHERE room_id=?"),params![text(&target,"id"),text(old,"id")])?; } } text(old,"id").to_owned()}else{let room=format!("room_hermes_{}",&digest(format!("{profile}\0{id}"))[..32]);s.db.execute("INSERT INTO rooms(id,project_id,name,status,memory_release,source,external_session_id,external_profile,external_updated_at,created_at,updated_at) VALUES(?,?,?,'active','not-requested','hermes',?,?,?,?,?)",params![room,text(&target,"id"),title,id,profile,activity,now(),activity])?;s.db.execute("INSERT INTO room_members(room_id,bot_id,position) VALUES(?,?,0)",params![room,bot])?;s.append_event(text(&target,"id"),&room,None,"room.connected","ares",json!({"text":"Connected to existing Hermes room","sessionId":id,"profile":profile,"source":truncate(text(session,"source"),64)}))?;count+=1;room};s.db.execute("UPDATE rooms SET project_id=?,name=?,external_updated_at=?,updated_at=?,external_archived=? WHERE id=?",params![text(&target,"id"),title,activity,activity,i64::from(archived),room])?;s.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,runtime_session_id,lifecycle_state,updated_at) VALUES(?,?,?,?,'','resident',?) ON CONFLICT(room_id,bot_id,node_id) DO UPDATE SET stored_session_id=excluded.stored_session_id,updated_at=excluded.updated_at",params![room,bot,node,id,now()])?;}Ok(count)})
    }
    pub fn sync_hermes_transcript(
        &self,
        profile: &str,
        session: &str,
        messages: &[Value],
        metadata: &Value,
    ) -> Result<usize> {
        let rooms=self.rows("SELECT * FROM rooms WHERE external_profile=? AND external_session_id=? AND deleted_at IS NULL",params![profile,session])?;
        let Some(room) = rooms.first() else {
            return Ok(0);
        };
        let activity_at = number(&metadata["last_activity_at"]);
        let mut facets: Vec<(String, String, String, Value, f64)> = Vec::new();
        let mut starts = HashMap::new();
        for m in messages.iter().skip(messages.len().saturating_sub(200)) {
            if !m.is_object() || m["display_kind"] == "hidden" {
                continue;
            }
            let role = text(m, "role");
            let message_id = scalar_string(&m["id"]);
            let mid = message_id.trim();
            let created = number(&m["timestamp"])
                .or(activity_at)
                .or(room["external_updated_at"].as_f64())
                .filter(|v| *v != 0.0)
                .unwrap_or_else(now);
            let base = json!({"externalMessageId":mid,"externalSessionId":session,"mirrored":true});
            if role == "assistant"
                && ["reasoning", "reasoning_content", "reasoning_details"]
                    .iter()
                    .any(|k| truthy(&m[*k]))
            {
                let tokens = &m["token_count"];
                let mut p = base.clone();
                p["text"] = json!(tokens
                    .as_u64()
                    .filter(|n| *n > 0)
                    .map(|n| format!("Thinking · {n} tokens"))
                    .unwrap_or("Thinking…".into()));
                p["tokenCount"] = tokens.clone();
                p["sourceOrder"] = json!(0);
                facets.push((
                    "reasoning".into(),
                    "reasoning.status".into(),
                    "assistant".into(),
                    p,
                    created,
                ));
            }
            let content = m
                .get("display_content")
                .filter(|v| !v.is_null())
                .unwrap_or(&m["content"]);
            let mut body = content_text(content, "\n", true).trim().to_owned();
            if role == "user" {
                if body.starts_with("[CONTEXT COMPACTION — REFERENCE ONLY]") {
                    body.clear();
                }
                let marker="[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool output and not a new delivery when replayed from conversation history]";
                if let Some((_, rest)) = body.split_once(marker) {
                    if let Some((inner, _)) = rest.split_once("[/OUT-OF-BAND USER MESSAGE]") {
                        body = inner.trim().into();
                    }
                }
            }
            if ["user", "assistant"].contains(&role) && !body.is_empty() {
                let mut p = base.clone();
                p["text"] = sanitize(json!(body));
                p["sourceOrder"] = json!(1);
                facets.push((
                    "message".into(),
                    format!("message.{role}"),
                    role.into(),
                    p,
                    created,
                ));
            }
            if role == "assistant" {
                for (i, call) in array(&m["tool_calls"]).iter().enumerate() {
                    if !call.is_object() {
                        continue;
                    }
                    let f = &call["function"];
                    let name = truncate(first(f, &["name"], first(call, &["name"], "tool")), 160);
                    let raw = f
                        .get("arguments")
                        .or(call.get("arguments"))
                        .cloned()
                        .unwrap_or(json!({}));
                    let args = sanitize(if let Some(s) = raw.as_str() {
                        serde_json::from_str(s).unwrap_or(raw)
                    } else {
                        raw
                    });
                    let cid = truncate(first(call, &["id"], &format!("{mid}:{i}")), 256);
                    starts.insert(cid.clone(), created);
                    let rendered = if name == "terminal" && args["command"].is_string() {
                        format!("$ {}", text(&args, "command"))
                    } else {
                        let a = args
                            .as_str()
                            .map(str::to_owned)
                            .unwrap_or_else(|| serde_json::to_string_pretty(&args).unwrap());
                        format!(
                            "{name}{}",
                            if a.is_empty() {
                                String::new()
                            } else {
                                format!("\n{a}")
                            }
                        )
                    };
                    let mut p = base.clone();
                    p["text"] = json!(rendered);
                    p["toolName"] = json!(name);
                    p["callId"] = json!(cid);
                    p["arguments"] = args;
                    p["sourceOrder"] = json!(2.0 + i as f64 / 1000.0);
                    facets.push((
                        format!("tool-call:{cid}"),
                        "tool.call".into(),
                        "assistant".into(),
                        p,
                        created,
                    ));
                }
            }
            if role == "tool" && !body.is_empty() {
                let safe = sanitize(serde_json::from_str(&body).unwrap_or(json!(body)));
                let cid = truncate(text(m, "tool_call_id"), 256);
                let mut p = base.clone();
                p["toolName"] = json!(truncate(first(m, &["tool_name"], "tool"), 160));
                p["callId"] = json!(cid);
                p["text"] = json!(crate::render::tool_text(&safe));
                p["sourceOrder"] = json!(3);
                if let Some(v) = safe.get("exit_code") {
                    p["exitCode"] = v.clone();
                }
                if let Some(success) = safe["success"].as_bool() {
                    p["status"] = json!(if success { "ok" } else { "failed" });
                }
                if truthy(&safe["error"]) {
                    p["error"] = safe["error"].clone();
                    p["status"] = json!("failed");
                }
                if let Some(start) = starts.get(&cid) {
                    p["durationSeconds"] = json!((created - start).max(0.0));
                }
                facets.push((
                    format!("tool-result:{}", if cid.is_empty() { mid } else { &cid }),
                    "tool.result".into(),
                    "tool".into(),
                    p,
                    created,
                ));
            }
        }
        self.transaction(|s| {
            let rid=text(room,"id");
            if let Some(latest)=facets.iter().map(|f|f.4).reduce(f64::max) {
                s.db.execute("DELETE FROM room_events WHERE room_id=? AND created_at<=? AND json_extract(payload,'$.ephemeral')=1 AND json_extract(payload,'$.externalSessionId')=?",params![rid,latest,session])?;
            }
            let bots=s.rows("SELECT bot_id FROM room_members WHERE room_id=? ORDER BY position LIMIT 1",[rid])?;
            let bot=bots.first().map(|b|text(b,"bot_id")).unwrap_or("default");
            let mut inserted=0;
            for (facet,kind,author,payload,created) in facets {
                let mut identity=text(&payload,"externalMessageId").to_owned();
                if identity.is_empty() {
                    identity=json!({"kind":kind,"timestamp":created,"payload":payload}).to_string();
                }
                if facet!="message" {identity.push('\0');identity.push_str(&facet);}
                let stable=format!("hermes_msg_{}",digest(format!("{profile}\0{session}\0{identity}")));
                inserted+=s.db.execute("INSERT OR IGNORE INTO room_events(id,project_id,room_id,execution_id,sequence,kind,author,payload,created_at) VALUES(?,?,?,NULL,(SELECT COALESCE(MAX(sequence),0)+1 FROM room_events WHERE room_id=?),?,?,?,?)",params![stable,text(room,"project_id"),rid,rid,kind,if ["user","tool"].contains(&author.as_str()){&author}else{bot},sanitize(payload).to_string(),created])?;
            }
            if inserted>0{s.db.execute("DELETE FROM room_events WHERE room_id=? AND kind='room.connected'",[rid])?;}s.db.execute("UPDATE rooms SET external_activity=?,external_activity_at=?,external_updated_at=COALESCE(?,external_updated_at),external_transcript_version=2,updated_at=MAX(updated_at,COALESCE(?,updated_at)),revision=revision+? WHERE id=?",params![truncate(text(metadata,"last_activity_description").trim(),500),activity_at,activity_at,activity_at.or(room["external_updated_at"].as_f64()),i64::from(inserted>0),rid])?;Ok(inserted)})
    }
}
pub fn content_text(v: &Value, separator: &str, typed: bool) -> String {
    if let Some(s) = v.as_str() {
        s.into()
    } else if let Some(a) = v.as_array() {
        a.iter()
            .filter_map(|v| {
                v.as_str().or_else(|| {
                    if !typed || ["text", "input_text", "output_text"].contains(&text(v, "type")) {
                        v["text"].as_str()
                    } else {
                        None
                    }
                })
            })
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(separator)
    } else if v.is_null() {
        String::new()
    } else {
        v.to_string()
    }
}
