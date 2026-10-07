"""Contract tests for the `ares-server token` command."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "bin" / "ares-server"


def run_token(state_home: Path, *arguments: str) -> tuple[int, dict]:
    env = dict(os.environ)
    env["XDG_STATE_HOME"] = str(state_home)
    completed = subprocess.run(
        [str(SERVER), "token", *arguments],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )
    return completed.returncode, json.loads(completed.stdout)


class TokenCommandTest(unittest.TestCase):
    def test_launcher_is_packaged_as_a_regular_file(self):
        self.assertFalse(SERVER.is_symlink())
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            launcher = fake_bin / "ares-server"
            shutil.copy2(SERVER, launcher)
            run = subprocess.run(
                [str(launcher), "status"], capture_output=True, text=True,
                timeout=5, check=False,
            )
            self.assertEqual(run.returncode, 127)
            self.assertIn("cargo build --release", run.stderr)

    def test_show_creates_and_returns_a_token_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            state_home = Path(directory)
            code, payload = run_token(state_home)
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            value = payload["token"]
            self.assertGreaterEqual(len(value), 32)
            token_path = state_home / "omarchy" / "ares" / "access.token"
            self.assertEqual(token_path.read_text(encoding="utf-8").strip(), value)
            mode = stat.S_IMODE(token_path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            again_code, again = run_token(state_home)
            self.assertEqual(again_code, 0)
            self.assertEqual(again["token"], value)

    def test_set_accepts_a_valid_custom_token(self):
        with tempfile.TemporaryDirectory() as directory:
            state_home = Path(directory)
            custom = "myCustom_token-0123456789abcdefg"
            code, payload = run_token(state_home, "--set", custom)
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["token"], custom)
            self.assertFalse(payload["restarted"])
            _, shown = run_token(state_home)
            self.assertEqual(shown["token"], custom)

    def test_set_rejects_short_or_invalid_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            state_home = Path(directory)
            for invalid in ("short", "x" * 31, "has space " + "x" * 30, "ñ" * 32, "x" * 257):
                code, payload = run_token(state_home, "--set", invalid)
                self.assertEqual(code, 1, invalid)
                self.assertFalse(payload["ok"])
                self.assertIn("32-256", payload["error"])
            self.assertFalse((state_home / "omarchy" / "ares" / "access.token").exists())

    def test_regenerate_replaces_the_token(self):
        with tempfile.TemporaryDirectory() as directory:
            state_home = Path(directory)
            _, first = run_token(state_home)
            code, second = run_token(state_home, "--regenerate")
            self.assertEqual(code, 0)
            self.assertTrue(second["ok"])
            self.assertNotEqual(first["token"], second["token"])
            _, shown = run_token(state_home)
            self.assertEqual(shown["token"], second["token"])


if __name__ == "__main__":
    unittest.main()
