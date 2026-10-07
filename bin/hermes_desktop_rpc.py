#!/usr/bin/env python3
"""Official Desktop RPC, without Hermes plugins or gateway control sockets.

exchange({"verb": "rooms", "action": "list", ...params}, timeout=4) returns
{"ok": True, "result": dict} or {"ok": False, "error": str}. Ordinary calls send
one RPC. Native turn operations retain one WS through resume/create, exactly
one prompt.submit, event collection and completion polling; never resend.
Linux discovery is same-user, exact HERMES_HOME, default-profile, loopback-only.
The discovered serve interpreter supplies its own websockets dependency; only
non-secret endpoint metadata is sent via private stdin. Bootstrap credentials
never leave the worker's memory. Neither HTTP nor WebSocket follows redirects.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import quote, urlencode
import uuid

_ACTIONS = frozenset(('capabilities', 'list', 'state', 'log', 'create', 'send', 'rename', 'members', 'disband', 'stop',
                      'peer.register', 'peer.revoke'))
_MAX_BYTES = 2 * 1024 * 1024
_BROKER_MAX_BYTES = 40 * 1024 * 1024
_BROKER_METHODS = frozenset({
    'groups.capabilities', 'groups.list', 'groups.state', 'groups.log', 'groups.send',
    'groups.create', 'groups.rename', 'groups.disband',
    'approval.respond', 'approval.pending', 'clarify.respond', 'sudo.respond', 'secret.respond',
    'profiles.list', 'profiles.configure', 'profiles.create', 'projects.tree', 'projects.project_sessions',
    'session.list', 'session.active_list', 'session.resume', 'session.history', 'session.messages',
    'session.events.since', 'session.interrupt', 'session.close', 'image.attach', 'image.attach_bytes', 'file.attach',
    'prompt.submit', 'session.create',
})


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


_PROC = Path('/proc')


def _is_hermes_serve(argv):
    module_entry = argv[1:3] == ['-m', 'hermes_cli.main'] and 'serve' in argv[3:]
    script_entry = len(argv) >= 3 and Path(argv[1]).name == 'hermes' and 'serve' in argv[2:]
    bootstrap_entry = (argv[1:3] == ['-I', '-c'] and len(argv) >= 5
                       and 'serve' in argv[4:]
                       and 'import hermes_bootstrap' in argv[3]
                       and 'from hermes_cli.main import main' in argv[3]
                       and 'sys.exit(main())' in argv[3])
    return module_entry or script_entry or bootstrap_entry


def _discover(deadline):
    """Join kernel listener inodes to validated same-user serve processes.

    No port scanning, configuration credentials, ss/ps output or persistent
    discovery cache. Named-profile servers are never a fallback for default.
    """
    home = Path(os.environ.get('HERMES_HOME') or '~/.hermes').expanduser().resolve()
    uid = os.getuid()
    listeners = {}
    for table in ('tcp', 'tcp6'):
        try:
            lines = (_PROC / 'net' / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            _remaining(deadline)
            fields = line.split()
            if len(fields) < 10 or fields[3] != '0A' or fields[7] != str(uid):
                continue
            raw, port = fields[1].split(':')
            packed = bytes.fromhex(raw)
            if sys.byteorder == 'little':
                packed = b''.join(packed[i:i + 4][::-1] for i in range(0, len(packed), 4))
            address = ipaddress.ip_address(packed)
            if address.is_loopback:
                listeners[fields[9]] = (str(address), int(port, 16))
    candidates = []
    for process in _PROC.iterdir():
        _remaining(deadline)
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != uid:
                continue
            argv = (process / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
            if not _is_hermes_serve(argv):
                continue
            profiles = []
            for index, arg in enumerate(argv):
                if arg in ('--profile', '-p'):
                    profiles.append(argv[index + 1])
                elif arg.startswith('--profile='):
                    profiles.append(arg.split('=', 1)[1])
                elif arg.startswith('-p') and arg != '-p':
                    profiles.append(arg[2:])
            if any(profile != 'default' for profile in profiles):
                continue
            env = dict(item.split('=', 1) for item in
                       (process / 'environ').read_bytes().decode().split('\0') if '=' in item)
            if env.get('HERMES_PROFILE', 'default') not in ('', 'default'):
                continue
            process_home = Path(env.get('HERMES_HOME') or str(Path(env.get('HOME', str(Path.home()))) / '.hermes'))
            if process_home.expanduser().resolve() != home:
                continue
            interpreter = argv[0]
            if not Path(interpreter).is_absolute() or not os.access(interpreter, os.X_OK):
                continue
            for fd in (process / 'fd').iterdir():
                _remaining(deadline)
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue
                if link.startswith('socket:[') and link[8:-1] in listeners:
                    host, port = listeners[link[8:-1]]
                    candidates.append((int(process.name), host, port, interpreter))
        except (OSError, UnicodeError, ValueError, IndexError):
            continue  # Processes may exit during discovery; never log environ.
    if not candidates:
        raise OSError('Default-profile Desktop server unavailable')
    # A second simultaneous default server is possible. Stable oldest-first
    # selection avoids routing the same mutation to multiple authorities.
    _, host, port, interpreter = min(candidates)
    return host, port, interpreter


def _turn_flow(call, payload, signal):
    """Same resume/create/submit/resume contract as packaged Gye/ibe."""
    profile = payload['profile']
    response = call('session.resume', {'session_id': payload['title'], 'profile': profile})
    if response.get('ok') is True:
        session = response['result']
    elif response.get('code') == 4007:
        response = call('session.create', {'profile': profile, 'title': payload['title'], 'hidden': True,
                                           'room_plumbing': True, 'follow_profile_config': True, 'close_on_disconnect': False})
        if response.get('ok') is not True:
            return response
        session = response['result']
    else:
        return response
    if session.get('running') or session.get('inflight') or session.get('pending_approval') or session.get('pending_clarify'):
        return {'ok': False, 'error': 'Native session already working or waiting; no prompt sent'}
    sid = session.get('session_id')
    stored = session.get('session_key') or session.get('stored_session_id') or sid
    if not sid:
        return {'ok': False, 'error': 'Native session unavailable'}
    signal(sid, 'working')
    before = len(session.get('messages') or [])
    response = call('prompt.submit', {'session_id': sid, 'text': payload['text']})
    if response.get('ok') is not True:
        return response  # Never replay an ambiguous prompt.
    while True:
        time.sleep(1)
        response = call('session.resume', {'session_id': stored, 'profile': profile})
        if response.get('ok') is not True:
            return response
        state = response['result']
        signal(state.get('session_id') or sid, 'waiting' if state.get('pending_approval') or state.get('pending_clarify') else 'working')
        if state.get('running') or state.get('inflight') or state.get('pending_approval') or state.get('pending_clarify'):
            continue
        messages = state.get('messages') or []
        if len(messages) <= before:
            continue
        fallback = ''
        for message in reversed(messages[before:]):
            if message.get('role') != 'assistant':
                continue
            text = message.get('content', message.get('text', ''))
            if isinstance(text, list):
                text = ''.join(part if isinstance(part, str) else str(part.get('text') or '') for part in text)
            text = str(text or '').strip()
            if not text or re.fullmatch(r'\(?\s*pass\s*\)?\.?', text, re.I):
                fallback = text
                continue
            return {'ok': True, 'result': {'text': text, 'session_id': state.get('session_id') or sid}}
        return {'ok': True, 'result': {'text': fallback, 'session_id': state.get('session_id') or sid}}


def _telemetry_text(value, limit=12000):
    text = str(value or '')
    text = re.sub(r'(?i)https?://\S+|\b(?:Bearer|Basic)\s+\S+|\b(?:sk-|gh[pousr]_|github_pat_|AKIA|AIza|xox[baprs]-)[A-Za-z0-9_-]+|\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2}', '[redacted]', text)
    text = re.sub(r'''(?i)\b[\w-]*(?:api[_-]?key|token|secret|password)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''', '[redacted]', text)
    return text[-limit:]


def _stream_turn(ws, payload, deadline):
    """Keep the owning WS connected; persist only matching session events."""
    import tempfile
    path = Path(payload['telemetry_path'])
    ids = set()
    detail = {'memberKey': payload['member_key'], 'thread': payload['thread'], 'sessionId': '',
              'text': '', 'reasoning': '', 'thinking': '', 'tool': '', 'status': 'available',
              'state': 'working', 'activity': '', 'eventTypes': []}
    buffers = {'text': '', 'reasoning': '', 'thinking': ''}
    def publish():
        detail['updatedAt'] = time.time()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.telemetry-')
        try:
            with os.fdopen(fd, 'w') as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(detail, handle)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    def signal(sid, state):
        ids.add(sid)
        if detail['sessionId'] != sid or detail['state'] != state:
            detail.update(sessionId=sid, state=state)
            publish()
    def observe(item):
        if item.get('method') != 'event':
            return
        event = item.get('params') or {}
        if event.get('session_id') not in ids:
            return
        kind = event.get('type')
        data = event.get('payload') or {}
        field = {'message.delta': 'text', 'reasoning.delta': 'reasoning', 'thinking.delta': 'thinking'}.get(kind)
        if field:
            buffers[field] = (buffers[field] + str(data.get('text') or data.get('delta') or ''))[-24000:]
            detail[field] = _telemetry_text(buffers[field])
        elif kind in ('tool.start', 'tool.complete', 'tool.generating', 'tool.progress'):
            detail['tool'] = _telemetry_text(str(data.get('name') or '') + (' completado' if kind == 'tool.complete' else ' en ejecución') + (' · ' + str(data.get('summary')) if data.get('summary') else ''), 1000)
            detail['activity'] = detail['tool']
        elif kind == 'status':
            detail['activity'] = _telemetry_text(data.get('text') or data.get('kind') or '', 240)
        else:
            return
        if kind not in detail['eventTypes']:
            detail['eventTypes'].append(kind)
        publish()
    def call(method, params):
        ident = uuid.uuid4().hex
        ws.send(json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}))
        while True:
            for line in ws.recv(timeout=_remaining(deadline)).splitlines():
                item = json.loads(line)
                if not isinstance(item, dict):
                    continue
                if item.get('id') != ident:
                    observe(item)
                    continue
                if 'error' in item:
                    code = (item.get('error') or {}).get('code')
                    return {'ok': False, 'code': code if type(code) is int else None, 'error': 'Native Desktop turn RPC rejected; not retried'}
                return {'ok': True, 'result': item.get('result') or {}}
    result = _turn_flow(call, payload, signal)
    detail['state'] = 'idle' if result.get('ok') else 'error'
    publish()
    if result.get('ok'):
        result['result']['detail'] = detail
    return result


def _worker(host, port, payload, timeout):
    # All exceptions are converted to fixed messages, never exception repr/URLs.
    # websockets >= 12 follows handshake redirects by default (up to
    # WEBSOCKETS_MAX_REDIRECTS=10); this client must never follow one: the
    # session token rides in the ws URI query and a redirect would leak it.
    os.environ['WEBSOCKETS_MAX_REDIRECTS'] = '1'  # range(N): 1 = one attempt, redirect raises
    from websockets.sync.client import connect
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection(host, port, timeout=_remaining(deadline))
    try:
        connection.request('GET', '/', headers={'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            return {'ok': False, 'error': 'Desktop bootstrap rejected (authentication or redirect)'}
        body = response.read(_MAX_BYTES + 1)
    finally:
        connection.close()
    if len(body) > _MAX_BYTES:
        return {'ok': False, 'error': 'Desktop bootstrap response too large'}
    match = re.search(rb'__HERMES_SESSION_TOKEN__\s*=\s*("(?:\\.|[^"\\])*")', body)
    if not match:
        return {'ok': False, 'error': 'Desktop session bootstrap unavailable'}
    token = json.loads(match.group(1))
    if not isinstance(token, str) or not token:
        return {'ok': False, 'error': 'Desktop session bootstrap unavailable'}
    address = '[' + host + ']' if ':' in host else host
    uri = f'ws://{address}:{port}/api/ws?token={quote(token, safe="")}'
    logger = logging.getLogger('hermes-desktop-rpc-private')
    logger.disabled = True
    # sync.connect does not follow redirects. Explicit proxy=None prevents
    # environment proxies from receiving the loopback session credential.
    ws = connect(uri, proxy=None, open_timeout=_remaining(deadline),
                 close_timeout=0, max_size=_MAX_BYTES, logger=logger)
    try:
        if payload['verb'] == 'native':
            return _stream_turn(ws, payload, deadline)
        ident = uuid.uuid4().hex
        ws.send(json.dumps({'jsonrpc': '2.0', 'id': ident,
                            'method': ('groups' if payload['verb'] == 'rooms' else payload['verb']) + '.' + payload['action'],
                            'params': {k: v for k, v in payload.items() if k not in ('verb', 'action')}}))
        while True:
            message = ws.recv(timeout=_remaining(deadline))
            for line in message.splitlines():
                item = json.loads(line)
                if not isinstance(item, dict) or item.get('id') != ident:
                    continue
                if item.get('jsonrpc') != '2.0' or ('result' in item) == ('error' in item):
                    return {'ok': False, 'error': 'Invalid Desktop RPC envelope'}
                if 'error' in item:
                    error = item['error']
                    code = error.get('code') if isinstance(error, dict) else None
                    message = error.get('message', '') if isinstance(error, dict) else ''
                    # Whitelist diagnostic categories, not arbitrary server text:
                    # errors may echo prompts, credentials, paths or token URLs.
                    missing = code == -32601 or (isinstance(message, str) and 'method not found' in message.lower())
                    label = 'Desktop method not found' if missing else 'Desktop RPC rejected'
                    suffix = f' ({code})' if type(code) is int and abs(code) < 1000000 else ''
                    if missing and payload['action'] == 'members':
                        suffix += '. Esta versión de Hermes Desktop no admite cambiar miembros de una sala.'
                    namespace = 'groups' if payload['verb'] == 'rooms' else payload['verb']
                    result = {'ok': False, 'error': f'{label}: {namespace}.{payload["action"]}{suffix}'}
                    if namespace != 'groups' and type(code) is int and abs(code) < 1000000:
                        result['code'] = code
                    return result
                if not isinstance(item.get('result'), dict):
                    return {'ok': False, 'error': 'Invalid Desktop RPC result'}
                return {'ok': True, 'result': item['result']}
    finally:
        ws.close()


def exchange(payload, timeout=4):
    """One bounded official Desktop call; failures never expose credentials."""
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            return {'ok': False, 'error': 'Invalid Desktop RPC timeout'}
        timeout = min(timeout, 1250.0 if isinstance(payload, dict) and payload.get('verb') == 'native' else 60.0)
        profiles_read = (isinstance(payload, dict) and payload ==
                         {'verb': 'profiles', 'action': 'list', 'include_sessions': False}
                         and payload.get('include_sessions') is False)
        native = isinstance(payload, dict) and (payload.get('verb'), payload.get('action')) in {
            ('profiles', 'configure'), ('session', 'resume'), ('session', 'create'), ('prompt', 'submit'), ('native', 'turn'),
            ('session', 'active_list'), ('session', 'events.since'), ('session', 'history')}
        native_list = (isinstance(payload, dict) and payload.get('verb') == 'session' and payload.get('action') == 'list'
                       and isinstance(payload.get('title'), str) and payload['title'].startswith('Group: ')
                       and bool(payload['title'][7:].strip()) and len(payload['title']) <= 256)
        if not profiles_read and not native and not native_list and (not isinstance(payload, dict) or payload.get('verb') != 'rooms' or payload.get('action') not in _ACTIONS):
            return {'ok': False, 'error': 'Unsupported Desktop room action'}
        if payload.get('verb') == 'native':
            root = Path(os.environ.get('XDG_STATE_HOME') or '~/.local/state').expanduser() / 'omarchy-hermes' / 'native-rooms'
            path = Path(str(payload.get('telemetry_path') or ''))
            if (path.parent.resolve() != root.resolve() or not re.fullmatch(r'[a-f0-9]{64}\.live\.json', path.name)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(payload.get('profile') or ''))
                    or not str(payload.get('title') or '').startswith('Group: ')):
                return {'ok': False, 'error': 'Invalid native turn route or telemetry path'}
        event = payload.get('payload')
        if payload['action'] == 'send' and (payload.get('image_path') or
                (isinstance(event, dict) and event.get('image_path'))):
            # Official Desktop currently advertises attachments=False. Never
            # silently accept text while losing the user's attached image.
            return {'ok': False, 'error': 'Hermes Desktop no admite imágenes en salas; no se envió el mensaje.'}
        deadline = time.monotonic() + timeout
        host, port, interpreter = _discover(deadline)
        if not ipaddress.ip_address(host).is_loopback or not 0 < port < 65536:
            return {'ok': False, 'error': 'Invalid Desktop loopback endpoint'}
        data = json.dumps({'host': host, 'port': port, 'payload': payload, 'timeout': _remaining(deadline)})
        if len(data.encode()) > _MAX_BYTES:
            return {'ok': False, 'error': 'Desktop RPC request too large'}
        completed = subprocess.run([interpreter, '-I', str(Path(__file__).resolve()), '--worker'],
                                   input=data, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=_remaining(deadline), check=False)
        if completed.returncode != 0:
            return {'ok': False, 'error': 'Desktop RPC worker unavailable'}
        return json.loads(completed.stdout)
    except (TimeoutError, subprocess.TimeoutExpired):
        return {'ok': False, 'error': 'Desktop RPC timed out; outcome unknown, not retried'}
    except Exception:
        return {'ok': False, 'error': 'Desktop RPC unavailable; not retried'}


def _broker_worker():
    """Own one authenticated Desktop WebSocket and serve bounded NDJSON reads."""
    # Same redirect policy as _worker: never follow ws handshake redirects.
    os.environ['WEBSOCKETS_MAX_REDIRECTS'] = '1'  # range(N): 1 = one attempt, redirect raises
    from websockets.sync.client import connect
    first = sys.stdin.readline(_MAX_BYTES + 1)
    config = json.loads(first)
    host, port = config.get('host'), config.get('port')
    if (not isinstance(host, str) or not ipaddress.ip_address(host).is_loopback
            or type(port) is not int or not 0 < port < 65536):
        raise ValueError('invalid broker endpoint')
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request('GET', '/', headers={'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            raise OSError('bootstrap rejected')
        body = response.read(_MAX_BYTES + 1)
    finally:
        connection.close()
    if len(body) > _MAX_BYTES:
        raise ValueError('bootstrap too large')
    match = re.search(rb'__HERMES_SESSION_TOKEN__\s*=\s*("(?:\\.|[^"\\])*")', body)
    token = json.loads(match.group(1)) if match else None
    if not isinstance(token, str) or not token:
        raise ValueError('bootstrap unavailable')
    address = '[' + host + ']' if ':' in host else host
    uri = f'ws://{address}:{port}/api/ws?token={quote(token, safe="")}'
    logger = logging.getLogger('hermes-desktop-broker-private')
    logger.disabled = True
    ws = connect(uri, proxy=None, open_timeout=5, close_timeout=0,
                 max_size=_BROKER_MAX_BYTES, logger=logger)
    responses = queue.Queue()
    output_lock = threading.Lock()

    def emit(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=False), flush=True)

    def session_messages(params):
        """Read a persisted transcript without resuming or mutating its Hermes session."""
        session_id = params.get('session_id')
        profile = params.get('profile') or 'default'
        limit = params.get('limit', 500)
        since = params.get('since', 0)
        if (not isinstance(session_id, str) or not session_id or len(session_id) > 256
                or not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', profile)
                or type(limit) is not int or not 1 <= limit <= 500
                or not isinstance(since, (int, float)) or not math.isfinite(float(since)) or float(since) < 0):
            raise ValueError

        def get_json(path):
            connection = http.client.HTTPConnection(host, port, timeout=10)
            try:
                connection.request('GET', path, headers={
                    'Authorization': 'Bearer ' + token,
                    'Accept': 'application/json',
                    'Connection': 'close',
                })
                response = connection.getresponse()
                body = response.read(_BROKER_MAX_BYTES + 1)
                if response.status != 200 or len(body) > _BROKER_MAX_BYTES:
                    raise OSError('persisted session read rejected')
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError
                return value
            finally:
                connection.close()

        encoded = quote(session_id, safe='')
        query = urlencode({'profile': profile, 'limit': limit, 'order': 'latest', 'include_compacted': 'true'})
        detail = get_json(f'/api/sessions/{encoded}?{urlencode({"profile": profile})}')
        if float(detail.get('last_activity_at') or 0) <= float(since):
            messages = []
        else:
            transcript = get_json(f'/api/sessions/{encoded}/messages?{query}')
            messages = transcript.get('messages')
            if not isinstance(messages, list):
                raise ValueError
        metadata_keys = ('id', 'last_activity_at', 'last_activity_description', 'last_activity_provenance', 'ended_at')
        return {'messages': messages, 'session': {key: detail.get(key) for key in metadata_keys}}

    def receive():
        try:
            while True:
                message = ws.recv()
                for raw in message.splitlines():
                    item = json.loads(raw)
                    if not isinstance(item, dict):
                        continue
                    if item.get('method') == 'event' and isinstance(item.get('params'), dict):
                        emit({'event': item['params']})
                    elif isinstance(item.get('id'), str):
                        responses.put(item)
        except Exception:
            responses.put(None)

    reader = threading.Thread(target=receive, daemon=True,
                              name='hermes-desktop-broker-reader')
    reader.start()
    try:
        for line in sys.stdin:
            if len(line.encode()) > _BROKER_MAX_BYTES:
                continue
            request_id = None
            try:
                request = json.loads(line)
                request_id = request.get('id') if isinstance(request, dict) else None
                method = request.get('method') if isinstance(request, dict) else None
                params = request.get('params', {}) if isinstance(request, dict) else None
                if (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id)
                        or method not in _BROKER_METHODS or not isinstance(params, dict)):
                    raise ValueError
                if method == 'session.messages':
                    try:
                        result = session_messages(params)
                    except (OSError, http.client.HTTPException, json.JSONDecodeError):
                        emit({'id': request_id, 'ok': False, 'error': 'Persisted session transcript unavailable'})
                        continue
                    emit({'id': request_id, 'ok': True, 'result': result})
                    continue
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                    'method': method, 'params': params}))
                while True:
                    matched = responses.get(timeout=60)
                    if matched is None:
                        raise OSError('broker connection closed')
                    if matched.get('id') != request_id:
                        continue
                    if 'result' in matched and isinstance(matched['result'], dict):
                        reply = {'id': request_id, 'ok': True, 'result': matched['result']}
                    else:
                        error = matched.get('error') if isinstance(matched.get('error'), dict) else {}
                        code = error.get('code')
                        reply = {'id': request_id, 'ok': False, 'error': 'Desktop RPC rejected'}
                        if type(code) is int and abs(code) < 1000000:
                            reply['code'] = code
                    emit(reply)
                    break
            except (ValueError, TypeError, json.JSONDecodeError):
                emit({'id': request_id, 'ok': False, 'error': 'Invalid broker request'})
            except queue.Empty:
                emit({'id': request_id, 'ok': False,
                      'error': 'Desktop RPC timed out; outcome unknown'})
                return 1
            except Exception:
                emit({'id': request_id, 'ok': False,
                      'error': 'Desktop RPC connection lost; outcome unknown'})
                return 1
    finally:
        ws.close()
    return 0


def _broker():
    """Discover safely, then relay NDJSON to the serve interpreter's worker."""
    try:
        host, port, interpreter = _discover(time.monotonic() + 5)
        child = subprocess.Popen(
            [interpreter, '-I', str(Path(__file__).resolve()), '--broker-worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        child.stdin.write(json.dumps({'host': host, 'port': port}) + '\n')
        child.stdin.flush()
        def relay():
            for reply in child.stdout:
                print(reply, end='', flush=True)
        relay_thread = threading.Thread(target=relay, daemon=True,
                                        name='hermes-desktop-broker-relay')
        relay_thread.start()
        try:
            for line in sys.stdin:
                if len(line.encode()) > 65536:
                    continue
                child.stdin.write(line)
                child.stdin.flush()
        finally:
            child.terminate()
            child.wait(timeout=3)
        return 0
    except Exception:
        return 1


if __name__ == '__main__' and sys.argv[1:] == ['--broker-worker']:
    raise SystemExit(_broker_worker())
elif __name__ == '__main__' and sys.argv[1:] == ['--broker']:
    raise SystemExit(_broker())
elif __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    try:
        args = json.loads(sys.stdin.read(_MAX_BYTES + 1))
        result = _worker(**args)
    except TimeoutError:
        result = {'ok': False, 'error': 'Desktop RPC timed out; outcome unknown, not retried'}
    except Exception:
        result = {'ok': False, 'error': 'Desktop RPC connection failed; not retried'}
    print(json.dumps(result, ensure_ascii=False))
