//! Fail-closed remote transcript history for an exact active Ares room.
use crate::{peer, remote, store::Store, terminals, text, Result};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};

type SharedStore = Arc<Mutex<Store>>;

#[derive(PartialEq)]
struct Identity {
    room: Value,
    alias: String,
    profile: String,
    session: String,
    host: String,
}

fn safe(s: &str, maximum: usize) -> bool {
    !s.is_empty()
        && s.len() <= maximum
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

pub(super) fn valid_cursor(cursor: &str) -> bool {
    cursor
        .as_bytes()
        .first()
        .is_some_and(|c| (b'1'..=b'9').contains(c))
        && cursor.bytes().all(|c| c.is_ascii_digit())
        && cursor.parse::<i64>().is_ok_and(|value| value > 0)
}

fn identity(store: &SharedStore, id: &str) -> Result<Identity> {
    let guard = store.lock().map_err(|_| "History unavailable")?;
    let room = guard.room(id, false)?;
    if text(&room, "source") != "hermes"
        || text(&room, "status") != "active"
        || room["external_archived"] != 0 && room["external_archived"] != false
    {
        return Err("History unavailable".into());
    }
    let host = terminals::room_host(&guard, id)?;
    let reference = text(&room, "external_profile");
    let parts: Vec<_> = reference.split("::").collect();
    if parts.len() != 3
        || parts[0] != "remote"
        || parts[1] != host
        || !safe(parts[1], 64)
        || !safe(parts[2], 64)
    {
        return Err("History unavailable".into());
    }
    let session = remote::parse_remote_session(text(&room, "external_session_id"), &host, parts[2])
        .ok_or("History unavailable")?;
    if !safe(session, 128) {
        return Err("History unavailable".into());
    }
    let profile = parts[2].to_owned();
    let session = session.to_owned();
    Ok(Identity {
        room,
        alias: host.clone(),
        profile,
        session,
        host,
    })
}

fn trust_binding(alias: &str) -> Result<String> {
    let value = peer::local_control(&json!({"op":"status"}))?;
    let aliases = value["trusted"].as_array().ok_or("History unavailable")?;
    if aliases
        .iter()
        .filter(|item| item.as_str() == Some(alias))
        .count()
        != 1
    {
        return Err("History unavailable".into());
    }
    let binding = value["trusted_bindings"]
        .as_object()
        .and_then(|bindings| bindings.get(alias))
        .and_then(Value::as_str)
        .filter(|binding| {
            binding.len() == 64
                && binding
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .ok_or("History unavailable")?;
    Ok(binding.to_owned())
}

fn clean_page(value: &Value, before: Option<i64>) -> Result<Value> {
    if serde_json::to_vec(value)?.len() > 16_384 {
        return Err("History unavailable".into());
    }
    let object = value.as_object().ok_or("History unavailable")?;
    if object.len() != 3
        || !object.contains_key("messages")
        || !object.contains_key("next_before")
        || !object.contains_key("has_more")
    {
        return Err("History unavailable".into());
    }
    let rows = value["messages"].as_array().ok_or("History unavailable")?;
    if rows.len() > 8 {
        return Err("History unavailable".into());
    }
    let mut messages = Vec::with_capacity(rows.len());
    let mut previous = None;
    for row in rows {
        let map = row.as_object().ok_or("History unavailable")?;
        if map.len() != 3
            || !map.contains_key("role")
            || !map.contains_key("text")
            || !map.contains_key("cursor")
        {
            return Err("History unavailable".into());
        }
        let role = row["role"].as_str().ok_or("History unavailable")?;
        let body = row["text"].as_str().ok_or("History unavailable")?;
        let cursor = row["cursor"]
            .as_i64()
            .filter(|n| *n > 0)
            .ok_or("History unavailable")?;
        if !matches!(role, "user" | "assistant")
            || body.is_empty()
            || body.chars().count() > 500
            || previous.is_some_and(|old| cursor <= old)
            || before.is_some_and(|upper| cursor >= upper)
        {
            return Err("History unavailable".into());
        }
        previous = Some(cursor);
        messages.push(json!({"role":role,"text":body,"cursor":cursor.to_string()}));
    }
    let more = value["has_more"].as_bool().ok_or("History unavailable")?;
    let next = match &value["next_before"] {
        Value::Null => None,
        Value::Number(_) => Some(
            value["next_before"]
                .as_i64()
                .filter(|n| *n > 0)
                .ok_or("History unavailable")?,
        ),
        _ => return Err("History unavailable".into()),
    };
    if more && (rows.is_empty() || next != rows.first().and_then(|r| r["cursor"].as_i64()))
        || !more && next.is_some()
    {
        return Err("History unavailable".into());
    }
    Ok(json!({"messages":messages,"next_before":next.map(|n| n.to_string()),"has_more":more}))
}

pub(super) fn read(store: &SharedStore, id: &str, before: Option<&str>) -> Result<Value> {
    let before = before
        .map(|raw| {
            if !valid_cursor(raw) {
                return Err("History unavailable");
            }
            raw.parse::<i64>().map_err(|_| "History unavailable")
        })
        .transpose()?;
    let first = identity(store, id)?;
    let binding = trust_binding(&first.alias)?;
    if identity(store, id)? != first {
        return Err("History unavailable".into());
    }
    let response = peer::local_control_readonly_strict(&json!({"op":"rpc","peer":first.alias,
        "request":{"op":"history","profile":first.profile,"session_id":first.session,"before":before,"limit":8}}))?;
    let page = clean_page(&response, before)?;
    if trust_binding(&first.alias)? != binding {
        return Err("History unavailable".into());
    }
    if identity(store, id)? != first {
        return Err("History unavailable".into());
    }
    Ok(page)
}
