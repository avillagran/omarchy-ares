use ares_server::{store::Store, text};
use rusqlite::params;
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

fn image(store: &Store, project: &str, room: &str, name: &str) -> Value {
    store.store_artifact(&json!({"projectId":project,"roomId":room,"name":name,"mime":"image/png","author":"user"}), b"image bytes").unwrap()
}

#[test]
fn reads_scoped_attachments_from_existing_sqlite_drafts_without_leaking_paths() {
    let (dir, store, project, room) = fixture();
    let artifact = image(&store, &project, &room, "photo.png");
    let id = text(&artifact, "id");
    store.db.execute("INSERT INTO drafts(device_id,room_id,text,attachment_ids,updated_at) VALUES('desktop',?,?,?,1)", params![room, "draft", json!([id]).to_string()]).unwrap();
    assert_eq!(
        store.device_drafts("desktop").unwrap()[0],
        json!({
            "roomId":room,"text":"draft","updatedAt":1.0,
            "attachmentIds":[id],"attachments":[{"id":id,"name":"photo.png","mime":"image/png"}]
        })
    );
    drop(store);
    let reopened = Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap();
    assert_eq!(
        reopened.device_drafts("desktop").unwrap()[0]["attachmentIds"],
        json!([id])
    );
    assert!(reopened.device_drafts("another-device").unwrap().is_empty());
}

#[test]
fn saves_image_only_draft_and_preserves_attachments_on_legacy_text_updates() {
    let (_dir, store, project, room) = fixture();
    let first = image(&store, &project, &room, "one.png");
    let second = image(&store, &project, &room, "two.png");
    let ids = json!([first["id"], second["id"], first["id"]]);
    let saved = store
        .save_draft_with_artifacts("desktop", &room, "", Some(ids.as_array().unwrap()))
        .unwrap();
    assert_eq!(saved["attachmentIds"], json!([first["id"], second["id"]]));
    assert_eq!(
        store.device_drafts("desktop").unwrap()[0]["attachments"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    store.save_draft("desktop", &room, "updated text").unwrap();
    assert_eq!(
        store.device_drafts("desktop").unwrap()[0]["attachmentIds"],
        json!([first["id"], second["id"]])
    );
    store.save_draft("desktop", &room, " ").unwrap();
    assert_eq!(
        store.device_drafts("desktop").unwrap()[0]["attachmentIds"],
        json!([first["id"], second["id"]])
    );
    store
        .save_draft_with_artifacts("desktop", &room, "", Some(&[]))
        .unwrap();
    assert!(store.device_drafts("desktop").unwrap().is_empty());
    assert_eq!(
        store
            .rows("SELECT id FROM artifacts WHERE room_id=?", [&room])
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn rejects_cross_room_and_non_image_ids_without_mutating_existing_draft() {
    let (_dir, store, project, room) = fixture();
    let other = store
        .create_room(&project, "Other", &[json!("default")])
        .unwrap();
    let other_image = image(&store, &project, text(&other, "id"), "other.png");
    let video = store.store_artifact(&json!({"projectId":project,"roomId":room,"name":"movie.mp4","mime":"video/mp4","author":"user"}), b"video bytes").unwrap();
    store.save_draft("desktop", &room, "before").unwrap();
    for invalid in [
        json!("missing"),
        other_image["id"].clone(),
        video["id"].clone(),
        json!(42),
        json!("../../outside"),
    ] {
        assert_eq!(
            store
                .save_draft_with_artifacts("desktop", &room, "after", Some(&[invalid]))
                .unwrap_err()
                .to_string(),
            "artifact does not belong to the room"
        );
        let drafts = store.device_drafts("desktop").unwrap();
        assert_eq!(drafts[0]["text"], "before");
        assert_eq!(drafts[0]["attachmentIds"], json!([]));
    }
}
