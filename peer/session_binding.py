"""Read-only, fail-closed process proof for Herdr's observed Hermes pane.

Pass a SessionBinding instance as LocalSource(verified_metadata=...). Herdr's
independent agent_session claim is checked by LocalSource *after* this callback;
this module cannot establish that claim from the callback's pane argument.
It never acquires a lease, calls Hermes's pruning snapshot, or reads chat data.
"""

import json
import hashlib
import math
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_PANE = re.compile(r'w[A-Za-z0-9]+:p[A-Za-z0-9]+\Z')
_TAB = re.compile(r'w[A-Za-z0-9]+:t[A-Za-z0-9]+\Z')
_ENV = (('HERDR_PANE_ID', 'paneId'), ('HERDR_TAB_ID', 'tabId'),
        ('HERDR_SOCKET_PATH', 'socketPath'))
_MAX_REGISTRY = 1024 * 1024
_MAX_ENV = 256 * 1024
_MAX_STAT = 8192
_MAX_PROC_SUMMARY = 1024 * 1024


class _Unproven(Exception):
    """Incomplete or inconsistent proof; do not disclose owner details."""


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Unproven()
        result[key] = value
    return result


def _bounded(path, limit):
    with path.open('rb') as handle:
        content = handle.read(limit + 1)
    if not content or len(content) > limit:
        raise _Unproven()
    return content


class SessionBinding:
    """Callable proof scoped to explicit {profile: (profile_home, state_db)}.

    Return a six-field proof or None. Only use the result with LocalSource,
    which separately checks a fresh Herdr agent_session claim and terminal ID.
    This is observation, NOT a lease/authorization to submit or launch work.
    """

    def __init__(self, profiles, *, proc_root='/proc'):
        self.profiles = {name: (Path(home), Path(db))
                         for name, (home, db) in profiles.items()}
        self.proc_root = Path(proc_root)
        # Instance-scoped read caches keyed by filesystem identity. A registry or
        # database rewrite (atomic replace or size/mtime change) invalidates the
        # entry, so mid-proof lease/session changes are still detected; a process
        # environment is immutable after exec, so (pid, start) is a safe key.
        self._registry_cache = {}
        self._env_cache = {}
        self._stored_cache = {}
        self._start_cache = {}
        self._btime = None

    @staticmethod
    def _stat_sig(path):
        st = path.stat()
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def _proc_stat_path(self, pid):
        return self.proc_root / str(pid) / 'stat'

    def _started_at(self, pid):
        # /proc stat's comm may contain spaces and closing parentheses.
        text = _bounded(self._proc_stat_path(pid), _MAX_STAT).decode('ascii')
        if not text.startswith(str(pid) + ' (') or ') ' not in text:
            raise _Unproven()
        fields = text.rsplit(') ', 1)[1].split()
        if len(fields) < 20 or fields[0] == 'Z':
            raise _Unproven()
        ticks = int(fields[19])
        if ticks <= 0:
            raise _Unproven()
        return self._boot_time() + ticks / self._clock_ticks()

    def _started_at_for(self, pid):
        # Process start is immutable while the process lives; a stale entry can
        # only disagree with a fresh registry row, which fails closed. Failures
        # are never cached. The pre-disclosure re-check in _prove still reads
        # /proc fresh so mid-proof PID reuse is caught.
        if pid not in self._start_cache:
            if len(self._start_cache) >= 8192:
                self._start_cache.clear()
            self._start_cache[pid] = self._started_at(pid)
        return self._start_cache[pid]

    def _boot_time(self):
        if self._btime is None:
            boot = _bounded(self.proc_root / 'stat', _MAX_PROC_SUMMARY).decode('ascii')
            btimes = [line.split() for line in boot.splitlines() if line.startswith('btime ')]
            if len(btimes) != 1 or len(btimes[0]) != 2:
                raise _Unproven()
            self._btime = int(btimes[0][1])
        return self._btime

    def _clock_ticks(self):
        hz = os.sysconf('SC_CLK_TCK')
        if hz <= 0:
            raise _Unproven()
        return hz

    def _environment(self, pid):
        # Do not decode, log, or retain unrelated environment entries.
        wanted = {key.encode(): key for key, _ in _ENV}
        found = {}
        for field in _bounded(self.proc_root / str(pid) / 'environ', _MAX_ENV).split(b'\0'):
            key, separator, value = field.partition(b'=')
            if key in wanted:
                if not separator or key in found:
                    raise _Unproven()
                found[key] = value.decode('utf-8')
        if set(found) != set(wanted):
            raise _Unproven()
        return {wanted[key]: value for key, value in found.items()}

    def _environment_for(self, pid, started):
        key = (pid, started)
        if key not in self._env_cache:
            if len(self._env_cache) >= 4096:
                self._env_cache.clear()
            self._env_cache[key] = self._environment(pid)
        return self._env_cache[key]

    def _entries(self, home):
        path = home / 'runtime' / 'active_sessions.json'
        try:
            sig = self._stat_sig(path)
        except OSError:
            sig = None
        if sig is not None:
            cached = self._registry_cache.get(path)
            if cached is not None and cached[0] == sig:
                return cached[1]
        try:
            data = json.loads(_bounded(path, _MAX_REGISTRY), object_pairs_hook=_unique_keys)
        except FileNotFoundError:
            return []
        if not isinstance(data, dict) or set(data) != {'entries'} or not isinstance(data['entries'], list):
            raise _Unproven()
        if len(data['entries']) > 4096:
            raise _Unproven()
        seen = set()
        valid = []
        for row in data['entries']:
            # Hermes also records gateway routing leases here (colon session
            # keys, no live_session_id). Those rows are never pane proof, so
            # skip them; one malformed row must not blind every valid owner.
            if not isinstance(row, dict) or not isinstance(row.get('metadata'), dict):
                continue
            lease = row.get('lease_id')
            sid = row.get('session_id')
            pid = row.get('pid')
            started = row.get('process_start_time')
            if (not isinstance(lease, str) or not lease
                    or not isinstance(sid, str) or not _ID.fullmatch(sid)
                    or type(pid) is not int or pid <= 0
                    or type(started) not in (int, float) or not math.isfinite(started) or started <= 0
                    or not isinstance(row['metadata'].get('live_session_id'), str)
                    or not row['metadata']['live_session_id']):
                continue
            if lease in seen:
                raise _Unproven()
            seen.add(lease)
            valid.append(row)
        if sig is not None:
            if len(self._registry_cache) >= 64:
                self._registry_cache.clear()
            self._registry_cache[path] = (sig, valid)
        return valid

    def _stored(self, db_path, sid):
        # mode=ro is essential: ordinary sqlite connections can create a missing DB.
        try:
            sig = self._stat_sig(db_path)
        except OSError:
            sig = None
        if sig is not None:
            cached = self._stored_cache.get(db_path)
            if cached is not None and cached[0] == sig:
                return sid in cached[1]
        uri = db_path.resolve(strict=True).as_uri() + '?mode=ro&immutable=1'
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            if sig is None:
                return db.execute('SELECT 1 FROM sessions WHERE id = ? LIMIT 1', (sid,)).fetchone() is not None
            rows = db.execute('SELECT id FROM sessions LIMIT 262144').fetchall()
            ids = {row[0] for row in rows if type(row[0]) is str}
        if len(self._stored_cache) >= 64:
            self._stored_cache.clear()
        self._stored_cache[db_path] = (sig, ids)
        return sid in ids

    def _prove(self, pane, *, include_generation=False):
        if not isinstance(pane, dict) or pane.get('agentKind') != 'hermes':
            raise _Unproven()
        if not (isinstance(pane.get('paneId'), str) and _PANE.fullmatch(pane['paneId'])
                and isinstance(pane.get('tabId'), str) and _TAB.fullmatch(pane['tabId'])
                and isinstance(pane.get('terminalId'), str) and _ID.fullmatch(pane['terminalId'])
                and isinstance(pane.get('socketPath'), str) and pane['socketPath'].startswith('/')
                and '\x00' not in pane['socketPath']):
            raise _Unproven()
        matches = []
        live_owners = {}
        registry_snapshots = []
        for profile, (home, db) in self.profiles.items():
            if not isinstance(profile, str) or not _ID.fullmatch(profile):
                raise _Unproven()
            # No cross-profile DB aliases: an entry in a different home is not
            # evidence for this profile's stored session.
            if not db.resolve(strict=True).is_relative_to(home.resolve(strict=True)):
                raise _Unproven()
            entries = self._entries(home)
            registry_snapshots.append((home, entries))
            for row in entries:
                pid = row['pid']
                # Stale rows are not candidates; unreadable liveness is not proof.
                try:
                    started = self._started_at_for(pid)
                except FileNotFoundError:
                    continue
                if abs(started - row['process_start_time']) > 0.02:
                    continue
                live_owners[row['session_id']] = live_owners.get(row['session_id'], 0) + 1
                env = self._environment_for(pid, started)
                if not all(env[key] == pane[field] for key, field in _ENV):
                    continue
                matches.append((profile, db, row, pid, started))
        if len(matches) != 1:
            raise _Unproven()
        profile, db, row, pid, started = matches[0]
        sid = row['session_id']
        if row['metadata']['live_session_id'] != sid or live_owners[sid] != 1 or not self._stored(db, sid):
            raise _Unproven()
        # Re-read after SQLite and the process environment to catch a reused
        # PID during observation, without changing the registry or the process.
        if abs(self._started_at(pid) - started) > 0.02:
            raise _Unproven()
        # A lease may be withdrawn or replaced while SQLite and /proc are read.
        # Refuse even an otherwise identical owner with a new lease or source.
        for home, entries in registry_snapshots:
            if self._entries(home) != entries:
                raise _Unproven()
        proof = {key: pane[key] for key in ('paneId', 'tabId', 'terminalId', 'socketPath')} | {
            'sessionId': sid, 'profile': profile}
        if include_generation:
            # This is the independent Hermes process/lease component only.
            # LocalSource must additionally hash source-authenticated Herdr
            # server_instance_id and per-pane runtime_generation; absent
            # attestation can never yield a write-grant generation.
            basis = (profile, sid, row['lease_id'], pid, started,
                     pane['socketPath'], pane['paneId'], pane['tabId'], pane['terminalId'])
            proof['terminalGeneration'] = hashlib.sha256(
                json.dumps(basis, separators=(',', ':')).encode()).hexdigest()
        return proof

    def __call__(self, pane):
        try:
            return self._prove(pane)
        except (OSError, ValueError, TypeError, UnicodeError, sqlite3.Error, _Unproven):
            return None

    def generation_for(self, pane):
        """Fresh process/lease generation; only meaningful with a Herdr claim."""
        try:
            return self._prove(pane, include_generation=True)
        except (OSError, ValueError, TypeError, UnicodeError, sqlite3.Error, _Unproven):
            return None
