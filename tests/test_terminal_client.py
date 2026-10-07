"""Exercise the actual launcher with fake transports; never attach user terminals."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TerminalClientTest(unittest.TestCase):
    def test_room_navigation_has_no_agent_creation_or_legacy_discovery_path(self):
        source = (ROOT / "server/src/http.rs").read_text()
        route = source.split('if ["ensure", "focus"].contains(action)', 1)[1].split('if app.bridge.is_some()', 1)[0]
        for forbidden in ("ensure_room_tab", "attach_live_room", "probe_live_room", "session.active_list"):
            self.assertNotIn(forbidden, route)

    def test_generic_local_terminal_uses_exact_socket_and_cleans_nested_context(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "herdr"
            binary.write_text("#!/usr/bin/env python3\nimport os,sys,json\nprint(json.dumps({'args':sys.argv[1:],'env':{k:v for k,v in os.environ.items() if k.startswith('HERDR_')}}))\n")
            binary.chmod(0o700)
            socket = directory + "/herdr.sock"
            env = dict(os.environ, HERDR_UNKNOWN_FUTURE="nested", HERDR_ENV="1")
            run = subprocess.run([str(ROOT / "bin/ares-herdr-client"), "terminal", "terminal_123", "", str(binary), socket], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result["args"], ["terminal", "attach", "terminal_123"])
            self.assertEqual(result["env"], {"HERDR_SOCKET_PATH": socket})

    def test_generic_remote_terminal_cleans_context_on_both_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "ssh"
            fake.write_text("#!/usr/bin/env python3\nimport os,sys,json\nprint(json.dumps({'args':sys.argv[1:],'env':{k:v for k,v in os.environ.items() if k.startswith('HERDR_')}}))\n")
            fake.chmod(0o700)
            env = dict(os.environ, PATH=directory + ":" + os.environ["PATH"], HERDR_FUTURE="nested")
            run = subprocess.run([str(ROOT / "bin/ares-herdr-client"), "terminal", "terminal_123", "user@peer", "/usr/bin/herdr", "/home/user/.config/herdr/herdr.sock"], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result["env"], {})
            self.assertIn("BatchMode=yes", result["args"])
            self.assertIn("StrictHostKeyChecking=yes", result["args"])
            self.assertIn("startswith", result["args"][-1])
            self.assertIn("terminal_123", result["args"][-1])
            self.assertNotIn("takeover", result["args"][-1])

    def test_invalid_identity_never_launches_transport(self):
        for args in (("terminal", "x;touch", "", "/usr/bin/herdr", "/x/herdr.sock"),
                     ("terminal", "term", "-oProxyCommand=x", "/usr/bin/herdr", "/x/herdr.sock"),
                     ("terminal", "term", "", "/usr/bin/../herdr", "/x/herdr.sock")):
            run = subprocess.run([str(ROOT / "bin/ares-herdr-client"), *args], capture_output=True)
            self.assertEqual(run.returncode, 2)
