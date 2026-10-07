"""Read-only inventory and fail-closed terminal attachment contract."""
import argparse
import importlib.util
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "bin" / "ares_cli_terminal.py"
spec = importlib.util.spec_from_file_location("ares_cli_terminal", MODULE)
terminal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terminal)


class Client:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.replies[path]


def args(*words):
    parser = argparse.ArgumentParser()
    terminal.register(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["terminals", *words])


class TerminalCliTest(unittest.TestCase):
    def test_list_reads_room_inventory_only(self):
        client = Client({"/v1/tty/rooms/room-1/terminals": {"terminals": [
            {"id": "term-1", "label": "build", "status": "running", "host": "local"}
        ]}})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(terminal.dispatch(args("list", "--room", "room-1"), client), 0)
        self.assertIn("term-1", output.getvalue())
        self.assertIn("build", output.getvalue())
        self.assertEqual(client.calls, [("GET", "/v1/tty/rooms/room-1/terminals", None)])

    def test_attach_group_member_uses_exact_descriptor_as_argv(self):
        descriptor = {"ok": True, "tty": {"sessionName": "terminal", "agentName": "t9",
            "terminalId": "t9", "host": "local", "sshTarget": "",
            "remoteBin": "/opt/herdr/bin/herdr", "remoteSocket": "/home/alice/.config/herdr/herdr.sock",
            "roomId": "g1", "memberKey": "local::builder", "readOnly": True,
            "agentStarted": True, "created": False}}
        path = "/v1/tty/groups/g1/members/local%3A%3Abuilder/terminal"
        client = Client({path: descriptor})
        with patch.object(terminal.sys.stdin, "isatty", return_value=True), \
             patch.object(terminal.sys.stdout, "isatty", return_value=True), \
             patch.object(terminal.Path, "is_file", return_value=True), \
             patch.object(terminal.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(terminal.dispatch(args("attach", "--group", "g1", "--member", "local::builder"), client), 0)
        self.assertEqual(client.calls, [("GET", path, None)])
        self.assertEqual(run.call_args.args[0][1:], ["terminal", "t9", "",
            "/opt/herdr/bin/herdr", "/home/alice/.config/herdr/herdr.sock"])
        self.assertEqual(run.call_args.kwargs, {"check": False})

    def test_attach_requires_tty_before_network_or_subprocess(self):
        client = Client({})
        with patch.object(terminal.sys.stdin, "isatty", return_value=False), \
             patch.object(terminal.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(terminal.dispatch(args("attach", "--room", "r1"), client), 2)
        self.assertEqual(client.calls, [])
        run.assert_not_called()

    def test_capture_is_honestly_unsupported_without_api_or_subprocess(self):
        client = Client({})
        error = io.StringIO()
        with redirect_stderr(error), patch.object(terminal.subprocess, "run") as run:
            self.assertEqual(terminal.dispatch(args("capture", "--room", "r1"), client), 2)
        self.assertIn("unsupported", error.getvalue())
        self.assertEqual(client.calls, [])
        run.assert_not_called()

    def test_bad_scope_and_path_fails_closed(self):
        for words in [("attach", "--room", "../x"),
                      ("attach", "--group", "g1"),
                      ("capture", "--room", "r1", "--member", "m1")]:
            with self.subTest(words=words), redirect_stderr(io.StringIO()):
                self.assertEqual(terminal.dispatch(args(*words), Client({})), 2)

    def test_room_attach_uses_binding_for_remote_identity(self):
        path = "/v1/tty/rooms/r1/terminals"
        item = {"id": "a" * 64, "terminalId": "t9", "host": "peer_1",
                "sshTarget": "alice@host.example", "herdrBin": "/opt/herdr/bin/herdr",
                "socketPath": "/home/alice/.config/herdr/herdr.sock"}
        client = Client({path: {"ok": True, "complete": True, "host": "peer_1", "errors": [],
                                "binding": {"host": "peer_1", "terminal_key": "a" * 64},
                                "terminals": [item, dict(item, id="b" * 64, terminalId="other")]}})
        with patch.object(terminal.sys.stdin, "isatty", return_value=True), \
             patch.object(terminal.sys.stdout, "isatty", return_value=True), \
             patch.object(terminal.Path, "is_file", return_value=True), \
             patch.object(terminal.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(terminal.dispatch(args("attach", "--room", "r1"), client), 0)
        self.assertEqual(client.calls, [("GET", path, None)])
        self.assertEqual(run.call_args.args[0][1:], ["terminal", "t9", "alice@host.example",
            "/opt/herdr/bin/herdr", "/home/alice/.config/herdr/herdr.sock"])

    def test_room_attach_without_binding_never_selects_only_candidate(self):
        path = "/v1/tty/rooms/r1/terminals"
        client = Client({path: {"ok": True, "complete": True, "host": "local", "errors": [],
                                "binding": None, "terminals": [{"terminalId": "t9"}]}})
        with patch.object(terminal.sys.stdin, "isatty", return_value=True), \
             patch.object(terminal.sys.stdout, "isatty", return_value=True), \
             patch.object(terminal.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(terminal.dispatch(args("attach", "--room", "r1"), client), 2)
        run.assert_not_called()

    def test_room_attach_refuses_ambiguous_inventory(self):
        client = Client({"/v1/tty/rooms/r1/terminals": {"terminals": [{}, {}]}})
        with patch.object(terminal.sys.stdin, "isatty", return_value=True), \
             patch.object(terminal.sys.stdout, "isatty", return_value=True), \
             patch.object(terminal.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(terminal.dispatch(args("attach", "--room", "r1"), client), 2)
        run.assert_not_called()

    def test_conflicting_host_identity_fails_closed(self):
        path = "/v1/tty/groups/g1/members/m1/terminal"
        base = {"terminalId": "t9", "host": "local", "sshTarget": "",
                "herdrBin": "/usr/bin/herdr", "socketPath": "/home/a/herdr.sock"}
        with patch.object(terminal.sys.stdin, "isatty", return_value=True), \
             patch.object(terminal.sys.stdout, "isatty", return_value=True), \
             patch.object(terminal.Path, "is_file", return_value=True), \
             patch.object(terminal.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            for descriptor in ({**base, "sshTarget": "alice@host"},
                               {**base, "host": "peer_1"},
                               {key: value for key, value in base.items() if key != "host"}):
                with self.subTest(descriptor=descriptor):
                    self.assertEqual(terminal.dispatch(args("attach", "--group", "g1", "--member", "m1"),
                                                       Client({path: descriptor})), 2)
        run.assert_not_called()

    def test_malicious_remote_identity_never_launches(self):
        path = "/v1/tty/groups/g1/members/m1/terminal"
        valid = {"terminalId": "t9", "host": "peer_1", "sshTarget": "alice@host.example",
                 "herdrBin": "/opt/herdr/bin/herdr", "socketPath": "/home/alice/herdr.sock"}
        invalid = [{**valid, "sshTarget": "alice@host;touch /x"},
                   {**valid, "herdrBin": "/opt/../herdr"},
                   {**valid, "terminalId": "t9;evil"},
                   {**valid, "socketPath": "/tmp/other.sock"},
                   {**valid, "socketPath": "relative/herdr.sock"}]
        with patch.object(terminal.sys.stdin, "isatty", return_value=True), \
             patch.object(terminal.sys.stdout, "isatty", return_value=True), \
             patch.object(terminal.subprocess, "run") as run, redirect_stderr(io.StringIO()):
            for descriptor in invalid:
                with self.subTest(descriptor=descriptor):
                    self.assertEqual(terminal.dispatch(args("attach", "--group", "g1", "--member", "m1"),
                                                       Client({path: descriptor})), 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
