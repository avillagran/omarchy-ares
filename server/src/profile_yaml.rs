//! Bounded, read-only owner-local Hermes Desktop group snapshot source.
//! No general configuration, credentials, or other ui_meta enter the projection.
use serde::de::{DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde::Deserializer;
use serde_json::{json, Map, Number, Value};
use std::{
    cell::Cell,
    env, fmt,
    fs::{self, File},
    io::{self, Read},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, fs::MetadataExt},
    },
    path::{Path, PathBuf},
};

const MAX_BYTES: u64 = 1_048_576;
const MAX_DEPTH: usize = 32;
const MAX_NODES: usize = 20_000;
const MAX_COLLECTION: usize = 10_000;
const MAX_SCALAR: usize = 65_536;
fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "Native profile source unavailable",
    )
}
fn same(a: &fs::Metadata, b: &fs::Metadata) -> bool {
    a.dev() == b.dev()
        && a.ino() == b.ino()
        && a.len() == b.len()
        && a.mtime() == b.mtime()
        && a.mtime_nsec() == b.mtime_nsec()
        && a.ctime() == b.ctime()
        && a.ctime_nsec() == b.ctime_nsec()
}
fn private(meta: &fs::Metadata, uid: u32, directory: bool) -> bool {
    meta.uid() == uid
        && meta.mode() & 0o777 == if directory { 0o700 } else { 0o600 }
        && if directory {
            meta.is_dir()
        } else {
            meta.is_file()
        }
}
// Every directory is opened relative to its already-checked parent, never
// resolved through an unchecked pathname. No component may be a symlink.
fn open_directory(parent: &File, name: &std::ffi::CStr) -> io::Result<File> {
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(invalid());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}
fn trusted_ancestor(meta: &fs::Metadata, uid: u32) -> bool {
    meta.is_dir() && (meta.uid() == uid || meta.uid() == 0) && meta.mode() & 0o022 == 0
}
fn components(home: &Path) -> io::Result<Vec<std::ffi::CString>> {
    if !home.is_absolute() {
        return Err(invalid());
    }
    home.components()
        .filter_map(|component| match component {
            std::path::Component::RootDir => None,
            std::path::Component::Normal(name) => {
                Some(std::ffi::CString::new(name.as_bytes()).map_err(|_| invalid()))
            }
            _ => Some(Err(invalid())),
        })
        .collect()
}
// The root is an anchor; at the final check re-open every component from it.
// A moved/replaced ancestor must not silently turn the held leaf into a stale tree.
fn check_chain(
    root: &File,
    names: &[std::ffi::CString],
    held: &[File],
    uid: u32,
) -> io::Result<()> {
    let mut current = root.try_clone().map_err(|_| invalid())?;
    for (index, name) in names.iter().enumerate() {
        let next = open_directory(&current, name)?;
        let actual = next.metadata().map_err(|_| invalid())?;
        let original = held[index].metadata().map_err(|_| invalid())?;
        if !same(&actual, &original)
            || (index + 1 == names.len() && !private(&actual, uid, true))
            || (index + 1 != names.len() && !trusted_ancestor(&actual, uid))
        {
            return Err(invalid());
        }
        current = next;
    }
    Ok(())
}
/// Resolve only this process's owner-selected Hermes home, never a request parameter.
pub fn load_default_profiles() -> io::Result<Vec<Value>> {
    let home = if let Some(path) = env::var_os("HERMES_HOME") {
        PathBuf::from(path)
    } else {
        PathBuf::from(env::var_os("HOME").ok_or_else(invalid)?).join(".hermes")
    };
    load_profiles_at(&home)
}

/// An explicit path is used only by isolated tests and the owner-local resolver.
pub(crate) fn load_profiles_at(home: &Path) -> io::Result<Vec<Value>> {
    #[cfg(test)]
    { load_profiles_at_with_hook(home, || {}) }
    #[cfg(not(test))]
    { load_profiles_at_impl(home, || {}) }
}

// Test seam: swap an ancestor after the descriptors are acquired, before reading.
#[cfg(test)]
pub(crate) fn load_profiles_at_with_hook(
    home: &Path,
    hook: impl FnOnce(),
) -> io::Result<Vec<Value>> {
    load_profiles_at_impl(home, hook)
}

fn load_profiles_at_impl(home: &Path, hook: impl FnOnce()) -> io::Result<Vec<Value>> {
    let names = components(home)?;
    if names.is_empty() {
        return Err(invalid());
    }
    let uid = unsafe { libc::geteuid() };
    let root = File::open("/").map_err(|_| invalid())?;
    if !trusted_ancestor(&root.metadata().map_err(|_| invalid())?, uid) {
        return Err(invalid());
    }
    let mut held = Vec::with_capacity(names.len());
    for (index, name) in names.iter().enumerate() {
        let parent = held.last().unwrap_or(&root);
        let dir = open_directory(parent, name)?;
        let meta = dir.metadata().map_err(|_| invalid())?;
        if (index + 1 == names.len() && !private(&meta, uid, true))
            || (index + 1 != names.len() && !trusted_ancestor(&meta, uid))
        {
            return Err(invalid());
        }
        held.push(dir);
    }
    hook();
    let dir = held.last().ok_or_else(invalid)?;
    let name = std::ffi::CString::new("profile.yaml").unwrap();
    let fd = unsafe {
        libc::openat(
            dir.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(invalid());
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    let before = file.metadata().map_err(|_| invalid())?;
    if !private(&before, uid, false) || before.len() > MAX_BYTES {
        return Err(invalid());
    }
    let mut bytes = Vec::new();
    file.by_ref()
        .take(MAX_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| invalid())?;
    if bytes.len() as u64 > MAX_BYTES || !same(&before, &file.metadata().map_err(|_| invalid())?) {
        return Err(invalid());
    }
    let projection = parse_projection(&bytes)?;
    let reopened_fd = unsafe {
        libc::openat(
            dir.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        )
    };
    if reopened_fd < 0 {
        return Err(invalid());
    }
    let reopened = unsafe { File::from_raw_fd(reopened_fd) };
    if !same(&before, &reopened.metadata().map_err(|_| invalid())?)
        || !same(&before, &file.metadata().map_err(|_| invalid())?)
    {
        return Err(invalid());
    }
    check_chain(&root, &names, &held, uid)?;
    Ok(projection)
}

struct Bounded<'a> {
    depth: usize,
    nodes: &'a Cell<usize>,
}
impl<'de> DeserializeSeed<'de> for Bounded<'_> {
    type Value = Value;
    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Value, D::Error> {
        if self.depth > MAX_DEPTH || self.nodes.get() >= MAX_NODES {
            return Err(serde::de::Error::custom("profile bound"));
        }
        self.nodes.set(self.nodes.get() + 1);
        deserializer.deserialize_any(BoundedVisitor {
            depth: self.depth,
            nodes: self.nodes,
        })
    }
}
struct BoundedVisitor<'a> {
    depth: usize,
    nodes: &'a Cell<usize>,
}
impl<'de> Visitor<'de> for BoundedVisitor<'_> {
    type Value = Value;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("bounded profile value")
    }
    fn visit_unit<E: serde::de::Error>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }
    fn visit_none<E: serde::de::Error>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }
    fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Value, E> {
        Ok(Value::Bool(v))
    }
    fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Value, E> {
        Ok(v.into())
    }
    fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Value, E> {
        Ok(v.into())
    }
    fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Value, E> {
        Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("nonfinite"))
    }
    fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Value, E> {
        if v.len() > MAX_SCALAR {
            Err(E::custom("scalar bound"))
        } else {
            Ok(v.into())
        }
    }
    fn visit_string<E: serde::de::Error>(self, v: String) -> Result<Value, E> {
        self.visit_str(&v)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        let mut values = Vec::new();
        while let Some(value) = seq.next_element_seed(Bounded {
            depth: self.depth + 1,
            nodes: self.nodes,
        })? {
            if values.len() == MAX_COLLECTION {
                return Err(serde::de::Error::custom("collection bound"));
            }
            values.push(value);
        }
        Ok(Value::Array(values))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        let mut values = Map::new();
        while let Some(key) = map.next_key::<String>()? {
            if key.len() > MAX_SCALAR || values.len() == MAX_COLLECTION || values.contains_key(&key)
            {
                return Err(serde::de::Error::custom("mapping bound"));
            }
            let value = map.next_value_seed(Bounded {
                depth: self.depth + 1,
                nodes: self.nodes,
            })?;
            values.insert(key, value);
        }
        Ok(Value::Object(values))
    }
}
fn parse_projection(bytes: &[u8]) -> io::Result<Vec<Value>> {
    let mut documents = serde_yaml::Deserializer::from_slice(bytes);
    let document = documents.next().ok_or_else(invalid)?;
    let nodes = Cell::new(0);
    let root = Bounded {
        depth: 0,
        nodes: &nodes,
    }
    .deserialize(document)
    .map_err(|_| invalid())?;
    if documents.next().is_some() {
        return Err(invalid());
    }
    let groups = root
        .get("ui_meta")
        .and_then(|v| v.get("hermes-bots-groups"))
        .ok_or_else(invalid)?;
    let revisions = root
        .get("_ui_meta_revisions")
        .and_then(|v| v.get("hermes-bots-groups"))
        .ok_or_else(invalid)?;
    if !groups.is_object()
        || !groups.get("rooms").is_some_and(Value::is_object)
        || !groups.get("deleted").is_some_and(Value::is_object)
        || !groups.get("version").is_some_and(Value::is_number)
        || revisions.as_i64().is_none_or(|n| n < 0)
    {
        return Err(invalid());
    }
    Ok(vec![
        json!({"name":"default","ui_meta":{"hermes-bots-groups":groups},"ui_meta_revisions":{"hermes-bots-groups":revisions}}),
    ])
}
