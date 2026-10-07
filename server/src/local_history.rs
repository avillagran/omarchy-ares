//! Bounded, owner-local, read-only SQLite history for an exact Hermes room.
use crate::{store::Store, terminals, text, Result};
use regex::Regex;
use rusqlite::{params, Connection, OpenFlags};
use serde_json::{json, Value};
use std::sync::OnceLock;
use std::{
    collections::HashSet,
    fs,
    path::PathBuf,
    sync::{Arc, Mutex},
};

fn private_preview() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"(?im)(?:authorization\s*:\s*bearer|\b(?:api[_-]?key|access[_-]?token|secret|password|pairing\s+code)\s*[:=]|\b(?:pairing|verification)\s+code\s*:\s*\d{6,8}\b|^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\s+(?:INFO|DEBUG|WARN|ERROR)\b)").expect("constant preview pattern"))
}

#[derive(PartialEq)]
struct Identity {
    room: Value,
    profile: String,
    session: String,
    source: String,
    path: PathBuf,
}
fn safe(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
fn identity(store: &Arc<Mutex<Store>>, id: &str) -> Result<Identity> {
    let guard = store.lock().map_err(|_| "History unavailable")?;
    let room = guard.room(id, false)?;
    let profile = text(&room, "external_profile");
    let session = text(&room, "external_session_id");
    if text(&room, "source") != "hermes"
        || text(&room, "status") != "active"
        || room["external_archived"] != 0
        || !safe(profile)
        || profile.len() > 64
        || !safe(session)
        || terminals::room_host(&guard, id)? != "local"
    {
        return Err("History unavailable".into());
    }
    let home = fs::canonicalize(&guard.hermes_home)?;
    let candidate = if profile == "default" {
        home.join("state.db")
    } else {
        home.join("profiles").join(profile).join("state.db")
    };
    let path = fs::canonicalize(candidate)?;
    if !path.starts_with(&home) || !fs::metadata(&path)?.is_file() {
        return Err("History unavailable".into());
    }
    let source = source_identity(&path, profile, session)?;
    let profile = profile.to_owned();
    let session = session.to_owned();
    Ok(Identity {
        room,
        profile,
        session,
        source,
        path,
    })
}
fn connection(path: &PathBuf) -> Result<Connection> {
    let db = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )?;
    db.execute_batch("PRAGMA query_only=ON; PRAGMA busy_timeout=1000;")?;
    Ok(db)
}
fn source_identity(path: &PathBuf, profile: &str, session: &str) -> Result<String> {
    let db = connection(path)?;
    let cols = columns(&db, "sessions")?;
    if !["id", "source", "hidden", "archived", "profile_name"]
        .iter()
        .all(|c| cols.contains(*c))
    {
        return Err("History unavailable".into());
    }
    let row: (String,Option<String>)=db.query_row("SELECT source,profile_name FROM sessions WHERE id=? AND COALESCE(hidden,0)=0 AND COALESCE(archived,0)=0",[session],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if row.0.is_empty()
        || row.0.len() > 64
        || row
            .1
            .as_deref()
            .is_some_and(|name| !name.is_empty() && name != profile)
    {
        return Err("History unavailable".into());
    }
    Ok(row.0)
}
fn columns(db: &Connection, table: &str) -> Result<HashSet<String>> {
    Ok(db
        .prepare(&format!("PRAGMA table_info({table})"))?
        .query_map([], |r| r.get(1))?
        .collect::<rusqlite::Result<_>>()?)
}
fn page(identity: &Identity, before: Option<i64>) -> Result<Value> {
    let db = connection(&identity.path)?;
    db.execute_batch("BEGIN")?;
    let source = source_identity_in(&db, &identity.profile, &identity.session)?;
    if source != identity.source {
        return Err("History unavailable".into());
    }
    let cols = columns(&db, "messages")?;
    if ![
        "id",
        "session_id",
        "role",
        "content",
        "display_order",
        "display_identity",
        "active",
        "compacted",
        "display_kind",
    ]
    .iter()
    .all(|c| cols.contains(*c))
    {
        return Err("History unavailable".into());
    }
    let index = db
        .prepare("PRAGMA index_list(messages)")?
        .query_map([], |r| r.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    if !index.iter().any(|i| i == "idx_messages_display_page") {
        return Err("History unavailable".into());
    }
    let missing:i64=db.query_row("SELECT EXISTS(SELECT 1 FROM messages INDEXED BY idx_messages_display_page WHERE session_id=? AND (active=1 OR compacted=1) AND (display_order IS NULL OR display_identity IS NULL) LIMIT 1)",[&identity.session],|r|r.get(0))?;
    if missing != 0 {
        return Err("History unavailable".into());
    }
    let visible = |alias: &str| {
        let mut parts = vec![
            format!("{alias}.role IN ('user','assistant')"),
            format!("typeof({alias}.content)='text'"),
            format!("length({alias}.content)>0"),
            format!("({alias}.active=1 OR {alias}.compacted=1)"),
            format!(
                "({alias}.display_kind IS NULL OR {alias}.display_kind IN ('','message','text'))"
            ),
        ];
        for col in [
            "tool_calls",
            "tool_call_id",
            "tool_name",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "codex_reasoning_items",
        ] {
            if cols.contains(col) {
                parts.push(format!("({alias}.{col} IS NULL OR {alias}.{col}='')"))
            }
        }
        if cols.contains("_compressed_summary") {
            parts.push(format!("COALESCE({alias}._compressed_summary,0)=0"))
        }
        if cols.contains("display_metadata") {
            parts.push(format!("({alias}.display_metadata IS NULL OR {alias}.display_metadata='' OR (json_valid({alias}.display_metadata) AND COALESCE(json_extract(CASE WHEN json_valid({alias}.display_metadata) THEN {alias}.display_metadata ELSE '{{}}' END,'$.model_only'),0)=0))"))
        }
        parts.join(" AND ")
    };
    let sql=format!("SELECT m.display_order,(SELECT c.id FROM messages c INDEXED BY idx_messages_display_page WHERE c.session_id=m.session_id AND c.display_order=m.display_order AND {} ORDER BY c.active DESC,c.id DESC LIMIT 1) FROM messages m INDEXED BY idx_messages_display_page WHERE m.session_id=? AND (m.active=1 OR m.compacted=1) AND m.display_order {} ? AND {} GROUP BY m.display_order ORDER BY m.display_order DESC LIMIT 9",visible("c"),if before.is_some(){"<"}else{"<="},visible("m"));
    let mut stmt = db.prepare(&sql)?;
    let keys: Vec<(i64, i64)> = stmt
        .query_map(params![identity.session, before.unwrap_or(i64::MAX)], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })?
        .collect::<rusqlite::Result<_>>()?;
    let mut messages = Vec::new();
    for (cursor, row_id) in keys.iter().take(8).rev() {
        if *cursor <= 0 {
            return Err("History unavailable".into());
        }
        let (role, body): (String, String) = db.query_row(
            "SELECT role,substr(content,1,500) FROM messages WHERE id=? AND session_id=?",
            params![row_id, identity.session],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        // Bound JSON escaping as well as Unicode characters (8 rows fit the private transport).
        let text = if private_preview().is_match(&body) {
            "[redacted]".to_owned()
        } else {
            let mut escaped = 0;
            body.chars()
                .take_while(|c| {
                    escaped += c.escape_default().count();
                    escaped <= 1700
                })
                .collect::<String>()
        };
        if text.is_empty() {
            return Err("History unavailable".into());
        }
        messages.push(json!({"role":role,"text":text,"cursor":cursor.to_string()}));
    }
    let more = keys.len() > 8;
    let next = if more {
        messages
            .first()
            .and_then(|m| m["cursor"].as_str())
            .map(str::to_owned)
    } else {
        None
    };
    Ok(json!({"messages":messages,"next_before":next,"has_more":more}))
}
fn source_identity_in(db: &Connection, profile: &str, session: &str) -> Result<String> {
    let cols = columns(db, "sessions")?;
    if !["id", "source", "profile_name", "hidden", "archived"]
        .iter()
        .all(|c| cols.contains(*c))
    {
        return Err("History unavailable".into());
    }
    let (source,name):(String,Option<String>)=db.query_row("SELECT source,profile_name FROM sessions WHERE id=? AND COALESCE(hidden,0)=0 AND COALESCE(archived,0)=0",[session],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if source.is_empty()
        || source.len() > 64
        || name
            .as_deref()
            .is_some_and(|n| !n.is_empty() && n != profile)
    {
        return Err("History unavailable".into());
    }
    Ok(source)
}
pub(super) fn read(store: &Arc<Mutex<Store>>, id: &str, before: Option<&str>) -> Result<Value> {
    let cursor = before.map(|s| s.parse::<i64>()).transpose()?;
    let first = identity(store, id)?;
    let result = page(&first, cursor)?;
    // A fresh read catches visibility and transcript changes since the first snapshot.
    if identity(store, id)? != first || page(&first, cursor)? != result {
        return Err("History unavailable".into());
    }
    Ok(result)
}
