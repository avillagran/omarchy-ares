use ares_server::peer_control::ControlClient;
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixListener},
    path::PathBuf,
    thread,
    time::Duration,
};
use tempfile::TempDir;

fn mock<F: FnOnce(std::os::unix::net::UnixStream) + Send + 'static>(
    f: F,
) -> (TempDir, PathBuf, thread::JoinHandle<()>) {
    let dir = TempDir::new().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let worker = thread::spawn(move || f(listener.accept().unwrap().0));
    (dir, path, worker)
}
fn read(s: &std::os::unix::net::UnixStream) -> Value {
    let mut line = String::new();
    BufReader::new(s.try_clone().unwrap())
        .read_line(&mut line)
        .unwrap();
    serde_json::from_str(&line).unwrap()
}
fn write(s: &mut std::os::unix::net::UnixStream, v: Value) {
    s.write_all(format!("{v}\n").as_bytes()).unwrap();
}
fn begin(path: &std::path::Path) -> ares_server::Result<ControlClient> {
    ControlClient::begin_at(
        path,
        "exact_alias",
        "default",
        "sess_1",
        "term_1",
        30,
        Duration::from_secs(2),
    )
}
#[test]
fn same_connection_receipt_and_ordered_send_release() {
    let dir = TempDir::new().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let worker = thread::spawn(move || {
        for seq in 0..=2 {
            let (mut s, _) = listener.accept().unwrap();
            let req = read(&s);
            assert_eq!(
                req["op"],
                ["control.begin", "control.send", "control.release"][seq]
            );
            assert_eq!(req["peer"], "exact_alias");
            assert_eq!(req["profile"], "default");
            assert_eq!(req["session_id"], "sess_1");
            assert_eq!(req["terminal_id"], "term_1");
            if seq == 0 {
                assert_eq!(req["ttl"], 30);
            } else {
                assert_eq!(
                    req["handle"],
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                );
                assert_eq!(req["sequence"], seq);
                if seq == 1 {
                    assert_eq!(req["text"], "hello");
                } else {
                    assert!(req.get("text").is_none());
                }
            }
            let result = if seq == 0 {
                json!({"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0})
            } else {
                json!({"sequence":seq})
            };
            write(&mut s, json!({"ok":true,"result":result}));
            if seq < 2 {
                assert_eq!(
                    read(&s),
                    json!({"op":"control.ack","handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":seq})
                );
                write(&mut s, json!({"ok":true,"result":{"ack":true}}));
            }
        }
    });
    let mut client = begin(&path).unwrap();
    client.send("hello").unwrap();
    client.release().unwrap();
    assert!(client.send("again").is_err());
    worker.join().unwrap();
}
#[test]
fn invalid_receipt_identity_never_acknowledged() {
    for response in [
        json!({"ok":true,"result":{"handle":"","sequence":0}}),
        json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":1}}),
        json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":false}}),
        json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0},"extra":1}),
    ] {
        let (_dir, path, worker) = mock(move |mut s| {
            let _ = read(&s);
            write(&mut s, response);
            s.set_read_timeout(Some(Duration::from_millis(200)))
                .unwrap();
            let mut b = [0u8; 1];
            assert_eq!(s.read(&mut b).unwrap(), 0);
        });
        assert!(begin(&path).is_err());
        worker.join().unwrap();
    }
}
#[test]
fn lost_final_ack_is_ambiguous_and_never_retried() {
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        write(
            &mut s,
            json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}),
        );
        assert_eq!(read(&s)["op"], "control.ack");
    });
    let err = begin(&path).err().unwrap().to_string();
    assert!(err.contains("ambiguous"));
    worker.join().unwrap();
}
#[test]
fn lost_send_final_receipt_never_replays_private_input() {
    let dir = TempDir::new().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let worker = thread::spawn(move || {
        let (mut s, _) = listener.accept().unwrap();
        let _ = read(&s);
        write(
            &mut s,
            json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}),
        );
        assert_eq!(read(&s)["op"], "control.ack");
        write(&mut s, json!({"ok":true,"result":{"ack":true}}));
        let (mut s, _) = listener.accept().unwrap();
        assert_eq!(read(&s)["text"], "once-only");
        write(&mut s, json!({"ok":true,"result":{"sequence":1}}));
        assert_eq!(
            read(&s),
            json!({"op":"control.ack","handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":1})
        );
        drop(s);
        listener.set_nonblocking(true).unwrap();
        // A poisoned client must not create another connection to repeat the input.
        thread::sleep(Duration::from_millis(20));
        assert!(listener.accept().is_err());
    });
    let mut c = begin(&path).unwrap();
    let error = c.send("once-only").err().unwrap().to_string();
    assert!(error.contains("ambiguous"));
    assert!(!error.contains("once-only"));
    assert!(c.send("once-only").is_err());
    assert!(c.release().is_err());
    worker.join().unwrap();
}
#[test]
fn mismatched_send_sequence_never_acknowledges_and_poisoned_client_cannot_retry() {
    let dir = TempDir::new().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let worker = thread::spawn(move || {
        let (mut s, _) = listener.accept().unwrap();
        let _ = read(&s);
        write(
            &mut s,
            json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}),
        );
        assert_eq!(read(&s)["op"], "control.ack");
        write(&mut s, json!({"ok":true,"result":{"ack":true}}));
        let (mut s, _) = listener.accept().unwrap();
        assert_eq!(read(&s)["text"], "private-input");
        write(&mut s, json!({"ok":true,"result":{"sequence":2}}));
        s.set_read_timeout(Some(Duration::from_millis(200)))
            .unwrap();
        let mut b = [0u8; 1];
        assert_eq!(s.read(&mut b).unwrap(), 0);
        listener.set_nonblocking(true).unwrap();
        assert!(listener.accept().is_err());
    });
    let mut c = begin(&path).unwrap();
    let e = c.send("private-input").err().unwrap();
    assert!(!e.to_string().contains("private-input"));
    assert!(c.send("private-input").is_err());
    assert!(c.release().is_err());
    worker.join().unwrap();
}
#[test]
fn oversized_or_trailing_frames_and_unsafe_socket_refused() {
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        let _ = s.write_all(&vec![b'x'; 16385]);
    });
    assert!(begin(&path).is_err());
    worker.join().unwrap();
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        let _ = s.write_all(b"{\"ok\":true}\n{}\n");
    });
    assert!(begin(&path).is_err());
    worker.join().unwrap();
    let (_dir, path, worker) = mock(|_s| {});
    fs::set_permissions(&path, fs::Permissions::from_mode(0o666)).unwrap();
    assert!(begin(&path).is_err());
    drop(path);
    drop(worker);
}
#[test]
fn rejects_invalid_identity_and_multiline_or_oversized_text_before_connect() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("none");
    assert!(ControlClient::begin_at(
        &path,
        "alias;bad",
        "default",
        "s",
        "t",
        30,
        Duration::from_secs(1)
    )
    .is_err());
    assert!(ControlClient::begin_at(
        &path,
        "alias",
        "default",
        "s",
        "t",
        0,
        Duration::from_secs(1)
    )
    .is_err());
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        write(
            &mut s,
            json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}),
        );
        let _ = read(&s);
        write(&mut s, json!({"ok":true,"result":{"ack":true}}));
    });
    let mut c = begin(&path).unwrap();
    worker.join().unwrap();
    assert!(c.send("line\nbreak").is_err());
    assert!(c.send(&"x".repeat(2049)).is_err());
}

#[test]
fn exact_16384_byte_response_frame_is_accepted() {
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        let base = json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}).to_string();
        let padding = " ".repeat(16_384 - base.len() - 1);
        let frame = format!("{base}{padding}\n");
        assert_eq!(frame.len(), 16_384);
        s.write_all(frame.as_bytes()).unwrap();
        assert_eq!(read(&s)["op"], "control.ack");
        write(&mut s, json!({"ok":true,"result":{"ack":true}}));
    });
    begin(&path).unwrap();
    worker.join().unwrap();
}

#[test]
fn response_frame_of_16385_bytes_is_rejected_without_ack() {
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        let base = json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}).to_string();
        let frame = format!("{base}{}\n", " ".repeat(16_385 - base.len() - 1));
        assert_eq!(frame.len(), 16_385);
        s.write_all(frame.as_bytes()).unwrap();
        s.set_read_timeout(Some(Duration::from_millis(200)))
            .unwrap();
        let mut b = [0];
        assert_eq!(s.read(&mut b).unwrap(), 0);
    });
    assert!(begin(&path).is_err());
    worker.join().unwrap();
}

#[test]
fn multibyte_input_accepts_exact_2048_bytes() {
    let dir = TempDir::new().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let worker = thread::spawn(move || {
        let (mut s, _) = listener.accept().unwrap();
        let _ = read(&s);
        write(
            &mut s,
            json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}),
        );
        assert_eq!(read(&s)["op"], "control.ack");
        write(&mut s, json!({"ok":true,"result":{"ack":true}}));
        let (mut s, _) = listener.accept().unwrap();
        let request = read(&s);
        assert_eq!(request["text"].as_str().unwrap().as_bytes().len(), 2048);
        write(&mut s, json!({"ok":true,"result":{"sequence":1}}));
        assert_eq!(read(&s)["sequence"], 1);
        write(&mut s, json!({"ok":true,"result":{"ack":true}}));
    });
    let mut client = begin(&path).unwrap();
    client.send(&"é".repeat(1024)).unwrap();
    worker.join().unwrap();
}

#[test]
fn multibyte_input_rejects_2049_bytes_before_connection() {
    let dir = TempDir::new().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let path = dir.path().join("control.sock");
    let listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let worker = thread::spawn(move || {
        let (mut s, _) = listener.accept().unwrap();
        let _ = read(&s);
        write(
            &mut s,
            json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}),
        );
        assert_eq!(read(&s)["op"], "control.ack");
        write(&mut s, json!({"ok":true,"result":{"ack":true}}));
        listener.set_nonblocking(true).unwrap();
        listener
    });
    let mut client = begin(&path).unwrap();
    let listener = worker.join().unwrap();
    let input = format!("{}x", "é".repeat(1024));
    assert_eq!(input.len(), 2049);
    assert!(client.send(&input).is_err());
    assert!(
        listener.accept().is_err(),
        "oversized input opened a socket"
    );
}

#[test]
fn partial_response_times_out_without_ack() {
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        s.write_all(b"{\"ok\":true").unwrap();
        thread::sleep(Duration::from_millis(150));
        s.set_nonblocking(true).unwrap();
        let mut b = [0];
        assert!(matches!(s.read(&mut b), Ok(0) | Err(_)));
    });
    let error = ControlClient::begin_at(
        &path,
        "exact_alias",
        "default",
        "sess_1",
        "term_1",
        30,
        Duration::from_millis(40),
    )
    .err()
    .unwrap()
    .to_string();
    assert!(
        error.contains("timed out") || error.contains("unavailable"),
        "{error}"
    );
    assert!(!error.contains("sess_1"));
    worker.join().unwrap();
}

#[test]
fn final_ack_timeout_is_ambiguous() {
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        write(
            &mut s,
            json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}),
        );
        assert_eq!(read(&s)["op"], "control.ack");
        thread::sleep(Duration::from_millis(150));
    });
    let error = ControlClient::begin_at(
        &path,
        "exact_alias",
        "default",
        "sess_1",
        "term_1",
        30,
        Duration::from_millis(40),
    )
    .err()
    .unwrap()
    .to_string();
    assert!(error.contains("ambiguous"), "{error}");
    worker.join().unwrap();
}

#[test]
fn malformed_final_ack_is_ambiguous() {
    let (_dir, path, worker) = mock(|mut s| {
        let _ = read(&s);
        write(
            &mut s,
            json!({"ok":true,"result":{"handle":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sequence":0}}),
        );
        assert_eq!(read(&s)["op"], "control.ack");
        s.write_all(b"{bad-json}\n").unwrap();
    });
    let error = begin(&path).err().unwrap().to_string();
    assert!(error.contains("ambiguous"), "{error}");
    worker.join().unwrap();
}

#[test]
fn private_directory_required_before_connect() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("control.sock");
    let _listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o750)).unwrap();
    assert!(begin(&path).err().unwrap().to_string().contains("Unsafe"));
}

#[test]
fn symlinked_control_socket_refused_before_connect() {
    let dir = TempDir::new().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let real = dir.path().join("real.sock");
    let _listener = UnixListener::bind(&real).unwrap();
    fs::set_permissions(&real, fs::Permissions::from_mode(0o600)).unwrap();
    let link = dir.path().join("control.sock");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    assert!(begin(&link).err().unwrap().to_string().contains("Unsafe"));
}

#[test]
fn symlinked_private_directory_refused_before_connect() {
    let root = TempDir::new().unwrap();
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let real = root.path().join("real");
    fs::create_dir(&real).unwrap();
    fs::set_permissions(&real, fs::Permissions::from_mode(0o700)).unwrap();
    let path = real.join("control.sock");
    let _listener = UnixListener::bind(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let link = root.path().join("linked");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    assert!(begin(&link.join("control.sock"))
        .err()
        .unwrap()
        .to_string()
        .contains("Unsafe"));
}
