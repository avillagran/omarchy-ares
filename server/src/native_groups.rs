//! Read-only catalog of Desktop native groups, never an execution/session binding.
use crate::{
    digest, now,
    producers::{array, first},
    store::Store,
    text, Result,
};
use rusqlite::params;
use serde_json::{json, Value};

pub struct NativeSnapshot {
    authority: String,
    revision: i64,
    groups: Vec<Value>,
    deleted: serde_json::Map<String, Value>,
}
impl NativeSnapshot {
    pub fn from_profiles(authority: &str, profiles: &[Value]) -> Result<Self> {
        let profile = profiles
            .iter()
            .find(|p| p["name"] == "default")
            .ok_or("Missing default profile")?;
        let snapshot = &profile["ui_meta"]["hermes-bots-groups"];
        if snapshot["version"] != 3 {
            return Err("Unsupported native group snapshot".into());
        }
        let rooms = snapshot["rooms"]
            .as_object()
            .ok_or("Invalid native rooms")?;
        let deleted = snapshot
            .get("deleted")
            .cloned()
            .unwrap_or(json!({}))
            .as_object()
            .ok_or("Invalid tombstones")?
            .clone();
        let revision = profile["ui_meta_revisions"]["hermes-bots-groups"]
            .as_i64()
            .unwrap_or(0);
        if rooms.len() > 1024 || deleted.len() > 4096 || revision < 0 {
            return Err("Native snapshot exceeds bounds".into());
        }
        let mut groups = Vec::new();
        for (key, room) in rooms {
            let room_id = text(room, "roomId");
            if key.is_empty()
                || key.len() > 512
                || key.chars().any(char::is_control)
                || room_id.is_empty()
                || room_id.len() > 256
                || room_id.chars().any(char::is_control)
                || key.strip_prefix("id:").is_some_and(|id| id != room_id)
                || room["revision"].as_i64().is_none_or(|v| v < 0)
            {
                return Err("Invalid native group identity".into());
            }
            let members = room["members"].as_array().ok_or("Invalid native members")?;
            if members.len() > 256 {
                return Err("Too many native members".into());
            }
            let mut clean = Vec::new();
            for m in members {
                if !m.is_object() {
                    return Err("Invalid native member".into());
                }
                let connection = text(m, "connectionId");
                let profile = text(m, "name");
                if profile.is_empty()
                    || profile.len() > 256
                    || profile.chars().any(char::is_control)
                    || connection.is_empty()
                    || connection.len() > 256
                    || connection.chars().any(char::is_control)
                    || !matches!(text(m, "connectionKind"), "local" | "remote")
                {
                    return Err("Invalid native member identity".into());
                }
                let local =
                    connection == "local" && m["connectionKind"] == "local" && authority == "local";
                let member_key = if m["sourceScoped"] == true || m["remoteSource"] == true {
                    format!("{connection}::{profile}")
                } else {
                    profile.to_owned()
                };
                clean.push(json!({"memberKey":member_key,"host":if local {authority} else {""},"connectionId":connection,"connectionKind":text(m,"connectionKind"),"profile":profile,"storedSessionId":"","resolvedSessionId":"","resolution":if local {"missing"} else {"unsupported"}}));
            }
            groups.push(json!({"desktopKey":key,"desktopRoomId":text(room,"roomId"),"revision":room["revision"].as_i64().unwrap_or(0),"name":first(room,&["name"],key),"members":clean}));
        }
        Ok(Self {
            authority: authority.into(),
            revision,
            groups,
            deleted,
        })
    }
}
fn deletion_number(v: &Value) -> Option<f64> {
    crate::producers::number(v).filter(|n| *n >= 0.0)
}
pub(crate) fn deleted_at(key: &str, tombstone: &Value, revision: i64) -> bool {
    key.starts_with("id:") || deletion_number(tombstone).is_none_or(|n| n >= revision as f64)
}
pub async fn discover_native_groups(
    store: &crate::integration::SharedStore,
    bridge: &crate::broker::HermesBroker,
    authority: &str,
    profiles: &[Value],
) -> Result<()> {
    use futures_util::{stream, StreamExt};
    let mut snapshot = NativeSnapshot::from_profiles(authority, profiles)?;
    crate::integration::with_store(store, |s| s.validate_native_group_identities(&snapshot))?;
    let mut work = Vec::new();
    for (gi, g) in snapshot.groups.iter().enumerate() {
        let key = text(g, "desktopKey");
        if snapshot
            .deleted
            .get(key)
            .is_some_and(|t| deleted_at(key, t, g["revision"].as_i64().unwrap_or(0)))
        {
            continue;
        }
        for (mi, m) in array(&g["members"]).iter().enumerate() {
            if m["resolution"] == "missing"
                && !text(m, "profile").is_empty()
                && !text(g, "desktopRoomId").is_empty()
            {
                work.push((
                    gi,
                    mi,
                    text(m, "profile").to_owned(),
                    format!("Group: {}", text(g, "desktopRoomId")),
                ));
            }
        }
    }
    let results = stream::iter(work.into_iter().map(|(gi, mi, profile, title)| async move {
        let response = bridge
            .call("session.list", json!({"profile":profile,"title":title}), 5)
            .await;
        (gi, mi, title, response)
    }))
    .buffer_unordered(4)
    .collect::<Vec<_>>()
    .await;
    for (gi, mi, title, response) in results {
        let m = &mut snapshot.groups[gi]["members"][mi];
        if response["ok"] != true || !response["result"]["sessions"].is_array() {
            m["resolution"] = json!("error");
            continue;
        }
        let sessions = array(&response["result"]["sessions"]);
        let exact = sessions
            .iter()
            .filter(|s| s["title"] == title && !text(s, "id").is_empty())
            .collect::<Vec<_>>();
        m["resolution"] = json!(match exact.len() {
            0 => "missing",
            1 => "resolved",
            _ => "ambiguous",
        });
        if exact.len() == 1 {
            m["storedSessionId"] = exact[0]["id"].clone();
            m["resolvedSessionId"] = json!(first(exact[0], &["resolved_id", "id"], ""));
        }
    }
    crate::integration::with_store(store, |s| s.sync_native_groups(&snapshot))
}
impl Store {
    fn validate_native_group_identities(&self, snapshot: &NativeSnapshot) -> Result<()> {
        for group in &snapshot.groups {
            let old = self.rows("SELECT desktop_room_id FROM native_groups WHERE authority_host=? AND desktop_key=?",params![snapshot.authority,text(group,"desktopKey")])?;
            if old.first().is_some_and(|old| {
                !text(old, "desktop_room_id").is_empty()
                    && old["desktop_room_id"] != group["desktopRoomId"]
            }) {
                return Err("Native group identity cannot change".into());
            }
        }
        Ok(())
    }
    pub fn sync_native_groups(&self, snapshot: &NativeSnapshot) -> Result<()> {
        self.transaction(|s| {
            s.validate_native_group_identities(snapshot)?;
            let authority=&snapshot.authority;
            let latest=s.one("SELECT COALESCE(MAX(snapshot_revision),0) AS revision FROM native_group_authorities WHERE authority_host=?",[authority],"Missing revision")?["revision"].as_i64().unwrap_or(0);
            if snapshot.revision < latest { return Ok(()); }
            s.db.execute("INSERT INTO native_group_authorities(authority_host,snapshot_revision) VALUES(?,?) ON CONFLICT(authority_host) DO UPDATE SET snapshot_revision=excluded.snapshot_revision",params![authority,snapshot.revision])?;
            for (key,tombstone) in &snapshot.deleted {
                let old=s.rows("SELECT * FROM native_groups WHERE authority_host=? AND desktop_key=?",params![authority,key])?;
                // Retain the strongest tombstone, including corrupt values (fail closed).
                let previous=old.first().and_then(|r|r["tombstone"].as_str()).and_then(|v|serde_json::from_str::<Value>(v).ok());
                let chosen=match previous { Some(v) if deletion_number(&v).is_none() || (deletion_number(tombstone).is_some() && deletion_number(&v) > deletion_number(tombstone)) => v, _ => tombstone.clone() };
                s.db.execute("INSERT INTO native_groups(authority_host,desktop_key,snapshot_revision,tombstone) VALUES(?,?,?,?) ON CONFLICT(authority_host,desktop_key) DO UPDATE SET tombstone=excluded.tombstone,snapshot_revision=excluded.snapshot_revision",params![authority,key,snapshot.revision,chosen.to_string()])?;
                if let Some(old)=old.first() {
                    if deleted_at(key,&chosen,old["revision"].as_i64().unwrap_or(0)) {
                        s.db.execute("UPDATE rooms SET native_suppressed=1 WHERE id=?",params![text(old,"room_id")])?;
                    }
                }
            }
            let path="hermes://native-groups";
            let project=format!("prj_native_{}", &digest(authority)[..32]);
            s.db.execute("INSERT OR IGNORE INTO projects(id,name,node_id,path,location_key,created_at) VALUES(?,'Native groups',?,?,?,?)",params![project,authority,path,digest(format!("{authority}\0{path}")),now()])?;
            for group in &snapshot.groups {
                let key=text(group,"desktopKey");
                let revision=group["revision"].as_i64().unwrap_or(0);
                let old=s.rows("SELECT * FROM native_groups WHERE authority_host=? AND desktop_key=?",params![authority,key])?;
                if let Some(old)=old.first() {
                    if revision < old["revision"].as_i64().unwrap_or(0) { continue; }
                    if let Some(t)=old["tombstone"].as_str() {
                        if deleted_at(key,&serde_json::from_str::<Value>(t).unwrap_or(Value::Null),revision) { continue; }
                    }
                }
                let room=format!("room_native_{}", &digest(format!("{authority}\0{key}"))[..32]);
                s.db.execute("INSERT INTO rooms(id,project_id,name,status,memory_release,source,control_included,created_at,updated_at) VALUES(?,?,?,'active','not-requested','hermes-native-group',0,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,native_suppressed=0",params![room,project,text(group,"name"),now(),now()])?;
                s.db.execute("INSERT INTO native_groups(authority_host,desktop_key,room_id,desktop_room_id,revision,snapshot_revision) VALUES(?,?,?,?,?,?) ON CONFLICT(authority_host,desktop_key) DO UPDATE SET room_id=excluded.room_id,desktop_room_id=excluded.desktop_room_id,revision=excluded.revision,snapshot_revision=excluded.snapshot_revision",params![authority,key,room,text(group,"desktopRoomId"),group["revision"].as_i64().unwrap_or(0),snapshot.revision])?;
                let previous_members=s.rows("SELECT * FROM native_group_members WHERE room_id=?",[&room])?;
                s.db.execute("DELETE FROM native_group_members WHERE room_id=?",[&room])?;
                for (position,m) in array(&group["members"]).iter().enumerate() {
                    let mut m=m.clone();
                    if m["resolution"]=="error" {
                        if let Some(old)=previous_members.iter().find(|old|old["member_key"]==m["memberKey"] && old["host"]==m["host"] && old["profile"]==m["profile"] && old["connection_id"]==m["connectionId"] && old["connection_kind"]==m["connectionKind"]) {
                            m["storedSessionId"]=old["stored_session_id"].clone();
                            m["resolvedSessionId"]=old["resolved_session_id"].clone();
                        }
                    }
                    let m=&m;
                    if !text(m,"host").is_empty() && !text(m,"storedSessionId").is_empty() {
                        s.db.execute("UPDATE rooms SET source='hermes-native-member',control_included=0 WHERE source='hermes' AND external_profile=? AND external_session_id<>'' AND (external_session_id=? OR external_session_id=?) AND project_id IN (SELECT id FROM projects WHERE node_id=?)",params![text(m,"profile"),text(m,"storedSessionId"),text(m,"resolvedSessionId"),text(m,"host")])?;
                    }
                    s.db.execute("INSERT INTO native_group_members(room_id,position,member_key,host,connection_id,connection_kind,profile,stored_session_id,resolved_session_id,resolution) VALUES(?,?,?,?,?,?,?,?,?,?)",params![room,position as i64,text(m,"memberKey"),text(m,"host"),text(m,"connectionId"),text(m,"connectionKind"),text(m,"profile"),text(m,"storedSessionId"),text(m,"resolvedSessionId"),text(m,"resolution")])?;
                }
            }
            Ok(())
        })
    }
    pub fn native_group_dto(&self, room: &str) -> Result<Value> {
        let g = self.one(
            "SELECT * FROM native_groups WHERE room_id=?",
            [room],
            "Native group missing",
        )?;
        let members=self.rows("SELECT * FROM native_group_members WHERE room_id=? ORDER BY position",[room])?.iter().map(|m|json!({"memberKey":m["member_key"],"host":m["host"],"connectionId":m["connection_id"],"connectionKind":m["connection_kind"],"profile":m["profile"],"storedSessionId":m["stored_session_id"],"resolvedSessionId":m["resolved_session_id"],"resolution":m["resolution"]})).collect::<Vec<_>>();
        Ok(
            json!({"authorityHost":g["authority_host"],"desktopKey":g["desktop_key"],"desktopRoomId":g["desktop_room_id"],"revision":g["revision"],"members":members}),
        )
    }
}

fn history_field(v: &Value, max: usize, optional: bool) -> Result<&str> {
    let value = if optional && v.is_null() {
        ""
    } else {
        v.as_str().ok_or("Invalid native group metadata")?
    };
    if (!optional && value.is_empty()) || value.len() > max || value.chars().any(char::is_control) {
        return Err("Invalid native group metadata".into());
    }
    Ok(value)
}

impl Store {
    /// Project only the currently authenticated Desktop snapshot. Nothing enters SQLite.
    pub fn native_group_history(
        &self,
        room: &str,
        profiles: &[Value],
        before: i64,
    ) -> Result<Value> {
        if before <= 0 {
            return Err("Invalid group history cursor".into());
        }
        let group = self.one("SELECT g.*,r.source,r.native_suppressed,r.deleted_at FROM native_groups g JOIN rooms r ON r.id=g.room_id WHERE g.room_id=?", [room], "Native group unavailable")?;
        if group["source"] != "hermes-native-group"
            || group["native_suppressed"] != 0
            || !group["deleted_at"].is_null()
            || text(&group, "authority_host") != "local"
        {
            return Err("Native group unavailable".into());
        }
        let snapshot = NativeSnapshot::from_profiles("local", profiles)?;
        if snapshot.revision < group["snapshot_revision"].as_i64().unwrap_or(0) {
            return Err("Native group snapshot is stale".into());
        }
        let key = text(&group, "desktop_key");
        let source = snapshot
            .groups
            .iter()
            .find(|g| {
                text(g, "desktopKey") == key
                    && g["desktopRoomId"] == group["desktop_room_id"]
                    && g["revision"].as_i64().unwrap_or(-1)
                        >= group["revision"].as_i64().unwrap_or(0)
            })
            .ok_or("Native group no longer available at source")?;
        let revision = source["revision"].as_i64().unwrap_or(0);
        if snapshot
            .deleted
            .get(key)
            .is_some_and(|t| deleted_at(key, t, revision))
            || group["tombstone"].as_str().is_some_and(|t| {
                deleted_at(
                    key,
                    &serde_json::from_str::<Value>(t).unwrap_or(Value::Null),
                    revision,
                )
            })
        {
            return Err("Native group deleted".into());
        }
        let default = profiles
            .iter()
            .find(|p| p["name"] == "default")
            .ok_or("Default profile unavailable")?;
        let log = default["ui_meta"]["hermes-bots-groups"]["rooms"][key]["log"]
            .as_array()
            .ok_or("Native group log unavailable")?;
        if log.len() > 10000 {
            return Err("Native group log exceeds limit".into());
        }
        // Keep the cursor within JavaScript's exact integer range. The high
        // 39 bits fingerprint this immutable source view; the low 14 are the
        // original 1-based position (at most 10,000 events).
        let fingerprint =
            i64::from_str_radix(&digest(serde_json::to_vec(log)?)[..10], 16)? & ((1 << 39) - 1);
        let cursor = |index: i64| (fingerprint << 14) | index;
        let bound = if before == i64::MAX {
            i64::try_from(log.len())? + 1
        } else {
            let index = before & ((1 << 14) - 1);
            if index < 1 || index > i64::try_from(log.len())? + 1 || before != cursor(index) {
                return Err("Native group history snapshot changed".into());
            }
            index
        };
        let mut page = Vec::new();
        let mut page_bytes = 0usize;
        for (index, event) in log.iter().enumerate().rev() {
            let seq = i64::try_from(index + 1)?;
            if seq >= bound {
                continue;
            }
            let actor = &event["from"];
            let role = match text(actor, "kind") {
                "member" => "assistant",
                "user" => "user",
                _ => continue,
            };
            let id = history_field(&event["id"], 256, false)?;
            let thread = history_field(&event["thread"], 256, true)?;
            let source_id = history_field(&actor["source"], 256, true)?;
            let body = event["text"]
                .as_str()
                .ok_or("Invalid native group message")?;
            if body.len() > 16384 {
                return Err("Native group message exceeds limit".into());
            }
            let name = if role == "user" {
                "user"
            } else {
                text(actor, "name")
            };
            if role == "assistant" {
                history_field(&actor["name"], 256, false)?;
                // The original local Desktop writer omits `from.source`.
                // Only a unique local member can supply that legacy identity.
                let matches = array(&source["members"])
                    .iter()
                    .filter(|m| {
                        text(m, "profile") == name
                            && if source_id.is_empty() {
                                text(m, "connectionId") == "local" && m["connectionKind"] == "local"
                            } else {
                                text(m, "connectionId") == source_id
                            }
                    })
                    .count();
                if matches != 1 {
                    return Err("Native group author is not a unique member".into());
                }
            }
            let message = json!({"seq":seq,"id":id,"role":role,"author":name,
                "text":body,"thread":thread,"time":event["at"],"source":source_id});
            let size = serde_json::to_vec(&message)?.len();
            if page_bytes + size > 128 * 1024 {
                if page.is_empty() {
                    return Err("Native group page exceeds limit".into());
                }
                break;
            }
            page_bytes += size;
            page.push(message);
            if page.len() == 200 {
                break;
            }
        }
        page.reverse();
        let oldest = page.first().and_then(|m| m["seq"].as_i64()).unwrap_or(0);
        let latest = page.last().and_then(|m| m["seq"].as_i64()).unwrap_or(0);
        let has_older = oldest > 1
            && log
                .iter()
                .take((oldest - 1) as usize)
                .any(|e| matches!(text(&e["from"], "kind"), "member" | "user"));
        let response = json!({"roomId":room,"readOnly":true,"messages":page,
            "oldestSeq":if oldest == 0 { 0 } else { cursor(oldest) },
            "latestSeq":if latest == 0 { 0 } else { cursor(latest) },
            "hasOlder":has_older,"members":source["members"]});
        if serde_json::to_vec(&response)?.len() > 512 * 1024 {
            return Err("Native group page exceeds limit".into());
        }
        Ok(response)
    }
}
