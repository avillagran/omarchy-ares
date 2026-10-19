//! Integration tests against the live local herdr (skipped when absent).
use libherdr::{local_socket_path, Client};

fn live_client() -> Option<Client> {
    let path = local_socket_path("default");
    Client::connect_local(&path).ok()
}

#[test]
fn snapshot_has_bounded_agents() {
    let Some(mut client) = live_client() else {
        eprintln!("no live herdr; skipped");
        return;
    };
    let result = client
        .call("session.snapshot", serde_json::json!({}))
        .expect("snapshot");
    let agents = result["snapshot"]["agents"].as_array().expect("agents list");
    assert!(agents.len() <= 512, "agent inventory must stay bounded");
    if let Some(agent) = agents.first() {
        assert!(agent["pane_id"].is_string(), "agents carry pane ids");
        assert!(agent["agent_status"].is_string(), "agents carry a status");
    }
}

#[test]
fn pane_read_returns_visible_text() {
    let Some(mut client) = live_client() else {
        eprintln!("no live herdr; skipped");
        return;
    };
    let result = client
        .call("session.snapshot", serde_json::json!({}))
        .expect("snapshot");
    let agents = result["snapshot"]["agents"].as_array().expect("agents list");
    let Some(pane) = agents.first().and_then(|a| a["pane_id"].as_str()) else {
        eprintln!("no agents; skipped");
        return;
    };
    // One-shot semantics: each call needs a fresh connection.
    let mut reader = live_client().expect("second connection");
    let read = reader
        .call(
            "pane.read",
            serde_json::json!({"pane_id": pane, "source": "visible", "format": "text", "lines": 5, "strip_ansi": true}),
        )
        .expect("pane.read");
    assert!(read["read"]["text"].is_string(), "pane.read returns text");
}

#[test]
fn subscription_starts() {
    let Some(client) = live_client() else {
        eprintln!("no live herdr; skipped");
        return;
    };
    let _sub = client
        .subscribe(vec![
            serde_json::json!({"type": "pane.updated"}),
            serde_json::json!({"type": "pane.created"}),
        ])
        .expect("subscription starts");
}
