#!/usr/bin/env python3
"""Read-only discovery of one live Hermes pane on its owning SSH host.

This script is sent to `python3 -` over SSH; it is never installed remotely.
It prints terminal identity only, never pane contents or credentials.
"""

import json
import hashlib
import os
import re
import stat
import subprocess
import sys
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
PANE_ID = re.compile(r"w[A-Za-z0-9]+:p[A-Za-z0-9]+\Z")
TAB_ID = re.compile(r"w[A-Za-z0-9]+:t[A-Za-z0-9]+\Z")


def valid_socket(socket, home):
    base = Path(home) / ".config/herdr"
    path = Path(socket)
    if path == base / "herdr.sock":
        return True
    try:
        relative = path.relative_to(base / "sessions")
    except ValueError:
        return False
    return (len(relative.parts) == 2 and relative.name == "herdr.sock"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", relative.parts[0]) is not None)


def validate_owner(pane, tab, socket, session_id, snapshot_for_socket, home):
    if not (PANE_ID.fullmatch(pane) and TAB_ID.fullmatch(tab) and valid_socket(socket, home)):
        return None
    snapshot = snapshot_for_socket(socket)
    agents = [agent for agent in snapshot.get("agents", [])
              if agent.get("agent") == "hermes" and agent.get("pane_id") == pane
              and agent.get("tab_id") == tab]
    if len(agents) != 1 or not any(item.get("pane_id") == pane and item.get("tab_id") == tab
                                   for item in snapshot.get("panes", [])):
        return None
    if not any(item.get("tab_id") == tab for item in snapshot.get("tabs", [])):
        return None
    claimed = agents[0].get("agent_session")
    if isinstance(claimed, dict) and claimed.get("value") not in (None, session_id):
        return None
    return {"paneId": pane, "tabId": tab, "socketPath": socket}


def find_live_pane(session_id, entries, environment_for_pid, snapshot_for_socket, home):
    matches = [entry for entry in entries if entry.get("session_id") == session_id]
    if len(matches) > 1:
        raise ValueError("ambiguous remote Hermes session owner")
    if not matches:
        return None
    owner = matches[0]
    if owner.get("surface") != "cli" or not isinstance(owner.get("pid"), int) or owner["pid"] <= 0:
        return None
    env = environment_for_pid(owner["pid"])
    return validate_owner(env.get("HERDR_PANE_ID", ""), env.get("HERDR_TAB_ID", ""),
                          env.get("HERDR_SOCKET_PATH", ""), session_id, snapshot_for_socket, home)


def iter_process_argv():
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = (Path("/proc") / name / "cmdline").read_bytes()
        except OSError:
            continue
        yield int(name), [token.decode(errors="replace") for token in raw.split(b"\0") if token]


def find_resumed_pane(session_id, list_processes, environment_for_pid, snapshot_for_socket, home):
    """Fallback when the lease registry has no entry: locate a live CLI process
    resumed with this exact session id and verify it owns a herdr pane."""
    matches = []
    for pid, argv in list_processes():
        for index, token in enumerate(argv[:-1]):
            if token == "--resume" and argv[index + 1] == session_id:
                matches.append(pid)
                break
    if len(matches) > 1:
        raise ValueError("ambiguous resumed remote Hermes session owner")
    if not matches:
        return None
    env = environment_for_pid(matches[0])
    return validate_owner(env.get("HERDR_PANE_ID", ""), env.get("HERDR_TAB_ID", ""),
                          env.get("HERDR_SOCKET_PATH", ""), session_id, snapshot_for_socket, home)


def process_environment(pid):
    raw = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    allowed = (b"HERDR_PANE_ID=", b"HERDR_TAB_ID=", b"HERDR_SOCKET_PATH=")
    return {key.decode(): value.decode(errors="replace")
            for item in raw if item.startswith(allowed)
            for key, value in [item.split(b"=", 1)]}


def inventory_items(snapshot, socket, session_name):
    """Normalize Herdr's panes for any CLI; never infer a chat from its title."""
    tabs = {tab.get("tab_id"): tab for tab in snapshot.get("tabs", [])}
    workspaces = {w.get("workspace_id"): w for w in snapshot.get("workspaces", [])}
    result = []
    for pane in snapshot.get("panes", [])[:512]:
        pid, tid = pane.get("pane_id", ""), pane.get("tab_id", "")
        terminal = pane.get("terminal_id", "")
        if not (PANE_ID.fullmatch(pid) and TAB_ID.fullmatch(tid) and tid in tabs
                and isinstance(terminal, str) and SESSION_ID.fullmatch(terminal)):
            continue
        agents = [a for a in snapshot.get("agents", []) if a.get("pane_id") == pid
                  and a.get("tab_id") == tid and a.get("terminal_id") == terminal]
        if len(agents) > 1:
            continue
        agent = agents[0] if agents else {}
        kind = agent.get("agent") or "shell"
        claimed = agent.get("agent_session") or {}
        sid = claimed.get("value", "") if (claimed.get("kind") == "id"
              and claimed.get("agent") == kind and claimed.get("source") == "herdr:" + kind) else ""
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            sid = ""
        result.append({"paneId": pid, "tabId": tid, "terminalId": terminal,
                       "socketPath": socket, "herdrSession": session_name,
                       "agentKind": kind, "sessionId": sid,
                       "profile": str(claimed.get("profile") or "") if sid else "",
                       "identitySource": "herdr" if sid else "",
                       "status": agent.get("agent_status") or "unknown",
                       "workspace": str(workspaces.get(pane.get("workspace_id"), {}).get("label") or "")[:160],
                       "label": str(tabs[tid].get("label") or "")[:160]})
    return result


def enrich_registry(items, entries, environment_for_pid, start_for_pid, profile):
    """A lease is optional identity evidence, never evidence of terminal absence."""
    for entry in entries[:512]:
        pid, sid = entry.get("pid"), entry.get("session_id")
        started = entry.get("process_start_time")
        if (entry.get("surface") != "cli" or type(pid) is not int or pid <= 0
                or not isinstance(sid, str) or not SESSION_ID.fullmatch(sid)
                or not isinstance(started, (int, float))):
            continue
        try:
            actual = start_for_pid(pid)
            if actual is None or abs(actual - started) > 0.05:
                continue
            env = environment_for_pid(pid)
        except OSError:
            continue
        matches = [item for item in items if item["agentKind"] == "hermes"
                   and item["paneId"] == env.get("HERDR_PANE_ID")
                   and item["tabId"] == env.get("HERDR_TAB_ID")
                   and item["socketPath"] == env.get("HERDR_SOCKET_PATH")]
        if len(matches) == 1:
            item = matches[0]
            if item["identitySource"] == "conflict":
                continue
            if (item.get("ownerPid") is not None and item["ownerPid"] != pid):
                item.update(sessionId="", profile="", identitySource="conflict")
                continue
            if ((item["sessionId"] and item["sessionId"] != sid)
                    or (item["profile"] and item["profile"] != profile)):
                item.update(sessionId="", profile="", identitySource="conflict")
            else:
                item.update(sessionId=sid, profile=profile, identitySource="registry",
                            ownerPid=pid, leaseId=entry.get("lease_id"))


def _private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Untrusted Ares launch receipt")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("Oversized Ares launch receipt")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid Ares launch receipt")
    return value


def restrict_guarded_inventory(items, home):
    """Suppress the identity of Ares-started panes until their child acknowledged restoration."""
    root = Path(home) / ".local/state/ares/terminal-open"
    for item in items:
        if item.get("herdrSession") != "ares" or item.get("agentKind") != "hermes":
            continue
        profile, sid = item.get("profile"), item.get("sessionId")
        if not (isinstance(profile, str) and SESSION_ID.fullmatch(profile)
                and isinstance(sid, str) and SESSION_ID.fullmatch(sid)):
            continue
        key = hashlib.sha256((profile + "\0" + sid).encode()).hexdigest()
        job = root / (key + ".json")
        if not job.exists() and not job.is_symlink():
            continue
        try:
            directory = root.stat()
            if directory.st_uid != os.getuid() or directory.st_mode & 0o077 or root.is_symlink():
                raise ValueError("Untrusted Ares launch directory")
            marker = _private_json(job)
            ready = Path(marker["ready"])
            if (ready.name != "ready.json" or ready.parent.parent != root
                    or not ready.parent.name.startswith("resume-")):
                raise ValueError("Untrusted Ares readiness path")
            ack = _private_json(ready)
            fields = {"protocol":"hermes-exact-resume-ready-v1", "nonce":marker["nonce"],
                      "sessionId":sid, "source":marker["source"], "pid":item.get("ownerPid"),
                      "leaseId":item.get("leaseId"), "paneId":item["paneId"], "tabId":item["tabId"]}
            if (marker.get("request", {}).get("sessionId") != sid
                    or marker.get("request", {}).get("profile") != profile
                    or not marker.get("attempted") or item.get("identitySource") != "registry"
                    or any(marker.get(k) != item.get(k) for k in ("paneId", "tabId", "terminalId"))
                    or any(v is None or type(ack.get(k)) is not type(v) or ack[k] != v
                           for k, v in fields.items())
                    or type(ack.get("messageCount")) is not int or ack["messageCount"] <= 0):
                raise ValueError("Ares launch has no verified child")
        except (OSError, KeyError, TypeError, ValueError):
            item.update(sessionId="", profile="", identitySource="conflict")


def process_started_at(pid):
    # comm can contain spaces and parentheses; fields after its final ')' are stable.
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    boot = next(float(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                if line.startswith("btime "))
    return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")


def read_inventory():
    """Observe every existing host session; never create a server, pane or agent."""
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    if not config.is_absolute():
        config = home / ".config"
    base = config / "herdr"
    sockets = [base / "herdr.sock", *sorted((base / "sessions").glob("*/herdr.sock"))]
    sockets = [path for path in sockets if path.is_socket()]
    discovery_errors = []
    if len(sockets) > 16:
        discovery_errors.append({"error": "Herdr session limit reached; inventory is incomplete"})
    sockets = sockets[:16]
    programs = [home / ".local/bin/herdr", home / ".cargo/bin/herdr",
                Path("/usr/bin/herdr"), Path("/usr/local/bin/herdr")]
    executable = next((p for p in programs if p.is_file() and os.access(p, os.X_OK)), None)
    found = shutil.which("herdr")
    if executable is None and found:
        executable = Path(found)
    if sockets and executable is None:
        raise RuntimeError("herdr is not installed on this host")

    def observe(path):
        name = "default" if path == base / "herdr.sock" else path.parent.name
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env["HERDR_SOCKET_PATH"] = str(path)
        try:
            output = subprocess.run([str(executable), "api", "snapshot"], env=env,
                                    capture_output=True, text=True, timeout=3, check=True)
            if len(output.stdout) > 1024 * 1024:
                raise ValueError("Herdr snapshot exceeds limit")
            state = json.loads(output.stdout)["result"]["snapshot"]
            items = inventory_items(state, str(path), name)
            for item in items:
                item["herdrBin"] = str(executable)
            incomplete = len(items) != len(state.get("panes", []))
            return items, ({"herdrSession": name, "error": "Some panes could not be identified; inventory is incomplete"} if incomplete else None)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return [], {"herdrSession": name, "error": "Herdr session could not be inspected"}

    terminals, errors = [], discovery_errors
    with ThreadPoolExecutor(max_workers=4) as pool:
        for items, error in pool.map(observe, sockets):
            terminals.extend(items)
            if error:
                errors.append(error)
    roots = [("default", home / ".hermes")]
    roots.extend((p.name, p) for p in sorted((home / ".hermes/profiles").glob("*"))[:32]
                 if p.is_dir() and SESSION_ID.fullmatch(p.name))
    for profile, root in roots:
        try:
            entries = json.loads((root / "runtime/active_sessions.json").read_text()).get("entries", [])
            enrich_registry(terminals, entries, process_environment, process_started_at, profile)
        except (OSError, ValueError, KeyError):
            pass
    restrict_guarded_inventory(terminals, home)
    return {"ok": True, "terminals": terminals, "errors": errors, "complete": not errors}


def main():
    if sys.argv[1:] == ["--inventory"]:
        print(json.dumps(read_inventory()))
        return
    if len(sys.argv) != 2 or not SESSION_ID.fullmatch(sys.argv[1]):
        raise ValueError("invalid stored session identity")
    home = Path.home()
    registry = home / ".hermes/runtime/active_sessions.json"
    entries = json.loads(registry.read_text()).get("entries", []) if registry.exists() else []
    programs = [home / ".local/bin/herdr", Path("/usr/bin/herdr"),
                Path("/usr/local/bin/herdr"), home / ".cargo/bin/herdr"]
    executable = next((path for path in programs if path.is_file() and os.access(path, os.X_OK)), None)
    if executable is None:
        raise RuntimeError("herdr is not installed on this peer")

    def snapshot(socket):
        clean_env = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_")}
        clean_env["HERDR_SOCKET_PATH"] = socket
        completed = subprocess.run([str(executable), "api", "snapshot"], env=clean_env,
                                   capture_output=True, text=True, timeout=6, check=True)
        return json.loads(completed.stdout)["result"]["snapshot"]

    match = find_live_pane(sys.argv[1], entries, process_environment, snapshot, str(home))
    if match is None:
        # Lease registry can drop a live CLI (e.g. provider-error cleanup); a
        # resumed process still names the session in its argv.
        match = find_resumed_pane(sys.argv[1], iter_process_argv, process_environment,
                                  snapshot, str(home))
    if match is None:
        print(json.dumps({"ok": False, "error": "Remote Hermes session is not active in herdr"}))
    else:
        print(json.dumps({"ok": True, **match, "herdrBin": str(executable)}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:200]}))
        sys.exit(1)
