"""Contract tests for the stdlib Ares API client. Author: Andrés Villagrán."""
import contextlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

CLI = Path(__file__).resolve().parents[1] / "bin" / "ares"


class Fixture(http.server.ThreadingHTTPServer):
    def __init__(self, address):
        super().__init__(address, Handler)
        self.requests = []
        self.status = 200
        self.payload = {"ok": True, "projects": [{"id": "p1", "name": "Alpha"}],
                        "rooms": [{"id": "r1", "projectId": "p1", "name": "Room", "status": "active"},
                                  {"id": "r3", "projectId": "p1", "name": "Paused", "status": "paused"}],
                        "archivedRooms": [{"id": "r2", "projectId": "p1", "name": "Old", "status": "deleted"}]}


class Handler(http.server.BaseHTTPRequestHandler):
    def handle_request(self):
        size = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(size) if size else b""
        self.server.requests.append((self.command, self.path, dict(self.headers), data))
        if self.headers.get("Authorization") != "Bearer private-fixture-token":
            status, payload = 401, {"ok": False, "error": "authentication required"}
        else:
            status, payload = self.server.status, self.server.payload
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = do_POST = handle_request

    def log_message(self, *args):
        pass


class CliTest(unittest.TestCase):
    def test_terminal_inventory_is_available_from_main_cli(self):
        self.server.payload = {"ok": True, "complete": True, "terminals": [
            {"id": "terminal-1", "host": "local"}]}
        result = self.run_cli("--json", "terminals", "list", "--room", "r1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["terminals"][0]["id"], "terminal-1")
        self.assertEqual(self.server.requests[0][0:2],
                         ("GET", "/v1/tty/rooms/r1/terminals"))

    def test_terminal_capture_fails_closed_in_json_mode(self):
        result = self.run_cli("terminals", "capture", "--room", "r1", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["ok"], False)
        self.assertEqual(self.server.requests, [])

    @classmethod
    def setUpClass(cls):
        cls.server = Fixture(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name) / "omarchy" / "ares"
        state.mkdir(parents=True)
        (state / "access.token").write_text("private-fixture-token\n")
        (state / "access.token").chmod(0o600)
        self.server.requests.clear()
        self.server.status = 200
        self.server.payload = {"ok": True, "projects": [{"id": "p1", "name": "Alpha"}],
                               "rooms": [{"id": "r1", "projectId": "p1", "name": "Room", "status": "active"},
                                         {"id": "r3", "projectId": "p1", "name": "Paused", "status": "paused"}],
                               "archivedRooms": [{"id": "r2", "projectId": "p1", "name": "Old", "status": "deleted"}]}

    def run_cli(self, *args):
        env = {**os.environ, "XDG_STATE_HOME": self.temp.name,
               "ARES_API_URL": f"http://127.0.0.1:{self.server.server_port}"}
        return subprocess.run([sys.executable, str(CLI), *args], text=True,
                              capture_output=True, env=env, timeout=8)

    def test_projects_list_uses_authenticated_compact_state(self):
        result = self.run_cli("--json", "projects", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["projects"], [{"id": "p1", "name": "Alpha"}])
        self.assertEqual(self.server.requests[0][0:2], ("GET", "/v1/state?compact=1"))
        self.assertEqual(self.server.requests[0][2]["Authorization"], "Bearer private-fixture-token")
        self.assertNotIn("private-fixture-token", result.stdout + result.stderr)

    def test_json_option_after_command(self):
        result = self.run_cli("projects", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["projects"][0]["id"], "p1")

    def test_projects_show_reports_missing_id_without_mutation(self):
        result = self.run_cli("--json", "projects", "show", "unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["ok"], False)
        self.assertEqual(len(self.server.requests), 1)

    def test_rooms_list_filters_project_and_archived(self):
        result = self.run_cli("--json", "rooms", "list", "--project", "p1", "--archived")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r["id"] for r in json.loads(result.stdout)["rooms"]], ["r2"])

    def test_rooms_default_includes_archived_and_active_excludes_paused(self):
        all_rooms = self.run_cli("--json", "rooms", "list")
        active = self.run_cli("--json", "rooms", "list", "--active")
        self.assertEqual([r["id"] for r in json.loads(all_rooms.stdout)["rooms"]],
                         ["r1", "r3", "r2"])
        self.assertEqual([r["id"] for r in json.loads(active.stdout)["rooms"]], ["r1"])

    def test_room_show_finds_archived_room(self):
        result = self.run_cli("--json", "rooms", "show", "r2")
        self.assertEqual(json.loads(result.stdout)["room"]["id"], "r2")

    def test_room_archive_sends_explicit_key_and_project(self):
        result = self.run_cli("--json", "rooms", "archive", "r1", "--project", "p1",
                              "--idempotency-key", "request-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        method, path, headers, data = self.server.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/rooms/r1/archive"))
        self.assertEqual(json.loads(data), {"projectId": "p1", "idempotencyKey": "request-1"})

    def test_room_mutation_without_explicit_key_never_sends(self):
        result = self.run_cli("--json", "rooms", "archive", "r1", "--project", "p1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["ok"], False)
        self.assertFalse(self.server.requests)

    def test_empty_idempotency_key_never_sends(self):
        result = self.run_cli("--json", "rooms", "archive", "r1", "--project", "p1",
                              "--idempotency-key", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.server.requests)

    def test_oversized_mutation_inputs_never_send(self):
        for args in (("rooms", "alias", "r1", "X" * 121),
                     ("rooms", "pause", "r1", "--project", "p1",
                      "--idempotency-key", "X" * 4096)):
            result = self.run_cli("--json", *args)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.server.requests)

    def test_map_color_set_and_reset_use_explicit_color(self):
        result = self.run_cli("--json", "projects", "map-color", "set", "p1", "#224466")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.requests[-1][0:2], ("POST", "/v1/projects/p1/map-color"))
        self.assertEqual(json.loads(self.server.requests[-1][3]), {"color": "#224466"})
        result = self.run_cli("--json", "projects", "map-color", "reset", "p1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.server.requests[-1][3]), {"color": ""})

    def test_invalid_color_fails_before_request(self):
        result = self.run_cli("--json", "projects", "map-color", "set", "p1", "red")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.server.requests)

    def test_alias_uses_alias_endpoint_not_nonexistent_rename(self):
        result = self.run_cli("--json", "rooms", "rename", "r1", "Desk")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.requests[0][0:2], ("POST", "/v1/rooms/r1/alias"))
        self.assertEqual(json.loads(self.server.requests[0][3]), {"alias": "Desk"})

    def test_server_error_is_nonzero_and_does_not_expose_token(self):
        self.server.status = 409
        self.server.payload = {"ok": False, "error": "conflict"}
        result = self.run_cli("--json", "rooms", "pause", "r1", "--project", "p1",
                              "--idempotency-key", "request-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["error"], "conflict")
        self.assertNotIn("private-fixture-token", result.stdout + result.stderr)

    def test_invalid_token_permissions_fail_closed(self):
        (Path(self.temp.name) / "omarchy" / "ares" / "access.token").chmod(0o644)
        result = self.run_cli("--json", "projects", "list")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.server.requests)
        self.assertNotIn("private-fixture-token", result.stdout + result.stderr)

    def test_success_payload_does_not_echo_access_token(self):
        self.server.payload = {"ok": True, "projects": [
            {"id": "p1", "name": "private-fixture-token"}]}
        for format_flag in (("--json",), ()):
            result = self.run_cli(*format_flag, "projects", "list")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("private-fixture-token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
