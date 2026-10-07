use crate::{
    expand_path, home, http, now, private_dir, store::Store, text, write_private, Result,
    MAX_ARTIFACT_BYTES,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::{Read, Write},
    net::{TcpStream, UdpSocket},
    os::unix::{
        fs::{OpenOptionsExt, PermissionsExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

#[derive(Clone)]
pub struct Paths {
    pub root: PathBuf,
}
impl Default for Paths {
    fn default() -> Self {
        Self {
            root: std::env::var_os("XDG_STATE_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| home().join(".local/state"))
                .join("omarchy/ares"),
        }
    }
}
impl Paths {
    pub fn file(&self, name: &str) -> PathBuf {
        self.root.join(name)
    }
}
pub fn valid_token(s: &str) -> bool {
    (32..=256).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
fn random_token() -> Result<String> {
    let mut bytes = [0u8; 32];
    let mut offset = 0;
    while offset < bytes.len() {
        // Linux getrandom supplies the same OS CSPRNG used by secrets.token_urlsafe.
        let n = unsafe {
            libc::getrandom(bytes[offset..].as_mut_ptr().cast(), bytes.len() - offset, 0)
        };
        if n < 0 {
            let e = std::io::Error::last_os_error();
            if e.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(e.into());
        }
        if n == 0 {
            return Err("random source unavailable".into());
        }
        offset += n as usize;
    }
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    let mut acc = 0u32;
    let mut bits = 0;
    for byte in bytes {
        acc = (acc << 8) | u32::from(byte);
        bits += 8;
        while bits >= 6 {
            bits -= 6;
            out.push(ALPHABET[((acc >> bits) & 63) as usize] as char);
        }
    }
    if bits > 0 {
        out.push(ALPHABET[((acc << (6 - bits)) & 63) as usize] as char);
    }
    Ok(out)
}
pub fn token(paths: &Paths) -> Result<String> {
    let path = paths.file("access.token");
    let value = match fs::read_to_string(&path) {
        Ok(s) => s.trim().to_owned(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            let v = random_token()?;
            write_private(&path, format!("{v}\n").as_bytes())?;
            v
        }
        Err(e) => return Err(e.into()),
    };
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(value)
}
pub fn process_alive(pid: i64) -> bool {
    if pid <= 0 {
        return false;
    }
    fs::read(format!("/proc/{pid}/cmdline"))
        .ok()
        .is_some_and(|b| {
            let line = String::from_utf8_lossy(&b).replace('\0', " ");
            line.contains("ares-server") && line.contains(" serve")
        })
}
fn raw_state(paths: &Paths) -> Value {
    fs::read(paths.file("server.json"))
        .ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .filter(Value::is_object)
        .unwrap_or(json!({}))
}
pub fn state(paths: &Paths) -> Option<Value> {
    let v = raw_state(paths);
    if process_alive(v["pid"].as_i64().unwrap_or(0)) {
        Some(v)
    } else {
        None
    }
}
fn ensure_started(paths: &Paths, host: Option<&str>, port: Option<u16>) -> Result<Option<Value>> {
    if let Some(current) = state(paths) {
        return Ok(Some(current));
    }
    private_dir(&paths.root)?;
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(paths.file("server.log"))?;
    let mut command = Command::new(std::env::current_exe()?);
    command.arg("serve");
    if let Some(host) = host {
        command.args(["--host", host]);
    }
    if let Some(port) = port {
        command.args(["--port", &port.to_string()]);
    }
    command
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log);
    // Only async-signal-safe libc operations are used between fork and exec.
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    let mut child = command.spawn()?;
    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        thread::sleep(Duration::from_millis(100));
        if let Some(current) = state(paths) {
            return Ok(Some(current));
        }
        if child.try_wait()?.is_some() {
            break;
        }
    }
    Ok(None)
}
fn stop_server(paths: &Paths) -> Result<bool> {
    let Some(current) = state(paths) else {
        return Ok(true);
    };
    let pid = current["pid"].as_i64().ok_or("invalid server pid")?;
    if unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) } < 0 {
        let e = std::io::Error::last_os_error();
        if e.raw_os_error() != Some(libc::ESRCH) {
            return Err(e.into());
        }
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline && process_alive(pid) {
        thread::sleep(Duration::from_millis(100));
    }
    Ok(!process_alive(pid))
}
fn lan_address() -> String {
    (|| -> std::io::Result<String> {
        let socket = UdpSocket::bind("0.0.0.0:0")?;
        socket.connect("192.0.2.1:9")?;
        Ok(socket.local_addr()?.ip().to_string())
    })()
    .unwrap_or_else(|_| "127.0.0.1".into())
}
pub fn print(value: Value) {
    // Python's CLI uses json.dumps defaults, unlike the compact HTTP protocol.
    // Preserve its field order, separator spaces, and ensure_ascii behavior.
    let mut fields = Vec::new();
    for key in [
        "ok",
        "running",
        "pid",
        "endpoint",
        "mobileEndpoint",
        "artifactRoot",
        "token",
        "restarted",
        "error",
    ] {
        if let Some(value) = value.get(key) {
            fields.push(format!("\"{key}\": {value}"));
        }
    }
    let raw = format!("{{{}}}", fields.join(", "));
    let mut ascii = String::new();
    for c in raw.chars() {
        if c.is_ascii() {
            ascii.push(c);
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                ascii.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    println!("{ascii}");
}
fn connection(paths: &Paths, start: bool) -> Result<i32> {
    let current = if start {
        ensure_started(paths, None, None).unwrap_or(None)
    } else {
        state(paths)
    };
    let Some(current) = current else {
        print(json!({"ok":false,"running":false,"error":"Ares service unavailable"}));
        return Ok(1);
    };
    let port = current["port"].as_u64().ok_or("invalid server port")?;
    print(
        json!({"ok":true,"running":true,"pid":current["pid"],"endpoint":format!("http://127.0.0.1:{port}"),"mobileEndpoint":format!("http://{}:{port}",lan_address()),"artifactRoot":paths.file("artifacts"),"token":token(paths)?}),
    );
    Ok(0)
}
/// Minimal HTTP/1.1 client: these CLI requests only target the local Ares server.
fn request(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
    timeout: Duration,
) -> Result<(u16, Vec<u8>)> {
    let mut stream = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    write!(stream,"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nContent-Length: {}\r\n",body.len())?;
    for (k, v) in headers {
        if k.contains(['\r', '\n']) || v.contains(['\r', '\n']) {
            return Err("invalid HTTP header".into());
        }
        write!(stream, "{k}: {v}\r\n")?;
    }
    stream.write_all(b"\r\n")?;
    stream.write_all(body)?;
    let mut response = Vec::new();
    stream.read_to_end(&mut response)?;
    let boundary = response
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or("invalid HTTP response")?;
    let head = std::str::from_utf8(&response[..boundary])?;
    let status = head
        .lines()
        .next()
        .and_then(|s| s.split_whitespace().nth(1))
        .ok_or("invalid HTTP status")?
        .parse()?;
    Ok((status, response[boundary + 4..].to_vec()))
}
fn wait_ready(port: u16) -> bool {
    let deadline = Instant::now() + Duration::from_secs(60);
    while Instant::now() < deadline {
        if request(port, "GET", "/v1/health", &[], &[], Duration::from_secs(2))
            .is_ok_and(|r| r.0 == 200)
        {
            return true;
        }
        thread::sleep(Duration::from_millis(500));
    }
    false
}
fn configure_token(paths: &Paths, value: Option<&str>, regenerate: bool) -> Result<i32> {
    if value.is_none() && !regenerate {
        print(json!({"ok":true,"token":token(paths)?}));
        return Ok(0);
    }
    let value = if regenerate {
        random_token()?
    } else {
        let v = value.unwrap_or("").trim();
        if !valid_token(v) {
            print(json!({"ok":false,"error":"Token must be 32-256 letters, digits, '_' or '-'"}));
            return Ok(1);
        }
        v.to_owned()
    };
    write_private(&paths.file("access.token"), format!("{value}\n").as_bytes())?;
    let current = state(paths);
    let running = current.is_some();
    if let Some(current) = current {
        let host = if text(&current, "host").is_empty() {
            "0.0.0.0"
        } else {
            text(&current, "host")
        };
        let port = current["port"].as_u64().ok_or("invalid server port")? as u16;
        if !stop_server(paths)? {
            print(json!({"ok":false,"error":"Token saved but the running service would not stop"}));
            return Ok(1);
        }
        let _ = ensure_started(paths, Some(host), Some(port));
        if !wait_ready(port) {
            print(json!({"ok":false,"error":"Token saved but the service did not restart"}));
            return Ok(1);
        }
    }
    print(json!({"ok":true,"token":value,"restarted":running}));
    Ok(0)
}
fn upload(paths: &Paths, path: &str, project: &str, room: &str) -> Result<i32> {
    let current = state(paths).ok_or("Ares service unavailable")?;
    let path = expand_path(path).canonicalize()?;
    let metadata = path.metadata()?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_ARTIFACT_BYTES as u64 {
        return Err("image size is invalid".into());
    }
    let mime = mime_guess::from_path(&path)
        .first_or_octet_stream()
        .to_string();
    if !mime.starts_with("image/") {
        return Err("selected file is not an image".into());
    }
    let name = path
        .file_name()
        .ok_or("invalid file name")?
        .to_string_lossy();
    let query = url::form_urlencoded::Serializer::new(String::new())
        .extend_pairs([
            ("projectId", project),
            ("roomId", room),
            ("name", &name),
            ("author", "user"),
        ])
        .finish();
    let auth = format!("Bearer {}", token(paths)?);
    let (status, body) = request(
        current["port"].as_u64().ok_or("invalid server port")? as u16,
        "PUT",
        &format!("/v1/artifacts?{query}"),
        &[("Authorization", &auth), ("Content-Type", &mime)],
        &fs::read(path)?,
        Duration::from_secs(30),
    )?;
    if status >= 400 {
        let message = serde_json::from_slice::<Value>(&body)
            .ok()
            .and_then(|v| v["error"].as_str().map(str::to_owned))
            .unwrap_or(format!("HTTP {status}"));
        print(json!({"ok":false,"error":message}));
        return Ok(1);
    }
    println!("{}", String::from_utf8(body)?);
    Ok(0)
}
fn tty(paths: &Paths, room: &str) -> Result<i32> {
    let current = state(paths).ok_or("Ares service unavailable")?;
    let auth = format!("Bearer {}", token(paths)?);
    let encoded: String = url::form_urlencoded::byte_serialize(room.as_bytes()).collect();
    let (status, body) = request(
        current["port"].as_u64().ok_or("invalid server port")? as u16,
        "POST",
        &format!("/v1/tty/rooms/{encoded}/focus"),
        &[
            ("Authorization", &auth),
            ("Content-Type", "application/json"),
        ],
        b"{}",
        Duration::from_secs(75),
    )?;
    println!("{}", String::from_utf8(body)?);
    Ok(if status < 400 { 0 } else { 1 })
}
struct StateGuard {
    path: PathBuf,
    pid: u32,
}
impl Drop for StateGuard {
    fn drop(&mut self) {
        if fs::read(&self.path)
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
            .is_some_and(|v| v["pid"].as_u64() == Some(self.pid as u64))
        {
            let _ = fs::remove_file(&self.path);
        }
    }
}
async fn serve(paths: &Paths, host: &str, port: u16) -> Result<i32> {
    if state(paths).is_some() {
        return Ok(2);
    }
    let access_token = token(paths)?;
    let hermes = std::env::var_os("HERMES_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".hermes"));
    let store = Store::open(&paths.file("ares.db"), &paths.file("artifacts"), hermes)?;
    store.db.execute("DELETE FROM room_events WHERE kind IN ('message.delta','reasoning.delta','thinking.delta') AND COALESCE(json_extract(payload,'$.ephemeral'),0)!=1",[])?;
    let listener = tokio::net::TcpListener::bind((host, port)).await?;
    let actual = listener.local_addr()?.port();
    let store = std::sync::Arc::new(std::sync::Mutex::new(store));
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    // Explicit isolation mode for bridge-less conformance fixtures.
    let mut runtime = if std::env::var("ARES_NO_HERMES").as_deref() == Ok("1") {
        None
    } else {
        Some(crate::sync::Runtime::start(store.clone(), &paths.file("server.log")).await?)
    };
    let pid = std::process::id();
    write_private(
        &paths.file("server.json"),
        format!(
            "{}\n",
            json!({"pid":pid,"port":actual,"host":host,"startedAt":now()})
        )
        .as_bytes(),
    )?;
    let _guard = StateGuard {
        path: paths.file("server.json"),
        pid,
    };
    let server = axum::serve(
        listener,
        http::integrated_router(http::App {
            store,
            token_hash: crate::digest(&access_token),
            web_root: Path::new(http::WEB_ROOT).into(),
            bridge: runtime.as_ref().map(|r| r.bridge.clone()),
            mirror: runtime.as_ref().map(|r| r.mirror.clone()),
        }).into_make_service_with_connect_info::<std::net::SocketAddr>(),
    )
    .with_graceful_shutdown(async move {
        tokio::select! {_=term.recv()=>(),_=interrupt.recv()=>()}
    });
    let result = server.await;
    if let Some(runtime) = &mut runtime {
        runtime.shutdown().await;
    }
    result?;
    Ok(0)
}
pub async fn run(args: Vec<String>) -> Result<i32> {
    let usage="Usage: ares-server {serve [--host HOST] [--port PORT]|connection|status|stop|token [--set VALUE|--regenerate]|tty --room ID|upload --project P --room R --path F}";
    let Some(command) = args.first() else {
        eprintln!("{usage}");
        return Ok(2);
    };
    if command == "--help" || command == "-h" {
        println!("{usage}");
        return Ok(0);
    }
    let mut options = HashMap::new();
    let mut i = 1;
    while i < args.len() {
        if args[i] == "--help" || args[i] == "-h" {
            println!("{usage}");
            return Ok(0);
        }
        if args[i] == "--regenerate" {
            options.insert(args[i].clone(), String::new());
            i += 1;
            continue;
        }
        let Some(value) = args.get(i + 1) else {
            return Err(format!("missing value for {}", args[i]).into());
        };
        options.insert(args[i].clone(), value.clone());
        i += 2;
    }
    let allowed: &[&str] = match command.as_str() {
        "serve" => &["--host", "--port"],
        "token" => &["--set", "--regenerate"],
        "tty" => &["--room"],
        "upload" => &["--project", "--room", "--path"],
        "connection" | "status" | "stop" => &[],
        _ => {
            eprintln!("{usage}");
            return Ok(2);
        }
    };
    if options.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err("unrecognized argument".into());
    }
    let paths = Paths::default();
    if command == "serve" {
        return serve(
            &paths,
            options
                .get("--host")
                .map(String::as_str)
                .unwrap_or("0.0.0.0"),
            options
                .get("--port")
                .map(|p| p.parse::<u16>())
                .transpose()?
                .unwrap_or(9420),
        )
        .await;
    }
    // Blocking supervision and the small local HTTP client run outside Tokio workers.
    let command = command.clone();
    tokio::task::spawn_blocking(move || match command.as_str() {
        "connection" => connection(&paths, true),
        "status" => connection(&paths, false),
        "stop" => {
            let stopped = stop_server(&paths)?;
            print(json!({"ok":stopped,"running":!stopped}));
            Ok(if stopped { 0 } else { 1 })
        }
        "token" => {
            if options.contains_key("--set") && options.contains_key("--regenerate") {
                return Err("--set and --regenerate are mutually exclusive".into());
            }
            configure_token(
                &paths,
                options.get("--set").map(String::as_str),
                options.contains_key("--regenerate"),
            )
        }
        "upload" => upload(
            &paths,
            options.get("--path").ok_or("--path is required")?,
            options.get("--project").ok_or("--project is required")?,
            options.get("--room").ok_or("--room is required")?,
        ),
        "tty" => tty(&paths, options.get("--room").ok_or("--room is required")?),
        _ => unreachable!(),
    })
    .await?
}
