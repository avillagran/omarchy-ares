use ares_server::store::Store;
use ares_server::Result;
use serde_json::json;

#[test]
fn terminal_appearance_preferences_persist_and_validate() -> Result<()> {
    let tmp = tempfile::tempdir()?;
    let store = Store::open(
        &tmp.path().join("ares.db"),
        &tmp.path().join("artifacts"),
        tmp.path().join("hermes"),
    )?;
    let defaults = store.preferences()?;
    assert_eq!(defaults["terminalFontSize"], 10);
    assert_eq!(defaults["terminalBackgroundRole"], "popups.background");
    assert_eq!(defaults["terminalBackgroundOpacity"], 1.0);
    assert_eq!(defaults["terminalBlur"], false);
    assert_eq!(defaults["terminalPreviewReadOnly"], true);
    let updated = store.update_preferences(&json!({
        "terminalFontSize": 14,
        "terminalBackgroundRole": "background",
        "terminalBackgroundOpacity": 0.42,
        "terminalBlur": true,
        "terminalPreviewReadOnly": false
    }))?;
    assert_eq!(updated["terminalFontSize"], 14);
    assert_eq!(updated["terminalBackgroundOpacity"], 0.42);
    assert_eq!(updated["terminalPreviewReadOnly"], false);
    assert_eq!(store.preferences()?, updated);
    for invalid in [
        json!({"terminalFontSize": 0}),
        json!({"terminalFontSize": 40}),
        json!({"terminalFontSize": 10.5}),
        json!({"terminalBackgroundRole": "#ff0000"}),
        json!({"terminalBackgroundOpacity": -0.1}),
        json!({"terminalBackgroundOpacity": 1.1}),
        json!({"terminalBackgroundOpacity": "transparent"}),
        json!({"terminalBlur": "yes"}),
        json!({"terminalPreviewReadOnly": "yes"}),
    ] {
        assert!(
            store.update_preferences(&invalid).is_err(),
            "accepted {invalid}"
        );
    }
    assert_eq!(store.preferences()?, updated);
    Ok(())
}
