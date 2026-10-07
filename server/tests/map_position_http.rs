use ares_server::{http, store::Store, text};
use serde_json::{json, Value};
use std::net::SocketAddr;
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

async fn call(addr: SocketAddr, method: &str, path: &str, body: Option<Value>) -> (u16, Value) {
    let mut stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    let payload = body.map(|v| v.to_string()).unwrap_or_default();
    let request = format!("{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-token\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}", payload.len());
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut response = Vec::new();
    stream.read_to_end(&mut response).await.unwrap();
    let text = String::from_utf8(response).unwrap();
    (text.split_whitespace().nth(1).unwrap().parse().unwrap(),
     serde_json::from_str(text.split("\r\n\r\n").nth(1).unwrap()).unwrap())
}

#[tokio::test]
async fn project_position_api_is_authenticated_and_persists_without_moving_rooms() {
    let dir = TempDir::new().unwrap();
    let store = Store::open(&dir.path().join("ares.db"), &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap();
    let project = store.create_project("A", dir.path().to_str().unwrap(), "local", None).unwrap();
    let pid = text(&project, "id").to_owned();
    let room = store.create_room(&pid, "Chat", &[json!("default")]).unwrap();
    let rid = text(&room, "id").to_owned();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = http::router(store, "test-token", dir.path().into());
    let task = tokio::spawn(async move { axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>()).await.unwrap(); });
    let path = format!("/v1/projects/{pid}/map-position");
    let (status, saved) = call(addr, "POST", &path, Some(json!({"x":640,"y":320}))).await;
    assert_eq!(status, 200);
    assert_eq!(saved["project"]["mapX"],640);
    assert_eq!(saved["project"]["mapY"],320);
    for invalid in [json!({"x":-1,"y":320}), json!({"x":9601,"y":320}),
                    json!({"x":3.5,"y":320}), json!({"x":null,"y":320})] {
        let (status, _) = call(addr,"POST",&path,Some(invalid)).await;
        assert_ne!(status,200);
    }
    task.abort();
    let store = Store::open(&dir.path().join("ares.db"), &dir.path().join("artifacts"), dir.path().join("hermes")).unwrap();
    let project = store.one("SELECT * FROM projects WHERE id=?",[&pid],"missing").unwrap();
    assert_eq!(project["map_x"],640);
    assert_eq!(project["map_y"],320);
    let room = store.room(&rid,false).unwrap();
    assert_eq!(room["project_id"],pid);
}
