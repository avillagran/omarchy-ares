//! Private Desktop transport. Credentials and transport errors never reach logs.
use crate::{ident, Result};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    net::IpAddr,
    os::unix::fs::MetadataExt,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU32, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::sync::{broadcast, mpsc, oneshot, watch};
use tokio_tungstenite::tungstenite::{protocol::WebSocketConfig, Message};
use url::Url;

const BOOTSTRAP_MAX: usize = 2 * 1024 * 1024;
const MESSAGE_MAX: usize = 40 * 1024 * 1024;
struct Request {
    id: String,
    method: String,
    params: Value,
    reply: oneshot::Sender<Value>,
}
#[derive(Clone)]
pub struct HermesBroker {
    requests: mpsc::Sender<Request>,
    events: broadcast::Sender<Value>,
    connected: Arc<AtomicBool>,
    stop: watch::Sender<bool>,
    timeouts: Arc<AtomicU32>,
    poison: Arc<tokio::sync::Notify>,
}
fn failure(message: &str) -> Value {
    json!({"ok":false,"error":message})
}
pub fn reconnect_backoff(attempt: u32) -> Duration {
    Duration::from_millis((250u64.saturating_mul(1u64 << attempt.min(5))).min(5000))
}
fn client() -> Result<reqwest::Client> {
    Ok(reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(10))
        .build()?)
}
async fn bounded(response: reqwest::Response, cap: usize) -> Result<Vec<u8>> {
    if response.status() != 200 {
        return Err("Desktop HTTP read rejected".into());
    }
    let mut body = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        if body.len() + chunk.len() > cap {
            return Err("Desktop response too large".into());
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}
pub fn bootstrap_url(base: &Url, html: &[u8]) -> Result<(Url, String)> {
    if html.len() > BOOTSTRAP_MAX {
        return Err("Desktop bootstrap response too large".into());
    }
    let re = regex::bytes::Regex::new(r#"__HERMES_SESSION_TOKEN__\s*=\s*("(?:\\.|[^"\\])*")"#)?;
    let capture = re
        .captures(html)
        .ok_or("Desktop session bootstrap unavailable")?;
    let token: String = serde_json::from_slice(&capture[1])?;
    if token.is_empty() {
        return Err("Desktop session bootstrap unavailable".into());
    }
    let mut ws = base.join("/api/ws")?;
    ws.set_scheme("ws").map_err(|_| "invalid endpoint")?;
    ws.query_pairs_mut().append_pair("token", &token);
    Ok((ws, token))
}
fn endpoint(host: &str, port: u16) -> Result<Url> {
    let ip: IpAddr = host.trim_matches(['[', ']']).parse()?;
    if !ip.is_loopback() || port == 0 {
        return Err("Invalid Desktop loopback endpoint".into());
    }
    Ok(Url::parse(&format!(
        "http://{}/",
        std::net::SocketAddr::new(ip, port)
    ))?)
}
fn is_hermes_serve(args: &[&str]) -> bool {
    let module =
        args.get(1..3) == Some(&["-m", "hermes_cli.main"][..]) && args[3..].contains(&"serve");
    let script = args.len() >= 3
        && std::path::Path::new(args[1])
            .file_name()
            .is_some_and(|s| s == "hermes")
        && args[2..].contains(&"serve");
    let bootstrap = args.get(1..3) == Some(&["-I", "-c"][..])
        && args.get(3..).is_some_and(|a| a.contains(&"serve"))
        && args.get(3).is_some_and(|code| {
            code.contains("import hermes_bootstrap")
                && code.contains("from hermes_cli.main import main")
                && code.contains("sys.exit(main())")
        });
    module || script || bootstrap
}
/// Match same-user default-profile serve processes to their kernel-owned listeners.
fn discover() -> Result<Url> {
    let home = std::env::var_os("HERMES_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::home().join(".hermes"));
    let home = home.canonicalize().unwrap_or(home);
    let uid = unsafe { libc::getuid() };
    let mut listeners = HashMap::new();
    for table in ["tcp", "tcp6"] {
        let data = std::fs::read_to_string(format!("/proc/net/{table}")).unwrap_or_default();
        for line in data.lines().skip(1) {
            let f: Vec<_> = line.split_whitespace().collect();
            if f.len() < 10 || f[3] != "0A" || f[7].parse::<u32>().ok() != Some(uid) {
                continue;
            }
            let Some((raw, port)) = f[1].split_once(':') else {
                continue;
            };
            let Ok(mut bytes) = hex::decode(raw) else {
                continue;
            };
            if cfg!(target_endian = "little") {
                for word in bytes.chunks_mut(4) {
                    word.reverse();
                }
            }
            let ip = match bytes.len() {
                4 => IpAddr::from(<[u8; 4]>::try_from(bytes.as_slice()).unwrap()),
                16 => IpAddr::from(<[u8; 16]>::try_from(bytes.as_slice()).unwrap()),
                _ => continue,
            };
            if ip.is_loopback() {
                if let Ok(port) = u16::from_str_radix(port, 16) {
                    listeners.insert(f[9].to_owned(), (ip, port));
                }
            }
        }
    }
    let mut candidates = Vec::new();
    for process in std::fs::read_dir("/proc")?.flatten() {
        let Ok(pid) = process.file_name().to_string_lossy().parse::<u32>() else {
            continue;
        };
        if !process.metadata().is_ok_and(|m| m.uid() == uid) {
            continue;
        }
        let path = process.path();
        let Ok(cmd) = std::fs::read_to_string(path.join("cmdline")) else {
            continue;
        };
        let args: Vec<_> = cmd.trim_end_matches('\0').split('\0').collect();
        if !is_hermes_serve(&args) {
            continue;
        }
        if !std::path::Path::new(args[0]).is_absolute()
            || !std::fs::metadata(args[0]).is_ok_and(|m| m.mode() & 0o111 != 0)
        {
            continue;
        }
        let mut valid = true;
        for (i, arg) in args.iter().enumerate() {
            let profile = if ["--profile", "-p"].contains(arg) {
                args.get(i + 1).copied()
            } else {
                arg.strip_prefix("--profile=")
                    .or_else(|| arg.strip_prefix("-p").filter(|s| !s.is_empty()))
            };
            if profile.is_some_and(|p| p != "default") {
                valid = false;
            }
        }
        if !valid {
            continue;
        }
        let Ok(env) = std::fs::read_to_string(path.join("environ")) else {
            continue;
        };
        let env: HashMap<_, _> = env.split('\0').filter_map(|s| s.split_once('=')).collect();
        if !["", "default"].contains(&env.get("HERMES_PROFILE").copied().unwrap_or("default")) {
            continue;
        }
        let ph = env
            .get("HERMES_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(env.get("HOME").copied().unwrap_or("")).join(".hermes")
            });
        if ph.canonicalize().unwrap_or(ph) != home {
            continue;
        }
        let Ok(fds) = std::fs::read_dir(path.join("fd")) else {
            continue;
        };
        for fd in fds.flatten() {
            if let Ok(link) = std::fs::read_link(fd.path()) {
                let link = link.to_string_lossy();
                if let Some(inode) = link
                    .strip_prefix("socket:[")
                    .and_then(|s| s.strip_suffix(']'))
                {
                    if let Some((ip, port)) = listeners.get(inode) {
                        candidates.push((pid, *ip, *port));
                    }
                }
            }
        }
    }
    candidates.sort();
    let (_, ip, port) = candidates
        .first()
        .ok_or("Default-profile Desktop server unavailable")?;
    endpoint(&ip.to_string(), *port)
}
type Pending = HashMap<String, oneshot::Sender<Value>>;
fn dispatch(item: Value, pending: &mut Pending, events: &broadcast::Sender<Value>) {
    if item["method"] == "event" && item["params"].is_object() {
        let _ = events.send(item["params"].clone());
        return;
    }
    let Some(id) = item["id"].as_str() else {
        return;
    };
    let Some(reply) = pending.remove(id) else {
        return;
    };
    let response = if item["jsonrpc"] != "2.0"
        || item.get("result").is_some() == item.get("error").is_some()
    {
        failure("Invalid Desktop RPC envelope")
    } else if item["result"].is_object() {
        json!({"ok":true,"result":item["result"]})
    } else {
        let mut v = failure("Desktop RPC rejected");
        if let Some(code) = item["error"]["code"]
            .as_i64()
            .filter(|c| c.unsigned_abs() < 1000000)
        {
            v["code"] = json!(code);
        }
        v
    };
    let _ = reply.send(response);
}
async fn persisted(client: &reqwest::Client, base: &Url, token: &str, p: &Value) -> Result<Value> {
    let sid = crate::require(crate::text(p, "session_id"), "session", 256)?;
    let profile = p["profile"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or("default");
    if !regex::Regex::new(r"^[A-Za-z0-9_-]{1,64}$")?.is_match(profile) {
        return Err("invalid profile".into());
    }
    let limit = p["limit"].as_u64().unwrap_or(500);
    let since = p["since"].as_f64().unwrap_or(0.0);
    if !(1..=500).contains(&limit) || since < 0.0 {
        return Err("invalid transcript request".into());
    }
    let mut url = base.clone();
    url.path_segments_mut()
        .map_err(|_| "invalid endpoint")?
        .clear()
        .extend(["api", "sessions", &sid]);
    url.query_pairs_mut().append_pair("profile", profile);
    let detail: Value = serde_json::from_slice(
        &bounded(
            client.get(url.clone()).bearer_auth(token).send().await?,
            MESSAGE_MAX,
        )
        .await?,
    )?;
    let messages = if detail["last_activity_at"].as_f64().unwrap_or(0.0) <= since {
        json!([])
    } else {
        url.path_segments_mut()
            .map_err(|_| "invalid endpoint")?
            .push("messages");
        url.query_pairs_mut()
            .append_pair("limit", &limit.to_string())
            .append_pair("order", "latest")
            .append_pair("include_compacted", "true");
        let v: Value = serde_json::from_slice(
            &bounded(
                client.get(url).bearer_auth(token).send().await?,
                MESSAGE_MAX,
            )
            .await?,
        )?;
        if !v["messages"].is_array() {
            return Err("invalid transcript".into());
        }
        v["messages"].clone()
    };
    let mut metadata = json!({});
    for key in [
        "id",
        "last_activity_at",
        "last_activity_description",
        "last_activity_provenance",
        "ended_at",
    ] {
        metadata[key] = detail[key].clone();
    }
    Ok(json!({"ok":true,"result":{"messages":messages,"session":metadata}}))
}
impl HermesBroker {
    pub fn start() -> Self {
        Self::with_endpoint(None)
    }
    pub fn with_endpoint(base: Option<Url>) -> Self {
        let (tx, mut rx) = mpsc::channel::<Request>(256);
        let (events, _) = broadcast::channel(4096);
        let (stop, mut shutdown) = watch::channel(false);
        let connected = Arc::new(AtomicBool::new(false));
        let timeouts = Arc::new(AtomicU32::new(0));
        let poison = Arc::new(tokio::sync::Notify::new());
        let broker = Self {
            requests: tx,
            events: events.clone(),
            connected: connected.clone(),
            stop,
            timeouts: timeouts.clone(),
            poison: poison.clone(),
        };
        tokio::spawn(async move {
            let Ok(client) = client() else { return };
            let mut attempt = 0;
            loop {
                if *shutdown.borrow() {
                    break;
                }
                let connection = async {
                    let base = match &base {
                        Some(b) => endpoint(
                            b.host_str().unwrap_or(""),
                            b.port_or_known_default().unwrap_or(0),
                        )?,
                        None => tokio::task::spawn_blocking(discover).await??,
                    };
                    let body =
                        bounded(client.get(base.clone()).send().await?, BOOTSTRAP_MAX).await?;
                    let (uri, token) = bootstrap_url(&base, &body)?;
                    // client_async performs ONE handshake on our direct TCP stream: no
                    // redirects and no environment proxy handling, including ws redirects.
                    let stream = tokio::net::TcpStream::connect((
                        base.host_str().ok_or("host")?.trim_matches(['[', ']']),
                        base.port_or_known_default().ok_or("port")?,
                    ))
                    .await?;
                    let config = WebSocketConfig::default()
                        .max_message_size(Some(MESSAGE_MAX))
                        .max_frame_size(Some(MESSAGE_MAX));
                    let (ws, _) = tokio_tungstenite::client_async_with_config(
                        uri.as_str(),
                        stream,
                        Some(config),
                    )
                    .await?;
                    Ok::<_, Box<dyn std::error::Error + Send + Sync>>((ws, base, token))
                };
                let result = tokio::select! { _=shutdown.changed()=>break, r=tokio::time::timeout(Duration::from_secs(5),connection)=>r };
                if let Ok(Ok((mut ws, base, token))) = result {
                    attempt = 0;
                    connected.store(true, Ordering::Release);
                    let mut pending = Pending::new();
                    loop {
                        tokio::select! {
                            _=shutdown.changed()=>break,
                            _=poison.notified()=>{eprintln!("broker: reconnecting Desktop RPC after repeated timeouts");break},
                            req=rx.recv()=>{let Some(req)=req else {break}; if req.reply.is_closed(){continue;}
                                if req.method=="session.messages" {let client=client.clone();let base=base.clone();let token=token.clone();tokio::spawn(async move {let value=persisted(&client,&base,&token,&req.params).await.unwrap_or_else(|_|failure("Persisted session transcript unavailable"));let _=req.reply.send(value);});continue;}
                                let body=json!({"jsonrpc":"2.0","id":req.id,"method":req.method,"params":req.params}).to_string();
                                if body.len()>MESSAGE_MAX {let _=req.reply.send(failure("Desktop RPC request too large"));continue;}
                                pending.retain(|_,r|!r.is_closed()); pending.insert(req.id,req.reply);
                                if ws.send(Message::Text(body.into())).await.is_err(){break;}
                            },
                            message=ws.next()=>match message {
                                Some(Ok(Message::Text(body)))=>for line in body.lines(){if let Ok(v)=serde_json::from_str(line){dispatch(v,&mut pending,&events);}},
                                Some(Ok(Message::Ping(data)))=>{if ws.send(Message::Pong(data)).await.is_err(){break;}},
                                Some(Ok(Message::Close(_)))|Some(Err(_))|None=>break,
                                _=>()
                            }
                        }
                    }
                    connected.store(false, Ordering::Release);
                    for (_, reply) in pending {
                        let _ = reply.send(failure("Desktop RPC connection lost; outcome unknown"));
                    }
                }
                let delay = tokio::time::sleep(reconnect_backoff(attempt));
                tokio::pin!(delay);
                attempt = attempt.saturating_add(1);
                loop {
                    tokio::select! {_=shutdown.changed()=>return,_=&mut delay=>break,req=rx.recv()=>{if let Some(req)=req{let _=req.reply.send(failure("Hermes Desktop broker unavailable"));}else{return;}}}
                }
            }
            connected.store(false, Ordering::Release);
        });
        broker
    }
    pub fn available(&self) -> bool {
        self.connected.load(Ordering::Acquire)
    }
    pub fn add_listener(&self) -> broadcast::Receiver<Value> {
        self.events.subscribe()
    }
    pub fn close(&self) {
        let _ = self.stop.send(true);
    }
    pub async fn call(&self, method: &str, params: Value, seconds: u64) -> Value {
        let (reply, receive) = oneshot::channel();
        let request = Request {
            id: ident("rpc"),
            method: method.into(),
            params,
            reply,
        };
        match tokio::time::timeout(Duration::from_secs(seconds), async {
            self.requests.send(request).await.map_err(|_| ())?;
            receive.await.map_err(|_| ())
        })
        .await
        {
            Ok(Ok(v)) => {
                self.timeouts.store(0, Ordering::Release);
                v
            }
            Ok(Err(_)) => failure("Hermes Desktop broker unavailable"),
            Err(_) => {
                // A dead WS transport reconnects on its own; a zombie connection where the
                // Desktop stops answering RPCs needs us to drop it explicitly.
                if self.timeouts.fetch_add(1, Ordering::AcqRel) + 1 >= 3 {
                    self.poison.notify_one();
                }
                failure("Hermes Desktop RPC timed out; outcome unknown")
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recognizes_hermes_bootstrap_serve_without_accepting_unrelated_python() {
        let command = "import hermes_bootstrap\nfrom hermes_cli.main import main\nsys.exit(main())";
        assert!(is_hermes_serve(&[
            "/usr/bin/python",
            "-m",
            "hermes_cli.main",
            "serve"
        ]));
        assert!(is_hermes_serve(&[
            "/usr/bin/python",
            "-I",
            "-c",
            command,
            "serve",
            "--port",
            "0"
        ]));
        assert!(!is_hermes_serve(&[
            "/usr/bin/python",
            "-I",
            "-c",
            "print('hermes_bootstrap')",
            "serve"
        ]));
        assert!(!is_hermes_serve(&[
            "/usr/bin/python",
            "-I",
            "-c",
            command,
            "chat"
        ]));
    }
    #[test]
    fn token_fixture() {
        let (url, token) = bootstrap_url(
            &Url::parse("http://127.0.0.1:1234/").unwrap(),
            include_bytes!("../tests/fixtures/desktop.html"),
        )
        .unwrap();
        assert_eq!(token, "a+ /\"b");
        assert_eq!(url.query_pairs().next().unwrap().1, token);
        assert_eq!(url.path(), "/api/ws");
        assert!(bootstrap_url(&url, b"missing").is_err());
    }
    #[tokio::test]
    async fn id_matching() {
        let (events, _) = broadcast::channel(2);
        let mut p = Pending::new();
        let (a, ar) = oneshot::channel();
        let (b, br) = oneshot::channel();
        p.insert("a".into(), a);
        p.insert("b".into(), b);
        dispatch(
            json!({"jsonrpc":"2.0","id":"b","result":{"n":2}}),
            &mut p,
            &events,
        );
        assert_eq!(br.await.unwrap()["result"]["n"], 2);
        assert!(p.contains_key("a"));
        dispatch(
            json!({"jsonrpc":"2.0","id":"a","error":{"code":4007,"message":"secret"}}),
            &mut p,
            &events,
        );
        let v = ar.await.unwrap();
        assert_eq!(v["code"], 4007);
        assert!(!v.to_string().contains("secret"));
    }
    #[test]
    fn backoff() {
        assert_eq!(reconnect_backoff(0).as_millis(), 250);
        assert_eq!(reconnect_backoff(1).as_millis(), 500);
        assert_eq!(reconnect_backoff(100).as_secs(), 5);
    }
    #[tokio::test]
    async fn refuses_redirects() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            for _ in 0..2 {
                let (mut s, _) = listener.accept().await.unwrap();
                let mut b = [0; 4096];
                let _ = s.read(&mut b).await;
                s.write_all(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/stolen\r\nContent-Length: 0\r\n\r\n").await.unwrap();
            }
        });
        let response = client()
            .unwrap()
            .get(format!("http://{address}/"))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 302);
        let stream = tokio::net::TcpStream::connect(address).await.unwrap();
        let result =
            tokio_tungstenite::client_async(format!("ws://{address}/api/ws?token=fixture"), stream)
                .await;
        assert!(
            matches!(result,Err(tokio_tungstenite::tungstenite::Error::Http(r)) if r.status()==302)
        );
        server.await.unwrap();
    }
}
