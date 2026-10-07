"""Disposable real Herdr and Hermes: claim absence must refuse exact resume.

No live Herdr, no direct PTY, no input, and no agent-claim fabrication.
"""
import json
import os
import pwd
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from peer.herdr_source import LocalSource, _start_ticks
from peer.session_binding import SessionBinding
from peer.new_lifecycle import NewLifecycle, HerdrCliDriver, CreationRefused

HERMES_SOURCE = Path.home() / '.hermes/hermes-agent'
HERMES = next(iter(sorted(Path(pwd.getpwuid(os.getuid()).pw_dir).glob(
    '.hermes/installs/*/environments/*/venv/bin/hermes'))),
    Path('/nonexistent/hermes'))
SID = '20260928_120000_newherdrfixture'


@unittest.skipUnless(os.environ.get('ARES_REAL_HERDR_FIXTURE') == '1',
                     'Requires opt-in disposable Herdr/Hermes fixture')
class RealHerdrResume(unittest.TestCase):
    def test_real_exact_receipt_and_lease_cannot_substitute_for_missing_herdr_claim(self):
        import hermes_bootstrap  # Resolve installed Hermes dependencies only for opt-in fixture.
        from hermes_state import SessionDB
        with tempfile.TemporaryDirectory(prefix='ares-herdr-', dir=Path.home() / '.cache') as root:
            root = Path(root)
            home = root / '.hermes'
            home.mkdir(mode=0o700)
            db = SessionDB(db_path=home / 'state.db')
            db.create_session(SID, source='cli')
            db.append_message(SID, 'user', 'fixture-user')
            db.append_message(SID, 'assistant', 'fixture-assistant')
            db.close()
            registry = home / 'runtime/active_sessions.json'
            registry.parent.mkdir(mode=0o700)
            registry.write_text('{"entries": []}')
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL')}
            env.update(HOME=str(root), HERMES_HOME=str(home),
                XDG_CONFIG_HOME=str(root / '.config'), XDG_CACHE_HOME=str(root / '.cache'),
                XDG_DATA_HOME=str(root / '.local/share'), SHELL='/bin/bash',
                TERM='xterm-256color', OPENAI_API_KEY='fixture-not-a-real-key',
                HERMES_DEFER_AGENT_STARTUP='1', HERMES_DISABLE_LAZY_INSTALLS='1',
                PYTHONPATH=str(HERMES_SOURCE), PYTHONDONTWRITEBYTECODE='1')
            sock = root / '.config/herdr/sessions/ares-fixture/herdr.sock'
            server = subprocess.Popen(['/usr/bin/herdr', '--session', 'ares-fixture', 'server'],
                                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            owner = None
            def cli(*args):
                result = subprocess.run(['/usr/bin/herdr', *args], env=env, capture_output=True,
                                        text=True, timeout=8)
                self.assertEqual(result.returncode, 0, (args, result.stderr))
                return json.loads(result.stdout) if result.stdout.strip() else {}
            try:
                deadline = time.monotonic() + 8
                while not sock.is_socket() and server.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(sock.is_socket())
                env['HERDR_SOCKET_PATH'] = str(sock)
                installed = subprocess.run(['/usr/bin/herdr', 'integration', 'install', 'hermes'],
                                           env=env, capture_output=True, timeout=8)
                self.assertEqual(installed.returncode, 0)
                proof = SessionBinding({'default': (home, home / 'state.db')})
                source = LocalSource({'default': home / 'state.db'}, [sock], verified_metadata=proof)
                driver = HerdrCliDriver('/usr/bin/herdr', str(HERMES), env, sock,
                                        root, Path(__file__).parents[1] / 'peer/herdr_launch.py')
                sut = NewLifecycle(source, driver, {'default': (home, home / 'state.db')},
                                   root / 'ready.json', process_birth=_start_ticks, timeout=4)
                with self.assertRaises(CreationRefused):
                    sut.resume('default', 'cli', SID)
                receipt = json.loads((root / 'ready.json').read_text())
                owner = receipt['pid']
                self.assertEqual((receipt['protocol'], receipt['sessionId'], receipt['messageCount']),
                    ('hermes-exact-resume-ready-v1', SID, 2))
                self.assertEqual(json.loads(registry.read_text())['entries'][0]['pid'], owner)
                self.assertEqual([t['sessionId'] for t in source.inventory()['terminals']], [''])
                self.assertEqual(sut.bindings, {})
                self.assertEqual(sut.quarantine, {('default', 'cli', SID)})
                print('DISPOSABLE_RESUME_REFUSED_MISSING_CLAIM',
                      json.dumps({'receipt': True, 'lease': True, 'claim': False,
                                  'inputReady': False, 'quarantined': True}))
            finally:
                if server.poll() is None:
                    try:
                        cli('server', 'stop')
                    except Exception:
                        server.terminate()
                try:
                    server.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.communicate()
                self.assertIsNotNone(server.returncode)
                if owner is not None:
                    deadline = time.monotonic() + 6
                    while Path('/proc', str(owner)).exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertFalse(Path('/proc', str(owner)).exists(), 'Hermes child survived fixture shutdown')


if __name__ == '__main__':
    unittest.main()
