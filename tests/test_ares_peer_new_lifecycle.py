"""Disposable source-local Herdr/Hermes exact-resume lifecycle tests."""
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import Mock

from peer.herdr_source import _start_ticks
from peer.new_lifecycle import NewLifecycle, CreationRefused, HerdrCliDriver
from test_ares_peer_session_supervisor import SourceFixture, SID


class NewLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'state.db'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(Path(__file__).with_name('fixture_hermes_session_schema.sql').read_text())
            db.execute('INSERT INTO sessions (id,source,profile_name,started_at) VALUES (?,?,?,?)',
                       (SID, 'cli', 'default', 1.0))
            db.execute('INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)',
                       (SID, 'user', 'sentinel', 1.0))
        self.registry = self.root / 'runtime/active_sessions.json'
        self.registry.parent.mkdir()
        self.registry.write_text('{"entries": []}')
        self.source = SourceFixture()
        self.driver = Mock()
        self.driver.provision.side_effect = self.source.provision_empty
        self.receipt = self.root / 'ready.json'
        self.sut = NewLifecycle(self.source, self.driver, {'default': (self.root, self.db)},
                                self.receipt, process_birth=_start_ticks, timeout=.3)
        self.child = None
        self.addCleanup(self.stop_child)

    def stop_child(self):
        if self.child:
            if self.child.poll() is None:
                self.child.terminate()
            self.child.wait(timeout=3)

    def launch_fixture(self, pane, profile, source, sid, path):
        self.child = subprocess.Popen(['sleep', '30'])
        lease = 'a' * 32
        self.registry.write_text(json.dumps({'entries': [{'session_id': sid, 'pid': self.child.pid,
                                                         'lease_id': lease}]}))
        self.source.add_claim(value=sid)
        path.write_text(json.dumps({'protocol': 'hermes-exact-resume-ready-v1',
            'sessionId': sid, 'source': source, 'pid': self.child.pid,
            'leaseId': lease, 'messageCount': 1}) + '\n')
        path.chmod(0o600)

    def test_exact_cli_resume_readback_is_nonwritable(self):
        self.driver.launch.side_effect = self.launch_fixture
        result = self.sut.resume('default', 'cli', SID)
        self.assertEqual(result['sessionId'], SID)
        self.assertEqual(result['paneId'], 'w1:p1')
        self.assertEqual(result['terminalId'], 'term_1')
        self.assertEqual(result['pid'], self.child.pid)
        self.assertFalse(result['inputReady'])
        self.assertEqual(self.driver.launch.call_count, 1)

    def test_missing_source_claim_refuses_after_launch(self):
        def no_claim(*args):
            self.launch_fixture(*args)
            self.source.panes[0]['claim'] = None
        self.driver.launch.side_effect = no_claim
        with self.assertRaises(CreationRefused):
            self.sut.resume('default', 'cli', SID)
        self.assertEqual(self.sut.bindings, {})
        self.assertEqual(self.driver.launch.call_count, 1)

    def test_ambiguous_provision_does_not_retry(self):
        self.driver.provision.side_effect = CreationRefused('lost create reply')
        with self.assertRaises(CreationRefused):
            self.sut.resume('default', 'cli', SID)
        with self.assertRaises(CreationRefused):
            self.sut.resume('default', 'cli', SID)
        self.assertEqual(self.driver.provision.call_count, 1)
        self.driver.launch.assert_not_called()

    def test_existing_lease_refuses_before_provision(self):
        self.registry.write_text(json.dumps({'entries': [{'session_id': SID, 'pid': 123}]}))
        with self.assertRaises(CreationRefused):
            self.sut.resume('default', 'cli', SID)
        self.driver.provision.assert_not_called()

    def test_gateway_source_refuses_without_provision(self):
        with self.assertRaises(CreationRefused):
            self.sut.resume('default', 'telegram', SID)
        self.driver.provision.assert_not_called()

    def test_driver_rejects_live_socket_even_with_disposable_home(self):
        env = {'HOME': str(self.root), 'HERMES_HOME': str(self.root / '.hermes'),
               'PYTHONPATH': '/fixture'}
        with self.assertRaises(CreationRefused):
            HerdrCliDriver('/usr/bin/herdr', '/fixture/hermes', env,
                           Path.home() / '.config/herdr/herdr.sock', self.root,
                           self.root / 'launch.py')

    def test_new_session_cannot_claim_readiness_from_title_or_pane(self):
        with self.assertRaises(CreationRefused):
            self.sut.create('default', 'Fixture')
        self.driver.provision.assert_not_called()


if __name__ == '__main__':
    unittest.main()
