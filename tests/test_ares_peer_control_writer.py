"""Fake-only contract for the source-local Herdr controller."""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from peer.control_writer import ControlWriter, ControlError, _Child
from peer.core import PeerState

PEER = 'a' * 32
TARGET = {'socketPath': '/synthetic/herdr.sock', 'paneId': 'w1:p1',
          'tabId': 'w1:t1', 'terminalId': 'term_1', 'agentKind': 'hermes',
          'sessionId': 'chat-a', 'profile': 'default', 'identitySource': 'verified',
          'terminalGeneration': 'owner-generation-1'}
FRAME = {'type': 'terminal.frame', 'seq': 1, 'encoding': 'ansi', 'width': 80,
         'height': 24, 'full': True, 'bytes': 'SGVsbG8='}


class Source:
    def __init__(self):
        self.items = [dict(TARGET)]
        self.sockets = (TARGET['socketPath'],)
    def inventory(self):
        return {'complete': True, 'terminals': self.items}


class ControlWriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.fake = self.home / 'herdr-fake'
        self.record = self.home / 'record.jsonl'
        self.config = self.home / 'config.json'
        self.fake.write_text('''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
cfg = json.loads(Path(os.environ['HOME'], 'config.json').read_text())
with Path(os.environ['HOME'], 'record.jsonl').open('a') as out:
    out.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')
    out.flush()
    if cfg.get('frame') is not None:
        print(json.dumps(cfg['frame']) if isinstance(cfg['frame'], dict) else cfg['frame'], flush=True)
    if cfg.get('after_frame') is not None:
        print(json.dumps(cfg['after_frame']) if isinstance(cfg['after_frame'], dict) else cfg['after_frame'], flush=True)
    if cfg.get('exit'):
        sys.exit(1)
    if cfg.get('stream'):
        import threading
        def stream():
            seq = 2
            while True:
                print(json.dumps(dict(cfg['frame'], seq=seq)), flush=True)
                seq += 1
                time.sleep(.003)
        threading.Thread(target=stream, daemon=True).start()
    for line in sys.stdin:
        out.write(json.dumps({'command': json.loads(line)}) + '\\n')
        out.flush()
        if json.loads(line)['type'] == 'terminal.release' and not cfg.get('ignore_release'):
            break
        if cfg.get('closed'):
            print(json.dumps({'type': 'terminal.closed', 'reason': 'private'}), flush=True)
''')
        self.fake.chmod(0o700)
        self.config.write_text(json.dumps({'frame': FRAME}))
        self.state = PeerState(self.home / 'state')
        self.state.tokens[PEER] = 'synthetic-digest'
        self.source = Source()
        self.state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, self.source)
        self.writer = ControlWriter(self.state, self.source, executable=self.fake,
                                    home=self.home, timeout=.25)
        self.addAsyncCleanup(self.writer.close)

    def recorded(self):
        return [json.loads(s) for s in self.record.read_text().splitlines()] if self.record.exists() else []

    async def test_invalid_utf8_frame_never_enters_exception_chain(self):
        private = b'private-terminal-output-\xff\n'
        with self.assertRaises(ControlError) as caught:
            self.writer._frame(private)
        seen = set()
        pending = [caught.exception]
        while pending:
            exc = pending.pop()
            if id(exc) in seen:
                continue
            seen.add(id(exc))
            self.assertNotIn(b'private-terminal-output-', repr(exc.args).encode())
            self.assertNotIn(b'private-terminal-output-', getattr(exc, 'object', b''))
            pending.extend(e for e in (exc.__cause__, exc.__context__) if e is not None)

    async def test_unencodable_send_text_never_enters_exception_chain(self):
        private = 'private-controller-text-\ud800'
        with self.assertRaises(ControlError) as caught:
            await self.writer.send(private)
        self.assertEqual(caught.exception.args, ('Invalid controller text',))
        seen = set()
        pending = [caught.exception]
        while pending:
            exc = pending.pop()
            if id(exc) in seen:
                continue
            seen.add(id(exc))
            self.assertNotIn(repr(private), repr(exc.args))
            self.assertNotIn(private, str(getattr(exc, 'object', '')))
            self.assertNotIn('private-controller-text-', repr(exc.args))
            self.assertNotIn('private-controller-text-', repr(getattr(exc, 'object', None)))
            pending.extend(e for e in (exc.__cause__, exc.__context__) if e is not None)
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input'
                             for r in self.recorded()))

    async def test_frame_proves_controller_before_text_input_and_release(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        await self.writer.send('héllo')
        await self.writer.close()
        rows = self.recorded()
        self.assertEqual(rows[0]['argv'], ['terminal', 'session', 'control', 'term_1'])
        self.assertEqual(rows[0]['env']['HERDR_SOCKET_PATH'], TARGET['socketPath'])
        self.assertNotIn('HERDR_PANE_ID', rows[0]['env'])
        self.assertEqual([r['command'] for r in rows[1:]],
                         [{'type': 'terminal.input', 'text': 'héllo'}, {'type': 'terminal.release'}])

    async def test_one_controller_and_occupied_refusal(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        with self.assertRaises(ControlError):
            await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        await self.writer.close()
        self.config.write_text(json.dumps({'frame': {'type': 'terminal.closed', 'reason': 'occupied'}}))
        with self.assertRaises(ControlError):
            await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input' for r in self.recorded()))

    async def test_cancelled_open_releases_and_drops_lock(self):
        self.config.write_text(json.dumps({'frame': None}))
        task = asyncio.create_task(self.writer.open(PEER, 'default', 'chat-a', 'term_1'))
        await asyncio.sleep(.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.config.write_text(json.dumps({'frame': FRAME}))
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        await self.writer.close()

    async def test_revocation_and_regrant_same_generation_refused(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        self.state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', False, 30, self.source)
        with self.assertRaises(ControlError):
            await self.writer.send('no')
        self.state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, self.source)
        with self.assertRaises(ControlError):
            await self.writer.send('no')
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input' for r in self.recorded()))

    async def test_stale_generation_and_incomplete_inventory(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        self.source.items = [dict(TARGET, terminalGeneration='owner-generation-2')]
        with self.assertRaises(ControlError):
            await self.writer.send('no')
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input' for r in self.recorded()))

    async def test_other_instance_cannot_acquire_same_controller(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        other = ControlWriter(self.state, self.source, executable=self.fake,
                              home=self.home, timeout=.25)
        with self.assertRaises(ControlError):
            await other.open(PEER, 'default', 'chat-a', 'term_1')
        await self.writer.close()
        await other.open(PEER, 'default', 'chat-a', 'term_1')
        await other.close()

    async def test_revoked_peer_cannot_write(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        self.state.revoke(PEER)
        with self.assertRaises(ControlError):
            await self.writer.send('no')
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input' for r in self.recorded()))

    async def test_timeout_and_post_frame_invalid_output_release_automatically(self):
        for cfg in ({'frame': None}, {'frame': FRAME, 'after_frame': 'bad-json'},
                    {'frame': FRAME, 'after_frame': {'type': 'terminal.closed', 'reason': 'private'}}):
            self.config.write_text(json.dumps(cfg))
            if cfg['frame'] is None:
                with self.assertRaises(ControlError):
                    await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
            else:
                await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
                await asyncio.sleep(.05)
                self.assertIsNone(self.writer.process)
            self.assertIsNone(self.writer.key)
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input' for r in self.recorded()))

    async def test_oversized_and_nontext_and_control_characters(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        for value in ('é' * 1025, b'raw', 'x\n', '\x1b[A', ''):
            with self.subTest(value=type(value)):
                with self.assertRaises(ControlError):
                    await self.writer.send(value)
        await self.writer.close()
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input' for r in self.recorded()))

    async def test_startup_failure_malformed_frame_and_eof(self):
        for cfg in ({'frame': None, 'exit': True}, {'frame': 'not-json'},
                    {'frame': {'type': 'terminal.frame', 'seq': 1}},
                    {'frame': FRAME | {'full': False}},
                    {'frame': FRAME | {'bytes': '!' * 20000}}):
            self.config.write_text(json.dumps(cfg))
            with self.subTest(cfg=cfg):
                with self.assertRaises(ControlError):
                    await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input' for r in self.recorded()))

    async def test_closed_after_first_frame_rejects_later_input(self):
        self.config.write_text(json.dumps({'frame': FRAME, 'closed': True}))
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        original_drain = self.writer.process.stdin.drain

        async def drain_after_close():
            await original_drain()
            # The fake reads and records first input, then emits terminal.closed.
            # Hold the send at its drain boundary until the watcher sees refusal.
            while not self.writer.failed:
                await asyncio.sleep(.001)

        self.writer.process.stdin.drain = drain_after_close
        with self.assertRaisesRegex(ControlError, '^Controller input refused$'):
            await asyncio.wait_for(self.writer.send('first'), 1)
        async def wait_closed():
            while self.writer.process is not None:
                await asyncio.sleep(.001)

        await asyncio.wait_for(wait_closed(), 1)
        self.assertIsNone(self.writer.key)
        inputs = lambda: [r['command'] for r in self.recorded()
                          if r.get('command', {}).get('type') == 'terminal.input']
        self.assertEqual(inputs(), [{'type': 'terminal.input', 'text': 'first'}])
        with self.assertRaisesRegex(ControlError, '^Controller input refused$'):
            await self.writer.send('second')
        self.assertEqual(inputs(), [{'type': 'terminal.input', 'text': 'first'}])

    async def test_idle_controller_remains_available_then_sends(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        await asyncio.sleep(.4)
        self.assertIsNotNone(self.writer.process)
        inputs = lambda: [r['command'] for r in self.recorded()
                          if r.get('command', {}).get('type') == 'terminal.input']
        self.assertEqual(inputs(), [])
        await self.writer.send('after-idle')
        self.assertEqual(inputs(), [{'type': 'terminal.input', 'text': 'after-idle'}])

    async def test_silent_expiry_and_revoke_release_without_send(self):
        for revoke in (False, True):
            with self.subTest(revoke=revoke):
                await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
                if revoke:
                    self.state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', False, 30, self.source)
                else:
                    self.state.write_grant['expires'] = time.monotonic() - 1
                await asyncio.sleep(.18)
                self.assertIsNone(self.writer.process)
                self.assertIsNone(self.writer.key)
                with self.assertRaises(ControlError):
                    await self.writer.send('denied')
                self.state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, self.source)
        self.assertFalse(any(r.get('command', {}).get('type') == 'terminal.input' for r in self.recorded()))

    async def test_silent_socket_allowlist_change_releases(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        self.source.sockets = ()
        await asyncio.sleep(.18)
        self.assertIsNone(self.writer.process)
        self.assertIsNone(self.writer.key)

    async def test_busy_stream_revocation_and_generation_change_release(self):
        self.config.write_text(json.dumps({'frame': FRAME, 'stream': True}))
        for change in ('revoke', 'expiry', 'generation'):
            with self.subTest(change=change):
                await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
                if change == 'revoke':
                    self.state.revoke(PEER)
                elif change == 'expiry':
                    self.state.write_grant['expires'] = time.monotonic() - 1
                else:
                    self.source.items = [dict(TARGET, terminalGeneration='changed')]
                await asyncio.sleep(.18)
                self.assertIsNone(self.writer.process)
                self.assertIsNone(self.writer.key)
                self.source.items = [dict(TARGET)]
                self.state.tokens[PEER] = 'synthetic-digest'
                self.state.set_write_grant(PEER, 'default', 'chat-a', 'term_1', True, 30, self.source)

    async def test_concurrent_send_and_close_never_writes_after_release(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        entered = asyncio.Event()
        proceed = asyncio.Event()
        original = self.writer.process.stdin.drain
        async def paused_drain():
            entered.set()
            await proceed.wait()
            await original()
        self.writer.process.stdin.drain = paused_drain
        sending = asyncio.create_task(self.writer.send('race'))
        await entered.wait()
        closing = asyncio.create_task(self.writer.close())
        await asyncio.sleep(.01)
        proceed.set()
        results = await asyncio.gather(sending, closing, return_exceptions=True)
        self.assertTrue(results[0] is None or isinstance(results[0], ControlError), results[0])
        commands = [r['command']['type'] for r in self.recorded() if 'command' in r]
        self.assertEqual(commands.count('terminal.release'), 1)
        self.assertNotIn('terminal.input', commands[commands.index('terminal.release') + 1:])
        self.assertIsNone(self.writer.key)

    async def test_async_spawn_injection_is_rejected_before_any_child_exists(self):
        async def child_then_raise(*args, **kwargs):
            child = await asyncio.create_subprocess_exec(*args, **kwargs)
            try:
                raise RuntimeError('child born without published ownership')
            finally:
                child.kill()  # RED harness must not leak an exact disposable child.
                await child.wait()

        with self.assertRaises(TypeError):
            ControlWriter(self.state, self.source, executable=self.fake,
                          home=self.home, spawn=child_then_raise)
        self.assertEqual(self.recorded(), [])

    async def test_assignment_cannot_route_around_synchronous_child_publication(self):
        born = []
        async def child_then_raise(*args, **kwargs):
            child = await asyncio.create_subprocess_exec('/usr/bin/sleep', '30')
            born.append(child)
            raise RuntimeError('unpublished child after async injection')

        self.writer.spawn = child_then_raise
        other = ControlWriter(self.state, self.source, executable=self.fake,
                              home=self.home, timeout=.25)
        try:
            await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
            pid = self.writer.process.pid
            with self.assertRaisesRegex(ControlError, 'occupied'):
                await other.open(PEER, 'default', 'chat-a', 'term_1')
            await self.writer.close()
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertEqual(born, [])
            await other.open(PEER, 'default', 'chat-a', 'term_1')
            await other.close()
        finally:
            for child in born:  # RED harness only: reap exact disposable PID.
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            await self.writer.close()

    async def test_cleanup_failure_does_not_keep_reservation(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        process = self.writer.process
        original = process.stdin.drain
        async def failed_drain():
            raise RuntimeError('synthetic cleanup failure')
        process.stdin.drain = failed_drain
        try:
            await self.writer.close()
        finally:
            process.stdin.drain = original
        self.assertIsNone(self.writer.key)
        self.assertIsNotNone(process.returncode)

    async def test_failed_kill_and_wait_quarantine_until_reaped(self):
        self.config.write_text(json.dumps({'frame': FRAME, 'ignore_release': True}))
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        child = self.writer.process
        original_drain, original_kill, original_wait = child.stdin.drain, child.kill, child.wait
        async def failed_drain():
            raise RuntimeError('synthetic drain failure')
        async def failed_wait():
            raise RuntimeError('synthetic wait failure')
        def failed_kill():
            raise RuntimeError('synthetic kill failure')
        child.stdin.drain, child.kill, child.wait = failed_drain, failed_kill, failed_wait
        try:
            await asyncio.wait_for(self.writer.close(), .5)
            self.assertIs(self.writer.process, child)
            self.assertIsNone(child.returncode)
            other = ControlWriter(self.state, self.source, executable=self.fake,
                                  home=self.home, timeout=.05)
            with self.assertRaisesRegex(ControlError, 'occupied'):
                await other.open(PEER, 'default', 'chat-a', 'term_1')
            self.assertEqual(len([r for r in self.recorded() if 'argv' in r]), 1)
        finally:
            child.stdin.drain, child.kill, child.wait = original_drain, original_kill, original_wait
            await self.writer.close()
        self.assertIsNotNone(child.returncode)
        self.assertIsNone(self.writer.key)

    async def test_cancelled_close_still_reaps_and_releases(self):
        self.config.write_text(json.dumps({'frame': FRAME, 'ignore_release': True}))
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        child = self.writer.process
        entered = asyncio.Event()
        original = child.stdin.drain
        async def delayed_drain():
            entered.set()
            await asyncio.sleep(.2)
            await original()
        child.stdin.drain = delayed_drain
        closing = asyncio.create_task(self.writer.close())
        await entered.wait()
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        await asyncio.sleep(.35)
        self.assertIsNotNone(child.returncode)
        self.assertIsNone(self.writer.key)

    async def test_real_child_constructor_failure_reaps_exact_pid(self):
        sleeper = self.home / 'constructor-sleep'
        sleeper.write_text('#!/bin/sh\nexec /usr/bin/sleep 30\n')
        sleeper.chmod(0o700)
        self.writer.executable = str(sleeper)
        baseline = subprocess.Popen(['/usr/bin/sleep', '30'])
        born = []
        original = subprocess.Popen._execute_child
        def fail_after_os_child(process, *args, **kwargs):
            original(process, *args, **kwargs)
            born.append(process.pid)
            raise RuntimeError('injected post-child constructor failure')
        try:
            with patch.object(subprocess.Popen, '_execute_child', fail_after_os_child):
                with self.assertRaises(ControlError):
                    await asyncio.wait_for(
                        self.writer.open(PEER, 'default', 'chat-a', 'term_1'), 1)
            self.assertEqual(len(born), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(born[0], 0)
            self.assertIsNone(self.writer.key)
            self.assertIsNone(self.writer.process)
            self.assertIsNone(baseline.poll())
        finally:
            baseline.kill()
            baseline.wait()
            for pid in born:
                try:
                    os.kill(pid, 9)  # Only the exact disposable child.
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass

    async def test_synchronous_child_closes_pipes_after_reap(self):
        await self.writer.open(PEER, 'default', 'chat-a', 'term_1')
        child = self.writer.process
        await self.writer.close()
        self.assertIsNotNone(child.returncode)
        self.assertTrue(child.raw.stdin.closed)
        self.assertTrue(child.raw.stdout.closed)

    async def test_real_child_cancelled_at_post_spawn_boundary_is_reaped(self):
        sleeper = self.home / 'disposable-sleep'
        sleeper.write_text('#!/bin/sh\nexec /usr/bin/sleep 30\n')
        sleeper.chmod(0o700)
        self.writer.executable = str(sleeper)
        baseline = subprocess.Popen(['/usr/bin/sleep', '30'])
        born = []
        original = subprocess.Popen._execute_child
        def cancel_after_os_child(process, *args, **kwargs):
            original(process, *args, **kwargs)
            born.append(process.pid)
            asyncio.current_task().cancel()
        try:
            with patch.object(subprocess.Popen, '_execute_child', cancel_after_os_child):
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(
                        self.writer.open(PEER, 'default', 'chat-a', 'term_1'), 1)
            self.assertEqual(len(born), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(born[0], 0)
            self.assertIsNone(self.writer.key)
            self.assertIsNone(baseline.poll())
        finally:
            baseline.kill()
            baseline.wait()
            for pid in born:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass

    async def test_real_child_failed_kill_keeps_reservation_until_reap(self):
        sleeper = self.home / 'disposable-sleep'
        sleeper.write_text('#!/bin/sh\nexec /usr/bin/sleep 30\n')
        sleeper.chmod(0o700)
        self.writer.executable = str(sleeper)
        self.writer.timeout = .03
        opening = asyncio.create_task(self.writer.open(PEER, 'default', 'chat-a', 'term_1'))
        while self.writer.process is None:
            await asyncio.sleep(.001)
        child = self.writer.process
        pid = child.pid
        other = ControlWriter(self.state, self.source, executable=self.fake,
                              home=self.home, timeout=.03)
        try:
            def failed_kill(_child):
                raise OSError('injected kill failure')
            with patch.object(_Child, 'kill', failed_kill):
                with self.assertRaises(ControlError):
                    await opening
                self.assertIsNone(child.returncode)
                self.assertIsNotNone(self.writer.key)
                with self.assertRaisesRegex(ControlError, 'occupied'):
                    await other.open(PEER, 'default', 'chat-a', 'term_1')
            await self.writer.close()
            self.assertIsNotNone(child.returncode)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertIsNone(self.writer.key)
        finally:
            if not opening.done():
                opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
            if child.returncode is None:
                child.kill()
                await child.wait()
            await self.writer.close()
