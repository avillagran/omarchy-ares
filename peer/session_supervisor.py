"""Checkout-only, fail-closed Hermes exact-resume eligibility probe.

This module is not wired to Peer serve. It reads a configured source profile's
SQLite and lease registry, and can inspect source-normalized Peer inventory.
LocalSource.inventory() has no authenticated empty-pane/safe-removal claim;
no child readiness FD adapter or gateway handoff receipt is connected. Thus
resume() deliberately refuses before provisioning, spawning, or binding.
"""
import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_PANE = re.compile(r'w[A-Za-z0-9]+:p[A-Za-z0-9]+\Z')
_TAB = re.compile(r'w[A-Za-z0-9]+:t[A-Za-z0-9]+\Z')


class ResumeRefused(Exception):
    """No exact owner/binding was established; never expose private details."""


class SessionSupervisor:
    """Fail-closed exact-history probe scoped to explicitly configured profiles.

    Constructor parameters retain the experimental adapter shape, but no
    source vacancy proof or inherited readiness receipt is available; neither
    the provisioner nor spawner is invoked by resume().
    """

    def __init__(self, profiles, herdr, spawn, *, process_birth, source_authorized=None):
        self.profiles = {name: (Path(home), Path(db)) for name, (home, db) in profiles.items()}
        self.herdr = herdr
        self.spawn = spawn
        self.process_birth = process_birth
        self.source_authorized = source_authorized or (lambda profile, source, sid: False)
        self.bindings = {}
        self.quarantine = set()
        self._inflight = set()
        self._lock = threading.Lock()

    def _identity(self, profile, source, sid):
        if (not all(isinstance(x, str) and _ID.fullmatch(x) for x in (profile, source, sid))
                or profile not in self.profiles):
            raise ResumeRefused('Invalid exact identity')
        return profile, source, sid

    def _registry(self):
        rows = []
        for profile, (home, _) in self.profiles.items():
            path = home / 'runtime' / 'active_sessions.json'
            raw = path.read_bytes()
            if len(raw) > 1024 * 1024:
                raise ResumeRefused('Registry unavailable')
            doc = json.loads(raw)
            if not isinstance(doc, dict) or not isinstance(doc.get('entries'), list) or len(doc['entries']) > 4096:
                raise ResumeRefused('Registry unavailable')
            for entry in doc['entries']:
                if not isinstance(entry, dict) or not isinstance(entry.get('session_id'), str):
                    raise ResumeRefused('Registry unavailable')
                rows.append((profile, entry))
        return rows

    def _history(self, profile, source, sid):
        home, path = self.profiles[profile]
        if not path.resolve(strict=True).is_relative_to(home.resolve(strict=True)):
            raise ResumeRefused('Database outside source profile')
        uri = path.as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            session_columns = [column[1] for column in db.execute('PRAGMA table_info(sessions)')]
            message_columns = [column[1] for column in db.execute('PRAGMA table_info(messages)')]
            if not {'id', 'source', 'profile_name', 'parent_session_id'} <= set(session_columns) or not {
                    'id', 'session_id', 'role', 'content', 'active', 'compacted',
                    'api_content', 'tool_calls', 'reasoning', '_compressed_summary'} <= set(message_columns):
                raise ResumeRefused('Stored history schema unavailable')
            sessions = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchall()
            if len(sessions) != 1:
                raise ResumeRefused('Stored identity missing')
            session = dict(zip(session_columns, sessions[0]))
            if (session['source'] != source or session['profile_name'] not in (None, '', profile)
                    or session['parent_session_id'] or db.execute(
                        'SELECT 1 FROM sessions WHERE parent_session_id=? LIMIT 1', (sid,)).fetchone()):
                raise ResumeRefused('Stored identity redirected or missing')
            history = db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY id', (sid,)).fetchall()
            records = [dict(zip(message_columns, row)) for row in history]
            active = [row for row in records if row['active'] == 1]
            if (not active or len(records) > 50000 or any(
                    type(row['id']) is not int or row['id'] <= 0 or row['active'] not in (0, 1)
                    or row['compacted'] not in (0, 1) for row in records)):
                raise ResumeRefused('Stored active history incomplete')
            # Bind every stored field, including tool calls, API content, reasoning,
            # inactive/compacted rows and summary flags, not just display text.
            def stable(value):
                return {'blob': value.hex()} if isinstance(value, bytes) else value
            digest = hashlib.sha256(json.dumps(
                {'session': session, 'messages': records}, sort_keys=True, default=stable,
                ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
            return len(active), active[-1]['id'], digest

    @staticmethod
    def _panes(inventory):
        if (not isinstance(inventory, dict) or inventory.get('complete') is not True
                or not isinstance(inventory.get('terminals'), list)):
            raise ResumeRefused('Herdr inventory incomplete')
        return inventory['terminals']

    @staticmethod
    def _pane_key(pane):
        if (not isinstance(pane, dict) or not isinstance(pane.get('paneId'), str)
                or not _PANE.fullmatch(pane['paneId']) or not isinstance(pane.get('tabId'), str)
                or not _TAB.fullmatch(pane['tabId']) or not isinstance(pane.get('terminalId'), str)
                or not _ID.fullmatch(pane['terminalId'])
                or not isinstance(pane.get('socketPath'), str)
                or not pane['socketPath'].startswith('/') or '\x00' in pane['socketPath']):
            raise ResumeRefused('Pane identity unavailable')
        return tuple(pane[k] for k in ('socketPath', 'paneId', 'tabId', 'terminalId'))

    def _unique_pane(self, pane):
        key = self._pane_key(pane)
        panes = self._panes(self.herdr.inventory())
        if (sum(isinstance(p, dict) and self._pane_key(p) == key for p in panes) != 1
                or sum(isinstance(p, dict) and p.get('terminalId') == pane['terminalId']
                       for p in panes) != 1):
            raise ResumeRefused('Pane ambiguous')
        return next(p for p in panes if self._pane_key(p) == key)

    def _source_claim(self, pane, profile, sid):
        """Read the normalized claim authored by LocalSource, not fake fields.

        Peer source verifies an agent_session against independent process/lease
        metadata. Its terminal record cannot attest vacancy, source or PID.
        """
        observed = self._unique_pane(pane)
        if (observed.get('agentKind') != 'hermes'
                or observed.get('identitySource') != 'verified'
                or observed.get('profile') != profile or observed.get('sessionId') != sid):
            raise ResumeRefused('Source-authored claim missing')
        return observed

    def _ready_evidence(self, profile, source, sid, pane, pid, raw, expected_history):
        """Check local evidence only; never grant Herdr control or claim vacancy.

        The inventory projection is not an authenticated socket response, so
        even a positive result is strictly non-writable checkout evidence.
        """
        if (type(pid) is not int or pid <= 1 or not isinstance(raw, bytes)
                or not 1 <= len(raw) <= 1024 or raw.count(b'\n') != 1
                or not raw.endswith(b'\n')):
            raise ResumeRefused('Invalid readiness record')
        def unique(pairs):
            value = {}
            for name, item in pairs:
                if name in value:
                    raise ValueError('Duplicate readiness key')
                value[name] = item
            return value
        record = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
        if (not isinstance(record, dict) or set(record) != {
                'protocol', 'sessionId', 'source', 'pid', 'leaseId', 'messageCount'}
                or record['protocol'] != 'hermes-exact-resume-ready-v1'
                or record['sessionId'] != sid or record['source'] != source
                or type(record['pid']) is not int or record['pid'] != pid
                or not isinstance(record['leaseId'], str)
                or not re.fullmatch(r'[a-f0-9]{32}', record['leaseId'])
                or type(record['messageCount']) is not int
                or record['messageCount'] != expected_history[0]):
            raise ResumeRefused('Readiness mismatch')
        born = self.process_birth(pid)
        if type(born) not in (int, float) or born <= 0:
            raise ResumeRefused('Process birth unavailable')
        matches = [(owner, row) for owner, row in self._registry()
                   if row.get('session_id') == sid]
        if len(matches) != 1 or matches[0][0] != profile:
            raise ResumeRefused('Lease ambiguous')
        lease = matches[0][1]
        if (type(lease.get('pid')) is not int or lease['pid'] != pid
                or lease.get('lease_id') != record['leaseId']):
            raise ResumeRefused('Lease mismatch')
        if self._history(profile, source, sid) != expected_history:
            raise ResumeRefused('History changed')
        self._source_claim(pane, profile, sid)
        if self.process_birth(pid) != born:
            raise ResumeRefused('Process replaced')
        if [(owner, row) for owner, row in self._registry()
                if row.get('session_id') == sid] != matches:
            raise ResumeRefused('Lease changed')
        return {'pane': self._pane_key(pane), 'pid': pid, 'birth': born,
                'leaseId': record['leaseId'], 'history': expected_history,
                'writable': False}

    def accept_ready(self, profile, source, sid, pane, pid, raw, expected_history):
        """Reserve verified evidence, not a writable or source-authenticated claim."""
        key = self._identity(profile, source, sid)
        try:
            with self._lock:
                if key in self.bindings or key in self._inflight or key in self.quarantine:
                    raise ResumeRefused('Session already reserved')
                self._inflight.add(key)
            try:
                value = self._ready_evidence(*key, pane, pid, raw, expected_history)
                with self._lock:
                    if (key in self.bindings or any(
                            prior['pane'] == value['pane'] or prior['pid'] == pid
                            for prior in self.bindings.values())):
                        raise ResumeRefused('Pane or process reserved')
                    self.bindings[key] = value
                return dict(value)
            finally:
                with self._lock:
                    self._inflight.discard(key)
        except BaseException:
            raise ResumeRefused('Exact resume evidence refused') from None

    def recheck_binding(self, profile, source, sid):
        key = self._identity(profile, source, sid)
        try:
            with self._lock:
                value = self.bindings[key]
            pane = dict(zip(('socketPath', 'paneId', 'tabId', 'terminalId'), value['pane']))
            raw = (json.dumps({'protocol': 'hermes-exact-resume-ready-v1',
                'sessionId': sid, 'source': source, 'pid': value['pid'],
                'leaseId': value['leaseId'], 'messageCount': value['history'][0]}) + '\n').encode()
            if self._ready_evidence(*key, pane, value['pid'], raw, value['history']) != value:
                raise ResumeRefused('Binding changed')
            return dict(value)
        except BaseException:
            with self._lock:
                self.bindings.pop(key, None)
                self.quarantine.add(key)
            raise ResumeRefused('Exact resume evidence revoked') from None

    def resume(self, profile, source, sid):
        key = self._identity(profile, source, sid)
        with self._lock:
            if key in self._inflight or key in self.bindings or key in self.quarantine:
                raise ResumeRefused('Session reserved')
            self._inflight.add(key)
        try:
            # A gateway/platform source needs an authenticated, coordinated
            # platform handoff. This checkout-only constructor has only a
            # Boolean callback, which cannot supply such a receipt.
            if source != 'cli':
                raise ResumeRefused('Gateway handoff receipt unavailable')
            if self.source_authorized(*key) is not True:
                raise ResumeRefused('Source owner has not coordinated handoff')
            before_rows = self._registry()
            if any(row['session_id'] == sid for _, row in before_rows):
                raise ResumeRefused('Existing lease')
            self._history(*key)
            # This checkout-only API has no source-authenticated empty-pane
            # claim, no spawner wiring an inherited Hermes readiness FD, and
            # no gateway handoff receipt. Refuse before provisioning any pane;
            # even a source-labelled shell is not proven empty.
            raise ResumeRefused('No source-authenticated vacancy/readiness contract')
        except BaseException:
            raise ResumeRefused('Exact resume refused') from None
        finally:
            with self._lock:
                self._inflight.discard(key)
