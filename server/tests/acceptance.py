#!/usr/bin/env python3
"""Isolated Python/Rust HTTP conformance and real-process CLI acceptance.

Run from any directory after cargo build --release. All fixtures, sidecars,
logs and state are created inside server/test-work; no live state is read.
"""
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
BIN = SERVER / "target/release/ares-server"
TOKEN = "acceptance_token_0123456789abcdef_ABC"
SPEC = importlib.util.spec_from_file_location("ares_service", ROOT / "bin/ares_service.py")
PY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PY)
COUNT = 0


def request(port, method, path, body=None, raw=None, headers=None, token=TOKEN):
    hdr = {"Authorization": "Bearer " + token}
    if body is not None:
        raw = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    hdr.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=raw, headers=hdr)
        response = conn.getresponse()
        payload = response.read()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if headers.get("content-type", "").startswith("application/json") and payload:
            payload = json.loads(payload)
        return response.status, payload, headers
    finally:
        conn.close()


def normalize(value, key=""):
    if key in {"createdAt", "updatedAt", "startedAt", "finishedAt", "deletedAt"} and value is not None:
        return "<time>"
    if isinstance(value, dict):
        return {k: normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\b(grp|prj|room|evt|exec|art)_[a-f0-9]{32}\b", r"\1_<id>", value)
    return value


def compare(label, left, right):
    global COUNT
    assert left[0] == right[0], (label, left, right)
    assert normalize(left[1]) == normalize(right[1]), (
        label, json.dumps(normalize(left[1]), default=str, indent=2),
        json.dumps(normalize(right[1]), default=str, indent=2))
    for name in ["x-content-type-options", "referrer-policy", "x-frame-options", "cache-control", "content-type"]:
        assert left[2].get(name) == right[2].get(name), (label, name, left[2], right[2])
    COUNT += 1


def wait_state(env, timeout=8):
    path = Path(env["XDG_STATE_HOME"]) / "omarchy/ares/server.json"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = json.loads(path.read_text())
            if request(value["port"], "GET", "/v1/health")[0] == 200:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(.05)
    raise AssertionError("server did not start: " + str(path))


def cli(env, *args, code=0):
    run = subprocess.run([str(BIN), *args], env=env, capture_output=True, text=True, timeout=75)
    assert run.returncode == code, (args, run.returncode, run.stdout, run.stderr)
    return json.loads(run.stdout)


def seed(path, artifacts, hermes):
    store = PY.AresStore(path, artifacts)
    project = store.create_project("Seed", "/seed", "remote")
    room = store.create_room(project["id"], "Seed room", ["default"])
    store._db.execute("INSERT INTO peers(id,name,status,updated_at,source_name) VALUES('peer','Peer','ok',0,'Peer')")
    store.sync_quotas({"updatedAt": 1, "providers": [{"provider": "fixture", "hourTokens": 10, "quota": {"status": "ok", "plan": "test", "details": ["detail"], "windows": [{"label": "Week", "availablePercent": 90, "usedPercent": 10, "windowSeconds": 604800}]}}]})
    samples = [
        "Plain **bold** and *italic*; __strong__ and _em_, ~~gone~~, `x < 1`.",
        "# Title\n\nA [safe](https://example.com) and [bad](javascript:alert).\n\n- one\n- two\n\n1. alpha\n2) beta",
        "```rust\nlet x = \"<tag>\";\n```\n\n> quote\n> continued\n\n---\n\n| A | B |\n| :- | -: |\n| C | D |",
        "snake_case a*b*c **nested _em_**\n\n<script>no</script>",
    ]
    for source in samples:
        store.append_event(project["id"], room["id"], None, "message.assistant", "default", {"text": source})
    for payload in [{"output": {"output": "hello"}}, {"args": {"command": " ls "}}, {"result": {"_multimodal": True, "content": [{"type": "text", "text": "caption"}]}}, {"error": "oops"}, {"text": '{"output": "wrapped"}'}]:
        store.append_event(project["id"], room["id"], None, "tool.result", "default", payload)
    store.append_event(project["id"], room["id"], None, "message.delta", "default", {"ephemeral": True, "delta": "live"})
    store.close()
    return project["id"], room["id"]


def http_checks(work):
    hermes = work / "hermes"
    profile = hermes / "profiles/default"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("# fixture\nmodel:\n  default: fixture-model\n  provider: fixture-provider\n")
    PY.HERMES_HOME = hermes
    seed_db = work / "seed.db"
    project, room = seed(seed_db, work / "seed-artifacts", hermes)
    rs_root = work / "rust/omarchy/ares"
    rs_root.mkdir(parents=True)
    shutil.copyfile(seed_db, rs_root / "ares.db")
    shutil.copyfile(seed_db, work / "python.db")
    (rs_root / "access.token").write_text(TOKEN + "\n")
    env = dict(os.environ, XDG_STATE_HOME=str(work / "rust"), HERMES_HOME=str(hermes))
    store = PY.AresStore(work / "python.db", work / "python-artifacts")
    py = PY.create_http_server(store, "127.0.0.1", 0, TOKEN, ROOT / "web", bridge=None)
    thread = threading.Thread(target=py.serve_forever, daemon=True)
    thread.start()
    with (work / "rust.log").open("wb") as log:
        rust = subprocess.Popen([str(BIN), "serve", "--host", "127.0.0.1", "--port", "0"], env=env, stdout=log, stderr=log)
        try:
            rs_port = wait_state(env)["port"]
            ports = [py.server_port, rs_port]

            def step(label, method, path, body=None, **kwargs):
                results = [request(p, method, path, body, **kwargs) for p in ports]
                compare(label, *results)
                return [r[1] for r in results]

            step("public health", "GET", "/v1/health", token="")
            step("auth", "GET", "/v1/state", token="bad")
            step("models", "GET", "/v1/models")
            for query in ["", "?compact=1", "?compact=1&eventRoomId=" + room, "?roomId=" + room, "?roomId=missing"]:
                step("snapshot " + query, "GET", "/v1/state" + query)
            step("live", "GET", "/v1/live?roomId=" + room)
            step("missing live room", "GET", "/v1/live")
            step("events", "GET", "/v1/events?after=0&limit=5")
            step("before", "GET", "/v1/events?before=100&roomId=" + room)
            step("missing history room", "GET", "/v1/events?before=1")
            step("group", "POST", "/v1/groups", {"name": " Group ", "instructions": " hello "})
            step("bad project", "POST", "/v1/projects", {"name": "Bad", "path": str(work / "absent")})
            step("existing project", "POST", "/v1/projects", {"name": "Ignored", "path": "/seed", "nodeId": "remote"})
            step("new remote project", "POST", "/v1/projects", {"name": "New", "path": "/new", "nodeId": "remote"})
            step("room", "POST", "/v1/rooms", {"projectId": project, "name": "New", "participants": ["default", "default"]})
            step("alias", "POST", f"/v1/rooms/{room}/alias", {"alias": "Alias"})
            step("alias missing", "POST", "/v1/rooms/missing/alias", {"alias": "Alias"})
            step("draft save", "POST", f"/v1/drafts/{room}", {"text": " Draft "})
            step("draft get", "GET", "/v1/drafts")
            step("draft clear", "POST", f"/v1/drafts/{room}", {"text": " "})
            step("preferences", "POST", "/v1/preferences", {"homeQuotaVisible": False, "sidebarWidth": 240, "hiddenHomeQuotaProviders": [" x ", "x"], "selectedRoomId": room})
            for body in [{"unknown": 1}, {"sidebarWidth": True}, {"panelSize": "huge"}, {"homeQuotaVisible": 1}, {"hiddenHomeQuotaProviders": [1]}]:
                step("bad preference", "POST", "/v1/preferences", body)
            step("peer preferences", "POST", "/v1/peers/peer/preferences", {"name": "Renamed", "colorRole": "info"})
            step("bot preferences", "POST", "/v1/bots/default", {"name": "Agent", "faceShape": "oval", "colorRole": "success"})
            step("missing bot", "POST", "/v1/bots/missing", {})
            step("bad bot", "POST", "/v1/bots/default", {"faceShape": "invalid"})
            step("approval", "POST", "/v1/executions/missing/approval", {})
            step("unknown", "GET", "/v1/unknown")
            step("JSON object", "POST", "/v1/groups", raw=b"[]")
            step("empty JSON", "POST", "/v1/groups", raw=b"")
            step("large JSON", "POST", "/v1/groups", headers={"Content-Length": str(2 * 1024 * 1024 + 1)})
            query = urllib.parse.urlencode(dict(projectId=project, roomId=room, name="picture.png", author="user"))
            artifacts = step("upload", "PUT", "/v1/artifacts?" + query, raw=b"image fixture", headers={"Content-Type": "image/png"})
            for range_ in ["", "bytes=0-0", "bytes=2-", "bytes=1-999", "bytes=999-", "bytes=-3", "bytes=5-2", "bytes=0-1,3-4"]:
                results = [request(p, "GET", "/v1/artifacts/" + a["artifact"]["id"], headers={"Range": range_}) for p, a in zip(ports, artifacts)]
                compare("artifact range " + range_, *results)
                if results[1][0] == 416:
                    assert results[1][2]["content-range"] == "bytes */13"
            step("unknown artifact", "GET", "/v1/artifacts/missing")
            step("unsupported artifact", "PUT", "/v1/artifacts?" + query, raw=b"text", headers={"Content-Type": "text/plain"})
            prompt = dict(projectId=project, text="hello", idempotencyKey="send-1")
            first = step("send", "POST", f"/v1/rooms/{room}/send", prompt)
            second = step("send replay", "POST", f"/v1/rooms/{room}/send", prompt)
            assert first == second
            step("send conflict", "POST", f"/v1/rooms/{room}/send", dict(prompt, text="different"))
            step("artifact rollback", "POST", f"/v1/rooms/{room}/send", dict(prompt, idempotencyKey="rollback", artifactIds=["missing"]))
            for action in ["pause", "resume", "archive", "restore"]:
                step(action, "POST", f"/v1/rooms/{room}/{action}", dict(projectId=project, idempotencyKey=action))
            for path in ["/", "/missing/spa/page", "/%2e%2e/bin/ares_service.py", "/app.js"]:
                step("static " + path, "GET", path, token="")
            step("final snapshot", "GET", "/v1/state")
            # A fresh room exercises successful delete/restore without queued executions.
            rooms = step("deletable room", "POST", "/v1/rooms", dict(projectId=project, name="Delete me"))
            for action in ["delete", "restore", "resume"]:
                results = [request(p, "POST", f"/v1/rooms/{r['room']['id']}/{action}", dict(projectId=project, idempotencyKey=action)) for p, r in zip(ports, rooms)]
                compare("successful " + action, *results)
            # Rust can read the complete Python-created schema without changing it.
            with sqlite3.connect(rs_root / "ares.db") as rs_db:
                left = store._db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                right = rs_db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                assert [tuple(r) for r in left] == right
        finally:
            rust.send_signal(signal.SIGTERM)
            rust.wait(timeout=5)
            assert rust.returncode == 0
            assert not (rs_root / "server.json").exists()
            py.shutdown()
            py.server_close()
            store.close()


def cli_checks(work):
    global COUNT
    env = dict(os.environ, XDG_STATE_HOME=str(work / "cli"), HERMES_HOME=str(work / "cli-hermes"))
    root = Path(env["XDG_STATE_HOME"]) / "omarchy/ares"
    assert cli(env, "status", code=1) == {"ok": False, "running": False, "error": "Ares service unavailable"}
    assert cli(env, "stop") == {"ok": True, "running": False}
    initial = cli(env, "token")["token"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", initial)
    assert cli(env, "token", "--set", "short", code=1)["error"] == "Token must be 32-256 letters, digits, '_' or '-'"
    assert cli(env, "token", "--set", TOKEN) == {"ok": True, "token": TOKEN, "restarted": False}
    proc = subprocess.Popen([str(BIN), "serve", "--host", "127.0.0.1", "--port", "0"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        original = wait_state(env)
        connection = cli(env, "connection")
        assert connection["pid"] == original["pid"]
        changed = cli(env, "token", "--regenerate")
        assert changed["restarted"] and changed["token"] != TOKEN
        proc.wait(timeout=5)
        current = wait_state(env)
        assert current["pid"] != original["pid"]
        assert current["host"] == original["host"] and current["port"] == original["port"]
        assert request(current["port"], "GET", "/v1/state", token=TOKEN)[0] == 401
        auth = changed["token"]
        project = request(current["port"], "POST", "/v1/projects", dict(name="CLI", path="/cli", nodeId="remote"), token=auth)[1]["project"]
        room = request(current["port"], "POST", "/v1/rooms", dict(projectId=project["id"], name="CLI room"), token=auth)[1]["room"]
        image = work / "cli.png"
        image.write_bytes(b"image fixture")
        upload = cli(env, "upload", "--project", project["id"], "--room", room["id"], "--path", str(image))
        assert upload["ok"] and upload["artifact"]["size"] == 13
        for name in ["access.token", "server.json", "ares.db"]:
            assert (root / name).stat().st_mode & 0o777 == 0o600
        for path in [root, root / "artifacts"]:
            assert path.stat().st_mode & 0o777 == 0o700
        assert cli(env, "stop") == {"ok": True, "running": False}
        assert not (root / "server.json").exists()
        # Exercise default-port startup if available without touching another service.
        with socket.socket() as probe:
            try:
                probe.bind(("0.0.0.0", 9420))
                default_available = True
            except OSError:
                default_available = False
        if default_available:
            connection = cli(env, "connection")
            assert connection["running"] and connection["endpoint"] == "http://127.0.0.1:9420"
            pid = connection["pid"]
        else:
            assert cli(env, "connection", code=1)["error"] == "Ares service unavailable"
            proc = subprocess.Popen([str(BIN), "serve", "--host", "127.0.0.1", "--port", "0"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            pid = wait_state(env)["pid"]
        os.kill(pid, signal.SIGINT)
        end = time.monotonic() + 5
        while (root / "server.json").exists() and time.monotonic() < end:
            time.sleep(.05)
        assert not (root / "server.json").exists()
        if not default_available:
            proc.wait(timeout=5)
            assert proc.returncode == 0
        cli(env, "status", code=1)
        COUNT += 20
    finally:
        with contextlib.suppress(Exception):
            cli(env, "stop")
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        proc.stderr.close()


def main():
    # Check the copied CREATE statements verbatim, including whitespace.
    source = (ROOT / "bin/ares_service.py").read_text()
    schema = (SERVER / "src/schema.sql").read_text()
    statements = re.findall(r"            CREATE TABLE IF NOT EXISTS .*?            \);", source, re.S)
    assert len(statements) == 17
    assert all(statement in schema for statement in statements)
    (SERVER / "test-work").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SERVER / "test-work") as tmp:
        work = Path(tmp)
        http_checks(work)
        cli_checks(work)
    print(f"PASS: {COUNT} HTTP/CLI compatibility checks; verbatim schema and Python DB reuse verified")


if __name__ == "__main__":
    main()
