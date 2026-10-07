#!/usr/bin/env python3
"""Side-by-side conformance check: Python ares_service vs Rust ares-server.

Copies the live Ares DB into two throwaway state dirs, boots the Python server
on one port and the Rust server on another, replays the same request sequence
against both, and diffs normalized responses (volatile ids/timestamps/cursors
stripped). Exits non-zero on any mismatch. Skips cleanly when the Rust binary
has not been built yet.

Usage: python3 tests/conformance_rust.py [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_SERVER = ROOT / "bin" / "ares-server"
RUST_BIN = ROOT / "server" / "target" / "release" / "ares-server"
LIVE_DB = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omarchy" / "ares" / "ares.db"

PY_PORT = 19510
RS_PORT = 19511

VOLATILE_KEYS = {
    "id", "createdAt", "updatedAt", "cursor", "latestCursor", "earliestCursor",
    "pid", "token", "endpoint", "mobileEndpoint", "artifactRoot", "startedAt",
    "idempotencyKey", "executionId", "eventRoomId", "path", "sha256",
    # Owned by the Hermes sync loops (Phase 2 in Rust): the Python server
    # rewrites them on boot while Rust serves the snapshot unchanged.
    "externalUpdatedAt", "externalActivityAt",
}
VOLATILE_ID_FIELDS = {"projectId", "roomId"}
# Steps where Phase 1 legitimately deviates: the Rust server has no Hermes
# bridge yet, so it answers with the Python no-bridge contract; compare the
# status code only.
STATUS_ONLY_STEPS = {"approval-no-bridge"}


def normalize(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in VOLATILE_KEYS:
                out[key] = "<volatile>"
            elif key in VOLATILE_ID_FIELDS and isinstance(item, str) and item:
                out[key] = "<id>"
            else:
                out[key] = normalize(item)
        return out
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, float):
        # Python repr vs Rust Ryu last-digit noise; 12 significant digits is
        # far inside anything a JSON consumer can observe.
        return float(f"{value:.12g}")
    if isinstance(value, str) and re.fullmatch(r"(prj|grp|room|evt|exec|art)_[0-9a-f]+", value):
        return "<id>"
    return value


def request(port: int, token: str, method: str, path: str, body=None, raw=None, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = raw
    hdrs = {"Accept": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = response.read()
            try:
                return response.status, json.loads(payload), dict(response.headers)
            except json.JSONDecodeError:
                return response.status, payload, dict(response.headers)
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            return error.code, json.loads(payload), dict(error.headers)
        except json.JSONDecodeError:
            return error.code, payload, dict(error.headers)


def wait_health(port: int, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.4)
    return False


def make_state_dir(base: Path) -> Path:
    state = base / "state"
    ares = state / "omarchy" / "ares"
    ares.mkdir(parents=True)
    if LIVE_DB.exists():
        source = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
        target = sqlite3.connect(ares / "ares.db")
        source.backup(target)
        target.close()
        source.close()
    (ares / "artifacts").mkdir(exist_ok=True)
    return state


class Server:
    def __init__(self, command: list[str], env: dict, port: int, log: Path):
        self.command, self.env, self.port, self.log = command, env, port, log
        self.proc = None

    def __enter__(self):
        self.log_handle = self.log.open("wb")
        self.proc = subprocess.Popen(
            self.command, env=self.env, stdin=subprocess.DEVNULL,
            stdout=self.log_handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if not wait_health(self.port):
            raise RuntimeError(f"server on port {self.port} did not become healthy; see {self.log}")
        return self

    def __exit__(self, *_):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log_handle.close()


def scrub_state(value):
    """Deterministic /v1/state view: top-level keys plus only the entities the
    scenario created. Sync loops (Phase 2) import live-desktop rooms, events,
    bots, peers and quota on independent timers, so those collections churn
    between the two servers and are excluded by design."""
    if not isinstance(value, dict):
        return value
    scenario_names = {"Conformance", "Conf room"}
    return {
        "topKeys": sorted(value.keys()),
        "projects": [p for p in value.get("projects", []) if p.get("name") in scenario_names],
        "rooms": [r for r in value.get("rooms", []) if r.get("name") in scenario_names],
        "archivedRooms": [r for r in value.get("archivedRooms", []) if r.get("name") in scenario_names],
    }


STATE_STEPS = {"state-compact", "state-final"}


def scenario(port: int, token: str) -> list[tuple[str, int, object]]:
    """One fixed request sequence; returns (label, status, normalized-body)."""
    results = []

    def step(label: str, method: str, path: str, body=None, raw=None, headers=None, expect_json=True):
        status, payload, _hdrs = request(port, token, method, path, body=body, raw=raw, headers=headers)
        if label in STATE_STEPS:
            payload = scrub_state(payload)
        results.append((label, status, normalize(payload) if expect_json and not isinstance(payload, bytes) else payload[:64]))

    status, payload, _ = request(port, "", "GET", "/v1/health")
    results.append(("health-public", status, normalize(payload)))
    step("unauthorized", "GET", "/v1/state?compact=1", headers={"Authorization": "Bearer wrong"})
    step("models", "GET", "/v1/models")
    step("state-compact", "GET", "/v1/state?compact=1&eventRoomId=")
    step("events-after", "GET", "/v1/events?after=0&limit=5")
    step("drafts", "GET", "/v1/drafts?device=desktop")
    step("group-create", "POST", "/v1/groups", {"name": "Conformance", "instructions": "side by side"})
    step("project-create", "POST", "/v1/projects", {"name": "Conformance", "path": "/tmp/conformance", "nodeId": "local"})
    # Discover the created project id for room creation (each server made its own).
    _, state_body, _ = request(port, token, "GET", "/v1/state?compact=1&eventRoomId=")
    assert isinstance(state_body, dict)
    project = next((p for p in state_body.get("projects", []) if p.get("name") == "Conformance"), None)
    if project:
        step("room-create", "POST", "/v1/rooms", {"projectId": project["id"], "name": "Conf room", "participants": ["default"]})
    _, state_body, _ = request(port, token, "GET", "/v1/state?compact=1&eventRoomId=")
    assert isinstance(state_body, dict)
    room = next((r for r in state_body.get("rooms", []) if r.get("name") == "Conf room"), None)
    if room:
        step("room-alias", "POST", f"/v1/rooms/{room['id']}/alias", {"alias": "conf"})
        step("draft-save", "POST", f"/v1/drafts/{room['id']}", {"device": "desktop", "text": "draft text"})
        step("artifact-put", "PUT", f"/v1/artifacts?projectId={room['projectId']}&roomId={room['id']}&name=conf.png&author=user",
             raw=b"\x89PNG\r\n\x1a\nconformance", headers={"Content-Type": "image/png"})
        step("room-send", "POST", f"/v1/rooms/{room['id']}/send",
             {"projectId": room["projectId"], "botId": "default", "text": "hello conformance", "idempotencyKey": "conf-key-1"})
        step("room-send-dupe", "POST", f"/v1/rooms/{room['id']}/send",
             {"projectId": room["projectId"], "botId": "default", "text": "hello conformance", "idempotencyKey": "conf-key-1"})
        step("room-pause", "POST", f"/v1/rooms/{room['id']}/pause", {"projectId": room["projectId"], "idempotencyKey": "conf-pause"})
        step("room-resume", "POST", f"/v1/rooms/{room['id']}/resume", {"projectId": room["projectId"], "idempotencyKey": "conf-resume"})
        step("room-archive", "POST", f"/v1/rooms/{room['id']}/archive", {"projectId": room["projectId"], "idempotencyKey": "conf-arch"})
        step("room-restore", "POST", f"/v1/rooms/{room['id']}/restore", {"projectId": room["projectId"], "idempotencyKey": "conf-restore"})
    step("approval-no-bridge", "POST", "/v1/executions/exec_missing/approval", {"requestId": "r", "choice": "once"})
    step("preferences", "POST", "/v1/preferences", {"homeQuotaVisible": False})
    step("state-final", "GET", "/v1/state?compact=1&eventRoomId=")
    step("not-found", "GET", "/v1/definitely-not-here")
    step("static-index", "GET", "/", headers={"Authorization": ""})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if not RUST_BIN.exists():
        print(f"SKIP: Rust binary not built yet ({RUST_BIN})")
        return 0
    if not LIVE_DB.exists():
        print(f"SKIP: live DB not found ({LIVE_DB})")
        return 0

    work = Path(tempfile.mkdtemp(prefix="ares-conformance-"))
    try:
        py_state = make_state_dir(work / "py")
        rs_state = make_state_dir(work / "rs")
        # Give each side its own artifacts dir + deterministic token.
        for state in (py_state, rs_state):
            token_path = state / "omarchy" / "ares" / "access.token"
            token_path.write_text("conformance_token_0123456789abcdef\n")
            token_path.chmod(0o600)
        token = "conformance_token_0123456789abcdef"

        py_env = dict(os.environ, XDG_STATE_HOME=str(py_state), PYTHONPATH=str(ROOT / "bin"))
        rs_env = dict(os.environ, XDG_STATE_HOME=str(rs_state))
        # Phase 2 sync loops re-import hermes rooms/events from the LIVE desktop
        # on independent timers, which makes full-state comparisons flaky; the
        # Python side has no such kill switch, so state steps tolerate sync-owned
        # drift via VOLATILE_KEYS and the live-sync note below.

        with Server([sys.executable, str(PY_SERVER), "serve", "--host", "127.0.0.1", "--port", str(PY_PORT)],
                    py_env, PY_PORT, work / "py.log"):
            with Server([str(RUST_BIN), "serve", "--host", "127.0.0.1", "--port", str(RS_PORT)],
                        rs_env, RS_PORT, work / "rs.log"):
                py_results = scenario(PY_PORT, token)
                rs_results = scenario(RS_PORT, token)

        py_map = {label: (status, body) for label, status, body in py_results}
        rs_map = {label: (status, body) for label, status, body in rs_results}
        mismatches = 0
        for label, status, body in py_results:
            other = rs_map.get(label)
            if other is None:
                print(f"MISSING in rust: {label}")
                mismatches += 1
                continue
            if other != (status, body):
                if label in STATUS_ONLY_STEPS and other[0] == status:
                    continue
                mismatches += 1
                print(f"MISMATCH {label}:")
                print(f"  python: {status} {json.dumps(body, sort_keys=True)[:400]}")
                print(f"  rust:   {other[0]} {json.dumps(other[1], sort_keys=True)[:400]}")
        for label in rs_map:
            if label not in py_map:
                print(f"EXTRA in rust: {label}")
                mismatches += 1
        total = len(py_results)
        print(f"\n{total - mismatches}/{total} steps conform")
        return 1 if mismatches else 0
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
