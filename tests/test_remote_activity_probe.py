import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "bin" / "ares_remote_activity.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("ares_remote_activity", MODULE)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    return probe


class RemoteActivityProbeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        hermes = self.home / ".hermes"
        (hermes / "runtime").mkdir(parents=True)
        (hermes / "runtime/active_sessions.json").write_text(json.dumps({"entries": [
            {"session_id": "session-a", "surface": "cli", "pid": 4321},
        ]}))
        with sqlite3.connect(hermes / "state.db") as db:
            db.execute("CREATE TABLE async_delegations (origin_session TEXT, origin_session_id TEXT, state TEXT, owner_pid INTEGER, owner_started_at INTEGER)")
            db.executemany("INSERT INTO async_delegations VALUES (?,?,?,?,?)", [
                ("session-a", "", "running", 4321, 12345),
                ("session-a", "", "finalizing", 4321, 12345),
                ("session-a", "", "completed", 4321, 12345),
                ("session-a", "", "running", 9999, 12345),
                ("session-b", "session-b", "running", 4321, 12345),
            ])

    def test_only_live_owner_units_join_the_exact_registry_session(self):
        probe = load_probe()
        result = probe.read_activity(self.home, lambda pid: 12346 if pid == 4321 else None)
        self.assertEqual(result, [{"profile": "default", "sessionId": "session-a", "runningDelegations": 2}])

    def test_stale_pid_and_ambiguous_registry_identity_are_rejected(self):
        probe = load_probe()
        self.assertEqual(probe.read_activity(self.home, lambda pid: 14000), [])
        registry = self.home / ".hermes/runtime/active_sessions.json"
        entry = {"session_id": "session-a", "surface": "cli", "pid": 4321}
        registry.write_text(json.dumps({"entries": [entry, entry]}))
        self.assertEqual(probe.read_activity(self.home, lambda pid: 12345), [])


if __name__ == "__main__":
    unittest.main()
