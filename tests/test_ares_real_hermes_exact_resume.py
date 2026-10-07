"""Disposable subprocess evidence for the installed Hermes exact-resume CLI.

No Ares SessionSupervisor, Herdr instance, live profile, provider request, or real
session is involved. The inherited pipe is the only readiness authority here.
"""
import json
import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from hermes_state import SessionDB
from hermes_cli.active_sessions import active_session_registry_snapshot, try_acquire_active_session

SOURCE = Path.home() / '.hermes/hermes-agent'
HERMES = SOURCE / 'venv/bin/hermes'
SID = '20260928_120000_exactfixture'


class RealHermesExactResume(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='ares-hermes-exact-', dir=Path.home() / '.cache')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.profile = self.root / 'profile'
        self.profile.mkdir(mode=0o700)
        self.db = SessionDB(db_path=self.profile / 'state.db')
        self.addCleanup(self.db.close)
        self.db.create_session(SID, source='desktop')
        self.db.append_message(SID, 'user', 'fixture-user-sentinel')
        self.db.append_message(SID, 'assistant', 'fixture-assistant-sentinel')
        self.env = {k: v for k, v in os.environ.items() if k in ('PATH', 'TERM', 'LANG', 'LC_ALL')}
        self.env.update(HOME=str(self.root), HERMES_HOME=str(self.profile),
                        XDG_CACHE_HOME=str(self.root / 'cache'), XDG_CONFIG_HOME=str(self.root / 'config'),
                        OPENAI_API_KEY='fixture-not-a-real-key', TERM='xterm-256color',
                        HERMES_DEFER_AGENT_STARTUP='1', HERMES_DISABLE_LAZY_INSTALLS='1',
                        PYTHONPATH=str(SOURCE), PYTHONDONTWRITEBYTECODE='1')
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=8)
            self.assertIsNotNone(child.returncode)
        self.assertEqual(active_session_registry_snapshot(self.profile), [])

    def launch(self, source='desktop'):
        master, slave = pty.openpty()
        reader, writer = os.pipe()
        cmd = [str(HERMES), 'chat', '--cli', '--resume', SID, '--source', source,
               '--exact-resume', '--exact-resume-ready-fd', str(writer)]
        child = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave,
                                 pass_fds=(writer,), close_fds=True, cwd=self.root,
                                 env=self.env, start_new_session=True)
        self.children.append(child)
        os.close(slave)
        os.close(writer)
        return child, master, reader

    def collect(self, master, reader, child, timeout=35):
        out = bytearray()
        receipt = bytearray()
        deadline = time.monotonic() + timeout
        pending = {master: out, reader: receipt}
        while time.monotonic() < deadline and pending:
            ready, _, _ = select.select(list(pending), [], [], min(.25, max(0, deadline - time.monotonic())))
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b''
                if data:
                    pending[fd].extend(data)
                else:
                    pending.pop(fd, None)
            if receipt or (child.poll() is not None and reader not in pending):
                break
        os.close(reader)
        os.close(master)
        return bytes(receipt), bytes(out)

    def test_real_cli_publishes_owned_exact_restoration(self):
        child, master, reader = self.launch()
        raw, output = self.collect(master, reader, child)
        self.assertTrue(raw, output[-3000:].decode(errors='replace'))
        self.assertIn(b'fixture-user-sentinel', output)
        self.assertIn(b'fixture-assistant-sentinel', output)
        receipt = json.loads(raw)
        self.assertEqual(receipt['protocol'], 'hermes-exact-resume-ready-v1')
        self.assertEqual((receipt['sessionId'], receipt['source'], receipt['pid'], receipt['messageCount']),
                         (SID, 'desktop', child.pid, 2))
        leases = active_session_registry_snapshot(self.profile, strict=True)
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0]['lease_id'], receipt['leaseId'])
        self.assertEqual(leases[0]['pid'], child.pid)
        self.assertEqual(leases[0]['session_id'], SID)
        self.assertIsNone(child.poll())
        self.assertEqual([m['content'] for m in self.db.get_messages_as_conversation(SID)],
                         ['fixture-user-sentinel', 'fixture-assistant-sentinel'])
        print('REAL_CLI_READY', json.dumps(receipt, sort_keys=True))

    def assert_refused(self, source='desktop', marker='Exact resume refused'):
        child, master, reader = self.launch(source)
        raw, output = self.collect(master, reader, child)
        child.wait(timeout=8)
        self.assertEqual(raw, b'', 'refused CLI emitted a readiness receipt')
        self.assertNotEqual(child.returncode, 0)
        self.assertIn(marker, output.decode(errors='replace'))
        self.assertEqual(active_session_registry_snapshot(self.profile, strict=True), [])

    def test_active_desktop_lease_refuses_second_cli_owner(self):
        lease, error = try_acquire_active_session(session_id=SID, surface='desktop',
                                                  config={}, registry_home=self.profile,
                                                  metadata={'live_session_id': SID})
        self.assertIsNone(error)
        self.addCleanup(lease.release)
        child, master, reader = self.launch()
        raw, output = self.collect(master, reader, child)
        child.wait(timeout=8)
        self.assertEqual(raw, b'')
        self.assertNotEqual(child.returncode, 0)
        self.assertIn('This chat is open in another Hermes window/terminal', output.decode(errors='replace'))
        self.assertEqual([entry['lease_id'] for entry in active_session_registry_snapshot(self.profile)], [lease.lease_id])
        lease.release()

    def test_source_mismatch_refuses_without_receipt(self):
        self.assert_refused(source='cli', marker='session source does not match')

    def test_compression_tip_redirect_refuses_without_receipt(self):
        child_id = '20260928_120100_exactchild'
        self.db._write_sql('UPDATE sessions SET end_reason = ?, ended_at = ? WHERE id = ?',
                           ('compression', time.time(), SID))
        self.db.create_session(child_id, source='desktop', parent_session_id=SID)
        self.db.append_message(child_id, 'user', 'descendant-sentinel')
        self.assertEqual(self.db.resolve_resume_session_id(SID), child_id)
        self.assert_refused(marker='requested ID resolved to a different session')

    def test_alternation_repair_refuses_altered_active_history(self):
        self.db.append_message(SID, 'user', 'extra-turn')
        self.db.append_message(SID, 'user', 'adjacent-user-breaks-exactness')
        self.assert_refused(marker='stored messages are missing, filtered, or not restored exactly')

    def test_missing_active_messages_refuses_without_receipt(self):
        self.db._write_sql('UPDATE messages SET active = 0 WHERE session_id = ?', (SID,))
        self.assert_refused(marker='stored messages are missing, filtered, or not restored exactly')

    def test_cancellation_reaps_child_and_releases_lease(self):
        child, master, reader = self.launch()
        raw, output = self.collect(master, reader, child)
        self.assertTrue(raw, output[-2000:].decode(errors='replace'))
        self.assertEqual(json.loads(raw)['pid'], child.pid)
        child.terminate()
        child.wait(timeout=8)
        self.assertIsNotNone(child.returncode)
        self.assertFalse(Path('/proc', str(child.pid)).exists())
        self.assertEqual(active_session_registry_snapshot(self.profile, strict=True), [])


if __name__ == '__main__':
    unittest.main()
