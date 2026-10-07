"""Synthetic regression for a Herdr snapshot without agent_session claims.

No live socket, process, registry, or session database is touched.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from peer.herdr_source import LocalSource
from peer.session_binding import SessionBinding


class MissingClaimTest(unittest.TestCase):
    def test_live_process_proof_cannot_replace_missing_herdr_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'profile'
            home.mkdir()
            db_path = home / 'state.db'
            proc = root / 'proc'
            proc.mkdir()
            (proc / 'stat').write_text('btime 1000\n')
            socket_path = str(root / 'herdr.sock')
            agents = []
            entries = []
            panes = []
            tabs = []
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY)')
                for index in range(43):
                    pane = f'w1:p{index + 1}'
                    tab = f'w1:t{index + 1}'
                    terminal = f'term_{index + 1}'
                    panes.append({'pane_id': pane, 'tab_id': tab, 'terminal_id': terminal})
                    tabs.append({'tab_id': tab})
                    if index >= 3:
                        continue
                    sid = f'synthetic_{index + 1}'
                    pid = 200 + index
                    db.execute('INSERT INTO sessions VALUES (?)', (sid,))
                    directory = proc / str(pid)
                    directory.mkdir()
                    # /proc stat field 22 is the twentieth after the command.
                    (directory / 'stat').write_text(
                        f'{pid} (hermes agent) S ' + ' '.join(['0'] * 18 + ['10050']) + '\n')
                    (directory / 'environ').write_bytes(b'\0'.join(
                        (key + '=' + value).encode() for key, value in (
                            ('HERDR_PANE_ID', pane), ('HERDR_TAB_ID', tab),
                            ('HERDR_SOCKET_PATH', socket_path))) + b'\0')
                    entries.append({'lease_id': f'lease_{index}', 'session_id': sid,
                                    'pid': pid, 'process_start_time': 1100.5,
                                    'metadata': {'live_session_id': sid}})
                    # Real Herdr snapshots can omit agent_session entirely.
                    agents.append({'agent': 'hermes', 'pane_id': pane,
                                   'tab_id': tab, 'terminal_id': terminal})
            registry = home / 'runtime' / 'active_sessions.json'
            registry.parent.mkdir()
            registry.write_text(json.dumps({'entries': entries}))
            binding = SessionBinding({'default': (home, db_path)}, proc_root=proc)
            source = LocalSource({'default': db_path}, [socket_path], verified_metadata=binding)
            state = {'panes': panes, 'tabs': tabs, 'agents': agents, 'workspaces': []}

            # Independent process and registry proof exists for all three agents.
            observed = source._items(socket_path, state)
            self.assertEqual(len(observed), 43)
            self.assertTrue(all(binding(item)['sessionId'] == entries[i]['session_id']
                                for i, item in enumerate(observed[:3])))
            self.assertTrue(all(item['identitySource'] == '' and item['sessionId'] == ''
                                for item in observed))

            # A real, exact Herdr claim crosses the remaining proof boundary.
            agents[0]['agent_session'] = {'kind': 'id', 'agent': 'hermes',
                                          'source': 'herdr:hermes', 'value': entries[0]['session_id']}
            observed = source._items(socket_path, state)
            self.assertEqual((observed[0]['sessionId'], observed[0]['profile'],
                              observed[0]['identitySource']),
                             (entries[0]['session_id'], 'default', 'verified'))
            self.assertTrue(all(item['identitySource'] == '' for item in observed[1:]))


if __name__ == '__main__':
    unittest.main()
