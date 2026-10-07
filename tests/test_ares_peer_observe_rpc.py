"""Isolated, authenticated source-side observation RPC contracts."""
import asyncio
import base64
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from peer.control import LocalControl
from peer.core import PeerState, PeerError
from peer.observe import ObserveAdapter
from peer.provider import provider_handler
from peer.transport import PeerClient, PeerServer, _encode


class VerifiedSource:
    def __init__(self, terminal):
        self.terminal = terminal
        self.calls = 0

    def inventory(self):
        self.calls += 1
        return {'complete': True, 'terminals': [self.terminal]}


class ObserveRPC(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source_state = PeerState(root / 'source')
        self.client_state = PeerState(root / 'client')
        self.target = {'socketPath': str(root / 'herdr.sock'), 'paneId': 'w1:p1',
                       'tabId': 'w1:t1', 'terminalId': 'term_1', 'agentKind': 'hermes',
                       'sessionId': 'chat-a', 'profile': 'default', 'identitySource': 'verified'}
        self.source = VerifiedSource(self.target)
        cli = root / 'fake-herdr'
        cli.write_text('#!/usr/bin/env python3\nimport os\nos.write(1,b"frame\\n")\n')
        cli.chmod(0o700)
        self.adapter = ObserveAdapter(self.source, str(cli))
        self.server = PeerServer(self.source_state,
            handler=provider_handler(self.source, observer=self.adapter), host='127.0.0.1')
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.url = f'wss://127.0.0.1:{self.server.port}'
        self.peer = await PeerClient.pair(self.client_state, self.url,
            self.source_state.identity.fingerprint, self.source_state.arm())
        self.client_state.trusted[self.peer]['alias'] = 'source'
        self.client_state.save()
        self.control = LocalControl(self.source_state)
        await self.control.start()
        self.addAsyncCleanup(self.control.close)

    async def local(self, request):
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        writer.write(json.dumps(request).encode() + b'\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    async def request(self, args=None):
        return await PeerClient(self.client_state).request(self.peer, self.url, 'observe',
            {'profile': 'default', 'session_id': 'chat-a', 'cols': 80, 'rows': 24}
            if args is None else args)

    async def test_pairing_does_not_grant_observation_and_owner_can_toggle_persistently(self):
        with self.assertRaises(PeerError):
            await self.request()
        self.assertEqual(self.source.calls, 0)
        self.assertTrue((await self.local({'op': 'observe_permission', 'peer': self.client_state.identity.peer_id,
                                           'allow': True}))['ok'])
        self.assertTrue(PeerState(self.source_state.root).observe_allowed(self.client_state.identity.peer_id))
        self.assertEqual(stat.S_IMODE(self.source_state.state_file.stat().st_mode), 0o600)
        result = await self.request()
        self.assertEqual(set(result), {'profile', 'session_id', 'terminal_id', 'encoding', 'frame', 'cols', 'rows'})
        self.assertEqual(base64.b64decode(result['frame'], validate=True), b'frame\r\n')
        self.assertEqual((result['profile'], result['session_id'], result['terminal_id']),
                         ('default', 'chat-a', 'term_1'))
        self.assertTrue((await self.local({'op': 'observe_permission', 'peer': self.client_state.identity.peer_id,
                                           'allow': False}))['ok'])
        with self.assertRaises(PeerError):
            await self.request()

    async def test_refusal_before_provider_for_wrong_token_or_unpaired_or_bad_request(self):
        self.source_state.set_observe_permission(self.client_state.identity.peer_id, True)
        token = self.client_state.trusted[self.peer]['token']
        self.client_state.trusted[self.peer]['token'] = 'invalid'
        with self.assertRaises(PeerError):
            await self.request()
        self.client_state.trusted[self.peer]['token'] = token
        for args in ({'profile': 'default', 'session_id': 'chat-a', 'cols': 80, 'rows': 24, 'input': 'x'},
                     {'profile': 'default', 'session_id': 'chat-a', 'cols': True, 'rows': 24},
                     {'profile': 'default', 'session_id': 'chat-a', 'cols': 4097, 'rows': 24}):
            with self.assertRaises(PeerError):
                await self.request(args)
        self.assertEqual(self.source.calls, 0)
        self.source_state.revoke(self.client_state.identity.peer_id)
        self.assertFalse(PeerState(self.source_state.root).observe_allowed(self.client_state.identity.peer_id))
        with self.assertRaises(PeerError):
            await self.request()

    async def test_exact_unique_verified_owner_required(self):
        self.source_state.set_observe_permission(self.client_state.identity.peer_id, True)
        for inventory in ({'complete': False, 'terminals': [self.target]},
                          {'complete': True, 'terminals': [dict(self.target, identitySource='')]},
                          {'complete': True, 'terminals': [self.target, dict(self.target, paneId='w1:p2')]},
                          {'complete': True, 'terminals': []}):
            self.source.inventory = lambda inventory=inventory: inventory
            with self.assertRaises(PeerError):
                await self.request()
        self.source.inventory = lambda: {'complete': True, 'terminals': [self.target]}
        with self.assertRaises(PeerError):
            await self.request({'profile': 'default', 'session_id': 'other', 'cols': 80, 'rows': 24})

    async def test_incomplete_or_error_inventory_returns_no_frame(self):
        self.source_state.set_observe_permission(self.client_state.identity.peer_id, True)
        for inventory in ({'complete': True, 'errors': ['stale'], 'terminals': [self.target]},
                          {'complete': False, 'errors': [], 'terminals': [self.target]}):
            self.source.inventory = lambda inventory=inventory: inventory
            with self.assertRaises(PeerError):
                await self.request()

    async def test_revoke_during_observation_suppresses_result(self):
        self.source_state.set_observe_permission(self.client_state.identity.peer_id, True)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.server.handler
        async def delayed(method, args):
            entered.set()
            await release.wait()
            return await original(method, args)
        self.server.handler = delayed
        pending = asyncio.create_task(self.request())
        await entered.wait()
        self.source_state.revoke(self.client_state.identity.peer_id)
        release.set()
        with self.assertRaises(PeerError):
            await pending
        self.assertGreaterEqual(self.source.calls, 1)

    async def test_repair_does_not_restore_old_observation_grant(self):
        self.source_state.set_observe_permission(self.client_state.identity.peer_id, True)
        await PeerClient.pair(self.client_state, self.url, self.source_state.identity.fingerprint,
                              self.source_state.arm())
        self.assertFalse(self.source_state.observe_allowed(self.client_state.identity.peer_id))
        with self.assertRaises(PeerError):
            await self.request()

    async def test_owner_change_after_capture_drops_buffered_frame(self):
        self.source_state.set_observe_permission(self.client_state.identity.peer_id, True)
        def inventory():
            self.source.calls += 1
            terminal = self.target if self.source.calls < 3 else dict(self.target, terminalId='replaced')
            return {'complete': True, 'errors': [], 'terminals': [terminal]}
        self.source.inventory = inventory
        with self.assertRaises(PeerError):
            await self.request()
        self.assertGreaterEqual(self.source.calls, 3)

    async def test_wire_bound_refuses_oversized_output_without_partial_frame(self):
        self.source_state.set_observe_permission(self.client_state.identity.peer_id, True)
        self.adapter.observe = lambda *a, **kw: iter([b'x' * 8193])
        with self.assertRaises(PeerError):
            await self.request()


if __name__ == '__main__':
    unittest.main()
