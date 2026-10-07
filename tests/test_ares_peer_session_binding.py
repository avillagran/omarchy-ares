"""Synthetic tests for independently verified, read-only Hermes ownership."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from peer.session_binding import SessionBinding

SID = '20260925_001441_887074'
PANE = {'paneId': 'w1:p1', 'tabId': 'w1:t1', 'terminalId': 'term_1',
        'socketPath': '/run/user/1000/herdr.sock', 'agentKind': 'hermes'}


class BindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        (self.proc / 'stat').write_text('btime 1000\n')
        self.home = self.root / 'default'
        self.db = self.home / 'state.db'
        self.home.mkdir()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)')
            db.execute('INSERT INTO sessions VALUES (?, ?)', (SID, 'cli'))
        self.registry = self.home / 'runtime' / 'active_sessions.json'
        self.registry.parent.mkdir()
        self.row = {'lease_id': 'lease1', 'session_id': SID, 'pid': 123,
                    'process_start_time': 1100.5,
                    'metadata': {'live_session_id': SID}, 'surface': 'cli'}
        self.rows([self.row])
        self.process(123, start=10050)
        self.binding = SessionBinding({'default': (self.home, self.db)}, proc_root=self.proc)

    def rows(self, entries):
        self.registry.write_text(json.dumps({'entries': entries}))

    def process(self, pid, start=10050, env=None):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        # Field 22 is the twentieth field after the parenthesized command.
        (directory / 'stat').write_text(f'{pid} (hermes agent) S ' + ' '.join(['0'] * 18 + [str(start)]) + '\n')
        env = env if env is not None else {'HERDR_PANE_ID': PANE['paneId'],
            'HERDR_TAB_ID': PANE['tabId'], 'HERDR_SOCKET_PATH': PANE['socketPath']}
        (directory / 'environ').write_bytes(b'\0'.join((k + '=' + v).encode() for k, v in env.items()) + b'\0')

    def test_verified_exact_binding(self):
        self.assertEqual(self.binding(PANE), {**{k: PANE[k] for k in
            ('paneId', 'tabId', 'terminalId', 'socketPath')}, 'sessionId': SID, 'profile': 'default'})

    def test_does_not_mutate_registry(self):
        before = self.registry.read_bytes()
        self.binding(PANE)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_rejects_stale_pid(self):
        (self.proc / '123' / 'stat').unlink()
        self.assertIsNone(self.binding(PANE))

    def test_rejects_pid_reuse(self):
        self.process(123, start=11050)
        self.assertIsNone(self.binding(PANE))

    def test_reads_boot_time_after_many_cpu_stat_lines(self):
        (self.proc / 'stat').write_text('cpu0 ' + '0 ' * 5000 + '\nbtime 1000\n')
        self.assertEqual(self.binding(PANE)['sessionId'], SID)

    def test_rejects_missing_live_session_id(self):
        self.row['metadata'] = {}
        self.rows([self.row])
        self.assertIsNone(self.binding(PANE))

    def test_rejects_mismatched_live_session_id(self):
        self.row['metadata']['live_session_id'] = 'different'
        self.rows([self.row])
        self.assertIsNone(self.binding(PANE))

    def test_rejects_duplicate_owner(self):
        other = dict(self.row, lease_id='lease2', pid=124)
        self.process(124)
        self.rows([self.row, other])
        self.assertIsNone(self.binding(PANE))

    def test_rejects_second_owner_of_same_session_on_different_pane(self):
        other = dict(self.row, lease_id='lease2', pid=124)
        self.process(124, env={'HERDR_PANE_ID': 'w1:p2', 'HERDR_TAB_ID': 'w1:t2',
                               'HERDR_SOCKET_PATH': PANE['socketPath']})
        self.rows([self.row, other])
        self.assertIsNone(self.binding(PANE))

    def test_rejects_missing_environ(self):
        (self.proc / '123' / 'environ').unlink()
        self.assertIsNone(self.binding(PANE))

    def test_rejects_mismatched_environment(self):
        self.process(123, env={'HERDR_PANE_ID': 'w1:p2', 'HERDR_TAB_ID': PANE['tabId'],
                               'HERDR_SOCKET_PATH': PANE['socketPath']})
        self.assertIsNone(self.binding(PANE))

    def test_rejects_missing_environment_field(self):
        self.process(123, env={'HERDR_PANE_ID': PANE['paneId'], 'HERDR_TAB_ID': PANE['tabId']})
        self.assertIsNone(self.binding(PANE))

    def test_rejects_duplicate_environment_field(self):
        with (self.proc / '123' / 'environ').open('ab') as handle:
            handle.write(b'HERDR_PANE_ID=w1:p1\0')
        self.assertIsNone(self.binding(PANE))

    def test_rejects_terminal_instance_change(self):
        self.assertIsNone(self.binding(dict(PANE, terminalId='')))
        self.assertIsNone(self.binding(dict(PANE, terminalId='term_2', agentKind='shell')))

    def test_rejects_non_hermes_agent(self):
        self.assertIsNone(self.binding(dict(PANE, agentKind='shell')))

    def test_rejects_missing_db_session(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DELETE FROM sessions')
        self.assertIsNone(self.binding(PANE))

    def test_accepts_resumed_desktop_history_with_live_cli_owner(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE sessions SET source = ?', ('desktop',))
        self.assertEqual(self.binding(PANE)['sessionId'], SID)

    def test_rejects_malformed_registry(self):
        for content in ('{broken', '{}', '{"entries": [null]}',
                        '{"entries": [{"session_id": "x"}]}'):
            with self.subTest(content=content):
                self.registry.write_text(content)
                self.assertIsNone(self.binding(PANE))

    def test_gateway_routing_rows_do_not_blind_valid_cli_owner(self):
        # Hermes records gateway routing leases (colon session keys, no
        # live_session_id) in the same registry; they are never pane proof
        # but must not disable verification of every real CLI owner.
        gateway = {'lease_id': 'lease-gw', 'session_id': 'agent:main:telegram:group:-100:1',
                   'pid': 123, 'process_start_time': 1100.5, 'metadata': 'routing'}
        self.rows([self.row, gateway])
        self.assertEqual(self.binding(PANE)['sessionId'], SID)

    def test_skipped_rows_still_cannot_forge_a_second_owner(self):
        other = dict(self.row, lease_id='lease2', pid=124)
        self.process(124)
        gateway = {'lease_id': 'lease-gw', 'session_id': 'agent:main:telegram:group:-100:1',
                   'pid': 125, 'process_start_time': 1100.5, 'metadata': None}
        self.rows([self.row, other, gateway])
        self.assertIsNone(self.binding(PANE))

    def test_rejects_profile_mismatch(self):
        other_home = self.root / 'other'
        other_home.mkdir()
        self.binding = SessionBinding({'other': (other_home, self.db)}, proc_root=self.proc)
        self.assertIsNone(self.binding(PANE))

    def test_rejects_cross_profile_duplicate_session(self):
        other_home = self.root / 'other'
        other_home.mkdir()
        other_db = other_home / 'state.db'
        with closing(sqlite3.connect(other_db)) as db, db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)')
            db.execute('INSERT INTO sessions VALUES (?, ?)', (SID, 'cli'))
        self.process(124)
        other_reg = other_home / 'runtime' / 'active_sessions.json'
        other_reg.parent.mkdir()
        other_reg.write_text(json.dumps({'entries': [dict(self.row, lease_id='lease2', pid=124)]}))
        self.binding = SessionBinding({'default': (self.home, self.db),
                                       'other': (other_home, other_db)}, proc_root=self.proc)
        self.assertIsNone(self.binding(PANE))

    def test_rejects_ambiguous_terminal_owner(self):
        self.process(124)
        second = dict(self.row, lease_id='lease2', session_id='other', pid=124,
                      metadata={'live_session_id': 'other'})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO sessions VALUES (?, ?)', ('other', 'cli'))
        self.rows([self.row, second])
        self.assertIsNone(self.binding(PANE))

    def test_rejects_registry_change_after_db_check(self):
        for changes in ([], [dict(self.row, lease_id="lease2")],
                        [dict(self.row, process_start_time=1100.51)],
                        [dict(self.row, surface="desktop")]):
            for generation in (False, True):
                with self.subTest(changes=changes, generation=generation):
                    self.rows([self.row])

                    class Changed(SessionBinding):
                        def _stored(binding, db_path, sid):
                            stored = super()._stored(db_path, sid)
                            self.rows(changes)
                            return stored

                    binding = Changed({"default": (self.home, self.db)}, proc_root=self.proc)
                    self.assertIsNone(binding.generation_for(PANE) if generation else binding(PANE))

    def test_rejects_process_replacement_during_observation(self):
        class Swapped(SessionBinding):
            def _environment(self, pid):
                env = super()._environment(pid)
                self._proc_stat_path(pid).write_text('123 (hermes agent) S ' + ' '.join(['0'] * 18 + ['11050']))
                return env
        self.binding = Swapped({'default': (self.home, self.db)}, proc_root=self.proc)
        self.assertIsNone(self.binding(PANE))


if __name__ == '__main__':
    unittest.main()
