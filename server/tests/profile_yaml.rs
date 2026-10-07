#[path = "../src/profile_yaml.rs"]
mod profile_yaml;
use ares_server::native_groups::NativeSnapshot;
use std::{fs, os::unix::fs::PermissionsExt, path::Path, sync::Mutex};
use tempfile::{tempdir_in, TempDir};

static HERMES_ENV_LOCK: Mutex<()> = Mutex::new(());

fn tempdir() -> std::io::Result<TempDir> {
    // A private runtime directory when available; never create fixtures in the live HOME.
    let base = std::env::var_os("XDG_RUNTIME_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    tempdir_in(base)
}

fn source(home: &Path, body: &str) {
    fs::create_dir(home).unwrap();
    fs::set_permissions(home, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(home.join("profile.yaml"), body).unwrap();
    fs::set_permissions(home.join("profile.yaml"), fs::Permissions::from_mode(0o600)).unwrap();
}
const PROFILE: &str = "ui_meta:\n  hermes-bots-groups:\n    version: 3\n    rooms:\n      'id:abc':\n        roomId: abc\n        revision: 4\n        members: []\n        log:\n          - {text: PRIVATE, thread: 1}\n    deleted: {'id:gone': 7}\n  other: {secret: NEVER_RETURN}\n_ui_meta_revisions: {hermes-bots-groups: 12}\ncredentials: SHOULD_NOT_LEAK\n";

#[test]
fn projects_only_default_native_group_slice() {
    let tmp = tempdir().unwrap();
    let home = tmp.path().join("hermes");
    source(&home, PROFILE);
    let profiles = profile_yaml::load_profiles_at(&home).unwrap();
    assert_eq!(profiles.len(), 1);
    assert_eq!(profiles[0]["name"], "default");
    assert_eq!(profiles[0]["ui_meta_revisions"]["hermes-bots-groups"], 12);
    assert_eq!(
        profiles[0]["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"][0]["text"],
        "PRIVATE"
    );
    assert!(!profiles[0].to_string().contains("NEVER_RETURN"));
    assert!(!profiles[0].to_string().contains("SHOULD_NOT_LEAK"));
    NativeSnapshot::from_profiles("local", &profiles).unwrap();
}

#[test]
fn owner_selected_home_is_used() {
    let tmp = tempdir().unwrap();
    let home = tmp.path().join("hermes");
    source(&home, PROFILE);
    let _guard = HERMES_ENV_LOCK.lock().unwrap();
    let previous = std::env::var_os("HERMES_HOME");
    std::env::set_var("HERMES_HOME", &home);
    let result = profile_yaml::load_default_profiles();
    if let Some(previous) = previous {
        std::env::set_var("HERMES_HOME", previous);
    } else {
        std::env::remove_var("HERMES_HOME");
    }
    let profiles = result.unwrap();
    assert_eq!(profiles[0]["ui_meta_revisions"]["hermes-bots-groups"], 12);
}

#[test]
fn real_owner_source_contract_when_explicitly_requested() {
    if std::env::var_os("ARES_TEST_REAL_PROFILE").is_none() {
        return;
    }
    let _guard = HERMES_ENV_LOCK.lock().unwrap();
    let profiles = profile_yaml::load_default_profiles().unwrap();
    assert_eq!(profiles.len(), 1);
    NativeSnapshot::from_profiles("local", &profiles).unwrap();
    assert!(
        profiles[0]["ui_meta"]["hermes-bots-groups"]["rooms"]
            .as_object()
            .unwrap()
            .len()
            > 0
    );
}

#[test]
fn rejects_public_directory_or_file_and_symlink() {
    let tmp = tempdir().unwrap();
    let home = tmp.path().join("hermes");
    source(&home, PROFILE);
    fs::set_permissions(&home, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(profile_yaml::load_profiles_at(&home).is_err());
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(home.join("profile.yaml"), fs::Permissions::from_mode(0o644)).unwrap();
    assert!(profile_yaml::load_profiles_at(&home).is_err());
    fs::remove_file(home.join("profile.yaml")).unwrap();
    std::os::unix::fs::symlink(tmp.path().join("outside"), home.join("profile.yaml")).unwrap();
    assert!(profile_yaml::load_profiles_at(&home).is_err());
}

#[test]
fn rejects_oversize_missing_or_malformed_source_without_echo() {
    let tmp = tempdir().unwrap();
    let home = tmp.path().join("hermes");
    source(&home, PROFILE);
    fs::write(home.join("profile.yaml"), "x".repeat(1_048_577)).unwrap();
    assert!(profile_yaml::load_profiles_at(&home).is_err());
    fs::write(home.join("profile.yaml"), "PRIVATE: [broken").unwrap();
    let error = profile_yaml::load_profiles_at(&home)
        .unwrap_err()
        .to_string();
    assert!(!error.contains("PRIVATE"));
    fs::remove_file(home.join("profile.yaml")).unwrap();
    assert!(profile_yaml::load_profiles_at(&home).is_err());
}

#[test]
fn rejects_deep_large_duplicate_and_multi_document_yaml() {
    let tmp = tempdir().unwrap();
    let home = tmp.path().join("hermes");
    source(&home, PROFILE);
    for body in [
        format!("ui_meta: {}null\n", "[".repeat(65)),
        format!("ui_meta: [{}]\n", "null,".repeat(10001)),
        format!("{PROFILE}ui_meta: {{}}\n"),
        format!("{PROFILE}---\nui_meta: {{}}\n"),
    ] {
        fs::write(home.join("profile.yaml"), body).unwrap();
        assert!(profile_yaml::load_profiles_at(&home).is_err());
    }
}

#[test]
fn rejects_missing_snapshot_or_revision_and_wrong_shape() {
    let tmp = tempdir().unwrap();
    let home = tmp.path().join("hermes");
    source(&home, PROFILE);
    for body in ["ui_meta: {}\n", "ui_meta: {hermes-bots-groups: []}\n", "ui_meta: {hermes-bots-groups: {version: 3, rooms: {}, deleted: {}}}\n", "ui_meta: {hermes-bots-groups: {version: 3, rooms: [], deleted: {}}}\n_ui_meta_revisions: {hermes-bots-groups: 1}\n"] {
        fs::write(home.join("profile.yaml"), body).unwrap();
        assert!(profile_yaml::load_profiles_at(&home).is_err());
    }
}

#[test]
fn accepts_private_ancestors_to_real_hermes_home() {
    let tmp = tempdir().unwrap();
    let parent = tmp.path().join("real");
    fs::create_dir(&parent).unwrap();
    let home = parent.join(".hermes");
    source(&home, PROFILE);
    assert!(profile_yaml::load_profiles_at(&home).is_ok());
}

#[test]
fn rejects_symlinked_intermediate_ancestor() {
    let tmp = tempdir().unwrap();
    let real = tmp.path().join("real");
    fs::create_dir(&real).unwrap();
    source(&real.join(".hermes"), PROFILE);
    std::os::unix::fs::symlink(&real, tmp.path().join("alias")).unwrap();
    assert!(profile_yaml::load_profiles_at(&tmp.path().join("alias/.hermes")).is_err());
}

#[test]
fn rejects_group_or_world_writable_intermediate_ancestor() {
    let tmp = tempdir().unwrap();
    let parent = tmp.path().join("parent");
    fs::create_dir(&parent).unwrap();
    let home = parent.join(".hermes");
    source(&home, PROFILE);
    for mode in [0o770, 0o707] {
        fs::set_permissions(&parent, fs::Permissions::from_mode(mode)).unwrap();
        assert!(
            profile_yaml::load_profiles_at(&home).is_err(),
            "mode {mode:o}"
        );
    }
}

#[test]
fn rejects_replaced_intermediate_directory_after_open() {
    let tmp = tempdir().unwrap();
    let parent = tmp.path().join("parent");
    fs::create_dir(&parent).unwrap();
    let home = parent.join(".hermes");
    source(&home, PROFILE);
    let moved = tmp.path().join("moved");
    assert!(profile_yaml::load_profiles_at_with_hook(&home, || {
        fs::rename(&parent, &moved).unwrap();
        // The old leaf/file inodes remain reachable through the same pathname.
        // A leaf-only pre/post check therefore cannot detect this parent swap.
        std::os::unix::fs::symlink(&moved, &parent).unwrap();
    })
    .is_err());
}
