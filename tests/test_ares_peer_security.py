"""Isolated security contracts for the Ares Peer transport."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from peer.core import Identity, PeerState, PeerError
from peer.transport import PeerServer, PeerClient
from peer.control import LocalControl
from peer.provider import provider_handler
from peer.transport import _encode, MAX_MESSAGE


class PeerCatalogBounds(unittest.IsolatedAsyncioTestCase):
    async def test_large_catalog_pages_without_losing_records(self):
        class Source:
            def sessions(self, profile='default', limit=100, offset=0):
                data = [{'id': str(i), 'profile': profile, 'title': 'a' * 180,
                         'archived': i in (0, 108, 228)}
                        for i in range(offset, min(offset + limit, 229))]
                return {'data': data, 'limit': limit, 'offset': offset,
                        'has_more': offset + limit < 229}

        handler = provider_handler(Source())
        offset, ids, archived = 0, [], []
        while True:
            page = await handler('sessions', {'profile': 'default', 'limit': 100, 'offset': offset})
            self.assertLessEqual(len(_encode({'ok': True, 'result': page})), MAX_MESSAGE)
            fresh = [row['id'] for row in page['data']]
            self.assertGreater(len(fresh), 0)
            ids.extend(fresh)
            archived.extend(row['id'] for row in page['data'] if row['archived'])
            offset += len(fresh)
            if not page['has_more']:
                break
        self.assertEqual(ids, [str(i) for i in range(229)])
        self.assertEqual(archived, ['0', '108', '228'])


class PeerSecurity(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._asyncioRunner.get_loop().set_debug(False)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = PeerState(self.root / 'source')
        self.client = PeerState(self.root / 'client')
        self.calls = []
        async def inventory(method, args):
            self.calls.append((method, args))
            return {'items': ['private-result']}
        self.server = PeerServer(self.source, inventory, host='127.0.0.1', port=0)
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.url = f'wss://127.0.0.1:{self.server.port}'

    async def test_tls_12_handshake_is_refused(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        try:
            _, writer = await asyncio.open_connection('127.0.0.1', self.server.port, ssl=context)
        except (ssl.SSLError, OSError):
            return
        writer.close()
        await writer.wait_closed()
        self.fail('TLS 1.2 must never establish a peer transport')

    async def test_identity_persists_and_private_files(self):
        again = PeerState(self.root / 'source')
        self.assertEqual(self.source.identity.peer_id, again.identity.peer_id)
        self.assertRegex(self.source.identity.peer_id, r'^[0-9a-f]{32}$')
        self.assertRegex(self.source.identity.fingerprint, r'^[0-9a-f]{64}$')
        self.assertEqual(self.source.identity.fingerprint, again.identity.fingerprint)
        for p in (self.source.identity.key, self.source.identity.cert, self.source.state_file):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.source.root.stat().st_mode), 0o700)

    async def test_pin_mismatch_sends_no_code_or_token(self):
        code = self.source.arm()
        with self.assertRaises(PeerError):
            await PeerClient.pair(self.client, self.url, '0' * 64, code)
        self.assertFalse(self.source.tokens)
        self.assertEqual(self.source.pair_attempts, 0)
        self.assertEqual(self.calls, [])

    async def test_pair_once_and_authenticated_inventory(self):
        code = self.source.arm()
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, code)
        self.assertIn(peer, self.client.trusted)
        self.assertEqual(stat.S_IMODE(self.client.state_file.stat().st_mode), 0o600)
        self.assertNotIn(self.client.trusted[peer]['token'], self.source.state_file.read_text())
        result = await PeerClient(self.client).request(peer, self.url, 'inventory', {})
        self.assertEqual(result, {'items': ['private-result']})
        with self.assertRaises(PeerError):
            await PeerClient.pair(PeerState(self.root / 'third'), self.url, self.source.identity.fingerprint, code)

    async def test_expired_code_and_rate_limit(self):
        code = self.source.arm(ttl=-1)
        with self.assertRaises(PeerError):
            await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, code)
        code = self.source.arm()
        for _ in range(5):
            with self.assertRaises(PeerError):
                await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, '00000000')
        with self.assertRaises(PeerError):
            await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, code)
        self.assertEqual(self.calls, [])

    async def test_bad_token_and_revoke(self):
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        token = self.client.trusted[peer]['token']
        self.client.trusted[peer]['token'] = 'invalid-token'
        with self.assertRaises(PeerError):
            await PeerClient(self.client).request(peer, self.url, 'sessions', {})
        self.client.trusted[peer]['token'] = token
        self.assertEqual(self.calls, [])
        self.source.revoke(self.client.identity.peer_id)
        with self.assertRaises(PeerError):
            await PeerClient(self.client).request(peer, self.url, 'inventory', {})
        self.assertEqual(self.calls, [])

    async def test_request_and_response_size_bounds_and_unknown_ops(self):
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        with self.assertRaises(PeerError):
            await PeerClient(self.client).request(peer, self.url, 'read', {'x': 'x' * 70000})
        self.assertEqual(self.calls, [])
        with self.assertRaises(PeerError):
            await PeerClient(self.client).request(peer, self.url, 'input', {})
        self.assertEqual(self.calls, [])
        async def huge(method, args):
            return {'x': 'x' * 70000}
        self.server.handler = huge
        # A 70KB response exceeds the old 16KB frame bound but fits the
        # 256KB response cap that busy-host inventories require.
        result = await PeerClient(self.client).request(peer, self.url, 'read', {})
        self.assertEqual(result, {'x': 'x' * 70000})
        async def oversized(method, args):
            return {'x': 'x' * 300000}
        self.server.handler = oversized
        with self.assertRaises(PeerError):
            await PeerClient(self.client).request(peer, self.url, 'read', {})

    async def test_busy_host_inventory_exceeds_legacy_frame_bound(self):
        terminals = [{'paneId': f'w1:p{index}', 'tabId': f'w1:t{index}',
                      'terminalId': f'term_{index:012d}', 'socketPath': '/run/user/1000/herdr.sock',
                      'agentKind': 'hermes', 'status': 'idle', 'workspace': 'proj',
                      'label': str(index), 'sessionId': '', 'profile': '',
                      'identitySource': ''} for index in range(300)]
        async def busy(method, args):
            return {'ok': True, 'terminals': terminals, 'errors': [], 'complete': True}
        self.server.handler = busy
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        result = await PeerClient(self.client).request(peer, self.url, 'inventory', {})
        self.assertGreater(len(json.dumps(result)), 16384)
        self.assertEqual(len(result['terminals']), 300)

    async def test_control_socket_owner_permissions_and_local_actions(self):
        control = LocalControl(self.source, self.root / 'source' / 'control.sock')
        await control.start()
        self.addAsyncCleanup(control.close)
        self.assertEqual(stat.S_IMODE(control.path.stat().st_mode), 0o600)
        reader, writer = await asyncio.open_unix_connection(str(control.path))
        writer.write(b'{"op":"arm"}\n')
        await writer.drain()
        result = json.loads(await reader.readline())
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['result']['code']), 8)
        self.assertRegex(result['result']['code'], r'^[0-9]{8}$')
        self.assertEqual(result['result']['fingerprint'], self.source.identity.fingerprint)
        writer.close()
        await writer.wait_closed()

    async def _local(self, control, request):
        reader, writer = await asyncio.open_unix_connection(str(control.path))
        writer.write(json.dumps(request).encode() + b'\n')
        await writer.drain()
        answer = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return answer

    async def test_discover_lists_paired_offline_and_online_without_tokens(self):
        class KnownDiscovery:
            async def discover(self):
                return [{'name': 'source', 'peer_id': self.peer_id, 'address': '127.0.0.1',
                         'port': 9431, 'fingerprint': self.pin}]
        discovery = KnownDiscovery()
        discovery.peer_id = self.source.identity.peer_id
        discovery.pin = self.source.identity.fingerprint
        self.client.trusted[discovery.peer_id] = {'alias': 'node', 'pin': discovery.pin,
            'url': self.url, 'token': 'secret-test-token'}
        offline = 'f' * 32
        self.client.trusted[offline] = {'alias': 'offline', 'pin': 'f' * 64,
            'url': 'wss://127.0.0.2:1', 'token': 'another-secret'}
        self.client.save()
        control = LocalControl(self.client, discovery=discovery)
        await control.start()
        self.addAsyncCleanup(control.close)
        response = await self._local(control, {'op': 'discover'})
        self.assertTrue(response['ok'])
        peers = {peer['id']: peer for peer in response['result']['peers']}
        self.assertEqual((peers[discovery.peer_id]['alias'], peers[discovery.peer_id]['paired'],
            peers[discovery.peer_id]['online']), ('node', True, True))
        self.assertEqual((peers[offline]['alias'], peers[offline]['paired'], peers[offline]['online']),
            ('offline', True, False))
        self.assertNotIn('secret-test-token', json.dumps(response))
        self.assertNotIn('another-secret', json.dumps(response))

    async def test_alias_pair_and_rpc_contract(self):
        class KnownDiscovery:
            async def discover(self):
                return [{'peer_id': self.source_id, 'address': '127.0.0.1', 'port': self.port}]
        known = KnownDiscovery()
        known.source_id, known.port = self.source.identity.peer_id, self.server.port
        control = LocalControl(self.client, discovery=known)
        await control.start()
        self.addAsyncCleanup(control.close)
        found = await self._local(control, {'op': 'discover'})
        self.assertTrue(found['ok'])
        code = self.source.arm()
        result = await self._local(control, {'op': 'pair', 'peerId': self.source.identity.peer_id,
            'fingerprint': self.source.identity.fingerprint, 'code': code, 'alias': 'node'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['peer'], 'node')
        class InventorySource:
            def inventory(self):
                return {'terminals': [], 'complete': True}
        self.server.handler = provider_handler(InventorySource())
        result = await self._local(control, {'op': 'rpc', 'peer': 'node', 'request': {'op': 'inventory'}})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result'], {'ok': True, 'terminals': [], 'errors': [], 'complete': True})
        result = await self._local(control, {'op': 'revoke', 'peer': 'node'})
        self.assertTrue(result['ok'], result)
        result = await self._local(control, {'op': 'rpc', 'peer': 'node', 'request': {'op': 'inventory'}})
        self.assertFalse(result['ok'])

    async def test_control_revoke_invalidates_source_and_local_trust(self):
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        token = self.client.trusted[peer]['token']
        self.client.trusted[peer]['alias'] = 'node'
        self.client.save()
        control = LocalControl(self.client)
        await control.start()
        self.addAsyncCleanup(control.close)
        answer = await self._local(control, {'op': 'revoke', 'peer': 'node'})
        self.assertEqual(answer, {'ok': True, 'result': {'revoked': 'node'}})
        self.assertNotIn(peer, self.client.trusted)
        self.assertNotIn(self.client.identity.peer_id, self.source.tokens)
        self.assertNotIn(peer, PeerState(self.client.root).trusted)
        self.assertNotIn(self.client.identity.peer_id, PeerState(self.source.root).tokens)
        self.assertFalse(self.source.authorize(self.client.identity.peer_id, token))
        self.assertEqual(self.calls, [])

    async def test_offline_revoke_refuses_and_retains_local_trust(self):
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        self.client.trusted[peer]['alias'] = 'node'
        self.client.save()
        control = LocalControl(self.client)
        await control.start()
        self.addAsyncCleanup(control.close)
        await self.server.close()
        answer = await self._local(control, {'op': 'revoke', 'peer': 'node'})
        self.assertFalse(answer['ok'])
        self.assertEqual(answer['error'], 'denied')
        self.assertIn(peer, self.client.trusted)
        self.assertIn(peer, PeerState(self.client.root).trusted)
        self.assertIn(self.client.identity.peer_id, self.source.tokens)
        self.assertNotIn(self.client.trusted[peer]['token'], json.dumps(answer))
        self.assertEqual(self.calls, [])

    async def test_replayed_revoke_denied_without_provider_or_secret_leak(self):
        import websockets
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        token = self.client.trusted[peer]['token']
        request = {'op': 'revoke', 'peer_id': self.client.identity.peer_id, 'token': token, 'args': {}}
        async with websockets.connect(self.url, ssl=self._unverified_ssl(), proxy=None) as ws:
            await ws.send(json.dumps(request))
            self.assertEqual(json.loads(await ws.recv()), {'ok': True, 'result': {'revoked': True}})
        for payload in (request, {'op': 'inventory', 'peer_id': self.client.identity.peer_id,
                                  'token': token, 'args': {}}):
            async with websockets.connect(self.url, ssl=self._unverified_ssl(), proxy=None) as ws:
                await ws.send(json.dumps(payload))
                response = await ws.recv()
                self.assertEqual(json.loads(response), {'ok': False, 'error': 'denied'})
                self.assertNotIn(token.encode(), response)
        self.assertEqual(self.calls, [])
        self.assertNotIn(self.client.identity.peer_id, PeerState(self.source.root).tokens)

    async def test_malformed_or_stale_revoke_never_calls_provider(self):
        import websockets
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        token = self.client.trusted[peer]['token']
        for candidate in ({'op': 'revoke', 'peer_id': self.client.identity.peer_id,
                           'token': 'stale-token', 'args': {}},
                          {'op': 'revoke', 'peer_id': self.client.identity.peer_id,
                           'token': token, 'args': {'unexpected': True}}):
            async with websockets.connect(self.url, ssl=self._unverified_ssl(), proxy=None) as ws:
                await ws.send(json.dumps(candidate))
                response = await ws.recv()
                self.assertEqual(json.loads(response), {'ok': False, 'error': 'denied'})
                self.assertNotIn(token.encode(), response)
        self.assertTrue(self.source.authorize(self.client.identity.peer_id, token))
        self.assertEqual(self.calls, [])

    async def test_source_owner_can_revoke_by_exact_peer_id(self):
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        control = LocalControl(self.source)
        await control.start()
        self.addAsyncCleanup(control.close)
        answer = await self._local(control, {'op': 'revoke', 'peer': self.client.identity.peer_id})
        self.assertEqual(answer, {'ok': True, 'result': {'revoked': self.client.identity.peer_id}})
        self.assertNotIn(self.client.identity.peer_id, PeerState(self.source.root).tokens)
        self.assertIn(peer, self.client.trusted)
        self.assertEqual(self.calls, [])

    async def test_stale_token_revoke_refuses_without_dropping_local_trust(self):
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        valid_token = self.client.trusted[peer]['token']
        self.client.trusted[peer]['token'] = 'stale-token'
        self.client.save()
        control = LocalControl(self.client)
        await control.start()
        self.addAsyncCleanup(control.close)
        answer = await self._local(control, {'op': 'revoke', 'peer': peer})
        self.assertEqual(answer, {'ok': False, 'error': 'denied'})
        self.assertIn(peer, PeerState(self.client.root).trusted)
        self.assertTrue(self.source.authorize(self.client.identity.peer_id, valid_token))
        self.assertEqual(self.calls, [])

    async def test_pin_mismatch_revoke_sends_no_token_and_retains_trust(self):
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        token = self.client.trusted[peer]['token']
        self.client.trusted[peer]['pin'] = '0' * 64
        self.client.save()
        control = LocalControl(self.client)
        await control.start()
        self.addAsyncCleanup(control.close)
        answer = await self._local(control, {'op': 'revoke', 'peer': peer})
        self.assertEqual(answer, {'ok': False, 'error': 'denied'})
        self.assertIn(peer, PeerState(self.client.root).trusted)
        self.assertTrue(self.source.authorize(self.client.identity.peer_id, token))
        self.assertEqual(self.calls, [])

    async def test_no_remote_data_before_authentication(self):
        import websockets
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        async with websockets.connect(self.url, ssl=context) as ws:
            await ws.send(json.dumps({'op': 'inventory', 'args': {}}))
            self.assertFalse(json.loads(await ws.recv())['ok'])
        self.assertEqual(self.calls, [])

    async def test_default_provider_denies_even_authenticated_inventory(self):
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        self.server.handler = None
        with self.assertRaises(PeerError):
            await PeerClient(self.client).request(peer, self.url, 'inventory', {})

    async def test_authenticated_provider_only_and_fail_closed(self):
        from peer.herdr_source import SourceError
        class FakeSource:
            def __init__(self):
                self.calls = []
            def inventory(self):
                self.calls.append('inventory')
                return {'terminals': [], 'complete': True}
            def sessions(self, profile='default', limit=50, offset=0):
                self.calls.append('sessions')
                return {'data': [], 'has_more': False}
            def read(self, target, lines=80, max_bytes=16384):
                self.calls.append('read')
                raise SourceError('local provider unavailable')
        source = FakeSource()
        self.server.handler = provider_handler(source)
        async with __import__('websockets').connect(self.url, ssl=self._unverified_ssl()) as ws:
            await ws.send(json.dumps({'op': 'inventory', 'peer_id': self.client.identity.peer_id,
                'token': 'bad', 'args': {}}))
            self.assertFalse(json.loads(await ws.recv())['ok'])
        self.assertEqual(source.calls, [])
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        result = await PeerClient(self.client).request(peer, self.url, 'inventory', {})
        self.assertEqual(result, {'ok': True, 'terminals': [], 'errors': [], 'complete': True})
        self.assertEqual((await PeerClient(self.client).request(peer, self.url, 'sessions', {}))['data'], [])
        with self.assertRaises(PeerError):
            await PeerClient(self.client).request(peer, self.url, 'read', {'target': {}, 'lines': 80})
        self.assertEqual(source.calls, ['inventory', 'sessions', 'read'])

    def _unverified_ssl(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    async def test_serve_provider_requires_independent_session_binding_proof(self):
        from peer.__main__ import local_source
        from peer.session_binding import SessionBinding
        source = local_source()
        self.assertIsInstance(source.verified_metadata, SessionBinding)
        self.assertIsNone(source.verified_metadata({'agentKind': 'hermes', 'paneId': 'w1:p1',
            'tabId': 'w1:t1', 'terminalId': 'term', 'socketPath': '/not/a/socket'}))

    async def test_cli_read_uses_stdin_target_and_never_argv_credentials(self):
        import sys
        peer = await PeerClient.pair(self.client, self.url, self.source.identity.fingerprint, self.source.arm())
        self.client.trusted[peer]['alias'] = 'node'
        self.client.save()
        control = LocalControl(self.client)
        await control.start()
        self.addAsyncCleanup(control.close)
        env = dict(os.environ, ARES_PEER_STATE=str(self.client.root))
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'peer', 'read', 'node', '--lines', '15',
            cwd=str(Path(__file__).resolve().parents[1]), env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, error = await process.communicate(b'{"target":{"paneId":"w1:p1"}}\n')
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(json.loads(output), {'items': ['private-result']})
        self.assertEqual(self.calls, [('read', {'target': {'paneId': 'w1:p1'}, 'lines': 15, 'max_bytes': 8192})])

    async def test_discovery_advertises_metadata_without_authentication(self):
        from peer.discovery import Discovery
        discovery = Discovery(self.source, '127.0.0.1', self.server.port)
        await discovery.start()
        try:
            discovered = await discovery.discover(timeout=1)
            self.assertTrue(any(p['peer_id'] == self.source.identity.peer_id for p in discovered))
            self.assertEqual(self.calls, [])
        finally:
            await discovery.close()

    async def test_discovery_wildcard_bind_has_routable_address(self):
        from peer.discovery import Discovery
        discovery = Discovery(self.source, '0.0.0.0', self.server.port)
        await discovery.start()
        try:
            self.assertTrue(discovery.info.parsed_addresses(), 'wildcard announcement omitted service address')
            self.assertNotIn('0.0.0.0', discovery.info.parsed_addresses())
        finally:
            await discovery.close()

    async def test_discovery_stays_running_until_lan_address_appears(self):
        from peer.discovery import Discovery
        discovery = Discovery(self.source, '0.0.0.0', self.server.port)
        addresses = [PeerError('no IPv4 LAN address for mDNS advertisement'),
                     [socket.inet_aton('127.0.0.1')]]
        with patch('peer.discovery._addresses', side_effect=addresses) as lookup, \
                patch('peer.discovery.RETRY_SECONDS', 0.01, create=True):
            await discovery.start()
            try:
                self.assertIsNone(discovery.info)
                for _ in range(100):
                    if discovery.info is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(discovery.info, 'mDNS never registered after IPv4 appeared')
                self.assertEqual(lookup.call_count, 2)
            finally:
                await discovery.close()

    async def test_unsafe_state_permissions_are_rejected(self):
        os.chmod(self.source.state_file, 0o644)
        with self.assertRaises(PeerError):
            PeerState(self.root / 'source')

    async def test_local_oversized_line_is_denied(self):
        control = LocalControl(self.client)
        await control.start()
        self.addAsyncCleanup(control.close)
        reader, writer = await asyncio.open_unix_connection(str(control.path))
        writer.write(b'{"op":"arm","padding":"' + b'x' * 20000 + b'"}\n')
        await writer.drain()
        answer = json.loads(await reader.readline())
        self.assertFalse(answer['ok'])
        writer.close()
        await writer.wait_closed()
        self.assertIsNone(self.client.armed)


if __name__ == '__main__':
    unittest.main()
