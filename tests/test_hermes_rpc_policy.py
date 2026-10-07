import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "hermes_desktop_rpc.py"
spec = importlib.util.spec_from_file_location("ares_hermes_desktop_rpc", MODULE_PATH)
rpc = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(rpc)


class HermesRpcPolicyTests(unittest.TestCase):
    def test_bootstrap_launcher_is_recognized_without_accepting_unrelated_python(self):
        command = "import hermes_bootstrap\nfrom hermes_cli.main import main\nsys.exit(main())"
        self.assertTrue(rpc._is_hermes_serve(["/usr/bin/python", "-I", "-c", command, "serve"]))
        self.assertFalse(rpc._is_hermes_serve(["/usr/bin/python", "-I", "-c", "print(1)", "serve"]))
        self.assertFalse(rpc._is_hermes_serve(["/usr/bin/python", "-I", "-c", command, "chat"]))

    def test_room_lifecycle_methods_are_allowed_through_private_broker(self):
        self.assertIn("session.interrupt", rpc._BROKER_METHODS)
        self.assertIn("session.close", rpc._BROKER_METHODS)
        self.assertIn("image.attach_bytes", rpc._BROKER_METHODS)
        self.assertIn("file.attach", rpc._BROKER_METHODS)

    def test_project_session_discovery_is_allowed_through_private_broker(self):
        self.assertIn("projects.tree", rpc._BROKER_METHODS)
        self.assertIn("projects.project_sessions", rpc._BROKER_METHODS)

    def test_persisted_session_messages_are_allowed_as_a_read_only_broker_call(self):
        self.assertIn("session.messages", rpc._BROKER_METHODS)


if __name__ == "__main__":
    unittest.main()
