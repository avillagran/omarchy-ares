use ares_server::{now, store::Store, text};
use rusqlite::params;
use serde_json::json;
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
        .create_project("Work", "/remote/work", "remote", None)
        .unwrap();
    let room = store
        .create_room(text(&project, "id"), "Work room", &[json!("default")])
        .unwrap();
    (
        dir,
        store,
        text(&project, "id").to_owned(),
        text(&room, "id").to_owned(),
    )
}

#[test]
fn control_tile_order_persists_and_rejects_invalid_project_ids() {
    let (dir, store, first, _) = fixture();
    let other = store.create_project("Other", "/remote/other", "remote", None).unwrap();
    let second = text(&other, "id").to_owned();
    let order = json!([second, first]);
    let saved = store.update_preferences(&json!({"controlTileOrder":order})).unwrap();
    assert_eq!(saved["controlTileOrder"], order);
    for invalid in [json!([first,first]), json!(["unknown-project"]),
                    json!([1]), json!("not-an-array")] {
        assert!(store.update_preferences(&json!({"controlTileOrder":invalid})).is_err());
        assert_eq!(store.preferences().unwrap()["controlTileOrder"], order);
    }
    drop(store);
    let reopened = Store::open(&dir.path().join("ares.db"),
        &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap();
    assert_eq!(reopened.preferences().unwrap()["controlTileOrder"], order);
}

#[test]
fn control_map_categories_persist_without_changing_projects_or_room_history() {
    let (dir, store, first, room) = fixture();
    let other = store.create_project("Other", "/remote/other", "remote", None).unwrap();
    let second = text(&other, "id").to_owned();
    let groups = json!([
        {"id":"priority", "name":"Priority", "color":"#A1b2C3"},
        {"id":"later", "name":"Later", "color":"#004488"}
    ]);
    let categories = json!({"groups":groups, "assignments":{first.clone():"priority", second.clone():"later"}});
    let order = json!([second, first]);
    let before = store.room(&room, false).unwrap();
    let saved = store.update_preferences(&json!({"controlMapCategories":categories, "controlTileOrder":order})).unwrap();
    assert_eq!(saved["controlMapCategories"], categories);
    assert_eq!(saved["controlTileOrder"], order);
    assert_eq!(store.room(&room, false).unwrap(), before);
    drop(store);
    let reopened = Store::open(&dir.path().join("ares.db"),
        &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap();
    assert_eq!(reopened.preferences().unwrap()["controlMapCategories"], categories);
    assert_eq!(reopened.preferences().unwrap()["controlTileOrder"], order);
    assert_eq!(reopened.room(&room, false).unwrap(), before);
}

#[test]
fn control_map_categories_reject_invalid_payloads_atomically() {
    let (dir, store, project, _) = fixture();
    let baseline = store.preferences().unwrap();
    let good = json!({"id":"active", "name":"Active", "color":"#1122aa"});
    let invalid = [
        json!(null), json!([]), json!({}),
        json!({"groups":[], "assignments":{}, "extra":true}),
        json!({"groups":{}, "assignments":{}}),
        json!({"groups":[], "assignments":[]}),
        json!({"groups":[good, good], "assignments":{}}),
        json!({"groups":[{"id":"bad space", "name":"A", "color":"#112233"}], "assignments":{}}),
        json!({"groups":[{"id":"a/b", "name":"A", "color":"#112233"}], "assignments":{}}),
        json!({"groups":[{"id":"x", "name":"  ", "color":"#112233"}], "assignments":{}}),
        json!({"groups":[{"id":"x", "name":"A", "color":"red"}], "assignments":{}}),
        json!({"groups":[{"id":"x", "name":"A", "color":"#112233", "extra":0}], "assignments":{}}),
        json!({"groups":[{"id":"x", "name":"A", "color":"#112233"}], "assignments":{"missing-project":"x"}}),
        json!({"groups":[], "assignments":{project.clone():"missing-group"}}),
        json!({"groups":[], "assignments":{project.clone():7}}),
        json!({"groups":[{"id":"x", "name":"A", "color":"#112233"}], "assignments":{project.clone():"missing-group"}}),
        json!({"groups":vec![json!({"id":"x", "name":"A", "color":"#112233"});65], "assignments":{}}),
        json!({"groups":[{"id":"x", "name":"A".repeat(121), "color":"#112233"}], "assignments":{}}),
    ];
    for category in invalid {
        assert!(store.update_preferences(&json!({"controlMapCategories":category, "homeQuotaVisible":false})).is_err(), "accepted {category}");
        assert_eq!(store.preferences().unwrap(), baseline);
        assert_eq!(Store::open(&dir.path().join("ares.db"), &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap().preferences().unwrap(), baseline);
    }
}

#[test]
fn removing_category_cleans_its_assignments_without_moving_rooms() {
    let (dir, store, first, room) = fixture();
    let second = text(&store.create_project("Other", "/remote/other", "remote", None).unwrap(), "id").to_owned();
    let groups = json!([{"id":"keep", "name":"Keep", "color":"#123456"},
        {"id":"remove", "name":"Remove", "color":"#654321"}]);
    store.update_preferences(&json!({"controlMapCategories":{"groups":groups,
        "assignments":{first.clone():"remove", second.clone():"keep"}}})).unwrap();
    let before = store.room(&room, false).unwrap();
    let saved = store.update_preferences(&json!({"controlMapCategories":{
        "groups":[{"id":"keep", "name":"Keep", "color":"#123456"}],
        "assignments":{first.clone():"remove", second.clone():"keep"}}})).unwrap();
    assert_eq!(saved["controlMapCategories"]["assignments"], json!({second.clone():"keep"}));
    assert_eq!(store.room(&room, false).unwrap(), before);
    drop(store);
    let reopened = Store::open(&dir.path().join("ares.db"), &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap();
    assert_eq!(reopened.preferences().unwrap()["controlMapCategories"]["assignments"], json!({second:"keep"}));
}

#[test]
fn project_map_color_is_validated_persisted_and_unique() {
    let (dir, store, project, _) = fixture();
    let updated = store.update_project_map_color(&project, "#12abCD").unwrap();
    assert_eq!(updated["mapColor"], "#12ABCD");
    let row = &store
        .rows("SELECT * FROM projects WHERE id=?", [&project])
        .unwrap()[0];
    assert_eq!(store.dto("projects", row).unwrap()["mapColor"], "#12ABCD");
    for invalid in [
        "red",
        "#abc",
        "#12abcd00",
        "#12345g",
        " #123456",
        "#123456 ",
    ] {
        assert!(
            store.update_project_map_color(&project, invalid).is_err(),
            "{invalid}"
        );
    }
    let other = store
        .create_project("Other", "/remote/other", "remote", None)
        .unwrap();
    assert!(store
        .update_project_map_color(text(&other, "id"), "#12ABCD")
        .is_err());
    assert_eq!(
        store.update_project_map_color(&project, "").unwrap()["mapColor"],
        ""
    );
    assert_eq!(
        store
            .update_project_map_color(text(&other, "id"), "#12ABCD")
            .unwrap()["mapColor"],
        "#12ABCD"
    );
    drop(store);
    let reopened = Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    assert_eq!(
        reopened
            .rows(
                "SELECT map_color FROM projects WHERE id=?",
                [text(&other, "id")]
            )
            .unwrap()[0]["map_color"],
        "#12ABCD"
    );
}

#[test]
fn project_map_position_validates_and_survives_reopen() {
    let (dir, store, project, room) = fixture();
    let updated = store.update_project_map_position(&project, 4700, 3500).unwrap();
    assert_eq!(updated["mapX"], 4700);
    assert_eq!(updated["mapY"], 3500);
    assert_eq!(updated["id"], project);
    let original_room = store.room(&room, false).unwrap();
    for (x, y) in [(-1, 1), (9601, 1), (1, 7201), (1, -1), (i64::MAX, 1), (1, i64::MIN)] {
        assert!(store.update_project_map_position(&project, x, y).is_err(), "{x:?}, {y:?}");
    }
    assert!(store.update_project_map_position("missing", 1, 2).is_err());
    assert_eq!(store.room(&room, false).unwrap(), original_room);
    drop(store);
    let reopened = Store::open(&dir.path().join("ares.db"), &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap();
    let project_row = reopened.one("SELECT * FROM projects WHERE id=?", [&project], "missing").unwrap();
    assert_eq!(reopened.dto("projects", &project_row).unwrap()["mapX"], 4700);
    assert_eq!(reopened.dto("projects", &project_row).unwrap()["mapY"], 3500);
    assert_eq!(reopened.room(&room, false).unwrap(), original_room);
}

#[test]
fn resync_preserves_project_map_position() {
    let (_dir, store, _, _) = fixture();
    let catalog = json!({"path":"/remote/position", "label":"Position",
        "repos":[{"groups":[{"sessions":[{"id":"session-map-position", "title":"Map session"}]}]}]});
    store.sync_hermes_project("default", &catalog, "remote", false).unwrap();
    let row = store.one("SELECT * FROM projects WHERE path=?", ["/remote/position"], "missing").unwrap();
    let id = text(&row, "id").to_owned();
    store.update_project_map_position(&id, 1000, 2000).unwrap();
    store.sync_hermes_project("default", &catalog, "remote", false).unwrap();
    let row = store.one("SELECT * FROM projects WHERE id=?", [&id], "missing").unwrap();
    assert_eq!(store.dto("projects", &row).unwrap()["mapX"], 1000);
    assert_eq!(store.dto("projects", &row).unwrap()["mapY"], 2000);
}

#[test]
fn legacy_project_migration_adds_nullable_map_position() {
    let dir = TempDir::new().unwrap();
    let db_path = dir.path().join("ares.db");
    let legacy = rusqlite::Connection::open(&db_path).unwrap();
    legacy.execute_batch("CREATE TABLE projects (id TEXT PRIMARY KEY, group_id TEXT, name TEXT NOT NULL, node_id TEXT NOT NULL, path TEXT NOT NULL, location_key TEXT NOT NULL UNIQUE, repo_identity TEXT NOT NULL DEFAULT '', map_color TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
        INSERT INTO projects(id,name,node_id,path,location_key,created_at) VALUES('legacy','Legacy','remote','/legacy','key',1);").unwrap();
    drop(legacy);
    let store = Store::open(&db_path, &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap();
    let row = store.one("SELECT * FROM projects WHERE id='legacy'", [], "missing").unwrap();
    assert!(store.dto("projects", &row).unwrap()["mapX"].is_null());
    assert!(store.dto("projects", &row).unwrap()["mapY"].is_null());
    assert_eq!(store.update_project_map_position("legacy", 640, 320).unwrap()["mapX"], 640);
    assert_eq!(store.update_project_map_position("legacy", 640, 320).unwrap()["mapY"], 320);
}

#[test]
fn control_room_is_one_special_room_and_never_a_prompt_target() {
    let (_dir, store, _, _) = fixture();
    let first = store.ensure_control_room().unwrap();
    let second = store.ensure_control_room().unwrap();
    assert_eq!(first["id"], second["id"]);
    assert_eq!(first["name"], "Control Room");
    assert_eq!(first["source"], "control");
    assert_eq!(first["controlIncluded"], false);
    assert!(store
        .accept_prompt(
            text(&first, "projectId"),
            text(&first, "id"),
            "default",
            "do this",
            "key-control",
            &[]
        )
        .is_err());
    assert!(store
        .lifecycle(
            text(&first, "projectId"),
            text(&first, "id"),
            "control-archive",
            "archive"
        )
        .is_err());
}

#[test]
fn included_rooms_surface_pending_work_and_exclusion_is_durable() {
    let (_dir, store, project, room) = fixture();
    let pending = store
        .accept_prompt(&project, &room, "default", "Review this", "key-work", &[])
        .unwrap();
    let items = store.control_items().unwrap();
    assert!(items
        .iter()
        .any(|item| item["roomId"] == room && item["kind"] == "execution"));
    assert_eq!(pending["status"], "queued");
    let excluded = store.set_control_included(&room, false).unwrap();
    assert_eq!(excluded["controlIncluded"], false);
    assert!(!store
        .control_items()
        .unwrap()
        .iter()
        .any(|item| item["roomId"] == room));
    assert_eq!(store.room(&room, false).unwrap()["control_included"], 0);
    assert!(store.set_control_included("missing-room", true).is_err());
}

#[test]
fn control_decision_is_audited_without_sending_to_the_work_room() {
    let (_dir, store, project, room) = fixture();
    let control = store.ensure_control_room().unwrap();
    let decision = store
        .record_control_decision(&room, "propose-prompt", "Check the test failure first")
        .unwrap();
    assert_eq!(decision["kind"], "control.decision");
    assert_eq!(decision["payload"]["roomId"], room);
    assert_eq!(decision["payload"]["action"], "propose-prompt");
    assert_eq!(
        store.events_after(0, 100, Some(&room)).unwrap()["events"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let history = store
        .events_after(0, 100, Some(text(&control, "id")))
        .unwrap();
    assert!(history["events"]
        .as_array()
        .unwrap()
        .iter()
        .any(|e| e["kind"] == "control.decision"));
    assert!(store
        .record_control_decision(&room, "execute", "do it")
        .is_err());
    assert!(store
        .record_control_decision(text(&control, "id"), "ask-user", "hi")
        .is_err());
    assert!(!project.is_empty());
}

#[test]
fn control_decisions_export_as_reviewable_laya_training_examples() {
    let (_dir, store, _, room) = fixture();
    let work_events_before = store.events_after(0, 100, Some(&room)).unwrap()["events"]
        .as_array()
        .unwrap()
        .len();
    store
        .record_control_decision(
            &room,
            "ask-user",
            "Need the deployment target before proceeding",
        )
        .unwrap();
    let examples = store.control_training_examples().unwrap();
    assert_eq!(examples.len(), 1);
    let example = &examples[0];
    assert_eq!(example["schema"], "ares-laya-decision-v1");
    assert_eq!(example["state"]["room_id"], room);
    assert_eq!(example["labels"]["action"], "ask-user");
    assert_eq!(example["labels"]["approved"], false);
    assert_eq!(example["questions"]["recommended_action"]["type"], "choice");
    assert_eq!(
        store.events_after(0, 100, Some(&room)).unwrap()["events"]
            .as_array()
            .unwrap()
            .len(),
        work_events_before
    );
}

#[test]
fn mirrored_herdr_processes_join_by_exact_session_id_and_respect_exclusion() {
    let (_dir, store, _, room) = fixture();
    store
        .db
        .execute(
            "UPDATE rooms SET source='hermes',external_session_id='session-one' WHERE id=?",
            [&room],
        )
        .unwrap();
    let agents = vec![
        json!({"agent":"hermes","agent_status":"working","agent_session":{"agent":"hermes","kind":"id","source":"herdr:hermes","value":"session-one"},"pane_id":"one"}),
        json!({"agent":"hermes","agent_status":"working","agent_session":{"agent":"hermes","kind":"id","source":"herdr:hermes","value":"session-other"},"pane_id":"other"}),
    ];
    let items = store.control_agent_items(&agents).unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["roomId"], room);
    assert_eq!(items[0]["status"], "working");
    store.set_control_included(&room, false).unwrap();
    assert!(store.control_agent_items(&agents).unwrap().is_empty());
    store.set_control_included(&room, true).unwrap();
    assert!(store
        .control_agent_items(&[
            json!({"agent":"hermes","agent_status":"idle","agent_session":{"agent":"hermes","kind":"id","source":"herdr:hermes","value":"session-one"}})
        ])
        .unwrap()
        .is_empty());
}

#[test]
fn unrelated_agent_cannot_claim_a_mirrored_hermes_room() {
    let (_dir, store, _, room) = fixture();
    store
        .db
        .execute(
            "UPDATE rooms SET source='hermes',external_session_id='shared-id' WHERE id=?",
            [&room],
        )
        .unwrap();
    for candidate in [
        json!({"agent":"codex","agent_status":"working","agent_session":{"agent":"codex","kind":"id","source":"herdr:codex","value":"shared-id"}}),
        json!({"agent":"hermes","agent_status":"working","agent_session":{"agent":"hermes","kind":"name","source":"herdr:hermes","value":"shared-id"}}),
        json!({"agent":"hermes","agent_status":"working","agent_session":{"agent":"hermes","kind":"id","source":"external","value":"shared-id"}}),
    ] {
        assert!(store.control_agent_items(&[candidate]).unwrap().is_empty());
    }
}

#[test]
fn control_logs_are_bounded_tool_tails_only_from_included_rooms() {
    let (_dir, store, project, room) = fixture();
    for (i, kind, text) in [
        (2, "message.user", "private request"),
        (3, "reasoning.delta", "private reasoning"),
        (4, "tool.result", "first line\nlast log line"),
        (5, "tool.result", "meaningful line\n}\n"),
    ] {
        store.db.execute(
            "INSERT INTO room_events(id,project_id,room_id,sequence,kind,author,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
            params![format!("event-{i}"), project, room, i, kind, "tool", json!({"text":text}).to_string(), now()],
        ).unwrap();
    }
    let logs = store.control_logs(&[room.as_str()]).unwrap();
    assert_eq!(logs[&room][0]["text"], "meaningful line");
    assert_eq!(logs[&room][1]["text"], "last log line");
    let serialized = logs.to_string();
    assert!(!serialized.contains("private request"));
    assert!(!serialized.contains("private reasoning"));
    store.set_control_included(&room, false).unwrap();
    assert!(store.control_logs(&[room.as_str()]).unwrap()[&room].is_null());
}

#[test]
fn remote_delegation_marks_only_the_exact_included_hermes_room_working() {
    let (_dir, store, _project, room) = fixture();
    store.db.execute(
        "UPDATE rooms SET source='hermes',external_profile='remote::host::default',external_session_id='remote::host::default::session-a' WHERE id=?",
        [&room],
    ).unwrap();
    let report =
        json!({"peer":"host","profile":"default","sessionId":"session-a","runningDelegations":1});
    assert_eq!(store.control_remote_peers().unwrap(), vec!["host"]);
    let found = store
        .control_remote_items(std::slice::from_ref(&report))
        .unwrap();
    assert_eq!(found.len(), 1);
    assert_eq!(found[0]["roomId"], room);
    assert_eq!(found[0]["status"], "working");
    assert_eq!(found[0]["delegationUnits"], 1);
    let mut wrong_peer = report.clone();
    wrong_peer["peer"] = json!("other");
    assert!(store
        .control_remote_items(&[wrong_peer])
        .unwrap()
        .is_empty());
    store.set_control_included(&room, false).unwrap();
    assert!(store.control_remote_items(&[report]).unwrap().is_empty());
    assert!(store.control_remote_peers().unwrap().is_empty());
}
