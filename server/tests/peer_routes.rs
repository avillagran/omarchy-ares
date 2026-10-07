use ares_server::{http, store::Store};
use serde_json::{json, Value};
use std::{io::{BufRead, BufReader, Write}, net::SocketAddr, os::unix::{fs::PermissionsExt, net::UnixListener}, path::Path};
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn store(dir: &Path) -> Store {
    Store::open(&dir.join("ares.db"), &dir.join("artifacts"), dir.join("hermes")).unwrap()
}
async fn call(addr: SocketAddr, method: &str, path: &str, body: Option<Value>, token: bool) -> (u16, Value) {
    let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    let payload = body.map(|v| v.to_string()).unwrap_or_default();
    let auth = if token { "Authorization: Bearer test-token\r\n" } else { "" };
    let request = format!("{method} {path} HTTP/1.1\r\nHost: localhost\r\n{auth}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",payload.len());
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut reply = Vec::new();
    stream.read_to_end(&mut reply).await.unwrap();
    let text = String::from_utf8(reply).unwrap();
    let status = text.split_whitespace().nth(1).unwrap().parse().unwrap();
    let value = serde_json::from_str(text.split("\r\n\r\n").nth(1).unwrap()).unwrap();
    (status,value)
}
async fn server(dir: &TempDir, bind: &str) -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind(bind).await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = http::router(store(dir.path()), "test-token", dir.path().into());
    let task = tokio::spawn(async move { axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>()).await.unwrap(); });
    (addr,task)
}
async fn server_without_connect_info(dir: &TempDir) -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = http::router(store(dir.path()), "test-token", dir.path().into());
    let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap(); });
    (addr,task)
}

fn mock(home: &Path, replies: Vec<Value>) -> std::thread::JoinHandle<Vec<Value>> {
    let dir = home.join(".local/share/ares-peer");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.join("control.sock");
    let listener = UnixListener::bind(path).unwrap();
    std::fs::set_permissions(dir.join("control.sock"), std::fs::Permissions::from_mode(0o600)).unwrap();
    std::thread::spawn(move || replies.into_iter().map(|reply| {
        let (mut stream, _) = listener.accept().unwrap();
        let mut line = String::new();
        BufReader::new(stream.try_clone().unwrap()).read_line(&mut line).unwrap();
        stream.write_all(format!("{}\n",json!({"ok":true,"result":reply})).as_bytes()).unwrap();
        serde_json::from_str(&line).unwrap()
    }).collect())
}

#[tokio::test]
async fn pair_requires_auth_local_origin_and_valid_secret() {
    let dir = TempDir::new().unwrap();
    let (local, task) = server(&dir,"127.0.0.1:0").await;
    let pair = json!({"peerId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","alias":"node","fingerprint":"a".repeat(64),"code":"12345678"});
    assert_eq!(call(local,"POST","/v1/peers/pair",Some(pair.clone()),false).await.0,401);
    let (status, body) = call(local,"POST","/v1/peers/pair",Some(json!({"peerId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","alias":"node","fingerprint":"a".repeat(64),"code":"leak-me; bad"})),true).await;
    assert_eq!(status,422);
    assert!(!body.to_string().contains("leak-me"));
    assert_eq!(call(local,"POST","/v1/peers/pair",Some(json!({"peerId":"a".repeat(32),"alias":"node","fingerprint":"a".repeat(64),"code":"123456"})),true).await.0,422);
    assert_eq!(call(local,"POST","/v1/peers/node/revoke",Some(json!({"confirmed":false})),true).await.0,422);
    assert_eq!(call(local,"GET","/v1/peers/node/sessions?code=secret",None,true).await.0,400);
    assert_eq!(call(local,"GET","/v1/health",None,false).await.0,200);
    let (missing, no_info) = server_without_connect_info(&dir).await;
    assert_eq!(call(missing,"POST","/v1/peers/pair",Some(pair.clone()),true).await.0,403);
    assert_eq!(call(missing,"POST","/v1/peers/node/revoke",Some(json!({"confirmed":true})),true).await.0,403);
    no_info.abort();
    let probe = std::net::UdpSocket::bind("0.0.0.0:0").unwrap();
    if probe.connect("192.0.2.1:9").is_ok() {
        let remote_ip = probe.local_addr().unwrap().ip();
        if !remote_ip.is_loopback() {
            let (remote, other) = server(&dir,"0.0.0.0:0").await;
            let remote = SocketAddr::new(remote_ip,remote.port());
            assert_eq!(call(remote,"POST","/v1/peers/pair",Some(pair),true).await.0,403);
            other.abort();
        }
    }
    task.abort();
}

#[tokio::test]
async fn discovery_pair_revoke_and_sessions_use_private_peer_socket() {
    let dir = TempDir::new().unwrap();
    std::env::set_var("HOME",dir.path());
    let expected = vec![json!({"peers":[{"peer_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"Node","fingerprint":"a".repeat(64),"address":"192.0.2.1","port":9421,"paired":false,"online":true}]}),json!({"peer":"node","peer_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","token":"must-not-echo"}),json!({"peers":[{"peer_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","alias":"node","paired":true,"online":true}]}),json!({"data":[{"id":"s1","token":"must-not-echo"}],"limit":100,"offset":0,"has_more":false}),json!({"revoked":"node","token":"must-not-echo"}),json!({"peers":[{"peer_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","alias":"","paired":false,"online":true}]}),json!({"peers":vec![json!({"peer_id":"x","paired":false});101]}),json!({"data":vec![json!({"id":"s","token":"must-not-echo"});101]})];
    let worker = mock(dir.path(),expected);
    let (addr, task) = server(&dir,"127.0.0.1:0").await;
    let (status, discovered) = call(addr,"GET","/v1/peers",None,true).await;
    assert_eq!(status,200);
    assert_eq!(discovered["peers"][0]["peerId"],"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    assert_eq!(discovered["peers"][0]["id"],"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    assert_eq!(discovered["peers"][0]["online"],true);
    assert_eq!(discovered["peers"][0]["trusted"],false);
    let pair = json!({"peerId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","alias":"node","fingerprint":"a".repeat(64),"code":"12345678"});
    let (status, paired) = call(addr,"POST","/v1/peers/pair",Some(pair),true).await;
    assert_eq!(status,200);
    assert!(!paired.to_string().contains("12345678"));
    assert!(!paired.to_string().contains("must-not-echo"));
    let (status, sessions) = call(addr,"GET","/v1/peers/node/sessions",None,true).await;
    assert_eq!(status,200);
    assert_eq!(sessions["sessions"][0]["id"],"s1");
    assert!(!sessions.to_string().contains("must-not-echo"));
    let (status, revoked) = call(addr,"POST","/v1/peers/node/revoke",Some(json!({"confirmed":true})),true).await;
    assert_eq!(status,200);
    assert_eq!(revoked["revoked"],"node");
    assert!(!revoked.to_string().contains("must-not-echo"));
    assert_eq!(call(addr,"GET","/v1/peers",None,true).await.0,502);
    let (status, excessive) = call(addr,"GET","/v1/peers/node/sessions?limit=100",None,true).await;
    assert_eq!(status,502);
    assert!(!excessive.to_string().contains("must-not-echo"));
    let requests = worker.join().unwrap();
    assert_eq!(requests[0],json!({"op":"discover"}));
    assert_eq!(requests[1],json!({"op":"pair","peer":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","alias":"node","fingerprint":"a".repeat(64),"code":"12345678"}));
    assert_eq!(requests[2],json!({"op":"discover"}));
    assert_eq!(requests[3],json!({"op":"rpc","peer":"node","request":{"op":"sessions","profile":"default","limit":100,"offset":0}}));
    assert_eq!(requests[4],json!({"op":"revoke","peer":"node"}));
    assert_eq!(requests[5],json!({"op":"discover"}));
    assert_eq!(requests[6],json!({"op":"discover"}));
    assert_eq!(requests[7]["request"]["limit"],100);
    task.abort();
}
