use ares_server::{digest, http, native_groups::NativeSnapshot, store::Store, text, Result};
use serde_json::{json, Value};
use std::{
    fs,
    net::SocketAddr,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};
use tempfile::{tempdir_in, TempDir};

// HERMES_HOME is process-wide; HTTP requests run on separate Tokio workers.
static SOURCE_ENV: Mutex<()> = Mutex::new(());
struct Fixture {
    _env_guard: std::sync::MutexGuard<'static, ()>,
    previous_home: Option<std::ffi::OsString>,
    _dir: TempDir,
    home: PathBuf,
    store: Arc<Mutex<Store>>,
    room: String,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(previous) = self.previous_home.take() {
            std::env::set_var("HERMES_HOME", previous);
        } else {
            std::env::remove_var("HERMES_HOME");
        }
    }
}
fn snapshot(revision: i64, room_id: &str, count: usize) -> Value {
    json!({"ui_meta":{"hermes-bots-groups":{"version":3,"deleted":{},"rooms":{"id:abc":{"roomId":room_id,"revision":revision,"name":"Team","members":[{"name":"builder","connectionId":"local","connectionKind":"local"}],"log":(1..=count).map(|i| json!({"id":format!("m{i}"),"from":{"kind":"member","name":"builder","source":"local"},"text":format!("private-{i}"),"thread":"t","at":i})).collect::<Vec<_>>()}}}},"_ui_meta_revisions":{"hermes-bots-groups":revision}})
}
fn write_source(home: &Path, data: &Value) -> Result<()> {
    let file = home.join("profile.yaml");
    fs::write(&file, serde_yaml::to_string(data)?)?;
    fs::set_permissions(file, fs::Permissions::from_mode(0o600))?;
    Ok(())
}
fn fixture() -> Result<Fixture> {
    let base = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    let dir = tempdir_in(base)?;
    let home = dir.path().join("hermes");
    fs::create_dir(&home)?;
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700))?;
    let source = snapshot(4, "abc", 205);
    write_source(&home, &source)?;
    let store = Store::open(
        &dir.path().join("db"),
        &dir.path().join("artifacts"),
        dir.path().join("ares-home"),
    )?;
    let projected = vec![
        json!({"name":"default","ui_meta":source["ui_meta"],"ui_meta_revisions":source["_ui_meta_revisions"]}),
    ];
    store.sync_native_groups(&NativeSnapshot::from_profiles("local", &projected)?)?;
    let room = text(&store.snapshot(None, None)?["rooms"][0], "id").to_string();
    let env_guard = SOURCE_ENV.lock().unwrap();
    let previous_home = std::env::var_os("HERMES_HOME");
    std::env::set_var("HERMES_HOME", &home);
    Ok(Fixture {
        _env_guard: env_guard,
        previous_home,
        _dir: dir,
        home,
        store: Arc::new(Mutex::new(store)),
        room,
    })
}
async fn serve(f: &Fixture, connect_info: bool) -> Result<(String, tokio::task::JoinHandle<()>)> {
    serve_at(f, connect_info, "127.0.0.1:0").await
}
async fn serve_at(
    f: &Fixture,
    connect_info: bool,
    bind: &str,
) -> Result<(String, tokio::task::JoinHandle<()>)> {
    let listener = tokio::net::TcpListener::bind(bind).await?;
    let url = format!("http://{}", listener.local_addr()?);
    let app = http::integrated_router(http::App {
        store: f.store.clone(),
        token_hash: digest("test-token"),
        web_root: f.home.clone(),
        bridge: None,
        mirror: None,
    });
    let task = tokio::spawn(async move {
        if connect_info {
            axum::serve(
                listener,
                app.into_make_service_with_connect_info::<SocketAddr>(),
            )
            .await
            .unwrap();
        } else {
            axum::serve(listener, app).await.unwrap();
        }
    });
    Ok((url, task))
}
fn client() -> reqwest::Client {
    reqwest::Client::builder().no_proxy().build().unwrap()
}

#[tokio::test]
async fn owner_private_history_pages_without_desktop_and_is_never_cached() -> Result<()> {
    let f = fixture()?;
    let (url, task) = serve(&f, true).await?;
    let endpoint = format!("{url}/v1/groups/{}/history", f.room);
    let c = client();
    assert_eq!(c.get(&endpoint).send().await?.status(), 401);
    let first = c.get(&endpoint).bearer_auth("test-token").send().await?;
    assert_eq!(first.status(), 200);
    assert_eq!(first.headers()["cache-control"], "no-store");
    let first: Value = first.json().await?;
    assert_eq!(first["messages"].as_array().unwrap().len(), 200);
    assert_eq!(first["messages"][199]["text"], "private-205");
    let older: Value = c
        .get(format!("{endpoint}?before={}", first["oldestSeq"]))
        .bearer_auth("test-token")
        .send()
        .await?
        .json()
        .await?;
    assert_eq!(older["messages"].as_array().unwrap().len(), 5);
    assert_eq!(older["messages"][0]["seq"], 1);
    assert!(f
        .store
        .lock()
        .unwrap()
        .rows("SELECT * FROM room_events WHERE room_id=?", [&f.room])?
        .is_empty());
    let changed = snapshot(5, "abc", 205);
    write_source(&f.home, &changed)?;
    assert_eq!(
        c.get(format!("{endpoint}?before={}", first["oldestSeq"]))
            .bearer_auth("test-token")
            .send()
            .await?
            .status(),
        200
    );
    let mut changed = changed;
    changed["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["log"][0]["text"] =
        json!("changed-source");
    write_source(&f.home, &changed)?;
    assert_eq!(
        c.get(format!("{endpoint}?before={}", first["oldestSeq"]))
            .bearer_auth("test-token")
            .send()
            .await?
            .status(),
        503
    );
    let fresh: Value = c
        .get(&endpoint)
        .bearer_auth("test-token")
        .send()
        .await?
        .json()
        .await?;
    assert_eq!(fresh["messages"][0]["text"], "private-6");
    task.abort();
    Ok(())
}

#[tokio::test]
async fn unavailable_or_wrong_source_fails_closed_without_desktop() -> Result<()> {
    let f = fixture()?;
    let (url, task) = serve(&f, true).await?;
    let endpoint = format!("{url}/v1/groups/{}/history", f.room);
    let c = client();
    for mode in 0..5 {
        let mut v = snapshot(4, "abc", 1);
        match mode {
            0 => {
                fs::remove_file(f.home.join("profile.yaml"))?;
            }
            1 => {
                v["ui_meta"]["hermes-bots-groups"]["rooms"]["id:abc"]["roomId"] = json!("wrong");
                write_source(&f.home, &v)?;
            }
            2 => {
                v["ui_meta"]["hermes-bots-groups"]["deleted"] = json!({"id:abc":0});
                write_source(&f.home, &v)?;
            }
            3 => {
                v["_ui_meta_revisions"]["hermes-bots-groups"] = json!(3);
                write_source(&f.home, &v)?;
            }
            _ => {
                v["ui_meta"]["hermes-bots-groups"]["rooms"] = json!({});
                write_source(&f.home, &v)?;
            }
        }
        let r = c.get(&endpoint).bearer_auth("test-token").send().await?;
        assert_eq!(r.status(), 503, "mode {mode}");
        assert!(!r.text().await?.contains("private-"));
    }
    assert_eq!(
        c.post(&endpoint)
            .bearer_auth("test-token")
            .send()
            .await?
            .status(),
        400
    );
    task.abort();
    Ok(())
}

#[tokio::test]
async fn nonloopback_bearer_rejects_before_reading_source() -> Result<()> {
    let probe = std::net::UdpSocket::bind("0.0.0.0:0")?;
    if probe.connect("192.0.2.1:9").is_err() {
        return Ok(());
    }
    let ip = probe.local_addr()?.ip();
    if ip.is_loopback() {
        return Ok(());
    }
    let f = fixture()?;
    fs::remove_file(f.home.join("profile.yaml"))?;
    let (url, task) = serve_at(&f, true, "0.0.0.0:0").await?;
    let port = url.rsplit(':').next().unwrap();
    let r = client()
        .get(format!("http://{ip}:{port}/v1/groups/{}/history", f.room))
        .bearer_auth("test-token")
        .send()
        .await?;
    assert_eq!(r.status(), 403);
    task.abort();
    Ok(())
}

#[tokio::test]
async fn missing_connect_info_rejects_before_reading_source() -> Result<()> {
    let f = fixture()?;
    fs::remove_file(f.home.join("profile.yaml"))?;
    let (url, task) = serve(&f, false).await?;
    let r = client()
        .get(format!("{url}/v1/groups/{}/history", f.room))
        .bearer_auth("test-token")
        .send()
        .await?;
    assert_eq!(r.status(), 403);
    assert!(!r.text().await?.contains("profile"));
    task.abort();
    Ok(())
}
