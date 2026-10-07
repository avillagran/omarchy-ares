use crate::{
    digest, expand_path, ident, now, private_dir, require, text, truncate, write_private, Result,
    MAX_ARTIFACT_BYTES,
};
use rusqlite::{params, types::ValueRef, Connection, Params};
use serde_json::{json, Map, Value};
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

pub struct Store {
    pub db: Connection,
    pub artifact_root: PathBuf,
    pub hermes_home: PathBuf,
}

impl Store {
    pub fn open(path: &Path, artifact_root: &Path, hermes_home: PathBuf) -> Result<Self> {
        private_dir(path.parent().ok_or("database has no parent")?)?;
        private_dir(artifact_root)?;
        let db = Connection::open(path)?;
        db.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;",
        )?;
        let s = Self {
            db,
            artifact_root: artifact_root.into(),
            hermes_home,
        };
        s.migrate()?;
        s.transaction(|s| {
            for row in s.rows("SELECT * FROM executions WHERE status IN ('queued','running')", [])? {
                s.db.execute("UPDATE executions SET status='interrupted',error='Ares service restarted',finished_at=? WHERE id=?", params![now(), text(&row,"id")])?;
                s.append_event(text(&row,"project_id"), text(&row,"room_id"), Some(text(&row,"id")), "execution.interrupted", "ares", json!({"text":"Ares service restarted"}))?;
            }
            Ok(())
        })?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
        Ok(s)
    }
    fn migrate(&self) -> Result<()> {
        self.db.execute_batch(include_str!("schema.sql"))?;
        for (table, columns) in [
            ("projects", vec![("map_color", "TEXT NOT NULL DEFAULT ''"), ("map_x", "INTEGER"), ("map_y", "INTEGER")]),
            (
                "rooms",
                vec![
                    ("source", "TEXT NOT NULL DEFAULT 'ares'"),
                    ("external_session_id", "TEXT NOT NULL DEFAULT ''"),
                    ("external_profile", "TEXT NOT NULL DEFAULT ''"),
                    ("external_updated_at", "REAL"),
                    ("external_archived", "INTEGER NOT NULL DEFAULT 0"),
                    ("external_activity", "TEXT NOT NULL DEFAULT ''"),
                    ("external_activity_at", "REAL"),
                    ("external_transcript_version", "INTEGER NOT NULL DEFAULT 0"),
                    ("alias", "TEXT NOT NULL DEFAULT ''"),
                    ("control_included", "INTEGER NOT NULL DEFAULT 1"),
                    ("native_suppressed", "INTEGER NOT NULL DEFAULT 0"),
                ],
            ),
            (
                "peers",
                vec![
                    ("source_name", "TEXT NOT NULL DEFAULT ''"),
                    ("color_role", "TEXT NOT NULL DEFAULT 'accent'"),
                ],
            ),
        ] {
            let existing = self.rows(&format!("PRAGMA table_info({table})"), [])?;
            for (name, declaration) in columns {
                if !existing.iter().any(|r| text(r, "name") == name) {
                    self.transaction(|s| {
                        s.db.execute_batch(&format!(
                            "ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        ))?;
                        if table == "rooms" && name == "native_suppressed" {
                            // Older catalogs used deleted_at for native suppression too.
                            // Local archive events remain authoritative even if native sync
                            // subsequently changed the room status to paused.
                            for group in s.rows("SELECT * FROM native_groups WHERE tombstone IS NOT NULL AND room_id IS NOT NULL", [])? {
                                let tombstone = serde_json::from_str::<Value>(text(&group,"tombstone")).unwrap_or(Value::Null);
                                let suppressed = crate::native_groups::deleted_at(text(&group,"desktop_key"), &tombstone, group["revision"].as_i64().unwrap_or(0));
                                s.db.execute("UPDATE rooms SET native_suppressed=?,deleted_at=CASE WHEN status='deleted' OR COALESCE((SELECT kind FROM room_events WHERE room_id=rooms.id AND kind IN ('room.deleted','room.restored') ORDER BY cursor DESC LIMIT 1),'')='room.deleted' THEN deleted_at ELSE NULL END WHERE id=? AND source='hermes-native-group'",params![suppressed,text(&group,"room_id")])?;
                            }
                        }
                        Ok(())
                    })?;
                }
            }
        }
        self.db.execute_batch("UPDATE peers SET source_name=name WHERE source_name='' OR source_name IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_external_session ON rooms(external_profile,external_session_id) WHERE external_session_id!='';
            INSERT OR IGNORE INTO bots(id,name,description,profile,node_id,face_shape,color_role) VALUES('default','Hermes','Default local Hermes agent','default','local','diamond','accent');
            INSERT OR IGNORE INTO preferences(key,value) VALUES('homeQuotaVisible','true'),('homeQuotaCollapsed','false'),('hiddenHomeQuotaProviders','[]');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_map_color ON projects(map_color) WHERE map_color!='';")?;
        Ok(())
    }
    pub fn transaction<T>(&self, f: impl FnOnce(&Self) -> Result<T>) -> Result<T> {
        self.db.execute_batch("BEGIN IMMEDIATE")?;
        let result = f(self);
        match result {
            Ok(value) => {
                if let Err(e) = self.db.execute_batch("COMMIT") {
                    let _ = self.db.execute_batch("ROLLBACK");
                    return Err(e.into());
                }
                Ok(value)
            }
            Err(e) => {
                let _ = self.db.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }
    pub fn rows(&self, sql: &str, params: impl Params) -> Result<Vec<Value>> {
        let mut stmt = self.db.prepare(sql)?;
        let names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
        let rows = stmt
            .query_map(params, |r| {
                let mut obj = Map::new();
                for (i, name) in names.iter().enumerate() {
                    let value = match r.get_ref(i)? {
                        ValueRef::Null => Value::Null,
                        ValueRef::Integer(n) => json!(n),
                        ValueRef::Real(n) => json!(n),
                        ValueRef::Text(s) => json!(String::from_utf8_lossy(s)),
                        ValueRef::Blob(s) => json!(hex::encode(s)),
                    };
                    obj.insert(name.clone(), value);
                }
                Ok(Value::Object(obj))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }
    pub fn one(&self, sql: &str, params: impl Params, error: &str) -> Result<Value> {
        self.rows(sql, params)?
            .into_iter()
            .next()
            .ok_or_else(|| error.into())
    }
    fn scalar(&self, sql: &str, params: impl Params) -> Result<i64> {
        Ok(self.db.query_row(sql, params, |r| r.get(0))?)
    }
    pub fn room(&self, id: &str, deleted: bool) -> Result<Value> {
        self.one(
            if deleted {
                "SELECT * FROM rooms WHERE id=?"
            } else {
                "SELECT * FROM rooms WHERE id=? AND deleted_at IS NULL AND native_suppressed=0"
            },
            [id],
            "room does not exist",
        )
    }
    fn owned_room(&self, project: &str, room: &str, deleted: bool) -> Result<Value> {
        let row = self.room(room, deleted)?;
        if text(&row, "project_id") != project {
            return Err("room does not belong to the project".into());
        }
        Ok(row)
    }
    pub fn dto(&self, table: &str, row: &Value) -> Result<Value> {
        let fields = match table {
            "project_groups" => "id name instructions created_at",
            "projects" => "id group_id name node_id:node path location_key repo_identity map_color:mapColor map_x:mapX map_y:mapY created_at",
            "rooms" => "id project_id name alias status memory_release revision source control_included:controlIncluded external_archived:externalArchived external_session_id external_profile external_updated_at external_activity external_activity_at created_at updated_at deleted_at",
            "executions" => "id project_id room_id bot_id prompt status final_text error created_at started_at finished_at",
            "room_events" => "cursor id project_id room_id execution_id sequence kind author payload created_at",
            "artifacts" => "id project_id room_id execution_id event_id author name mime size sha256 created_at",
            "session_bindings" => "room_id bot_id node_id stored_session_id runtime_session_id lifecycle_state:releaseState updated_at",
            "bots" => "id name description profile node_id model provider face_shape color_role",
            "peers" => "id name endpoint status source_name color_role capabilities last_seen_at updated_at",
            "quota_windows" => "provider label available_percent used_percent reset_at window_seconds status updated_at",
            "quota_providers" => "provider hour_tokens week_tokens hour_calls week_calls plan details status updated_at",
            "drafts" => "room_id text updated_at",
            _ => return Err("unknown DTO".into()),
        };
        let mut out = Map::new();
        for field in fields.split_whitespace() {
            let (column, key) = field
                .split_once(':')
                .map(|(c, k)| (c, k.to_string()))
                .unwrap_or_else(|| (field, camel(field)));
            let value = if ["payload", "capabilities", "details"].contains(&column) {
                serde_json::from_str(text(row, column))?
            } else {
                row[column].clone()
            };
            out.insert(key, value);
        }
        let mut out = Value::Object(out);
        match table {
            "rooms" => {
                if text(row, "source") == "hermes-native-group" {
                    out["nativeGroup"] = self.native_group_dto(text(row, "id"))?;
                }
                out["controlIncluded"] = json!(row["control_included"].as_i64() == Some(1));
                out["externalArchived"] = json!(row["external_archived"].as_i64() == Some(1));
                out["participants"] = json!(self
                    .rows(
                        "SELECT bot_id FROM room_members WHERE room_id=? ORDER BY position",
                        [text(row, "id")]
                    )?
                    .iter()
                    .map(|r| r["bot_id"].clone())
                    .collect::<Vec<_>>());
                out["eventCount"] = json!(self.scalar(
                    "SELECT COUNT(*) FROM room_events WHERE room_id=?",
                    [text(row, "id")]
                )?);
            }
            "room_events" => {
                let p = &out["payload"];
                let mut t = text(p, "text").to_owned();
                if t.is_empty() {
                    t = text(p, "delta").to_owned();
                }
                if text(row, "kind").starts_with("tool.") {
                    if !t.is_empty() {
                        t = crate::render::tool_text(&json!(t));
                    } else {
                        for key in ["output", "result"] {
                            if let Some(v) = p.get(key) {
                                t = crate::render::tool_text(v);
                                break;
                            }
                        }
                        if t.is_empty() && p.get("args").is_some_and(truthy) {
                            t = crate::render::tool_args(&p["args"]);
                        }
                        if t.is_empty() {
                            t = text(p, "error").to_owned();
                        }
                    }
                }
                if text(row, "kind") == "message.assistant" {
                    out["html"] = json!(crate::render::markdown(&t));
                }
                out["text"] = json!(t);
            }
            "artifacts" => out["url"] = json!(format!("/v1/artifacts/{}", text(row, "id"))),
            "drafts" => {
                let ids: Vec<String> = serde_json::from_str(text(row, "attachment_ids"))?;
                let mut attachments = Vec::new();
                for id in ids {
                    if let Some(artifact) = self.rows(
                        "SELECT id,name,mime FROM artifacts WHERE id=? AND room_id=? AND mime LIKE 'image/%'",
                        params![id, text(row, "room_id")],
                    )?.into_iter().next() {
                        attachments.push(json!({"id":artifact["id"],"name":artifact["name"],"mime":artifact["mime"]}));
                    }
                }
                out["attachmentIds"] = json!(attachments
                    .iter()
                    .map(|a| a["id"].clone())
                    .collect::<Vec<_>>());
                out["attachments"] = json!(attachments);
            }
            "peers" if text(row, "source_name").is_empty() => {
                out["sourceName"] = row["name"].clone()
            }
            "quota_windows" => {
                let seconds = row["window_seconds"].as_f64().unwrap_or(0.0);
                out["dailySeparators"] = json!(if seconds >= 86400.0 {
                    ((seconds / 86400.0).round_ties_even() as i64 - 1).max(0)
                } else {
                    0
                });
            }
            "quota_providers" => {
                out["windows"] = json!(self.dtos(
                    "quota_windows",
                    "SELECT * FROM quota_windows WHERE provider=? ORDER BY window_seconds,label",
                    [text(row, "provider")]
                )?)
            }
            _ => (),
        }
        Ok(out)
    }
    pub fn dtos(&self, table: &str, sql: &str, params: impl Params) -> Result<Vec<Value>> {
        self.rows(sql, params)?
            .iter()
            .map(|r| self.dto(table, r))
            .collect()
    }
    pub fn create_group(&self, name: &str, instructions: &str) -> Result<Value> {
        let id = ident("grp");
        let name = require(name, "group name", 256)?;
        let created = now();
        self.db.execute(
            "INSERT INTO project_groups(id,name,instructions,created_at) VALUES(?,?,?,?)",
            params![id, name, instructions.trim(), created],
        )?;
        Ok(json!({"id":id,"name":name,"instructions":instructions.trim(),"createdAt":created}))
    }
    pub fn create_project(
        &self,
        name: &str,
        path: &str,
        node: &str,
        group: Option<&str>,
    ) -> Result<Value> {
        let node = require(node, "node", 128)?;
        let raw = require(path, "project path", 2048)?;
        let location = if node == "local" {
            expand_path(&raw)
                .canonicalize()
                .map_err(|_| "project path must be an existing directory")?
                .to_string_lossy()
                .into_owned()
        } else {
            raw
        };
        if node == "local" && !Path::new(&location).is_dir() {
            return Err("project path must be an existing directory".into());
        }
        if let Some(g) = group.filter(|g| !g.is_empty()) {
            self.one(
                "SELECT id FROM project_groups WHERE id=?",
                [g],
                "group does not exist",
            )?;
        }
        let key = digest(format!("{node}\0{location}"));
        if let Some(row) = self
            .rows("SELECT * FROM projects WHERE location_key=?", [&key])?
            .first()
        {
            return self.dto("projects", row);
        }
        let id = ident("prj");
        let name = require(name, "project name", 256)?;
        let repo = if node == "local" {
            Path::new(&location)
                .join(".git")
                .canonicalize()
                .ok()
                .map(|p| digest(p.to_string_lossy().as_bytes()))
                .unwrap_or_default()
        } else {
            String::new()
        };
        self.db.execute("INSERT INTO projects(id,group_id,name,node_id,path,location_key,repo_identity,created_at) VALUES(?,?,?,?,?,?,?,?)",params![id,group,name,node,location,key,repo,now()])?;
        self.dto(
            "projects",
            &self.one(
                "SELECT * FROM projects WHERE id=?",
                [id],
                "project does not exist",
            )?,
        )
    }
    pub fn update_project_map_color(&self, id: &str, color: &str) -> Result<Value> {
        if !color.is_empty()
            && (color.len() != 7
                || !color.starts_with('#')
                || !color[1..].bytes().all(|byte| byte.is_ascii_hexdigit()))
        {
            return Err("map color must be a #RRGGBB hex value or empty to reset".into());
        }
        let color = color.to_ascii_uppercase();
        self.one(
            "SELECT id FROM projects WHERE id=?",
            [id],
            "project does not exist",
        )?;
        self.db.execute(
            "UPDATE projects SET map_color=? WHERE id=?",
            params![color, id],
        )?;
        self.dto(
            "projects",
            &self.one(
                "SELECT * FROM projects WHERE id=?",
                [id],
                "project does not exist",
            )?,
        )
    }
    pub fn update_project_map_position(&self, id: &str, x: i64, y: i64) -> Result<Value> {
        if !(0..=9600).contains(&x) || !(0..=7200).contains(&y) {
            return Err("map position must be inside the world".into());
        }
        self.one("SELECT id FROM projects WHERE id=?", [id], "project does not exist")?;
        self.db.execute("UPDATE projects SET map_x=?, map_y=? WHERE id=?", params![x, y, id])?;
        self.dto("projects", &self.one("SELECT * FROM projects WHERE id=?", [id], "project does not exist")?)
    }
    pub fn create_room(&self, project: &str, name: &str, participants: &[Value]) -> Result<Value> {
        self.one(
            "SELECT id FROM projects WHERE id=?",
            [project],
            "project does not exist",
        )?;
        let mut members = Vec::new();
        for p in participants {
            let bot = require(p.as_str().unwrap_or(""), "bot", 128)?;
            if !members.contains(&bot) {
                members.push(bot);
            }
        }
        if !(1..=6).contains(&members.len()) {
            return Err("a room requires one to six unique bots".into());
        }
        let id = ident("room");
        let name = require(name, "room name", 256)?;
        let created = now();
        self.transaction(|s| {
            s.db.execute("INSERT INTO rooms(id,project_id,name,status,memory_release,created_at,updated_at) VALUES(?,?,?,'active','not-requested',?,?)",params![id,project,name,created,created])?;
            for (position, bot) in members.iter().enumerate() { s.db.execute("INSERT INTO room_members(room_id,bot_id,position) VALUES(?,?,?)",params![id,bot,position])?; }
            s.append_event(project,&id,None,"room.created","ares",json!({"text":"Room created","participants":members}))?;
            s.dto("rooms", &s.room(&id,false)?)
        })
    }
    pub(crate) fn append_event(
        &self,
        project: &str,
        room: &str,
        execution: Option<&str>,
        kind: &str,
        author: &str,
        payload: Value,
    ) -> Result<()> {
        self.owned_room(project, room, true)?;
        let sequence = self.scalar(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM room_events WHERE room_id=?",
            [room],
        )?;
        let created = now();
        self.db.execute("INSERT INTO room_events(id,project_id,room_id,execution_id,sequence,kind,author,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?)",params![ident("evt"),project,room,execution,sequence,require(kind,"event kind",128)?,require(author,"event author",128)?,payload.to_string(),created])?;
        self.db.execute(
            "UPDATE rooms SET updated_at=?,revision=revision+1 WHERE id=?",
            params![created, room],
        )?;
        Ok(())
    }
    fn receipt(&self, scope: &str, key: &str, payload: &Value) -> Result<Option<Value>> {
        let key = require(key, "idempotency key", 160)?;
        if let Some(row) = self.rows("SELECT payload_hash,response FROM command_receipts WHERE scope=? AND idempotency_key=?",params![scope,key])?.first() {
            if text(row,"payload_hash") != digest(payload.to_string()) { return Err("idempotency conflict".into()); }
            return Ok(Some(serde_json::from_str(text(row,"response"))?));
        }
        Ok(None)
    }
    fn save_receipt(
        &self,
        scope: &str,
        key: &str,
        payload: &Value,
        response: &Value,
    ) -> Result<()> {
        self.db.execute("INSERT INTO command_receipts(scope,idempotency_key,payload_hash,response,created_at) VALUES(?,?,?,?,?)",params![scope,key,digest(payload.to_string()),response.to_string(),now()])?;
        Ok(())
    }
    pub fn accept_prompt(
        &self,
        project: &str,
        room: &str,
        bot: &str,
        prompt: &str,
        key: &str,
        artifacts: &[Value],
    ) -> Result<Value> {
        let r = self.owned_room(project, room, false)?;
        if text(&r, "status") != "active" {
            return Err("room is not active".into());
        }
        if matches!(
            text(&r, "source"),
            "hermes-native-group" | "hermes-native-member"
        ) {
            return Err("Native groups require their dedicated Desktop transport".into());
        }
        if text(&r, "source") == "control" {
            return Err("Control Room decisions cannot be submitted as execution prompts".into());
        }
        let prompt = require(prompt, "prompt", 100_000)?;
        let mut ids = Vec::new();
        for id in artifacts {
            if !ids.contains(id) {
                ids.push(id.clone());
            }
        }
        let payload = json!({"projectId":project,"roomId":room,"botId":bot,"prompt":prompt,"artifactIds":ids});
        let scope = format!("send:{room}");
        self.transaction(|s| {
            if let Some(replay) = s.receipt(&scope,key,&payload)? { return Ok(replay); }
            if s.scalar("SELECT COUNT(*) FROM terminal_bindings WHERE room_id=?",[room])? > 0 {
                return Err("This room is associated with a Herdr terminal; use its native input".into());
            }
            let id = ident("exec");
            s.db.execute("INSERT INTO executions(id,project_id,room_id,bot_id,prompt,status,created_at) VALUES(?,?,?,?,?,'queued',?)",params![id,project,room,bot,prompt,now()])?;
            s.append_event(project,room,Some(&id),"message.user","user",json!({"text":prompt}))?;
            for artifact in &ids {
                let artifact = artifact.as_str().ok_or("artifact does not belong to the room")?;
                let a = s.one("SELECT * FROM artifacts WHERE id=?",[artifact],"artifact does not belong to the room")?;
                if text(&a,"project_id") != project || text(&a,"room_id") != room { return Err("artifact does not belong to the room".into()); }
                let detail = "Waiting for the selected Hermes execution route";
                s.db.execute("INSERT INTO execution_artifacts(execution_id,artifact_id,delivery_state,detail,updated_at) VALUES(?,?,'pending',?,?)",params![id,artifact,detail,now()])?;
                s.append_event(project,room,Some(&id),"artifact.delivery.pending","ares",json!({"text":detail,"artifactId":artifact,"mime":a["mime"]}))?;
            }
            s.append_event(project,room,Some(&id),"execution.queued",bot,json!({"text":"Queued"}))?;
            let result = s.dto("executions",&s.one("SELECT * FROM executions WHERE id=?",[id],"execution does not exist")?)?;
            s.save_receipt(&scope,key,&payload,&result)?;
            // Use the stored representation on the initial response as well as replays.
            // This avoids a floating-point JSON parse changing timestamp bytes on replay.
            s.receipt(&scope,key,&payload)?.ok_or_else(|| "receipt disappeared".into())
        })
    }
    pub fn lifecycle(&self, project: &str, room: &str, key: &str, action: &str) -> Result<Value> {
        let action = if action == "archive" {
            "delete"
        } else {
            action
        };
        let scope = format!(
            "{}:{room}",
            match action {
                "delete" => "delete",
                "restore" => "restore",
                _ => "lifecycle",
            }
        );
        let payload = json!({"projectId":project,"roomId":room,"action":action});
        self.transaction(|s| {
            let target = s.owned_room(project,room,action == "restore")?;
            if matches!(text(&target,"source"), "hermes-native-group" | "hermes-native-member") && !["delete","restore"].contains(&action) {
                return Err("Native groups require their dedicated Desktop lifecycle".into());
            }
            if text(&target,"source") == "hermes-native-group" && action == "restore" {
                let group=s.one("SELECT desktop_key,revision,tombstone FROM native_groups WHERE room_id=?",[room],"Native group missing")?;
                if let Some(t) = group["tombstone"].as_str() { if crate::native_groups::deleted_at(text(&group,"desktop_key"), &serde_json::from_str::<Value>(t).unwrap_or(Value::Null), group["revision"].as_i64().unwrap_or(0)) { return Err("Deleted native groups cannot be restored locally".into()); } }
            }
            if text(&target,"source") == "control" { return Err("Control Room cannot be paused or archived".into()); }
            if let Some(replay) = s.receipt(&scope,key,&payload)? { return Ok(replay); }
            let (status,memory,kind,message) = match action {
                "pause" => ("paused","unconfirmed","room.pause.requested","Pause requested"),
                "resume" => ("active","not-requested","room.resume.requested","Room resumed"),
                "delete" => ("deleted","","room.deleted","Room moved to recovery"),
                "restore" => ("paused","unconfirmed","room.restored","Room restored in paused state"),
                _ => return Err("invalid room action".into()),
            };
            if action == "delete" {
                if s.scalar("SELECT COUNT(*) FROM executions WHERE room_id=? AND status IN ('queued','running')",[room])? > 0 { return Err("room has unfinished executions".into()); }
                let t = now(); s.db.execute("UPDATE rooms SET status='deleted',deleted_at=?,updated_at=?,revision=revision+1 WHERE id=?",params![t,t,room])?;
            } else {
                s.db.execute("UPDATE rooms SET status=?,memory_release=?,revision=revision+1,updated_at=? WHERE id=?",params![status,memory,now(),room])?;
                if action == "restore" { s.db.execute("UPDATE rooms SET deleted_at=NULL WHERE id=?",[room])?; }
            }
            let mut event = json!({"text":message});
            let mut result = json!({"roomId":room,"status":status});
            if action == "delete" { result["recoverable"] = json!(true); } else { result["memoryRelease"] = json!(memory); }
            if action == "pause" {
                s.db.execute("UPDATE session_bindings SET lifecycle_state='unconfirmed',updated_at=? WHERE room_id=?",params![now(),room])?;
                event["memoryRelease"] = json!(memory);
                result["sessions"] = json!(s.dtos("session_bindings","SELECT * FROM session_bindings WHERE room_id=? ORDER BY bot_id,node_id",[room])?);
            }
            s.append_event(project,room,None,kind,"ares",event)?;
            s.save_receipt(&scope,key,&payload,&result)?;
            s.receipt(&scope,key,&payload)?.ok_or_else(|| "receipt disappeared".into())
        })
    }
    pub fn update_room_alias(&self, room: &str, alias: &str) -> Result<Value> {
        let room = require(room, "room", 128)?;
        self.one(
            "SELECT id FROM rooms WHERE id=? AND deleted_at IS NULL",
            [&room],
            "room not found",
        )?;
        let alias = if alias.trim().is_empty() {
            String::new()
        } else {
            require(alias, "alias", 120)?
        };
        self.db.execute(
            "UPDATE rooms SET alias=?,updated_at=?,revision=revision+1 WHERE id=?",
            params![alias, now(), room],
        )?;
        self.dto("rooms", &self.room(&room, false)?)
    }
    pub fn preferences(&self) -> Result<Value> {
        let mut p = json!({"homeQuotaVisible":true,"homeQuotaCollapsed":false,"hiddenHomeQuotaProviders":[],"panelSize":"medium","sidebarCollapsed":false,"sidebarWidth":200,"terminalFontSize":10,"terminalBackgroundRole":"popups.background","terminalBackgroundOpacity":1.0,"terminalBlur":false,"terminalPreviewReadOnly":true,"searchShortcut":"Ctrl+Space","controlTileOrder":[],"controlMapCategories":{"groups":[],"assignments":{}}});
        for row in self.rows("SELECT key,value FROM preferences", [])? {
            let key = text(&row, "key");
            if p.get(key).is_some() {
                p[key] = serde_json::from_str(text(&row, "value"))?;
            }
        }
        Ok(p)
    }
    pub fn update_preferences(&self, values: &Value) -> Result<Value> {
        let mut values = values.as_object().ok_or("JSON object required")?.clone();
        let mut cleaned_categories = None;
        for (key, v) in &values {
            match key.as_str() {
                "homeQuotaVisible"
                | "homeQuotaCollapsed"
                | "sidebarCollapsed"
                | "terminalBlur"
                | "terminalPreviewReadOnly" => {
                    if !v.is_boolean() {
                        return Err(format!("{key} must be boolean").into());
                    }
                }
                "panelSize" => {
                    if !["small", "medium", "large"].contains(&v.as_str().unwrap_or("")) {
                        return Err("panelSize must be one of: small, medium, large".into());
                    }
                }
                "sidebarWidth" => {
                    if !v.as_i64().is_some_and(|n| (120..=480).contains(&n)) {
                        return Err("sidebarWidth must be an integer between 120 and 480".into());
                    }
                }
                "terminalFontSize" => {
                    if !v.as_i64().is_some_and(|n| (7..=28).contains(&n)) {
                        return Err("terminalFontSize must be an integer between 7 and 28".into());
                    }
                }
                "terminalBackgroundRole" => {
                    if !["popups.background", "background", "accent", "muted"]
                        .contains(&v.as_str().unwrap_or(""))
                    {
                        return Err("terminalBackgroundRole must be a theme color role".into());
                    }
                }
                "terminalBackgroundOpacity" => {
                    if !v.as_f64().is_some_and(|n| (0.0..=1.0).contains(&n)) {
                        return Err("terminalBackgroundOpacity must be between 0 and 1".into());
                    }
                }
                "searchShortcut" => {
                    if !matches!(v.as_str(), Some("Ctrl+Space" | "Alt+Space")) {
                        return Err("searchShortcut must be one of: Ctrl+Space, Alt+Space".into());
                    }
                }
                "hiddenHomeQuotaProviders" | "selectedRoomId" => (),
                "controlMapCategories" => {
                    let categories = v.as_object().filter(|o| o.len() == 2
                        && o.contains_key("groups") && o.contains_key("assignments"))
                        .ok_or("controlMapCategories requires groups and assignments")?;
                    let groups = categories["groups"].as_array().filter(|g| g.len() <= 64)
                        .ok_or("controlMapCategories groups must be an array of up to 64")?;
                    let mut ids = std::collections::HashSet::new();
                    for group in groups {
                        let group = group.as_object().filter(|g| g.len() == 3
                            && g.contains_key("id") && g.contains_key("name") && g.contains_key("color"))
                            .ok_or("category requires id, name and color")?;
                        let id = group["id"].as_str().filter(|id| !id.is_empty() && id.len() <= 64
                            && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'))
                            .ok_or("invalid category ID")?;
                        if !ids.insert(id) { return Err("duplicate category ID".into()); }
                        group["name"].as_str().filter(|name| !name.trim().is_empty()
                            && name.chars().count() <= 120 && !name.chars().any(char::is_control))
                            .ok_or("invalid category name")?;
                        group["color"].as_str().filter(|color| color.len() == 7
                            && color.starts_with('#') && color[1..].bytes().all(|b| b.is_ascii_hexdigit()))
                            .ok_or("invalid category color")?;
                    }
                    let assignments = categories["assignments"].as_object()
                        .ok_or("category assignments must be an object")?;
                    let previous = self.preferences()?["controlMapCategories"]["groups"]
                        .as_array().ok_or("invalid saved categories")?.iter()
                        .filter_map(|g| g["id"].as_str().map(str::to_owned))
                        .collect::<std::collections::HashSet<_>>();
                    let mut kept = Map::new();
                    for (project, group) in assignments {
                        if project.is_empty() || project.len() > 128
                            || self.rows("SELECT id FROM projects WHERE id=?", [project])?.is_empty() {
                            return Err("category assignment has unknown project".into());
                        }
                        let group_id = group.as_str().ok_or("invalid category assignment")?;
                        if ids.contains(group_id) {
                            kept.insert(project.clone(), group.clone());
                        } else if !previous.contains(group_id) {
                            return Err("category assignment has unknown group".into());
                        }
                    }
                    cleaned_categories = Some(json!({"groups":groups,"assignments":kept}));
                }
                "controlTileOrder" => {
                    let ids = v.as_array().filter(|ids| ids.len() <= 256)
                        .ok_or("controlTileOrder must be an array of up to 256 projects")?;
                    let mut seen = std::collections::HashSet::new();
                    for id in ids {
                        let id = id.as_str().filter(|id| !id.is_empty() && id.len() <= 128)
                            .ok_or("invalid control tile project ID")?;
                        if !seen.insert(id) || self.rows("SELECT id FROM projects WHERE id=?", [id])?.is_empty() {
                            return Err("control tile order contains a duplicate or unknown project".into());
                        }
                    }
                }
                _ => return Err(format!("unsupported preference: {key}").into()),
            }
        }
        if let Some(categories) = cleaned_categories {
            values.insert("controlMapCategories".into(), categories);
        }
        if let Some(hidden) = values
            .get("hiddenHomeQuotaProviders")
            .filter(|v| !v.is_null())
        {
            let items = hidden
                .as_array()
                .ok_or("hiddenHomeQuotaProviders must be a list of provider IDs")?;
            let mut ids = Vec::new();
            for p in items {
                let p = p
                    .as_str()
                    .filter(|p| !p.trim().is_empty() && p.chars().count() <= 128)
                    .ok_or("hiddenHomeQuotaProviders must be a list of provider IDs")?
                    .trim();
                if !ids.contains(&p) {
                    ids.push(p);
                }
            }
            values.insert("hiddenHomeQuotaProviders".into(), json!(ids));
        }
        self.transaction(|s| { for (k,v) in values { s.db.execute("INSERT INTO preferences(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",params![k,v.to_string()])?; } s.preferences() })
    }
    pub fn save_draft(&self, device: &str, room: &str, text: &str) -> Result<Value> {
        self.save_draft_with_artifacts(device, room, text, None)
    }
    pub fn save_draft_with_artifacts(
        &self,
        device: &str,
        room: &str,
        text: &str,
        artifact_ids: Option<&[Value]>,
    ) -> Result<Value> {
        let device = require(device, "device", 128)?;
        let room = require(room, "room", 128)?;
        self.room(&room, true)?;
        self.transaction(|s| {
            let existing = s.rows("SELECT attachment_ids FROM drafts WHERE device_id=? AND room_id=?", params![device, room])?;
            let ids: Vec<String> = if let Some(requested) = artifact_ids {
                let mut ids = Vec::new();
                for id in requested {
                    let id = id.as_str().ok_or("artifact does not belong to the room")?;
                    if ids.iter().any(|previous| previous == id) { continue; }
                    s.one("SELECT id FROM artifacts WHERE id=? AND room_id=? AND mime LIKE 'image/%'", params![id, room], "artifact does not belong to the room")?;
                    ids.push(id.to_owned());
                }
                ids
            } else if let Some(old) = existing.first() {
                serde_json::from_str(crate::text(old, "attachment_ids"))?
            } else {
                Vec::new()
            };
            let updated = now();
            if text.trim().is_empty() && ids.is_empty() {
                s.db.execute("DELETE FROM drafts WHERE device_id=? AND room_id=?", params![device, room])?;
                return Ok(json!({"roomId":room,"text":"","attachmentIds":[],"attachments":[],"updatedAt":updated}));
            }
            s.db.execute("INSERT INTO drafts(device_id,room_id,text,attachment_ids,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(device_id,room_id) DO UPDATE SET text=excluded.text,attachment_ids=excluded.attachment_ids,updated_at=excluded.updated_at", params![device, room, truncate(text, 20000), json!(ids).to_string(), updated])?;
            let row = s.one("SELECT room_id,text,attachment_ids,updated_at FROM drafts WHERE device_id=? AND room_id=?", params![device, room], "draft does not exist")?;
            s.dto("drafts", &row)
        })
    }
    pub fn device_drafts(&self, device: &str) -> Result<Vec<Value>> {
        self.dtos(
            "drafts",
            "SELECT room_id,text,attachment_ids,updated_at FROM drafts WHERE device_id=? ORDER BY updated_at DESC",
            [require(device, "device", 128)?],
        )
    }
    pub fn live_events(&self, room: &str) -> Result<Vec<Value>> {
        self.room(room, true)?;
        self.dtos("room_events","SELECT * FROM room_events WHERE room_id=? AND json_extract(payload,'$.ephemeral')=1 ORDER BY created_at,sequence",[room])
    }
    pub fn events_after(&self, cursor: i64, limit: i64, room: Option<&str>) -> Result<Value> {
        let rows = self.dtos("room_events","SELECT * FROM room_events WHERE cursor>? AND (? IS NULL OR room_id=?) ORDER BY cursor LIMIT ?",params![cursor,room,room,limit.clamp(1,1000)])?;
        let min = self.scalar("SELECT COALESCE(MIN(cursor),0) FROM room_events", [])?;
        let max = self.scalar("SELECT COALESCE(MAX(cursor),0) FROM room_events", [])?;
        let next = rows
            .last()
            .and_then(|r| r["cursor"].as_i64())
            .unwrap_or(cursor.max(max));
        Ok(
            json!({"events":rows,"nextCursor":next,"gap":min != 0 && cursor < min-1,"earliestCursor":min,"latestCursor":max}),
        )
    }
    pub fn events_before(&self, room: &str, before: i64, limit: i64) -> Result<Value> {
        self.room(room, false)?;
        let mut rows = self.dtos(
            "room_events",
            "SELECT * FROM room_events WHERE room_id=? AND cursor<? ORDER BY cursor DESC LIMIT ?",
            params![room, before, limit.clamp(1, 40)],
        )?;
        rows.reverse();
        let min = self.scalar(
            "SELECT COALESCE(MIN(cursor),0) FROM room_events WHERE room_id=?",
            [room],
        )?;
        let earliest = rows
            .first()
            .and_then(|r| r["cursor"].as_i64())
            .unwrap_or(before);
        Ok(
            json!({"hasOlder":!rows.is_empty() && min<earliest,"events":rows,"earliestCursor":earliest}),
        )
    }
    pub fn snapshot(&self, room: Option<&str>, event_room: Option<&str>) -> Result<Value> {
        let rooms = self.dtos("rooms","SELECT * FROM rooms WHERE deleted_at IS NULL AND native_suppressed=0 AND source!='hermes-native-member' AND (? IS NULL OR id=?) ORDER BY updated_at DESC",params![room,room])?;
        let mut events = Vec::new();
        let mut executions = Vec::new();
        let mut artifacts = Vec::new();
        let mut page = json!({"roomId":event_room.unwrap_or(""),"earliestCursor":0,"hasOlder":false,"limit":40});
        for r in &rooms {
            let id = text(r, "id");
            let rows = if room.is_some() {
                self.dtos(
                    "room_events",
                    "SELECT * FROM room_events WHERE room_id=? ORDER BY sequence",
                    [id],
                )?
            } else if let Some(selected) = event_room {
                if id == selected {
                    let mut rows = self.dtos(
                        "room_events",
                        "SELECT * FROM room_events WHERE room_id=? ORDER BY cursor DESC LIMIT 40",
                        [id],
                    )?;
                    rows.reverse();
                    let total =
                        self.scalar("SELECT COUNT(*) FROM room_events WHERE room_id=?", [id])?;
                    page = json!({"roomId":id,"earliestCursor":rows.first().map(|r| r["cursor"].clone()).unwrap_or(json!(0)),"hasOlder":total>rows.len() as i64,"limit":40});
                    rows
                } else {
                    self.dtos("room_events","SELECT * FROM room_events WHERE room_id=? ORDER BY created_at DESC,sequence DESC LIMIT 1",[id])?
                }
            } else {
                self.dtos(
                    "room_events",
                    "SELECT * FROM room_events WHERE room_id=? ORDER BY cursor",
                    [id],
                )?
            };
            events.extend(rows);
            executions.extend(self.dtos(
                "executions",
                "SELECT * FROM executions WHERE room_id=? ORDER BY created_at",
                [id],
            )?);
            artifacts.extend(self.dtos(
                "artifacts",
                "SELECT * FROM artifacts WHERE room_id=? ORDER BY created_at",
                [id],
            )?);
        }
        if room.is_none() {
            events.sort_by_key(|v| v["cursor"].as_i64().unwrap_or(0));
        }
        for items in [&mut executions, &mut artifacts] {
            items.sort_by(|a, b| {
                a["createdAt"]
                    .as_f64()
                    .partial_cmp(&b["createdAt"].as_f64())
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
        }
        Ok(
            json!({"ok":true,"version":3,"groups":self.dtos("project_groups","SELECT * FROM project_groups ORDER BY name",[])?,
            "projects":self.dtos("projects","SELECT * FROM projects ORDER BY name",[])?,"rooms":rooms,
            "archivedRooms":self.dtos("rooms","SELECT * FROM rooms WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC",[])?,
            "executions":executions,"events":events,"artifacts":artifacts,
            "bots":self.dtos("bots","SELECT * FROM bots WHERE enabled=1 ORDER BY name",[])?,
            "peers":self.dtos("peers","SELECT * FROM peers ORDER BY name",[])?,
            "quotas":self.dtos("quota_windows","SELECT * FROM quota_windows ORDER BY provider,label",[])?,
            "quotaProviders":self.dtos("quota_providers","SELECT * FROM quota_providers ORDER BY provider",[])?,
            "preferences":self.preferences()?,"latestCursor":self.scalar("SELECT COALESCE(MAX(cursor),0) FROM room_events",[])?,"eventPage":page}),
        )
    }
    pub fn store_artifact(&self, metadata: &Value, body: &[u8]) -> Result<Value> {
        let project = text(metadata, "projectId");
        let room = text(metadata, "roomId");
        self.owned_room(project, room, true)?;
        if body.is_empty() || body.len() > MAX_ARTIFACT_BYTES {
            return Err("artifact size is invalid".into());
        }
        let mime = require(text(metadata, "mime"), "MIME type", 160)?.to_lowercase();
        if !mime.starts_with("image/") && !mime.starts_with("video/") {
            return Err("only image and video artifacts are supported".into());
        }
        let raw_name = require(text(metadata, "name"), "artifact name", 255)?;
        let name = Path::new(&raw_name)
            .file_name()
            .map(|v| v.to_string_lossy().into_owned())
            .unwrap_or_default();
        let id = ident("art");
        let destination = self.artifact_root.join(&id);
        let hash = digest(body);
        write_private(&destination, body)?;
        let result = self.transaction(|s| {
            let execution = metadata["executionId"].as_str(); let author = require(text(metadata,"author"),"artifact author",128)?;
            s.db.execute("INSERT INTO artifacts(id,project_id,room_id,execution_id,event_id,author,name,mime,size,sha256,path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",params![id,project,room,execution,metadata["eventId"].as_str(),author,name,mime,body.len(),hash,destination.to_string_lossy(),now()])?;
            s.append_event(project,room,execution,"artifact.available",&author,json!({"text":name,"artifactId":id,"name":name,"mime":mime,"size":body.len()}))?;
            s.dto("artifacts",&s.one("SELECT * FROM artifacts WHERE id=?",[&id],"artifact does not exist")?)
        });
        if result.is_err() {
            let _ = fs::remove_file(destination);
        }
        result
    }
    pub fn read_artifact(&self, row: &Value, first: u64, last: u64) -> Result<Vec<u8>> {
        let mut file = fs::File::open(text(row, "path"))?;
        file.seek(SeekFrom::Start(first))?;
        let mut body = Vec::new();
        file.take(last - first + 1).read_to_end(&mut body)?;
        Ok(body)
    }
}
fn camel(s: &str) -> String {
    let mut out = String::new();
    let mut upper = false;
    for c in s.chars() {
        if c == '_' {
            upper = true;
        } else if upper {
            out.extend(c.to_uppercase());
            upper = false;
        } else {
            out.push(c);
        }
    }
    out
}
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
