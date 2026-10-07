use crate::{digest, store::Store, text, Result, MAX_ARTIFACT_BYTES};
use axum::{
    body::{to_bytes, Body},
    extract::{ConnectInfo, Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode},
    response::Response,
    Router,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    path::{Component, PathBuf},
    net::SocketAddr,
    sync::{Arc, Mutex},
};

#[path = "history.rs"]
mod history;
#[path = "local_history.rs"]
mod local_history;

pub const WEB_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../web");
#[derive(Clone)]
pub struct App {
    pub store: Arc<Mutex<Store>>,
    pub token_hash: String,
    pub web_root: PathBuf,
    pub bridge: Option<crate::broker::HermesBroker>,
    pub mirror: Option<Arc<Mutex<crate::integration::HermesLiveTranscriptMirror>>>,
}
pub fn router(store: Store, token: &str, web_root: PathBuf) -> Router {
    Router::new().fallback(handle).with_state(App {
        store: Arc::new(Mutex::new(store)),
        token_hash: digest(token),
        web_root,
        bridge: None,
        mirror: None,
    })
}
pub fn integrated_router(app: App) -> Router {
    Router::new().fallback(handle).with_state(app)
}
pub fn json_response(status: u16, value: Value) -> Response {
    response(
        status,
        "application/json; charset=utf-8",
        value.to_string().into_bytes(),
    )
}
fn response(status: u16, mime: &str, body: Vec<u8>) -> Response {
    let len = body.len();
    let mut r = Response::new(Body::from(body));
    *r.status_mut() = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    for (key, value) in [
        ("content-type", mime),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("x-frame-options", "DENY"),
        ("cache-control", "no-store"),
    ] {
        header(&mut r, key, value);
    }
    header(&mut r, "content-length", &len.to_string());
    r
}
fn header(r: &mut Response, key: &'static str, value: &str) {
    if let Ok(value) = HeaderValue::from_str(value) {
        r.headers_mut().insert(key, value);
    }
}
fn error(status: u16, message: &str) -> Response {
    json_response(status, json!({"ok":false,"error":message}))
}
// Peering secrets are permitted only from a direct loopback HTTP connection.
// A missing ConnectInfo is not evidence of locality (including in test routers).
fn local_peer_request(extensions: &axum::http::Extensions) -> bool {
    extensions.get::<ConnectInfo<SocketAddr>>()
        .is_some_and(|info| info.0.ip().is_loopback())
}
fn peer_name(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.len() <= maximum && value.bytes().all(|b|
        b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
fn peer_fingerprint(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}
fn peer_code(value: &str) -> bool {
    value.len() == 8 && value.bytes().all(|b| b.is_ascii_digit())
}
async fn peer_route(method: &Method, path: &str, query: &HashMap<String,String>,
                    extensions: &axum::http::Extensions, bytes: &[u8]) -> Response {
    let segments: Vec<_> = path.trim_start_matches('/').split('/').collect();
    let request = match (method, segments.as_slice()) {
        (&Method::GET, ["v1", "peers"]) => {
            if !query.is_empty() { return error(400,"Invalid peer query"); }
            json!({"op":"discover"})
        }
        (&Method::POST, ["v1", "peers", "pair"]) => {
            if !local_peer_request(extensions) { return error(403,"Local connection required"); }
            if !query.is_empty() { return error(400,"Invalid peer query"); }
            let Ok(body): std::result::Result<Value,_> = serde_json::from_slice(bytes) else {
                return error(422,"Invalid pairing request");
            };
            if body.as_object().is_none_or(|v| v.len()!=4)
                || text(&body,"peerId").len()!=32
                || !text(&body,"peerId").bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                || !peer_name(text(&body,"alias"),64)
                || text(&body,"alias")=="local"
                || !peer_fingerprint(text(&body,"fingerprint"))
                || !peer_code(text(&body,"code"))
            { return error(422,"Invalid pairing request"); }
            json!({"op":"pair","peer":body["peerId"],"alias":body["alias"],
                "fingerprint":body["fingerprint"],"code":body["code"]})
        }
        (&Method::POST, ["v1", "peers", alias, "revoke"]) => {
            if !local_peer_request(extensions) { return error(403,"Local connection required"); }
            if !query.is_empty() { return error(400,"Invalid peer query"); }
            let Ok(body): std::result::Result<Value,_> = serde_json::from_slice(bytes) else {
                return error(422,"Confirmation required");
            };
            let alias = decode(alias);
            if !peer_name(&alias,64) || alias=="local" || body != json!({"confirmed":true}) {
                return error(422,"Confirmation required");
            }
            json!({"op":"revoke","peer":alias})
        }
        (&Method::GET, ["v1", "peers", alias, "sessions"]) => {
            let alias = decode(alias);
            if !peer_name(&alias,64) || alias=="local" || query.keys().any(|k| k!="profile" && k!="limit" && k!="offset") {
                return error(400,"Invalid peer sessions query");
            }
            let profile = q(query,"profile","default");
            let limit = q(query,"limit","100").parse::<u32>();
            let offset = q(query,"offset","0").parse::<u32>();
            if !peer_name(profile,64) || !matches!(limit,Ok(1..=100)) || !matches!(offset,Ok(0..=10000)) {
                return error(400,"Invalid peer sessions query");
            }
            json!({"op":"rpc","peer":alias,"request":{"op":"sessions","profile":profile,
                "limit":limit.unwrap(),"offset":offset.unwrap()}})
        }
        _ => return error(404,"not found"),
    };
    let operation = text(&request,"op").to_owned();
    let readback_alias = text(&request,"alias").to_owned();
    let readback_id = text(&request,"peer").to_owned();
    // The code is moved into this blocking closure; errors are deliberately generic.
    match tokio::task::spawn_blocking(move || crate::peer::local_control(&request)).await {
        Ok(Ok(value)) => match operation.as_str() {
            "discover" => {
                let Some(peers) = value["peers"].as_array() else { return error(502,"Peer service unavailable"); };
                if peers.len()>100 { return error(502,"Peer response exceeds limit"); }
                let public: Vec<Value> = peers.iter().map(|p| {
                    let paired = p["paired"] == true;
                    let online = p["online"] == true;
                    json!({"id":p["peer_id"],"peerId":p["peer_id"],"peer_id":p["peer_id"],
                        "alias":p["alias"],"name":p["name"],"address":p["address"],
                        "port":p["port"],"fingerprint":p["fingerprint"],
                        "paired":paired,"trusted":paired,"online":online,
                        "status":if online {"online"} else {"offline"}})
                }).collect();
                ok(json!({"peers":public}))
            }
            "pair" => {
                let alias = &readback_alias;
                let id = &readback_id;
                match tokio::task::spawn_blocking(|| crate::peer::local_control(&json!({"op":"discover"}))).await {
                    Ok(Ok(found)) if found["peers"].as_array().is_some_and(|peers| peers.iter().any(|p|
                        p["peer_id"]==id.as_str() && p["alias"]==alias.as_str() && p["paired"]==true)) => ok(json!({"peer":alias,"peer_id":id})),
                    _ => error(502,"Peer trust readback failed"),
                }
            }
            "revoke" => {
                let alias = &readback_id;
                match tokio::task::spawn_blocking(|| crate::peer::local_control(&json!({"op":"discover"}))).await {
                    Ok(Ok(found)) if found["peers"].as_array().is_some_and(|peers| !peers.iter().any(|p|
                        p["alias"]==alias.as_str() && p["paired"]==true)) => ok(json!({"revoked":alias})),
                    _ => error(502,"Peer trust readback failed"),
                }
            },
            "rpc" => {
                let Some(sessions) = value["data"].as_array() else { return error(502,"Peer service unavailable"); };
                if sessions.len()>100 { return error(502,"Peer response exceeds limit"); }
                let public: Vec<Value> = sessions.iter().map(|s| json!({
                    "id":s["id"],"profile":s["profile"],"source":s["source"],
                    "title":s["title"],"message_count":s["message_count"],
                    "started_at":s["started_at"],"last_activity_at":s["last_activity_at"],
                    "cwd":s["cwd"],"git_repo_root":s["git_repo_root"]
                })).collect();
                ok(json!({"sessions":public,"limit":value["limit"],"offset":value["offset"],"has_more":value["has_more"]}))
            }
            _ => error(404,"not found"),
        },
        _ => error(502,"Peer service unavailable"),
    }
}

fn header_text<'a>(headers: &'a HeaderMap, key: &str) -> &'a str {
    headers.get(key).and_then(|v| v.to_str().ok()).unwrap_or("")
}
async fn handle(State(app): State<App>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let path = parts.uri.path().to_owned();
    let method = parts.method;
    if method == Method::GET && path == "/v1/health" {
        return json_response(200, json!({"ok":true,"service":"ares"}));
    }
    if !path.starts_with("/v1/") {
        if method != Method::GET {
            return error(404, "not found");
        }
        return match tokio::task::spawn_blocking(move || static_file(&app.web_root, &path)).await {
            Ok(r) => r,
            Err(e) => error(400, &e.to_string()),
        };
    }
    if !header_text(&parts.headers, "authorization")
        .strip_prefix("Bearer ")
        .is_some_and(|t| digest(t) == app.token_hash)
    {
        return error(401, "authentication required");
    }
    let is_upload = method == Method::PUT && path == "/v1/artifacts";
    let mut bytes = bytes::Bytes::new();
    if method == Method::POST || is_upload {
        let max = if is_upload {
            MAX_ARTIFACT_BYTES
        } else {
            2 * 1024 * 1024
        };
        let message = if is_upload {
            "artifact size is invalid"
        } else {
            "request size is invalid"
        };
        let length = header_text(&parts.headers, "content-length")
            .parse::<usize>()
            .unwrap_or(0);
        if length == 0 || length > max {
            return error(400, message);
        }
        bytes = match to_bytes(body, max).await {
            Ok(b) if b.len() == length => b,
            _ => return error(400, message),
        };
    }
    let query: HashMap<String, String> =
        url::form_urlencoded::parse(parts.uri.query().unwrap_or("").as_bytes())
            .filter(|(_, v)| !v.is_empty())
            .fold(HashMap::new(), |mut m, (k, v)| {
                m.entry(k.into_owned()).or_insert(v.into_owned());
                m
            });
    if path == "/v1/peers" || path.starts_with("/v1/peers/") {
        return peer_route(&method, &path, &query, &parts.extensions, &bytes).await;
    }
    let terminal_segments: Vec<_> = path.trim_start_matches('/').split('/').collect();
    if let ["v1", "rooms", id, "history"] = terminal_segments.as_slice() {
        if method != Method::GET { return error(405, "History is read-only"); }
        let raw: Vec<_> = url::form_urlencoded::parse(parts.uri.query().unwrap_or("").as_bytes()).collect();
        if !peer_name(id, 128) || raw.len() > 1
            || raw.iter().any(|(key, value)| key != "before" || !history::valid_cursor(value)) {
            return error(400, "Invalid history request");
        }
        let id = (*id).to_owned();
        let before = raw.first().map(|(_, value)| value.to_string());
        let store = app.store.clone();
        let local = { let guard = match store.lock() { Ok(guard) => guard, Err(_) => return error(503, "History unavailable") }; match guard.room(&id, false) { Ok(room) => text(&room, "external_profile") != "" && !text(&room, "external_profile").starts_with("remote::"), Err(_) => return error(503, "History unavailable") } };
        if local && !local_peer_request(&parts.extensions) { return error(403, "Local connection required"); }
        return match tokio::task::spawn_blocking(move || if local { local_history::read(&store, &id, before.as_deref()) } else { history::read(&store, &id, before.as_deref()) }).await {
            Ok(Ok(page)) => ok(page),
            _ => error(503, "History unavailable"),
        };
    }
    if let ["v1", "groups", id, "history"] = terminal_segments.as_slice() {
        if method != Method::GET { return error(405, "Group history is read-only"); }
        let raw: Vec<_> = url::form_urlencoded::parse(parts.uri.query().unwrap_or("").as_bytes()).collect();
        if raw.len() > 1 || raw.iter().any(|(key, value)| key != "before" || value.is_empty()) {
            return error(400, "Invalid group history cursor");
        }
        let before = match raw.first() {
            None => i64::MAX,
            Some((_, value)) => match value.parse::<i64>() { Ok(v) if v > 0 => v, _ => return error(400, "Invalid group history cursor") },
        };
        let id = decode(id);
        if !peer_name(&id, 128) { return error(400, "Invalid group identity"); }
        // A bearer alone is not sufficient: the listener also accepts LAN clients.
        // Reject missing or non-loopback connection evidence before touching the source.
        if !local_peer_request(&parts.extensions) {
            return error(403, "Local connection required");
        }
        let store = app.store.clone();
        return match tokio::task::spawn_blocking(move || -> Result<Value> {
            let identity = {
                let store = store.lock().map_err(|_| "Group history unavailable")?;
                let group = store.native_group_dto(&id)?;
                if group["authorityHost"] != "local" {
                    return Err("Group history unavailable".into());
                }
                group
            };
            // The private file is read for each request, never cached or synced to SQLite.
            let profiles = crate::profile_yaml::load_default_profiles()?;
            let store = store.lock().map_err(|_| "Group history unavailable")?;
            if store.native_group_dto(&id).ok().as_ref() != Some(&identity) {
                return Err("Group history unavailable".into());
            }
            // Recheck after waiting for the store lock: source replacement during
            // a request cannot release a page from the earlier source snapshot.
            if crate::profile_yaml::load_default_profiles()? != profiles {
                return Err("Group history unavailable".into());
            }
            store.native_group_history(&id, &profiles, before)
        }).await {
            Ok(Ok(page)) => ok(page),
            _ => error(503, "Group history unavailable"),
        };
    }
    if method == Method::GET {
        if let ["v1", "rooms", id, "preview"] = terminal_segments.as_slice() {
            let id = decode(id);
            let store = app.store.clone();
            return match tokio::task::spawn_blocking(move || -> Result<Response> {
                // Read only the selected Ares room. This never resumes a source session,
                // changes the panel selection, or fetches transcript history in sync.
                let (cached, source, remote) = {
                    let store = store.lock().map_err(|_| "store lock poisoned")?;
                    let room = match store.room(&id, true) {
                        Ok(room) => room,
                        Err(_) => return Ok(error(404, "Chat unavailable")),
                    };
                    let mut cached = store.rows(
                        "SELECT kind,payload FROM room_events WHERE room_id=? AND kind IN ('message.user','message.assistant') ORDER BY cursor DESC LIMIT 8",
                        [&id],
                    )?;
                    cached.reverse();
                    let cached: Vec<Value> = cached.into_iter().filter_map(|event| {
                        let role = match text(&event, "kind") {
                            "message.user" => "user",
                            "message.assistant" => "assistant",
                            _ => return None,
                        };
                        let payload: Value = serde_json::from_str(text(&event, "payload")).ok()?;
                        let body = payload["text"].as_str()?;
                        if body.is_empty() { return None; }
                        Some(json!({"role":role,"text":body.chars().take(500).collect::<String>()}))
                    }).collect();
                    let remote = text(&room, "external_profile").starts_with("remote::");
                    let source = if remote && text(&room, "source") == "hermes" {
                        let parts: Vec<&str> = text(&room, "external_profile").split("::").collect();
                        // The Ares room stores a composite reference; the peer
                        // source accepts only the exact underlying session ID.
                        let reference = text(&room, "external_session_id");
                        let prefix = if parts.len() == 3 {
                            format!("remote::{}::{}::", parts[1], parts[2])
                        } else { String::new() };
                        let session = reference.strip_prefix(&prefix).unwrap_or("");
                        if parts.len() == 3 && parts[0] == "remote"
                            && peer_name(parts[1], 64) && peer_name(parts[2], 64)
                            && peer_name(session, 128) {
                            Some((parts[1].to_owned(),parts[2].to_owned(),session.to_owned()))
                        } else { None }
                    } else { None };
                    (cached, source, remote)
                };
                // A remote room must be checked against its live paired source.
                // Cached history cannot bypass a hidden, revoked, or offline session.
                if !remote && !cached.is_empty() {
                    return Ok(ok(json!({"messages":cached})));
                }
                let Some((alias, profile, session)) = source else {
                    return if remote { Ok(error(502, "Preview unavailable")) }
                        else { Ok(ok(json!({"messages":[]}))) };
                };
                // The private owner socket enforces pairing and the TLS pin before
                // the source receives an exact, authenticated read-only peek.
                let result = crate::peer::local_control(&json!({"op":"rpc","peer":alias,
                    "request":{"op":"preview","profile":profile,"session_id":session,"limit":8}}))
                    .map_err(|_| "Preview unavailable")?;
                let rows = result["messages"].as_array().ok_or("Preview unavailable")?;
                if rows.len() > 8 { return Err("Preview unavailable".into()); }
                let messages: Vec<Value> = rows.iter().filter_map(|row| {
                    let role = row["role"].as_str()?;
                    if role != "user" && role != "assistant" { return None; }
                    let body = row["text"].as_str()?;
                    if body.is_empty() { return None; }
                    Some(json!({"role":role,"text":body.chars().take(500).collect::<String>()}))
                }).collect();
                Ok(ok(json!({"messages":messages})))
            }).await {
                Ok(Ok(response)) => response,
                Ok(Err(_)) => error(502, "Preview unavailable"),
                Err(_) => error(500, "Preview unavailable"),
            };
        }
    }
    if method == Method::GET {
        if let ["v1", "tty", "rooms", id, "observe"] = terminal_segments.as_slice() {
            let raw: Vec<_> = url::form_urlencoded::parse(parts.uri.query().unwrap_or("").as_bytes()).collect();
            if !peer_name(id, 128) || raw.len() != 2
                || raw.iter().filter(|(key, _)| key == "cols").count() != 1
                || raw.iter().filter(|(key, _)| key == "rows").count() != 1 {
                return error(400, "Invalid observe request");
            }
            let (Ok(cols), Ok(rows)) = (
                raw.iter().find(|(key, _)| key == "cols").unwrap().1.parse::<u16>(),
                raw.iter().find(|(key, _)| key == "rows").unwrap().1.parse::<u16>(),
            ) else { return error(400, "Invalid observe request"); };
            if !(1..=240).contains(&cols) || !(1..=100).contains(&rows) {
                return error(400, "Invalid observe request");
            }
            let id = (*id).to_owned();
            let store = app.store.clone();
            return match tokio::task::spawn_blocking(move || {
                crate::observe::observe(&store, &id, cols, rows)
            }).await {
                Ok(Ok(value)) => ok(value),
                _ => error(503, "Observe unavailable"),
            };
        }
    }
    if let ["v1", "tty", "rooms", id, "control", "preflight"] = terminal_segments.as_slice() {
        if method != Method::GET || !peer_name(id, 128) || parts.uri.query().is_some() {
            return error(400, "Invalid control preflight request");
        }
        if std::env::var("ARES_CONTROL_PREFLIGHT").as_deref() != Ok("1") {
            return error(503, "Control unavailable");
        }
        let id = (*id).to_owned();
        let store = app.store.clone();
        // The Store mutex is released before every owner-local source RPC.
        let _ = tokio::task::spawn_blocking(move || {
            crate::observe::control_preflight(&store, &id)
        })
        .await;
        // Preflight has no positive result until source epoch, generation,
        // installed fence and consent can all be attested independently.
        return error(503, "Control unavailable");
    }
    // Read-only member target: exact catalog identity + fresh observed inventory.
    // This route neither starts historical sessions nor modifies room bindings.
    if method == Method::GET {
        if let ["v1", "tty", "groups", id, "members", member, "terminal"] =
            terminal_segments.as_slice()
        {
            let id = decode(id);
            let member = decode(member);
            let store = app.store.clone();
            return match tokio::task::spawn_blocking(move || -> Result<Response> {
                let identity = crate::terminals::group_member_identity(
                    &*store.lock().map_err(|_| "store lock poisoned")?, &id, &member)?;
                let inventory = crate::terminals::inventory("local", true)?;
                let current = crate::terminals::group_member_identity(
                    &*store.lock().map_err(|_| "store lock poisoned")?, &id, &member)?;
                if identity != current {
                    return Err("Group member identity changed during terminal discovery".into());
                }
                Ok(ok(json!({"tty":crate::terminals::resolve_group_member(&id, &current, &inventory)?})))
            }).await {
                Ok(Ok(response)) => response,
                Ok(Err(e)) => error(409, &e.to_string()),
                Err(_) => error(500, "Group member terminal discovery failed"),
            };
        }
    }
    if let ["v1", "tty", "rooms", id, action] = terminal_segments.as_slice() {
        if (method == Method::GET && *action == "terminals")
            || (method == Method::POST && *action == "binding")
        {
            let id = decode(id);
            let body: Value = if method == Method::POST {
                match serde_json::from_slice(&bytes) {
                    Ok(value) => value,
                    Err(_) => return error(422, "Invalid terminal association"),
                }
            } else {
                json!({})
            };
            if method == Method::POST
                && (body["confirmed"] != true || text(&body, "terminalId").len() != 64)
            {
                return error(422, "Choose a terminal and confirm its association first");
            }
            let store = app.store.clone();
            let bind = method == Method::POST;
            return match tokio::task::spawn_blocking(move || -> Result<Response> {
                let host = crate::terminals::room_host(
                    &*store.lock().map_err(|_| "store lock poisoned")?,
                    &id,
                )?;
                // Do not hold SQLite's mutex while contacting another host.
                let mut inventory = crate::terminals::inventory(&host, true)?;
                let store = store.lock().map_err(|_| "store lock poisoned")?;
                if bind {
                    let saved = crate::terminals::bind(
                        &store,
                        &id,
                        &inventory,
                        text(&body, "terminalId"),
                        true,
                    )?;
                    Ok(ok(json!({"binding":saved})))
                } else {
                    inventory["binding"] = json!(crate::terminals::binding(&store, &id)?);
                    Ok(ok(inventory))
                }
            })
            .await
            {
                Ok(Ok(response)) => response,
                Ok(Err(e)) => error(
                    if e.to_string() == "room not found" {
                        404
                    } else {
                        409
                    },
                    &e.to_string(),
                ),
                Err(_) => error(500, "Terminal inventory failed"),
            };
        }
    }
    if method == Method::GET && path == "/v1/tty" {
        return match tokio::task::spawn_blocking(|| ok(crate::herdr::state())).await {
            Ok(response) => response,
            Err(e) => error(500, &e.to_string()),
        };
    }
    if method == Method::POST && path == "/v1/tty/ensure" {
        return match tokio::task::spawn_blocking(crate::herdr::ensure_and_state).await {
            Ok(Ok(value)) => ok(value),
            Ok(Err(e)) => error(400, &e.to_string()),
            Err(e) => error(500, &e.to_string()),
        };
    }
    if method == Method::POST {
        let segments: Vec<_> = path.trim_start_matches('/').split('/').collect();
        if let ["v1", "tty", "rooms", id, action] = segments.as_slice() {
            if *action == "prompt" {
                let id = decode(id);
                let body: Value = match serde_json::from_slice(&bytes) {
                    Ok(value) => value,
                    Err(_) => return error(400, "invalid TTY prompt JSON"),
                };
                let (text, key) = match validate_tty_prompt(&body) {
                    Ok(values) => values,
                    Err(e) => return error(422, &e.to_string()),
                };
                let text = text.to_owned();
                let key = key.to_owned();
                let store = app.store.clone();
                return match tokio::task::spawn_blocking(move || -> Result<Response> {
                    // Hold the store mutex over check/reserve/send/readback so the
                    // same Ares process cannot race a second prompt attempt.
                    let store = store.lock().map_err(|_| "store lock poisoned")?;
                    let target = match room_target_for_prompt(&store, &id) {
                        Ok(target) => target,
                        Err(e) if e.to_string() == "room not found" => return Ok(error(404, "room not found")),
                        Err(e) => return Ok(error(409, &e.to_string())),
                    };
                    let session = target.resume.as_deref().ok_or("TTY room has no stored Hermes session")?;
                    match tty_receipt_status(&store, &id, &key, &text, session) {
                        Ok(Some(status)) if status == "accepted" => return Ok(ok(json!({"accepted":true,"duplicate":true}))),
                        Ok(Some(_)) => return Ok(error(409, "TTY prompt outcome is uncertain; do not retry automatically")),
                        Ok(None) => (),
                        Err(e) => return Ok(error(409, &e.to_string())),
                    }
                    let prepared = match crate::herdr::prepare_prompt(&id, &target.profile, session, true) {
                        Ok(prepared) => prepared,
                        Err(_) => return Ok(error(409, "No verified idle local Herdr owner with an empty composer; draft was not sent")),
                    };
                    let reserved = match reserve_tty_receipt(&store, &id, &key, &text, session) {
                        Ok(reserved) => reserved,
                        Err(e) => return Ok(error(409, &e.to_string())),
                    };
                    if !reserved {
                        let status: String = store.db.query_row("SELECT status FROM tty_prompt_receipts WHERE room_id=? AND idempotency_key=?", rusqlite::params![id,key], |row| row.get(0))?;
                        return Ok(if status == "accepted" {
                            ok(json!({"accepted":true,"duplicate":true}))
                        } else {
                            error(409, "TTY prompt outcome is uncertain; do not retry automatically")
                        });
                    }
                    match crate::herdr::send_prepared_prompt(prepared, &text) {
                        Ok(()) => {
                            store.db.execute("UPDATE tty_prompt_receipts SET status='accepted' WHERE room_id=? AND idempotency_key=?", rusqlite::params![id,key])?;
                            Ok(json_response(202, json!({"ok":true,"accepted":true})))
                        }
                        Err(_) => Ok(error(409, "TTY prompt outcome is uncertain; do not retry automatically")),
                    }
                }).await {
                    Ok(Ok(response)) => response,
                    Ok(Err(_)) | Err(_) => error(500, "TTY prompt operation failed; do not retry automatically"),
                };
            }
            if ["ensure", "focus"].contains(action) {
                let id = decode(id);
                let focus = *action == "focus";
                let open_if_missing = match historical_open_requested(action, &bytes) {
                    Ok(value) => value,
                    Err(_) => return error(400, "invalid terminal opening JSON"),
                };
                let store = app.store.clone();
                return match tokio::task::spawn_blocking(move || -> Result<Response> {
                    let host = crate::terminals::room_host(
                        &*store.lock().map_err(|_| "store lock poisoned")?,
                        &id,
                    )?;
                    let inventory = if host != "local" && open_if_missing {
                        // Explicit interactive opening requires a separate, trusted SSH
                        // target. Peer inventory is output-only and cannot authorize it.
                        crate::terminals::inventory_ssh(&host, true)?
                    } else {
                        crate::terminals::inventory(&host, open_if_missing)?
                    };
                    let plan = crate::terminals::prepare_room(
                        &*store.lock().map_err(|_| "store lock poisoned")?,
                        &id,
                        &inventory,
                        open_if_missing,
                    )?;
                    match plan {
                        crate::terminals::RoomResolution::Attached(mut value) => {
                            if focus && value["tty"]["transport"] == "peer" {
                                // A peer inventory descriptor is never executable. Re-prove
                                // the stored exact owner before granting output-only observation.
                                let owner = crate::observe::prove(&store, &id)?;
                                if value["tty"]["terminalId"] != owner["terminalId"] {
                                    return Err("Observe unavailable".into());
                                }
                                value["tty"]["sessionName"] = json!("peer-observe");
                                value["tty"]["agentName"] = json!(id);
                                value["tty"]["agentStarted"] = json!(true);
                                value["tty"]["connectionState"] = json!("observed");
                            }
                            Ok(ok(value))
                        },
                        crate::terminals::RoomResolution::Open { host, request } => {
                            // Do not hold the shared database mutex during SSH/CLI startup.
                            let opened = crate::terminals::open_history(&host, &request)?;
                            let store = store.lock().map_err(|_| "store lock poisoned")?;
                            Ok(ok(crate::terminals::verify_opened_room(
                                &store, &id, &host, &request, &opened,
                            )?))
                        }
                    }
                })
                .await
                {
                    Ok(Ok(response)) => response,
                    Ok(Err(e)) => error(
                        if e.to_string() == "room not found" {
                            404
                        } else {
                            409
                        },
                        &e.to_string(),
                    ),
                    Err(_) => error(500, "Terminal resolution failed"),
                };
            }
        }
    }
    if app.bridge.is_some() {
        let result = integrated(&app, &method, &path, &query, &bytes).await;
        match result {
            Ok(Some(response)) => return response,
            Err(e) => {
                return error(
                    if e.to_string() == "bot not found" {
                        404
                    } else {
                        400
                    },
                    &e.to_string(),
                )
            }
            _ => (),
        }
    }
    match tokio::task::spawn_blocking(move || -> Response {
        let result=(|| -> Result<Response> {
            let store=app.store.lock().map_err(|_|"store lock poisoned")?;
            if method==Method::GET { return get(&store,&path,&query,&parts.headers); }
            if is_upload {
                let mut metadata=json!({"projectId":q(&query,"projectId",""),"roomId":q(&query,"roomId",""),"name":q(&query,"name",""),"author":q(&query,"author","user"),"mime":parts.headers.get("content-type").and_then(|v|v.to_str().ok()).unwrap_or("application/octet-stream")});
                if let Some(id)=query.get("executionId") { metadata["executionId"]=json!(id); }
                return Ok(json_response(201,json!({"ok":true,"artifact":store.store_artifact(&metadata,&bytes)?})));
            }
            if method==Method::POST {
                let value: Value=serde_json::from_slice(&bytes)?;
                if !value.is_object() { return Err("JSON object required".into()); }
                return post(&store,&path,&value);
            }
            Ok(error(404,"not found"))
        })();
        result.unwrap_or_else(|e| error(if ["bot not found","room not found"].contains(&e.to_string().as_str()) {404} else {400},&e.to_string()))
    }).await { Ok(r)=>r, Err(e)=>error(400,&e.to_string()) }
}
async fn integrated(
    app: &App,
    method: &Method,
    path: &str,
    query: &HashMap<String, String>,
    bytes: &[u8],
) -> Result<Option<Response>> {
    use crate::integration::{with_store, ExecutionRunner, InterventionManager, LifecycleManager};
    let bridge = app
        .bridge
        .as_ref()
        .ok_or("Hermes execution bridge is unavailable")?;
    if method == Method::GET && path == "/v1/live" {
        let room = query.get("roomId").ok_or("roomId is required")?;
        let events = with_store(&app.store, |s| {
            if let Some(m) = &app.mirror {
                m.lock()
                    .map_err(|_| "mirror lock poisoned")?
                    .live_events(s, room)
            } else {
                s.live_events(room)
            }
        })?;
        return Ok(Some(ok(json!({"events":events}))));
    }
    if method != Method::POST {
        return Ok(None);
    }
    let body: Value = serde_json::from_slice(bytes)?;
    if !body.is_object() {
        return Err("JSON object required".into());
    }
    let segments: Vec<_> = path.trim_start_matches('/').split('/').collect();
    Ok(Some(match segments.as_slice() {
        ["v1", "executions", id, "approval"] => ok(InterventionManager::approve(
            &app.store,
            bridge,
            id,
            text(&body, "requestId"),
            body["choice"].as_str().unwrap_or("once"),
        )
        .await?),
        ["v1", "rooms", id, "pause"] => ok(LifecycleManager::pause_room(
            &app.store,
            bridge,
            text(&body, "projectId"),
            id,
            text(&body, "idempotencyKey"),
        )
        .await?),
        ["v1", "rooms", id, "send"] => {
            let id_for_check = (*id).to_owned();
            let local_target = with_store(&app.store, |store| room_target(store, id)).ok();
            let owned_by_herdr = tokio::task::spawn_blocking(move || {
                crate::herdr::room_agent_running(&id_for_check)
                    || local_target.is_some_and(|target| {
                        if target.peer.is_some() {
                            // A peer's stored session cannot be sent through
                            // this machine's Hermes Desktop bridge.
                            return true;
                        }
                        crate::herdr::attach_live_room(
                            &id_for_check,
                            &target.profile,
                            target.resume.as_deref(),
                        )
                        .ok()
                        .flatten()
                        .is_some()
                    })
            })
            .await?;
            if owned_by_herdr {
                return Err("This chat cannot send through the local Hermes Desktop bridge. Use its herdr terminal when available.".into());
            }
            let execution = with_store(&app.store, |s| {
                s.accept_prompt(
                    text(&body, "projectId"),
                    id,
                    body["botId"].as_str().unwrap_or("default"),
                    text(&body, "text"),
                    text(&body, "idempotencyKey"),
                    crate::producers::array(&body["artifactIds"]),
                )
            })?;
            if execution["status"] == "queued" {
                let runner = ExecutionRunner::new(app.store.clone(), bridge.clone());
                let id = text(&execution, "id").to_owned();
                tokio::spawn(async move {
                    let _ = runner.run_execution(&id).await;
                });
            }
            json_response(
                202,
                json!({"ok":true,"accepted":true,"execution":execution}),
            )
        }
        ["v1", "bots", id] => {
            let bot = with_store(&app.store, |s| s.update_bot(&decode(id), &body))?;
            for session in crate::producers::array(&bot["unboundSessions"]) {
                let runtime = text(session, "runtimeSessionId");
                if !runtime.is_empty() {
                    let _ = bridge
                        .call("session.interrupt", json!({"session_id":runtime}), 10)
                        .await;
                    let _ = bridge
                        .call("session.close", json!({"session_id":runtime}), 20)
                        .await;
                }
            }
            ok(json!({"bot":bot}))
        }
        _ => return Ok(None),
    }))
}
fn historical_open_requested(action: &str, bytes: &[u8]) -> Result<bool> {
    if action != "focus" || bytes.is_empty() {
        return Ok(false);
    }
    let value: Value = serde_json::from_slice(bytes)?;
    if !value.is_object() {
        return Err("JSON object required".into());
    }
    // `open_history` starts only through the owner-host exact-resume path:
    // stored-ID lease, complete history acknowledgment, and Herdr readback.
    Ok(value["openIfMissing"] == true)
}

struct RoomTarget {
    profile: String,
    resume: Option<String>,
    peer: Option<String>,
}

#[cfg(test)]
fn desktop_owns_session(reply: &Value, resume: &str) -> bool {
    reply["ok"] == true
        && reply["result"]["sessions"]
            .as_array()
            .is_some_and(|sessions| {
                sessions.iter().any(|session| {
                    text(session, "id") == resume || text(session, "session_key") == resume
                })
            })
}

fn room_target(store: &Store, id: &str) -> Result<RoomTarget> {
    let room = store.one(
        "SELECT r.*,p.node_id AS project_node FROM rooms r JOIN projects p ON p.id=r.project_id WHERE r.id=? AND r.deleted_at IS NULL",
        [id], "room not found",
    )?;
    if matches!(
        text(&room, "source"),
        "hermes-native-group" | "hermes-native-member"
    ) {
        return Err("Native bot group discovery is read-only; no scalar session target".into());
    }
    let node = text(&room, "project_node");
    if node != "local" {
        let profile = text(&room, "external_profile")
            .strip_prefix(&format!("remote::{node}::"))
            .ok_or("remote room has no matching peer profile")?;
        let session =
            crate::remote::parse_remote_session(text(&room, "external_session_id"), node, profile)
                .ok_or("remote room has no matching stored session")?;
        crate::herdr::validate_target(id, profile, session)?;
        return Ok(RoomTarget {
            profile: profile.to_owned(),
            resume: Some(session.to_owned()),
            peer: Some(node.to_owned()),
        });
    }
    if text(&room, "external_profile").starts_with("remote::") {
        return Err("remote profile cannot run in local herdr".into());
    }
    let bot = store.one(
        "SELECT b.id,b.profile FROM room_members m JOIN bots b ON b.id=m.bot_id WHERE m.room_id=? AND b.node_id='local' AND b.enabled=1 ORDER BY m.position LIMIT 1",
        [id], "room has no local Hermes profile",
    )?;
    let profile = text(&bot, "profile");
    crate::herdr::validate_target(id, profile, "")?;
    let bindings = store.rows(
        "SELECT stored_session_id FROM session_bindings WHERE room_id=? AND bot_id=? AND node_id='local' AND lifecycle_state='resident' ORDER BY updated_at DESC",
        rusqlite::params![id, text(&bot, "id")],
    )?;
    let mut resume = bindings
        .iter()
        .map(|b| text(b, "stored_session_id"))
        .find(|sid| !sid.is_empty() && crate::herdr::validate_target(id, profile, sid).is_ok())
        .map(str::to_owned);
    if resume.is_none()
        && text(&room, "source") == "hermes"
        && text(&room, "external_profile") == profile
    {
        let external = text(&room, "external_session_id");
        if !external.is_empty() && crate::herdr::validate_target(id, profile, external).is_ok() {
            resume = Some(external.to_owned());
        }
    }
    Ok(RoomTarget {
        profile: profile.to_owned(),
        resume,
        peer: None,
    })
}

fn room_target_for_prompt(store: &Store, id: &str) -> Result<RoomTarget> {
    if crate::terminals::binding(store, id)?.is_some() {
        return Err("This room is associated with a Herdr terminal; use its native input".into());
    }
    let target = room_target(store, id)?;
    let room = store.one(
        "SELECT status FROM rooms WHERE id=? AND deleted_at IS NULL",
        [id],
        "room not found",
    )?;
    if text(&room, "status") != "active" {
        return Err("room is not active".into());
    }
    if target.peer.is_some() {
        return Err("remote TTY prompt route is not verified".into());
    }
    let resume = target
        .resume
        .as_deref()
        .ok_or("TTY room has no stored Hermes session")?;
    let external = store.one(
        "SELECT source,external_profile,external_session_id FROM rooms WHERE id=?",
        [id],
        "room not found",
    )?;
    if text(&external, "source") == "hermes"
        && (text(&external, "external_profile") != target.profile
            || text(&external, "external_session_id") != resume)
    {
        return Err("TTY room stored owner disagrees with its binding".into());
    }
    let members = store.rows("SELECT m.bot_id,b.profile,b.node_id,b.enabled FROM room_members m LEFT JOIN bots b ON b.id=m.bot_id WHERE m.room_id=?", [id])?;
    if members.len() != 1
        || text(&members[0], "profile") != target.profile
        || text(&members[0], "node_id") != "local"
        || members[0]["enabled"] != 1
    {
        return Err("TTY room owner is ambiguous".into());
    }
    let bindings = store.rows("SELECT bot_id,node_id,stored_session_id,lifecycle_state FROM session_bindings WHERE room_id=?", [id])?;
    if bindings.iter().any(|b| {
        text(b, "bot_id") != text(&members[0], "bot_id")
            || text(b, "node_id") != "local"
            || (text(b, "lifecycle_state") == "resident" && text(b, "stored_session_id") != resume)
    }) {
        return Err("TTY room bindings are ambiguous".into());
    }
    Ok(target)
}

fn tty_receipt_status(
    store: &Store,
    room: &str,
    key: &str,
    prompt: &str,
    session: &str,
) -> Result<Option<String>> {
    use rusqlite::{params, OptionalExtension};
    store.db.execute_batch("PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS tty_prompt_receipts (room_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'uncertain', PRIMARY KEY(room_id,idempotency_key));")?;
    let previous: Option<(String, String)> = store.db.query_row(
        "SELECT payload_hash,status FROM tty_prompt_receipts WHERE room_id=? AND idempotency_key=?",
        params![room, key],
        |row| Ok((row.get(0)?, row.get(1)?)),
    ).optional()?;
    match previous {
        Some((hash, _)) if hash != digest(json!([prompt, session]).to_string()) => {
            Err("idempotencyKey was used for a different TTY prompt".into())
        }
        Some((_, status)) => Ok(Some(status)),
        None => Ok(None),
    }
}

fn reserve_tty_receipt(
    store: &Store,
    room: &str,
    key: &str,
    prompt: &str,
    session: &str,
) -> Result<bool> {
    use rusqlite::params;
    store.db.execute_batch("PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS tty_prompt_receipts (room_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'uncertain', PRIMARY KEY(room_id,idempotency_key));")?;
    let payload_hash = digest(json!([prompt, session]).to_string());
    let inserted = store.db.execute("INSERT OR IGNORE INTO tty_prompt_receipts(room_id,idempotency_key,payload_hash) VALUES(?,?,?)", params![room,key,payload_hash])?;
    if inserted == 0 {
        let previous: String = store.db.query_row(
            "SELECT payload_hash FROM tty_prompt_receipts WHERE room_id=? AND idempotency_key=?",
            params![room, key],
            |row| row.get(0),
        )?;
        if previous != payload_hash {
            return Err("idempotencyKey was used for a different TTY prompt".into());
        }
    }
    Ok(inserted == 1)
}

fn validate_tty_prompt(body: &Value) -> Result<(&str, &str)> {
    let object = body.as_object().ok_or("JSON object required")?;
    if object.keys().any(|key| {
        ![
            "text",
            "artifactIds",
            "idempotencyKey",
            "confirmedEmptyComposer",
        ]
        .contains(&key.as_str())
    }) {
        return Err("unsupported TTY prompt field".into());
    }
    let artifacts = body["artifactIds"]
        .as_array()
        .ok_or("artifactIds must be an array")?;
    if !artifacts.is_empty() {
        return Err("TTY prompt does not support images or artifacts; draft was not sent".into());
    }
    if body["confirmedEmptyComposer"] != true {
        return Err("Review the visible TTY input and confirm it is empty before sending".into());
    }
    let text = body["text"].as_str().ok_or("TTY prompt text is required")?;
    if text.trim().is_empty() || text.len() > 65536 || text.contains('\0') || text.starts_with('-')
    {
        return Err("TTY prompt text is invalid".into());
    }
    let key = body["idempotencyKey"]
        .as_str()
        .ok_or("idempotencyKey is required")?;
    if key.is_empty()
        || key.len() > 128
        || !key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err("idempotencyKey is invalid".into());
    }
    Ok((text, key))
}

fn q<'a>(query: &'a HashMap<String, String>, key: &str, default: &'a str) -> &'a str {
    query.get(key).map(String::as_str).unwrap_or(default)
}
fn ok(mut value: Value) -> Response {
    value["ok"] = json!(true);
    json_response(200, value)
}
fn get(
    store: &Store,
    path: &str,
    query: &HashMap<String, String>,
    headers: &HeaderMap,
) -> Result<Response> {
    let room = query.get("roomId").map(String::as_str);
    Ok(match path {
        "/v1/models" => ok(json!({"models":store.models_catalog()?})),
        "/v1/control/training" => ok(json!({"examples":store.control_training_examples()?})),
        "/v1/control" => {
            let mut items = store.control_items()?;
            items.extend(store.control_agent_items(&crate::herdr::default_agents())?);
            let peers = store.control_remote_peers()?;
            let reports = crate::remote::observed_remote_activity(&peers);
            items.extend(store.control_remote_items(&reports)?);
            items.truncate(100);
            let mut active_rooms = Vec::new();
            for item in &items {
                if ["working", "running", "queued"].contains(&text(item, "status")) {
                    let room_id = text(item, "roomId");
                    if !room_id.is_empty() && !active_rooms.contains(&room_id) {
                        active_rooms.push(room_id);
                    }
                }
                if active_rooms.len() == 12 {
                    break;
                }
            }
            ok(json!({"items":items,"logs":store.control_logs(&active_rooms)?}))
        }
        "/v1/state" => {
            let event_room = query
                .get("eventRoomId")
                .map(String::as_str)
                .or_else(|| (q(query, "compact", "") == "1").then_some(""));
            json_response(200, store.snapshot(room, event_room)?)
        }
        "/v1/events" => {
            let limit = q(query, "limit", "40").parse::<i64>()?;
            if let Some(before) = query.get("before") {
                ok(store.events_before(
                    room.ok_or("roomId is required for historical events")?,
                    before.parse()?,
                    limit,
                )?)
            } else {
                ok(store.events_after(q(query, "after", "0").parse()?, limit, room)?)
            }
        }
        "/v1/live" => ok(json!({"events":store.live_events(room.ok_or("roomId is required")?)?})),
        "/v1/tty" => ok(crate::herdr::state()),
        "/v1/drafts" => ok(json!({"drafts":store.device_drafts(q(query,"device","desktop"))?})),
        _ if path.starts_with("/v1/artifacts/") => artifact(
            store,
            path.rsplit('/').next().unwrap_or(""),
            header_text(headers, "range"),
        )?,
        _ => error(404, "not found"),
    })
}
fn post(store: &Store, path: &str, body: &Value) -> Result<Response> {
    let created = |key: &str, value: Value| json_response(201, json!({"ok":true,key:value}));
    let default = |key: &str, value: &str| {
        body.get(key)
            .and_then(Value::as_str)
            .unwrap_or(value)
            .to_owned()
    };
    match path {
        "/v1/groups" => {
            return Ok(created(
                "group",
                store.create_group(text(body, "name"), text(body, "instructions"))?,
            ))
        }
        "/v1/projects" => {
            return Ok(created(
                "project",
                store.create_project(
                    text(body, "name"),
                    text(body, "path"),
                    &default("nodeId", "local"),
                    body["groupId"].as_str(),
                )?,
            ))
        }
        "/v1/preferences" => return Ok(ok(json!({"preferences":store.update_preferences(body)?}))),
        "/v1/tty/ensure" => return Ok(ok(crate::herdr::ensure_and_state()?)),
        "/v1/control/ensure" => return Ok(ok(json!({"room":store.ensure_control_room()?}))),
        "/v1/control/decisions" => {
            return Ok(ok(json!({"event":store.record_control_decision(
            text(body,"roomId"),text(body,"action"),text(body,"note"))?})))
        }
        "/v1/rooms" => {
            let participants = body["participants"]
                .as_array()
                .filter(|a| !a.is_empty())
                .cloned()
                .unwrap_or(vec![json!("default")]);
            return Ok(created(
                "room",
                store.create_room(text(body, "projectId"), text(body, "name"), &participants)?,
            ));
        }
        _ => (),
    }
    let segments: Vec<_> = path.trim_start_matches('/').split('/').collect();
    Ok(match segments.as_slice() {
        ["v1", "projects", id, "map-color"] => {
            ok(json!({"project":store.update_project_map_color(&decode(id), text(body, "color"))?}))
        }
        ["v1", "projects", id, "map-position"] => {
            if !body.as_object().is_some_and(|fields| fields.len() == 2
                && fields.contains_key("x") && fields.contains_key("y")) {
                return Ok(error(422, "Choose a map position"));
            }
            let (Some(x), Some(y)) = (body["x"].as_i64(), body["y"].as_i64()) else {
                return Ok(error(422, "Invalid map position"));
            };
            ok(json!({"project":store.update_project_map_position(&decode(id), x, y)?}))
        }
        ["v1", "drafts", id] => {
            let ids = match body.get("artifactIds") {
                None => None,
                Some(value) => Some(
                    value
                        .as_array()
                        .ok_or("artifactIds must be an array")?
                        .as_slice(),
                ),
            };
            ok(
                json!({"draft":store.save_draft_with_artifacts(&default("device","desktop"),&decode(id),text(body,"text"),ids)?}),
            )
        }
        ["v1", "executions", _, "approval"] => {
            return Err("Hermes execution bridge is unavailable".into())
        }
        ["v1", "peers", id, "preferences"] => ok(
            json!({"peer":store.update_peer_preferences(&decode(id),text(body,"name"),text(body,"colorRole"))?}),
        ),
        ["v1", "bots", id] => ok(json!({"bot":store.update_bot(&decode(id),body)?})),
        ["v1", "rooms", id, "alias"] => {
            ok(json!({"room":store.update_room_alias(&decode(id),text(body,"alias"))?}))
        }
        ["v1", "rooms", id, "control"] => {
            let included = body["included"]
                .as_bool()
                .ok_or("included must be a boolean")?;
            ok(json!({"room":store.set_control_included(&decode(id),included)?}))
        }
        ["v1", "rooms", id, "send"] => json_response(
            202,
            json!({"ok":true,"accepted":true,"execution":store.accept_prompt(text(body,"projectId"),id,&default("botId","default"),text(body,"text"),text(body,"idempotencyKey"),body["artifactIds"].as_array().map(Vec::as_slice).unwrap_or(&[]))?}),
        ),
        ["v1", "rooms", id, action]
            if ["pause", "resume", "archive", "delete", "restore"].contains(action) =>
        {
            ok(store.lifecycle(
                text(body, "projectId"),
                id,
                text(body, "idempotencyKey"),
                action,
            )?)
        }
        _ => error(404, "not found"),
    })
}
/// Python-compatible single explicit-start range. Suffix/multipart ranges are rejected.
pub fn parse_range(header: &str, size: u64) -> std::result::Result<(u64, u64), &'static str> {
    if size == 0 {
        return Err("artifact range is invalid");
    }
    if header.is_empty() {
        return Ok((0, size - 1));
    }
    let range = header.strip_prefix("bytes=").ok_or("invalid range")?;
    let (a, b) = range.split_once('-').ok_or("invalid range")?;
    if a.is_empty()
        || !a.bytes().all(|c| c.is_ascii_digit())
        || !b.bytes().all(|c| c.is_ascii_digit())
    {
        return Err("invalid range");
    }
    let first = a.parse::<u64>().map_err(|_| "artifact range is invalid")?;
    let last = if b.is_empty() {
        size - 1
    } else {
        b.parse::<u64>().unwrap_or(u64::MAX).min(size - 1)
    };
    if first > last || first >= size {
        return Err("artifact range is invalid");
    }
    Ok((first, last))
}
fn artifact(store: &Store, id: &str, range: &str) -> Result<Response> {
    let rows = store.rows("SELECT * FROM artifacts WHERE id=?", [id])?;
    let Some(row) = rows.first() else {
        return Ok(error(404, "artifact does not exist"));
    };
    let size = row["size"].as_u64().unwrap_or(0);
    let (first, last) = match parse_range(range, size) {
        Ok(r) => r,
        Err(message) => {
            let mut r = if message == "invalid range" {
                error(416, message)
            } else {
                let mut r = response(416, "application/json; charset=utf-8", vec![]);
                r.headers_mut().remove("content-type");
                r
            };
            header(&mut r, "content-range", &format!("bytes */{size}"));
            return Ok(r);
        }
    };
    let body = store.read_artifact(row, first, last)?;
    let mut r = response(
        if range.is_empty() { 200 } else { 206 },
        text(row, "mime"),
        body,
    );
    header(&mut r, "accept-ranges", "bytes");
    header(
        &mut r,
        "content-disposition",
        &format!("inline; filename*=UTF-8''{}", text(row, "name")),
    );
    if !range.is_empty() {
        header(
            &mut r,
            "content-range",
            &format!("bytes {first}-{last}/{size}"),
        );
    }
    Ok(r)
}
pub fn decode(s: &str) -> String {
    let mut bytes = Vec::new();
    let input = s.as_bytes();
    let mut i = 0;
    while i < input.len() {
        if input[i] == b'%' && i + 2 < input.len() {
            if let (Some(a), Some(b)) = (
                (input[i + 1] as char).to_digit(16),
                (input[i + 2] as char).to_digit(16),
            ) {
                bytes.push((a * 16 + b) as u8);
                i += 3;
                continue;
            }
        }
        bytes.push(input[i]);
        i += 1;
    }
    String::from_utf8_lossy(&bytes).into_owned()
}
fn static_file(root: &std::path::Path, path: &str) -> Response {
    let result = (|| -> Result<Response> {
        let root = root.canonicalize()?;
        let decoded = decode(path);
        let relative = decoded.trim_start_matches('/');
        let mut target = root.clone();
        for component in std::path::Path::new(relative).components() {
            match component {
                Component::ParentDir => {
                    target.pop();
                }
                Component::Normal(c) => target.push(c),
                Component::CurDir => (),
                _ => return Ok(error(403, "forbidden")),
            }
            // Resolve existing symlinks before processing subsequent '..' components.
            if target.exists() {
                target = target.canonicalize()?;
            }
        }
        if !target.starts_with(&root) {
            return Ok(error(403, "forbidden"));
        }
        if !target.is_file() {
            target = root.join("index.html");
        }
        if !target.is_file() {
            return Ok(error(404, "not found"));
        }
        let mime = mime_guess::from_path(&target)
            .first_or_octet_stream()
            .to_string();
        let mime = if mime.starts_with("text/") || mime.contains("javascript") {
            format!("{mime}; charset=utf-8")
        } else {
            mime
        };
        let mut r = response(200, &mime, fs::read(target)?);
        header(&mut r,"content-security-policy","default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'");
        Ok(r)
    })();
    result.unwrap_or_else(|e| error(400, &e.to_string()))
}

#[cfg(test)]
mod tty_tests {
    use super::*;

    #[test]
    fn native_group_cannot_route_to_a_single_member() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open(
            &dir.path().join("db"),
            &dir.path().join("artifacts"),
            dir.path().join("hermes"),
        )
        .unwrap();
        let project = store
            .create_project("QA", dir.path().to_str().unwrap(), "local", None)
            .unwrap();
        let room = store
            .create_room(text(&project, "id"), "Group", &[json!("default")])
            .unwrap();
        let id = text(&room, "id");
        for source in ["hermes-native-group", "hermes-native-member"] {
            store
                .db
                .execute("UPDATE rooms SET source=? WHERE id=?", [source, id])
                .unwrap();
            assert!(room_target(&store, id).is_err());
            assert!(room_target_for_prompt(&store, id).is_err());
        }
    }

    #[test]
    fn historical_open_requires_explicit_visible_focus() {
        assert!(!historical_open_requested("focus", b"").unwrap());
        assert!(!historical_open_requested("focus", br#"{}"#).unwrap());
        assert!(!historical_open_requested("focus", br#"{"openIfMissing":false}"#).unwrap());
        assert!(!historical_open_requested("focus", br#"{"openIfMissing":"true"}"#).unwrap());
        assert!(!historical_open_requested("ensure", br#"{"openIfMissing":true}"#).unwrap());
        assert!(historical_open_requested("focus", br#"{"openIfMissing":true}"#).unwrap());
        assert!(historical_open_requested("focus", b"[").is_err());
        assert!(historical_open_requested("focus", b"[]").is_err());
    }

    #[test]
    fn explicit_terminal_binding_blocks_legacy_prompt_routing() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open(
            &dir.path().join("db"),
            &dir.path().join("artifacts"),
            dir.path().join("hermes"),
        )
        .unwrap();
        let p = store
            .create_project("QA", dir.path().to_str().unwrap(), "local", None)
            .unwrap();
        let room = store
            .create_room(text(&p, "id"), "QA", &[json!("default")])
            .unwrap();
        let id = text(&room, "id");
        store.db.execute("UPDATE rooms SET source='hermes', external_profile='default', external_session_id='test-session' WHERE id=?", [id]).unwrap();
        assert!(room_target_for_prompt(&store, id).is_ok());
        store
            .db
            .execute(
                "INSERT INTO terminal_bindings VALUES(?,'local','test-instance','claude',0)",
                [id],
            )
            .unwrap();
        assert!(room_target_for_prompt(&store, id).is_err());
    }

    #[tokio::test]
    async fn terminal_association_requires_confirmation_before_contacting_host() {
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(
            &tmp.path().join("db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap();
        let app = App {
            store: Arc::new(Mutex::new(store)),
            token_hash: digest("test-token"),
            web_root: tmp.path().into(),
            bridge: None,
            mirror: None,
        };
        let body = json!({"terminalId":"fake", "confirmed":false}).to_string();
        let request = Request::builder()
            .method("POST")
            .uri("/v1/tty/rooms/missing/binding")
            .header("authorization", "Bearer test-token")
            .header("content-length", body.len())
            .body(Body::from(body))
            .unwrap();
        let response = handle(State(app), request).await;
        assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
    }

    #[tokio::test]
    async fn control_training_route_is_read_only() {
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(
            &tmp.path().join("db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap();
        let response = get(
            &store,
            "/v1/control/training",
            &HashMap::new(),
            &HeaderMap::new(),
        )
        .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), 4096).await.unwrap();
        let value: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(value["ok"], true);
        assert_eq!(value["examples"], json!([]));
    }

    #[test]
    fn tty_prompt_rejects_images_and_malformed_payloads_before_routing() {
        for body in [
            json!({"text":"hello","artifactIds":["image_1"],"idempotencyKey":"key_1","confirmedEmptyComposer":true}),
            json!({"text":"hello","artifactIds":"oops","idempotencyKey":"key_1","confirmedEmptyComposer":true}),
            json!({"text":"","artifactIds":[],"idempotencyKey":"key_1","confirmedEmptyComposer":true}),
            json!({"text":"hello","artifactIds":[],"idempotencyKey":"","confirmedEmptyComposer":true}),
            json!({"text":"--help","artifactIds":[],"idempotencyKey":"key_1","confirmedEmptyComposer":true}),
            json!({"text":"hello","artifactIds":[],"idempotencyKey":"key_1","image":"base64","confirmedEmptyComposer":true}),
        ] {
            assert!(
                validate_tty_prompt(&body).is_err(),
                "unexpectedly accepted: {body}"
            );
        }
        assert!(validate_tty_prompt(
            &json!({"text":"hello","artifactIds":[],"idempotencyKey":"key_1","confirmedEmptyComposer":true})
        )
        .is_ok());
    }

    #[test]
    fn tty_prompt_needs_explicit_user_review_of_visible_terminal_input() {
        assert!(validate_tty_prompt(&json!({"text":"hello","artifactIds":[],"idempotencyKey":"key_1","confirmedEmptyComposer":false})).is_err());
        assert!(validate_tty_prompt(&json!({"text":"hello","artifactIds":[],"idempotencyKey":"key_1","confirmedEmptyComposer":true})).is_ok());
    }

    #[test]
    fn tty_prompt_requires_one_bound_local_owner() {
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(
            &tmp.path().join("db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap();
        let project = store
            .create_project("Local", tmp.path().to_str().unwrap(), "local", None)
            .unwrap();
        let room = store
            .create_room(text(&project, "id"), "Room", &[json!("default")])
            .unwrap();
        let id = text(&room, "id");
        assert!(room_target_for_prompt(&store, id).is_err());
        store.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,updated_at) VALUES(?,'default','local','session_1',0)",[id]).unwrap();
        assert_eq!(
            room_target_for_prompt(&store, id)
                .unwrap()
                .resume
                .as_deref(),
            Some("session_1")
        );
        store.db.execute("UPDATE rooms SET source='hermes', external_profile='default', external_session_id='session_other' WHERE id=?", [id]).unwrap();
        assert!(room_target_for_prompt(&store, id).is_err());
        store.db.execute("UPDATE rooms SET source='ares', external_profile='', external_session_id='' WHERE id=?", [id]).unwrap();
        store.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,updated_at) VALUES(?,'default','remote','session_2',1)",[id]).unwrap();
        assert!(room_target_for_prompt(&store, id).is_err());
    }

    #[test]
    fn tty_prompt_rejects_paused_room_before_contacting_herdr() {
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(
            &tmp.path().join("db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap();
        let project = store
            .create_project("Local", tmp.path().to_str().unwrap(), "local", None)
            .unwrap();
        let room = store
            .create_room(text(&project, "id"), "Room", &[json!("default")])
            .unwrap();
        let id = text(&room, "id");
        store.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,updated_at) VALUES(?,'default','local','session_1',0)",[id]).unwrap();
        assert!(room_target_for_prompt(&store, id).is_ok());
        store
            .db
            .execute("UPDATE rooms SET status='paused' WHERE id=?", [id])
            .unwrap();
        assert_eq!(
            room_target_for_prompt(&store, id)
                .err()
                .unwrap()
                .to_string(),
            "room is not active"
        );
    }

    #[test]
    fn tty_receipt_is_durable_and_never_replays_an_uncertain_attempt() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("db");
        let artifacts = tmp.path().join("artifacts");
        let hermes = tmp.path().join("hermes");
        let store = Store::open(&path, &artifacts, hermes.clone()).unwrap();
        assert!(reserve_tty_receipt(&store, "room_1", "key_1", "hello", "session_1").unwrap());
        assert!(!reserve_tty_receipt(&store, "room_1", "key_1", "hello", "session_1").unwrap());
        assert!(reserve_tty_receipt(&store, "room_1", "key_1", "different", "session_1").is_err());
        drop(store);
        let store = Store::open(&path, &artifacts, hermes).unwrap();
        assert!(!reserve_tty_receipt(&store, "room_1", "key_1", "hello", "session_1").unwrap());
    }

    #[tokio::test]
    async fn tty_prompt_receipt_replay_precedes_idle_owner_lookup() {
        if std::env::var_os("ARES_TTY_RECEIPT_CHILD").is_none() {
            let tmp = tempfile::tempdir().unwrap();
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "http::tty_tests::tty_prompt_receipt_replay_precedes_idle_owner_lookup",
                    "--nocapture",
                ])
                .env("ARES_TTY_RECEIPT_CHILD", "1")
                .env("HOME", tmp.path())
                .env("XDG_CONFIG_HOME", tmp.path())
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(
            &tmp.path().join("db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap();
        let project = store
            .create_project("Local", tmp.path().to_str().unwrap(), "local", None)
            .unwrap();
        let room = store
            .create_room(text(&project, "id"), "Room", &[json!("default")])
            .unwrap();
        let id = text(&room, "id").to_owned();
        store.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,updated_at) VALUES(?,'default','local','session_1',0)", [&id]).unwrap();
        assert!(reserve_tty_receipt(&store, &id, "accepted_key", "hello", "session_1").unwrap());
        store.db.execute("UPDATE tty_prompt_receipts SET status='accepted' WHERE room_id=? AND idempotency_key='accepted_key'", [&id]).unwrap();
        assert!(reserve_tty_receipt(&store, &id, "uncertain_key", "hello", "session_1").unwrap());
        let app = App {
            store: Arc::new(Mutex::new(store)),
            token_hash: digest("test_token"),
            web_root: tmp.path().into(),
            bridge: None,
            mirror: None,
        };
        for (key, message, status, accepted) in [
            ("accepted_key", "hello", StatusCode::OK, true),
            ("uncertain_key", "hello", StatusCode::CONFLICT, false),
            ("accepted_key", "different", StatusCode::CONFLICT, false),
            ("uncertain_key", "different", StatusCode::CONFLICT, false),
        ] {
            let body = json!({"text":message,"artifactIds":[],"idempotencyKey":key,"confirmedEmptyComposer":true}).to_string();
            let request = Request::builder()
                .method("POST")
                .uri(format!("/v1/tty/rooms/{id}/prompt"))
                .header("authorization", "Bearer test_token")
                .header("content-length", body.len())
                .body(Body::from(body))
                .unwrap();
            let response = handle(State(app.clone()), request).await;
            assert_eq!(response.status(), status, "{key} / {message}");
            let value: Value =
                serde_json::from_slice(&to_bytes(response.into_body(), 4096).await.unwrap())
                    .unwrap();
            assert_eq!(value["accepted"] == true, accepted);
        }
    }

    #[tokio::test]
    async fn tty_prompt_fake_socket_cli_and_receipts_never_reinject() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::{fs::PermissionsExt, net::UnixListener};
        if std::env::var_os("ARES_TTY_FAKE_CHILD").is_none() {
            let tmp = tempfile::tempdir().unwrap();
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "http::tty_tests::tty_prompt_fake_socket_cli_and_receipts_never_reinject",
                    "--nocapture",
                ])
                .env("ARES_TTY_FAKE_CHILD", "1")
                .env("HOME", tmp.path())
                .env("XDG_CONFIG_HOME", tmp.path())
                .env(
                    "PATH",
                    format!(
                        "{}:{}",
                        tmp.path().join(".local/bin").display(),
                        std::env::var("PATH").unwrap_or_default()
                    ),
                )
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            return;
        }
        let home = PathBuf::from(std::env::var("HOME").unwrap());
        let bin = home.join(".local/bin");
        fs::create_dir_all(&bin).unwrap();
        let socket_dir = home.join("herdr/sessions/custom");
        fs::create_dir_all(&socket_dir).unwrap();
        let socket = socket_dir.join("herdr.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let marker = home.join("reject-prompt");
        let log = home.join("injections");
        let snapshot = json!({"ok":true,"result":{"snapshot":{
            "tabs":[{"tab_id":"w1:t1","label":"Room"}],
            "panes":[{"tab_id":"w1:t1","pane_id":"w1:p1","terminal_id":"term_1"}],
            "agents":[{"agent":"hermes","agent_session":{"kind":"id","value":"session_1","agent":"hermes","source":"herdr:hermes","profile":"default"},
                       "tab_id":"w1:t1","pane_id":"w1:p1","terminal_id":"term_1","agent_status":"working"}]
        }}});
        let script = format!(
            r#"#!/bin/sh
if [ "$1" = api ] && [ "$2" = snapshot ]; then
    printf '%s\n' '{snapshot}'
else
    exit 2
fi
"#
        );
        fs::write(bin.join("herdr"), script).unwrap();
        fs::set_permissions(bin.join("herdr"), fs::Permissions::from_mode(0o700)).unwrap();
        let marker_for_socket = marker.clone();
        let log_for_socket = log.clone();
        let prompt_server = std::thread::spawn(move || {
            for _ in 0..2 {
                let (mut stream, _) = listener.accept().unwrap();
                let mut line = String::new();
                BufReader::new(&stream).read_line(&mut line).unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                assert_eq!(request["method"], "agent.prompt");
                assert_eq!(request["params"]["target"], "w1:p1");
                fs::OpenOptions::new()
                    .append(true)
                    .create(true)
                    .open(&log_for_socket)
                    .unwrap()
                    .write_all(b"injected\n")
                    .unwrap();
                let response = if marker_for_socket.exists() {
                    json!({"id":request["id"],"error":{"code":"rejected","message":"not accepted"}})
                } else {
                    json!({"id":request["id"],"result":{"type":"agent_prompted","agent":{"terminal_id":"term_1"}}})
                };
                stream
                    .write_all(format!("{response}\n").as_bytes())
                    .unwrap();
            }
        });
        let store = Store::open(
            &home.join("db"),
            &home.join("artifacts"),
            home.join("hermes"),
        )
        .unwrap();
        let project = store
            .create_project("Local", home.to_str().unwrap(), "local", None)
            .unwrap();
        let room = store
            .create_room(text(&project, "id"), "Room", &[json!("default")])
            .unwrap();
        let id = text(&room, "id").to_owned();
        store.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,updated_at) VALUES(?,'default','local','session_1',0)", [&id]).unwrap();
        let app = App {
            store: Arc::new(Mutex::new(store)),
            token_hash: digest("test_token"),
            web_root: home.clone(),
            bridge: None,
            mirror: None,
        };
        let send = |key: &str, message: &str| {
            let body = json!({"text":message,"artifactIds":[],"idempotencyKey":key,"confirmedEmptyComposer":true}).to_string();
            Request::builder()
                .method("POST")
                .uri(format!("/v1/tty/rooms/{id}/prompt"))
                .header("authorization", "Bearer test_token")
                .header("content-length", body.len())
                .body(Body::from(body))
                .unwrap()
        };
        assert_eq!(
            handle(State(app.clone()), send("accepted_key", "secret"))
                .await
                .status(),
            StatusCode::ACCEPTED
        );
        assert_eq!(fs::read_to_string(&log).unwrap(), "injected\n");
        fs::write(&marker, "").unwrap();
        assert_eq!(
            handle(State(app.clone()), send("uncertain_key", "secret"))
                .await
                .status(),
            StatusCode::CONFLICT
        );
        assert_eq!(fs::read_to_string(&log).unwrap(), "injected\ninjected\n");
        prompt_server.join().unwrap();
        fs::remove_file(socket).unwrap();
        let accepted = handle(State(app.clone()), send("accepted_key", "secret")).await;
        assert_eq!(accepted.status(), StatusCode::OK);
        let reply: Value =
            serde_json::from_slice(&to_bytes(accepted.into_body(), 4096).await.unwrap()).unwrap();
        assert_eq!(reply["accepted"], true);
        for (key, message) in [
            ("uncertain_key", "secret"),
            ("accepted_key", "changed"),
            ("uncertain_key", "changed"),
        ] {
            assert_eq!(
                handle(State(app.clone()), send(key, message))
                    .await
                    .status(),
                StatusCode::CONFLICT
            );
        }
        assert_eq!(fs::read_to_string(log).unwrap(), "injected\ninjected\n");
    }

    #[tokio::test]
    async fn tty_prompt_http_rejects_images_without_echoing_text() {
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(
            &tmp.path().join("db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap();
        let app = App {
            store: Arc::new(Mutex::new(store)),
            token_hash: digest("test_token"),
            web_root: tmp.path().into(),
            bridge: None,
            mirror: None,
        };
        let secret = "private prompt text";
        let body =
            json!({"text":secret,"artifactIds":["image_1"],"idempotencyKey":"key_1"}).to_string();
        let request = Request::builder()
            .method("POST")
            .uri("/v1/tty/rooms/room_1/prompt")
            .header("authorization", "Bearer test_token")
            .header("content-length", body.len())
            .body(Body::from(body))
            .unwrap();
        let response = handle(State(app), request).await;
        assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let bytes = to_bytes(response.into_body(), 4096).await.unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains(secret));
    }

    #[tokio::test]
    async fn draft_post_accepts_artifact_ids_and_get_returns_scoped_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open(
            &dir.path().join("db"),
            &dir.path().join("artifacts"),
            dir.path().join("hermes"),
        )
        .unwrap();
        let project = store
            .create_project("Fixture", "/remote/project", "remote", None)
            .unwrap();
        let room = store
            .create_room(text(&project, "id"), "Room", &[json!("default")])
            .unwrap();
        let id = text(&room, "id");
        let artifact = store.store_artifact(&json!({"projectId":project["id"],"roomId":id,"name":"photo.png","mime":"image/png","author":"user"}), b"image").unwrap();
        let response = post(
            &store,
            &format!("/v1/drafts/{id}"),
            &json!({"text":"","artifactIds":[artifact["id"]]}),
        )
        .unwrap();
        let result: Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap())
                .unwrap();
        assert_eq!(result["draft"]["attachmentIds"], json!([artifact["id"]]));
        let response = get(&store, "/v1/drafts", &HashMap::new(), &HeaderMap::new()).unwrap();
        let result: Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap())
                .unwrap();
        assert_eq!(
            result["drafts"][0]["attachments"],
            json!([{"id":artifact["id"],"name":"photo.png","mime":"image/png"}])
        );
    }

    #[test]
    fn detects_only_the_matching_live_desktop_session() {
        let live = json!({"ok":true,"result":{"sessions":[
            {"id":"runtime_1","session_key":"stored_1"},
            {"id":"runtime_2","session_key":"stored_2"}
        ]}});
        assert!(desktop_owns_session(&live, "runtime_1"));
        assert!(desktop_owns_session(&live, "stored_2"));
        assert!(!desktop_owns_session(&live, "other"));
        assert!(!desktop_owns_session(&json!({"ok":false}), "stored_1"));
    }

    #[test]
    fn resolves_local_binding_and_rejects_remote_project() {
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(
            &tmp.path().join("db"),
            &tmp.path().join("artifacts"),
            tmp.path().join("hermes"),
        )
        .unwrap();
        let project = store
            .create_project("Local", tmp.path().to_str().unwrap(), "local", None)
            .unwrap();
        let room = store
            .create_room(text(&project, "id"), "Same title", &[json!("default")])
            .unwrap();
        assert!(room_target(&store, text(&room, "id"))
            .unwrap()
            .resume
            .is_none());
        store.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,updated_at) VALUES(?,'default','local','session_1',0)",[text(&room,"id")]).unwrap();
        assert_eq!(
            room_target(&store, text(&room, "id"))
                .unwrap()
                .resume
                .as_deref(),
            Some("session_1")
        );
        store.db.execute("UPDATE session_bindings SET node_id='remote',stored_session_id='remote::peer::session' WHERE room_id=?",[text(&room,"id")]).unwrap();
        assert!(room_target(&store, text(&room, "id"))
            .unwrap()
            .resume
            .is_none());
        store.db.execute("UPDATE rooms SET source='hermes',external_profile='default',external_session_id='external_1' WHERE id=?",[text(&room,"id")]).unwrap();
        assert_eq!(
            room_target(&store, text(&room, "id"))
                .unwrap()
                .resume
                .as_deref(),
            Some("external_1")
        );
        store
            .db
            .execute(
                "UPDATE rooms SET external_profile='remote::peer::default' WHERE id=?",
                [text(&room, "id")],
            )
            .unwrap();
        assert!(room_target(&store, text(&room, "id")).is_err());
        store
            .db
            .execute(
                "UPDATE rooms SET external_profile='default' WHERE id=?",
                [text(&room, "id")],
            )
            .unwrap();
        store
            .db
            .execute(
                "UPDATE projects SET node_id='peer-host' WHERE id=?",
                [text(&project, "id")],
            )
            .unwrap();
        assert!(room_target(&store, text(&room, "id")).is_err());
    }
}
