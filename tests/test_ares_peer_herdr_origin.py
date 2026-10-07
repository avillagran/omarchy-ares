"""Disposable Unix servers: socket replies alone cannot certify a writer."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from peer.herdr_source import LocalSource
from peer.core import PeerState, PeerError
from peer.__main__ import trusted_herdr_executable

SERVER = r'''
import json, os, socket, sys
path, epoch = sys.argv[1:]
s = socket.socket(socket.AF_UNIX)
s.bind(path)
os.chmod(path, 0o600)
s.listen(8)
while True:
    conn, _ = s.accept()
    if epoch == 'forked-responder':
        child = os.fork()
        if child:
            conn.close()
            os.waitpid(child, 0)
            continue
        s.close()
    with conn:
        request = json.loads(conn.makefile('rb').readline())
        snapshot = {'server_instance_id': epoch, 'protected_control_fence_version': 1,
          'workspaces': [], 'tabs': [{'tab_id': 'w1:t1'}],
          'panes': [{'pane_id': 'w1:p1', 'tab_id': 'w1:t1',
                     'terminal_id': 'term_1', 'runtime_generation': 'runtime-1'}],
          'agents': [{'agent': 'hermes', 'pane_id': 'w1:p1', 'tab_id': 'w1:t1',
                      'terminal_id': 'term_1', 'agent_session': {'agent': 'hermes',
                      'kind': 'id', 'source': 'herdr:hermes', 'value': 'chat-a'}}]}
        conn.sendall(json.dumps({'id': request['id'], 'result': {'snapshot': snapshot}}).encode() + b'\n')
    if epoch == 'forked-responder':
        os._exit(0)
'''

class Proof:
    def __call__(self, item):
        return dict(item, sessionId='chat-a', profile='default')
    def generation_for(self, item):
        return dict(item, terminalGeneration='lease-one')

class OriginTest(unittest.TestCase):
    def setUp(self):
        # A private test tree under a trusted real-home ancestor; /tmp itself
        # is world writable and must not qualify as a pin ancestor.
        self.tmp = tempfile.TemporaryDirectory(dir=pwd.getpwuid(os.getuid()).pw_dir)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.sock = self.root / 'herdr.sock'
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            child.stderr.close()

    def launch(self, epoch='same-epoch'):
        child = subprocess.Popen([sys.executable, '-c', SERVER, str(self.sock), epoch],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.children.append(child)
        for _ in range(200):
            if self.sock.is_socket():
                return child
            if child.poll() is not None:
                self.fail(child.stderr.read().decode())
            time.sleep(.01)
        self.fail('fake server socket did not appear')

    def source(self, binary=None):
        return LocalSource({'default': self.root / 'unused.db'}, [str(self.sock)],
                           verified_metadata=Proof(), trusted_herdr_executable=binary)

    def item(self, source):
        return source.inventory()['terminals'][0]

    def test_unconfigured_origin_preserves_observation_but_never_grants(self):
        self.launch()
        item = self.item(self.source())
        self.assertEqual(item['identitySource'], 'verified')
        self.assertNotIn('terminalGeneration', item)

    def test_matching_process_and_executable_cannot_prove_connected_listener(self):
        self.launch()
        item = self.item(self.source(Path(sys.executable).resolve()))
        self.assertNotIn('terminalGeneration', item)

    def test_wrong_executable_and_world_readable_parent_refuse_writer(self):
        self.launch()
        self.assertNotIn('terminalGeneration', self.item(self.source('/bin/sh')))
        self.root.chmod(0o755)
        self.assertNotIn('terminalGeneration', self.item(self.source(Path(sys.executable).resolve())))

    def test_restart_with_same_epoch_revokes_generation(self):
        old = self.launch()
        source = self.source(Path(sys.executable).resolve())
        before = self.item(source)
        self.assertNotIn('terminalGeneration', before)
        old.terminate()
        old.wait(timeout=5)
        self.sock.unlink()
        self.launch()
        after = self.item(source)
        self.assertNotIn('terminalGeneration', after)

    def test_grant_recheck_revokes_on_same_epoch_server_restart(self):
        old = self.launch()
        source = self.source(Path(sys.executable).resolve())
        peer = 'a' * 32
        state = PeerState(self.root / 'peer')
        state.tokens[peer] = 'fake-pairing-digest'
        with self.assertRaises(PeerError):
            state.set_write_grant(peer, 'default', 'chat-a', 'term_1', True, 30, source)
        self.assertFalse(state.write_allowed(peer, source))
        old.terminate()
        old.wait(timeout=5)
        self.sock.unlink()
        self.launch()
        self.assertFalse(state.write_allowed(peer, source))

    def test_socket_swap_between_snapshots_refuses_generation(self):
        self.launch()
        source = self.source(Path(sys.executable).resolve())
        original = source._snapshot
        count = 0
        def swap(path):
            nonlocal count
            result = original(path)
            count += 1
            if count == 1:
                self.sock.unlink()
                self.launch()
            return result
        source._snapshot = swap
        self.assertNotIn('terminalGeneration', self.item(source))

    def test_transient_forked_responder_cannot_claim_listener_origin(self):
        """The pinned listener PID is not proof its fork served the connection."""
        self.launch()
        original = self.root / 'original.sock'
        self.sock.rename(original)
        attacker = self.launch('forked-responder')
        forged = self.root / 'forged.sock'
        self.sock.rename(forged)
        original.rename(self.sock)
        source = self.source(Path(sys.executable).resolve())
        snapshot = source._snapshot
        calls = 0
        def transient(path):
            nonlocal calls
            self.sock.rename(original)
            forged.rename(self.sock)
            try:
                result = snapshot(path)
                calls += 1
                return result
            finally:
                self.sock.rename(forged)
                original.rename(self.sock)
        source._snapshot = transient
        item = self.item(source)
        source._snapshot(str(self.sock))
        self.assertEqual(item['identitySource'], 'verified')
        self.assertGreaterEqual(calls, 2, 'attack must survive both origin snapshots')
        self.assertIsNone(item.get('terminalGeneration'))
        self.assertIsNone(attacker.poll())

    def test_socket_mode_change_revokes_existing_grant(self):
        self.launch()
        source = self.source(Path(sys.executable).resolve())
        peer = 'a' * 32
        state = PeerState(self.root / 'peer')
        state.tokens[peer] = 'fake-pairing-digest'
        with self.assertRaises(PeerError):
            state.set_write_grant(peer, 'default', 'chat-a', 'term_1', True, 30, source)
        self.sock.chmod(0o666)
        self.assertFalse(state.write_allowed(peer, source))

    def test_serve_executable_pin_requires_private_owner_configuration(self):
        root = self.root / 'peer'
        root.mkdir(mode=0o700)
        config = root / 'herdr-executable'
        with patch.dict(os.environ, ARES_PEER_STATE=str(root)):
            self.assertIsNone(trusted_herdr_executable())
            config.write_text(str(Path(sys.executable).resolve()) + '\n')
            config.chmod(0o600)
            self.assertEqual(trusted_herdr_executable(), Path(sys.executable).resolve())
            config.chmod(0o644)
            self.assertIsNone(trusted_herdr_executable())
            config.chmod(0o600)
            root.chmod(0o755)
            self.assertIsNone(trusted_herdr_executable())

    def test_pin_refuses_owner_executable_file_and_symlinked_ancestor(self):
        root = self.root / 'peer'
        root.mkdir(mode=0o700)
        config = root / 'herdr-executable'
        config.write_text(str(Path(sys.executable).resolve()) + '\n')
        config.chmod(0o700)
        with patch.dict(os.environ, ARES_PEER_STATE=str(root)):
            self.assertIsNone(trusted_herdr_executable())
        config.chmod(0o600)
        link = self.root / 'alias'
        link.symlink_to(root, target_is_directory=True)
        with patch.dict(os.environ, ARES_PEER_STATE=str(link)):
            self.assertIsNone(trusted_herdr_executable())

    def test_socket_symlinked_ancestor_remains_observation_only(self):
        self.launch()
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        path = alias / 'herdr.sock'
        source = LocalSource({'default': self.root / 'unused.db'}, [str(path)],
                             verified_metadata=Proof(),
                             trusted_herdr_executable=Path(sys.executable).resolve())
        self.assertNotIn('terminalGeneration', self.item(source))

    def test_executable_pin_refuses_symlinked_ancestor(self):
        executable = Path(sys.executable).resolve()
        alias = self.root / 'binary_alias'
        alias.symlink_to(executable.parent, target_is_directory=True)
        source = self.source(alias / executable.name)
        self.assertIsNone(source.trusted_herdr_executable)

if __name__ == '__main__':
    unittest.main()
