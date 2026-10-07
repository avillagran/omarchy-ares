"""Synthetic CLI/PTY contract: never connect to a live Herdr terminal."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from peer.observe import ObserveAdapter, ObserveError


class Source:
    def __init__(self, terminal):
        self.terminal = dict(terminal)
        self.calls = 0

    def inventory(self):
        self.calls += 1
        return {'complete': True, 'terminals': [dict(self.terminal)]}


class ObserveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'call.json'
        self.cli = self.root / 'fake-herdr'
        self.cli.write_text('#!/usr/bin/env python3\n'
            'import fcntl,json,os,struct,sys,termios,time\n'
            'size=struct.unpack("HHHH",fcntl.ioctl(1,termios.TIOCGWINSZ,b"\\0"*8))\n'
            'with open(os.environ["TEST_LOG"],"w") as f:\n'
            ' json.dump({"argv":sys.argv[1:],"size":size[:2],"tty":os.isatty(1),'
            '"pid":os.getpid(),"herdr":{k:v for k,v in os.environ.items() if k.startswith("HERDR_")}},f)\n'
            'os.write(1,b"synthetic bytes\\r\\n")\n'
            'if os.environ.get("TEST_WAIT"): time.sleep(30)\n')
        self.cli.chmod(0o755)
        self.target = {'socketPath': str(self.root / 'herdr.sock'), 'paneId': 'w1:p1',
                       'tabId': 'w1:t1', 'terminalId': 'term_1', 'agentKind': 'hermes',
                       'sessionId': 'chat-a', 'profile': 'default', 'identitySource': 'verified'}
        self.source = Source(self.target)
        self.env = dict(os.environ, TEST_LOG=str(self.log), HERDR_PANE_ID='untrusted',
                        HERDR_SOCKET_PATH='/wrong.sock')
        self.adapter = ObserveAdapter(self.source, str(self.cli), env=self.env)

    def test_exact_command_pty_size_and_scrubbed_environment(self):
        chunks = list(self.adapter.observe(self.target, scope='output-only', cols=93, rows=27))
        self.assertIn(b'synthetic bytes', b''.join(chunks))
        log = json.loads(self.log.read_text())
        self.assertEqual(log['argv'], ['terminal', 'session', 'observe', 'term_1',
                                        '--cols', '93', '--rows', '27'])
        self.assertEqual(log['size'], [27, 93])
        self.assertTrue(log['tty'])
        self.assertEqual(log['herdr'], {'HERDR_SOCKET_PATH': self.target['socketPath']})
        self.assertGreaterEqual(self.source.calls, 1)

    def test_unverified_session_never_launches_observer(self):
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(dict(self.target, sessionId="", profile="", identitySource=""), scope="output-only", cols=80, rows=24))
        self.assertFalse(self.log.exists())

    def test_two_owners_for_same_verified_session_refused(self):
        target = dict(self.target, sessionId="chat-a", profile="default", identitySource="verified")
        self.source.terminal = target
        self.source.inventory = lambda: {"complete": True, "terminals": [
            target, dict(target, paneId="w1:p2", terminalId="term_2")]}
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(target, scope="output-only", cols=80, rows=24))
        self.assertFalse(self.log.exists())

    def test_swapped_terminal_rejected_before_process_launch(self):
        self.source.terminal['terminalId'] = 'reused'
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24))
        self.assertFalse(self.log.exists())

    def test_swapped_pane_tab_socket_and_binding_rejected(self):
        for key, value in [('paneId', 'w1:p2'), ('tabId', 'w1:t2'),
                           ('socketPath', '/other.sock'), ('agentKind', 'shell'),
                           ('sessionId', 'claim')]:
            self.source.terminal = dict(self.target, **{key: value})
            with self.subTest(key=key), self.assertRaises(ObserveError):
                list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24))
            self.assertFalse(self.log.exists())

    def test_incomplete_inventory_rejected(self):
        self.source.inventory = lambda: {'complete': False, 'terminals': [self.target]}
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24))
        self.assertFalse(self.log.exists())

    def test_invalid_identity_and_dimensions_rejected(self):
        for cols, rows in [(0, 24), (80, 0), (True, 24), (4097, 24), (80, 2049)]:
            with self.subTest(cols=cols, rows=rows), self.assertRaises(ObserveError):
                list(self.adapter.observe(self.target, scope='output-only', cols=cols, rows=rows))
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(dict(self.target, terminalId='--takeover'), scope='output-only', cols=80, rows=24))
        for scope in ('input', 'attach', 'takeover', 'history', None):
            with self.subTest(scope=scope), self.assertRaises(ObserveError):
                list(self.adapter.observe(self.target, scope=scope, cols=80, rows=24))
        self.assertFalse(self.log.exists())

    def test_cancellation_reaps_child_and_releases_capacity(self):
        cancel = threading.Event()
        self.adapter = ObserveAdapter(self.source, str(self.cli), env=dict(self.env, TEST_WAIT='1'), max_concurrent=1)
        stream = self.adapter.observe(self.target, scope='output-only', cols=80, rows=24, cancel=cancel)
        self.assertIn(b'synthetic', next(stream))
        pid = json.loads(self.log.read_text())['pid']
        cancel.set()
        with self.assertRaises(ObserveError):
            next(stream)
        stream.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        # A second stream must not be blocked by the cancelled child's permit.
        stream2 = self.adapter.observe(self.target, scope='output-only', cols=80, rows=24, cancel=threading.Event())
        self.assertIn(b'synthetic', next(stream2))
        stream2.close()

    def test_snapshot_returns_quiet_frame_and_reaps_persistent_observer(self):
        self.adapter = ObserveAdapter(self.source, str(self.cli), env=dict(self.env, TEST_WAIT='1'))
        start = time.monotonic()
        frame = b''.join(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24,
                                               max_bytes=8192, timeout=2, snapshot=True))
        self.assertIn(b'synthetic bytes', frame)
        self.assertLess(time.monotonic() - start, 2)
        pid = json.loads(self.log.read_text())['pid']
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_byte_and_time_limits(self):
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24, max_bytes=4))
        self.adapter = ObserveAdapter(self.source, str(self.cli), env=dict(self.env, TEST_WAIT='1'))
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24, timeout=0.2))

    def test_inventory_delay_exhausts_timeout_before_launch(self):
        def slow_inventory():
            time.sleep(0.08)
            return {'complete': True, 'terminals': [self.target]}
        self.source.inventory = slow_inventory
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24, timeout=0.05))
        self.assertFalse(self.log.exists())

    def test_duplicate_target_rejected(self):
        self.source.inventory = lambda: {'complete': True, 'terminals': [self.target, self.target]}
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24))
        self.assertFalse(self.log.exists())

    def test_duplicate_terminal_on_other_pane_rejected(self):
        self.source.inventory = lambda: {'complete': True, 'terminals': [
            self.target, dict(self.target, paneId='w1:p2')]}
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24))
        self.assertFalse(self.log.exists())

    def test_concurrency_gate_rejects_without_launch(self):
        self.adapter = ObserveAdapter(self.source, str(self.cli), env=dict(self.env, TEST_WAIT='1'), max_concurrent=1)
        stream = self.adapter.observe(self.target, scope='output-only', cols=80, rows=24)
        self.assertIn(b'synthetic', next(stream))
        with self.assertRaises(ObserveError):
            list(self.adapter.observe(self.target, scope='output-only', cols=80, rows=24))
        stream.close()


if __name__ == '__main__':
    unittest.main()
