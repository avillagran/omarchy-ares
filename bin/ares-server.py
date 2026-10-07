#!/usr/bin/env python3
"""Start, inspect, or stop the persistent Omarchy Ares service."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ares_service import (
    ARTIFACT_ROOT, DB_PATH, MAX_ARTIFACT_BYTES, STATE_ROOT, HermesBroker, HermesLiveTranscriptMirror, AresStore,
    create_http_server, sync_hermes_rooms, sync_recent_hermes_transcripts,
    sync_remote_hermes_chat, sync_remote_hermes_rooms,
)

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
SERVER_STATE = STATE_ROOT / "server.json"
TOKEN_PATH = STATE_ROOT / "access.token"
LOG_PATH = STATE_ROOT / "server.log"
RUNTIME_HELPER = ROOT / "bin" / "ares-hermes-runtime"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9420


def runtime_json(*arguments: str, timeout: int = 20) -> dict:
    completed = subprocess.run(
        [str(RUNTIME_HELPER), *arguments], capture_output=True, text=True, timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        return {}
    payload = json.loads(completed.stdout)
    return payload if isinstance(payload, dict) else {}


def sync_remote_runtime(store: AresStore, transcript_limit: int = 16) -> None:
    payload = runtime_json(timeout=25)
    sessions = [row for row in payload.get("sessions") or []
                if isinstance(row, dict) and str(row.get("id") or "").startswith("remote::")]
    sync_remote_hermes_rooms(store, sessions)

    references = [str(row["id"]) for row in sessions[:max(1, min(transcript_limit, 32))]]
    if not references:
        return

    def load(reference: str) -> dict:
        try:
            result = runtime_json("--session", reference, "--limit", "100", timeout=20)
            chats = result.get("chats") or []
            return chats[0] if chats and isinstance(chats[0], dict) else {}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(references))) as pool:
        for chat in pool.map(load, references):
            if chat:
                sync_remote_hermes_chat(store, chat)


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def token() -> str:
    try:
        value = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = secrets.token_urlsafe(32)
        write_private(TOKEN_PATH, value + "\n")
    os.chmod(TOKEN_PATH, 0o600)
    return value


CUSTOM_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")


def process_alive(pid: int) -> bool:
    try:
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes().decode(errors="ignore").replace("\0", " ")
        return "ares-server" in cmdline and " serve" in cmdline
    except (OSError, UnicodeError):
        return False


def state() -> dict:
    try:
        value = json.loads(SERVER_STATE.read_text(encoding="utf-8"))
        if isinstance(value, dict) and process_alive(int(value.get("pid", 0))):
            return value
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def lan_address() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def sync_runtime(store: AresStore, bridge: HermesBroker, discover: bool = False) -> None:
    profile_items = []
    try:
        profiles = bridge.call("profiles.list", {"include_sessions": False}, timeout=10)
        if profiles.get("ok") is True:
            result = profiles.get("result") or {}
            profile_items = result.get("profiles") or result.get("items") or []
            store.sync_profiles(profile_items)
    except Exception:
        pass
    try:
        sync_hermes_rooms(store, bridge, profile_items)
    except Exception:
        pass
    try:
        sync_recent_hermes_transcripts(store, bridge, limit=16)
    except Exception:
        pass
    try:
        sync_remote_runtime(store, transcript_limit=16)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    try:
        if discover:
            subprocess.run([str(RUNTIME_HELPER), "--discover"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25, check=False)
            subprocess.run([str(RUNTIME_HELPER), "--sync-peers"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25, check=False)
        peers = subprocess.run([str(RUNTIME_HELPER), "--peers"], capture_output=True, text=True, timeout=20, check=False)
        if peers.returncode == 0:
            store.sync_peers((json.loads(peers.stdout).get("peers") or []))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    try:
        usage = subprocess.run([str(RUNTIME_HELPER), "--usage"], capture_output=True, text=True, timeout=30, check=False)
        if usage.returncode == 0:
            store.sync_quotas(json.loads(usage.stdout))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass


def ensure_started(host: str | None = None, port: int | None = None) -> dict:
    current = state()
    if current:
        return current
    STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "serve"]
    if host is not None:
        command += ["--host", str(host)]
    if port is not None:
        command += ["--port", str(int(port))]
    with LOG_PATH.open("ab") as log:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(0.1)
        current = state()
        if current:
            break
    return current


def wait_ready(port: int, timeout: float = 60.0) -> bool:
    """Poll the public health endpoint until the respawned server answers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/v1/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    return False


def connection(start: bool = True) -> int:
    current = state() if not start else ensure_started()
    if not current:
        print(json.dumps({"ok": False, "running": False, "error": "Ares service unavailable"}))
        return 1
    port = int(current["port"])
    print(json.dumps({
        "ok": True, "running": True, "pid": current["pid"],
        "endpoint": f"http://127.0.0.1:{port}",
        "mobileEndpoint": f"http://{lan_address()}:{port}",
        "artifactRoot": str(ARTIFACT_ROOT),
        "token": token(),
    }))
    return 0


def configure_token(set_value: str | None, regenerate: bool) -> int:
    """Show, replace, or regenerate the browser access token."""
    if not regenerate and set_value is None:
        print(json.dumps({"ok": True, "token": token()}))
        return 0
    if regenerate:
        value = secrets.token_urlsafe(32)
    else:
        value = (set_value or "").strip()
        if not CUSTOM_TOKEN_PATTERN.fullmatch(value):
            print(json.dumps({"ok": False, "error": "Token must be 32-256 letters, digits, '_' or '-'"}))
            return 1
    write_private(TOKEN_PATH, value + "\n")
    # The running server holds the token in memory, so a change only takes
    # effect after a restart; the desktop bridge re-reads it on reconnect.
    # server.json is removed by the old process on exit, so the original
    # bind address is captured before stopping to respawn on the same one.
    current = state()
    was_running = bool(current)
    host = str(current.get("host") or DEFAULT_HOST) if was_running else None
    port = int(current["port"]) if was_running else None
    if was_running:
        if not stop_server():
            print(json.dumps({"ok": False, "error": "Token saved but the running service would not stop"}))
            return 1
        # serve() publishes server.json only after its broker handshake, which
        # can take tens of seconds, so readiness is the health endpoint, not state().
        ensure_started(host=host, port=port)
        if not wait_ready(port):
            print(json.dumps({"ok": False, "error": "Token saved but the service did not restart"}))
            return 1
    print(json.dumps({"ok": True, "token": value, "restarted": was_running}))
    return 0


def upload(path_value: str, project_id: str, room_id: str) -> int:
    """Upload one local image through the authenticated Ares API."""
    current = state()
    path = Path(path_value).expanduser().resolve()
    if not current:
        print(json.dumps({"ok": False, "error": "Ares service unavailable"}))
        return 1
    try:
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not path.is_file() or size <= 0 or size > MAX_ARTIFACT_BYTES:
            raise ValueError("image size is invalid")
        if not mime.startswith("image/"):
            raise ValueError("selected file is not an image")
        query = urllib.parse.urlencode({
            "projectId": project_id,
            "roomId": room_id,
            "name": path.name,
            "author": "user",
        })
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(current['port'])}/v1/artifacts?{query}",
            data=path.read_bytes(),
            headers={"Authorization": f"Bearer {token()}", "Content-Type": mime},
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.read().decode("utf-8"))
        return 0
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get("error", f"HTTP {error.code}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = f"HTTP {error.code}"
        print(json.dumps({"ok": False, "error": message}))
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
    return 1


def serve(host: str, port: int) -> int:
    if state():
        return 2
    access_token = token()
    store = AresStore(DB_PATH, ARTIFACT_ROOT)
    bridge = HermesBroker()
    owned_desktop = None
    try:
        profiles = bridge.call("profiles.list", {"include_sessions": False}, timeout=10)
    except Exception:
        profiles = {"ok": False}
    if profiles.get("ok") is not True:
        bridge.close()
        with LOG_PATH.open("ab") as log:
            owned_desktop = subprocess.Popen(
                ["hermes", "serve", "--port", "0", "--skip-build"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            time.sleep(0.25)
            bridge = HermesBroker()
            try:
                profiles = bridge.call("profiles.list", {"include_sessions": False}, timeout=3)
            except Exception:
                profiles = {"ok": False}
            if profiles.get("ok") is True:
                break
            bridge.close()
    if profiles.get("ok") is True:
        result = profiles.get("result") or {}
        store.sync_profiles(result.get("profiles") or result.get("items") or [])
    removed_deltas = store.cleanup_transient_delta_rows()
    if removed_deltas:
        print(json.dumps({"cleanup": "transient-delta-rows", "removed": removed_deltas}), flush=True)
    live_transcript = HermesLiveTranscriptMirror(store)
    bridge.add_listener(live_transcript.observe)
    server = create_http_server(store, host, port, access_token, WEB_ROOT, bridge, live_transcript.live_events)
    actual_port = int(server.server_address[1])
    write_private(SERVER_STATE, json.dumps({"pid": os.getpid(), "port": actual_port, "host": host, "startedAt": time.time()}) + "\n")
    def runtime_loop() -> None:
        while True:
            try:
                sync_runtime(store, bridge, discover=True)
            except Exception as exc:  # noqa: BLE001 - one bad sync must not kill discovery forever
                print(json.dumps({"runtimeSyncError": str(exc)[:300]}), flush=True)
            time.sleep(60)
    threading.Thread(target=runtime_loop, daemon=True, name="ares-runtime-sync").start()
    def transcript_loop():
        last_persisted_sync = 0.0
        while True:
            try:
                with store._lock:
                    profile_rows = store._db.execute(
                        "SELECT DISTINCT external_profile FROM rooms WHERE source='hermes' AND deleted_at IS NULL "
                        "AND external_profile NOT LIKE 'remote::%'"
                    ).fetchall()
                for profile_row in profile_rows:
                    profile = str(profile_row["external_profile"] or "default")
                    active = bridge.call("session.active_list", {"profile": profile}, timeout=10)
                    sessions = (active.get("result") or {}).get("sessions") if active.get("ok") is True else []
                    live_transcript.update_active_sessions(profile, sessions if isinstance(sessions, list) else [])
                if time.monotonic() - last_persisted_sync >= 2:
                    sync_recent_hermes_transcripts(store, bridge, limit=8)
                    last_persisted_sync = time.monotonic()
            except Exception:
                pass
            time.sleep(0.5)
    threading.Thread(target=transcript_loop, daemon=True, name="ares-transcript-sync").start()

    def shutdown(_signum, _frame):
        # serve_forever must be stopped from another thread; process exit is safe
        # because SQLite is WAL-backed and every mutation is transactional.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.remove_listener(live_transcript.observe)
        bridge.close()
        if owned_desktop is not None and owned_desktop.poll() is None:
            owned_desktop.terminate()
            try:
                owned_desktop.wait(timeout=5)
            except subprocess.TimeoutExpired:
                owned_desktop.kill()
        store.close()
        try:
            if json.loads(SERVER_STATE.read_text()).get("pid") == os.getpid():
                SERVER_STATE.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return 0


def stop_server() -> bool:
    current = state()
    if not current:
        return True
    pid = int(current["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process_alive(pid):
        time.sleep(0.1)
    return not process_alive(pid)


def stop() -> int:
    had_server = bool(state())
    stopped = stop_server()
    print(json.dumps({"ok": stopped, "running": not stopped} if had_server else {"ok": True, "running": False}))
    return 0 if stopped else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    commands.add_parser("connection")
    commands.add_parser("status")
    commands.add_parser("stop")
    token_parser = commands.add_parser("token", help="Show, set, or regenerate the browser access token")
    token_group = token_parser.add_mutually_exclusive_group()
    token_group.add_argument("--set", dest="set_value", metavar="VALUE",
                             help="Use a custom token: 32-256 letters, digits, '_' or '-'")
    token_group.add_argument("--regenerate", action="store_true",
                             help="Replace the token with a fresh random one")
    upload_parser = commands.add_parser("upload")
    upload_parser.add_argument("--project", required=True)
    upload_parser.add_argument("--room", required=True)
    upload_parser.add_argument("--path", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        return serve(args.host, args.port)
    if args.command == "connection":
        return connection(True)
    if args.command == "status":
        return connection(False)
    if args.command == "token":
        return configure_token(args.set_value, args.regenerate)
    if args.command == "upload":
        return upload(args.path, args.project, args.room)
    return stop()


if __name__ == "__main__":
    raise SystemExit(main())
