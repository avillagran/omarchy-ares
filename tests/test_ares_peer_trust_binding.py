"""Private Peer status binds an alias to one exact pairing, not an alias alone."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from peer.control import LocalControl


PEER = '1' * 32
PIN = '2' * 64
TOKEN_A = 'A' * 42 + 'A'
TOKEN_B = 'B' * 42 + 'A'


class TrustBindingStatus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'private'
        root.mkdir(mode=0o700)
        self.state = SimpleNamespace(root=root, identity=SimpleNamespace(
            peer_id='3' * 32, fingerprint='4' * 64), trusted={}, tokens={})
        self.control = LocalControl(self.state)
        await self.control.start()
        self.addAsyncCleanup(self.control.close)

    async def call(self, request):
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        try:
            writer.write(json.dumps(request).encode() + b'\n')
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()

    async def status(self):
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        try:
            writer.write(b'{"op":"status"}\n')
            await writer.drain()
            response = await reader.readline()
            return json.loads(response)
        finally:
            writer.close()
            await writer.wait_closed()

    def trust(self, peer=PEER, alias='source', pin=PIN, token=TOKEN_A):
        self.state.trusted[peer] = {'alias': alias, 'pin': pin,
                                    'token': token, 'url': 'wss://private.invalid:9431'}

    async def test_status_contains_stable_domain_separated_binding_without_secrets(self):
        self.trust()
        first = await self.status()
        second = await self.status()
        self.assertTrue(first['ok'])
        result = first['result']
        expected = hashlib.sha256(b'ares-peer/trusted-binding/v1\0' +
                                  PEER.encode() + b'\0' + PIN.encode() + b'\0' + TOKEN_A.encode()).hexdigest()
        self.assertEqual(result['trusted_bindings'], {'source': expected})
        self.assertEqual(first, second)
        self.assertEqual(result['trusted'], ['source'])
        self.assertNotIn(TOKEN_A, json.dumps(result))
        self.assertNotIn('private.invalid', json.dumps(result))

    async def test_revoke_and_repair_same_alias_changes_binding(self):
        self.trust()
        original = (await self.status())['result']['trusted_bindings']['source']
        del self.state.trusted[PEER]
        revoked = (await self.status())['result']
        self.assertEqual(revoked['trusted_bindings'], {})
        self.trust(token=TOKEN_B)
        repaired = (await self.status())['result']
        self.assertEqual(repaired['trusted'], ['source'])
        self.assertNotEqual(repaired['trusted_bindings']['source'], original)

    async def test_duplicate_aliases_have_no_binding_but_remain_in_legacy_inventory(self):
        self.trust()
        self.trust(peer='5' * 32, token=TOKEN_B)
        result = (await self.status())['result']
        self.assertEqual(result['trusted'], ['source', 'source'])
        self.assertEqual(result['trusted_bindings'], {})

    async def test_missing_or_invalid_token_fails_closed_only_for_binding(self):
        for token in (None, '', 'short', '!' * 43, 'a' * 43):
            with self.subTest(token_shape=repr(token)[:12]):
                self.trust(token=token)
                result = (await self.status())['result']
                self.assertEqual(result['trusted'], ['source'])
                self.assertEqual(result['trusted_bindings'], {})

    async def test_malformed_pair_identity_or_pin_fails_closed(self):
        for peer, pin in (('not-an-id', PIN), (PEER, 'bad-pin')):
            with self.subTest(peer=peer, pin_length=len(pin)):
                self.state.trusted.clear()
                self.trust(peer=peer, pin=pin)
                result = (await self.status())['result']
                self.assertEqual(result['trusted'], ['source'])
                self.assertEqual(result['trusted_bindings'], {})


class TrustAliasSafety(TrustBindingStatus):
    async def test_duplicate_alias_refuses_rpc_and_revoke_without_remote_calls(self):
        self.trust()
        self.trust(peer='5' * 32, token=TOKEN_B)
        calls = []
        async def request(*args):
            calls.append(('rpc', args))
            return {'private': True}
        async def revoke(*args):
            calls.append(('revoke', args))
        self.control.remote = SimpleNamespace(request=request, revoke=revoke)
        for op in ({'op': 'rpc', 'peer': 'source', 'request': {'op': 'inventory'}},
                   {'op': 'revoke', 'peer': 'source'}):
            answer = await self.call(op)
            self.assertFalse(answer['ok'], answer)
        self.assertEqual(calls, [])
        self.assertEqual(set(self.state.trusted), {PEER, '5' * 32})

    async def test_alias_matching_another_peer_id_refuses_rpc_and_revoke(self):
        other = '5' * 32
        self.trust(alias=other)
        self.trust(peer=other, alias='other', token=TOKEN_B)
        self.state.tokens = {other: 'token'}
        calls = []
        async def remote(*args):
            calls.append(args)
            return {'private': True}
        self.control.remote = SimpleNamespace(request=remote, revoke=remote)
        for op in ({'op': 'rpc', 'peer': other, 'request': {'op': 'inventory'}},
                   {'op': 'revoke', 'peer': other}):
            answer = await self.call(op)
            self.assertFalse(answer['ok'], answer)
        self.assertEqual(calls, [])
        self.assertEqual(set(self.state.trusted), {PEER, other})

    async def test_concurrent_same_alias_pair_never_enters_second_network_pair(self):
        first_started = asyncio.Event()
        release = asyncio.Event()
        attempts = []
        other = '5' * 32
        self.control.discovered = {
            PEER: {'address': '127.0.0.1', 'port': 9431},
            other: {'address': '127.0.0.2', 'port': 9431}}
        self.state.save = lambda: None
        async def pair(state, url, pin, code, expected_peer):
            attempts.append(expected_peer)
            if expected_peer == PEER:
                first_started.set()
                await release.wait()
            state.trusted[expected_peer] = {'url': url, 'pin': pin, 'token': TOKEN_A}
            return expected_peer
        def req(peer):
            return {'op': 'pair', 'peer': peer, 'fingerprint': PIN,
                    'code': '12345678', 'alias': 'source'}
        with patch('peer.control.PeerClient.pair', side_effect=pair):
            first = asyncio.create_task(self.call(req(PEER)))
            await asyncio.wait_for(first_started.wait(), 2)
            try:
                second = await asyncio.wait_for(self.call(req(other)), 2)
                self.assertFalse(second['ok'], second)
                self.assertEqual(attempts, [PEER])
            finally:
                release.set()
            self.assertTrue((await asyncio.wait_for(first, 2))['ok'])
        self.assertEqual(self.state.trusted[PEER]['alias'], 'source')
        self.assertNotIn(other, self.state.trusted)

    async def test_pair_refuses_alias_equal_to_existing_different_peer_id(self):
        self.trust(alias='original')
        other = '5' * 32
        self.control.discovered = {other: {'address': '127.0.0.1', 'port': 9431}}
        with patch('peer.control.PeerClient.pair') as pair:
            answer = await self.call({'op': 'pair', 'peer': other, 'fingerprint': PIN,
                                      'code': '12345678', 'alias': PEER})
        self.assertFalse(answer['ok'], answer)
        pair.assert_not_called()

    async def test_failed_pair_releases_alias_for_retry(self):
        from peer.core import PeerError
        other = '5' * 32
        self.control.discovered = {PEER: {'address': '127.0.0.1', 'port': 9431},
                                   other: {'address': '127.0.0.2', 'port': 9431}}
        self.state.save = lambda: None
        calls = []
        async def pair(state, url, pin, code, expected_peer):
            calls.append(expected_peer)
            if expected_peer == PEER:
                raise PeerError('network pairing failed')
            state.trusted[expected_peer] = {'url': url, 'pin': pin, 'token': TOKEN_A}
            return expected_peer
        def req(peer):
            return {'op': 'pair', 'peer': peer, 'fingerprint': PIN,
                    'code': '12345678', 'alias': 'source'}
        with patch('peer.control.PeerClient.pair', side_effect=pair):
            self.assertFalse((await self.call(req(PEER)))['ok'])
            self.assertTrue((await self.call(req(other)))['ok'])
        self.assertEqual(calls, [PEER, other])
        self.assertEqual(self.state.trusted[other]['alias'], 'source')

    async def test_cancelled_pair_releases_alias_for_retry(self):
        started = asyncio.Event()
        other = '5' * 32
        self.control.discovered = {PEER: {'address': '127.0.0.1', 'port': 9431},
                                   other: {'address': '127.0.0.2', 'port': 9431}}
        self.state.save = lambda: None
        async def pair(state, url, pin, code, expected_peer):
            if expected_peer == PEER:
                started.set()
                await asyncio.Future()
            state.trusted[expected_peer] = {'url': url, 'pin': pin, 'token': TOKEN_A}
            return expected_peer
        def req(peer):
            return {'op': 'pair', 'peer': peer, 'fingerprint': PIN,
                    'code': '12345678', 'alias': 'source'}
        with patch('peer.control.PeerClient.pair', side_effect=pair):
            caller = asyncio.create_task(self.call(req(PEER)))
            await asyncio.wait_for(started.wait(), 2)
            # The waiting server task is the one handling the sole live pairing.
            handlers = [task for task in asyncio.all_tasks()
                        if task.get_coro().__qualname__ == 'LocalControl._handle']
            self.assertEqual(len(handlers), 1)
            handlers[0].cancel()
            try:
                await handlers[0]
            except asyncio.CancelledError:
                pass
            caller.cancel()
            try:
                await caller
            except (asyncio.CancelledError, ConnectionError, json.JSONDecodeError):
                pass
            self.assertTrue((await asyncio.wait_for(self.call(req(other)), 2))['ok'])
        self.assertEqual(self.state.trusted[other]['alias'], 'source')

    async def test_pair_different_alias_can_enter_network_concurrently(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        other = '5' * 32
        self.control.discovered = {PEER: {'address': '127.0.0.1', 'port': 9431},
                                   other: {'address': '127.0.0.2', 'port': 9431}}
        self.state.save = lambda: None
        async def pair(state, url, pin, code, expected_peer):
            if expected_peer == PEER:
                entered.set()
                await release.wait()
            state.trusted[expected_peer] = {'url': url, 'pin': pin, 'token': TOKEN_A}
            return expected_peer
        def req(peer, alias):
            return {'op': 'pair', 'peer': peer, 'fingerprint': PIN,
                    'code': '12345678', 'alias': alias}
        with patch('peer.control.PeerClient.pair', side_effect=pair):
            first = asyncio.create_task(self.call(req(PEER, 'first')))
            await asyncio.wait_for(entered.wait(), 2)
            try:
                self.assertTrue((await asyncio.wait_for(self.call(req(other, 'second')), 2))['ok'])
            finally:
                release.set()
            self.assertTrue((await asyncio.wait_for(first, 2))['ok'])
        self.assertEqual({t['alias'] for t in self.state.trusted.values()}, {'first', 'second'})
