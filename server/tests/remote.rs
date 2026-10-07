use ares_server::remote::{parse_remote_session, ssh_target_for_peer, validate_ssh_target};
use serde_json::json;

#[test]
fn remote_room_identity_cannot_cross_peer_or_profile() {
    let reference = "remote::host::default::20260921_192605_4360ed";
    assert_eq!(
        parse_remote_session(reference, "host", "default"),
        Some("20260921_192605_4360ed")
    );
    assert_eq!(parse_remote_session(reference, "other", "default"), None);
    assert_eq!(parse_remote_session(reference, "host", "other"), None);
    assert_eq!(
        parse_remote_session("remote::host::default::--bad", "host", "default"),
        None
    );
}

#[test]
fn ssh_destination_requires_explicit_valid_peer_mapping() {
    assert!(validate_ssh_target("user@192.0.2.10").is_ok());
    for unsafe_value in [
        "-oProxyCommand=touch /bad",
        "user@host;cmd",
        "user@host/path",
        "user@host name",
        "",
    ] {
        assert!(validate_ssh_target(unsafe_value).is_err());
    }
    let configured = json!({"host":"user@192.0.2.10"});
    assert_eq!(
        ssh_target_for_peer(&configured, "host").unwrap(),
        "user@192.0.2.10"
    );
    assert!(ssh_target_for_peer(&configured, "another-peer").is_err());
}
