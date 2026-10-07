"""Remote terminal identity must come from the owning process, not a title."""

import importlib.util
import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "bin" / "ares_remote_probe.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("ares_remote_probe", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RemoteTerminalProbeTests(unittest.TestCase):
    def test_client_rejects_unsafe_ssh_target_without_connecting(self):
        client = MODULE.with_name("ares-herdr-client")
        result = subprocess.run(
            [str(client), "remote", "wN:p1", "user@host;touch", "/home/user/.local/bin/herdr", "/home/user/.config/herdr/herdr.sock"],
            capture_output=True, text=True, timeout=2,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Invalid remote terminal identity", result.stderr)

    def setUp(self):
        self.probe = load_probe()
        self.entry = {"session_id": "20260921_192605_4360ed", "surface": "cli", "pid": 1339842}
        self.environment = {
            "HERDR_PANE_ID": "wN:p1",
            "HERDR_TAB_ID": "wN:t1",
            "HERDR_SOCKET_PATH": "/home/agent/.config/herdr/herdr.sock",
        }
        self.snapshot = {
            "agents": [{"agent": "hermes", "pane_id": "wN:p1", "tab_id": "wN:t1"}],
            "panes": [{"pane_id": "wN:p1", "tab_id": "wN:t1"}],
            "tabs": [{"tab_id": "wN:t1"}],
        }

    def test_pending_ares_pane_is_not_discoverable_as_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / '.local/state/ares/terminal-open'
            root.mkdir(parents=True)
            root.chmod(0o700)
            sid = self.entry['session_id']
            key = hashlib.sha256(('default\0'+sid).encode()).hexdigest()
            job = root / (key+'.json')
            job.write_text(json.dumps({'request':{'sessionId':sid, 'profile':'default'},
                'attempted':True, 'source':'cli', 'nonce':'a'*32, 'ready':str(root/'missing.json'),
                'paneId':'wN:p1', 'tabId':'wN:t1', 'terminalId':'term_1'}))
            job.chmod(0o600)
            item = {'agentKind':'hermes', 'profile':'default', 'sessionId':sid,
                'herdrSession':'ares', 'identitySource':'registry', 'paneId':'wN:p1',
                'tabId':'wN:t1', 'terminalId':'term_1', 'ownerPid':1339842, 'leaseId':'owned'}
            self.probe.restrict_guarded_inventory([item], home)
            self.assertEqual(item['identitySource'], 'conflict')
            self.assertEqual(item['sessionId'], '')

    def test_ready_ares_pane_preserves_exact_registry_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home/'.local/state/ares/terminal-open'
            root.mkdir(parents=True)
            root.chmod(0o700)
            stage = root/'resume-fixture'
            stage.mkdir(mode=0o700)
            sid = self.entry['session_id']
            item = {'agentKind':'hermes','profile':'default','sessionId':sid,'herdrSession':'ares',
                    'identitySource':'registry','paneId':'wN:p1','tabId':'wN:t1',
                    'terminalId':'term_1','ownerPid':1339842,'leaseId':'owned'}
            marker = {'request':{'sessionId':sid,'profile':'default'},'attempted':True,
                      'source':'cli','nonce':'a'*32,'ready':str(stage/'ready.json'),
                      'paneId':'wN:p1','tabId':'wN:t1','terminalId':'term_1'}
            job = root/(hashlib.sha256(('default\0'+sid).encode()).hexdigest()+'.json')
            job.write_text(json.dumps(marker))
            job.chmod(0o600)
            ack = {'protocol':'hermes-exact-resume-ready-v1','nonce':'a'*32,
                   'sessionId':sid,'source':'cli','pid':1339842,'leaseId':'owned',
                   'messageCount':2,'paneId':'wN:p1','tabId':'wN:t1'}
            receipt = stage/'ready.json'
            receipt.write_text(json.dumps(ack))
            receipt.chmod(0o600)
            self.probe.restrict_guarded_inventory([item], home)
            self.assertEqual(item['identitySource'], 'registry')
            self.assertEqual(item['sessionId'], sid)
            ack['nonce'] = 'b'*32
            receipt.write_text(json.dumps(ack))
            self.probe.restrict_guarded_inventory([item], home)
            self.assertEqual(item['identitySource'], 'conflict')

    def test_reuses_exact_live_remote_session_pane(self):
        match = self.probe.find_live_pane(
            self.entry["session_id"], [self.entry],
            lambda pid: self.environment if pid == 1339842 else {},
            lambda socket: self.snapshot if socket == self.environment["HERDR_SOCKET_PATH"] else {},
            "/home/agent",
        )
        self.assertEqual(match, {"paneId": "wN:p1", "tabId": "wN:t1", "socketPath": self.environment["HERDR_SOCKET_PATH"]})

    def test_does_not_attach_to_wrong_or_spoofed_process(self):
        self.assertIsNone(self.probe.find_live_pane("another_session", [self.entry], lambda _: self.environment, lambda _: self.snapshot, "/home/agent"))
        wrong = {**self.snapshot, "agents": [{"agent": "shell", "pane_id": "wN:p1", "tab_id": "wN:t1"}]}
        self.assertIsNone(self.probe.find_live_pane(self.entry["session_id"], [self.entry], lambda _: self.environment, lambda _: wrong, "/home/agent"))
        outside = {**self.environment, "HERDR_SOCKET_PATH": "/run/user/1000/malicious.sock"}
        self.assertIsNone(self.probe.find_live_pane(self.entry["session_id"], [self.entry], lambda _: outside, lambda _: self.snapshot, "/home/agent"))

    def test_duplicate_live_owners_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.probe.find_live_pane(self.entry["session_id"], [self.entry, self.entry], lambda _: self.environment, lambda _: self.snapshot, "/home/agent")


if __name__ == "__main__":
    unittest.main()
