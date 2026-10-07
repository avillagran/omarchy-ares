"""Installer contracts without touching the real user service."""
import importlib.machinery
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / 'bin' / 'ares-peer-install'
if not SCRIPT.is_file():
    SCRIPT = Path(__file__).with_name('ares-peer-install')
loader = importlib.machinery.SourceFileLoader('ares_peer_install', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
installer = importlib.util.module_from_spec(spec)
loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_unit_is_user_scoped_and_has_no_ssh(self):
        text = installer.render_unit(9431)
        self.assertIn('ExecStart=%h/.local/share/ares-peer/venv/bin/python -m peer serve --bind 0.0.0.0 --port 9431', text)
        self.assertIn('WorkingDirectory=%h/.local/share/ares-peer/app', text)
        self.assertIn('UMask=0077', text)
        self.assertNotIn('ssh ', text)
        self.assertNotIn('token', text)

    def test_package_copy_skips_artifacts_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source = base / 'source' / 'peer'
            source.mkdir(parents=True)
            (source / '__main__.py').write_text('print(1)\n')
            (source / 'README.md').write_text('x')
            (source / '__pycache__').mkdir()
            (source / '__pycache__' / 'cache.pyc').write_text('x')
            target = base / 'install' / 'app'
            installer.stage_package(source, target)
            self.assertEqual((target / 'peer' / '__main__.py').read_text(), 'print(1)\n')
            self.assertFalse((target / 'peer' / '__pycache__').exists())
            self.assertEqual(stat.S_IMODE((target / 'peer' / '__main__.py').stat().st_mode), 0o600)
            (source / 'bad.py').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                installer.stage_package(source, target)

    def test_install_without_enable_preserves_private_state_and_does_not_start(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            source = home / 'source' / 'peer'
            source.mkdir(parents=True)
            (source / '__main__.py').write_text('pass\n')
            state = home / '.local/share/ares-peer'
            state.mkdir(parents=True)
            (state / 'sentinel').write_text('private')
            commands = []
            def runner(*args):
                commands.append(args)
            installer.install(source, home, 9431, enable=False, runner=runner)
            unit = home / '.config/systemd/user/ares-peer.service'
            self.assertTrue(unit.exists())
            self.assertIn('-m peer serve', unit.read_text())
            self.assertEqual((state / 'sentinel').read_text(), 'private')
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o600)
            self.assertFalse(any('enable' in str(item) or 'start' in str(item) for item in commands))

    def test_invalid_port_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            source = home / 'source' / 'peer'
            source.mkdir(parents=True)
            (source / '__main__.py').write_text('pass\n')
            with self.assertRaises(ValueError):
                installer.install(source, home, 0, enable=False, runner=lambda *args: None)
            self.assertFalse((home / '.local/share/ares-peer').exists())

    def test_enable_restarts_existing_service_after_code_upgrade(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            source = home / 'source' / 'peer'
            source.mkdir(parents=True)
            (source / '__main__.py').write_text('pass\n')
            commands = []
            installer.install(source, home, enable=True, runner=lambda *args: commands.append(args))
            self.assertIn(('systemctl', '--user', 'daemon-reload'), commands)
            self.assertIn(('systemctl', '--user', 'enable', '--now', 'ares-peer.service'), commands)
            self.assertIn(('systemctl', '--user', 'restart', 'ares-peer.service'), commands)


if __name__ == '__main__':
    unittest.main()
