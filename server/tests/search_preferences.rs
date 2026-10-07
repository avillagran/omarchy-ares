use ares_server::store::Store;
use serde_json::json;
use tempfile::TempDir;

#[test]
fn search_shortcut_updates_and_survives_reopen() {
    let dir = TempDir::new().unwrap();
    for shortcut in ["Alt+Space", "Ctrl+Space"] {
        let store = open(&dir);
        assert_eq!(
            store
                .update_preferences(&json!({"searchShortcut": shortcut}))
                .unwrap()["searchShortcut"],
            shortcut
        );
        drop(store);
        assert_eq!(
            open(&dir).preferences().unwrap()["searchShortcut"],
            shortcut
        );
    }
}

#[test]
fn invalid_search_shortcut_rejects_entire_payload_without_writes() {
    let dir = TempDir::new().unwrap();
    let store = open(&dir);
    let before = store.preferences().unwrap();
    for invalid in [
        json!(null),
        json!(true),
        json!(7),
        json!([]),
        json!({}),
        json!(""),
        json!("ctrl+space"),
        json!("Alt+Space "),
        json!("Ctrl+Shift+Space"),
    ] {
        assert!(
            store
                .update_preferences(&json!({"homeQuotaVisible": false, "searchShortcut": invalid}))
                .is_err(),
            "accepted {invalid}"
        );
        assert_eq!(store.preferences().unwrap(), before);
        assert_eq!(open(&dir).preferences().unwrap(), before);
    }
    assert!(store
        .update_preferences(&json!({"searchShortcut": "Alt+Space", "terminalFontSize": "bad"}))
        .is_err());
    assert_eq!(open(&dir).preferences().unwrap(), before);
}

fn open(dir: &TempDir) -> Store {
    Store::open(
        &dir.path().join("ares.db"),
        &dir.path().join("artifacts"),
        dir.path().join("hermes"),
    )
    .unwrap()
}

#[test]
fn search_shortcut_defaults_to_ctrl_space() {
    let dir = TempDir::new().unwrap();
    assert_eq!(
        open(&dir).preferences().unwrap()["searchShortcut"],
        "Ctrl+Space"
    );
}
