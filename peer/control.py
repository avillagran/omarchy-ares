"""Owner-only line-delimited JSON control socket for Ares adapters."""
import asyncio
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import socket
import stat

from .core import PeerError
from .transport import PeerClient, MAX_MESSAGE, MAX_RESPONSE, _encode


class LocalControl:
    def __init__(self, state, path=None, discovery=None, remote=None, source=None, broker=None):
        self.state = state
        self.path = Path(path or state.root / 'control.sock')
        self.discovery = discovery
        self.remote = remote or PeerClient(state)
        self.source = source
        self.broker = broker  # Deliberately absent from normal serve.
        self.discovered = {}
        self._pending_pairs = set()
        self.server = None

    async def start(self):
        if self.path.parent != self.state.root:
            raise PeerError('control socket must be inside private state directory')
        if self.path.exists() or self.path.is_symlink():
            if not self.path.is_socket() or self.path.is_symlink() or self.path.stat().st_uid != os.getuid():
                raise PeerError('unsafe control socket')
            try:
                probe = socket.socket(socket.AF_UNIX)
                probe.settimeout(.2)
                probe.connect(str(self.path))
            except (ConnectionRefusedError, FileNotFoundError):
                self.path.unlink()
            else:
                probe.close()
                raise PeerError('control socket already active')
            finally:
                probe.close()
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path), limit=MAX_MESSAGE + 1)
        os.chmod(self.path, 0o600)

    async def close(self):
        if self.broker is not None:
            await self.broker.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.path.unlink(missing_ok=True)

    def _resolve(self, alias):
        if not isinstance(alias, str):
            return None
        matches = [peer_id for peer_id, item in self.state.trusted.items()
                   if peer_id == alias or (isinstance(item, dict) and item.get('alias') == alias)]
        if len(matches) > 1:
            raise PeerError('ambiguous peer alias')
        return matches[0] if matches else None

    def _trusted_bindings(self):
        # A duplicate alias cannot name one exact pairing; retain the legacy
        # inventory entry, but withhold its binding rather than choose a peer.
        aliases = Counter(item.get('alias', peer_id) for peer_id, item in self.state.trusted.items()
                          if isinstance(item, dict))
        bindings = {}
        for peer_id, item in self.state.trusted.items():
            if not isinstance(item, dict):
                continue
            alias = item.get('alias', peer_id)
            pin, token = item.get('pin'), item.get('token')
            if (not isinstance(alias, str) or not alias or aliases[alias] != 1
                    or not isinstance(peer_id, str) or len(peer_id) != 32
                    or any(c not in '0123456789abcdef' for c in peer_id)
                    or not isinstance(pin, str) or len(pin) != 64
                    or any(c not in '0123456789abcdef' for c in pin)
                    or not isinstance(token, str) or len(token) != 43):
                continue
            try:
                raw = base64.b64decode(token + '=', altchars=b'-_', validate=True)
                if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') != token:
                    continue
            except (ValueError, UnicodeError):
                continue
            material = (b'ares-peer/trusted-binding/v1\0' + peer_id.encode('ascii') + b'\0' +
                        pin.encode('ascii') + b'\0' + token.encode('ascii'))
            bindings[alias] = hashlib.sha256(material).hexdigest()
        return bindings

    async def _broker_call(self, reader, operation, handle=None):
        """Client EOF during an ambiguous RPC quarantines its remote socket."""
        work = asyncio.create_task(operation)
        disconnected = asyncio.create_task(reader.read(1))
        completed = False
        try:
            done, _ = await asyncio.wait((work, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise PeerError('control denied')
            result = await work
            completed = True
            return result
        finally:
            if not completed:
                # Cancellation of this handler does not cancel a task created by
                # create_task(). Reap it before discovering its possible handle.
                work.cancel()
                try:
                    await work
                except (Exception, asyncio.CancelledError):
                    pass
                if handle is None and work.done() and not work.cancelled():
                    try:
                        handle = work.result().get('handle')
                    except Exception:
                        pass
                if handle is not None:
                    await self.broker.abort(handle)
            disconnected.cancel()
            try:
                await disconnected
            except (Exception, asyncio.CancelledError):
                pass

    async def _handle(self, reader, writer):
        receipt = None
        acknowledged = False
        try:
            sock = writer.get_extra_info('socket')
            # Directory and socket modes are primary guards; peer UID adds defense in depth.
            uid = os.getuid()
            if hasattr(socket, 'SO_PEERCRED'):
                import struct
                _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise PeerError('local owner only')
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line.endswith(b'\n') or len(line) > MAX_MESSAGE:
                raise PeerError('invalid local message')
            req = json.loads(line)
            if not isinstance(req, dict):
                raise PeerError('invalid local message')
            op = req.get('op')
            if op == 'control.begin' and self.broker is not None and set(req) == {
                    'op', 'peer', 'profile', 'session_id', 'terminal_id', 'ttl'}:
                result = await self._broker_call(reader, self.broker.begin(
                    req['peer'], req['profile'], req['session_id'], req['terminal_id'],
                    req['ttl'], uid))
            elif op in ('control.send', 'control.release') and self.broker is not None and set(req) == (
                    {'op', 'handle', 'peer', 'profile', 'session_id', 'terminal_id', 'sequence', 'text'}
                    if op == 'control.send' else
                    {'op', 'handle', 'peer', 'profile', 'session_id', 'terminal_id', 'sequence'}):
                result = await self._broker_call(reader, self.broker.step(
                    op, req['handle'], req['peer'], req['profile'], req['session_id'],
                    req['terminal_id'], req['sequence'], uid, req.get('text')), req['handle'])
            elif op == 'arm' and set(req) == {'op'}:
                result = {'code': self.state.arm(), 'fingerprint': self.state.identity.fingerprint}
            elif op == 'status' and set(req) == {'op'}:
                result = {'peer_id': self.state.identity.peer_id, 'fingerprint': self.state.identity.fingerprint,
                          'trusted': sorted((item.get('alias', peer_id) for peer_id, item in self.state.trusted.items())),
                          'trusted_bindings': self._trusted_bindings(),
                          'authorized': sorted(self.state.tokens)}
            elif op == 'discover' and set(req) == {'op'}:
                announced = await self.discovery.discover() if self.discovery else []
                self.discovered = {item['peer_id']: item for item in announced if isinstance(item, dict)
                                   and isinstance(item.get('peer_id'), str)}
                peers = []
                for peer_id, item in sorted(self.discovered.items()):
                    trust = self.state.trusted.get(peer_id)
                    pin_matches = not trust or trust.get('pin') == item.get('fingerprint')
                    peers.append({'id': peer_id, 'peer_id': peer_id,
                                  'alias': trust.get('alias', peer_id) if trust else '',
                                  'paired': bool(trust), 'online': bool(pin_matches),
                                  'name': item.get('name', ''), 'address': item.get('address', ''),
                                  'port': item.get('port', 0), 'fingerprint': item.get('fingerprint', '')})
                for peer_id, trust in sorted(self.state.trusted.items()):
                    if peer_id not in self.discovered:
                        peers.append({'id': peer_id, 'peer_id': peer_id,
                                      'alias': trust.get('alias', peer_id), 'paired': True,
                                      'online': False, 'name': '', 'address': '',
                                      'port': 0, 'fingerprint': trust.get('pin', '')})
                result = {'peers': peers}
            elif op == 'pair' and (set(req) == {'op', 'peer', 'fingerprint', 'code', 'alias'}
                                   or set(req) == {'op', 'peerId', 'fingerprint', 'code', 'alias'}):
                requested_id = req.get('peerId', req.get('peer'))
                discovered = self.discovered.get(requested_id) if isinstance(requested_id, str) else None
                alias = req['alias']
                if not discovered or not isinstance(alias, str) or not alias or len(alias) > 64 or not all(
                    c.isascii() and (c.isalnum() or c in '-_') for c in alias
                ) or alias in self._pending_pairs or requested_id in self._pending_pairs or any(
                    peer_id == alias or (isinstance(v, dict) and v.get('alias') == alias)
                    for peer_id, v in self.state.trusted.items()):
                    raise PeerError('unknown discovered peer or alias')
                address, port = discovered['address'], discovered['port']
                if not isinstance(address, str) or not isinstance(port, int) or not 1 <= port <= 65535:
                    raise PeerError('invalid discovery record')
                import ipaddress
                ip = ipaddress.ip_address(address)
                url = f'wss://[{address}]:{port}' if ip.version == 6 else f'wss://{address}:{port}'
                self._pending_pairs.update((alias, requested_id))
                try:
                    peer_id = await PeerClient.pair(self.state, url, req['fingerprint'], req['code'], expected_peer=requested_id)
                    self.state.trusted[peer_id]['alias'] = alias
                    self.state.save()
                finally:
                    self._pending_pairs.difference_update((alias, requested_id))
                result = {'peer': alias, 'peer_id': peer_id}
            elif op == 'observe_permission' and set(req) == {'op', 'peer', 'allow'}:
                self.state.set_observe_permission(req['peer'], req['allow'])
                result = {'peer': req['peer'], 'observe_allowed': req['allow']}
            elif op == 'write_grant' and set(req) == {'op', 'peer', 'profile', 'session_id',
                                                       'terminal_id', 'allow', 'ttl'}:
                if self.source is None:
                    raise PeerError('source unavailable')
                self.state.set_write_grant(req['peer'], req['profile'], req['session_id'],
                                           req['terminal_id'], req['allow'], req['ttl'], self.source)
                # Never report a successful consent if identity changed between
                # the grant inventory and the reply.
                allowed = self.state.write_allowed(req['peer'], self.source) if req['allow'] else False
                if req['allow'] and not allowed:
                    raise PeerError('terminal identity changed')
                result = {'peer': req['peer'], 'write_allowed': allowed}
            elif op == 'revoke' and set(req) == {'op', 'peer'}:
                peer_id = self._resolve(req['peer'])
                if peer_id:
                    await self.remote.revoke(peer_id, self.state.trusted[peer_id]['url'])
                elif (not isinstance(req['peer'], str) or len(req['peer']) != 32
                      or any(c not in '0123456789abcdef' for c in req['peer'])
                      or req['peer'] not in self.state.tokens):
                    raise PeerError('unknown peer')
                else:
                    peer_id = req['peer']  # Source-owner emergency revocation by exact peer ID.
                self.state.revoke(peer_id)
                result = {'revoked': req['peer']}
            elif op == 'rpc' and set(req) == {'op', 'peer', 'request'}:
                peer_id = self._resolve(req['peer'])
                request = req['request']
                if not peer_id or not isinstance(request, dict) or request.get('op') not in ('inventory', 'sessions', 'read', 'preview', 'history', 'observe') or set(request) - {'op', 'before', 'target', 'lines', 'max_bytes', 'profile', 'limit', 'offset', 'session_id', 'cols', 'rows'}:
                    raise PeerError('untrusted peer or invalid request')
                args = {key: value for key, value in request.items() if key != 'op'}
                trust = self.state.trusted[peer_id]
                result = await self.remote.request(peer_id, trust['url'], request['op'], args)
            else:
                raise PeerError('unknown control operation')
            if op in ('control.begin', 'control.send'):
                receipt = (result.get('handle', req.get('handle')), result['sequence'])
            writer.write(_encode({'ok': True, 'result': result}, limit=MAX_RESPONSE) + b'\n')
        except (PeerError, ValueError, TypeError, KeyError, asyncio.TimeoutError,
                asyncio.LimitOverrunError, json.JSONDecodeError):
            writer.write(b'{"ok":false,"error":"denied"}\n')
        except Exception:
            writer.write(b'{"ok":false,"error":"unavailable"}\n')
        try:
            await writer.drain()
            if receipt is not None:
                # A completed RPC isn't delivered merely because write() returned.
                # The next local frame must prove receipt on this same connection.
                line = await asyncio.wait_for(reader.readline(), timeout=2)
                if not line.endswith(b'\n') or len(line) > MAX_MESSAGE:
                    raise PeerError('missing receipt')
                ack = json.loads(line)
                if (not isinstance(ack, dict) or set(ack) != {'op', 'handle', 'sequence'}
                        or ack != {'op': 'control.ack', 'handle': receipt[0],
                                   'sequence': receipt[1]}):
                    raise PeerError('invalid receipt')
                writer.write(_encode({'ok': True, 'result': {'ack': True}}) + b'\n')
                await writer.drain()
                acknowledged = True
        except (BrokenPipeError, ConnectionResetError, PeerError, ValueError,
                asyncio.TimeoutError, asyncio.LimitOverrunError):
            pass
        finally:
            if receipt is not None and not acknowledged:
                await self.broker.abort(receipt[0])
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
