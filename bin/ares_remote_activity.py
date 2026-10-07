#!/usr/bin/env python3
"""Report only live delegation-unit counts from the owning Hermes host.

Sent over SSH stdin; never reads or returns prompts, task payloads, or transcripts.
"""

import json
import re
import sqlite3
from pathlib import Path

IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def process_start(pid):
    try:
        return int((Path("/proc") / str(pid) / "stat").read_text().split()[21])
    except (OSError, IndexError, ValueError):
        return None


def read_activity(home, fingerprint=process_start):
    hermes = Path(home) / ".hermes"
    registry_path = hermes / "runtime/active_sessions.json"
    if not registry_path.is_file():
        return []
    entries = json.loads(registry_path.read_text()).get("entries", [])
    owners = {}
    for entry in entries:
        session = entry.get("session_id", "")
        profile = entry.get("profile") or "default"
        pid = entry.get("pid")
        if (entry.get("surface") != "cli" or not isinstance(session, str)
                or not IDENTITY.fullmatch(session) or not isinstance(profile, str)
                or not PROFILE.fullmatch(profile) or type(pid) is not int or pid <= 0):
            continue
        owners.setdefault((profile, session), []).append(pid)
    databases = [("default", hermes / "state.db")]
    profiles_dir = hermes / "profiles"
    if profiles_dir.is_dir():
        databases.extend((profile.name, profile / "state.db") for profile in profiles_dir.iterdir()
                         if PROFILE.fullmatch(profile.name))
    reports = []
    for profile, path in databases[:64]:
        if not path.is_file():
            continue
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as db:
                records = db.execute(
                    "SELECT origin_session,origin_session_id,owner_pid,owner_started_at "
                    "FROM async_delegations WHERE state IN ('running','finalizing') LIMIT 512"
                ).fetchall()
        except sqlite3.Error:
            continue
        counts = {}
        for origin, explicit, pid, started in records:
            session = explicit or origin
            if not isinstance(session, str) or not IDENTITY.fullmatch(session):
                continue
            candidates = owners.get((profile, session), [])
            if len(candidates) != 1 or candidates[0] != pid or not started:
                continue
            current = fingerprint(pid)
            if current is None or abs(current - int(started)) > 200:
                continue
            counts[session] = counts.get(session, 0) + 1
        reports.extend({"profile": profile, "sessionId": session,
                        "runningDelegations": units}
                       for session, units in sorted(counts.items()))
    return reports


def main():
    try:
        print(json.dumps({"ok": True, "sessions": read_activity(Path.home())}))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, sqlite3.Error):
        print(json.dumps({"ok": False, "sessions": []}))


if __name__ == "__main__":
    main()
