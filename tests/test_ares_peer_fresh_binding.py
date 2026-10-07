"""Fresh Herdr claim + Hermes lease bind exactly one Ares Peer terminal."""
import unittest

from peer.herdr_source import LocalSource
from peer.session_binding import SessionBinding
from test_ares_peer_session_binding import BindingTest, SID
from test_ares_peer_source import SocketFixture, state


class FreshBindingTest(unittest.TestCase):
    rows = BindingTest.rows
    process = BindingTest.process

    def setUp(self):
        # Reuse the synthetic process/registry/SQLite fixture, never live state.
        BindingTest.setUp(self)
        self.sock = SocketFixture(self.root / 'herdr.sock', state())
        self.addCleanup(self.sock.close)
        self.pane = {'paneId': 'w1:p1', 'tabId': 'w1:t1',
                     'terminalId': 'term_1', 'socketPath': str(self.sock.path),
                     'agentKind': 'hermes'}
        self.process(123, env={'HERDR_PANE_ID': 'w1:p1',
                               'HERDR_TAB_ID': 'w1:t1',
                               'HERDR_SOCKET_PATH': str(self.sock.path)})
        self.binding = SessionBinding({'default': (self.home, self.db)}, proc_root=self.proc)
        self.source = LocalSource({'default': self.db}, [self.sock.path],
                                  verified_metadata=self.binding)

    def test_new_claim_with_exact_active_lease_resolves_to_one_terminal(self):
        terminal = self.source.inventory()['terminals'][0]
        self.assertEqual((terminal['sessionId'], terminal['profile'],
                          terminal['identitySource'], terminal['terminalId']),
                         (SID, 'default', 'verified', 'term_1'))
        self.assertEqual(self.binding(self.pane)['sessionId'], SID)

    def test_missing_or_stale_lease_cannot_bind_claim(self):
        for rows in ([], [dict(self.row, process_start_time=1100.6)]):
            with self.subTest(rows=rows):
                self.rows(rows)
                terminal = self.source.inventory()['terminals'][0]
                self.assertEqual((terminal['sessionId'], terminal['identitySource']), ('', ''))

    def test_duplicate_owner_cannot_bind_claim(self):
        self.process(124, env={'HERDR_PANE_ID': 'w1:p2', 'HERDR_TAB_ID': 'w1:t2',
                               'HERDR_SOCKET_PATH': str(self.sock.path)})
        self.rows([self.row, dict(self.row, lease_id='lease2', pid=124)])
        terminal = self.source.inventory()['terminals'][0]
        self.assertEqual((terminal['sessionId'], terminal['identitySource']), ('', ''))

    def test_duplicate_claim_across_sockets_cannot_bind_same_session(self):
        second = SocketFixture(self.root / 'other.sock', state())
        self.addCleanup(second.close)
        source = LocalSource({'default': self.db}, [self.sock.path, second.path],
                             verified_metadata=self.binding)
        terminals = source.inventory()['terminals']
        self.assertEqual(len(terminals), 2)
        self.assertTrue(all(t['identitySource'] == '' and t['sessionId'] == '' for t in terminals))

    def test_duplicate_claim_in_other_pane_cannot_bind_same_session(self):
        self.sock.snapshot['panes'].append({'pane_id': 'w1:p2', 'tab_id': 'w1:t2',
                                             'terminal_id': 'term_2', 'workspace_id': 'w1'})
        self.sock.snapshot['tabs'].append({'tab_id': 'w1:t2', 'label': 'Other'})
        self.sock.snapshot['agents'].append({'agent': 'hermes', 'pane_id': 'w1:p2',
                                              'tab_id': 'w1:t2', 'terminal_id': 'term_2',
                                              'agent_session': {'kind': 'id', 'agent': 'hermes',
                                                                'source': 'herdr:hermes', 'value': SID}})
        terminals = self.source.inventory()['terminals']
        self.assertEqual(len(terminals), 2)
        self.assertTrue(all(t['identitySource'] == '' and t['sessionId'] == '' for t in terminals))


if __name__ == '__main__':
    unittest.main()
