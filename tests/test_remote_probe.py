"""Remote room probe: resumed-CLI fallback when the lease registry has no entry."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "bin" / "ares_remote_probe.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("ares_remote_probe", MODULE)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    return probe


SID = "20260925_001441_887074"
PANE, TAB = "w1:p1", "w1:t1"
SOCKET = ".config/herdr/herdr.sock"


def snapshot_ok(pane=PANE, tab=TAB, claimed=None):
    agent = {"agent": "hermes", "pane_id": pane, "tab_id": tab}
    if claimed is not None:
        agent["agent_session"] = {"value": claimed}
    return {"agents": [agent], "panes": [{"pane_id": pane, "tab_id": tab}],
            "tabs": [{"tab_id": tab}]}


class ResumedPaneFallbackTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        (self.home / ".config/herdr").mkdir(parents=True)
        self.probe = load_probe()

    def find(self, processes, environments, snapshot=None):
        snapshot = snapshot if snapshot is not None else snapshot_ok()
        return self.probe.find_resumed_pane(
            SID,
            lambda: iter(processes),
            lambda pid: environments.get(pid, {}),
            lambda socket: snapshot,
            str(self.home),
        )

    def test_resumed_process_with_valid_herdr_env_matches(self):
        match = self.find(
            [(555, ["python", "hermes", "--resume", SID])],
            {555: {"HERDR_PANE_ID": PANE, "HERDR_TAB_ID": TAB, "HERDR_SOCKET_PATH": str(self.home / SOCKET)}},
        )
        self.assertEqual(match, {"paneId": PANE, "tabId": TAB, "socketPath": str(self.home / SOCKET)})

    def test_resume_token_must_be_an_exact_argv_pair(self):
        env = {7: {"HERDR_PANE_ID": PANE, "HERDR_TAB_ID": TAB, "HERDR_SOCKET_PATH": str(self.home / SOCKET)}}
        self.assertIsNone(self.find([(7, ["hermes", "--resumeX", SID])], env))
        self.assertIsNone(self.find([(7, ["hermes", "--resume", SID + "0"])], env))
        self.assertIsNone(self.find([(7, ["hermes", "--resume"])], env))
        self.assertIsNotNone(self.find([(7, ["hermes", "--resume", SID, "--verbose"])], env))

    def test_ambiguous_resumed_processes_are_rejected(self):
        env = {8: {"HERDR_PANE_ID": PANE, "HERDR_TAB_ID": TAB, "HERDR_SOCKET_PATH": str(self.home / SOCKET)}}
        with self.assertRaises(ValueError):
            self.find([(8, ["hermes", "--resume", SID]), (9, ["hermes", "--resume", SID])], env)

    def test_resumed_process_without_herdr_env_is_ignored(self):
        self.assertIsNone(self.find([(555, ["hermes", "--resume", SID])], {555: {}}))

    def test_resumed_process_must_exist_in_snapshot(self):
        env = {555: {"HERDR_PANE_ID": PANE, "HERDR_TAB_ID": TAB, "HERDR_SOCKET_PATH": str(self.home / SOCKET)}}
        self.assertIsNone(self.find(
            [(555, ["hermes", "--resume", SID])], env,
            snapshot=snapshot_ok(pane="w9:p9")))

    def test_registry_match_still_short_circuits(self):
        match = self.probe.find_live_pane(
            SID,
            [{"session_id": SID, "surface": "cli", "pid": 4321}],
            lambda pid: {"HERDR_PANE_ID": PANE, "HERDR_TAB_ID": TAB,
                         "HERDR_SOCKET_PATH": str(self.home / SOCKET)},
            lambda socket: snapshot_ok(),
            str(self.home),
        )
        self.assertEqual(match["paneId"], PANE)


if __name__ == "__main__":
    unittest.main()
