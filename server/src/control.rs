use crate::{now, require, text, truncate, Result};
use rusqlite::params;
use serde_json::{json, Value};

use crate::store::Store;

impl Store {
    /// Create on first use, so existing databases and snapshots remain unchanged until opened.
    pub fn ensure_control_room(&self) -> Result<Value> {
        let project =
            self.create_project("Control Room", "ares://control-room", "control", None)?;
        let project_id = text(&project, "id");
        if let Some(room) = self.rows(
            "SELECT * FROM rooms WHERE project_id=? AND source='control' AND deleted_at IS NULL LIMIT 1",
            [project_id],
        )?.first() {
            return self.dto("rooms", room);
        }
        let room = self.create_room(project_id, "Control Room", &[json!("default")])?;
        self.db.execute(
            "UPDATE rooms SET source='control',control_included=0 WHERE id=?",
            [text(&room, "id")],
        )?;
        self.dto("rooms", &self.room(text(&room, "id"), false)?)
    }

    pub fn set_control_included(&self, room_id: &str, included: bool) -> Result<Value> {
        let room = self.room(room_id, false)?;
        if matches!(
            text(&room, "source"),
            "hermes-native-group" | "hermes-native-member"
        ) {
            return Err("Native group discovery is not a single Control Room agent".into());
        }
        if text(&room, "source") == "control" {
            return Err("Control Room cannot be included in its own feed".into());
        }
        self.db.execute(
            "UPDATE rooms SET control_included=?,revision=revision+1 WHERE id=?",
            params![included, room_id],
        )?;
        self.dto("rooms", &self.room(room_id, false)?)
    }

    /// This is an audit entry, not an agent prompt or an approval response.
    pub fn record_control_decision(
        &self,
        room_id: &str,
        action: &str,
        note: &str,
    ) -> Result<Value> {
        if !["propose-prompt", "ask-user", "defer", "review-option"].contains(&action) {
            return Err("unsupported Control Room action".into());
        }
        let room = self.room(room_id, false)?;
        if text(&room, "source") == "control" || room["control_included"] != 1 {
            return Err("room is not included in Control Room".into());
        }
        let note = require(note, "decision note", 5000)?;
        let control = self.ensure_control_room()?;
        self.transaction(|s| {
            s.append_event(
                text(&control, "projectId"),
                text(&control, "id"),
                None,
                "control.decision",
                "user",
                json!({"text":note,"roomId":room_id,"action":action,"createdAt":now()}),
            )?;
            s.dto(
                "room_events",
                &s.one(
                    "SELECT * FROM room_events WHERE room_id=? ORDER BY sequence DESC LIMIT 1",
                    [text(&control, "id")],
                    "decision was not recorded",
                )?,
            )
        })
    }

    /// A bounded read-only inbox. No item may send a prompt or resolve an approval.
    pub fn control_items(&self) -> Result<Vec<Value>> {
        let mut items = Vec::new();
        for row in self.rows(
            "SELECT e.id,e.room_id,e.status,e.prompt,e.created_at,r.name room_name,r.source,p.name project_name \
             FROM executions e JOIN rooms r ON r.id=e.room_id JOIN projects p ON p.id=r.project_id \
             WHERE e.status IN ('queued','running') AND r.status='active' AND r.deleted_at IS NULL \
             AND r.control_included=1 AND r.source!='control' ORDER BY e.created_at DESC LIMIT 100", [],
        )? {
            items.push(json!({
                "id":format!("exec:{}",text(&row,"id")),"roomId":row["room_id"],
                "roomName":row["room_name"],"projectName":row["project_name"],
                "kind":"execution","status":row["status"],
                "title":truncate(text(&row,"prompt"),240),"source":row["source"],
                "createdAt":row["created_at"]
            }));
        }
        for row in self.rows(
            "SELECT ev.id,ev.room_id,ev.execution_id,ev.kind,ev.payload,ev.created_at,ev.cursor, \
             r.name room_name,r.source,p.name project_name \
             FROM room_events ev JOIN rooms r ON r.id=ev.room_id JOIN projects p ON p.id=r.project_id \
             WHERE ev.kind IN ('approval.request','clarify.request') AND r.status='active' \
             AND r.deleted_at IS NULL AND r.control_included=1 AND r.source!='control' \
             ORDER BY ev.cursor DESC LIMIT 100", [],
        )? {
            let kind = text(&row, "kind");
            let family = kind.split('.').next().unwrap_or("");
            let payload: Value = serde_json::from_str(text(&row, "payload"))?;
            let request_id = payload["request_id"].as_str().or(payload["requestId"].as_str()).unwrap_or("");
            // Never re-surface a decision already recorded by its owner.
            let resolved = self.rows(
                "SELECT id FROM room_events WHERE room_id=? AND cursor>? AND kind=? \
                 AND (json_extract(payload,'$.request_id')=? OR json_extract(payload,'$.requestId')=?) LIMIT 1",
                params![text(&row,"room_id"),row["cursor"].as_i64().unwrap_or(0),format!("{family}.responded"),request_id,request_id],
            )?;
            if !resolved.is_empty() { continue; }
            if let Some(exec_id) = row["execution_id"].as_str() {
                let active = self.rows("SELECT id FROM executions WHERE id=? AND status IN ('queued','running')", [exec_id])?;
                if active.is_empty() { continue; }
            }
            items.push(json!({
                "id":format!("event:{}",text(&row,"id")),"roomId":row["room_id"],
                "roomName":row["room_name"],"projectName":row["project_name"],
                "kind":kind,"status":"waiting","source":row["source"],
                "title":truncate(payload["text"].as_str().or(payload["prompt"].as_str()).unwrap_or(kind),240),
                "createdAt":row["created_at"]
            }));
        }
        items.sort_by(|a, b| {
            b["createdAt"]
                .as_f64()
                .partial_cmp(&a["createdAt"].as_f64())
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        items.truncate(100);
        Ok(items)
    }

    /// Export audited human decisions as typed examples. This remains local data:
    /// callers must explicitly review it before using it for Laya fine-tuning.
    pub fn control_training_examples(&self) -> Result<Vec<Value>> {
        let control = self.ensure_control_room()?;
        let mut examples = Vec::new();
        for row in self.rows(
            "SELECT e.payload,e.created_at,r.name room_name,r.source,p.name project_name \
             FROM room_events e \
             JOIN rooms r ON r.id=json_extract(e.payload,'$.roomId') \
             JOIN projects p ON p.id=r.project_id \
             WHERE e.room_id=? AND e.kind='control.decision' \
             ORDER BY e.cursor ASC",
            [text(&control, "id")],
        )? {
            let payload: Value = serde_json::from_str(text(&row, "payload"))?;
            let room_id = text(&payload, "roomId");
            let action = text(&payload, "action");
            let note = text(&payload, "text");
            examples.push(json!({
                "schema":"ares-laya-decision-v1",
                "state":{
                    "room_id":room_id,
                    "room_name":row["room_name"],
                    "project_name":row["project_name"],
                    "source":row["source"],
                    "decision_note":note,
                    "created_at":row["created_at"]
                },
                "questions":{
                    "recommended_action":{
                        "type":"choice",
                        "instructions":"Choose the safest next Control Room action. This recommendation must not execute work or send a prompt.",
                        "criteria":{
                            "propose-prompt":"draft a prompt for review only",
                            "ask-user":"request missing user input",
                            "defer":"leave the item pending",
                            "review-option":"record an option for later review"
                        }
                    }
                },
                "labels":{"action":action,"approved":false},
                "review":{"status":"unreviewed","warning":"Human review is required before fine-tuning or applying this example."}
            }));
        }
        Ok(examples)
    }
    pub fn control_agent_items(&self, agents: &[Value]) -> Result<Vec<Value>> {
        let mut items = Vec::new();
        let mut seen = std::collections::HashSet::new();
        for agent in agents {
            if text(agent, "agent") != "hermes"
                || text(&agent["agent_session"], "agent") != "hermes"
                || text(&agent["agent_session"], "kind") != "id"
                || text(&agent["agent_session"], "source") != "herdr:hermes"
            {
                continue;
            }
            let status = text(agent, "agent_status");
            if !["working", "blocked", "waiting", "needs_input"].contains(&status) {
                continue;
            }
            let session = text(&agent["agent_session"], "value");
            if session.is_empty() {
                continue;
            }
            let rooms = self.rows(
                "SELECT r.id,r.name,r.source,r.updated_at,p.name project_name FROM rooms r \
                 JOIN projects p ON p.id=r.project_id WHERE r.external_session_id=? \
                 AND r.source='hermes' AND r.external_profile NOT LIKE 'remote::%' \
                 AND r.deleted_at IS NULL AND r.status='active' AND r.control_included=1 LIMIT 2",
                [session],
            )?;
            // A duplicated mapping is ambiguous; never attribute work to the wrong chat.
            if rooms.len() != 1 {
                continue;
            }
            let room = &rooms[0];
            if !seen.insert(text(room, "id").to_owned()) {
                continue;
            }
            items.push(json!({
                "id":format!("agent:{}",text(room,"id")), "roomId":room["id"],
                "roomName":room["name"],"projectName":room["project_name"],
                "kind":"agent","status":status,"title":if status=="working" {"Agent is working"} else {"Agent needs attention"},
                "source":room["source"],"createdAt":room["updated_at"]
            }));
        }
        Ok(items)
    }

    pub fn control_remote_peers(&self) -> Result<Vec<String>> {
        let mut peers = std::collections::BTreeSet::new();
        for row in self.rows(
            "SELECT DISTINCT external_profile FROM rooms WHERE source='hermes' \
             AND external_profile LIKE 'remote::%' AND status='active' \
             AND deleted_at IS NULL AND control_included=1 LIMIT 64",
            [],
        )? {
            if let Some((peer, _)) = text(&row, "external_profile")
                .strip_prefix("remote::")
                .and_then(|value| value.split_once("::"))
            {
                peers.insert(peer.to_owned());
            }
        }
        Ok(peers.into_iter().collect())
    }

    /// Join observed remote delegation units to one included room by full peer/profile/session identity.
    pub fn control_remote_items(&self, reports: &[Value]) -> Result<Vec<Value>> {
        let mut items = Vec::new();
        let mut seen = std::collections::HashSet::new();
        for report in reports {
            let peer = text(report, "peer");
            let profile = text(report, "profile");
            let session = text(report, "sessionId");
            let units = report["runningDelegations"].as_u64().unwrap_or(0);
            let reference = format!("remote::{peer}::{profile}::{session}");
            if units == 0
                || units > 100
                || crate::remote::parse_remote_session(&reference, peer, profile) != Some(session)
            {
                continue;
            }
            let rooms = self.rows(
                "SELECT r.id,r.name,r.source,r.updated_at,p.name project_name FROM rooms r \
                 JOIN projects p ON p.id=r.project_id WHERE r.external_profile=? AND r.external_session_id=? \
                 AND r.source='hermes' AND r.deleted_at IS NULL AND r.status='active' \
                 AND r.control_included=1 LIMIT 2",
                params![format!("remote::{peer}::{profile}"), reference],
            )?;
            if rooms.len() != 1 {
                continue;
            }
            let room = &rooms[0];
            if !seen.insert(text(room, "id").to_owned()) {
                continue;
            }
            items.push(json!({
                "id":format!("delegation:{}",text(room,"id")),"roomId":room["id"],
                "roomName":room["name"],"projectName":room["project_name"],
                "kind":"delegation","status":"working","delegationUnits":units,
                "title":"Subagent delegation in progress","source":room["source"],
                "createdAt":room["updated_at"]
            }));
        }
        Ok(items)
    }

    /// Recent display-only activity for included rooms. User prompts and reasoning stay out.
    pub fn control_logs(&self, room_ids: &[&str]) -> Result<Value> {
        let mut logs = serde_json::Map::new();
        for room_id in room_ids.iter().take(12) {
            if logs.contains_key(*room_id) {
                continue;
            }
            let visible = self.rows(
                "SELECT id FROM rooms WHERE id=? AND status='active' AND deleted_at IS NULL \
                 AND control_included=1 AND source!='control' LIMIT 1",
                [room_id],
            )?;
            if visible.is_empty() {
                continue;
            }
            let mut lines = Vec::new();
            for row in self.rows(
                "SELECT kind,payload FROM room_events WHERE room_id=? AND created_at>=? \
                 AND kind IN ('tool.result','tool.complete','message.assistant') \
                 ORDER BY cursor DESC LIMIT 12",
                params![room_id, now() - 900.0],
            )? {
                let payload: Value = serde_json::from_str(text(&row, "payload"))?;
                let kind = text(&row, "kind");
                let raw = if kind == "tool.complete" {
                    payload["summary"].as_str().or(payload["name"].as_str())
                } else {
                    payload["summary"]
                        .as_str()
                        .or(payload["text"].as_str())
                        .or(payload["output"].as_str())
                };
                let Some(tail) = raw.and_then(|s| {
                    s.lines().rev().find(|line| {
                        let line = line.trim();
                        !line.is_empty() && !line.chars().all(|c| "{}[]),".contains(c))
                    })
                }) else {
                    continue;
                };
                let clean: String = tail.chars().filter(|c| !c.is_control()).take(140).collect();
                if clean.trim().is_empty() {
                    continue;
                }
                lines.push(json!({"kind":kind,"text":clean.trim()}));
                if lines.len() == 2 {
                    break;
                }
            }
            logs.insert((*room_id).to_owned(), json!(lines));
        }
        Ok(Value::Object(logs))
    }
}
