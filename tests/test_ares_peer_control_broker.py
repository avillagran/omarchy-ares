"""Fake pinned mTLS source, fake writer, disposable identities; never live Herdr."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from peer.core import PeerError

from peer.control import LocalControl
from peer.control_writer import ControlWriter
from peer.core import PeerState
from peer.transport import PeerServer
from peer.control_broker import ControlBroker


class Source:
    def __init__(self, socket):
        self.sockets = [socket]
        self.target = {'socketPath': socket, 'paneId': 'w1:p1', 'tabId': 'w1:t1',
                       'terminalId': 'term_1', 'agentKind': 'hermes', 'sessionId': 'chat-a',
                       'profile': 'default', 'identitySource': 'verified', 'terminalGeneration': 'proof-1'}

    def inventory(self):
        return {'complete': True, 'terminals': [dict(self.target)]}


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source_state, self.state = PeerState(root / 'source'), PeerState(root / 'coordinator')
        self.source = Source(str(root / 'fake.sock'))
        self.log = root / 'input.jsonl'
        exe = root / 'fake-control'
        exe.write_text('#!/usr/bin/env python3\nimport json,sys\n'
            'print(json.dumps({"type":"terminal.frame","seq":0,"encoding":"ansi",'
            '"width":80,"height":24,"full":True,"bytes":""}),flush=True)\n'
            'for line in sys.stdin:\n'
            ' obj=json.loads(line)\n'
            f' open({str(self.log)!r},"a").write(line)\n'
            ' if obj.get("type")=="terminal.release": break\n')
        exe.chmod(0o700)
        writer = ControlWriter(self.source_state, self.source, executable=exe, home=root, timeout=.5)
        self.server = PeerServer(self.source_state, host='127.0.0.1', writer=writer,
            enable_writer=True, writer_peer_id=self.state.identity.peer_id,
            writer_cert=self.state.identity.cert, writer_pin=self.state.identity.fingerprint)
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.peer = self.source_state.identity.peer_id
        self.url = f'wss://127.0.0.1:{self.server.port}'
        token = 'fake-token-never-log'
        self.source_state.tokens[self.state.identity.peer_id] = hashlib.sha256(token.encode()).hexdigest()
        self.state.trusted[self.peer] = {'alias': 'source', 'url': self.url,
            'pin': self.source_state.identity.fingerprint, 'token': token}
        self.broker = ControlBroker(self.state)
        self.control = LocalControl(self.state, broker=self.broker)
        await self.control.start()
        self.addAsyncCleanup(self.control.close)

    def grant(self):
        self.source_state.set_write_grant(self.state.identity.peer_id, 'default', 'chat-a', 'term_1',
                                          True, 30, self.source)

    async def call(self, payload):
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        writer.write(json.dumps(payload).encode() + b'\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        if response['ok'] and payload['op'] in ('control.begin', 'control.send'):
            writer.write(json.dumps({'op': 'control.ack',
                'handle': response['result'].get('handle', payload.get('handle')),
                'sequence': response['result']['sequence']}).encode() + b'\n')
            await writer.drain()
            self.assertEqual(json.loads(await reader.readline()), {'ok': True, 'result': {'ack': True}})
        writer.close()
        await writer.wait_closed()
        return response

    def begin(self):
        return {'op': 'control.begin', 'peer': 'source', 'profile': 'default',
                'session_id': 'chat-a', 'terminal_id': 'term_1', 'ttl': 10}

    async def test_begin_reply_without_local_ack_quarantines_remote_writer(self):
        self.grant()
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        writer.write(json.dumps(self.begin()).encode() + b'\n')
        await writer.drain()
        reply = json.loads(await reader.readline())
        self.assertTrue(reply['ok'], reply)
        handle = reply['result']['handle']
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(.1)
        self.assertNotIn(handle, self.broker.active)
        self.grant()
        self.assertFalse((await self.call(self.begin()))['ok'])
        self.assertEqual(self.inputs(), [])

    def inputs(self):
        return [json.loads(x)['text'] for x in self.log.read_text().splitlines()
                if json.loads(x).get('type') == 'terminal.input'] if self.log.exists() else []

    async def test_begin_send_release_over_one_pinned_socket(self):
        self.grant()
        begin = await self.call(self.begin())
        self.assertTrue(begin['ok'], begin)
        handle = begin['result']['handle']
        self.assertEqual(len(handle), 64)
        self.assertEqual(await self.call({'op': 'control.send', 'handle': handle,
            'peer': 'source', 'profile': 'default', 'session_id': 'chat-a',
            'terminal_id': 'term_1', 'sequence': 1, 'text': 'one'}),
            {'ok': True, 'result': {'sequence': 1}})
        self.assertEqual(await self.call({'op': 'control.release', 'handle': handle,
            'peer': 'source', 'profile': 'default', 'session_id': 'chat-a',
            'terminal_id': 'term_1', 'sequence': 2}),
            {'ok': True, 'result': {'sequence': 2}})
        self.assertEqual(self.inputs(), ['one'])

    async def test_confirmed_release_replays_only_to_exact_owner_and_never_writes(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        release = {'op': 'control.release', 'handle': handle, 'peer': 'source',
            'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
            'sequence': 1}
        ack = {'ok': True, 'result': {'sequence': 1}}
        self.assertEqual(await self.call(release), ack)
        self.assertEqual(await self.call(release), ack)
        self.assertFalse((await self.call(dict(release, session_id='other')))['ok'])
        self.assertFalse((await self.call(dict(release, sequence=2)))['ok'])
        self.assertFalse((await self.call(dict(release, op='control.send', text='stale')))['ok'])
        from peer.core import PeerError
        import os
        with self.assertRaises(PeerError):
            await self.broker.step('control.release', handle, 'source', 'default',
                'chat-a', 'term_1', 1, os.getuid() + 1)
        self.broker.released[handle] = (*self.broker.released[handle][:-1], 0)
        self.assertFalse((await self.call(release))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_replay_change_binding_and_duplicate_begin_refused(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        self.assertFalse((await self.call(self.begin()))['ok'])
        send = {'op': 'control.send', 'handle': handle, 'peer': 'source',
                'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
                'sequence': 1, 'text': 'one'}
        self.assertFalse((await self.call(dict(send, session_id='chat-b')))['ok'])
        self.assertFalse((await self.call(send))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_replay_does_not_send_second_input(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        send = {'op': 'control.send', 'handle': handle, 'peer': 'source',
                'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
                'sequence': 1, 'text': 'one'}
        self.assertTrue((await self.call(send))['ok'])
        self.assertFalse((await self.call(send))['ok'])
        self.assertEqual(self.inputs(), ['one'])

    async def test_changed_url_closes_idle_handle(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        self.state.trusted[self.peer]['url'] = 'wss://127.0.0.1:1'
        await asyncio.sleep(.4)
        self.assertNotIn(handle, self.broker.active)
        self.assertEqual(self.inputs(), [])

    async def test_pin_mismatch_rejected_before_grant_consumption(self):
        self.grant()
        self.state.trusted[self.peer]['pin'] = '0' * 64
        self.assertFalse((await self.call(self.begin()))['ok'])
        self.assertFalse(self.source_state.write_grant.get('rpc_used', False))
        self.assertEqual(self.inputs(), [])

    async def test_changed_pin_revokes_handle_before_input(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        self.state.trusted[self.peer]['pin'] = '0' * 64
        self.assertFalse((await self.call({'op': 'control.send', 'handle': handle,
            'peer': 'source', 'profile': 'default', 'session_id': 'chat-a',
            'terminal_id': 'term_1', 'sequence': 1, 'text': 'blocked'}))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_ambiguous_begin_keeps_binding_quarantined(self):
        self.grant()
        original = self.server.writer.open
        async def lost_begin_ack(*args):
            await original(*args)
            raise ConnectionError('begin acknowledgment lost')
        self.server.writer.open = lost_begin_ack
        self.assertFalse((await self.call(self.begin()))['ok'])
        self.assertEqual(len(self.broker.bindings), 1)
        self.server.writer.open = original
        self.grant()
        self.assertFalse((await self.call(self.begin()))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_lost_ack_quarantines_and_never_retries_input(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        self.server.writer.send_original = self.server.writer.send
        async def lost_ack(text):
            await self.server.writer.send_original(text)
            await self.server.writer.close()
            raise ConnectionError('ack lost after input')
        self.server.writer.send = lost_ack
        send = {'op': 'control.send', 'handle': handle, 'peer': 'source',
                'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
                'sequence': 1, 'text': 'once'}
        self.assertFalse((await self.call(send))['ok'])
        self.assertFalse((await self.call(send))['ok'])
        self.grant()
        self.assertFalse((await self.call(self.begin()))['ok'])
        self.assertEqual(self.inputs(), ['once'])

    async def test_send_reply_without_receipt_never_retries_input(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        request = {'op': 'control.send', 'handle': handle, 'peer': 'source',
            'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
            'sequence': 1, 'text': 'once'}
        writer.write(json.dumps(request).encode() + b'\n')
        await writer.drain()
        self.assertTrue(json.loads(await reader.readline())['ok'])
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(.1)
        self.assertEqual(self.inputs(), ['once'])
        self.assertNotIn(handle, self.broker.active)
        self.assertFalse((await self.call(request))['ok'])
        self.grant()
        self.assertFalse((await self.call(self.begin()))['ok'])

    async def test_failed_websocket_close_retains_explicit_quarantine(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        ws = self.broker.active[handle]['ws']
        async def failed_close():
            raise RuntimeError('fake transport close failed')
        ws.close = failed_close
        await self.broker.abort(handle)
        binding = self.peer, 'default', 'chat-a', 'term_1', __import__('os').getuid()
        self.assertNotIn(handle, self.broker.active)
        self.assertIn(binding, self.broker.quarantine)
        self.assertIs(self.broker.quarantine[binding], ws)
        self.grant()
        self.assertFalse((await self.call(self.begin()))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_revoke_and_expiry_close_broker_without_input(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        self.source_state.revoke(self.state.identity.peer_id)
        send = {'op': 'control.send', 'handle': handle, 'peer': 'source',
                'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
                'sequence': 1, 'text': 'blocked'}
        self.assertFalse((await self.call(send))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_handle_expiry_closes_without_input(self):
        self.grant()
        begin = dict(self.begin(), ttl=1)
        handle = (await self.call(begin))['result']['handle']
        send = {'op': 'control.send', 'handle': handle, 'peer': 'source',
                'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
                'sequence': 1, 'text': 'blocked'}
        await asyncio.sleep(1.1)
        self.assertFalse((await self.call(dict(send, handle=handle)))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_cancelled_local_request_does_not_keep_remote_handle(self):
        self.grant()
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        original = self.server.writer.open
        entered, resume = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            await original(*args)
            entered.set()
            await resume.wait()
        self.server.writer.open = delayed
        writer.write(json.dumps(self.begin()).encode() + b'\n')
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 3)
        writer.close()
        await writer.wait_closed()
        resume.set()
        await asyncio.sleep(.2)
        self.assertFalse(self.broker.active)
        self.assertEqual(self.inputs(), [])

    async def test_local_disconnect_during_send_quarantines_binding(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        entered = asyncio.Event()
        resume = asyncio.Event()
        original = self.server.writer.send
        async def delayed(text):
            await original(text)
            entered.set()
            await resume.wait()
        self.server.writer.send = delayed
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        writer.write(json.dumps({'op': 'control.send', 'handle': handle,
            'peer': 'source', 'profile': 'default', 'session_id': 'chat-a',
            'terminal_id': 'term_1', 'sequence': 1, 'text': 'once'}).encode() + b'\n')
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 3)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(.1)
        resume.set()
        self.assertNotIn(handle, self.broker.active)
        self.assertEqual(self.inputs(), ['once'])
        self.grant()
        self.assertFalse((await self.call(self.begin()))['ok'])

    def handler_task(self):
        handlers = [task for task in asyncio.all_tasks()
                    if task.get_coro().__qualname__ == 'LocalControl._handle' and not task.done()]
        self.assertEqual(len(handlers), 1)
        return handlers[0]

    async def test_cancellation_while_begin_awaits_source_quarantines_binding(self):
        self.grant()
        entered, resume = asyncio.Event(), asyncio.Event()
        original = self.server.writer.open
        async def delayed(*args):
            await original(*args)
            entered.set()
            await resume.wait()
        self.server.writer.open = delayed
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        writer.write(json.dumps(self.begin()).encode() + b'\n')
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 3)
        handler = self.handler_task()
        handler.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(handler, return_exceptions=True), 3)
            resume.set()
            await asyncio.sleep(.1)
            self.assertFalse(self.broker.active)
            self.assertEqual(len(self.broker.bindings), 1)
            self.assertFalse((await self.call(self.begin()))['ok'])
            self.assertEqual(self.inputs(), [])
        finally:
            resume.set()
            writer.close()
            await writer.wait_closed()

    async def test_cancellation_while_send_awaits_source_never_retries_input(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        entered, resume = asyncio.Event(), asyncio.Event()
        original = self.server.writer.send
        async def delayed(text):
            await original(text)
            entered.set()
            await resume.wait()
        self.server.writer.send = delayed
        request = {'op': 'control.send', 'handle': handle, 'peer': 'source',
            'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
            'sequence': 1, 'text': 'once'}
        reader, writer = await asyncio.open_unix_connection(str(self.control.path))
        writer.write(json.dumps(request).encode() + b'\n')
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 3)
        handler = self.handler_task()
        handler.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(handler, return_exceptions=True), 3)
            resume.set()
            await asyncio.sleep(.1)
            self.assertNotIn(handle, self.broker.active)
            self.assertEqual(self.inputs(), ['once'])
            self.assertFalse((await self.call(request))['ok'])
            self.grant()
            self.assertFalse((await self.call(self.begin()))['ok'])
        finally:
            resume.set()
            writer.close()
            await writer.wait_closed()

    async def test_cancellation_during_response_drain_quarantines_handle(self):
        self.grant()
        entered, resume = asyncio.Event(), asyncio.Event()
        original = asyncio.StreamWriter.drain
        server_streams = []
        async def stalled_drain(stream):
            if (asyncio.current_task().get_coro().__qualname__ == 'LocalControl._handle'
                    and not entered.is_set()):
                server_streams.append(stream)
                entered.set()
                await resume.wait()
            return await original(stream)
        asyncio.StreamWriter.drain = stalled_drain
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.control.path))
            writer.write(json.dumps(self.begin()).encode() + b'\n')
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 3)
            handler = self.handler_task()
            handles = set(self.broker.active)
            self.assertEqual(len(handles), 1)
            handler.cancel()
            await asyncio.wait_for(asyncio.gather(handler, return_exceptions=True), 3)
            self.assertFalse(self.broker.active)
            self.assertTrue(server_streams[0].is_closing())
            self.assertEqual(len(self.broker.bindings), 1)
            self.assertFalse((await self.call(self.begin()))['ok'])
            self.assertEqual(self.inputs(), [])
        finally:
            resume.set()
            asyncio.StreamWriter.drain = original
            writer.close()
            await writer.wait_closed()

    async def test_cancellation_during_send_response_drain_fences_input(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        request = {'op': 'control.send', 'handle': handle, 'peer': 'source',
            'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
            'sequence': 1, 'text': 'once'}
        entered, resume = asyncio.Event(), asyncio.Event()
        original = asyncio.StreamWriter.drain
        async def stalled_drain(stream):
            if (asyncio.current_task().get_coro().__qualname__ == 'LocalControl._handle'
                    and not entered.is_set()):
                entered.set()
                await resume.wait()
            return await original(stream)
        asyncio.StreamWriter.drain = stalled_drain
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.control.path))
            writer.write(json.dumps(request).encode() + b'\n')
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 3)
            handler = self.handler_task()
            handler.cancel()
            await asyncio.wait_for(asyncio.gather(handler, return_exceptions=True), 3)
            self.assertEqual(self.inputs(), ['once'])
            self.assertNotIn(handle, self.broker.active)
            self.assertFalse((await self.call(request))['ok'])
            self.grant()
            self.assertFalse((await self.call(self.begin()))['ok'])
        finally:
            resume.set()
            asyncio.StreamWriter.drain = original
            writer.close()
            await writer.wait_closed()

    async def test_shutdown_fences_every_handle_before_first_socket_close(self):
        self.grant()
        first = (await self.call(self.begin()))['result']['handle']
        root = Path(self.tmp.name)
        second_state = PeerState(root / 'source-b')
        second_source = Source(str(root / 'fake-b.sock'))
        second_source.target.update(sessionId='chat-b', terminalId='term_2')
        second_writer = ControlWriter(second_state, second_source,
            executable=root / 'fake-control', home=root, timeout=.5)
        second_server = PeerServer(second_state, host='127.0.0.1', writer=second_writer,
            enable_writer=True, writer_peer_id=self.state.identity.peer_id,
            writer_cert=self.state.identity.cert, writer_pin=self.state.identity.fingerprint)
        await second_server.start()
        self.addAsyncCleanup(second_server.close)
        second_state.tokens[self.state.identity.peer_id] = hashlib.sha256(
            b'fake-token-b-never-log').hexdigest()
        second_state.set_write_grant(self.state.identity.peer_id, 'default', 'chat-b',
            'term_2', True, 30, second_source)
        self.state.trusted[second_state.identity.peer_id] = {
            'alias': 'source-b', 'url': f'wss://127.0.0.1:{second_server.port}',
            'pin': second_state.identity.fingerprint, 'token': 'fake-token-b-never-log'}
        second = (await self.broker.begin('source-b', 'default', 'chat-b',
            'term_2', 10, os.getuid()))['handle']
        second_ws = self.broker.active[second]['ws']

        # An unrelated, pre-existing process must survive broker teardown.
        unrelated = subprocess.Popen(['/usr/bin/sleep', '30'])
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait())
                        if unrelated.poll() is None else None)
        entered, resume = asyncio.Event(), asyncio.Event()
        ws = self.broker.active[first]['ws']
        original_close = ws.close
        async def held_close():
            entered.set()
            await resume.wait()
            await original_close()
        ws.close = held_close
        closing = asyncio.create_task(self.broker.close())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with self.assertRaises(PeerError):
                await self.broker.step('control.send', second, 'source-b', 'default',
                    'chat-b', 'term_2', 1, os.getuid(), 'blocked')
            self.assertEqual(self.inputs(), [])
            self.assertIsNone(unrelated.poll())
        finally:
            resume.set()
            await asyncio.wait_for(closing, 5)
        self.assertFalse(self.broker.active)
        for handle_ws in (ws, second_ws):
            self.assertIn(handle_ws, self.broker.quarantine.values())
        self.assertEqual(len(self.broker.bindings), 2)
        self.assertEqual(self.inputs(), [])
        self.assertIsNone(unrelated.poll())

    async def test_shutdown_waits_for_inflight_send_before_completing_fence(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        ws = self.broker.active[handle]['ws']
        original_send = ws.send
        entered, resume = asyncio.Event(), asyncio.Event()
        async def delayed_send(frame):
            entered.set()
            # Model a transport send that cannot be cancelled after admission.
            while not resume.is_set():
                try:
                    await resume.wait()
                except asyncio.CancelledError:
                    pass
            await original_send(frame)
        ws.send = delayed_send
        sending = asyncio.create_task(self.broker.step('control.send', handle,
            'source', 'default', 'chat-a', 'term_1', 1, os.getuid(), 'late'))
        await asyncio.wait_for(entered.wait(), 3)
        closing = asyncio.create_task(self.broker.close())
        try:
            await asyncio.sleep(.05)
            self.assertFalse(closing.done(), 'shutdown published a fence while send was pending')
            self.assertEqual(self.inputs(), [])
        finally:
            resume.set()
        await asyncio.wait_for(asyncio.gather(sending, return_exceptions=True), 5)
        await asyncio.wait_for(closing, 5)
        # The uncertain in-flight send MAY have reached the source before the
        # fence; its delivery is not evidence of safe application receipt.
        before = self.inputs()
        await asyncio.sleep(.05)
        self.assertEqual(self.inputs(), before)
        self.assertNotIn(handle, self.broker.active)
        self.assertEqual(len(self.broker.bindings), 1)

    async def test_shutdown_waits_for_inflight_begin_before_completing_fence(self):
        self.grant()
        from peer import control_broker
        original_connect = control_broker.websockets.connect
        entered, resume = asyncio.Event(), asyncio.Event()
        async def delayed_connect(*args, **kwargs):
            ws = await original_connect(*args, **kwargs)
            original_send = ws.send
            async def delayed_send(frame):
                entered.set()
                while not resume.is_set():
                    try:
                        await resume.wait()
                    except asyncio.CancelledError:
                        pass
                await original_send(frame)
            ws.send = delayed_send
            return ws
        control_broker.websockets.connect = delayed_connect
        try:
            beginning = asyncio.create_task(self.broker.begin('source', 'default',
                'chat-a', 'term_1', 10, os.getuid()))
            await asyncio.wait_for(entered.wait(), 3)
            closing = asyncio.create_task(self.broker.close())
            await asyncio.sleep(.05)
            self.assertFalse(closing.done())
            resume.set()
            self.assertIsInstance((await asyncio.wait_for(asyncio.gather(
                beginning, return_exceptions=True), 5))[0], BaseException)
            await asyncio.wait_for(closing, 5)
            self.assertFalse(self.broker.active)
            self.assertEqual(len(self.broker.bindings), 1)
        finally:
            resume.set()
            control_broker.websockets.connect = original_connect

    async def test_shutdown_reports_incomplete_when_send_cannot_settle(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        ws = self.broker.active[handle]['ws']
        original_send = ws.send
        entered, resume = asyncio.Event(), asyncio.Event()
        async def stuck_send(frame):
            entered.set()
            while not resume.is_set():
                try:
                    await asyncio.wait_for(resume.wait(), .1)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            await original_send(frame)
        ws.send = stuck_send
        sending = asyncio.create_task(self.broker.step('control.send', handle,
            'source', 'default', 'chat-a', 'term_1', 1, os.getuid(), 'uncertain'))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            with self.assertRaisesRegex(PeerError, 'shutdown incomplete'):
                await asyncio.wait_for(self.broker.close(), 8)
            self.assertIn(sending, self.broker.inflight)
            self.assertEqual(len(self.broker.quarantine), 1)
            self.assertEqual(len(self.broker.bindings), 1)
        finally:
            resume.set()
            await asyncio.wait_for(asyncio.gather(sending, return_exceptions=True), 5)

    async def test_disconnect_and_shutdown_invalidate_handles(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        await self.broker.close()
        self.assertFalse((await self.call({'op': 'control.send', 'handle': handle,
            'peer': 'source', 'profile': 'default', 'session_id': 'chat-a',
            'terminal_id': 'term_1', 'sequence': 1, 'text': 'blocked'}))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_invalid_text_and_oversized_frame_fail_closed(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        base = {'op': 'control.send', 'handle': handle, 'peer': 'source',
                'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
                'sequence': 1}
        self.assertFalse((await self.call(dict(base, text='line\nnext')))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_oversized_utf8_input_is_rejected(self):
        self.grant()
        handle = (await self.call(self.begin()))['result']['handle']
        base = {'op': 'control.send', 'handle': handle, 'peer': 'source',
                'profile': 'default', 'session_id': 'chat-a', 'terminal_id': 'term_1',
                'sequence': 1}
        self.assertFalse((await self.call(dict(base, text='é' * 1025)))['ok'])
        self.assertEqual(self.inputs(), [])

    async def test_default_off_without_broker(self):
        other = LocalControl(self.state, path=self.state.root / 'other.sock')
        await other.start()
        self.addAsyncCleanup(other.close)
        reader, writer = await asyncio.open_unix_connection(str(other.path))
        writer.write(json.dumps(self.begin()).encode() + b'\n')
        await writer.drain()
        self.assertFalse(json.loads(await reader.readline())['ok'])
        writer.close()
        await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
