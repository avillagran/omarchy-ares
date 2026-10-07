//! Owner-local Ares Peer control client. Inventory is read-only; mutation requires explicit control requests.
use crate::{terminals, Result};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt},
        net::UnixStream,
    },
    path::Path,
    time::{Duration, Instant},
};

const MAX_REPLY: usize = 2 * 1024 * 1024;
const MAX_REQUEST: usize = 16_384;
const MAX_HISTORY_REPLY: usize = 16_384;
const TIMEOUT: Duration = Duration::from_secs(10);

pub fn inventory(alias: &str) -> Result<Value> {
    let path = crate::home().join(".local/share/ares-peer/control.sock");
    inventory_at(&path, alias, TIMEOUT)
}

/// The alias is sent exactly, never inferred from a network address or SSH key.
pub fn inventory_at(path: &Path, alias: &str, timeout: Duration) -> Result<Value> {
    if alias == "local"
        || alias.is_empty()
        || alias.len() > 128
        || !alias
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
    {
        return Err("Invalid peer alias".into());
    }
    let result = local_control_at(
        path,
        &json!({"op":"rpc","peer":alias,"request":{"op":"inventory"}}),
        timeout,
    )?;
    terminals::normalize_peer_inventory(alias, &result)
}

/// Synchronous owner-local control call for HTTP `spawn_blocking` routes.
/// Returns the successful `result` object, never the response envelope.
pub fn local_control(request: &Value) -> Result<Value> {
    let path = crate::home().join(".local/share/ares-peer/control.sock");
    local_control_at(&path, request, TIMEOUT)
}

pub fn local_control_at(path: &Path, request: &Value, timeout: Duration) -> Result<Value> {
    if !request.is_object() {
        return Err("Invalid peer control request".into());
    }
    let encoded = serde_json::to_vec(request)?;
    if encoded.len() + 1 > MAX_REQUEST {
        return Err("Peer control request exceeds limit".into());
    }
    exchange(path, &encoded, timeout)
}

/// History-only read-only RPC: accepts precisely one bounded JSON line and EOF.
/// Do not use this for write ACKs or generic control calls.
pub fn local_control_readonly_strict(request: &Value) -> Result<Value> {
    let path = crate::home().join(".local/share/ares-peer/control.sock");
    local_control_readonly_strict_at(&path, request, TIMEOUT)
}

pub fn local_control_readonly_strict_at(
    path: &Path,
    request: &Value,
    timeout: Duration,
) -> Result<Value> {
    if request.get("op").and_then(Value::as_str) != Some("rpc")
        || request.pointer("/request/op").and_then(Value::as_str) != Some("history")
    {
        return Err("Invalid history request".into());
    }
    let encoded = serde_json::to_vec(request)?;
    if encoded.len() + 1 > MAX_REQUEST {
        return Err("Peer control request exceeds limit".into());
    }
    let deadline = Instant::now() + timeout;
    let mut stream = owner_stream(path, deadline)?;
    write_bounded(&mut stream, &encoded, deadline)?;
    write_bounded(&mut stream, b"\n", deadline)?;

    let mut response = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        stream.set_read_timeout(Some(remaining(deadline)?))?;
        let n = stream
            .read(&mut chunk)
            .map_err(|_| "Peer service timed out or disconnected")?;
        if n == 0 {
            break;
        }
        if response.len() + n > MAX_HISTORY_REPLY {
            return Err("Peer history response exceeds limit".into());
        }
        response.extend_from_slice(&chunk[..n]);
        // Reject trailing data, including a second frame arriving in a later read.
        if response[..response.len().saturating_sub(1)].contains(&b'\n') {
            return Err("Peer history response has trailing data".into());
        }
    }
    if response.last() != Some(&b'\n') {
        return Err("Peer history response incomplete".into());
    }
    let envelope: Value = serde_json::from_slice(&response[..response.len() - 1])
        .map_err(|_| "Invalid peer service response")?;
    if envelope["ok"] != true {
        return Err("Peer control denied or unavailable".into());
    }
    envelope
        .get("result")
        .cloned()
        .ok_or_else(|| "Peer control result missing".into())
}

fn exchange(path: &Path, request: &[u8], timeout: Duration) -> Result<Value> {
    let deadline = Instant::now() + timeout;
    let mut stream = owner_stream(path, deadline)?;
    stream.set_write_timeout(Some(remaining(deadline)?))?;
    stream
        .write_all(&request)
        .map_err(|_| "Peer service request failed")?;
    stream
        .write_all(b"\n")
        .map_err(|_| "Peer service request failed")?;
    let mut response = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        stream.set_read_timeout(Some(remaining(deadline)?))?;
        let n = stream
            .read(&mut chunk)
            .map_err(|_| "Peer service timed out or disconnected")?;
        if n == 0 {
            return Err("Peer service returned an incomplete response".into());
        }
        if let Some(end) = chunk[..n].iter().position(|b| *b == b'\n') {
            if response.len() + end > MAX_REPLY || end + 1 != n {
                return Err("Peer service response exceeds limit or has trailing data".into());
            }
            response.extend_from_slice(&chunk[..end]);
            break;
        }
        if response.len() + n > MAX_REPLY {
            return Err("Peer service response exceeds limit".into());
        }
        response.extend_from_slice(&chunk[..n]);
    }
    let envelope: Value =
        serde_json::from_slice(&response).map_err(|_| "Invalid peer service response")?;
    if envelope["ok"] != true {
        return Err("Peer control denied or unavailable".into());
    }
    envelope
        .get("result")
        .cloned()
        .ok_or_else(|| "Peer control result missing".into())
}

fn owner_stream(path: &Path, deadline: Instant) -> Result<UnixStream> {
    let parent = path.parent().ok_or("Peer control directory unavailable")?;
    let dir = fs::symlink_metadata(parent).map_err(|_| "Peer service unavailable")?;
    let socket = fs::symlink_metadata(path).map_err(|_| "Peer service unavailable")?;
    let uid = unsafe { libc::geteuid() };
    if !dir.is_dir()
        || dir.file_type().is_symlink()
        || dir.uid() != uid
        || dir.mode() & 0o077 != 0
        || !socket.file_type().is_socket()
        || socket.uid() != uid
        || socket.mode() & 0o077 != 0
    {
        return Err("Unsafe peer control socket".into());
    }
    let stream = connect_bounded(path, deadline)?;
    // Filesystem permissions can race; verify the connected service's UID too.
    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd;
        let mut cred = libc::ucred {
            pid: 0,
            uid: 0,
            gid: 0,
        };
        let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        let status = unsafe {
            libc::getsockopt(
                stream.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_PEERCRED,
                &mut cred as *mut _ as *mut libc::c_void,
                &mut len,
            )
        };
        if status != 0 || len as usize != std::mem::size_of::<libc::ucred>() || cred.uid != uid {
            return Err("Peer service owner mismatch".into());
        }
    }
    Ok(stream)
}

fn write_bounded(stream: &mut UnixStream, mut bytes: &[u8], deadline: Instant) -> Result<()> {
    while !bytes.is_empty() {
        stream.set_write_timeout(Some(remaining(deadline)?))?;
        let n = stream
            .write(bytes)
            .map_err(|_| "Peer service request failed")?;
        if n == 0 {
            return Err("Peer service request failed".into());
        }
        bytes = &bytes[n..];
    }
    Ok(())
}

fn remaining(deadline: Instant) -> Result<Duration> {
    let left = deadline.saturating_duration_since(Instant::now());
    if left.is_zero() {
        return Err("Peer service timed out".into());
    }
    Ok(left)
}

#[cfg(target_os = "linux")]
pub(crate) fn connect_bounded(path: &Path, deadline: Instant) -> Result<UnixStream> {
    use std::os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::ffi::OsStrExt,
    };
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.is_empty() || bytes.len() >= address.sun_path.len() {
        return Err("Peer socket path too long".into());
    }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (dest, source) in address.sun_path.iter_mut().zip(bytes.iter()) {
        *dest = *source as libc::c_char;
    }
    let raw = unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if raw < 0 {
        return Err("Peer service unavailable".into());
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw) };
    let size = (std::mem::size_of::<libc::sa_family_t>() + bytes.len() + 1) as libc::socklen_t;
    let result = unsafe {
        libc::connect(
            fd.as_raw_fd(),
            &address as *const _ as *const libc::sockaddr,
            size,
        )
    };
    if result < 0 {
        let error = std::io::Error::last_os_error();
        if !matches!(error.raw_os_error(), Some(libc::EINPROGRESS | libc::EAGAIN)) {
            return Err("Peer service unavailable".into());
        }
        let mut poll = libc::pollfd {
            fd: fd.as_raw_fd(),
            events: libc::POLLOUT,
            revents: 0,
        };
        let ms = remaining(deadline)?.as_millis().min(i32::MAX as u128) as i32;
        if unsafe { libc::poll(&mut poll, 1, ms.max(1)) } <= 0 {
            return Err("Peer service connection timed out".into());
        }
        let mut code: libc::c_int = 0;
        let mut len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
        if unsafe {
            libc::getsockopt(
                fd.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_ERROR,
                &mut code as *mut _ as *mut libc::c_void,
                &mut len,
            )
        } < 0
            || code != 0
        {
            return Err("Peer service unavailable".into());
        }
    }
    let stream = UnixStream::from(fd);
    stream.set_nonblocking(false)?;
    Ok(stream)
}

#[cfg(not(target_os = "linux"))]
pub(crate) fn connect_bounded(_path: &Path, _deadline: Instant) -> Result<UnixStream> {
    Err("Peer service requires Linux owner verification".into())
}
