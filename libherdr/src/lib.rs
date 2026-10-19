//! Herdr socket protocol client: local unix RPC, event subscriptions, and an
//! owned SSH remote transport.
//!
//! The protocol is newline-delimited JSON-RPC: one request line
//! `{"id","method","params"}` per connection for one-shot calls
//! (`session.snapshot`, `pane.read`), and a long-lived connection for
//! `events.subscribe`. Herdr closes a one-shot connection after its reply.
//!
//! Security invariants ported from the omarchy-link Python client:
//! - the socket must be a regular unix socket owned by the current uid;
//! - the peer is fenced by SO_PEERCRED uid plus socket inode/dev stability;
//! - the SSH transport revalidates the persisted target, never reads SSH
//!   config (`-F /dev/null`), never prompts (BatchMode), and probes the remote
//!   peer identity before and after forwarding.

use serde_json::{json, Value};
use std::io::{BufReader, Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const MAX_LINE: usize = 512 * 1024;
const IO_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug)]
pub enum Error {
    Io(std::io::Error),
    Protocol(String),
    Remote(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io: {e}"),
            Error::Protocol(m) => write!(f, "protocol: {m}"),
            Error::Remote(m) => write!(f, "remote: {m}"),
        }
    }
}

impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;

fn proto<T: Into<String>>(msg: T) -> Error {
    Error::Protocol(msg.into())
}

/// Stable fingerprint of the connected herdr server process: two connections
/// with the same fingerprint belong to the same server incarnation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionFingerprint(pub String);

fn sha256_hex(parts: &[&[u8]]) -> String {
    // Minimal SHA-256 (no external crypto dep): streaming implementation.
    struct Sha256 {
        state: [u32; 8],
        buf: [u8; 64],
        buf_len: usize,
        total: u64,
    }
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    impl Sha256 {
        fn new() -> Self {
            Sha256 {
                state: [
                    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
                    0x1f83d9ab, 0x5be0cd19,
                ],
                buf: [0u8; 64],
                buf_len: 0,
                total: 0,
            }
        }
        fn block(&mut self, block: &[u8]) {
            let mut w = [0u32; 64];
            for (i, chunk) in block.chunks_exact(4).enumerate() {
                w[i] = u32::from_be_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
            }
            for i in 16..64 {
                let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
                let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
                w[i] = w[i - 16]
                    .wrapping_add(s0)
                    .wrapping_add(w[i - 7])
                    .wrapping_add(s1);
            }
            let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = self.state;
            for i in 0..64 {
                let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
                let ch = (e & f) ^ (!e & g);
                let t1 = h
                    .wrapping_add(s1)
                    .wrapping_add(ch)
                    .wrapping_add(K[i])
                    .wrapping_add(w[i]);
                let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
                let maj = (a & b) ^ (a & c) ^ (b & c);
                let t2 = s0.wrapping_add(maj);
                h = g;
                g = f;
                f = e;
                e = d.wrapping_add(t1);
                d = c;
                c = b;
                b = a;
                a = t1.wrapping_add(t2);
            }
            self.state[0] = self.state[0].wrapping_add(a);
            self.state[1] = self.state[1].wrapping_add(b);
            self.state[2] = self.state[2].wrapping_add(c);
            self.state[3] = self.state[3].wrapping_add(d);
            self.state[4] = self.state[4].wrapping_add(e);
            self.state[5] = self.state[5].wrapping_add(f);
            self.state[6] = self.state[6].wrapping_add(g);
            self.state[7] = self.state[7].wrapping_add(h);
        }
        fn update(&mut self, mut data: &[u8]) {
            self.total = self.total.wrapping_add(data.len() as u64);
            if self.buf_len > 0 {
                let need = 64 - self.buf_len;
                let take = need.min(data.len());
                self.buf[self.buf_len..self.buf_len + take].copy_from_slice(&data[..take]);
                self.buf_len += take;
                data = &data[take..];
                if self.buf_len == 64 {
                    let block = self.buf;
                    self.block(&block);
                    self.buf_len = 0;
                }
            }
            while data.len() >= 64 {
                let (block, rest) = data.split_at(64);
                self.block(block);
                data = rest;
            }
            if !data.is_empty() {
                self.buf[..data.len()].copy_from_slice(data);
                self.buf_len = data.len();
            }
        }
        fn finish(mut self) -> [u8; 32] {
            let bit_len = self.total.wrapping_mul(8);
            self.update(&[0x80]);
            while self.buf_len != 56 {
                self.update(&[0]);
            }
            // Length appended explicitly through the buffer path.
            self.buf[56..64].copy_from_slice(&bit_len.to_be_bytes());
            if self.buf_len == 56 {
                let block = self.buf;
                self.block(&block);
                self.buf_len = 0;
            }
            let mut out = [0u8; 32];
            for (i, word) in self.state.iter().enumerate() {
                out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
            }
            out
        }
    }
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update(part);
        hasher.update(&[0]);
    }
    hasher.finish().iter().map(|b| format!("{b:02x}")).collect()
}

fn peercred(stream: &UnixStream) -> Result<(i32, u32)> {
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let rc = unsafe {
        libc::getsockopt(
            std::os::unix::io::AsRawFd::as_raw_fd(stream),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut cred as *mut _ as *mut libc::c_void,
            &mut len,
        )
    };
    if rc != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok((cred.pid, cred.uid))
}

fn process_birth(pid: i32) -> Result<String> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let after = stat
        .rsplit(')')
        .nth(0)
        .and_then(|tail| tail.split_whitespace().nth(19))
        .ok_or_else(|| proto("unreadable peer process birth"))?;
    Ok(after.to_string())
}

fn boot_id() -> Result<String> {
    Ok(std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?
        .trim()
        .to_string())
}

/// One connected herdr socket with verified same-uid peer fencing.
pub struct Client {
    stream: UnixStream,
    reader: BufReader<UnixStream>,
    fingerprint: SessionFingerprint,
}

impl Client {
    /// Connect to a local herdr socket with ownership and peer fencing.
    pub fn connect_local(path: &Path) -> Result<Client> {
        let info = std::fs::symlink_metadata(path)?;
        if !info.file_type().is_socket() || info.uid() != unsafe { libc::getuid() } {
            return Err(proto("owned unix herdr socket required"));
        }
        let stream = UnixStream::connect(path)?;
        stream.set_read_timeout(Some(IO_TIMEOUT))?;
        stream.set_write_timeout(Some(IO_TIMEOUT))?;
        let (pid, uid) = peercred(&stream)?;
        let after = std::fs::symlink_metadata(path)?;
        if uid != unsafe { libc::getuid() }
            || (after.ino(), after.dev()) != (info.ino(), info.dev())
        {
            return Err(proto("herdr peer changed"));
        }
        let fingerprint = SessionFingerprint(sha256_hex(&[
            path.as_os_str().as_bytes(),
            &info.ino().to_be_bytes(),
            &info.dev().to_be_bytes(),
            &uid.to_be_bytes(),
            &pid.to_be_bytes(),
            process_birth(pid)?.as_bytes(),
            boot_id()?.as_bytes(),
        ]));
        let reader = BufReader::new(stream.try_clone()?);
        Ok(Client { stream, reader, fingerprint })
    }

    pub fn fingerprint(&self) -> &SessionFingerprint {
        &self.fingerprint
    }

    fn write_line(&mut self, line: &[u8]) -> Result<()> {
        self.stream.write_all(line)?;
        self.stream.write_all(b"\n")?;
        Ok(())
    }

    fn read_line(&mut self) -> Result<Vec<u8>> {
        let mut line = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            match self.reader.read(&mut byte) {
                Ok(0) => return Err(proto("herdr disconnected")),
                Ok(_) => {
                    if byte[0] == b'\n' {
                        break;
                    }
                    line.push(byte[0]);
                    if line.len() >= MAX_LINE {
                        return Err(proto("herdr reply too large"));
                    }
                }
                Err(e) => return Err(e.into()),
            }
        }
        Ok(line)
    }

    /// One-shot JSON-RPC call. Read-only inventory methods only; herdr closes
    /// the connection after the reply.
    pub fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        const ALLOWED: [&str; 4] = ["ping", "session.snapshot", "pane.read", "events.subscribe"];
        if !ALLOWED.contains(&method) {
            return Err(proto("inventory method not allowed"));
        }
        let id = sha256_hex(&[
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos().to_be_bytes())
                .unwrap_or([0u8; 16])
                .as_slice(),
            &std::process::id().to_be_bytes(),
        ]);
        let request = json!({"id": id, "method": method, "params": params});
        let mut line = serde_json::to_vec(&request).map_err(|e| proto(e.to_string()))?;
        line.retain(|&b| b != b'\n');
        self.write_line(&line)?;
        for _ in 0..128 {
            let reply_line = self.read_line()?;
            let reply: Value = serde_json::from_slice(&reply_line)
                .map_err(|e| proto(format!("invalid herdr reply: {e}")))?;
            if !reply.is_object() {
                return Err(proto("invalid herdr reply"));
            }
            if reply.get("id").and_then(Value::as_str) != Some(id.as_str()) {
                continue;
            }
            if reply.get("error").is_some() {
                return Err(proto("herdr rejected inventory request"));
            }
            return Ok(reply.get("result").cloned().unwrap_or(Value::Null));
        }
        Err(proto("missing herdr response"))
    }

    /// Long-lived event subscription. Entries are subscription objects such as
    /// `{"type": "pane.updated"}`; some types (pane.agent_status_changed)
    /// additionally require a pane_id. The subscription response is
    /// validated, then the connection streams one event line per change.
    pub fn subscribe(mut self, entries: Vec<Value>) -> Result<Subscription> {
        let id = "libherdr-events".to_string();
        let request = json!({
            "id": id,
            "method": "events.subscribe",
            "params": {"subscriptions": entries},
        });
        let mut line = serde_json::to_vec(&request).map_err(|e| proto(e.to_string()))?;
        line.retain(|&b| b != b'\n');
        self.write_line(&line)?;
        let reply_line = self.read_line()?;
        let reply: Value = serde_json::from_slice(&reply_line)
            .map_err(|e| proto(format!("invalid subscription reply: {e}")))?;
        if reply.get("error").is_some() {
            return Err(proto(format!(
                "events subscription failed: {}",
                reply["error"]["message"].as_str().unwrap_or("unknown")
            )));
        }
        if reply.get("id").and_then(Value::as_str) != Some(id.as_str()) {
            return Err(proto("events subscription id mismatch"));
        }
        if reply["result"]["type"].as_str() != Some("subscription_started") {
            return Err(proto("events subscription not started"));
        }
        self.stream.set_read_timeout(None)?;
        Ok(Subscription { client: self })
    }
}

pub struct Subscription {
    client: Client,
}

/// One streamed herdr event: canonical dotted name plus the raw payload.
#[derive(Debug, Clone)]
pub struct Event {
    pub name: String,
    pub data: Value,
}

fn canonical_event_name(name: &str) -> String {
    match name.strip_suffix("_changed") {
        Some(base) if base.starts_with("pane_agent_status") => "pane.agent_status_changed".into(),
        _ => name.replacen('_', ".", 1),
    }
}

impl Subscription {
    /// Blocks until the next event arrives or the connection fails.
    pub fn next_event(&mut self) -> Result<Event> {
        loop {
            let line = self.client.read_line()?;
            let value: Value = serde_json::from_slice(&line)
                .map_err(|e| proto(format!("invalid herdr event: {e}")))?;
            let name = value
                .get("event")
                .and_then(Value::as_str)
                .map(canonical_event_name);
            if let Some(name) = name {
                return Ok(Event {
                    name,
                    data: value.get("data").cloned().unwrap_or(Value::Null),
                });
            }
            // Lines without an event field are protocol noise; keep reading.
        }
    }
}

// --- Remote (SSH) transport -------------------------------------------------

/// A validated remote herdr target from the link registry.
#[derive(Debug, Clone)]
pub struct RemoteTarget {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub session: String,
}

impl RemoteTarget {
    pub fn validate(&self) -> Result<()> {
        let field = |v: &str, what: &str| -> Result<()> {
            if v.is_empty()
                || v.len() > 64
                || !v
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
            {
                return Err(Error::Remote(format!("invalid remote {what}")));
            }
            Ok(())
        };
        field(&self.user, "user")?;
        // Host may be a dotted IPv4/hostname; brackets/colons for IPv6 rejected.
        if self.host.is_empty()
            || self.host.len() > 253
            || !self
                .host
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || ".-".contains(c))
        {
            return Err(Error::Remote("invalid remote host".into()));
        }
        if self.port == 0 {
            return Err(Error::Remote("invalid remote port".into()));
        }
        field(&self.session, "session")?;
        Ok(())
    }
}

/// Remote peer identity reported by the probe.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RemotePeer {
    pub socket: String,
    pub uid: u32,
    pub pid: i32,
    pub birth: String,
    pub boot: String,
    pub ino: u64,
    pub dev: u64,
}

/// Remote probe: resolves the herdr binary outside the non-interactive SSH
/// PATH (the exact regression this fixes), verifies session/socket ownership,
/// and prints one JSON line with the peer identity. Runs only on the
/// explicitly registered host over the pinned ssh argv.
const PROBE: &str = r#"
import json,os,pathlib,re,shutil,socket,stat,struct,subprocess,sys
name=sys.argv[1]
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',name): raise ValueError('session')
env={k:v for k,v in os.environ.items() if not k.startswith('HERDR_')}
exe=shutil.which('herdr')
if exe is None:
 for c,u in ((pathlib.Path.home()/'.local/bin/herdr',os.getuid()),(pathlib.Path.home()/'.cargo/bin/herdr',os.getuid()),(pathlib.Path('/usr/local/bin/herdr'),0),(pathlib.Path('/usr/bin/herdr'),0)):
  try:
   i=c.lstat()
   if stat.S_ISREG(i.st_mode) and i.st_uid==u and os.access(c,os.X_OK): exe=str(c);break
  except OSError: pass
if exe is None: raise ValueError('herdr')
r=subprocess.run([exe,'session','list','--json'],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=3)
if r.returncode or len(r.stdout.encode())>262144: raise ValueError('metadata')
rows=json.loads(r.stdout)['sessions']
if not isinstance(rows,list) or len(rows)>128: raise ValueError('sessions')
selected=[x for x in rows if isinstance(x,dict) and x.get('running') is True and ('default' if x.get('default') is True else x.get('name'))==name]
if len(selected)!=1: raise ValueError('ambiguous or inactive session')
config=pathlib.Path(os.environ.get('XDG_CONFIG_HOME',pathlib.Path.home()/'.config'))
p=config/'herdr'/'herdr.sock' if name=='default' else config/'herdr'/'sessions'/name/'herdr.sock'
if not p.is_absolute() or '..' in p.parts or not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(p)) or len(str(p).encode())>100 or selected[0].get('socket_path')!=str(p): raise ValueError('socket')
i=p.lstat()
if not stat.S_ISSOCK(i.st_mode) or i.st_uid!=os.getuid(): raise ValueError('owned socket')
s=socket.socket(socket.AF_UNIX);s.settimeout(2)
try:
 s.connect(str(p));pid,uid,_=struct.unpack('3i',s.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
 after=p.lstat()
 if uid!=os.getuid() or (after.st_ino,after.st_dev)!=(i.st_ino,i.st_dev): raise ValueError('peer')
 birth=pathlib.Path('/proc/%s/stat'%pid).read_text().rsplit(')',1)[1].split()[19]
 boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
 print(json.dumps(dict(socket=str(p),uid=uid,pid=pid,birth=birth,boot=boot,ino=i.st_ino,dev=i.st_dev)))
finally: s.close()
"#;

fn ssh_base_argv(target: &RemoteTarget, control_master: bool) -> Result<Vec<String>> {
    target.validate()?;
    let mut argv = vec![
        "-F".into(),
        "/dev/null".into(),
        "-T".into(),
        "-o".into(),
        "BatchMode=yes".into(),
        "-o".into(),
        "StrictHostKeyChecking=yes".into(),
        "-o".into(),
        "UpdateHostKeys=no".into(),
        "-o".into(),
        "ConnectTimeout=5".into(),
        "-o".into(),
        "ConnectionAttempts=1".into(),
    ];
    if control_master {
        argv.push("-M".into());
    } else {
        argv.push("-o".into());
        argv.push("ControlMaster=no".into());
        argv.push("-o".into());
        argv.push("ControlPath=none".into());
        argv.push("-o".into());
        argv.push("ControlPersist=no".into());
    }
    argv.push("-o".into());
    argv.push("ForwardAgent=no".into());
    argv.push("-o".into());
    argv.push("ForwardX11=no".into());
    argv.push("-p".into());
    argv.push(target.port.to_string());
    Ok(argv)
}

fn destination(target: &RemoteTarget) -> String {
    format!("{}@{}", target.user, target.host)
}

/// Run the probe over SSH and parse the bounded peer identity.
pub fn probe_remote(target: &RemoteTarget) -> Result<RemotePeer> {
    let mut argv = ssh_base_argv(target, false)?;
    argv.push(destination(target));
    argv.push(format!("python3 -c {} {}", shell_quote(PROBE), shell_quote(&target.session)));
    let output = Command::new("ssh")
        .args(&argv)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()?;
    if !output.status.success() {
        return Err(Error::Remote(
            "remote host unavailable or SSH trust/authentication required".into(),
        ));
    }
    if output.stdout.len() > 8192 {
        return Err(Error::Remote("remote metadata too large".into()));
    }
    let row: Value = serde_json::from_slice(&output.stdout)
        .map_err(|e| Error::Remote(format!("invalid remote identity: {e}")))?;
    let obj = row.as_object().ok_or_else(|| Error::Remote("invalid remote identity".into()))?;
    let expected: [&str; 7] = ["socket", "uid", "pid", "birth", "boot", "ino", "dev"];
    let mut keys: Vec<&String> = obj.keys().collect();
    keys.sort();
    let mut sorted_expected = expected;
    sorted_expected.sort();
    if keys.iter().map(|k| k.as_str()).collect::<Vec<_>>() != sorted_expected {
        return Err(Error::Remote("invalid remote identity".into()));
    }
    let socket = obj["socket"].as_str().unwrap_or("");
    if socket.is_empty()
        || socket.len() > 100
        || !socket.starts_with('/')
        || !socket.ends_with("/herdr.sock")
        || socket.contains("..")
        || !socket
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "/._-".contains(c))
    {
        return Err(Error::Remote("invalid remote peer metadata".into()));
    }
    let int_field = |k: &str| -> Result<u64> {
        obj[k]
            .as_u64()
            .ok_or_else(|| Error::Remote(format!("invalid remote {k}")))
    };
    let peer = RemotePeer {
        socket: socket.to_string(),
        uid: int_field("uid")? as u32,
        pid: int_field("pid")? as i32,
        birth: obj["birth"].as_str().unwrap_or("").to_string(),
        boot: obj["boot"].as_str().unwrap_or("").to_string(),
        ino: int_field("ino")?,
        dev: int_field("dev")?,
    };
    if peer.pid == 0
        || peer.birth.is_empty()
        || !peer.birth.chars().all(|c| c.is_ascii_digit())
        || peer.boot.is_empty()
        || peer.boot.len() > 64
    {
        return Err(Error::Remote("invalid remote peer metadata".into()));
    }
    Ok(peer)
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

/// Owned SSH bridge: one master process forwarding the remote herdr socket to
/// a private local unix socket. Connections through it are plain Clients.
pub struct RemoteBridge {
    dir: PathBuf,
    local_socket: PathBuf,
    control: PathBuf,
    master: Child,
    pub peer: RemotePeer,
}

impl RemoteBridge {
    pub fn establish(target: &RemoteTarget) -> Result<RemoteBridge> {
        let peer = probe_remote(target)?;
        let dir = std::env::temp_dir().join(format!("libherdr-ssh-{}", std::process::id()));
        std::fs::create_dir_all(&dir)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))?;
        }
        let local_socket = dir.join("bridge.sock");
        let control = dir.join("control.sock");
        let mut argv = ssh_base_argv(target, true)?;
        argv.push("-S".into());
        argv.push(control.to_string_lossy().into_owned());
        argv.push("-N".into());
        argv.push("-o".into());
        argv.push("ExitOnForwardFailure=yes".into());
        argv.push("-o".into());
        argv.push("StreamLocalBindUnlink=no".into());
        argv.push("-o".into());
        argv.push("ServerAliveInterval=10".into());
        argv.push("-o".into());
        argv.push("ServerAliveCountMax=1".into());
        argv.push("-L".into());
        argv.push(format!("{}:{}", local_socket.display(), peer.socket));
        argv.push(destination(target));
        let master = Command::new("ssh")
            .args(&argv)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let deadline = Instant::now() + Duration::from_secs(5);
        let bridge = RemoteBridge { dir, local_socket, control, master, peer };
        while Instant::now() < deadline {
            if let Ok(info) = std::fs::symlink_metadata(&bridge.local_socket) {
                if !info.file_type().is_socket() || info.uid() != unsafe { libc::getuid() } {
                    let _ = bridge.close();
                    return Err(Error::Remote("invalid owned bridge socket".into()));
                }
                return Ok(bridge);
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        let _ = bridge.close();
        Err(Error::Remote("SSH bridge timed out".into()))
    }

    pub fn connect(&self) -> Result<Client> {
        Client::connect_local(&self.local_socket)
    }

    pub fn close(mut self) -> Result<()> {
        let _ = Command::new("ssh")
            .args(["-S", &self.control.to_string_lossy(), "-O", "exit", "bridge"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        let _ = self.master.kill();
        let _ = self.master.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
        Ok(())
    }
}

impl Drop for RemoteBridge {
    fn drop(&mut self) {
        let _ = self.master.kill();
        let _ = self.master.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Default local herdr socket for a session name.
pub fn local_socket_path(session: &str) -> PathBuf {
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").expect("HOME")).join(".config")
        })
        .join("herdr");
    if session == "default" {
        config.join("herdr.sock")
    } else {
        config.join("sessions").join(session).join("herdr.sock")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_matches_known_vector() {
        // SHA-256 of the NUL-joined parts: "abc\0" here.
        assert_eq!(
            sha256_hex(&[b"abc"]),
            "dc1114cd074914bd872cc1f9a23ec910ea2203bc79779ab2e17da25782a624fc"
        );
    }

    #[test]
    fn remote_target_validation() {
        let good = RemoteTarget {
            host: "192.168.1.177".into(),
            port: 22,
            user: "node".into(),
            session: "default".into(),
        };
        assert!(good.validate().is_ok());
        let bad = RemoteTarget { host: "a;b".into(), ..good.clone() };
        assert!(bad.validate().is_err());
        let bad = RemoteTarget { user: "root;rm".into(), ..good.clone() };
        assert!(bad.validate().is_err());
        let bad = RemoteTarget { port: 0, ..good.clone() };
        assert!(bad.validate().is_err());
    }

    #[test]
    fn event_name_aliases() {
        assert_eq!(canonical_event_name("pane_agent_status_changed"), "pane.agent_status_changed");
        assert_eq!(canonical_event_name("pane.updated"), "pane.updated");
        assert_eq!(canonical_event_name("pane_created"), "pane.created");
    }
}
