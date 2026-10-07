"""Read-only local Ares Peer source. No transport or terminal control.

A caller may supply verified_metadata(pane) from an *independent* live process
identity verifier. Its result must name the exact socket/pane/tab/terminal,
session and profile; Herdr's own live agent_session must also agree. The
provider never promotes a title, path, lease alone, or absent agent_session.
"""

import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import struct
from contextlib import closing
from pathlib import Path


class SourceError(Exception):
    """Source unavailable, incomplete, or identity changed; never expose private data."""


_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_PANE = re.compile(r'w[A-Za-z0-9]+:p[A-Za-z0-9]+\Z')
_TAB = re.compile(r'w[A-Za-z0-9]+:t[A-Za-z0-9]+\Z')
_ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[P^_][^\x1b]*\x1b\\|\x1b[@-_]')
_MAX_RESPONSE = 1024 * 1024


def _trusted_ancestors(path):
    """Check every pathname component without resolving away symlinks."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        return False
    current = Path('/')
    try:
        for component in path.parent.parts[1:]:
            current /= component
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or info.st_mode & 0o022):
                return False
        return True
    except OSError:
        return False


class _Snapshot(dict):
    """Local-only origin accompanies a reply, never a JSON field."""
    def __init__(self, value, origin):
        super().__init__(value)
        self.origin = origin


class _HistoryPage(dict):
    """In-process visibility proof; never serialized as part of the page."""
    def __init__(self, value, source, profile, session_id):
        super().__init__(value)
        self._source = source
        self._profile = profile
        self._session_id = session_id

    def recheck_visibility(self, profile, session_id):
        if profile != self._profile or session_id != self._session_id:
            raise SourceError('Session unavailable')
        self._source.check_history_visibility(profile, session_id)


def _start_ticks(pid):
    # /proc stat comm may contain closing parentheses.
    value = (Path('/proc') / str(pid) / 'stat').read_bytes()[:8192].decode('ascii')
    if not value.startswith(str(pid) + ' ('):
        raise ValueError('invalid process')
    fields = value.rsplit(') ', 1)[1].split()
    if len(fields) < 20 or fields[0] == 'Z':
        raise ValueError('dead process')
    ticks = int(fields[19])
    if ticks <= 0:
        raise ValueError('invalid process start')
    return ticks
_PRIVATE_PREVIEW = re.compile(
    r'(?i)(?:authorization\s*:\s*bearer|\b(?:api[_-]?key|access[_-]?token|secret|password|pairing\s+code)\s*[:=]'
    r'|\b(?:pairing|verification)\s+code\s*:\s*\d{6,8}\b'
    r'|^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\s+(?:INFO|DEBUG|WARN|ERROR)\b)',
    re.MULTILINE)


def _valid(value, pattern=_ID):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _recorded_dir(value):
    if not isinstance(value, str) or not value.startswith('/') or '\x00' in value:
        return ''
    path = Path(value)
    return value if path.is_dir() else ''


class LocalSource:
    def __init__(self, databases, sockets, verified_metadata=None, timeout=2,
                 optional_sockets=(), trusted_herdr_executable=None):
        """Explicit profile->DB mapping and allowlisted socket paths; no scanning."""
        self.databases = {key: Path(value) for key, value in databases.items()}
        self.required_sockets = tuple(str(Path(path)) for path in sockets)
        self.optional_sockets = tuple(str(Path(path)) for path in optional_sockets)
        self.sockets = tuple(dict.fromkeys((*self.required_sockets, *self.optional_sockets)))
        self.verified_metadata = verified_metadata or (lambda pane: None)
        self.timeout = timeout
        self.trusted_herdr_executable = None
        self.trusted_executable_inode = None
        # No executable is inferred from the socket, peer response, or PATH.
        if trusted_herdr_executable is not None:
            path = Path(trusted_herdr_executable)
            try:
                info = path.lstat()
                if (not _trusted_ancestors(path) or not stat.S_ISREG(info.st_mode)
                        or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022):
                    raise ValueError('untrusted executable')
                self.trusted_herdr_executable = path
                self.trusted_executable_inode = (info.st_dev, info.st_ino)
            except (OSError, ValueError):
                pass  # Observation remains available; writing stays disabled.

    def _socket_origin(self, path, connection):
        try:
            if self.trusted_herdr_executable is None or not _trusted_ancestors(path):
                return None
            parent = Path(path).parent.lstat()
            file = Path(path).lstat()
            if (not Path(path).is_absolute() or parent.st_uid != os.getuid()
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077
                    or file.st_uid != os.getuid() or not stat.S_ISSOCK(file.st_mode)
                    or file.st_mode & 0o077):
                return None
            pid, uid, gid = struct.unpack('3i', connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
            if pid <= 0 or uid != os.getuid():
                return None
            expected = self.trusted_herdr_executable.lstat()
            actual = (Path('/proc') / str(pid) / 'exe').stat()
            inode = self.trusted_executable_inode
            if ((expected.st_dev, expected.st_ino) != inode
                    or (actual.st_dev, actual.st_ino) != inode):
                return None
            started = _start_ticks(pid)
            again = Path(path).lstat()
            if (again.st_dev, again.st_ino) != (file.st_dev, file.st_ino):
                return None
            # SO_PEERCRED names credentials captured at connect/listen time,
            # not the process handling a forked accepted socket. fstat() of
            # the connected client likewise identifies the client socket,
            # NOT the filesystem listener inode. Linux exposes no verified
            # listener-to-connected-socket binding here. An owner can rename
            # sockets transiently and a child can reply while the pinned
            # parent remains alive. Until an authenticated source-side
            # attestation exists, origin cannot authorize terminal input.
            return None
        except (OSError, ValueError, UnicodeError, struct.error):
            return None

    def sessions(self, profile='default', limit=50, offset=0):
        if not _valid(profile) or profile not in self.databases:
            raise SourceError('Unknown profile')
        if type(limit) is not int or type(offset) is not int or limit < 1 or offset < 0 or offset > 10000:
            raise SourceError('Invalid catalog bounds')
        limit = min(limit, 100)
        path = self.databases[profile]
        try:
            uri = path.resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=self.timeout)) as db:
                db.execute('PRAGMA query_only=ON')
                rows = db.execute('SELECT id, source, title, message_count, started_at, '
                                  'last_activity_at, cwd, git_repo_root, archived FROM sessions '
                                  'WHERE COALESCE(hidden, 0) != 1 ORDER BY COALESCE(last_activity_at, started_at) DESC, id '
                                  'LIMIT ? OFFSET ?', (limit + 1, offset)).fetchall()
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise SourceError('Session catalog unavailable') from exc
        data = []
        for sid, source, title, count, started, active, cwd, root, archived in rows[:limit]:
            if not _valid(sid):
                continue
            data.append({'id': sid, 'profile': profile, 'source': str(source or '')[:64],
                         'title': str(title or '')[:160], 'message_count': int(count or 0),
                         'started_at': started, 'last_activity_at': active,
                         'cwd': _recorded_dir(cwd), 'git_repo_root': _recorded_dir(root),
                          'archived': archived == 1})
        return {'data': data, 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}

    def preview(self, profile, session_id, limit=8):
        """Return only bounded visible chat text for an exact local session."""
        if (not _valid(profile) or profile not in self.databases
                or not _valid(session_id) or type(limit) is not int or not 1 <= limit <= 8):
            raise SourceError('Invalid preview identity or bounds')
        try:
            uri = self.databases[profile].resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=self.timeout)) as db:
                db.execute('PRAGMA query_only=ON')
                session_columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
                if not {'id', 'hidden'} <= session_columns:
                    raise SourceError('Session visibility unavailable')
                match = db.execute('SELECT profile_name FROM sessions '
                                   'WHERE id=? AND COALESCE(hidden, 0)=0'
                                   if 'profile_name' in session_columns else
                                   'SELECT id FROM sessions WHERE id=? AND COALESCE(hidden, 0)=0',
                                   (session_id,)).fetchone()
                if match is None or ('profile_name' in session_columns
                                     and match[0] not in (None, '', profile)):
                    raise SourceError('Session unavailable')
                columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
                if not columns:  # Older Hermes versions have no message records.
                    return {'messages': []}
                required = {'id', 'session_id', 'role', 'content'}
                if not required <= columns:
                    raise SourceError('Message visibility unavailable')
                guards = ['session_id=?', "role IN ('user','assistant')", "typeof(content)='text'",
                          "length(content)>0"]
                for column in ('tool_calls', 'tool_call_id', 'tool_name', 'reasoning',
                               'reasoning_content', 'reasoning_details', 'codex_reasoning_items'):
                    if column in columns:
                        guards.append(f"({column} IS NULL OR {column}='')")
                for column in ('active', 'compacted', '_compressed_summary'):
                    if column in columns:
                        guards.append(f'COALESCE({column}, 0)={1 if column == "active" else 0}')
                if 'display_kind' in columns:
                    guards.append("(display_kind IS NULL OR display_kind IN ('', 'message', 'text'))")
                rows = db.execute('SELECT role, substr(content,1,500) FROM messages WHERE '
                                  + ' AND '.join(guards) + ' ORDER BY id DESC LIMIT ?',
                                  (session_id, limit)).fetchall()
        except SourceError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise SourceError('Chat preview unavailable') from exc
        # JSON ASCII escaping can expand each character; eight 500-character
        # messages must still fit one 16 KiB authenticated transport frame.
        messages = []
        for role, text in reversed(rows):
            if _PRIVATE_PREVIEW.search(text):
                messages.append({'role': role, 'text': '[redacted]'})
                continue
            escaped_size = 0
            chars = []
            for char in text:
                size = len(json.dumps(char, ensure_ascii=True)) - 2
                if escaped_size + size > 1700:
                    break
                escaped_size += size
                chars.append(char)
            messages.append({'role': role, 'text': ''.join(chars)})
        return {'messages': messages}

    def check_history_visibility(self, profile, session_id):
        """Fresh SQLite connection: latest committed visibility, not page snapshot."""
        if not _valid(profile) or profile not in self.databases or not _valid(session_id):
            raise SourceError('Session unavailable')
        try:
            uri = self.databases[profile].resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=self.timeout)) as db:
                db.execute('PRAGMA query_only=ON')
                columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
                if not {'id', 'hidden'} <= columns:
                    raise SourceError('Session visibility unavailable')
                match = db.execute('SELECT profile_name FROM sessions WHERE id=? AND COALESCE(hidden,0)=0'
                                   if 'profile_name' in columns else
                                   'SELECT id FROM sessions WHERE id=? AND COALESCE(hidden,0)=0',
                                   (session_id,)).fetchone()
                if match is None or ('profile_name' in columns and match[0] not in (None, '', profile)):
                    raise SourceError('Session unavailable')
        except SourceError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise SourceError('Session visibility unavailable') from exc

    def history(self, profile, session_id, before=None, limit=8):
        """Indexed, cursor-bounded display history; never hydrate a transcript."""
        if (not _valid(profile) or profile not in self.databases or not _valid(session_id)
                or type(limit) is not int or not 1 <= limit <= 8
                or (before is not None and (type(before) is not int or not 1 <= before <= 9223372036854775807))):
            raise SourceError('Invalid history identity or bounds')
        try:
            uri = self.databases[profile].resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=self.timeout)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                session_columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
                if not {'id', 'hidden'} <= session_columns:
                    raise SourceError('Session visibility unavailable')
                match = db.execute('SELECT profile_name FROM sessions WHERE id=? AND COALESCE(hidden,0)=0'
                                   if 'profile_name' in session_columns else
                                   'SELECT id FROM sessions WHERE id=? AND COALESCE(hidden,0)=0',
                                   (session_id,)).fetchone()
                if match is None or ('profile_name' in session_columns
                                     and match[0] not in (None, '', profile)):
                    raise SourceError('Session unavailable')
                columns = {row[1] for row in db.execute('PRAGMA table_info(messages)')}
                required = {'id', 'session_id', 'role', 'content', 'display_order',
                            'display_identity', 'active', 'compacted', 'display_kind'}
                indexes = {row[1] for row in db.execute('PRAGMA index_list(messages)')}
                if not required <= columns or 'idx_messages_display_page' not in indexes:
                    raise SourceError('Indexed message history unavailable')
                # A legacy/unbackfilled session cannot be projected correctly read-only.
                missing = db.execute('SELECT 1 FROM messages INDEXED BY idx_messages_display_page '
                                     'WHERE session_id=? AND (active=1 OR compacted=1) '
                                     'AND (display_order IS NULL OR display_identity IS NULL) LIMIT 1',
                                     (session_id,)).fetchone()
                if missing:
                    raise SourceError('Display order unavailable')
                def visible(alias):
                    guards = [f"{alias}.role IN ('user','assistant')",
                              f"typeof({alias}.content)='text'",
                              f'length({alias}.content)>0',
                              f'({alias}.active=1 OR {alias}.compacted=1)']
                    for col in ('tool_calls', 'tool_call_id', 'tool_name', 'reasoning',
                                'reasoning_content', 'reasoning_details', 'codex_reasoning_items'):
                        if col in columns:
                            guards.append(f"({alias}.{col} IS NULL OR {alias}.{col}='')")
                    if '_compressed_summary' in columns:
                        guards.append(f'COALESCE({alias}._compressed_summary,0)=0')
                    guards.append(f"({alias}.display_kind IS NULL OR {alias}.display_kind IN ('', 'message', 'text'))")
                    if 'display_metadata' in columns:
                        guards.append(f"({alias}.display_metadata IS NULL OR {alias}.display_metadata='' OR "
                                      f"(json_valid({alias}.display_metadata) AND "
                                      f"COALESCE(json_extract(CASE WHEN json_valid({alias}.display_metadata) "
                                      f"THEN {alias}.display_metadata ELSE '{{}}' END, '$.model_only'),0)=0))")
                    return ' AND '.join(guards)
                # The partial display index handles the exact session, cursor and
                # reverse ordering. Select a preferred active copy per display key.
                sql = ('SELECT m.display_order, (SELECT c.id FROM messages c '
                       'INDEXED BY idx_messages_display_page WHERE c.session_id=m.session_id '
                       'AND c.display_order=m.display_order AND ' + visible('c') +
                       ' ORDER BY c.active DESC,c.id DESC LIMIT 1) FROM messages m '
                       'INDEXED BY idx_messages_display_page WHERE m.session_id=? '
                       'AND (m.active=1 OR m.compacted=1) AND m.display_order<? AND '
                       + visible('m') + ' GROUP BY m.display_order '
                       'ORDER BY m.display_order DESC LIMIT ?')
                keys = db.execute(sql, (session_id, before or 9223372036854775807, limit + 1)).fetchall()
                chosen = keys[:limit]
                records = [db.execute('SELECT role, substr(content,1,500) FROM messages '
                                      'WHERE id=? AND session_id=?', (row_id, session_id)).fetchone()
                           for _, row_id in chosen]
        except SourceError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise SourceError('Chat history unavailable') from exc
        messages = []
        for (cursor, _), (role, text) in reversed(list(zip(chosen, records))):
            if _PRIVATE_PREVIEW.search(text):
                text = '[redacted]'
            else:
                escaped = 0
                chars = []
                for char in text:
                    size = len(json.dumps(char, ensure_ascii=True)) - 2
                    if escaped + size > 1700:
                        break
                    escaped += size
                    chars.append(char)
                text = ''.join(chars)
            messages.append({'role': role, 'text': text, 'cursor': cursor})
        page = _HistoryPage({'messages': messages,
                             'next_before': messages[0]['cursor'] if len(keys) > limit else None,
                             'has_more': len(keys) > limit}, self, profile, session_id)
        # This read is on a NEW connection after the page snapshot/projection.
        # It guarantees visibility only at this recheck, not atomic non-delivery
        # if the session is hidden after the recheck.
        page.recheck_visibility(profile, session_id)
        return page

    def _call(self, path, method, params, *, with_origin=False):
        if path not in self.sockets:
            raise SourceError('Unknown Herdr socket')
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(self.timeout)
                connection.connect(path)
                origin = self._socket_origin(path, connection) if with_origin else None
                connection.sendall(json.dumps({'id': 'ares-peer-read', 'method': method,
                                               'params': params}, separators=(',', ':')).encode() + b'\n')
                chunks = bytearray()
                while len(chunks) <= _MAX_RESPONSE:
                    part = connection.recv(min(8192, _MAX_RESPONSE + 1 - len(chunks)))
                    if not part:
                        break
                    chunks.extend(part)
                    if b'\n' in part:
                        break
                if with_origin and origin != self._socket_origin(path, connection):
                    origin = None
            if len(chunks) > _MAX_RESPONSE or not chunks.endswith(b'\n') or chunks.count(b'\n') != 1:
                raise SourceError('Herdr response incomplete or oversized')
            response = json.loads(chunks)
            if (not isinstance(response, dict) or response.get('id') != 'ares-peer-read'
                    or 'error' in response or not isinstance(response.get('result'), dict)):
                raise SourceError('Herdr response rejected')
            return (response['result'], origin) if with_origin else response['result']
        except (OSError, ValueError, UnicodeError) as exc:
            raise SourceError('Herdr unavailable') from exc

    def _snapshot(self, path):
        value, origin = self._call(path, 'session.snapshot', {}, with_origin=True)
        state = value.get('snapshot')
        if (not isinstance(state, dict) or any(not isinstance(state.get(k), list)
                                                or len(state[k]) > 512
                                                for k in ('panes', 'tabs', 'agents', 'workspaces'))):
            raise SourceError('Herdr snapshot incomplete')
        return _Snapshot(state, origin)

    def _items(self, path, state, ambiguous_claims=frozenset()):
        tabs = state['tabs']
        items = []
        for pane in state['panes']:
            if not isinstance(pane, dict):
                continue
            pid, tid, terminal = (pane.get(k) for k in ('pane_id', 'tab_id', 'terminal_id'))
            if not (_valid(pid, _PANE) and _valid(tid, _TAB) and _valid(terminal)):
                continue
            if sum(t.get('tab_id') == tid for t in tabs if isinstance(t, dict)) != 1:
                continue
            if sum(p.get('pane_id') == pid for p in state['panes'] if isinstance(p, dict)) != 1:
                continue
            agents = [a for a in state['agents'] if isinstance(a, dict) and a.get('pane_id') == pid]
            if len(agents) > 1:
                continue
            agent = agents[0] if agents else {}
            if agent and (agent.get('tab_id') != tid or agent.get('terminal_id') != terminal):
                continue
            kind = agent.get('agent', 'shell')
            if not _valid(kind):
                continue
            item = {'paneId': pid, 'tabId': tid, 'terminalId': terminal,
                    'socketPath': path, 'agentKind': kind,
                    'status': str(agent.get('agent_status') or 'unknown')[:40],
                    'workspace': '', 'label': '', 'sessionId': '',
                    'profile': '', 'identitySource': ''}
            for tab in tabs:
                if isinstance(tab, dict) and tab.get('tab_id') == tid:
                    item['label'] = str(tab.get('label') or '')[:160]
            for workspace in state['workspaces']:
                if (isinstance(workspace, dict)
                        and workspace.get('workspace_id') == pane.get('workspace_id')):
                    item['workspace'] = str(workspace.get('label') or '')[:160]
            claim = agent.get('agent_session') if kind == 'hermes' else None
            # A second pane asserting the same session makes Herdr ownership
            # ambiguous even if only one pane has independent lease proof.
            # Never promote that one pane by ignoring the conflicting claim.
            claim_unique = isinstance(claim, dict) and _valid(claim.get('value')) and sum(
                isinstance(other, dict)
                and isinstance(other.get('agent_session'), dict)
                and other['agent_session'].get('value') == claim.get('value')
                for other in state['agents']) == 1
            proof = (self.verified_metadata(dict(item)) if claim_unique
                     and claim.get('value') not in ambiguous_claims else None)
            if isinstance(claim, dict) and isinstance(proof, dict):
                sid, profile = proof.get('sessionId'), proof.get('profile')
                exact = all(proof.get(key) == item[key]
                            for key in ('paneId', 'tabId', 'terminalId', 'socketPath'))
                if (_valid(sid) and _valid(profile) and profile in self.databases and exact
                        and claim.get('kind') == 'id' and claim.get('agent') == 'hermes'
                        and claim.get('source') == 'herdr:hermes' and claim.get('value') == sid
                        and (claim.get('profile') in (None, profile))):
                    item.update(sessionId=sid, profile=profile, identitySource='verified')
                    # Process/lease proof is independent of Herdr's claim.
                    # Neither persistent terminal_id nor inferred process/pane
                    # state can attest a restarted server or PTY runtime. The
                    # two required values come ONLY from this socket snapshot.
                    generator = getattr(self.verified_metadata, 'generation_for', None)
                    generated = generator(dict(item)) if callable(generator) else None
                    if (isinstance(state, _Snapshot) and state.origin is not None
                            and type(state.get('protected_control_fence_version')) is int
                            and state['protected_control_fence_version'] == 1
                            and isinstance(generated, dict) and
                            all(generated.get(key) == item[key] for key in
                                ('paneId', 'tabId', 'terminalId', 'socketPath', 'sessionId', 'profile'))
                            and _valid(generated.get('terminalGeneration'))
                            and _valid(state.get('server_instance_id'))
                            and _valid(pane.get('runtime_generation'))):
                        basis = (generated['terminalGeneration'], state['server_instance_id'],
                                 pane['runtime_generation'], state.origin)
                        item['terminalGeneration'] = hashlib.sha256(
                            json.dumps(basis, separators=(',', ':')).encode()).hexdigest()
            items.append(item)
        return items

    def inventory(self):
        snapshots = []
        for path in self.sockets[:16]:
            if path in self.optional_sockets and not Path(path).is_socket():
                continue
            snapshots.append((path, self._snapshot(path)))
        if len(self.sockets) > 16:
            raise SourceError('Herdr socket limit exceeded')
        claim_counts = {}
        for _, state in snapshots:
            for agent in state['agents']:
                claim = agent.get('agent_session') if isinstance(agent, dict) else None
                if isinstance(claim, dict) and _valid(claim.get('value')):
                    sid = claim['value']
                    claim_counts[sid] = claim_counts.get(sid, 0) + 1
        ambiguous = {sid for sid, count in claim_counts.items() if count > 1}
        items = []
        for path, state in snapshots:
            current = self._items(path, state, ambiguous)
            # A snapshot captured just before a server/runtime replacement
            # must never authorize a writer. Observe remains available even
            # when the second source-local attestation fails or changes.
            if any('terminalGeneration' in item for item in current):
                try:
                    fresh = self._items(path, self._snapshot(path))
                    fresh_generations = {
                        (item['paneId'], item['tabId'], item['terminalId']): item.get('terminalGeneration')
                        for item in fresh}
                except SourceError:
                    fresh_generations = {}
                for item in current:
                    key = (item['paneId'], item['tabId'], item['terminalId'])
                    if item.get('terminalGeneration') != fresh_generations.get(key):
                        item.pop('terminalGeneration', None)
            items.extend(current)
        return {'terminals': items, 'complete': True}

    def read(self, target, lines=80, max_bytes=16384):
        if (type(lines) is not int or not 1 <= lines <= 200
                or type(max_bytes) is not int or not 1 <= max_bytes <= 32768
                or not isinstance(target, dict)):
            raise SourceError('Invalid read bounds or identity')
        path = target.get('socketPath')
        expected = ('socketPath', 'paneId', 'tabId', 'terminalId', 'agentKind')
        if path not in self.sockets or not _valid(target.get('paneId'), _PANE) or not _valid(target.get('tabId'), _TAB) or not _valid(target.get('terminalId')):
            raise SourceError('Invalid terminal identity')

        def identity():
            matches = [item for item in self._items(path, self._snapshot(path))
                       if item['paneId'] == target['paneId']]
            if len(matches) != 1 or any(matches[0][key] != target.get(key) for key in expected):
                raise SourceError('Terminal identity changed')
            for key in ('sessionId', 'profile'):
                if target.get(key) and target[key] != matches[0][key]:
                    raise SourceError('Session binding changed')
            return matches[0]

        identity()
        response = self._call(path, 'pane.read', {'pane_id': target['paneId'],
                    'source': 'recent', 'lines': lines, 'format': 'text', 'strip_ansi': True})
        identity()
        text = response.get('text')
        if not isinstance(text, str):
            raise SourceError('Pane read response incomplete')
        text = _ANSI.sub('', text)
        text = ''.join(ch for ch in text if ch == '\n' or ch == '\t' or ord(ch) >= 32)
        recent = text.splitlines(keepends=True)
        limited = ''.join(recent[-lines:])
        raw = limited.encode('utf-8')
        clipped = raw[:max_bytes].decode('utf-8', errors='ignore')
        return {'text': clipped, 'truncated': len(raw) > max_bytes or len(recent) > lines,
                'paneId': target['paneId'], 'terminalId': target['terminalId']}
