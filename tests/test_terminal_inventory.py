"""Host-independent Herdr discovery does not depend on a Hermes lease."""
import importlib.util
import unittest
import tempfile
import subprocess
import sys
import os
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "inventory_probe", Path(__file__).resolve().parents[1] / "bin/ares_remote_probe.py")
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def snapshot(kind="claude", session="chat-a"):
    return {
        "workspaces": [{"workspace_id": "w1", "label": "Project"}],
        "tabs": [{"tab_id": "w1:t1", "label": "Chat"}],
        "panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1",
                   "terminal_id": "term_123", "terminal_title_stripped": "Chat"}],
        "agents": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "terminal_id": "term_123",
                    "agent": kind, "agent_status": "idle",
                    "agent_session": {"agent": kind, "kind": "id", "source": "herdr:" + kind,
                                      "value": session}}],
    }


class InventoryTest(unittest.TestCase):
    def test_discovery_limits_are_reported_as_incomplete(self):
        from unittest.mock import patch as mock_patch
        from types import SimpleNamespace
        import json
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / "cfg/herdr/sessions"
            for index in range(17):
                socket = base / ("s" + str(index)) / "herdr.sock"
                socket.parent.mkdir(parents=True)
                socket.touch()
            with mock_patch.object(probe.Path, "home", return_value=home), mock_patch.dict(os.environ, {"XDG_CONFIG_HOME":str(home / "cfg")}), mock_patch.object(probe.Path,"is_socket",return_value=True), mock_patch.object(probe.subprocess,"run",return_value=SimpleNamespace(stdout=json.dumps({"result":{"snapshot":snapshot()}}))):
                inventory = probe.read_inventory()
            self.assertFalse(inventory["complete"])
            self.assertTrue(inventory["errors"])
            self.assertTrue(inventory["terminals"])

    def test_profile_is_unknown_until_observed_and_conflicts_never_choose_first(self):
        items = probe.inventory_items(snapshot("hermes", "chat-a"), "/home/user/.config/herdr/herdr.sock", "default")
        self.assertEqual(items[0]["profile"], "")
        entry = {"pid": 42, "session_id": "chat-a", "surface": "cli", "process_start_time": 100.0}
        env = {"HERDR_PANE_ID": "w1:p1", "HERDR_TAB_ID": "w1:t1", "HERDR_SOCKET_PATH": items[0]["socketPath"]}
        probe.enrich_registry(items, [entry], lambda pid: env, lambda pid: 100.0, "coder")
        self.assertEqual(items[0]["profile"], "coder")
        probe.enrich_registry(items, [dict(entry, session_id="chat-b")], lambda pid: env, lambda pid: 100.0, "coder")
        self.assertEqual(items[0]["sessionId"], "")
        self.assertEqual(items[0]["identitySource"], "conflict")

    def test_registry_only_enriches_a_verified_live_process_and_matching_pane(self):
        items = probe.inventory_items(snapshot("hermes", ""), "/home/user/.config/herdr/herdr.sock", "default")
        entry = {"pid": 42, "session_id": "chat-a", "surface": "cli", "process_start_time": 100.0}
        env = {"HERDR_PANE_ID": "w1:p1", "HERDR_TAB_ID": "w1:t1", "HERDR_SOCKET_PATH": items[0]["socketPath"]}
        probe.enrich_registry(items, [entry], lambda pid: env, lambda pid: 101.0, "default")
        self.assertEqual(items[0]["sessionId"], "")
        probe.enrich_registry(items, [entry], lambda pid: env, lambda pid: 100.0, "default")
        self.assertEqual(items[0]["sessionId"], "chat-a")
        self.assertEqual(items[0]["identitySource"], "registry")

    def test_invalid_agent_claim_is_not_a_session_identity(self):
        state = snapshot("hermes")
        state["agents"][0]["agent_session"]["agent"] = "claude"
        items = probe.inventory_items(state, "/home/user/.config/herdr/herdr.sock", "default")
        self.assertEqual(items[0]["sessionId"], "")

    def test_inventory_mode_returns_empty_without_starting_herdr(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
            env.update(HOME=directory, XDG_CONFIG_HOME=directory + "/.config")
            result = subprocess.run([sys.executable, str(SPEC.origin), "--inventory"],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            import json
            data = json.loads(result.stdout)
            self.assertEqual(data["terminals"], [])
            self.assertEqual(data["errors"], [])

    def test_all_agent_kinds_share_the_same_terminal_identity(self):
        for kind in ("hermes", "claude", "kimi"):
            with self.subTest(kind=kind):
                items = probe.inventory_items(snapshot(kind), "/home/user/.config/herdr/herdr.sock", "default")
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["agentKind"], kind)
                self.assertEqual(items[0]["terminalId"], "term_123")
                self.assertEqual(items[0]["paneId"], "w1:p1")
                self.assertEqual(items[0]["sessionId"], "chat-a")
                self.assertEqual(items[0]["workspace"], "Project")


if __name__ == "__main__":
    unittest.main()

class StaleSessionSocketTest(unittest.TestCase):
    """A bound-but-listenerless session socket is a stopped session, not an error."""

    def _home_with_sockets(self, directory, names):
        import socket as sock
        home = Path(directory)
        listeners = []
        for name, live in names:
            path = home / "cfg/herdr/herdr.sock" if name == "default" else home / ("cfg/herdr/sessions/" + name) / "herdr.sock"
            path.parent.mkdir(parents=True, exist_ok=True)
            listener = sock.socket(sock.AF_UNIX, sock.SOCK_STREAM)
            listener.bind(str(path))
            if live:
                listener.listen(1)
                listeners.append(listener)
            else:
                listener.close()  # the socket file remains without a listener
        return home, listeners

    @staticmethod
    def _cli_reply(requested):
        import json as json_module
        from types import SimpleNamespace
        stopped = json_module.dumps({"id": "cli:api:snapshot", "error": {
            "code": "server_not_running",
            "message": "no herdr server is running at the socket"}})
        def run(argv, env=None, **kwargs):
            target = (env or {}).get("HERDR_SOCKET_PATH", "")
            if "/sessions/" in target:
                raise subprocess.CalledProcessError(1, argv, output="", stderr=stopped)
            return SimpleNamespace(stdout=json_module.dumps({"result": {"snapshot": snapshot()}}))
        return run

    def test_stale_session_is_skipped_without_poisoning_complete_inventory(self):
        from unittest.mock import patch as mock_patch
        with tempfile.TemporaryDirectory() as directory:
            home, listeners = self._home_with_sockets(directory, [("default", True), ("ares", False), ("hermes", False)])
            self.addCleanup(lambda: [listener.close() for listener in listeners])
            with mock_patch.object(probe.Path, "home", return_value=home), \
                 mock_patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home / "cfg")}), \
                 mock_patch.object(probe.subprocess, "run", side_effect=self._cli_reply(None)):
                inventory = probe.read_inventory()
            self.assertTrue(inventory["complete"], "dead sessions must not fail the live inventory")
            self.assertEqual([], inventory["errors"])
            self.assertTrue(inventory["terminals"])
            self.assertEqual(sorted(item["herdrSession"] for item in inventory.get("stale", [])),
                             ["ares", "hermes"], "stopped sessions stay visible as informational")

    def test_live_session_with_a_failing_snapshot_still_fails_closed(self):
        from unittest.mock import patch as mock_patch
        with tempfile.TemporaryDirectory() as directory:
            home, listeners = self._home_with_sockets(directory, [("default", True), ("ares", True)])
            self.addCleanup(lambda: [listener.close() for listener in listeners])
            with mock_patch.object(probe.Path, "home", return_value=home), \
                 mock_patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home / "cfg")}), \
                 mock_patch.object(probe.subprocess, "run", side_effect=OSError("snapshot boom")):
                inventory = probe.read_inventory()
            self.assertFalse(inventory["complete"])
            self.assertEqual({item["herdrSession"] for item in inventory["errors"]}, {"default", "ares"})
            self.assertEqual([], inventory.get("stale", []))

    def test_a_live_but_empty_session_is_not_reported_stale(self):
        import json as json_module
        from types import SimpleNamespace
        from unittest.mock import patch as mock_patch
        with tempfile.TemporaryDirectory() as directory:
            home, listeners = self._home_with_sockets(directory, [("default", True)])
            self.addCleanup(lambda: [listener.close() for listener in listeners])
            empty = SimpleNamespace(stdout=json_module.dumps({"result": {"snapshot": {"panes": [], "agents": [], "tabs": [], "workspaces": []}}}))
            with mock_patch.object(probe.Path, "home", return_value=home), \
                 mock_patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home / "cfg")}), \
                 mock_patch.object(probe.subprocess, "run", return_value=empty):
                inventory = probe.read_inventory()
            self.assertTrue(inventory["complete"])
            self.assertEqual([], inventory["terminals"])
            self.assertEqual([], inventory.get("stale", []), "a running session with zero panes is live, not stale")
