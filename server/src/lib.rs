pub mod broker;
pub mod cli;
pub mod control;
pub mod herdr;
pub mod http;
pub mod integration;
pub mod native_groups;
pub mod observe;
pub mod peer;
pub mod peer_control;
pub mod profile_yaml;
pub mod producers;
pub mod remote;
pub mod render;
pub mod sidecar;
pub mod store;
pub mod sync;
pub mod terminals;

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub const MAX_ARTIFACT_BYTES: usize = 512 * 1024 * 1024;
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn ident(prefix: &str) -> String {
    format!("{prefix}_{}", uuid::Uuid::new_v4().simple())
}
pub fn digest(data: impl AsRef<[u8]>) -> String {
    hex::encode(Sha256::digest(data.as_ref()))
}
pub fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub fn require(value: &str, label: &str, maximum: usize) -> Result<String> {
    let s = value.trim();
    if s.is_empty() {
        return Err(format!("{label} is required").into());
    }
    if s.chars().count() > maximum {
        return Err(format!("{label} is too long").into());
    }
    Ok(s.to_owned())
}
pub fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
pub fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}
pub fn expand_path(s: &str) -> PathBuf {
    if s == "~" {
        home()
    } else if let Some(s) = s.strip_prefix("~/") {
        home().join(s)
    } else {
        PathBuf::from(s)
    }
}
pub fn private_dir(path: &Path) -> Result<()> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}
pub fn write_private(path: &Path, body: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or("path has no parent")?;
    private_dir(parent)?;
    let tmp = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().unwrap().to_string_lossy(),
        ident("write")
    ));
    let result = (|| -> Result<()> {
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)?;
        f.write_all(body)?;
        f.sync_all()?;
        fs::rename(&tmp, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}
