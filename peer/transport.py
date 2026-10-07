"""Bounded TLS RPC; optional source-local, one-connection exclusive writer."""
import asyncio
import hashlib
import hmac
import json
import ssl

import websockets
from websockets.exceptions import ConnectionClosed

from .core import PeerError

# Requests stay small; responses may carry a busy host's full inventory
# (hundreds of bounded terminal records), so the reply cap is larger.
MAX_MESSAGE = 16384
MAX_RESPONSE = 262144
OPS = frozenset(('inventory', 'sessions', 'read', 'preview', 'history', 'observe'))


def _encode(obj, limit=MAX_MESSAGE):
    data = json.dumps(obj, separators=(',', ':'), ensure_ascii=True).encode()
    if len(data) > limit:
        raise PeerError('message too large')
    return data


def _decode(data, limit=MAX_MESSAGE):
    if not isinstance(data, (str, bytes)) or len(data.encode() if isinstance(data, str) else data) > limit:
        raise PeerError('message too large')
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise PeerError('invalid request')
    return obj


class PeerServer:
    def __init__(self, state, handler=None, host='0.0.0.0', port=0, writer=None,
                 enable_writer=False, writer_peer_id=None, writer_cert=None, writer_pin=None):
        self.state, self.handler, self.host, self.port = state, handler, host, port
        # All opt-ins are explicit; the production entrypoint passes none.
        self.writer = writer
        self.enable_writer = enable_writer is True
        self.writer_peer_id = writer_peer_id
        self.writer_cert = writer_cert
        self.writer_pin = writer_pin
        self.server = None

    async def start(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(str(self.state.identity.cert), str(self.state.identity.key))
        if self.enable_writer:
            if (self.writer is None or not isinstance(self.writer_peer_id, str)
                    or len(self.writer_peer_id) != 32
                    or any(c not in '0123456789abcdef' for c in self.writer_peer_id)
                    or not isinstance(self.writer_pin, str) or len(self.writer_pin) != 64
                    or any(c not in '0123456789abcdef' for c in self.writer_pin)
                    or self.writer_cert is None):
                raise PeerError('writer TLS identity unavailable')
            try:
                from pathlib import Path
                certificate = Path(self.writer_cert).read_text('ascii')
                digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
                if not hmac.compare_digest(digest, self.writer_pin):
                    raise PeerError('writer TLS identity mismatch')
                context.load_verify_locations(cafile=str(self.writer_cert))
                context.verify_mode = ssl.CERT_OPTIONAL
            except (OSError, ValueError, ssl.SSLError) as exc:
                raise PeerError('writer TLS identity unavailable') from exc
        self.server = await websockets.serve(self._handle, self.host, self.port, ssl=context,
            max_size=MAX_MESSAGE, max_queue=2, compression=None, open_timeout=5, ping_interval=20)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def _writer_identity(self, websocket, peer):
        tls = websocket.transport.get_extra_info('ssl_object')
        certificate = tls.getpeercert(binary_form=True) if tls else None
        return (peer == self.writer_peer_id and bool(certificate)
                and hmac.compare_digest(hashlib.sha256(certificate).hexdigest(), self.writer_pin))

    async def _control(self, websocket, request):
        """One socket owns one controller; no frames or source identity leave it."""
        owned = False
        controller = self.writer if self.enable_writer else None
        try:
            if (controller is None or set(request) != {'op', 'peer_id', 'token', 'args'}
                    or request['op'] != 'control.begin'
                    or not self._writer_identity(websocket, request['peer_id'])
                    or not self.state.authorize(request['peer_id'], request['token'])
                    or not isinstance(request['args'], dict)
                    or set(request['args']) != {'profile', 'session_id', 'terminal_id'}):
                raise PeerError('control denied')
            peer, token = request['peer_id'], request['token']
            args = request['args']
            grant = self.state.write_grant
            if (not grant or grant.get('rpc_used') or grant['peer'] != peer
                    or not self.state.write_allowed(peer, controller.source)
                    or self.state.write_grant is not grant):
                raise PeerError('control denied')
            # Consume before the first await. A lost reply/disconnect cannot
            # replay a sequence on a fresh connection without new local consent.
            grant['rpc_used'] = True
            await controller.open(peer, args['profile'], args['session_id'], args['terminal_id'])
            owned = True
            # A re-pair or revocation during acquisition invalidates the old token.
            if not self.state.authorize(peer, token):
                raise PeerError('control denied')
            await websocket.send(_encode({'ok': True, 'result': {'sequence': 0}}))
            sequence = 0
            while True:
                message = _decode(await asyncio.wait_for(websocket.recv(), timeout=10))
                if (set(message) != {'op', 'peer_id', 'token', 'args'}
                        or message['peer_id'] != peer or message['token'] != token
                        or not self.state.authorize(peer, token)
                        or not isinstance(message['args'], dict)):
                    raise PeerError('control denied')
                op, args = message['op'], message['args']
                expected = {'sequence', 'text'} if op == 'control.send' else {'sequence'}
                if (op not in ('control.send', 'control.release') or set(args) != expected
                        or type(args['sequence']) is not int or args['sequence'] != sequence + 1):
                    raise PeerError('control denied')
                if op == 'control.send':
                    await controller.send(args['text'])  # Source rechecks grant/epoch/lease immediately before input.
                else:
                    await controller.close()
                    if controller.process is not None or controller.key is not None:
                        raise PeerError('control release unconfirmed')
                    owned = False
                sequence += 1
                await websocket.send(_encode({'ok': True, 'result': {'sequence': sequence}}))
                if op == 'control.release':
                    return
        except Exception:
            try:
                await websocket.send(_encode({'ok': False, 'error': 'denied'}))
            except (ConnectionClosed, OSError):
                pass
        finally:
            if owned:
                await controller.close()  # Disconnect, invalid message, expiry or revoke.

    async def _handle(self, websocket):
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            request = _decode(raw)
            if isinstance(request.get('op'), str) and request['op'].startswith('control.'):
                await self._control(websocket, request)
                return
            if request.get('op') == 'pair':
                if set(request) != {'op', 'peer_id', 'code'}:
                    raise PeerError('invalid request')
                token = self.state.pair(request['peer_id'], request['code'])
                result = {'peer_id': self.state.identity.peer_id, 'token': token}
            elif request.get('op') == 'revoke':
                if set(request) != {'op', 'peer_id', 'token', 'args'} or request['args'] != {}:
                    raise PeerError('unauthorized')
                self.state.revoke_authorized(request['peer_id'], request['token'])
                result = {'revoked': True}
            else:
                if set(request) != {'op', 'peer_id', 'token', 'args'} or request.get('op') not in OPS:
                    raise PeerError('unauthorized')
                if not self.state.authorize(request['peer_id'], request['token']):
                    raise PeerError('unauthorized')
                if request['op'] == 'observe' and not self.state.observe_allowed(request['peer_id']):
                    raise PeerError('observation not authorized')
                if not isinstance(request['args'], dict) or self.handler is None:
                    raise PeerError('unavailable')
                result = await self.handler(request['op'], request['args'])
                if request['op'] == 'history':
                    if not self.state.authorize(request['peer_id'], request['token']):
                        raise PeerError('history permission changed')
                    recheck = getattr(result, 'recheck_visibility', None)
                    if not callable(recheck):
                        raise PeerError('history visibility unavailable')
                    # Re-read committed source visibility just before the response.
                    # This is not an atomic no-delivery-after-hide guarantee.
                    await asyncio.to_thread(recheck, request['args']['profile'],
                                            request['args']['session_id'])
                    if not self.state.authorize(request['peer_id'], request['token']):
                        raise PeerError('history permission changed')
                if request['op'] == 'observe' and (not self.state.authorize(request['peer_id'], request['token'])
                                                   or not self.state.observe_allowed(request['peer_id'])):
                    raise PeerError('observation permission changed')
            await websocket.send(_encode({'ok': True, 'result': result}, limit=MAX_RESPONSE))
        except (PeerError, ValueError, TypeError, KeyError, json.JSONDecodeError, asyncio.TimeoutError,
                websockets.exceptions.ConnectionClosed, websockets.exceptions.PayloadTooBig):
            try:
                await websocket.send(_encode({'ok': False, 'error': 'denied'}))
            except (ConnectionClosed, PeerError):
                pass
        except Exception:
            # Provider failures are never returned as detail or logged with secrets.
            try:
                await websocket.send(_encode({'ok': False, 'error': 'unavailable'}))
            except (ConnectionClosed, PeerError):
                pass


class PeerClient:
    def __init__(self, state):
        self.state = state

    async def _send(self, url, pin, payload):
        if not isinstance(pin, str) or len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin):
            raise PeerError('invalid fingerprint')
        data = _encode(payload)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Explicit certificate pin below is authoritative.
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        try:
            async with websockets.connect(url, ssl=context, proxy=None, max_size=MAX_RESPONSE, max_queue=2,
                                          compression=None, open_timeout=5, close_timeout=2) as ws:
                cert = ws.transport.get_extra_info('ssl_object').getpeercert(binary_form=True)
                if not cert or not hmac.compare_digest(hashlib.sha256(cert).hexdigest(), pin):
                    raise PeerError('fingerprint mismatch')
                await ws.send(data)
                response = _decode(await asyncio.wait_for(ws.recv(), timeout=10), limit=MAX_RESPONSE)
                if response.get('ok') is not True or not isinstance(response.get('result'), dict):
                    raise PeerError('peer denied request')
                return response['result']
        except (OSError, ssl.SSLError, asyncio.TimeoutError, ConnectionClosed, ValueError,
                websockets.exceptions.WebSocketException) as exc:
            raise PeerError('peer unavailable') from exc

    @classmethod
    async def pair(cls, state, url, pin, code, expected_peer=None):
        if not isinstance(code, str) or len(code) != 8 or not code.isascii() or not code.isdigit():
            raise PeerError('invalid pairing code')
        result = await cls(state)._send(url, pin, {'op': 'pair', 'peer_id': state.identity.peer_id, 'code': code})
        peer_id, token = result.get('peer_id'), result.get('token')
        if not isinstance(peer_id, str) or len(peer_id) != 32 or any(c not in '0123456789abcdef' for c in peer_id) or (expected_peer is not None and peer_id != expected_peer) or not isinstance(token, str) or len(token) < 32:
            raise PeerError('invalid pairing reply')
        state.trusted[peer_id] = {'pin': pin, 'token': token, 'url': url}
        state.save()
        return peer_id

    async def revoke(self, peer_id, url):
        trust = self.state.trusted.get(peer_id)
        if not trust or trust.get('url') != url:
            raise PeerError('untrusted peer')
        result = await self._send(url, trust['pin'], {'op': 'revoke',
            'peer_id': self.state.identity.peer_id, 'token': trust['token'], 'args': {}})
        if result != {'revoked': True}:
            raise PeerError('invalid revocation reply')

    async def request(self, peer_id, url, method, args):
        if method not in OPS or not isinstance(args, dict):
            raise PeerError('operation denied')
        trust = self.state.trusted.get(peer_id)
        if not trust or trust.get('url') != url:
            raise PeerError('untrusted peer')
        result = await self._send(url, trust['pin'], {'op': method, 'peer_id': self.state.identity.peer_id,
            'token': trust['token'], 'args': args})
        return result
