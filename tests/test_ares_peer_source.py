"""Synthetic, read-only Ares Peer source contract tests."""
import json
from contextlib import closing
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from peer.herdr_source import LocalSource, SourceError

SID = '20260925_001441_887074'


def state(session=SID):
    return {'workspaces': [{'workspace_id': 'w1', 'label': 'Project'}],
            'tabs': [{'tab_id': 'w1:t1', 'label': 'Chat'}],
            'panes': [{'pane_id': 'w1:p1', 'tab_id': 'w1:t1', 'terminal_id': 'term_1', 'workspace_id': 'w1'}],
            'agents': [{'agent': 'hermes', 'pane_id': 'w1:p1', 'tab_id': 'w1:t1',
                        'terminal_id': 'term_1', 'agent_status': 'done',
                        'agent_session': {'kind': 'id', 'agent': 'hermes', 'source': 'herdr:hermes', 'value': session}}]}


class SocketFixture:
    def __init__(self, path, snapshot, output='one\nsecret\nthree'):
        self.path, self.snapshot, self.output, self.calls = path, snapshot, output, []
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen(5)
        self.stop = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                request = json.loads(conn.makefile('rb').readline())
                self.calls.append(request)
                if request['method'] == 'session.snapshot':
                    result = {'snapshot': self.snapshot}
                elif request['method'] == 'pane.read':
                    result = {'text': self.output}
                    if getattr(self, 'replace_after_read', False):
                        self.snapshot['panes'][0]['terminal_id'] = 'term_replaced'
                        self.snapshot['agents'][0]['terminal_id'] = 'term_replaced'
                else:
                    result = {}
                try:
                    conn.sendall(json.dumps({'id': request['id'], 'result': result}).encode() + b'\n')
                except OSError:
                    pass

    def close(self):
        self.stop = True
        self.listener.close()


class LocalSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'state.db'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, '
                         'message_count INTEGER, started_at REAL, last_activity_at REAL, '
                         'cwd TEXT, git_repo_root TEXT, profile_name TEXT, transcript TEXT, archived INTEGER, hidden INTEGER)')
        self.sock = SocketFixture(self.root / 'herdr.sock', state())
        self.addCleanup(self.sock.close)
        self.proof = lambda pane: None
        self.source = LocalSource({'default': self.db}, [self.sock.path],
                                  verified_metadata=lambda pane: self.proof(pane))

    def insert(self, cwd=None, root=None):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO sessions (id,source,title,message_count,started_at,last_activity_at,cwd,git_repo_root,profile_name,transcript) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (SID, 'cli', 'Untrusted title', 3, 42., 43., cwd, root,
                          'default', 'PRIVATE TRANSCRIPT'))

    def test_unknown_workspace_and_bounded_catalog_never_expose_transcript(self):
        self.insert('/does/not/exist', '/does/not/exist')
        rows = self.source.sessions('default', limit=10000)['data']
        self.assertEqual(len(rows), 1)
        self.assertNotIn('transcript', str(rows).lower())
        self.assertNotIn('PRIVATE', str(rows))
        self.assertEqual(rows[0]['cwd'], '')
        self.assertEqual(rows[0]['git_repo_root'], '')
        self.assertEqual(rows[0]['id'], SID)
        self.assertTrue(self.source.sessions('default', limit=10000)['limit'] <= 100)

    def test_archive_and_hidden_are_filtered_before_paging(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for i in range(232):
                conn.execute('INSERT INTO sessions (id,source,title,message_count,started_at,last_activity_at,archived,hidden) VALUES (?,?,?,?,?,?,?,?)',
                             (f's{i:03}', 'cli', 'Chat', 1, 1, 500-i,
                              1 if i in (0, 108, 228) else 0, 1 if i in (5, 99, 201) else 0))
        rows = []
        offset = 0
        while True:
            page = self.source.sessions(limit=100, offset=offset)
            rows.extend(page['data'])
            if not page['has_more']:
                break
            offset += len(page['data'])
        self.assertEqual(len(rows), 229)
        self.assertEqual(len({r['id'] for r in rows}), 229)
        self.assertEqual([r['id'] for r in rows if r['archived']], ['s000', 's108', 's228'])
        self.assertTrue(all(type(r['archived']) is bool for r in rows))
        self.assertTrue(all(r['id'] not in ('s005', 's099', 's201') for r in rows))

    def test_existing_workspace_only_if_recorded(self):
        folder = self.root / 'repo'
        folder.mkdir()
        self.insert(str(folder), None)
        row = self.source.sessions('default')['data'][0]
        self.assertEqual(row['cwd'], str(folder))
        self.assertEqual(row['git_repo_root'], '')

    def test_db_errors_fail_closed(self):
        (self.root / 'missing.db').touch()
        source = LocalSource({'default': self.root / 'missing.db'}, [])
        with self.assertRaises(SourceError):
            source.sessions('default')
        with self.assertRaises(SourceError):
            self.source.sessions('../default')

    def test_unverified_herdr_claim_is_observed_only(self):
        terminal = self.source.inventory()['terminals'][0]
        self.assertEqual((terminal['sessionId'], terminal['profile'], terminal['identitySource']), ('', '', ''))
        self.assertEqual(terminal['paneId'], 'w1:p1')
        self.assertEqual(terminal['terminalId'], 'term_1')
        self.assertNotIn('Untrusted title', str(terminal))

    def test_exact_independent_proof_and_live_claim_bind(self):
        self.proof = lambda item: {'paneId': 'w1:p1', 'tabId': 'w1:t1', 'terminalId': 'term_1',
                                    'socketPath': str(self.sock.path), 'sessionId': SID, 'profile': 'default'}
        terminal = self.source.inventory()['terminals'][0]
        self.assertEqual((terminal['sessionId'], terminal['profile'], terminal['identitySource']),
                         (SID, 'default', 'verified'))
        self.sock.snapshot['agents'][0]['agent_session'] = None
        self.assertEqual(self.source.inventory()['terminals'][0]['sessionId'], '')

    def test_replaced_pane_or_terminal_blocks_read(self):
        old = self.source.inventory()['terminals'][0]
        self.sock.snapshot['panes'][0]['terminal_id'] = 'term_2'
        self.sock.snapshot['agents'][0]['terminal_id'] = 'term_2'
        with self.assertRaises(SourceError):
            self.source.read(old, lines=3)
        self.assertFalse(any(c['method'] == 'pane.read' for c in self.sock.calls))

    def test_bounded_recent_read_strips_ansi_and_caps_bytes(self):
        self.sock.output = 'before\n\x1b[31mSECRET\x1b[0m\nafter'
        target = self.source.inventory()['terminals'][0]
        result = self.source.read(target, lines=2, max_bytes=8)
        self.assertEqual(result['text'], 'SECRET\na')
        self.assertTrue(result['truncated'])
        request = next(c for c in self.sock.calls if c['method'] == 'pane.read')
        self.assertEqual(request['params'], {'pane_id': 'w1:p1', 'source': 'recent',
                                              'lines': 2, 'format': 'text', 'strip_ansi': True})

    def test_read_rejects_swap_after_read_before_returning_output(self):
        target = self.source.inventory()['terminals'][0]
        self.sock.replace_after_read = True
        with self.assertRaises(SourceError):
            self.source.read(target)
        self.assertTrue(any(c['method'] == 'pane.read' for c in self.sock.calls))

    def test_duplicate_pane_is_not_inventory_identity(self):
        self.sock.snapshot['panes'].append(dict(self.sock.snapshot['panes'][0]))
        self.assertEqual(self.source.inventory()['terminals'], [])

    def test_read_rejects_invalid_identity_and_unknown_socket(self):
        target = self.source.inventory()['terminals'][0]
        for changed in [dict(target, paneId='w1:p2'), dict(target, socketPath=str(self.root / 'other.sock'))]:
            with self.assertRaises(SourceError):
                self.source.read(changed)


if __name__ == '__main__':
    unittest.main()
