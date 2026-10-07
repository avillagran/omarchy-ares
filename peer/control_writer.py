"""Source-local, in-memory controller for one already-granted Herdr terminal.

No peer operation or network route calls this module. Herdr's ControlTerminal
(non-takeover) is the sole authority for the terminal writer. A frame is a
positive attach result only with the official control command: Herdr's server
renders it after attaching, while refusal sends terminal.closed/EOF instead.
Never use a plain attach command, inherited Herdr context, or a remote socket.
"""
import asyncio
import base64
import binascii
import json
import os
import subprocess
from pathlib import Path
import re
import time

from .core import _verified_write_target, PeerError

_TERMINAL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_MAX_LINE = 16384
_MAX_FRAME_BYTES = 8192
_ACTIVE = set()  # Per-process reservation; Herdr also fences other processes.


class ControlError(Exception):
    """Controller unavailable; never include terminal output or private details."""


class _PublishedPopen(subprocess.Popen):
    """Publish a real child even if Popen's constructor fails after fork."""

    def __init__(self, *args, publish, **kwargs):
        self._publish = publish
        super().__init__(*args, **kwargs)

    def _execute_child(self, *args, **kwargs):
        try:
            return super()._execute_child(*args, **kwargs)
        finally:
            if self.pid is not None:
                self._publish(self)


class _PipeInput:
    def __init__(self, pipe):
        self.pipe = pipe

    def write(self, data):
        self.pipe.write(data)

    async def drain(self):
        await asyncio.to_thread(self.pipe.flush)

    def is_closing(self):
        return self.pipe.closed


class _PipeOutput:
    def __init__(self, pipe):
        self.pipe = pipe

    async def readline(self):
        # Bound read memory even if an executable ignores the frame limit.
        return await asyncio.to_thread(self.pipe.readline, _MAX_LINE + 1)


class _Child:
    def __init__(self, raw):
        self.raw = raw
        self.pid = raw.pid
        self.stdin = _PipeInput(raw.stdin) if raw.stdin else None
        self.stdout = _PipeOutput(raw.stdout) if raw.stdout else None

    @property
    def returncode(self):
        return self.raw.poll()

    async def wait(self):
        return await asyncio.to_thread(self.raw.wait)

    def kill(self):
        self.raw.kill()

    def close_pipes(self):
        for pipe in (self.raw.stdin, self.raw.stdout):
            if pipe is not None and not pipe.closed:
                try:
                    pipe.close()
                except OSError:
                    pass  # A dead child's pipe can report EPIPE on flush.


class ControlWriter:
    def __init__(self, state, source, *, executable, home, timeout=2):
        self.state = state
        self.source = source
        self.executable = str(executable)
        self.home = str(home)
        self.timeout = timeout
        self.process = None
        self.reader = None
        self.monitor = None
        self.target = None
        self.grant = None
        self.key = None
        self.failed = False
        self._closing = asyncio.Lock()
        self._reaper = None

    def _check(self):
        grant = self.grant
        if (self.failed or not grant or self.state.write_grant is not grant
                or self.target['socketPath'] not in self.source.sockets
                or not self.state.write_allowed(grant['peer'], self.source)
                or self.state.write_grant is not grant):
            raise ControlError('Controller authorization unavailable')
        try:
            current = _verified_write_target(self.source, self.target['profile'],
                                             self.target['sessionId'], self.target['terminalId'])
        except PeerError as exc:
            raise ControlError('Controller authorization unavailable') from exc
        if current != self.target or grant['target'] != self.target:
            raise ControlError('Controller authorization unavailable')

    @staticmethod
    def _frame(line, *, initial=False):
        try:
            # Do not decode or return terminal bytes, even on error.
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate key')
                    result[key] = value
                return result
            value = json.loads(line, object_pairs_hook=unique)
            if not isinstance(value, dict) or value.get('type') != 'terminal.frame':
                raise ValueError('not a frame')
            if (type(value.get('seq')) is not int or value['seq'] < 0
                    or value.get('encoding') != 'ansi'
                    or type(value.get('width')) is not int or not 1 <= value['width'] <= 1000
                    or type(value.get('height')) is not int or not 1 <= value['height'] <= 1000
                    or type(value.get('full')) is not bool
                    or not isinstance(value.get('bytes'), str)):
                raise ValueError('invalid frame')
            if initial and value['full'] is not True:
                raise ValueError('no initial baseline')
            payload = base64.b64decode(value['bytes'], validate=True)
            if len(payload) > _MAX_FRAME_BYTES:
                raise ValueError('oversized frame')
            return value['seq']
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            pass
        # Raise outside the handler: even `from None` retains __context__,
        # whose UnicodeDecodeError.object can contain the entire private line.
        raise ControlError('Invalid controller frame')

    async def _read_line(self, *, startup=True):
        try:
            read = self.process.stdout.readline()
            line = await asyncio.wait_for(read, self.timeout) if startup else await read
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            raise ControlError('Controller stream unavailable') from exc
        if not line or len(line) > _MAX_LINE or not line.endswith(b'\n'):
            raise ControlError('Controller stream unavailable')
        return line

    async def _watch(self, sequence):
        try:
            while True:
                line = await self._read_line(startup=False)
                self._check()
                next_sequence = self._frame(line)
                if next_sequence <= sequence:
                    raise ControlError('Controller frame out of order')
                sequence = next_sequence
        except (ControlError, asyncio.CancelledError):
            self.failed = True
            await self.close()

    async def _monitor(self):
        try:
            while True:
                await asyncio.sleep(min(.05, max(0, self.grant['expires'] - time.monotonic())))
                self._check()
        except (ControlError, asyncio.CancelledError):
            self.failed = True
            await self.close()

    async def open(self, peer, profile, session_id, terminal_id):
        if self.key is not None:
            raise ControlError('Controller occupied')
        grant = self.state.write_grant
        if (not isinstance(peer, str) or not isinstance(profile, str)
                or not isinstance(session_id, str) or not isinstance(terminal_id, str)
                or not _TERMINAL.fullmatch(terminal_id)
                or not grant or grant['peer'] != peer):
            raise ControlError('Controller authorization unavailable')
        target = grant['target']
        if ((target['profile'], target['sessionId'], target['terminalId'])
                != (profile, session_id, terminal_id)
                or target['socketPath'] not in self.source.sockets):
            raise ControlError('Controller authorization unavailable')
        socket = target['socketPath']
        if not isinstance(socket, str) or not socket.startswith('/') or '\x00' in socket:
            raise ControlError('Controller socket unavailable')
        key = (socket, terminal_id)
        if key in _ACTIVE:
            raise ControlError('Controller occupied')
        _ACTIVE.add(key)
        self.key, self.grant, self.target = key, grant, dict(target)
        self.failed = False
        try:
            self._check()
            # Absolute executable and minimal environment; no parent HERDR_*,
            # proxy, XDG, authentication, or SSH variables survive.
            if not Path(self.executable).is_absolute() or not Path(self.home).is_absolute():
                raise ControlError('Controller executable unavailable')
            args = (self.executable, 'terminal', 'session', 'control', terminal_id)
            kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL,
                          env={'HERDR_SOCKET_PATH': socket, 'HOME': self.home,
                               'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'})
            # Publish the OS child synchronously before the first await.
            # Even a constructor exception after fork leaves self.process owned.
            _PublishedPopen(args, publish=lambda raw: setattr(self, 'process', _Child(raw)),
                            **kwargs)
            first = self._frame(await self._read_line(), initial=True)
            if self.process.returncode is not None:
                raise ControlError('Controller closed')
            self._check()
            self.reader = asyncio.create_task(self._watch(first))
            self.monitor = asyncio.create_task(self._monitor())
            return self
        except BaseException as exc:
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ControlError('Controller startup refused') from exc

    async def send(self, text):
        if (not isinstance(text, str) or not text or
                any(ord(c) < 32 or ord(c) == 127 for c in text)):
            raise ControlError('Invalid controller text')
        try:
            data = text.encode('utf-8', errors='strict')
        except UnicodeError:
            data = None
        if data is None:
            # Outside the handler: UnicodeEncodeError.object contains private text.
            raise ControlError('Invalid controller text')
        if len(data) > 2048:
            raise ControlError('Controller text too large')
        try:
            async with self._closing:
                if not self.process or self.process.returncode is not None:
                    raise ControlError('Controller closed')
                self._check()  # Fresh inventory, trust, lease, server epoch and pane generation.
                line = json.dumps({'type': 'terminal.input', 'text': text},
                                  ensure_ascii=True, separators=(',', ':')).encode() + b'\n'
                self.process.stdin.write(line)
                await asyncio.wait_for(self.process.stdin.drain(), self.timeout)
                if self.failed or self.process.returncode is not None:
                    raise ControlError('Controller closed')
        except (ControlError, OSError, asyncio.TimeoutError) as exc:
            await self.close()
            raise ControlError('Controller input refused') from exc
        except asyncio.CancelledError:
            await self.close()
            raise

    async def close(self):
        cleanup = asyncio.create_task(self._close_locked())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Finish release/reap before propagating cancellation; the
            # reservation is never freed while a writer process still runs.
            await asyncio.shield(cleanup)
            raise

    async def _close_locked(self):
        async with self._closing:
            await self._close_owned()

    async def _close_owned(self):
        process = self.process
        self.failed = True
        if self.reader:
            if self.reader is not asyncio.current_task():
                self.reader.cancel()
            self.reader = None
        if self.monitor:
            if self.monitor is not asyncio.current_task():
                self.monitor.cancel()
            self.monitor = None
        if process:
            try:
                if process.returncode is None and process.stdin and not process.stdin.is_closing():
                    process.stdin.write(b'{"type":"terminal.release"}\n')
                    await asyncio.wait_for(process.stdin.drain(), self.timeout)
                await asyncio.wait_for(process.wait(), self.timeout)
            except Exception:
                if process.returncode is None:
                    try:
                        process.kill()
                        await asyncio.wait_for(process.wait(), self.timeout)
                    except Exception:
                        pass  # Quarantine; never release an unconfirmed writer.
            finally:
                if process.returncode is not None:
                    if isinstance(process, _Child):
                        process.close_pipes()
                    self.process = None
        if self.process is None:
            if self.key is not None:
                _ACTIVE.discard(self.key)
            self.key = self.grant = self.target = None
        elif self.process is not None and (self._reaper is None or self._reaper.done()):
            self._reaper = asyncio.create_task(self._retry_cleanup())

    async def _retry_cleanup(self):
        while self.process is not None:
            await asyncio.sleep(max(.05, self.timeout))
            async with self._closing:
                await self._close_owned()
