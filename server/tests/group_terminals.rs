use ares_server::{store::Store, terminals, text};
use serde_json::json;
use tempfile::TempDir;

#[test]
fn native_group_member_only_attaches_to_exact_live_profile_and_session() {
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
    store
        .db
        .execute(
            "UPDATE rooms SET source='hermes-native-group' WHERE id=?",
            [id],
        )
        .unwrap();
    store.db.execute("INSERT INTO native_groups(authority_host,desktop_key,room_id,desktop_room_id,revision) VALUES('local','id:group',?,'group',1)", [id]).unwrap();
    store.db.execute("INSERT INTO native_group_members(room_id,position,member_key,host,connection_id,connection_kind,profile,stored_session_id,resolved_session_id,resolution) VALUES(?,0,'local::builder','local','local','local','builder','old-session','current-session','resolved')", [id]).unwrap();
    let identity = terminals::group_member_identity(&store, id, "local::builder").unwrap();
    assert_eq!(identity["profile"], "builder");
    let inventory = terminals::normalize_inventory("local", "", &json!({"ok":true,"errors":[],"terminals":[{
        "paneId":"w1:p1","tabId":"w1:t1","terminalId":"term_123", "socketPath":"/home/user/.config/herdr/herdr.sock","herdrBin":"/usr/bin/herdr",
        "agentKind":"hermes","sessionId":"current-session","profile":"builder","herdrSession":"default","identitySource":"registry","status":"idle"
    }]})).unwrap();
    let tty = terminals::resolve_group_member(id, &identity, &inventory).unwrap();
    assert_eq!(tty["roomId"], id);
    assert_eq!(tty["memberKey"], "local::builder");
    assert_eq!(tty["agentName"], "term_123");
    assert_eq!(tty["sessionName"], "terminal");
    assert!(terminals::group_member_identity(&store, id, "local::cto").is_err());
    for (profile, session, kind) in [
        ("cto", "current-session", "hermes"),
        ("builder", "unknown", "hermes"),
        ("builder", "current-session", "claude"),
    ] {
        let mut wrong = inventory.clone();
        wrong["terminals"][0]["profile"] = json!(profile);
        wrong["terminals"][0]["sessionId"] = json!(session);
        wrong["terminals"][0]["agentKind"] = json!(kind);
        assert!(terminals::resolve_group_member(id, &identity, &wrong).is_err());
    }
    let mut partial = inventory.clone();
    partial["complete"] = json!(false);
    assert!(terminals::resolve_group_member(id, &identity, &partial).is_err());
    let mut duplicate = inventory.clone();
    let mut second = duplicate["terminals"][0].clone();
    second["terminalId"] = json!("term_other");
    duplicate["terminals"].as_array_mut().unwrap().push(second);
    assert!(terminals::resolve_group_member(id, &identity, &duplicate).is_err());
    let mut same_id = identity.clone();
    same_id["storedSessionId"] = json!("current-session");
    assert_eq!(
        terminals::resolve_group_member(id, &same_id, &inventory).unwrap()["agentName"],
        "term_123"
    );
    let mut both_live = inventory.clone();
    both_live["terminals"][0]["sessionId"] = json!("old-session");
    let mut tip = inventory["terminals"][0].clone();
    tip["id"] = json!("different-terminal-instance");
    tip["terminalId"] = json!("term_tip");
    both_live["terminals"].as_array_mut().unwrap().push(tip);
    assert!(terminals::resolve_group_member(id, &identity, &both_live).is_err());
    store
        .db
        .execute(
            "UPDATE native_group_members SET resolution='unsupported' WHERE room_id=?",
            [id],
        )
        .unwrap();
    assert!(terminals::group_member_identity(&store, id, "local::builder").is_err());
}

#[test]
fn group_member_rejects_unobserved_or_ambiguous_and_never_exposes_input() {
    let member = json!({"memberKey":"local::builder","host":"local","resolution":"resolved",
        "profile":"builder","storedSessionId":"stored","resolvedSessionId":"tip"});
    let base = json!({"host":"local","complete":true,"errors":[],"terminals":[{
        "id":"observed","paneId":"w1:p1","tabId":"w1:t1","terminalId":"term_123",
        "socketPath":"/home/user/.config/herdr/herdr.sock","herdrBin":"/usr/bin/herdr",
        "agentKind":"hermes","sessionId":"tip","profile":"builder","identitySource":"registry"}]});
    let tty = terminals::resolve_group_member("room", &member, &base).unwrap();
    assert_eq!(tty["readOnly"], true);
    let mut unknown = base.clone();
    unknown["terminals"][0]["identitySource"] = json!("guessed");
    assert!(terminals::resolve_group_member("room", &member, &unknown).is_err());
    let mut duplicate = base.clone();
    duplicate["terminals"]
        .as_array_mut()
        .unwrap()
        .push(base["terminals"][0].clone());
    assert!(terminals::resolve_group_member("room", &member, &duplicate).is_err());
}
