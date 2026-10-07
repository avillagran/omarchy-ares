"""Exercise the production XHR request function in offscreen Qt QML."""
import http.server
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"


class FailureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":false,"error":"unauthorized"}')

    def log_message(self, *args):
        pass


@unittest.skipUnless(Path(RUNNER).is_file(), "Qt Quick Test runner unavailable")
class BridgeRequestQmlTests(unittest.TestCase):
    def test_failure_callbacks_run_in_real_qml(self):
        source = (ROOT / "AresBridge.qml").read_text()
        request = re.search(r"^  function request\(method, path, body, callback\) \{[\s\S]*?^  \}", source, re.M)
        self.assertIsNotNone(request)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FailureHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        # Reserve an ephemeral port and close it: a real refused connection, not a mocked XHR.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory(prefix="ares-bridge-qml-", dir=os.environ.get("TMPDIR")) as directory:
                qml = '''import QtQuick
import QtTest
Item {
  id: bridge
  width: 100; height: 100
  property bool available: true
  property string endpoint: ""
  property string token: "test-token"
  property string error: ""
  property int offlineSignals: 0
  property int reconnects: 0
  signal connectionChanged(bool online)
  onConnectionChanged: function(online) { if (!online) offlineSignals++ }
  QtObject { id: reconnect; function restart() { bridge.reconnects++ } }
REQUEST_FUNCTION
  TestCase {
    name: "AresBridgeRequest"; when: windowShown
    function init() {
      bridge.available = true; bridge.error = "";
      bridge.offlineSignals = 0; bridge.reconnects = 0
    }
    function test_http_401() {
      bridge.endpoint = "http://127.0.0.1:HTTP_PORT"
      var replies = []
      bridge.request("GET", "/reject", null, function(result) { replies.push(result) })
      tryCompare(replies, "length", 1)
      wait(30)
      compare(replies.length, 1)
      compare(replies[0].error, "unauthorized")
      compare(bridge.available, false)
      compare(bridge.offlineSignals, 1)
      compare(bridge.reconnects, 1)
    }
    function test_network_failure() {
      bridge.endpoint = "http://127.0.0.1:CLOSED_PORT"
      var replies = []
      bridge.request("GET", "/missing", null, function(result) { replies.push(result) })
      tryCompare(replies, "length", 1)
      wait(30)
      compare(replies.length, 1)
      compare(replies[0].ok, false)
      compare(bridge.available, false)
      compare(bridge.offlineSignals, 1)
      compare(bridge.reconnects, 1)
    }
  }
}
'''.replace("REQUEST_FUNCTION", request.group()).replace("HTTP_PORT", str(server.server_port)).replace("CLOSED_PORT", str(closed_port))
                (Path(directory) / "tst_bridge.qml").write_text(qml)
                result = subprocess.run([RUNNER, "-input", directory], capture_output=True, text=True, timeout=25,
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("ReferenceError", result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
