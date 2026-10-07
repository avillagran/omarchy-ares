//! Exact, read-only peer observation. No SSH, pane input, or historical startup.
use crate::{peer, store::Store, terminals, text, Result};
use base64::Engine;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
type SharedStore = Arc<Mutex<Store>>;
fn current(store: &SharedStore, id: &str) -> Result<Identity> {
    identity(&*store.lock().map_err(|_| "Observe unavailable")?, id)
}

#[derive(Clone, PartialEq)]
struct Identity {
    alias: String,
    profile: String,
    session: String,
    room: Value,
    binding: Option<Value>,
    node: String,
}

fn identity(store: &Store, id: &str) -> Result<Identity> {
    let room = store.room(id, false)?;
    if text(&room, "source") != "hermes"
        || text(&room, "status") != "active"
        || room["external_archived"] != 0 && room["external_archived"] != false
    {
        return Err("Observe unavailable".into());
    }
    let node = terminals::room_host(store, id)?;
    let reference = text(&room, "external_profile");
    let parts: Vec<_> = reference.split("::").collect();
    if parts.len() != 3
        || parts[0] != "remote"
        || parts[1] != node
        || !safe(parts[1], 64)
        || !safe(parts[2], 64)
    {
        return Err("Observe unavailable".into());
    }
    let session =
        crate::remote::parse_remote_session(text(&room, "external_session_id"), &node, parts[2])
            .ok_or("Observe unavailable")?;
    if !safe(session, 128) {
        return Err("Observe unavailable".into());
    }
    let binding = terminals::binding(store, id)?;
    Ok(Identity {
        alias: node.clone(),
        profile: parts[2].to_owned(),
        session: session.to_owned(),
        room,
        binding,
        node,
    })
}
fn safe(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
fn trusted(alias: &str) -> Result<String> {
    let state = peer::local_control(&json!({"op":"status"}))?;
    let unique = state["trusted"]
        .as_array()
        .is_some_and(|list| list.iter().filter(|v| v.as_str() == Some(alias)).count() == 1);
    let binding = state["trusted_bindings"][alias]
        .as_str()
        .unwrap_or_default();
    if !unique
        || binding.len() != 64
        || !binding
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err("Observe unavailable".into());
    }
    Ok(binding.to_owned())
}
fn target(identity: &Identity, inventory: &Value) -> Result<Value> {
    if inventory["host"] != identity.node || inventory["complete"] != true {
        return Err("Observe unavailable".into());
    }
    let terminal =
        terminals::select_session(inventory, "hermes", &identity.profile, &identity.session)?
            .ok_or("Observe unavailable")?;
    if terminal["identitySource"] != "verified"
        || terminal["transport"] != "peer"
        || terminal["peer"] != identity.alias
        || text(&terminal, "terminalId").is_empty()
        || identity.binding.as_ref().is_some_and(|saved| {
            saved["host"] != identity.alias
                || saved["terminal_key"] != terminal["id"]
                || saved["agent_kind"] != "hermes"
        })
    {
        return Err("Observe unavailable".into());
    }
    Ok(terminal)
}

/// Prove a room owns exactly one authenticated peer terminal; never opens a pane.
pub fn prove(store: &SharedStore, id: &str) -> Result<Value> {
    let first = current(store, id)?;
    trusted(&first.alias)?;
    let inventory = peer::inventory(&first.alias)?;
    let owner = target(&first, &inventory)?;
    if current(store, id)? != first {
        return Err("Observe unavailable".into());
    }
    Ok(owner)
}

pub fn observe(store: &SharedStore, id: &str, cols: u16, rows: u16) -> Result<Value> {
    if !(1..=240).contains(&cols) || !(1..=100).contains(&rows) {
        return Err("Observe unavailable".into());
    }
    let first = current(store, id)?;
    let pairing = trusted(&first.alias)?;
    let initial = peer::inventory(&first.alias)?;
    let owner = target(&first, &initial)?;
    let frame = peer::local_control(&json!({"op":"rpc","peer":first.alias,
        "request":{"op":"observe","profile":first.profile,"session_id":first.session,"cols":cols,"rows":rows}}))?;
    // Enforce source's wire bound and exact response identity before release.
    if serde_json::to_vec(&frame)?.len() > 16_384
        || frame["profile"] != first.profile
        || frame["session_id"] != first.session
        || frame["terminal_id"] != owner["terminalId"]
        || frame["cols"] != cols
        || frame["rows"] != rows
        || frame["encoding"] != "base64"
    {
        return Err("Observe unavailable".into());
    }
    let encoded = frame["frame"].as_str().ok_or("Observe unavailable")?;
    if encoded.len() > 10_924 {
        return Err("Observe unavailable".into());
    }
    let decoded = base64::engine::general_purpose::STANDARD.decode(encoded)?;
    if decoded.len() > 8192 || base64::engine::general_purpose::STANDARD.encode(&decoded) != encoded
    {
        return Err("Observe unavailable".into());
    }
    // Observe can block. Recheck authorization, room identity and unique source owner.
    if trusted(&first.alias)? != pairing {
        return Err("Observe unavailable".into());
    }
    let latest = peer::inventory(&first.alias)?;
    if target(&first, &latest)?["id"] != owner["id"] || current(store, id)? != first {
        return Err("Observe unavailable".into());
    }
    Ok(
        json!({"transport":"peer","encoding":"base64","frame":encoded,"terminalId":owner["terminalId"]}),
    )
}

/// Read-only control readiness probe. This cannot authorize input: the paired
/// read inventory does not expose source-authenticated Herdr epoch/runtime
/// attestation or installed all-input fence status. Never turn a grant, pane,
/// or terminal ID into a positive writer capability.
pub fn control_preflight(store: &SharedStore, id: &str) -> Result<()> {
    let first = current(store, id)?;
    trusted(&first.alias)?;
    if current(store, id)? != first {
        return Err("Control unavailable".into());
    }
    let inventory = peer::inventory(&first.alias)?;
    if current(store, id)? != first {
        return Err("Control unavailable".into());
    }
    let owner = target(&first, &inventory)?;
    // Control requires an explicit, exact saved room-to-terminal binding.
    if first.binding.is_none() {
        return Err("Control unavailable".into());
    }
    trusted(&first.alias)?;
    if current(store, id)? != first {
        return Err("Control unavailable".into());
    }
    let latest = peer::inventory(&first.alias)?;
    if target(&first, &latest)? != owner || current(store, id)? != first {
        return Err("Control unavailable".into());
    }
    // No read-only source RPC currently attests installed ControlTerminal
    // fences, server_instance_id, runtime_generation, and unconsumed grant
    // together. Do not call write_grant (it mutates source consent).
    Err("Control unavailable".into())
}
