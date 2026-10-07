//! Explicit owner-only smoke test. Never print or persist source messages.
use ares_server::{
    digest, http, native_groups::NativeSnapshot, profile_yaml, store::Store, text, Result,
};
use axum::{
    extract::{ConnectInfo, Request},
    middleware::{from_fn, Next},
    response::Response,
};
use serde_json::Value;
use std::{
    net::SocketAddr,
    sync::{Arc, Mutex},
};

async fn pretend_remote(mut request: Request, next: Next) -> Response {
    request
        .extensions_mut()
        .insert(ConnectInfo(SocketAddr::from(([192, 0, 2, 1], 40000))));
    next.run(request).await
}

#[tokio::test]
#[ignore = "requires ARES_TEST_REAL_GROUP_HISTORY=1 and the owner-private profile"]
async fn owner_profile_history_through_disposable_loopback_router() -> Result<()> {
    if std::env::var("ARES_TEST_REAL_GROUP_HISTORY").as_deref() != Ok("1") {
        return Err("Explicit live smoke opt-in required".into());
    }
    // This test must not silently select another profile via a caller's environment.
    if std::env::var_os("HERMES_HOME").is_some()
        || std::env::var_os("ARES_LIVE_SMOKE").is_none()
    {
        return Err("Unexpected owner profile location".into());
    }
    let profiles =
        profile_yaml::load_default_profiles().map_err(|_| "Owner profile unavailable")?;
    let snapshot = NativeSnapshot::from_profiles("local", &profiles)
        .map_err(|_| "Owner group metadata unavailable")?;
    let source_rooms = profiles[0]["ui_meta"]["hermes-bots-groups"]["rooms"]
        .as_object()
        .ok_or("Owner group metadata unavailable")?;
    if source_rooms.is_empty() {
        return Err("No owner groups to exercise".into());
    }

    // The sole Ares Store is created under a fresh private temporary directory.
    // No live Ares path, Herdr socket, Desktop broker, or settings path is opened.
    let fixture = tempfile::tempdir()?;
    let store = Store::open(
        &fixture.path().join("ares.sqlite"),
        &fixture.path().join("artifacts"),
        fixture.path().join("disposable-hermes-home"),
    )?;
    store.sync_native_groups(&snapshot)?;
    let groups = store.rows("SELECT room_id,desktop_key FROM native_groups WHERE authority_host='local' AND room_id IS NOT NULL", [])?;
    assert_eq!(
        groups.len(),
        source_rooms.len(),
        "Metadata projection must be complete"
    );
    let store = Arc::new(Mutex::new(store));
    let bearer = "disposable-group-history-smoke-bearer";
    let app = http::integrated_router(http::App {
        store: store.clone(),
        token_hash: digest(bearer),
        web_root: fixture.path().join("empty-web-root"),
        bridge: None,
        mirror: None,
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    assert!(address.ip().is_loopback());
    let server = tokio::spawn(async move {
        let _ = axum::serve(
            listener,
            app.into_make_service_with_connect_info::<SocketAddr>(),
        )
        .await;
    });
    let client = reqwest::Client::builder().no_proxy().build()?;
    let mut total = 0usize;
    for group in &groups {
        let room_id = text(group, "room_id");
        let source = source_rooms
            .get(text(group, "desktop_key"))
            .ok_or("Missing owner group")?;
        let log = source["log"]
            .as_array()
            .ok_or("Owner group log unavailable")?;
        let endpoint = format!("http://{address}/v1/groups/{room_id}/history");
        for denied in [
            client.get(&endpoint),
            client.get(&endpoint).bearer_auth("revoked-test-bearer"),
        ] {
            let response = denied.send().await.map_err(|_| "Denied request failed")?;
            assert_eq!(
                response.status(),
                401,
                "Missing or revoked bearer must fail"
            );
            let body: Value = response
                .json()
                .await
                .map_err(|_| "Invalid denial response")?;
            assert_eq!(body["ok"], false);
            assert!(body.get("messages").is_none());
        }
        // This server also listens ONLY on loopback: inject a non-loopback
        // ConnectInfo in a separate request path to exercise the route guard.
        let remote_app = http::integrated_router(http::App {
            store: store.clone(),
            token_hash: digest(bearer),
            web_root: fixture.path().join("empty-web-root"),
            bridge: None,
            mirror: None,
        })
        .layer(from_fn(pretend_remote));
        let remote_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let remote_address = remote_listener.local_addr()?;
        assert!(remote_address.ip().is_loopback());
        let remote_server = tokio::spawn(async move {
            let _ = axum::serve(
                remote_listener,
                remote_app.into_make_service_with_connect_info::<SocketAddr>(),
            )
            .await;
        });
        let denied = client
            .get(format!(
                "http://{remote_address}/v1/groups/{room_id}/history"
            ))
            .bearer_auth(bearer)
            .send()
            .await
            .map_err(|_| "Remote denial request failed")?;
        assert_eq!(denied.status(), 403, "Nonloopback identity must fail");
        let body: Value = denied
            .json()
            .await
            .map_err(|_| "Invalid remote denial response")?;
        assert_eq!(body["ok"], false);
        assert!(body.get("messages").is_none());
        remote_server.abort();

        let response = client
            .get(&endpoint)
            .bearer_auth(bearer)
            .send()
            .await
            .map_err(|_| "Owner history request failed")?;
        assert_eq!(response.status(), 200, "Owner group history unavailable");
        assert_eq!(
            response
                .headers()
                .get("cache-control")
                .and_then(|v| v.to_str().ok()),
            Some("no-store")
        );
        let page: Value = response
            .json()
            .await
            .map_err(|_| "Invalid owner history response")?;
        assert_eq!(page["readOnly"], true);
        let messages = page["messages"].as_array().ok_or("Missing history page")?;
        assert!(!messages.is_empty() && messages.len() <= 200 && messages.len() <= log.len());
        let members = page["members"].as_array().ok_or("Missing group members")?;
        for message in messages {
            let role = message["role"].as_str().ok_or("Missing message role")?;
            let author = message["author"].as_str().ok_or("Missing message author")?;
            match role {
                "user" => assert_eq!(author, "user"),
                "assistant" => assert!(
                    members.iter().any(|m| m["profile"] == author),
                    "Author not in group"
                ),
                _ => return Err("Unexpected history role".into()),
            }
            assert!(message["text"]
                .as_str()
                .is_some_and(|body| body.len() <= 16384));
        }
        total += messages.len();
    }
    assert_eq!(
        store
            .lock()
            .unwrap()
            .rows("SELECT * FROM room_events", [])?
            .len(),
        0,
        "History must not enter Ares events"
    );
    server.abort();
    drop(store);
    drop(fixture);
    println!(
        "live group history smoke: groups={}, messages={total}",
        groups.len()
    );
    Ok(())
}
