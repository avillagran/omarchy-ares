"""Source-local consent only; no live pairing, Herdr, or network input."""
import asyncio
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from peer.control import LocalControl
from peer.core import PeerState
from peer.session_binding import SessionBinding
from peer.herdr_source import LocalSource, _Snapshot

PEER = 'a' * 32
TARGET = {'socketPath': '/synthetic/herdr.sock', 'paneId': 'w1:p1',
          'tabId': 'w1:t1', 'terminalId': 'term_1', 'agentKind': 'hermes',
          'sessionId': 'chat-a', 'profile': 'default', 'identitySource': 'verified',
          'terminalGeneration': 'owner-process-1'}


class Source:
    def __init__(self):
        self.items = [dict(TARGET)]
        self.complete = True

    def inventory(self):
        return {'complete': self.complete, 'terminals': self.items}


class WriteGrantTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = PeerState(Path(self.tmp.name) / 'peer')
        self.state.tokens[PEER] = 'synthetic-token-digest'
        self.state.save()
        self.source = Source()
        self.control = LocalControl(self.state, source=self.source)
        await self.control.start()
        self.addAsyncCleanup(self.control.close)

    async def local(self, **request):
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        writer.write(json.dumps(request).encode() + b'\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    async def grant(self, peer=PEER, **changes):
        return await self.local(op='write_grant', peer=peer, profile=changes.pop('profile', 'default'),
                                session_id=changes.pop('session_id', 'chat-a'),
                                terminal_id=changes.pop('terminal_id', 'term_1'),
                                allow=changes.pop('allow', True), ttl=changes.pop('ttl', 30), **changes)

    async def test_local_consent_is_explicit_short_lived_and_ephemeral(self):
        self.assertFalse(self.state.write_allowed(PEER, self.source))
        self.assertTrue((await self.grant())['ok'])
        self.assertTrue(self.state.write_allowed(PEER, self.source))
        self.assertFalse(PeerState(self.state.root).write_allowed(PEER, self.source))
        self.assertEqual((await self.grant(allow=False))['result']['write_allowed'], False)
        self.assertFalse(self.state.write_allowed(PEER, self.source))

    async def test_invalid_or_unpaired_peer_and_bad_ttl_refused(self):
        for peer, ttl in [('b'*32, 30), ('BAD', 30), (PEER, 0), (PEER, 301), (PEER, True)]:
            with self.subTest(peer=peer, ttl=ttl):
                self.assertFalse((await self.grant(peer=peer, ttl=ttl))['ok'])
        self.assertFalse(self.state.write_allowed(PEER, self.source))

    async def test_unverified_ambiguous_or_missing_generation_refused(self):
        for items in ([], [dict(TARGET, identitySource='')],
                      [dict(TARGET, terminalGeneration='')],
                      [TARGET, dict(TARGET, paneId='w1:p2', terminalId='term_2')],
                      [dict(TARGET, sessionId='other')]):
            self.source.items = items
            with self.subTest(items=items):
                self.assertFalse((await self.grant())['ok'])
        self.source.items = [TARGET]
        self.source.complete = False
        self.assertFalse((await self.grant())['ok'])

    async def test_exact_identity_generation_expiry_and_revoke_fail_closed(self):
        self.assertTrue((await self.grant(ttl=1))['ok'])
        for key, value in [('terminalGeneration', 'owner-process-2'), ('socketPath', '/other'),
                           ('paneId', 'w1:p2'), ('tabId', 'w1:t2'), ('terminalId', 'term_2'),
                           ('identitySource', '')]:
            self.source.items = [dict(TARGET, **{key: value})]
            with self.subTest(key=key):
                self.assertFalse(self.state.write_allowed(PEER, self.source))
        self.source.items = [TARGET]
        self.assertFalse(self.state.write_allowed(PEER, self.source))
        self.assertTrue((await self.grant(ttl=1))['ok'])
        await asyncio.sleep(1.05)
        self.assertFalse(self.state.write_allowed(PEER, self.source))
        self.assertTrue((await self.grant())['ok'])
        self.state.revoke(PEER)
        self.assertFalse(self.state.write_allowed(PEER, self.source))

    async def test_one_owner_only_and_pair_never_inherits_grant(self):
        self.assertTrue((await self.grant())['ok'])
        self.assertFalse((await self.grant(peer='b'*32))['ok'])
        self.state.tokens['b'*32] = 'synthetic-digest'
        self.assertFalse((await self.grant(peer='b'*32))['ok'])
        self.assertTrue(self.state.write_allowed(PEER, self.source))
        self.state.armed = {'hash': __import__('hashlib').sha256(b'12345678').hexdigest(),
                            'expires': __import__('time').time()+30}
        self.state.pair(PEER, '12345678')
        self.assertFalse(self.state.write_allowed(PEER, self.source))

    async def test_cli_uses_only_owner_socket(self):
        import os
        import sys
        env = dict(os.environ, ARES_PEER_STATE=str(self.state.root))
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'peer',
            'write-grant', PEER, 'default', 'chat-a', 'term_1', 'allow', '--ttl', '30',
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        self.assertEqual(process.returncode, 0, err.decode())
        self.assertTrue(json.loads(out)['write_allowed'])
        self.assertTrue(self.state.write_allowed(PEER, self.source))
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'peer',
            'write-grant', PEER, 'default', 'chat-a', 'term_1', 'deny',
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        self.assertEqual(process.returncode, 0, err.decode())
        self.assertFalse(json.loads(out)['write_allowed'])
        self.assertFalse(self.state.write_allowed(PEER, self.source))

    async def test_bad_binding_or_extra_control_fields_never_grant(self):
        for changes in ({'profile': 'wrong'}, {'session_id': 'wrong'},
                        {'terminal_id': 'term_2'}, {'terminal_id': '--bad'},
                        {'server_instance_id': 'forged-server'},
                        {'runtime_generation': 'forged-runtime'},
                        {'protected_control_fence_version': 1},
                        {'unexpected': 'value'}):
            with self.subTest(changes=changes):
                self.assertFalse((await self.grant(**changes))['ok'])
        self.assertFalse(self.state.write_allowed(PEER, self.source))


class GenerationProofTest(unittest.TestCase):
    def test_process_instance_generation_changes_with_verified_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'hermes'
            home.mkdir()
            db = home / 'state.db'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY)')
                connection.execute('INSERT INTO sessions VALUES (?)', ('chat-a',))
            registry = home / 'runtime' / 'active_sessions.json'
            registry.parent.mkdir()
            proc = root / 'proc'
            (proc / '123').mkdir(parents=True)
            (proc / 'stat').write_text('btime 1000\n')
            row = {'lease_id': 'lease1', 'session_id': 'chat-a', 'pid': 123,
                   'process_start_time': 1100.5,
                   'metadata': {'live_session_id': 'chat-a'}}
            registry.write_text(json.dumps({'entries': [row]}))
            (proc / '123' / 'stat').write_text('123 (hermes) S ' + ' '.join(['0']*18 + ['10050']))
            (proc / '123' / 'environ').write_bytes(b'HERDR_PANE_ID=w1:p1\0HERDR_TAB_ID=w1:t1\0HERDR_SOCKET_PATH=/synthetic/herdr.sock\0')
            proof = SessionBinding({'default': (home, db)}, proc_root=proc)
            source = LocalSource({'default': db}, ['/synthetic/herdr.sock'], verified_metadata=proof)
            claim = {'kind': 'id', 'agent': 'hermes', 'source': 'herdr:hermes',
                     'value': 'chat-a'}
            snapshot = {'protected_control_fence_version': 1, 'server_instance_id': 'server-1',
                        'panes': [{'pane_id': 'w1:p1', 'tab_id': 'w1:t1',
                                   'terminal_id': 'term_1', 'runtime_generation': 'runtime-1'}],
                        'tabs': [{'tab_id': 'w1:t1'}],
                        'agents': [{'pane_id': 'w1:p1', 'tab_id': 'w1:t1',
                                    'terminal_id': 'term_1', 'agent': 'hermes',
                                    'agent_session': claim}], 'workspaces': []}
            # Synthetic origin here isolates the process/lease hash unit; the
            # disposable Unix subprocess tests exercise real SO_PEERCRED.
            snapshot = _Snapshot(snapshot, ('synthetic-test-process',))
            # Absence or a non-exact source fence must retain observation but
            # never promote a process/epoch/runtime tuple into write proof.
            for capability in (None, False, True, '1', 0, 2, -1):
                with self.subTest(capability=capability):
                    if capability is None:
                        snapshot.pop('protected_control_fence_version', None)
                    else:
                        snapshot['protected_control_fence_version'] = capability
                    observed = source._items('/synthetic/herdr.sock', snapshot)[0]
                    self.assertEqual(observed['identitySource'], 'verified')
                    self.assertNotIn('terminalGeneration', observed)
            snapshot['protected_control_fence_version'] = 1
            first = source._items('/synthetic/herdr.sock', snapshot)[0]
            self.assertEqual(first['identitySource'], 'verified')
            self.assertTrue(first['terminalGeneration'])
            row['lease_id'] = 'lease2'
            registry.write_text(json.dumps({'entries': [row]}))
            second = source._items('/synthetic/herdr.sock', snapshot)[0]
            self.assertNotEqual(first['terminalGeneration'], second['terminalGeneration'])
            row['lease_id'] = 'lease1'
            registry.write_text(json.dumps({'entries': [row]}))
            snapshot['server_instance_id'] = 'server-2'
            changed_server = source._items('/synthetic/herdr.sock', snapshot)[0]
            self.assertNotEqual(first['terminalGeneration'], changed_server['terminalGeneration'])
            snapshot['server_instance_id'] = 'server-1'
            snapshot['panes'][0]['runtime_generation'] = 'runtime-2'
            changed_runtime = source._items('/synthetic/herdr.sock', snapshot)[0]
            self.assertNotEqual(first['terminalGeneration'], changed_runtime['terminalGeneration'])
            for key, container in [('server_instance_id', snapshot),
                                   ('runtime_generation', snapshot['panes'][0])]:
                previous = container.pop(key)
                self.assertEqual(source._items('/synthetic/herdr.sock', snapshot)[0]['identitySource'], 'verified')
                self.assertFalse(source._items('/synthetic/herdr.sock', snapshot)[0].get('terminalGeneration'))
                container[key] = previous
                container[key] = True
                self.assertFalse(source._items('/synthetic/herdr.sock', snapshot)[0].get('terminalGeneration'))
                container[key] = previous
            del snapshot['agents'][0]['agent_session']
            self.assertFalse(source._items('/synthetic/herdr.sock', snapshot)[0].get('terminalGeneration'))

    def test_changed_snapshot_during_inventory_cannot_attest_write_generation(self):
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as tmp:
            # Use a real SessionBinding instance in the test above; here the
            # metadata callback isolates snapshot freshness from process proof.
            class Proof:
                def __call__(self, pane):
                    return dict(pane, sessionId='chat-a', profile='default')
                def generation_for(self, pane):
                    return dict(pane, terminalGeneration='process-proof')
            source = LocalSource({'default': Path(tmp) / 'db'},
                                 ['/synthetic/herdr.sock'], verified_metadata=Proof())
            state = {'protected_control_fence_version': 1, 'server_instance_id': 'server-1',
                     'panes': [{'pane_id': 'w1:p1', 'tab_id': 'w1:t1',
                                'terminal_id': 'term_1', 'runtime_generation': 'runtime-1'}],
                     'tabs': [{'tab_id': 'w1:t1'}],
                     'agents': [{'pane_id': 'w1:p1', 'tab_id': 'w1:t1',
                                 'terminal_id': 'term_1', 'agent': 'hermes',
                                 'agent_session': {'kind': 'id', 'agent': 'hermes',
                                                   'source': 'herdr:hermes', 'value': 'chat-a'}}],
                     'workspaces': []}
            state = _Snapshot(state, ('synthetic-test-process',))
            peer_state = PeerState(Path(tmp) / 'peer')
            peer_state.tokens[PEER] = 'synthetic-token-digest'
            source._snapshot = lambda path: state
            del state['protected_control_fence_version']
            state['panes'][0]['protected_control_fence_version'] = 1
            state['agents'][0]['protected_control_fence_version'] = 1
            with self.assertRaisesRegex(Exception, 'terminal instance unverified'):
                peer_state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, source)
            self.assertFalse(peer_state.write_allowed(PEER, source))
            state['protected_control_fence_version'] = 1
            second = _Snapshot(deepcopy(dict(state)), state.origin)
            second['protected_control_fence_version'] = False
            snapshots = iter([state, second])
            source._snapshot = lambda path: next(snapshots)
            item = source.inventory()['terminals'][0]
            self.assertEqual(item['identitySource'], 'verified')
            self.assertFalse(item.get('terminalGeneration'))
            source._snapshot = lambda path: state
            peer_state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, source)
            self.assertTrue(peer_state.write_allowed(PEER, source))
            state['protected_control_fence_version'] = '1'
            self.assertFalse(peer_state.write_allowed(PEER, source))
            state['protected_control_fence_version'] = 1
            peer_state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, source)
            state['server_instance_id'] = 'server-2'
            self.assertFalse(peer_state.write_allowed(PEER, source))
            state['server_instance_id'] = 'server-1'
            peer_state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, source)
            state['panes'][0]['runtime_generation'] = 'runtime-2'
            self.assertFalse(peer_state.write_allowed(PEER, source))
            del state['panes'][0]['runtime_generation']
            with self.assertRaisesRegex(Exception, 'terminal instance unverified'):
                peer_state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, source)



if __name__ == '__main__':
    unittest.main()
