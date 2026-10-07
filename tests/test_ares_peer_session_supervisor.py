"""Checkout-only SessionSupervisor against real Hermes DDL and Peer projections.

No live profile, Herdr socket, CLI process, or user input is touched.
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from peer.herdr_source import LocalSource
from peer.session_supervisor import ResumeRefused, SessionSupervisor

SID = '20260928_120000_abc123'
PANE = {'paneId': 'w1:p1', 'tabId': 'w1:t1', 'terminalId': 'term_1',
        'socketPath': '/fixture/herdr.sock'}


class SourceFixture:
    def __init__(self):
        self.panes = []
        self.removed = []
        self.on_provision = None

    def provision_empty(self):
        pane = dict(PANE)
        self.panes.append({'pane': pane, 'agent': 'shell', 'claim': None, 'proof': None})
        if self.on_provision:
            self.on_provision()
        return dict(pane)

    def inventory(self):
        # Use the actual LocalSource projection of a Herdr session.snapshot,
        # not synthetic supervisor fields (vacant, source, processId).
        state = {'workspaces': [{'workspace_id': 'w1', 'label': ''}],
                 'tabs': [{'tab_id': p['pane']['tabId'], 'label': ''} for p in self.panes],
                 'panes': [{'pane_id': p['pane']['paneId'], 'tab_id': p['pane']['tabId'],
                            'terminal_id': p['pane']['terminalId'], 'workspace_id': 'w1'}
                           for p in self.panes],
                 'agents': [{'agent': p['agent'], 'pane_id': p['pane']['paneId'],
                             'tab_id': p['pane']['tabId'], 'terminal_id': p['pane']['terminalId'],
                             'agent_session': p['claim']}
                            for p in self.panes if p['agent'] != 'shell']}
        source = LocalSource({'default': '/fixture/state.db'}, ['/fixture/herdr.sock'],
            verified_metadata=lambda item: next((p['proof'] for p in self.panes
                if p['pane']['paneId'] == item['paneId']), None))
        return {'complete': True, 'terminals': source._items('/fixture/herdr.sock', state)}

    def remove(self, pane):
        self.removed.append(pane)

    def add_claim(self, *, value=SID, proof=True):
        row = self.panes[0]
        row['agent'] = 'hermes'
        row['claim'] = {'kind': 'id', 'agent': 'hermes',
                        'source': 'herdr:hermes', 'value': value}
        row['proof'] = {**row['pane'], 'profile': 'default', 'sessionId': SID} if proof else None


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = self.home / 'state.db'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(Path(__file__).with_name('fixture_hermes_session_schema.sql').read_text())
            db.execute('INSERT INTO sessions (id,source,profile_name,started_at,message_count) '
                       'VALUES (?,?,?,?,?)', (SID, 'cli', 'default', 1.0, 2))
            db.executemany('INSERT INTO messages (session_id,role,content,timestamp) '
                           'VALUES (?,?,?,?)', [(SID, 'user', 'hello', 1.0),
                                                 (SID, 'assistant', 'world', 2.0)])
        self.registry = self.home / 'runtime' / 'active_sessions.json'
        self.registry.parent.mkdir()
        self.registry.write_text('{"entries": []}')
        self.herdr = SourceFixture()
        self.spawned = []
        self.authorized = True
        self.supervisor = SessionSupervisor({'default': (self.home, self.db)}, self.herdr,
            lambda *args: self.spawned.append(args), process_birth=lambda pid: None,
            source_authorized=lambda profile, source, sid: self.authorized)

    def refuse(self):
        with self.assertRaises(ResumeRefused):
            self.supervisor.resume('default', 'cli', SID)
        self.assertEqual(self.supervisor.bindings, {})
        self.assertEqual(self.spawned, [])

    def test_actual_sqlite_integer_ids_and_compacted_summary_count(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE sessions SET message_count=99 WHERE id=?', (SID,))
            db.execute('UPDATE messages SET _compressed_summary=1 WHERE id=1')
            db.execute('UPDATE messages SET role=?,tool_calls=?,content=NULL WHERE id=2',
                       ('assistant', '[{"name":"tool"}]'))
        count, tip, digest = self.supervisor._history('default', 'cli', SID)
        self.assertEqual((count, tip, len(digest)), (2, 2, 64))

    def test_digest_binds_tool_calls_api_content_reasoning_and_inactive_rows(self):
        before = self.supervisor._history('default', 'cli', SID)
        for field, value in [('tool_calls', '[{"name":"x"}]'), ('api_content', 'private'),
                             ('reasoning', 'private'), ('active', 0), ('_compressed_summary', 1)]:
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute(f'UPDATE messages SET {field}=? WHERE id=1', (value,))
            after = self.supervisor._history('default', 'cli', SID)
            self.assertNotEqual(before[2], after[2], field)
            before = after

    def test_peer_projection_never_claims_vacancy_or_process_or_source(self):
        self.herdr.provision_empty()
        observed = self.herdr.inventory()['terminals'][0]
        self.assertEqual(observed['agentKind'], 'shell')
        self.assertFalse({'vacant', 'processId', 'processStartTime', 'source'} & observed.keys())

    def test_source_claim_requires_independent_proof_and_herdr_agent_session(self):
        pane = self.herdr.provision_empty()
        self.herdr.add_claim(proof=False)
        with self.assertRaises(ResumeRefused):
            self.supervisor._source_claim(pane, 'default', SID)
        self.herdr.add_claim(value='different')
        with self.assertRaises(ResumeRefused):
            self.supervisor._source_claim(pane, 'default', SID)
        self.herdr.add_claim()
        claim = self.supervisor._source_claim(pane, 'default', SID)
        self.assertEqual((claim['identitySource'], claim['sessionId']), ('verified', SID))
        self.assertNotIn('processId', claim)

    def test_missing_vacancy_and_readiness_contract_blocks_before_provision(self):
        self.refuse()
        self.assertEqual(self.herdr.panes, [])
        self.assertEqual(self.herdr.removed, [])
        self.assertEqual(self.supervisor.quarantine, set())

    def test_existing_lease_refuses_before_provision(self):
        self.registry.write_text(json.dumps({'entries': [{'session_id': SID}]}))
        self.refuse()
        self.assertEqual(self.herdr.panes, [])

    def test_gateway_boolean_cannot_authorize_resume(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE sessions SET source=? WHERE id=?', ('telegram', SID))
        self.authorized = True  # Boolean is not an authenticated gateway handoff.
        with self.assertRaises(ResumeRefused):
            self.supervisor.resume('default', 'telegram', SID)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.herdr.panes, [])
        self.assertEqual(self.supervisor.bindings, {})

    def test_duplicate_or_redirected_session_refuses_before_provision(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO sessions (id,source,profile_name,started_at,parent_session_id) '
                       'VALUES (?,?,?,?,?)', ('descendant', 'cli', 'default', 2.0, SID))
        self.refuse()
        self.assertEqual(self.herdr.panes, [])

    def test_missing_messages_refuses_before_provision(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DELETE FROM messages WHERE session_id=?', (SID,))
        self.refuse()
        self.assertEqual(self.herdr.panes, [])

    def test_authorization_denied_refuses_before_provision(self):
        self.authorized = False
        self.refuse()
        self.assertEqual(self.herdr.panes, [])


if __name__ == '__main__':
    unittest.main()
