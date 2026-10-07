use ares_server::terminals::{normalize_inventory, select_session};
use ares_server::{store::Store, terminals, text};
use serde_json::json;
use tempfile::TempDir;

fn payload(kind: &str) -> serde_json::Value {
    json!({"ok":true,"errors":[],"terminals":[{
        "paneId":"w1:p1","tabId":"w1:t1","terminalId":"term_123",
        "socketPath":"/home/user/.config/herdr/herdr.sock","herdrBin":"/usr/bin/herdr",
        "agentKind":kind,"sessionId":"chat-a","profile":"default","herdrSession":"default",
        "identitySource":"verified","workspace":"Project","label":"Chat","status":"idle"
    }]})
}

#[test]
fn observed_only_terminal_cannot_be_persisted_as_session_owner() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(
        &dir.path().join("db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let project = store
        .create_project("Peer", "/remote/work", "node", None)
        .unwrap();
    let room = store
        .create_room(text(&project, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id");
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='remote::node::default',external_session_id='remote::node::default::chat-a' WHERE id=?", [id]).unwrap();
    let mut data = payload("hermes");
    data["terminals"][0]["identitySource"] = json!("observed-only");
    let inventory = normalize_inventory("node", "user@node", &data).unwrap();
    let candidate = text(&inventory["terminals"][0], "id");
    assert!(terminals::bind(&store, id, &inventory, candidate, true).is_err());
    assert!(terminals::binding(&store, id).unwrap().is_none());
    assert!(store
        .rows(
            "SELECT * FROM room_events WHERE room_id=? AND kind='terminal.associated'",
            [id]
        )
        .unwrap()
        .is_empty());
}

fn remote_binding_fixture() -> (TempDir, Store, String, serde_json::Value) {
    let dir = TempDir::new().unwrap();
    let store = Store::open(
        &dir.path().join("db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let project = store
        .create_project("Peer", "/remote/work", "node", None)
        .unwrap();
    let room = store
        .create_room(text(&project, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id").to_owned();
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='remote::node::default',external_session_id='remote::node::default::chat-a' WHERE id=?", [&id]).unwrap();
    let mut data = payload("hermes");
    data["terminals"][0]["identitySource"] = json!("verified");
    let inventory = normalize_inventory("node", "user@node", &data).unwrap();
    (dir, store, id, inventory)
}

fn assert_binding_denied_without_mutation(
    store: &Store,
    room: &str,
    inventory: &serde_json::Value,
) {
    let previous = terminals::binding(store, room).unwrap();
    let events = store
        .rows("SELECT * FROM room_events WHERE room_id=?", [room])
        .unwrap();
    let candidate = text(&inventory["terminals"][0], "id");
    assert!(terminals::bind(store, room, inventory, candidate, true).is_err());
    assert_eq!(terminals::binding(store, room).unwrap(), previous);
    assert_eq!(
        store
            .rows("SELECT * FROM room_events WHERE room_id=?", [room])
            .unwrap(),
        events
    );
}

#[test]
fn mismatched_session_or_profile_cannot_bind_or_replace_saved_owner() {
    let (_dir, store, room, inventory) = remote_binding_fixture();
    let mut wrong_session = inventory.clone();
    wrong_session["terminals"][0]["sessionId"] = json!("chat-b");
    assert_binding_denied_without_mutation(&store, &room, &wrong_session);
    let mut wrong_profile = inventory.clone();
    wrong_profile["terminals"][0]["profile"] = json!("other");
    assert_binding_denied_without_mutation(&store, &room, &wrong_profile);
    terminals::bind(
        &store,
        &room,
        &inventory,
        text(&inventory["terminals"][0], "id"),
        true,
    )
    .unwrap();
    assert_binding_denied_without_mutation(&store, &room, &wrong_session);
    assert_binding_denied_without_mutation(&store, &room, &wrong_profile);
}

#[test]
fn incomplete_or_ambiguous_inventory_cannot_bind() {
    let (_dir, store, room, inventory) = remote_binding_fixture();
    let mut incomplete = inventory.clone();
    incomplete["complete"] = json!(false);
    assert_binding_denied_without_mutation(&store, &room, &incomplete);
    let mut errors = inventory.clone();
    errors["errors"] = json!(["one pane unavailable"]);
    assert_binding_denied_without_mutation(&store, &room, &errors);
    let mut duplicate = inventory.clone();
    duplicate["terminals"]
        .as_array_mut()
        .unwrap()
        .push(inventory["terminals"][0].clone());
    assert_binding_denied_without_mutation(&store, &room, &duplicate);
    let mut second_owner = inventory.clone();
    let mut other = inventory["terminals"][0].clone();
    other["id"] = json!("different-terminal");
    second_owner["terminals"]
        .as_array_mut()
        .unwrap()
        .push(other);
    assert_binding_denied_without_mutation(&store, &room, &second_owner);
}

#[test]
fn verified_exact_remote_identity_persists_one_binding() {
    let (_dir, store, room, inventory) = remote_binding_fixture();
    let candidate = text(&inventory["terminals"][0], "id");
    let saved = terminals::bind(&store, &room, &inventory, candidate, true).unwrap();
    assert_eq!(saved["host"], "node");
    assert_eq!(saved["terminal_key"], candidate);
    assert_eq!(
        terminals::resolve_bound(&store, &room, &inventory)
            .unwrap()
            .unwrap()["sessionId"],
        "chat-a"
    );
}

#[test]
fn locally_verified_registry_identity_can_bind_exact_session() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(
        &dir.path().join("db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let project = store
        .create_project("Local", dir.path().to_str().unwrap(), "local", None)
        .unwrap();
    let room = store
        .create_room(text(&project, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id");
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='default',external_session_id='chat-a' WHERE id=?", [id]).unwrap();
    let mut data = payload("hermes");
    data["terminals"][0]["identitySource"] = json!("registry");
    let inventory = normalize_inventory("local", "", &data).unwrap();
    assert!(terminals::bind(
        &store,
        id,
        &inventory,
        text(&inventory["terminals"][0], "id"),
        true
    )
    .is_ok());
}

#[test]
fn saved_binding_cannot_resolve_after_session_identity_changes() {
    let (_dir, store, room, inventory) = remote_binding_fixture();
    let candidate = text(&inventory["terminals"][0], "id");
    terminals::bind(&store, &room, &inventory, candidate, true).unwrap();
    let mut observed = inventory.clone();
    observed["terminals"][0]["identitySource"] = json!("observed-only");
    assert!(terminals::resolve_bound(&store, &room, &observed).is_err());
    let mut other_session = inventory.clone();
    other_session["terminals"][0]["sessionId"] = json!("chat-b");
    assert!(terminals::resolve_bound(&store, &room, &other_session).is_err());
    store
        .db
        .execute(
            "UPDATE rooms SET external_session_id='remote::node::default::chat-b' WHERE id=?",
            [&room],
        )
        .unwrap();
    assert!(terminals::resolve_bound(&store, &room, &inventory).is_err());
}

#[test]
fn native_groups_never_resolve_or_bind_as_one_terminal_even_with_stale_binding() {
    let dir = TempDir::new().unwrap();
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
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='default',external_session_id='chat-a' WHERE id=?", [id]).unwrap();
    let inventory = normalize_inventory("local", "", &payload("hermes")).unwrap();
    let candidate = text(&inventory["terminals"][0], "id");
    terminals::bind(&store, id, &inventory, candidate, true).unwrap();
    for source in ["hermes-native-group", "hermes-native-member"] {
        store
            .db
            .execute("UPDATE rooms SET source=? WHERE id=?", [source, id])
            .unwrap();
        assert!(terminals::resolve_room(&store, id, &inventory).is_err());
        assert!(terminals::binding(&store, id).is_err());
        assert!(terminals::bind(&store, id, &inventory, candidate, true).is_err());
        assert!(
            terminals::resolve_or_open(&store, id, &inventory, true, |_, _| panic!(
                "Groups must never launch a scalar session"
            ))
            .is_err()
        );
        assert!(store.set_control_included(id, true).is_err());
    }
}

#[test]
fn historical_open_is_opt_in_reuses_existing_and_verifies_returned_identity() {
    let dir = TempDir::new().unwrap();
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
        .create_room(text(&project, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id");
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='default',external_session_id='chat-a' WHERE id=?", [id]).unwrap();
    let empty = json!({"host":"local","terminals":[],"errors":[],"complete":true});
    let existing = normalize_inventory("local", "", &payload("hermes")).unwrap();
    assert!(
        terminals::resolve_or_open(&store, id, &empty, false, |_, _| panic!(
            "Passive view must not launch"
        ))
        .is_err()
    );
    let reused = terminals::resolve_or_open(&store, id, &existing, true, |_, _| {
        panic!("Existing owner must be reused")
    })
    .unwrap();
    assert_eq!(reused["tty"]["agentName"], "term_123");
    let opened = terminals::resolve_or_open(&store, id, &empty, true, |host, request| {
        assert_eq!(host, "local");
        assert_eq!(request["sessionId"], "chat-a");
        Ok(existing.clone())
    })
    .unwrap();
    assert_eq!(opened["tty"]["agentName"], "term_123");
    let wrong = normalize_inventory("local", "", &payload("claude")).unwrap();
    assert!(terminals::resolve_or_open(&store, id, &empty, true, |_, _| Ok(wrong)).is_err());
    let partial = json!({"host":"local","terminals":[],"errors":["unavailable"],"complete":false});
    assert!(
        terminals::resolve_or_open(&store, id, &partial, true, |_, _| panic!(
            "Incomplete inventory must not launch"
        ))
        .is_err()
    );
}

#[test]
fn historical_open_request_uses_stored_owner_not_display_names() {
    let dir = TempDir::new().unwrap();
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
        .create_room(text(&project, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id");
    assert!(terminals::historical_open_request(&store, id).is_err());
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='default',external_session_id='telegram-id',alias='Ohm' WHERE id=?", [id]).unwrap();
    let request = terminals::historical_open_request(&store, id).unwrap();
    assert_eq!(request["profile"], "default");
    assert_eq!(request["sessionId"], "telegram-id");
    assert_eq!(request["title"], "Ohm");
    store
        .db
        .execute(
            "UPDATE projects SET node_id='node' WHERE id=?",
            [text(&project, "id")],
        )
        .unwrap();
    assert!(terminals::historical_open_request(&store, id).is_err());
    store.db.execute("UPDATE rooms SET external_profile='remote::node::coder',external_session_id='remote::node::coder::telegram-id' WHERE id=?", [id]).unwrap();
    let remote = terminals::historical_open_request(&store, id).unwrap();
    assert_eq!(remote["profile"], "coder");
    assert_eq!(remote["sessionId"], "telegram-id");
    store
        .db
        .execute("UPDATE rooms SET status='paused' WHERE id=?", [id])
        .unwrap();
    assert!(terminals::historical_open_request(&store, id).is_err());
}

#[test]
fn historical_open_refuses_pending_execution_and_explicit_association() {
    let dir = TempDir::new().unwrap();
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
        .create_room(text(&project, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id");
    let execution = store
        .accept_prompt(text(&project, "id"), id, "default", "fixture", "key", &[])
        .unwrap();
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='default',external_session_id='chat-a' WHERE id=?", [id]).unwrap();
    assert!(terminals::historical_open_request(&store, id).is_err());
    store
        .finish_execution(text(&execution, "id"), "completed", "", "")
        .unwrap();
    assert!(terminals::historical_open_request(&store, id).is_ok());
    let inventory = normalize_inventory("local", "", &payload("hermes")).unwrap();
    terminals::bind(
        &store,
        id,
        &inventory,
        text(&inventory["terminals"][0], "id"),
        true,
    )
    .unwrap();
    assert!(terminals::historical_open_request(&store, id).is_err());
}

#[test]
fn terminal_identity_is_host_scoped_and_agent_agnostic() {
    for kind in ["hermes", "claude", "kimi"] {
        let local = normalize_inventory("local", "", &payload(kind)).unwrap();
        let remote = normalize_inventory("node", "user@node", &payload(kind)).unwrap();
        assert_ne!(local["terminals"][0]["id"], remote["terminals"][0]["id"]);
        assert_eq!(
            select_session(&remote, kind, "default", "chat-a")
                .unwrap()
                .unwrap()["agentKind"],
            kind
        );
        assert!(select_session(&remote, "codex", "default", "chat-a")
            .unwrap()
            .is_none());
        assert!(select_session(&remote, kind, "other", "chat-a")
            .unwrap()
            .is_none());
    }
}

#[test]
fn moved_panes_keep_instance_identity_but_recreated_terminals_do_not() {
    let first = normalize_inventory("local", "", &payload("kimi")).unwrap();
    let mut moved = payload("kimi");
    moved["terminals"][0]["paneId"] = json!("w2:p7");
    moved["terminals"][0]["tabId"] = json!("w2:t4");
    let second = normalize_inventory("local", "", &moved).unwrap();
    assert_eq!(first["terminals"][0]["id"], second["terminals"][0]["id"]);
    moved["terminals"][0]["terminalId"] = json!("term_new");
    let third = normalize_inventory("local", "", &moved).unwrap();
    assert_ne!(first["terminals"][0]["id"], third["terminals"][0]["id"]);
}

#[test]
fn descriptor_uses_the_same_terminal_transport_for_every_host() {
    assert!(terminals::inventory("bad host;", false).is_err());
    for (host, ssh) in [("local", ""), ("node", "user@node")] {
        let inventory = normalize_inventory(host, ssh, &payload("kimi")).unwrap();
        let tty = terminals::descriptor("room-a", &inventory["terminals"][0]);
        assert_eq!(tty["sessionName"], "terminal");
        assert_eq!(tty["agentName"], "term_123");
        assert_eq!(tty["connectionState"], "resolved");
        assert_eq!(tty["sshTarget"], ssh);
    }
}

#[test]
fn explicit_binding_is_durable_host_scoped_and_never_retargets_on_missing_terminal() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let p = store
        .create_project("Project", "/remote/work", "node", None)
        .unwrap();
    let room = store
        .create_room(text(&p, "id"), "Chat", &[json!("default")])
        .unwrap();
    let id = text(&room, "id");
    let inventory = normalize_inventory("node", "user@node", &payload("claude")).unwrap();
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='remote::node::default',external_session_id='remote::node::default::chat-a' WHERE id=?", [id]).unwrap();
    assert!(terminals::resolve_room(&store, id, &inventory)
        .unwrap()
        .is_none());
    let hermes_inventory = normalize_inventory("node", "user@node", &payload("hermes")).unwrap();
    assert_eq!(
        terminals::resolve_room(&store, id, &hermes_inventory)
            .unwrap()
            .unwrap()["agentKind"],
        "hermes"
    );
    let candidate = text(&hermes_inventory["terminals"][0], "id");
    assert!(terminals::bind(
        &store,
        id,
        &inventory,
        text(&inventory["terminals"][0], "id"),
        true
    )
    .is_err());
    assert!(terminals::bind(&store, id, &hermes_inventory, candidate, false).is_err());
    let wrong = normalize_inventory("other", "user@node", &payload("hermes")).unwrap();
    assert!(terminals::bind(&store, id, &wrong, text(&wrong["terminals"][0], "id"), true).is_err());
    terminals::bind(&store, id, &hermes_inventory, candidate, true).unwrap();
    assert!(store
        .accept_prompt(
            text(&p, "id"),
            id,
            "default",
            "must not route to Hermes",
            "blocked",
            &[]
        )
        .is_err());
    assert_eq!(
        terminals::binding(&store, id).unwrap().unwrap()["terminal_key"],
        candidate
    );
    let target = terminals::resolve_bound(&store, id, &hermes_inventory)
        .unwrap()
        .unwrap();
    assert_eq!(target["agentKind"], "hermes");
    let absent = json!({"host":"node","terminals":[],"errors":[]});
    assert!(terminals::resolve_bound(&store, id, &absent).is_err());
    assert_eq!(store.rows("SELECT * FROM executions", []).unwrap().len(), 0);
    drop(store);
    let reopened = Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    assert!(terminals::resolve_bound(&reopened, id, &hermes_inventory)
        .unwrap()
        .is_some());
}

#[test]
fn partial_inventory_cannot_prove_a_unique_automatic_owner() {
    let mut data = payload("hermes");
    data["errors"] = json!([{"error":"one session unavailable"}]);
    let inventory = normalize_inventory("local", "", &data).unwrap();
    assert!(select_session(&inventory, "hermes", "default", "chat-a").is_err());
}

#[test]
fn ares_origin_uses_its_stored_identity_in_any_named_session() {
    let dir = TempDir::new().unwrap();
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
        .create_room(text(&project, "id"), "QA", &[json!("default")])
        .unwrap();
    store
        .bind_session(text(&room, "id"), "default", "local", "chat-a", "")
        .unwrap();
    let mut data = payload("hermes");
    data["terminals"][0]["socketPath"] =
        json!("/home/user/.config/herdr/sessions/other/herdr.sock");
    let inventory = normalize_inventory("local", "", &data).unwrap();
    let target = terminals::resolve_room(&store, text(&room, "id"), &inventory)
        .unwrap()
        .unwrap();
    assert_eq!(target["socketPath"], data["terminals"][0]["socketPath"]);
}

#[test]
fn association_rejects_pending_executions_and_start_is_fenced() {
    let dir = TempDir::new().unwrap();
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
        .create_room(text(&project, "id"), "QA", &[json!("default")])
        .unwrap();
    let id = text(&room, "id");
    let execution = store
        .accept_prompt(text(&project, "id"), id, "default", "fixture", "key", &[])
        .unwrap();
    store.db.execute("UPDATE rooms SET source='hermes',external_profile='default',external_session_id='chat-a' WHERE id=?", [id]).unwrap();
    let inventory = normalize_inventory("local", "", &payload("hermes")).unwrap();
    let candidate = text(&inventory["terminals"][0], "id");
    assert!(terminals::bind(&store, id, &inventory, candidate, true).is_err());
    assert!(store.start_execution(text(&execution, "id")).unwrap());
    assert!(terminals::bind(&store, id, &inventory, candidate, true).is_err());
    store
        .finish_execution(text(&execution, "id"), "completed", "", "")
        .unwrap();
    terminals::bind(&store, id, &inventory, candidate, true).unwrap();
    // Defensive fence for a legacy queue restored after association.
    store
        .db
        .execute(
            "UPDATE executions SET status='queued' WHERE id=?",
            [text(&execution, "id")],
        )
        .unwrap();
    assert!(!store.start_execution(text(&execution, "id")).unwrap());
}

#[test]
fn concurrent_execution_start_and_association_never_transfer_a_pending_room() {
    use std::sync::{Arc, Barrier};
    let dir = TempDir::new().unwrap();
    let db = dir.path().join("db");
    let artifacts = dir.path().join("artifacts");
    let hermes = dir.path().join("hermes");
    let first = Store::open(&db, &artifacts, hermes.clone()).unwrap();
    let second = Store::open(&db, &artifacts, hermes).unwrap();
    let project = first
        .create_project("QA", dir.path().to_str().unwrap(), "local", None)
        .unwrap();
    let room = first
        .create_room(text(&project, "id"), "QA", &[json!("default")])
        .unwrap();
    let id = text(&room, "id").to_owned();
    let execution = first
        .accept_prompt(text(&project, "id"), &id, "default", "fixture", "key", &[])
        .unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let start_barrier = barrier.clone();
    let worker = std::thread::spawn(move || {
        start_barrier.wait();
        first.start_execution(text(&execution, "id")).unwrap()
    });
    let inventory = normalize_inventory("local", "", &payload("claude")).unwrap();
    barrier.wait();
    assert!(terminals::bind(
        &second,
        &id,
        &inventory,
        text(&inventory["terminals"][0], "id"),
        true
    )
    .is_err());
    assert!(worker.join().unwrap());
    assert!(terminals::binding(&second, &id).unwrap().is_none());
}
