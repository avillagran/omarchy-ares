use ares_server::{cli::valid_token, http::parse_range, store::Store, text};
use serde_json::{json, Value};
use tempfile::TempDir;

fn fixture() -> (TempDir, Store, String, String) {
    let dir = TempDir::new().unwrap();
    let store = Store::open(
        &dir.path().join("ares.db"),
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
    (
        dir,
        store,
        text(&project, "id").into(),
        text(&room, "id").into(),
    )
}

#[test]
fn token_validation() {
    for n in [32, 43, 256] {
        assert!(valid_token(&"a".repeat(n)));
    }
    for n in [0, 31, 257] {
        assert!(!valid_token(&"a".repeat(n)));
    }
    assert!(valid_token(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789-abcdefghijklmnopqrstuvwxyz"
    ));
    for bad in [' ', '\n', '=', '/', '+', 'é'] {
        assert!(!valid_token(&format!("{}{}", "a".repeat(32), bad)));
    }
}

#[test]
fn ranges() {
    assert_eq!(parse_range("", 10), Ok((0, 9)));
    assert_eq!(parse_range("bytes=0-0", 10), Ok((0, 0)));
    assert_eq!(parse_range("bytes=2-", 10), Ok((2, 9)));
    assert_eq!(parse_range("bytes=2-999", 10), Ok((2, 9)));
    for bad in [
        "bytes=-3",
        "bytes=10-",
        "bytes=3-2",
        "bytes=0-1,3-4",
        "bytes= 0-1",
        "bytes=+0-1",
        "items=0-1",
        "bytes=0-x",
    ] {
        assert!(parse_range(bad, 10).is_err(), "{bad}");
    }
    assert!(parse_range("", 0).is_err());
}

#[test]
fn schema_and_dto_contract() {
    let (_dir, s, p, r) = fixture();
    let tables = s
        .rows("SELECT name FROM sqlite_master WHERE type='table'", [])
        .unwrap();
    assert_eq!(tables.len(), 22); // Twenty-one application tables plus sqlite_sequence.
    for name in [
        "native_group_authorities",
        "native_groups",
        "native_group_members",
    ] {
        assert!(tables.iter().any(|row| row["name"] == name));
    }
    assert!(tables.iter().any(|row| row["name"] == "terminal_bindings"));
    assert_eq!(
        s.db.query_row("PRAGMA journal_mode", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "wal"
    );
    assert_eq!(
        s.db.query_row("PRAGMA foreign_keys", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
    let snapshot = s.snapshot(None, None).unwrap();
    let project = &snapshot["projects"][0];
    assert_eq!(project["id"], p);
    assert!(project.get("node").is_some());
    assert!(project.get("nodeId").is_none());
    let room = &snapshot["rooms"][0];
    assert_eq!(room["id"], r);
    assert_eq!(room["participants"], json!(["default"]));
    assert_eq!(room["revision"], 2);
    assert_eq!(room["eventCount"], 1);
    for key in [
        "projectId",
        "memoryRelease",
        "externalSessionId",
        "externalActivityAt",
        "createdAt",
        "deletedAt",
    ] {
        assert!(room.get(key).is_some(), "{key}");
    }
    assert!(room.get("project_id").is_none());
    assert_eq!(snapshot["events"][0]["kind"], "room.created");
    assert_eq!(snapshot["version"], 3);
}

#[test]
fn idempotency_conflict_and_transactional_rollback() {
    let (_dir, s, p, r) = fixture();
    let first = s
        .accept_prompt(&p, &r, "default", " hello ", "key", &[])
        .unwrap();
    let second = s
        .accept_prompt(&p, &r, "default", "hello", "key", &[])
        .unwrap();
    assert_eq!(first, second);
    assert_eq!(first["status"], "queued");
    assert_eq!(
        s.accept_prompt(&p, &r, "default", "changed", "key", &[])
            .unwrap_err()
            .to_string(),
        "idempotency conflict"
    );
    assert!(s
        .accept_prompt(&p, &r, "default", "rollback", "other", &[json!("missing")])
        .is_err());
    let snapshot = s.snapshot(Some(&r), None).unwrap();
    assert_eq!(snapshot["executions"].as_array().unwrap().len(), 1);
    assert_eq!(snapshot["events"].as_array().unwrap().len(), 3);
    assert_eq!(
        s.db.query_row("SELECT COUNT(*) FROM command_receipts", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn artifact_delivery_and_pagination() {
    let (_dir, s, p, r) = fixture();
    let artifact = s
        .store_artifact(
            &json!({"projectId":p,"roomId":r,"name":"../a.png","mime":"image/png","author":"user"}),
            b"image bytes",
        )
        .unwrap();
    assert_eq!(artifact["name"], "a.png");
    let ids = [artifact["id"].clone(), artifact["id"].clone()];
    s.accept_prompt(&p, &r, "default", "image", "image-key", &ids)
        .unwrap();
    assert_eq!(
        s.db.query_row("SELECT COUNT(*) FROM execution_artifacts", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
    for i in 0..30 {
        s.accept_prompt(&p, &r, "default", "more", &format!("key-{i}"), &[])
            .unwrap();
    }
    let compact = s.snapshot(None, Some(&r)).unwrap();
    assert_eq!(compact["events"].as_array().unwrap().len(), 40);
    assert_eq!(compact["eventPage"]["hasOlder"], true);
    let before = s
        .events_before(
            &r,
            compact["eventPage"]["earliestCursor"].as_i64().unwrap(),
            100,
        )
        .unwrap();
    assert!(!before["events"].as_array().unwrap().is_empty());
    let metadata = s
        .one(
            "SELECT * FROM artifacts WHERE id=?",
            [text(&artifact, "id")],
            "missing",
        )
        .unwrap();
    assert_eq!(s.read_artifact(&metadata, 1, 4).unwrap(), b"mage");
}

#[test]
fn lifecycle_and_restart_recovery() {
    let (dir, s, p, r) = fixture();
    let paused = s.lifecycle(&p, &r, "pause", "pause").unwrap();
    assert_eq!(paused, s.lifecycle(&p, &r, "pause", "pause").unwrap());
    assert!(s
        .accept_prompt(&p, &r, "default", "hello", "send", &[])
        .is_err());
    s.lifecycle(&p, &r, "resume", "resume").unwrap();
    s.accept_prompt(&p, &r, "default", "hello", "send", &[])
        .unwrap();
    assert_eq!(
        s.lifecycle(&p, &r, "delete", "delete")
            .unwrap_err()
            .to_string(),
        "room has unfinished executions"
    );
    drop(s);
    let s = Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    let state = s.snapshot(None, None).unwrap();
    assert_eq!(state["executions"][0]["status"], "interrupted");
    s.lifecycle(&p, &r, "delete", "delete").unwrap();
    assert!(s.snapshot(None, None).unwrap()["rooms"]
        .as_array()
        .unwrap()
        .is_empty());
    assert_eq!(
        s.lifecycle(&p, &r, "restore", "restore").unwrap()["status"],
        "paused"
    );
}

#[test]
fn model_switch_sidecars_and_unbinding() {
    let (dir, s, _p, r) = fixture();
    let profile = dir.path().join("hermes/profiles/default");
    std::fs::create_dir_all(&profile).unwrap();
    std::fs::write(
        profile.join("config.yaml"),
        "# keep\nmodel:\n  default: old\n  provider: old-provider\nother: yes\n",
    )
    .unwrap();
    s.db.execute("INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,runtime_session_id,updated_at) VALUES(?,'default','local','stored','runtime',0)",[&r]).unwrap();
    let bot = s
        .update_bot(
            "default",
            &json!({"model":"new","provider":"new-provider","name":"Local"}),
        )
        .unwrap();
    assert_eq!(
        bot["unboundSessions"],
        json!([{"storedSessionId":"stored","runtimeSessionId":"runtime"}])
    );
    assert_eq!(
        ares_server::sidecar::profile_model_pair(&profile),
        Some(("new-provider".into(), "new".into()))
    );
    let sidecar: Value =
        serde_json::from_slice(&std::fs::read(profile.join("ares.json")).unwrap()).unwrap();
    assert_eq!(sidecar["name"], "Local");
    assert_eq!(sidecar["face_shape"], "diamond");
    assert_eq!(
        s.models_catalog().unwrap(),
        vec![json!({"model":"new","provider":"new-provider"})]
    );
}
