"""Codex quota auth resolution: Hermes moved OAuth tokens into credential_pool entries."""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(_BIN))
_LOADER = importlib.machinery.SourceFileLoader(
    "ares_hermes_runtime", str(_BIN / "ares-hermes-runtime")
)
SPEC = importlib.util.spec_from_loader("ares_hermes_runtime", _LOADER)
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)


class CodexAccessTokenTest(unittest.TestCase):
    def test_reads_new_credential_pool_schema(self):
        auth = {"credential_pool": {"openai-codex": [
            {"id": "a", "priority": 0, "access_token": "pool-token-a"},
            {"id": "b", "priority": 1, "access_token": "pool-token-b"},
        ]}}
        self.assertEqual(RUNTIME._codex_access_token(auth), "pool-token-a")

    def test_skips_pool_entries_without_token(self):
        auth = {"credential_pool": {"openai-codex": [
            {"id": "a", "priority": 0},
            {"id": "b", "priority": 1, "access_token": "pool-token-b"},
        ]}}
        self.assertEqual(RUNTIME._codex_access_token(auth), "pool-token-b")

    def test_falls_back_to_legacy_providers_schema(self):
        auth = {"providers": {"openai-codex": {"tokens": {"access_token": "legacy-token"}}}}
        self.assertEqual(RUNTIME._codex_access_token(auth), "legacy-token")

    def test_pool_wins_over_legacy(self):
        auth = {
            "credential_pool": {"openai-codex": [{"access_token": "pool-token"}]},
            "providers": {"openai-codex": {"tokens": {"access_token": "legacy-token"}}},
        }
        self.assertEqual(RUNTIME._codex_access_token(auth), "pool-token")

    def test_falls_back_to_codex_cli_auth_file(self):
        auth = {"credential_pool": {"openai-codex": []}, "providers": {}}
        with tempfile.TemporaryDirectory() as tmp:
            cli_auth = Path(tmp) / "auth.json"
            cli_auth.write_text(json.dumps({"access_token": "cli-token"}))
            token = RUNTIME._codex_access_token(auth, codex_auth_path=cli_auth)
        self.assertEqual(token, "cli-token")

    def test_returns_none_when_no_credential(self):
        auth = {"credential_pool": {"openai-codex": []}, "providers": {}}
        with tempfile.TemporaryDirectory() as tmp:
            token = RUNTIME._codex_access_token(
                auth, codex_auth_path=Path(tmp) / "missing.json"
            )
        self.assertIsNone(token)


if __name__ == "__main__":
    unittest.main()
