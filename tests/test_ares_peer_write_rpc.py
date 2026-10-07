"""Fake-only checkout source writer RPC; no live Herdr or installed Peer."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import websockets

from peer.control_writer import ControlWriter
from peer.core import PeerState
from peer.transport import PeerServer


class Source:
    def __init__(self, socket):
        self.sockets = [socket]
        self.target = {'socketPath': socket, 'paneId': 'w1:p1', 'tabId': 'w1:t1',
                       'terminalId': 'term_1', 'agentKind': 'hermes', 'sessionId': 'chat-a',
                       'profile': 'default', 'identitySource': 'verified',
                       'terminalGeneration': 'proof-1'}

    def inventory(self):
        return {'complete': True, 'terminals': [dict(self.target)]}


class WriteRPC(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = PeerState(root / 'peer')
        self.client = PeerState(root / 'client')
        self.peer = self.client.identity.peer_id
        self.token = 'synthetic-private-token'
        import hashlib
        self.state.tokens[self.peer] = hashlib.sha256(self.token.encode()).hexdigest()
        self.source = Source(str(root / 'fake-herdr.sock'))
        self.log = root / 'inputs.jsonl'
        self.cli = root / 'fake-control'
        self.cli.write_text('#!/usr/bin/env python3\nimport json,sys\n'
            'print(json.dumps({"type":"terminal.frame","seq":0,"encoding":"ansi",'
            '"width":80,"height":24,"full":True,"bytes":""}),flush=True)\n'
            'for line in sys.stdin:\n'
            ' obj=json.loads(line)\n'
            f' open({str(self.log)!r},"a").write(line)\n'
            ' if obj.get("type")=="terminal.release": break\n')
        self.cli.chmod(0o700)
        self.writer = ControlWriter(self.state, self.source, executable=self.cli,
                                    home=root, timeout=.5)
        self.server = PeerServer(self.state, host='127.0.0.1', writer=self.writer,
                                 enable_writer=True, writer_peer_id=self.peer,
                                 writer_cert=self.client.identity.cert,
                                 writer_pin=self.client.identity.fingerprint)
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.url = f'wss://127.0.0.1:{self.server.port}'

    async def connect(self, with_cert=True):
        import ssl
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        if with_cert:
            ssl_context.load_cert_chain(str(self.client.identity.cert),
                                        str(self.client.identity.key))
        return await websockets.connect(self.url, ssl=ssl_context, proxy=None)

    async def rpc(self, ws, op, args, token=None):
        await ws.send(json.dumps({'op': op, 'peer_id': self.peer,
                                  'token': self.token if token is None else token, 'args': args}))
        return json.loads(await ws.recv())

    def inputs(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    async def test_matching_bearer_without_peer_tls_certificate_cannot_control(self):
        self.state.set_write_grant(self.peer, 'default', 'chat-a', 'term_1', True, 30, self.source)
        async with await self.connect(with_cert=False) as ws:
            reply = await self.rpc(ws, 'control.begin',
                {'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1'})
            self.assertFalse(reply['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_default_off_even_with_grant(self):
        self.state.set_write_grant(self.peer, 'default', 'chat-a', 'term_1', True, 30, self.source)
        self.server.enable_writer = False
        async with await self.connect() as ws:
            reply = await self.rpc(ws, 'control.begin',
                {'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1'})
            self.assertFalse(reply['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_begin_send_release_once_without_frames_or_secrets(self):
        self.state.set_write_grant(self.peer, 'default', 'chat-a', 'term_1', True, 30, self.source)
        async with await self.connect() as ws:
            begin = await self.rpc(ws, 'control.begin',
                {'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1'})
            self.assertEqual(begin, {'ok': True, 'result': {'sequence': 0}})
            sent = await self.rpc(ws, 'control.send', {'sequence': 1, 'text': 'private text'})
            self.assertEqual(sent, {'ok': True, 'result': {'sequence': 1}})
            done = await self.rpc(ws, 'control.release', {'sequence': 2})
            self.assertEqual(done, {'ok': True, 'result': {'sequence': 2}})
        self.assertEqual([x for x in self.inputs() if x['type'] == 'terminal.input'],
                         [{'type': 'terminal.input', 'text': 'private text'}])

    async def test_replay_denied_without_second_input(self):
        self.state.set_write_grant(self.peer, 'default', 'chat-a', 'term_1', True, 30, self.source)
        async with await self.connect() as ws:
            await self.rpc(ws, 'control.begin',
                {'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1'})
            self.assertTrue((await self.rpc(ws, 'control.send', {'sequence': 1, 'text': 'once'}))['ok'])
            self.assertFalse((await self.rpc(ws, 'control.send', {'sequence': 1, 'text': 'twice'}))['ok'])
        self.assertEqual([x['text'] for x in self.inputs() if x['type'] == 'terminal.input'], ['once'])

    async def test_grant_is_single_use_across_reconnect(self):
        self.state.set_write_grant(self.peer, 'default', 'chat-a', 'term_1', True, 30, self.source)
        begin = {'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1'}
        async with await self.connect() as ws:
            self.assertTrue((await self.rpc(ws, 'control.begin', begin))['ok'])
            self.assertTrue((await self.rpc(ws, 'control.send',
                                            {'sequence': 1, 'text': 'once'}))['ok'])
        async with await self.connect() as ws:
            self.assertFalse((await self.rpc(ws, 'control.begin', begin))['ok'])
        self.assertEqual([x['text'] for x in self.inputs() if x['type'] == 'terminal.input'], ['once'])

    async def test_release_ack_requires_confirmed_process_exit(self):
        self.state.set_write_grant(self.peer, 'default', 'chat-a', 'term_1', True, 30, self.source)
        original = self.writer.close
        calls = 0
        async def delayed_close():
            nonlocal calls
            calls += 1
            if calls > 1:
                await original()
        self.writer.close = delayed_close
        async with await self.connect() as ws:
            self.assertTrue((await self.rpc(ws, 'control.begin',
                {'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1'}))['ok'])
            self.assertFalse((await self.rpc(ws, 'control.release', {'sequence': 1}))['ok'])
        self.writer.close = original
        await original()

    async def test_revocation_and_identity_change_refuse_input(self):
        self.state.set_write_grant(self.peer, 'default', 'chat-a', 'term_1', True, 30, self.source)
        async with await self.connect() as ws:
            await self.rpc(ws, 'control.begin',
                {'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1'})
            self.source.target['terminalGeneration'] = 'proof-2'
            self.assertFalse((await self.rpc(ws, 'control.send', {'sequence': 1, 'text': 'blocked'}))['ok'])
        self.assertEqual([x for x in self.inputs() if x['type'] == 'terminal.input'], [])


if __name__ == '__main__':
    unittest.main()
