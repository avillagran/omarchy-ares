use ares_server::peer;
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    os::unix::{fs::PermissionsExt, net::UnixListener},
    time::{Duration, Instant},
};
use tempfile::TempDir;

fn serve<F: FnOnce(std::os::unix::net::UnixStream) + Send + 'static>(
    responder: F,
) -> (TempDir, std::path::PathBuf, std::thread::JoinHandle<Value>) {
    let dir = TempDir::new().unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let worker = std::thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        let mut line = String::new();
        BufReader::new(stream.try_clone().unwrap())
            .read_line(&mut line)
            .unwrap();
        responder(stream);
        serde_json::from_str(&line).unwrap()
    });
    (dir, path, worker)
}

fn request() -> Value {
    json!({"op":"rpc","peer":"trusted","request":{"op":"history","session_id":"abc"}})
}

#[test]
fn strict_history_accepts_one_complete_frame_followed_by_eof() {
    let (_dir, path, worker) = serve(|mut stream| {
        stream
            .write_all(b"{\"ok\":true,\"result\":{\"messages\":[]}}\n")
            .unwrap();
    });
    assert_eq!(
        peer::local_control_readonly_strict_at(&path, &request(), Duration::from_secs(1)).unwrap(),
        json!({"messages":[]})
    );
    assert_eq!(worker.join().unwrap(), request());
}

#[test]
fn strict_history_accepts_exactly_16384_bytes_including_newline() {
    let frame_base = serde_json::to_vec(&json!({"ok":true,"result":{"padding":""}})).unwrap();
    let padding = "x".repeat(16_384 - frame_base.len() - 1);
    let (_dir, path, worker) = serve(move |mut stream| {
        let frame = format!("{{\"ok\":true,\"result\":{{\"padding\":\"{padding}\"}}}}\n");
        assert_eq!(frame.len(), 16_384);
        stream.write_all(frame.as_bytes()).unwrap();
    });
    let result =
        peer::local_control_readonly_strict_at(&path, &request(), Duration::from_secs(1)).unwrap();
    assert_eq!(result["padding"].as_str().unwrap().len(), padding_len());
    worker.join().unwrap();
}

fn padding_len() -> usize {
    16_384
        - serde_json::to_vec(&json!({"ok":true,"result":{"padding":""}}))
            .unwrap()
            .len()
        - 1
}

#[test]
fn strict_history_rejects_junk_delivered_after_newline() {
    let (_dir, path, worker) = serve(|mut stream| {
        stream.write_all(b"{\"ok\":true,\"result\":{}}\n").unwrap();
        std::thread::sleep(Duration::from_millis(20));
        stream.write_all(b"second frame\n").unwrap();
    });
    assert!(
        peer::local_control_readonly_strict_at(&path, &request(), Duration::from_secs(1)).is_err()
    );
    worker.join().unwrap();
}

#[test]
fn strict_history_caps_total_envelope_including_newline() {
    let (_dir, path, worker) = serve(|mut stream| {
        let frame = format!(
            "{{\"ok\":true,\"result\":{{\"padding\":\"{}\"}}}}\n",
            "x".repeat(16_384)
        );
        let _ = stream.write_all(frame.as_bytes());
    });
    assert!(
        peer::local_control_readonly_strict_at(&path, &request(), Duration::from_secs(1)).is_err()
    );
    worker.join().unwrap();
}

#[test]
fn strict_history_rejects_partial_frame() {
    let (_dir, path, worker) = serve(|mut stream| {
        stream.write_all(b"{\"ok\":true").unwrap();
    });
    assert!(
        peer::local_control_readonly_strict_at(&path, &request(), Duration::from_secs(1)).is_err()
    );
    worker.join().unwrap();
}

#[test]
fn strict_history_requires_eof_after_complete_frame() {
    let (_dir, path, worker) = serve(|mut stream| {
        stream.write_all(b"{\"ok\":true,\"result\":{}}\n").unwrap();
        std::thread::sleep(Duration::from_millis(150));
    });
    let start = Instant::now();
    assert!(
        peer::local_control_readonly_strict_at(&path, &request(), Duration::from_millis(40))
            .is_err()
    );
    assert!(start.elapsed() < Duration::from_millis(130));
    worker.join().unwrap();
}

#[test]
fn strict_history_rejects_malformed_json() {
    let (_dir, path, worker) = serve(|mut stream| {
        stream.write_all(b"{\"ok\":true,\"result\":\n").unwrap();
    });
    assert!(
        peer::local_control_readonly_strict_at(&path, &request(), Duration::from_secs(1)).is_err()
    );
    worker.join().unwrap();
}

#[test]
fn strict_history_rejects_unsafe_socket_permissions() {
    let (_dir, path, worker) = serve(|mut stream| {
        let _ = stream.write_all(b"{\"ok\":true,\"result\":{}}\n");
    });
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o666)).unwrap();
    assert!(
        peer::local_control_readonly_strict_at(&path, &request(), Duration::from_millis(40))
            .is_err()
    );
    // No client should have connected; dropping a UnixListener in the worker requires a connection.
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut dummy = std::os::unix::net::UnixStream::connect(&path).unwrap();
    dummy.write_all(b"{}\n").unwrap();
    worker.join().unwrap();
}
