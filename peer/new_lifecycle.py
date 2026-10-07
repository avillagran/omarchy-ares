"""Checkout-only source-local lifecycle, never installed or exposed on Peer wire.

A creation/readback experiment, NOT an input grant. Ambiguous launches are
quarantined; no automatic retry, pane takeover, direct PTY or SSH fallback.
"""
import os
from pathlib import Path
import re
import stat
import time

from .session_supervisor import SessionSupervisor, ResumeRefused

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')


class CreationRefused(Exception):
    pass


class HerdrCliDriver:
    """A source-local CLI adapter, restricted to an isolated socket/profile.

    No SSH, direct PTY, pane send-text, or report-agent-session calls.
    """
    def __init__(self, binary, hermes, environment, socket_path, cwd, launcher):
        self.binary = str(binary)
        self.hermes = str(hermes)
        self.environment = dict(environment)
        self.socket_path = str(socket_path)
        self.cwd = Path(cwd)
        self.launcher = str(launcher)
        owner_home = Path(self.environment.get('HOME', ''))
        profile_home = Path(self.environment.get('HERMES_HOME', ''))
        socket = Path(self.socket_path)
        if (not owner_home.is_absolute() or owner_home == Path.home()
                or not profile_home.is_relative_to(owner_home)
                or not self.cwd.is_relative_to(owner_home)
                or not socket.is_relative_to(owner_home / '.config/herdr/sessions')
                or '..' in socket.parts or '..' in profile_home.parts):
            raise CreationRefused('This checkout driver requires isolated source paths')
        self.environment['HERDR_SOCKET_PATH'] = self.socket_path

    def _call(self, *argv):
        import json
        import subprocess
        completed = subprocess.run([self.binary, *argv], env=self.environment,
                                   capture_output=True, text=True, timeout=8)
        if completed.returncode:
            raise CreationRefused('Herdr command refused')
        return json.loads(completed.stdout) if completed.stdout.strip() else {}

    def provision(self):
        self._call('workspace', 'create', '--label', 'Ares disposable resume',
                   '--cwd', str(self.cwd), '--no-focus',
                   '--env', 'HERMES_HOME=' + self.environment['HERMES_HOME'],
                   '--env', 'PYTHONPATH=' + self.environment['PYTHONPATH'],
                   '--env', 'HERDR_ENV=1',
                   '--env', 'HERMES_DEFER_AGENT_STARTUP=1',
                   '--env', 'HERMES_DISABLE_LAZY_INSTALLS=1')
        response = self._call('api', 'snapshot')
        snap = response['result']['snapshot']
        workspaces = [w for w in snap['workspaces'] if w.get('label') == 'Ares disposable resume']
        if len(workspaces) != 1:
            raise CreationRefused('New Herdr workspace ambiguous')
        workspace_id = workspaces[0]['workspace_id']
        panes = [p for p in snap['panes'] if p.get('workspace_id') == workspace_id]
        if len(panes) != 1:
            raise CreationRefused('New Herdr pane ambiguous')
        pane = panes[0]
        return {'socketPath': self.socket_path, 'paneId': pane['pane_id'],
                'tabId': pane['tab_id'], 'terminalId': pane['terminal_id']}

    def launch(self, pane, profile, source, sid, receipt):
        import shlex
        import sys
        command = shlex.join([sys.executable, self.launcher, self.hermes,
                              profile, source, sid, str(receipt)])
        self._call('pane', 'run', pane['paneId'], command)


class NewLifecycle:
    def __init__(self, source, driver, profiles, receipt, *, process_birth, timeout=30):
        self.source = source
        self.driver = driver
        self.profiles = profiles
        self.receipt = Path(receipt)
        self.timeout = timeout
        self.supervisor = SessionSupervisor(profiles, source, None, process_birth=process_birth)
        self.bindings = self.supervisor.bindings
        self.quarantine = set()

    def create(self, profile, title):
        # Fresh Hermes has no inherited, authenticated session-ID readiness
        # receipt. A pane title or DB recency cannot fill that gap.
        raise CreationRefused('New session identity receipt unavailable')

    def _read_receipt(self):
        fd = os.open(self.receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            mode = os.fstat(fd)
            if (mode.st_uid != os.getuid() or not stat.S_ISREG(mode.st_mode)
                    or stat.S_IMODE(mode.st_mode) != 0o600):
                raise CreationRefused('Untrusted readiness receipt')
            raw = os.read(fd, 1025)
            if len(raw) > 1024:
                raise CreationRefused('Oversized readiness receipt')
            return raw
        finally:
            os.close(fd)

    def resume(self, profile, source, sid):
        if (source != 'cli' or profile not in self.profiles
                or not isinstance(sid, str) or not _ID.fullmatch(sid)):
            raise CreationRefused('Only an exact stored CLI session may resume')
        key = profile, source, sid
        if key in self.quarantine or key in self.bindings or self.receipt.exists():
            raise CreationRefused('Prior attempt requires manual reconciliation')
        try:
            self.stage = 'history'
            before = self.supervisor._history(*key)
            self.stage = 'registry'
            registry = self.supervisor._registry()
            if any(row.get('session_id') == sid for _, row in registry):
                raise CreationRefused('Session has an existing owner')
            self.stage = 'inventory-before'
            initial = self.supervisor._panes(self.source.inventory())
            if any(p.get('sessionId') == sid for p in initial):
                raise CreationRefused('Existing terminal identity')
            # The source-local driver creates a wholly new pane; never reuse
            # an observed empty shell whose provenance cannot be established.
            # Even a lost workspace-create reply may already have made a pane.
            self.quarantine.add(key)
            self.stage = 'provision'
            pane = self.driver.provision()
            self.stage = 'new-pane-readback'
            target = self.supervisor._unique_pane(pane)
            if (target.get('agentKind') != 'shell' or target.get('sessionId')
                    or self.supervisor._pane_key(target) in {
                        self.supervisor._pane_key(p) for p in initial}):
                raise CreationRefused('New pane was not empty and unique')
            self.stage = 'launch'
            self.driver.launch(pane, profile, source, sid, self.receipt)
            self.stage = 'readiness'
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self.receipt.exists():
                    raw = self._read_receipt()
                    import json
                    record = json.loads(raw)
                    try:
                        self.supervisor._source_claim(pane, profile, sid)
                    except ResumeRefused:
                        time.sleep(.03)
                        continue
                    result = self.supervisor.accept_ready(
                        *key, pane, record['pid'], raw, before)
                    self.quarantine.discard(key)
                    return {'profile': profile, 'sessionId': sid,
                            'paneId': pane['paneId'], 'tabId': pane['tabId'],
                            'terminalId': pane['terminalId'], 'pid': result['pid'],
                            'inputReady': False}
                time.sleep(.03)
            raise CreationRefused('Readiness timed out')
        except (OSError, ValueError, KeyError, TypeError, ResumeRefused, CreationRefused) as exc:
            raise CreationRefused('Exact lifecycle could not be proven') from None
