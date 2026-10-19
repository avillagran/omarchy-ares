#!/usr/bin/env python3
"""Owner-host explicit opening through the native Hermes CLI; never send a prompt.

Import open_session(request), including from an SSH in-memory bootstrap, or pass
one JSON request on stdin. Routed sessions use a foreground native-lease parent;
unsupported hosts fail closed before creating any terminal.
"""
import fcntl
from contextlib import closing, contextmanager
import json
import os
import hashlib
import math
import shutil
import secrets
import shlex
import subprocess
import sys
import time
import threading
import unicodedata
from pathlib import Path

import ares_remote_probe as probe


def validate_request(request):
    if not isinstance(request, dict) or any(
            not isinstance(request.get(key), str) or not probe.SESSION_ID.fullmatch(request[key])
            for key in ("roomId", "profile", "sessionId")) or request["sessionId"] == "latest":
        raise ValueError("Unsafe room, profile, or exact stored session identifier")


def existing_terminal(state, request):
    if state.get("ok") is not True or state.get("complete") is not True or state.get("errors"):
        raise RuntimeError("Terminal inventory is incomplete")
    matches = [item for item in state["terminals"]
               if item.get("agentKind") == "hermes" and item.get("profile") == request["profile"]
               and item.get("sessionId") == request["sessionId"]]
    if len(matches) > 1:
        raise RuntimeError("Ambiguous Hermes terminal ownership")
    if matches:
        item = matches[0]
        if item.get("identitySource") not in ("herdr", "registry") or item.get("status") not in ("idle", "working"):
            raise RuntimeError("Hermes terminal identity or readiness is unknown")
        return item
    return None


def profile_home(request):
    root = Path.home() / ".hermes"
    return root if request["profile"] == "default" else root / "profiles" / request["profile"]


def stored_session(request):
    import sqlite3
    root = profile_home(request)
    try:
        with closing(sqlite3.connect((root / "state.db").as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT id, source, profile_name, session_key, cwd, parent_session_id, model_config FROM sessions WHERE id = ?",
                             (request["sessionId"],)).fetchone()
            if row is None:
                raise RuntimeError("The exact stored session does not exist; title fallback is forbidden")
            row = dict(row)
            profile = row["profile_name"] or "default"
            if profile != request["profile"]:
                raise RuntimeError("Stored session profile conflicts with request")
            for child_source, raw_config in db.execute(
                    "SELECT source, model_config FROM sessions WHERE parent_session_id = ?", (row["id"],)):
                try:
                    config = json.loads(raw_config) if raw_config else {}
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Stored session descendant identity is corrupt") from exc
                if not isinstance(config, dict):
                    raise RuntimeError("Stored session descendant identity is unprovable")
                # Hermes excludes delegated/branched/tool children when resolving an exact parent.
                if (child_source == "tool" or
                        (child_source == "subagent" and config.get("_delegate_from") == row["id"]) or
                        any(config.get(flag) == row["id"] for flag in ("_branched_from", "_reset_from"))):
                    continue
                raise RuntimeError("Stored session has unsupported descendants; resume could redirect")
            if not db.execute("SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (row["id"],)).fetchone():
                raise RuntimeError("Stored session has no resumable history")
            if row["source"] == "subagent":
                try:
                    config = json.loads(row["model_config"]) if row["model_config"] else {}
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Subagent stored session has invalid delegate identity") from exc
                parent = row["parent_session_id"]
                if (not isinstance(config, dict) or not isinstance(parent, str)
                        or not probe.SESSION_ID.fullmatch(parent)
                        or config.get("_delegate_from") != parent):
                    raise RuntimeError("Subagent stored session has no verified delegate parent")
            elif row["source"] not in ("cli", "telegram", "ares"):
                raise RuntimeError("Stored session source is unsupported for guarded opening")
            if row["source"] == "telegram" and (not isinstance(row["session_key"], str)
                    or not row["session_key"].strip() or row["session_key"] == row["id"]):
                raise RuntimeError("Telegram routing identity is unavailable")
            return row
    except sqlite3.Error as exc:
        raise RuntimeError("Stored session database cannot prove an exact safe resume") from exc


def live_entries(request):
    path = profile_home(request) / "runtime/active_sessions.json"
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RuntimeError("Active-session registry is unreadable or corrupt") from exc
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("Active-session registry has invalid shape")
    live, seen = [], set()
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("lease_id"), str)
                or not entry["lease_id"] or entry["lease_id"] in seen
                or not isinstance(entry.get("session_id"), str) or not entry["session_id"]
                or type(entry.get("pid")) is not int or entry["pid"] <= 0
                or type(entry.get("process_start_time")) not in (float, int)
                or not math.isfinite(entry["process_start_time"])):
            raise RuntimeError("Active-session registry contains an unprovable owner")
        seen.add(entry["lease_id"])
        try:
            actual = probe.process_started_at(entry["pid"])
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise RuntimeError("Active-session owner liveness is unknown") from exc
        if actual is None:
            raise RuntimeError("Active-session owner liveness is unknown")
        if abs(actual - entry["process_start_time"]) < 0.001:
            live.append(entry)
    return live


def process_parent(pid):
    return int((Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])


def verified_launch_ancestor(pid, entries, state, request):
    """A wrapper need not lease twice; prove ancestry AND the same observed pane."""
    try:
        env = probe.process_environment(pid)
        fields = (("HERDR_PANE_ID", "paneId"), ("HERDR_TAB_ID", "tabId"), ("HERDR_SOCKET_PATH", "socketPath"))
        matches = [t for t in state["terminals"] if t.get("agentKind") == "hermes"
                   and t.get("identitySource") in ("herdr", "registry")
                   and t.get("sessionId") and t["sessionId"] != request["sessionId"]
                   and all(env.get(k) and env[k] == t.get(f) for k, f in fields)]
        if len(matches) != 1:
            return False
        target = matches[0]
        for entry in entries:
            if entry.get("surface") != "cli" or entry["session_id"] != target["sessionId"]:
                continue
            child_env = probe.process_environment(entry["pid"])
            if not all(child_env.get(k) == env[k] for k, _ in fields):
                continue
            ancestor, seen = entry["pid"], set()
            for _ in range(64):
                if ancestor == pid:
                    return True
                if ancestor <= 1 or ancestor in seen:
                    break
                seen.add(ancestor)
                ancestor = process_parent(ancestor)
    except (OSError, ValueError, KeyError):
        pass
    return False


def preflight(request, state, row, ignored_lease=None):
    if existing_terminal(state, request) is not None:
        raise RuntimeError("An existing terminal appeared during recheck; reuse it instead")
    for item in state["terminals"]:
        if item.get("identitySource") == "conflict" or (item.get("agentKind") == "hermes"
                and (not item.get("sessionId") or (not item.get("profile")
                    and not (item.get("identitySource") in ("herdr", "registry")
                             and item["sessionId"] != request["sessionId"])))):
            raise RuntimeError("A Hermes terminal has unknown or conflicting ownership")
    keys = {request["sessionId"], row.get("session_key")}
    entries = live_entries(request)
    if any(e["session_id"] in keys and e["lease_id"] != ignored_lease for e in entries):
        raise RuntimeError("Stored session or gateway route already has a live owner")
    for pid, argv in probe.iter_process_argv():
        if (any(token in ("--resume", "-r") and argv[i + 1] == request["sessionId"]
                for i, token in enumerate(argv[:-1])) or "--resume=" + request["sessionId"] in argv):
            raise RuntimeError("A resumed process exists without a verified terminal owner")
        interactive = any(Path(token).name in ("hermes", "cli.py") or token == "hermes_cli.main" for token in argv[:3])
        other_profile = any(token in ("--profile", "-p") and argv[i + 1] != request["profile"]
                            for i, token in enumerate(argv[:-1]))
        executable_index = next((i for i, token in enumerate(argv[:3])
                                 if Path(token).name in ("hermes", "cli.py") or token == "hermes_cli.main"), None)
        command_args = argv[executable_index + 1:] if executable_index is not None else []
        if len(command_args) >= 2 and command_args[0] in ("--profile", "-p"):
            command_args = command_args[2:]
        administrative = bool(command_args) and command_args[0] in (
            "gateway", "serve", "sessions", "profile", "config", "doctor", "--help", "--version")
        if (interactive and not other_profile and not administrative
                and not any(e["pid"] == pid for e in entries)
                and not verified_launch_ancestor(pid, entries, state, request)):
            raise RuntimeError("A Hermes process has an unknown owner; refusing a second writer")


def room_identity(request):
    digest = hashlib.sha256(request["roomId"].encode()).hexdigest()
    title = request.get("title") or "room"
    if not isinstance(title, str):
        title = "room"
    clean = " ".join("".join(" " if unicodedata.category(c) == "Cc" else c for c in title).split()) or "room"
    suffix = " [" + digest[:12] + "]"
    return "ares-" + digest[:24], clean[:32 - len(suffix)].rstrip() + suffix, suffix


def socket_path():
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not config.is_absolute():
        config = Path.home() / ".config"
    return config / "herdr/sessions/ares/herdr.sock"


def herdr_program():
    for p in [Path.home() / ".local/bin/herdr", Path.home() / ".cargo/bin/herdr",
              Path("/usr/bin/herdr"), Path("/usr/local/bin/herdr")]:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    found = shutil.which("herdr")
    if not found:
        raise RuntimeError("Herdr is unavailable on the owning host")
    return found


def clean_environment():
    return {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}


def herdr(args):
    env = clean_environment()
    env["HERDR_SOCKET_PATH"] = str(socket_path())
    try:
        output = subprocess.run([herdr_program(), *args], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=35 if args[:2] == ["agent", "start"] else 4)
        if output.returncode or len(output.stdout) > 1024 * 1024:
            raise RuntimeError("Herdr request failed")
        # Native pane lifecycle commands acknowledge with no JSON; identity is read back below.
        if (args[:2] in (["pane", "run"], ["pane", "report-agent"],
                         ["pane", "report-agent-session"], ["pane", "release-agent"])
                and not output.stdout.strip()):
            return {}
        value = json.loads(output.stdout)
        if not isinstance(value, dict) or value.get("error") or value.get("ok") is False:
            raise RuntimeError("Herdr request was rejected")
        return value
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Herdr request failed or timed out; output was withheld") from exc


def snapshot():
    value = herdr(["api", "snapshot"])["result"]["snapshot"]
    if any(not isinstance(value.get(k), list) for k in ("tabs", "panes", "agents", "workspaces")):
        raise RuntimeError("Herdr snapshot is incomplete")
    return value


def ensure_server():
    path = socket_path()
    if path.exists():
        if not path.is_socket():
            raise RuntimeError("Ares socket path is not a socket")
        snapshot()  # An existing but unresponsive server must never be replaced.
        return
    marker = private_root() / "server-start.json"
    if marker.exists():
        raise RuntimeError("Earlier Ares server startup is uncertain; automatic retry disabled")
    save_job(marker, {"attempted": True})
    child = subprocess.Popen([herdr_program(), "--session", "ares", "server"], env=clean_environment(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    threading.Thread(target=child.wait, daemon=True).start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.is_socket():
            snapshot()
            return
        time.sleep(0.1)
    raise RuntimeError("Ares server startup is uncertain; do not retry automatically")


def room_pane(request, *, fresh=False, environment=None):
    name, label, suffix = room_identity(request)
    state = snapshot()
    named = [a for a in state["agents"] if a.get("name") == name]
    tabs = [t for t in state["tabs"] if str(t.get("label") or "").endswith(suffix)
            or any(a.get("tab_id") == t.get("tab_id") for a in named)]
    if len(tabs) > 1 or len(named) > 1:
        raise RuntimeError("Room tab identity is ambiguous")
    if fresh and tabs:
        raise RuntimeError("Routed resume requires a brand-new Ares pane")
    env_args = [arg for key, value in (environment or {}).items() for arg in ("--env", key + "=" + value)]
    if not tabs:
        if state["workspaces"]:
            herdr(["tab", "create", "--workspace", state["workspaces"][0]["workspace_id"], "--label", label, "--no-focus", *env_args])
        else:
            created = herdr(["workspace", "create", "--label", "Ares", "--no-focus", *env_args])
            tab = created["result"]["tab"]["tab_id"]
            if not probe.TAB_ID.fullmatch(tab):
                raise RuntimeError("Herdr returned an invalid tab")
            herdr(["tab", "rename", tab, label])
        state = snapshot()  # Read back the exact target, not the create response alone.
        tabs = [t for t in state["tabs"] if str(t.get("label") or "").endswith(suffix)]
    if len(tabs) != 1:
        raise RuntimeError("Room tab creation is uncertain")
    tab = tabs[0]["tab_id"]
    panes = [p for p in state["panes"] if p.get("tab_id") == tab]
    if len(panes) != 1 or any(a.get("tab_id") == tab for a in state["agents"]):
        raise RuntimeError("Room tab contains another agent or ambiguous panes")
    pane = panes[0]
    if (pane.get("agent") is not None or pane.get("agent_session") is not None
            or pane.get("agent_status") != "unknown"):
        raise RuntimeError("Room pane is occupied or restoring")
    if not (probe.PANE_ID.fullmatch(pane.get("pane_id", "")) and probe.TAB_ID.fullmatch(tab)
            and probe.SESSION_ID.fullmatch(pane.get("terminal_id", ""))):
        raise RuntimeError("Room pane identity is incomplete")
    # Herdr agent.start atomically checks that this is an interactive shell prompt.
    return pane


def verified_owner(request, item):
    matches = [e for e in live_entries(request) if e["session_id"] == request["sessionId"] and e.get("surface") == "cli"]
    if len(matches) != 1:
        raise RuntimeError("Exact resumed CLI lease is missing or ambiguous")
    owner = matches[0]
    env = probe.process_environment(owner["pid"])
    if any(env.get(key) != item.get(field) for key, field in (
            ("HERDR_PANE_ID", "paneId"), ("HERDR_TAB_ID", "tabId"), ("HERDR_SOCKET_PATH", "socketPath"))):
        raise RuntimeError("Resumed CLI lease does not own the expected terminal")
    return owner


def verified_ready(request, item, owner, launch, source):
    """The child's post-restore acknowledgment must name this live leased pane."""
    path = launch.get("ready")
    if not path:
        raise RuntimeError("Exact history acknowledgment is missing")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            stat = os.fstat(stream.fileno())
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077 or not stat.st_mode & 0o100000:
                raise RuntimeError("Exact history acknowledgment is not private")
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise RuntimeError("Exact history acknowledgment is oversized")
        ack = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("Exact history acknowledgment is missing or invalid") from exc
    required = {"protocol": "hermes-exact-resume-ready-v1", "nonce": launch.get("nonce"),
                "sessionId": request["sessionId"], "source": source,
                "pid": owner["pid"], "leaseId": owner["lease_id"],
                "paneId": item["paneId"], "tabId": item["tabId"]}
    if (not isinstance(ack, dict) or not required["nonce"]
            or any(type(ack.get(key)) is not type(value) or ack[key] != value for key, value in required.items())
            or type(ack.get("messageCount")) is not int or ack["messageCount"] <= 0):
        raise RuntimeError("Exact history acknowledgment does not match the live owner")
    return True


def guarded_existing(request, match):
    """A pane from an attempted Ares launch must carry its own verified receipt."""
    if match.get("herdrSession") != "ares":
        return
    key = hashlib.sha256((request["profile"] + "\0" + request["sessionId"]).encode()).hexdigest()
    job = private_root() / (key + ".json")
    if not job.exists():
        return  # Older, independently verified pane; no launch journal owns it.
    try:
        data = json.loads(job.read_text())
        if (data.get("request") != request or not data.get("attempted")
                or any(data.get(k) != match.get(v) for k, v in
                       (("paneId", "paneId"), ("tabId", "tabId"), ("terminalId", "terminalId")))):
            raise RuntimeError("Guarded historical launch identity changed")
        row = stored_session(request)
        if data.get("source") != row["source"]:
            raise RuntimeError("Guarded historical session source changed")
        verified_ready(request, match, verified_owner(request, match), data, row["source"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Guarded history acknowledgment is unavailable") from exc


def private_root():
    root = Path.home() / ".local/state/ares/terminal-open"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Ares opener state directory must be owner-private")
    return root


@contextmanager
def host_lock(root):
    fd = os.open(root / "host.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_job(path, payload):
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def failure(state, message, code=None):
    error = {"error": message}
    if code:
        error["code"] = code
    return {**state, "ok": False, "error": message, "errors": [*state.get("errors", []), error]}


def prepare_resume():
    """Stage private immutable templates; SSH callers inject TERMINAL_SOURCES.

    TERMINAL_SOURCES is a trusted bootstrap global, NEVER a request field. This
    keeps stdin-loaded modules independent of __file__ or remote installation.
    """
    import tempfile
    if sys.platform != "linux":
        raise RuntimeError("Parent-death fencing is unavailable")
    isolated = Path.home() / ".local/share/omarchy-ares/hermes-exact/bin/hermes"
    candidates = ([str(isolated)] if isolated.exists() or isolated.is_symlink() else [
        shutil.which("hermes"),
        str(Path.home() / ".hermes/hermes-agent/venv/bin/hermes"),
        str(Path.home() / ".local/bin/hermes")])
    selected = None
    for candidate in dict.fromkeys(filter(None, candidates)):
        binary = Path(candidate).absolute()
        python = binary.parent / "python"
        if not (binary.is_file() and os.access(binary, os.X_OK)
                and python.is_file() and os.access(python, os.X_OK)):
            continue
        help_result = subprocess.run([str(binary), "--help"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=8)
        if (help_result.returncode or "--exact-resume" not in help_result.stdout
                or "--exact-resume-ready-fd" not in help_result.stdout):
            continue
        checked = subprocess.run([str(python), "-c",
            "import inspect; from hermes_cli.active_sessions import try_acquire_active_session as a; "
            "assert {'registry_home','track_liveness'} <= set(inspect.signature(a).parameters)"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        if checked.returncode == 0:
            selected = binary, python
            break
    if selected is None:
        raise RuntimeError("Native Hermes lacks fail-closed exact resume support")
    binary, python = selected
    for args, required in ((["pane", "run", "--help"], "<PANE_ID>"),
                           (["tab", "create", "--help"], "--env"),
                           (["workspace", "create", "--help"], "--env")):
        checked = subprocess.run([herdr_program(), *args], env=clean_environment(),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=4)
        if checked.returncode or required not in checked.stdout:
            raise RuntimeError("Herdr foreground launch API is unavailable")
    names = ("ares_terminal_open.py", "ares_remote_probe.py", "ares_terminal_resume.py")
    sources = globals().get("TERMINAL_SOURCES")
    if sources is None:
        location = globals().get("__file__")
        if not location:
            raise RuntimeError("Trusted foreground templates were not embedded")
        sources = {name: (Path(location).parent / name).read_text() for name in names}
    if not isinstance(sources, dict) or any(not isinstance(sources.get(n), str) for n in names):
        raise RuntimeError("Foreground templates are incomplete")
    stage = Path(tempfile.mkdtemp(prefix="resume-", dir=private_root()))
    for name in names:
        compile(sources[name], name, "exec")
        fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(sources[name])
    return {"python": str(python), "hermes": str(binary), "nonce": secrets.token_hex(16),
            "launcher": str(stage / "ares_terminal_resume.py"), "payload": str(stage / "request.json"),
            "ready": str(stage / "ready.json")}


def save_resume_payload(launch, request, row, pane):
    save_job(Path(launch["payload"]), {"request": request, "row": row, "pane": pane,
        "home": str(profile_home(request)), "hermes": launch["hermes"],
        "ready": launch["ready"], "nonce": launch["nonce"]})


def open_session(request: dict) -> dict:
    state = {"ok": False, "terminals": [], "errors": [], "complete": False}
    try:
        validate_request(request)
        with host_lock(private_root()):
            state = probe.read_inventory()
            live_entries(request)
            match = existing_terminal(state, request)
            if match:
                guarded_existing(request, match)
                return {**state, "openedTerminalId": match["terminalId"]}
            row = stored_session(request)
            preflight(request, state, row)
            route = row.get("session_key")
            route = route if route and route != row["id"] else None
            try:
                launch = prepare_resume()
            except Exception:
                return failure(state, "Native fail-closed exact resume is unavailable; no terminal was started.",
                               "EXACT_RESUME_UNAVAILABLE")
            key = hashlib.sha256((request["profile"] + "\0" + request["sessionId"]).encode()).hexdigest()
            job = private_root() / (key + ".json")
            if job.exists():
                raise RuntimeError("An earlier open may still own this session; automatic retry disabled")
            save_job(job, {"request": request, "attempted": True})
            ensure_server()
            pane = room_pane(request, fresh=True, environment={"HERMES_HOME": str(profile_home(request))})
            # Recheck immediately before dispatch, while still holding the host lock.
            if stored_session(request) != row:
                raise RuntimeError("Stored identity changed before launch")
            preflight(request, probe.read_inventory(), row)
            save_resume_payload(launch, request, row, pane)
            save_job(job, {"request": request, "attempted": True, "ready": launch["ready"],
                           "nonce": launch["nonce"], "source": row["source"],
                           "paneId": pane["pane_id"], "tabId": pane["tab_id"],
                           "terminalId": pane["terminal_id"]})
            herdr(["pane", "run", pane["pane_id"], *[shlex.quote(launch[k]) for k in ("python", "launcher", "payload")]])
            state = probe.read_inventory()
            match = existing_terminal(state, request)
            deadline = time.monotonic() + 70
            ready_path = launch.get("ready")
            if not ready_path:
                raise RuntimeError("Child exact-history acknowledgment path is unavailable")
            while (not match or not Path(ready_path).is_file()) and time.monotonic() < deadline:
                time.sleep(0.2)
                state = probe.read_inventory()
                match = existing_terminal(state, request)
            if (not match or match.get("socketPath") != str(socket_path()) or match.get("herdrSession") != "ares"
                    or (match["terminalId"], match["paneId"], match["tabId"]) != (
                        pane["terminal_id"], pane["pane_id"], pane["tab_id"])):
                raise RuntimeError("Started terminal did not resume the exact stored identity; automatic retry disabled")
            owner = verified_owner(request, match)
            verified_ready(request, match, owner, launch, row["source"])
            result = {**state, "openedTerminalId": match["terminalId"]}
            save_job(job, {"request": request, "attempted": True, "ready": launch["ready"],
                           "nonce": launch["nonce"], "source": row["source"],
                           "paneId": pane["pane_id"], "tabId": pane["tab_id"],
                           "terminalId": match["terminalId"], "owner": owner})
            return result
    except Exception as exc:
        message = str(exc) if type(exc) in (RuntimeError, ValueError) else "Opening failed; ownership could not be verified"
        return failure(state, message)


if __name__ == "__main__":
    try:
        result = open_session(json.loads(sys.stdin.read(65537)))
    except Exception:
        result = failure({"terminals": [], "errors": [], "complete": False}, "Invalid opener request")
    print(json.dumps(result))
