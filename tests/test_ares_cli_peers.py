"""Peer CLI contracts through the executable and a mocked authenticated API."""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / "bin" / "ares"
PAIR_CODE = "48372910"
PEER_ID = "a" * 32
FINGERPRINT = "b" * 64


class Fixture(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests = []
        self.status = 200
        self.payload = {"ok": True}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        data = json.dumps(self.server.payload).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class PeerCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Fixture()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        token = Path(self.temp.name) / "omarchy/ares/access.token"
        token.parent.mkdir(parents=True)
        token.write_text("fixture-private-token\n")
        token.chmod(0o600)
        self.server.requests.clear()
        self.server.status = 200
        self.server.payload = {"ok": True}

    def cli(self, *args, input=None):
        env = {**os.environ, "XDG_STATE_HOME": self.temp.name,
               "ARES_API_URL": f"http://127.0.0.1:{self.server.server_port}"}
        return subprocess.run([sys.executable, str(CLI), *args], input=input,
                              text=True, capture_output=True, env=env, timeout=8)

    def test_list_returns_bounded_discovery_and_trust_inventory(self):
        peers = [{"peerId": PEER_ID, "alias": "desk", "trusted": False}]
        self.server.payload = {"ok": True, "peers": peers}
        result = self.cli("peers", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "peers": peers})
        self.assertEqual(self.server.requests[0][:2], ("GET", "/v1/peers"))
        self.assertEqual(self.server.requests[0][2]["Authorization"], "Bearer fixture-private-token")
        human = self.cli("peers", "list")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("desk", human.stdout)
        self.assertIn("untrusted", human.stdout)

    def test_list_preserves_public_pairing_and_offline_flags(self):
        peers = [{"id": PEER_ID, "peerId": PEER_ID, "alias": "desk", "paired": True,
                  "online": False, "trusted": True, "status": "offline"}]
        self.server.payload = {"ok": True, "peers": peers}
        result = self.cli("peers", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["peers"], peers)
        human = self.cli("peers", "list")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("offline", human.stdout)

    def test_pair_code_only_in_explicit_stdin_and_request_body(self):
        self.server.payload = {"ok": True, "code": PAIR_CODE, "secret": "fixture-private-token"}
        result = self.cli("--json", "peers", "pair", "desk", "--peer-id", PEER_ID,
                          "--fingerprint", FINGERPRINT, "--code-stdin", input=PAIR_CODE + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(PAIR_CODE, result.stdout + result.stderr)
        self.assertNotIn("fixture-private-token", result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "alias": "desk", "peerId": PEER_ID})
        method, path, _, body = self.server.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/peers/pair"))
        self.assertEqual(json.loads(body), {"alias": "desk", "peerId": PEER_ID,
                                            "fingerprint": FINGERPRINT, "code": PAIR_CODE})

    def test_pair_rejects_invalid_fingerprint_before_code_or_api(self):
        result = self.cli("--json", "peers", "pair", "desk", "--peer-id", PEER_ID,
                          "--fingerprint", "SHA256:bad", "--code-stdin", input=PAIR_CODE + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.server.requests)
        self.assertEqual(json.loads(result.stdout)["ok"], False)

    def test_pair_rejection_never_reflects_server_code_or_token(self):
        self.server.status = 403
        self.server.payload = {"ok": False, "error": f"rejected {PAIR_CODE} fixture-private-token"}
        result = self.cli("--json", "peers", "pair", "desk", "--peer-id", PEER_ID,
                          "--fingerprint", FINGERPRINT, "--code-stdin", input=PAIR_CODE + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertNotIn(PAIR_CODE, result.stdout + result.stderr)
        self.assertNotIn("fixture-private-token", result.stdout + result.stderr)

    def test_pair_no_code_option_or_positional_code_reflection(self):
        for extra in (("--code", PAIR_CODE), (PAIR_CODE,)):
            with self.subTest(extra=extra):
                result = self.cli("--json", "peers", "pair", "desk", "--peer-id", PEER_ID,
                                  "--fingerprint", FINGERPRINT, *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(PAIR_CODE, result.stdout + result.stderr)
        self.assertFalse(self.server.requests)

    def test_pair_rejects_invalid_identifier_or_code_without_api(self):
        for alias, peer_id, code in (("../desk", PEER_ID, PAIR_CODE),
                                     ("a" * 65, PEER_ID, PAIR_CODE),
                                     ("desk", "bad/id", PAIR_CODE),
                                     ("desk", PEER_ID, "1234"),
                                     ("desk", PEER_ID, PAIR_CODE + "x")):
            with self.subTest(alias=alias, peer_id=peer_id, code=code):
                result = self.cli("--json", "peers", "pair", alias, "--peer-id", peer_id,
                                  "--fingerprint", FINGERPRINT, "--code-stdin", input=code + "\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(code, result.stdout + result.stderr)
        self.assertFalse(self.server.requests)

    def test_pair_without_stdin_requires_interactive_prompt(self):
        result = self.cli("--json", "peers", "pair", "desk", "--peer-id", PEER_ID,
                          "--fingerprint", FINGERPRINT)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.server.requests)

    def test_masked_prompt_reads_code_without_echo(self):
        module_path = CLI.with_name("ares_cli_peers.py")
        spec = importlib.util.spec_from_file_location("ares_cli_peers", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.sys.stdin, "isatty", return_value=True), \
             patch.object(module.getpass, "getpass", return_value=PAIR_CODE) as prompt:
            self.assertEqual(module._read_code(False), PAIR_CODE)
        prompt.assert_called_once_with("Pairing code: ")

    def test_pair_rejects_oversized_stdin_without_api(self):
        result = self.cli("--json", "peers", "pair", "desk", "--peer-id", PEER_ID,
                          "--fingerprint", FINGERPRINT, "--code-stdin", input="9" * 1000)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.server.requests)
        self.assertNotIn("9" * 1000, result.stdout + result.stderr)

    def test_revoke_requires_confirmation_and_explicit_payload(self):
        denied = self.cli("--json", "peers", "revoke", "desk")
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse(self.server.requests)
        accepted = self.cli("peers", "revoke", "desk", "--confirm", "--json")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout), {"ok": True, "alias": "desk", "revoked": True})
        self.assertEqual(self.server.requests[0][:2], ("POST", "/v1/peers/desk/revoke"))
        self.assertEqual(json.loads(self.server.requests[0][3]), {"confirmed": True})

    def test_sessions_is_read_only_and_alias_is_bounded(self):
        self.server.payload = {"ok": True, "sessions": [{"id": "s1", "profile": "default"}]}
        result = self.cli("peers", "sessions", "desk", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sessions"][0]["id"], "s1")
        self.assertEqual(self.server.requests[0][:2], ("GET", "/v1/peers/desk/sessions"))
        self.server.requests.clear()
        bad = self.cli("--json", "peers", "sessions", "../../oops")
        self.assertNotEqual(bad.returncode, 0)
        self.assertFalse(self.server.requests)

    def test_inventory_strips_unexpected_secret_fields(self):
        self.server.payload = {"ok": True, "peers": [
            {"peerId": PEER_ID, "alias": "desk", "trusted": True,
             "token": "unexpected-secret", "code": PAIR_CODE}]}
        result = self.cli("peers", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unexpected-secret", result.stdout)
        self.assertNotIn(PAIR_CODE, result.stdout)


if __name__ == "__main__":
    unittest.main()
