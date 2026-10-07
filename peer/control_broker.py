"""Default-off coordinator control broker; private in-memory handles only."""
import asyncio
import hashlib
import hmac
import secrets
import ssl
import time

import websockets

from .core import PeerError
from .transport import MAX_MESSAGE, _decode, _encode


class ControlBroker:
    """One pinned mTLS socket per exact source binding, never a retry queue."""
    def __init__(self, state):
        self.state = state
        self.active = {}
        self.bindings = set()
        self.quarantine = {}  # Ambiguous bindings retain their transport and fence.
        self.released = {}  # Confirmed release receipts, never writer authority.
        self.closed = False
        self.inflight = set()  # Operations that may still enter ws.send.

    def _trust(self, alias):
        if not isinstance(alias, str) or not alias:
            raise PeerError('control denied')
        matches = [(peer, item) for peer, item in self.state.trusted.items()
                   if peer == alias or item.get('alias') == alias]
        if len(matches) != 1:
            raise PeerError('control denied')
        peer, item = matches[0]
        snapshot = (peer, item.get('alias'), item.get('url'), item.get('pin'), item.get('token'))
        if (not isinstance(snapshot[2], str) or not snapshot[2].startswith('wss://')
                or not isinstance(snapshot[3], str) or len(snapshot[3]) != 64
                or any(c not in '0123456789abcdef' for c in snapshot[3])
                or not isinstance(snapshot[4], str) or len(snapshot[4]) < 16):
            raise PeerError('control denied')
        return snapshot

    def _verify(self, handle, session, alias, profile, session_id, terminal_id, uid):
        if (self.closed or self.active.get(handle) is not session
                or session['uid'] != uid or session['alias'] != alias
                or session['target'] != (profile, session_id, terminal_id)
                or self._trust(alias) != session['trust']
                or time.monotonic() >= session['deadline']):
            raise PeerError('control denied')

    async def _abort(self, handle, confirmed=False):
        session = self.active.pop(handle, None)
        if session is None:
            return
        # An uncertain release is fenced for this process lifetime. A new
        # grant must not reuse a binding whose old writer may still be alive.
        if confirmed:
            self.bindings.discard(session['binding'])
            self.released[handle] = (session['uid'], session['alias'],
                session['target'], session['trust'], session['sequence'],
                min(session['deadline'], time.monotonic() + 300))
            # Bounded by active ownership plus a fixed receipt cap.
            for key, receipt in list(self.released.items()):
                if receipt[-1] <= time.monotonic():
                    del self.released[key]
            while len(self.released) > 256:
                del self.released[next(iter(self.released))]
        task = session.get('watch')
        if task and task is not asyncio.current_task():
            task.cancel()
        ws = session['ws']
        if not confirmed:
            self.quarantine[session['binding']] = ws
        # Close the remote controller even when release/ACK was ambiguous.
        try:
            await asyncio.wait_for(ws.close(), timeout=3)
        except (Exception, asyncio.CancelledError):
            pass

    async def abort(self, handle):
        await self._abort(handle)

    async def close(self):
        self.closed = True  # Stop new admission; this is not a completed wire fence.
        sessions = list(self.active.values())
        self.active.clear()
        for session in sessions:
            self.quarantine[session['binding']] = session['ws']
            task = session.get('watch')
            if task and task is not asyncio.current_task():
                task.cancel()
        pending = self.inflight.copy()
        pending.discard(asyncio.current_task())
        for task in pending:
            task.cancel()
        async def close_socket(ws):
            try:
                await asyncio.wait_for(ws.close(), timeout=3)
            except (Exception, asyncio.CancelledError):
                pass  # Quarantine still holds the transport and binding.
        await asyncio.gather(*(close_socket(session['ws']) for session in sessions))
        # A cancelled send can still ignore cancellation and enter its transport.
        # A shutdown is not complete until every such coroutine has terminated.
        _, pending = await asyncio.wait(pending, timeout=3) if pending else (set(), set())
        if pending:
            raise PeerError('control shutdown incomplete')

    async def _watch(self, handle, ws, deadline):
        closed = asyncio.create_task(ws.wait_closed())
        try:
            while time.monotonic() < deadline:
                if closed.done():
                    break
                session = self.active.get(handle)
                if session is None:
                    break
                try:
                    if self._trust(session['alias']) != session['trust']:
                        break
                except PeerError:
                    break
                await asyncio.wait((closed,), timeout=min(.2, max(0, deadline - time.monotonic())))
        finally:
            closed.cancel()
            await self._abort(handle)

    async def begin(self, alias, profile, session_id, terminal_id, ttl, uid):
        if self.closed or type(ttl) is not int or not 1 <= ttl <= 300 or any(
                not isinstance(v, str) or not v or len(v) > 128 or not all(
                    c.isascii() and (c.isalnum() or c in '_-') for c in v)
                for v in (profile, session_id, terminal_id)):
            raise PeerError('control denied')
        trust = self._trust(alias)
        binding = (trust[0], profile, session_id, terminal_id, uid)
        if binding in self.bindings:
            raise PeerError('control denied')
        self.bindings.add(binding)  # Reserve before any await.
        self.inflight.add(asyncio.current_task())
        ws = None
        begin_maybe_sent = False
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.load_cert_chain(str(self.state.identity.cert), str(self.state.identity.key))
            ws = await websockets.connect(trust[2], ssl=context, proxy=None,
                max_size=MAX_MESSAGE, max_queue=2, compression=None,
                open_timeout=5, close_timeout=2)
            cert = ws.transport.get_extra_info('ssl_object').getpeercert(binary_form=True)
            if (self.closed or not cert or not hmac.compare_digest(hashlib.sha256(cert).hexdigest(), trust[3])
                    or self._trust(alias) != trust):
                raise PeerError('control denied')
            begin_maybe_sent = True  # send may raise after a partial remote write.
            await ws.send(_encode({'op': 'control.begin', 'peer_id': self.state.identity.peer_id,
                'token': trust[4], 'args': {'profile': profile, 'session_id': session_id,
                                           'terminal_id': terminal_id}}))
            reply = _decode(await asyncio.wait_for(ws.recv(), timeout=10))
            if (self.closed or reply != {'ok': True, 'result': {'sequence': 0}}
                    or self._trust(alias) != trust):
                raise PeerError('control denied')
            handle = secrets.token_hex(32)
            deadline = time.monotonic() + ttl
            session = {'ws': ws, 'trust': trust, 'binding': binding, 'alias': alias,
                       'target': (profile, session_id, terminal_id), 'uid': uid,
                       'sequence': 0, 'deadline': deadline}
            self.active[handle] = session
            session['watch'] = asyncio.create_task(self._watch(handle, ws, deadline))
            return {'handle': handle, 'sequence': 0}
        except BaseException:
            if ws is not None:
                if begin_maybe_sent:
                    self.quarantine[binding] = ws
                try:
                    await asyncio.wait_for(ws.close(), timeout=3)
                except (Exception, asyncio.CancelledError):
                    pass
            if not begin_maybe_sent:
                self.bindings.discard(binding)
            raise
        finally:
            self.inflight.discard(asyncio.current_task())

    async def step(self, op, handle, alias, profile, session_id, terminal_id, sequence, uid, text=None):
        if self.closed:
            raise PeerError('control denied')
        session = self.active.get(handle) if isinstance(handle, str) else None
        if session is None:
            receipt = self.released.get(handle) if isinstance(handle, str) else None
            if receipt is not None:
                owner, saved_alias, target, trust, saved_sequence, deadline = receipt
                if deadline <= time.monotonic():
                    del self.released[handle]
                elif (op == 'control.release' and text is None and uid == owner
                      and alias == saved_alias and target == (profile, session_id, terminal_id)
                      and sequence == saved_sequence and self._trust(alias) == trust):
                    return {'sequence': sequence}
            raise PeerError('control denied')
        # A concurrent call must not wait and then reuse an already-spent sequence.
        if session.get('busy'):
            raise PeerError('control denied')
        session['busy'] = True
        try:
            self._verify(handle, session, alias, profile, session_id, terminal_id, uid)
            if type(sequence) is not int or sequence != session['sequence'] + 1:
                raise PeerError('control denied')
            if op == 'control.send':
                if (not isinstance(text, str) or not text or any(ord(c) < 32 or ord(c) == 127 for c in text)
                        or len(text.encode('utf-8')) > 2048):
                    raise PeerError('control denied')
                args = {'sequence': sequence, 'text': text}
            elif op == 'control.release' and text is None:
                args = {'sequence': sequence}
            else:
                raise PeerError('control denied')
            trust = session['trust']
            self.inflight.add(asyncio.current_task())
            await session['ws'].send(_encode({'op': op, 'peer_id': self.state.identity.peer_id,
                                              'token': trust[4], 'args': args}))
            self._verify(handle, session, alias, profile, session_id, terminal_id, uid)
            reply = _decode(await asyncio.wait_for(session['ws'].recv(), timeout=10))
            self._verify(handle, session, alias, profile, session_id, terminal_id, uid)
            if reply != {'ok': True, 'result': {'sequence': sequence}}:
                raise PeerError('control denied')
            session['sequence'] = sequence
            if op == 'control.release':
                await self._abort(handle, confirmed=True)
            return {'sequence': sequence}
        except BaseException:
            await self._abort(handle)
            raise
        finally:
            self.inflight.discard(asyncio.current_task())
            session['busy'] = False
