//! Checkout-only owner-local control client. No HTTP/UI caller is wired to this module.
//! An uncertain write poisons the client; never retry a sequence or log its text.
use crate::Result;
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt},
        net::UnixStream,
    },
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

const MAX_FRAME: usize = 16_384;
const ACK_TIMEOUT: Duration = Duration::from_secs(2);
const AMBIGUOUS: &str = "Peer control receipt ambiguous; do not retry";

pub struct ControlClient {
    path: PathBuf,
    alias: String,
    profile: String,
    session_id: String,
    terminal_id: String,
    handle: String,
    sequence: u64,
    timeout: Duration,
    active: bool,
}

fn ident(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
fn handle(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn left(deadline: Instant) -> Result<Duration> {
    let n = deadline.saturating_duration_since(Instant::now());
    if n.is_zero() {
        return Err("Peer control timed out".into());
    }
    Ok(n)
}
fn connect(path: &Path, deadline: Instant) -> Result<UnixStream> {
    let parent = path.parent().ok_or("Peer control directory unavailable")?;
    let dir = fs::symlink_metadata(parent).map_err(|_| "Peer control unavailable")?;
    let sock = fs::symlink_metadata(path).map_err(|_| "Peer control unavailable")?;
    let uid = unsafe { libc::geteuid() };
    if !dir.is_dir()
        || dir.file_type().is_symlink()
        || dir.uid() != uid
        || dir.mode() & 0o077 != 0
        || !sock.file_type().is_socket()
        || sock.uid() != uid
        || sock.mode() & 0o077 != 0
    {
        return Err("Unsafe peer control socket".into());
    }
    let stream =
        crate::peer::connect_bounded(path, deadline).map_err(|_| "Peer control unavailable")?;
    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd;
        let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
        let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        let ok = unsafe {
            libc::getsockopt(
                stream.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_PEERCRED,
                &mut cred as *mut _ as *mut libc::c_void,
                &mut len,
            )
        };
        if ok != 0 || len as usize != std::mem::size_of::<libc::ucred>() || cred.uid != uid {
            return Err("Peer service owner mismatch".into());
        }
    }
    Ok(stream)
}
fn frame(stream: &mut UnixStream, deadline: Instant) -> Result<Value> {
    let mut bytes = Vec::new();
    loop {
        stream
            .set_read_timeout(Some(left(deadline)?))
            .map_err(|_| "Peer control unavailable")?;
        let mut b = [0u8; 1];
        if stream
            .read(&mut b)
            .map_err(|_| "Peer control response unavailable")?
            == 0
        {
            return Err("Peer control response incomplete".into());
        }
        bytes.push(b[0]);
        if bytes.len() > MAX_FRAME {
            return Err("Peer control response exceeds limit".into());
        }
        if b[0] == b'\n' {
            break;
        }
    }
    // Reject already-buffered extra frames instead of accidentally treating them as a later receipt.
    // MSG_PEEK is nonblocking: a concurrently arriving extra frame cannot be mistaken for an ACK,
    // since subsequent response shape checks still reject it.
    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd;
        let mut b = [0u8; 1];
        let n = unsafe {
            libc::recv(
                stream.as_raw_fd(),
                b.as_mut_ptr() as *mut libc::c_void,
                1,
                libc::MSG_PEEK | libc::MSG_DONTWAIT,
            )
        };
        if n > 0 {
            return Err("Peer control response has trailing data".into());
        }
    }
    serde_json::from_slice(&bytes[..bytes.len() - 1])
        .map_err(|_| "Invalid peer control response".into())
}
fn exchange(
    path: &Path,
    request: &Value,
    timeout: Duration,
    expected: &Value,
    ack: Option<(&str, u64)>,
) -> Result<()> {
    let mut encoded = serde_json::to_vec(request).map_err(|_| "Invalid peer control request")?;
    encoded.push(b'\n');
    if encoded.len() > MAX_FRAME {
        return Err("Peer control request exceeds limit".into());
    }
    let deadline = Instant::now() + timeout;
    let mut stream = connect(path, deadline)?;
    stream
        .set_write_timeout(Some(left(deadline)?))
        .map_err(|_| "Peer control unavailable")?;
    stream
        .write_all(&encoded)
        .map_err(|_| "Peer control request ambiguous; do not retry")?;
    let response = frame(&mut stream, deadline)?;
    if response != *expected {
        return Err("Peer control denied or invalid response".into());
    }
    if let Some((handle, seq)) = ack {
        let mut receipt =
            serde_json::to_vec(&json!({"op":"control.ack","handle":handle,"sequence":seq}))
                .map_err(|_| AMBIGUOUS)?;
        receipt.push(b'\n');
        let receipt_deadline = std::cmp::min(deadline, Instant::now() + ACK_TIMEOUT);
        stream
            .set_write_timeout(Some(left(receipt_deadline).map_err(|_| AMBIGUOUS)?))
            .map_err(|_| AMBIGUOUS)?;
        stream.write_all(&receipt).map_err(|_| AMBIGUOUS)?;
        let final_response = frame(&mut stream, receipt_deadline).map_err(|_| AMBIGUOUS)?;
        if final_response != json!({"ok":true,"result":{"ack":true}}) {
            return Err(AMBIGUOUS.into());
        }
    }
    Ok(())
}
impl ControlClient {
    pub fn begin(
        alias: &str,
        profile: &str,
        session_id: &str,
        terminal_id: &str,
        ttl: u16,
    ) -> Result<Self> {
        Self::begin_at(
            &crate::home().join(".local/share/ares-peer/control.sock"),
            alias,
            profile,
            session_id,
            terminal_id,
            ttl,
            Duration::from_secs(10),
        )
    }
    pub fn begin_at(
        path: &Path,
        alias: &str,
        profile: &str,
        session_id: &str,
        terminal_id: &str,
        ttl: u16,
        timeout: Duration,
    ) -> Result<Self> {
        if ![alias, profile, session_id, terminal_id]
            .iter()
            .all(|s| ident(s))
            || !(1..=300).contains(&ttl)
            || timeout.is_zero()
        {
            return Err("Invalid peer control identity".into());
        }
        let request = json!({"op":"control.begin","peer":alias,"profile":profile,"session_id":session_id,"terminal_id":terminal_id,"ttl":ttl});
        let mut encoded =
            serde_json::to_vec(&request).map_err(|_| "Invalid peer control request")?;
        encoded.push(b'\n');
        if encoded.len() > MAX_FRAME {
            return Err("Peer control request exceeds limit".into());
        }
        let deadline = Instant::now() + timeout;
        let mut stream = connect(path, deadline)?;
        stream
            .set_write_timeout(Some(left(deadline)?))
            .map_err(|_| "Peer control unavailable")?;
        stream
            .write_all(&encoded)
            .map_err(|_| "Peer control request ambiguous; do not retry")?;
        let response = frame(&mut stream, deadline)?;
        let obj = response
            .as_object()
            .ok_or("Invalid peer control response")?;
        if obj.len() != 2 || obj.get("ok") != Some(&Value::Bool(true)) {
            return Err("Peer control denied".into());
        }
        let result = obj
            .get("result")
            .and_then(Value::as_object)
            .ok_or("Invalid peer control response")?;
        if result.len() != 2 || result.get("sequence") != Some(&json!(0)) {
            return Err("Invalid peer control response".into());
        }
        let h = result
            .get("handle")
            .and_then(Value::as_str)
            .ok_or("Invalid peer control handle")?;
        if !handle(h) {
            return Err("Invalid peer control handle".into());
        }
        let ack_deadline = std::cmp::min(deadline, Instant::now() + ACK_TIMEOUT);
        let mut ack = serde_json::to_vec(&json!({"op":"control.ack","handle":h,"sequence":0}))
            .map_err(|_| AMBIGUOUS)?;
        ack.push(b'\n');
        stream
            .set_write_timeout(Some(left(ack_deadline).map_err(|_| AMBIGUOUS)?))
            .map_err(|_| AMBIGUOUS)?;
        stream.write_all(&ack).map_err(|_| AMBIGUOUS)?;
        if frame(&mut stream, ack_deadline).map_err(|_| AMBIGUOUS)?
            != json!({"ok":true,"result":{"ack":true}})
        {
            return Err(AMBIGUOUS.into());
        }
        Ok(Self {
            path: path.to_owned(),
            alias: alias.to_owned(),
            profile: profile.to_owned(),
            session_id: session_id.to_owned(),
            terminal_id: terminal_id.to_owned(),
            handle: h.to_owned(),
            sequence: 0,
            timeout,
            active: true,
        })
    }
    pub fn send(&mut self, text: &str) -> Result<()> {
        if !self.active {
            return Err("Peer control inactive; do not retry".into());
        }
        if text.is_empty() || text.len() > 2048 || text.chars().any(|c| c.is_control()) {
            return Err("Invalid peer control input".into());
        }
        self.step("control.send", Some(text))
    }
    pub fn release(&mut self) -> Result<()> {
        if !self.active {
            return Err("Peer control inactive; do not retry".into());
        }
        self.step("control.release", None)
    }
    fn step(&mut self, op: &str, text: Option<&str>) -> Result<()> {
        self.active = false; // Even a lost first response may have delivered input. No retries.
        let next = self
            .sequence
            .checked_add(1)
            .ok_or("Peer control sequence exhausted")?;
        let mut request = json!({"op":op,"handle":self.handle,"peer":self.alias,"profile":self.profile,"session_id":self.session_id,"terminal_id":self.terminal_id,"sequence":next});
        if let Some(text) = text {
            request["text"] = json!(text);
        }
        let expected = json!({"ok":true,"result":{"sequence":next}});
        exchange(
            &self.path,
            &request,
            self.timeout,
            &expected,
            if text.is_some() {
                Some((&self.handle, next))
            } else {
                None
            },
        )?;
        self.sequence = next;
        self.active = text.is_some();
        Ok(())
    }
}
