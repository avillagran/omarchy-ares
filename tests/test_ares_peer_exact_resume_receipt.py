"""Checkout-only receipt checks; fixture inventory does not attest a real Herdr."""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from peer.herdr_source import _start_ticks
from peer.session_supervisor import SessionSupervisor, ResumeRefused
from test_ares_peer_session_supervisor import SourceFixture, SID


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = self.home / 'state.db'
        with sqlite3.connect(self.db) as db:
            db.executescript(Path(__file__).with_name('fixture_hermes_session_schema.sql').read_text())
            db.execute('INSERT INTO sessions (id,source,profile_name,started_at) VALUES (?,?,?,?)',
                       (SID, 'cli', 'default', 1.0))
            db.execute('INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)',
                       (SID, 'user', 'sentinel', 1.0))
        self.registry = self.home / 'runtime' / 'active_sessions.json'
        self.registry.parent.mkdir()
        self.registry.write_text('{"entries": []}')
        self.herdr = SourceFixture()
        self.pane = self.herdr.provision_empty()
        self.child = subprocess.Popen(['sleep', '30'])
        self.addCleanup(self.stop_child)
        self.supervisor = SessionSupervisor({'default': (self.home, self.db)}, self.herdr,
             lambda *args: self.fail('must never spawn'), process_birth=_start_ticks)
        self.before = self.supervisor._history('default', 'cli', SID)
        self.lease = 'a' * 32
        self.registry.write_text(json.dumps({'entries': [{'session_id': SID, 'pid': self.child.pid,
                                                          'lease_id': self.lease}]}))
        self.raw = (json.dumps({'protocol': 'hermes-exact-resume-ready-v1', 'sessionId': SID,
                                'source': 'cli', 'pid': self.child.pid, 'leaseId': self.lease,
                                'messageCount': 1}) + '\n').encode()

    def stop_child(self):
        if self.child.poll() is None:
            self.child.terminate()
        self.child.wait(timeout=8)

    def claim(self):
        self.herdr.add_claim()

    def accept(self, raw=None):
        return self.supervisor.accept_ready('default', 'cli', SID, self.pane,
                                            self.child.pid, self.raw if raw is None else raw,
                                            self.before)

    def test_receipt_needs_source_claim_and_unique_process_lease(self):
        with self.assertRaises(ResumeRefused):
            self.accept()
        self.claim()
        binding = self.accept()
        self.assertEqual(binding['pid'], self.child.pid)
        self.assertEqual(binding['pane'], self.supervisor._pane_key(self.pane))
        self.assertFalse(binding['writable'])
        with self.assertRaises(ResumeRefused):
            self.accept()
        self.stop_child()
        with self.assertRaises(ResumeRefused):
            self.supervisor.recheck_binding('default', 'cli', SID)

    def test_receipt_mismatch_and_duplicate_lease_refuse_without_binding(self):
        self.claim()
        for changed in ({'source': 'desktop'}, {'leaseId': 'other'}, {'messageCount': 2},
                        {'pid': os.getpid()}, {'protocol': 'unknown'}):
            doc = json.loads(self.raw)
            doc.update(changed)
            with self.assertRaises(ResumeRefused):
                self.accept((json.dumps(doc) + '\n').encode())
        self.registry.write_text(json.dumps({'entries': [{'session_id': SID,
                         'pid': self.child.pid, 'lease_id': self.lease}] * 2}))
        with self.assertRaises(ResumeRefused):
            self.accept()
        self.assertEqual(self.supervisor.bindings, {})


if __name__ == '__main__':
    unittest.main()
