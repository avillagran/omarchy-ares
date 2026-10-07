"""Output-only, bounded Herdr observer for an independently authenticated peer stream.

This module has no authentication or network transport. It cannot attach, send
input, read history, or assert session ownership. Callers must close the iterator
when their downstream stream disconnects (or set the cancellation event).
"""

import errno
import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time


class ObserveError(Exception):
    """Observation refused, failed, cancelled, or exceeded its bounds."""


_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_PANE = re.compile(r'w[A-Za-z0-9]+:p[A-Za-z0-9]+\Z')
_TAB = re.compile(r'w[A-Za-z0-9]+:t[A-Za-z0-9]+\Z')
_IDENTITY = ('socketPath', 'paneId', 'tabId', 'terminalId', 'agentKind',
             'sessionId', 'profile', 'identitySource')


def _valid_target(target):
    return (isinstance(target, dict)
            and isinstance(target.get('socketPath'), str)
            and target['socketPath'].startswith('/') and '\x00' not in target['socketPath']
            and all(isinstance(target.get(k), str) and bool(p.fullmatch(target[k]))
                    for k, p in (('paneId', _PANE), ('tabId', _TAB),
                                 ('terminalId', _ID), ('agentKind', _ID)))
            and target.get('identitySource') == 'verified'
            and target.get('agentKind') == 'hermes'
            and all(isinstance(target.get(k), str) and bool(_ID.fullmatch(target[k]))
                    for k in ('sessionId', 'profile')))


class ObserveAdapter:
    """Safe local CLI adapter; auth and output delivery belong to the caller."""

    def __init__(self, source, herdr_bin, *, env=None, max_concurrent=2):
        if (not isinstance(herdr_bin, str) or not os.path.isabs(herdr_bin)
                or type(max_concurrent) is not int or not 1 <= max_concurrent <= 8):
            raise ObserveError('Invalid observer configuration')
        self.source = source
        self.herdr_bin = herdr_bin
        self.env = dict(os.environ if env is None else env)
        self._capacity = threading.BoundedSemaphore(max_concurrent)

    def observe(self, target, *, scope, cols, rows, max_bytes=32768, timeout=10, cancel=None,
                snapshot=False):
        """Yield PTY bytes for one exact, freshly inventoried terminal.

        Bounds: 1..4096 columns, 1..2048 rows, 1..65536 bytes, 0<timeout<=30s;
        nonblocking concurrency gate. Cancellation is polled within 0.1s.
        The iterator must be closed if abandoned; never forward bytes into it.
        """
        if (scope != 'output-only' or not _valid_target(target)
                or type(cols) is not int or not 1 <= cols <= 4096
                or type(rows) is not int or not 1 <= rows <= 2048
                or type(max_bytes) is not int or not 1 <= max_bytes <= 65536
                or type(timeout) not in (int, float) or not 0 < timeout <= 30
                or type(snapshot) is not bool
                or (cancel is not None and not isinstance(cancel, threading.Event))):
            raise ObserveError('Invalid observer bounds or identity')
        if hasattr(self.source, 'sockets') and target['socketPath'] not in self.source.sockets:
            raise ObserveError('Socket not allowlisted')
        if not self._capacity.acquire(blocking=False):
            raise ObserveError('Observer capacity exhausted')
        process = None
        master = slave = None
        deadline = time.monotonic() + timeout
        try:
            if cancel is not None and cancel.is_set():
                raise ObserveError('Observation cancelled')
            # Explicit source-of-truth recheck immediately before launching the CLI.
            try:
                inventory = self.source.inventory()
            except Exception as exc:
                raise ObserveError('Source inventory unavailable') from exc
            if (not isinstance(inventory, dict) or inventory.get('complete') is not True
                    or inventory.get('errors', []) != []
                    or not isinstance(inventory.get('terminals'), list)):
                raise ObserveError('Source inventory incomplete')
            matches = [entry for entry in inventory['terminals']
                       if isinstance(entry, dict) and (entry.get('paneId') == target['paneId']
                           or (entry.get('socketPath') == target['socketPath']
                               and entry.get('terminalId') == target['terminalId']))]
            if len(matches) != 1 or any(matches[0].get(k) != target[k] for k in _IDENTITY):
                raise ObserveError('Terminal identity changed')
            owners = [entry for entry in inventory['terminals']
                      if isinstance(entry, dict) and entry.get('agentKind') == 'hermes'
                      and entry.get('sessionId') == target['sessionId']
                      and entry.get('profile') == target['profile']]
            if len(owners) != 1:
                raise ObserveError('Session owner is not unique')
            if time.monotonic() >= deadline:
                raise ObserveError('Observation timed out')
            if cancel is not None and cancel.is_set():
                raise ObserveError('Observation cancelled')
            master, slave = os.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            env = {key: value for key, value in self.env.items() if not key.startswith('HERDR_')}
            env['HERDR_SOCKET_PATH'] = target['socketPath']
            argv = [self.herdr_bin, 'terminal', 'session', 'observe', target['terminalId'],
                    '--cols', str(cols), '--rows', str(rows)]
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=slave,
                                       stderr=subprocess.DEVNULL, close_fds=True,
                                       start_new_session=True, env=env)
            os.close(slave)
            slave = None
            count = 0
            last_output = None
            quiet_snapshot = False
            while True:
                if cancel is not None and cancel.is_set():
                    raise ObserveError('Observation cancelled')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ObserveError('Observation timed out')
                if snapshot and last_output is not None and time.monotonic() - last_output >= 0.15:
                    quiet_snapshot = True
                    break
                ready, _, _ = select.select([master], [], [], min(remaining, 0.05 if snapshot else 0.1))
                if not ready:
                    continue
                try:
                    data = os.read(master, min(8192, max_bytes - count + 1))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b''  # PTY EOF after the child closes its slave.
                if not data:
                    break
                count += len(data)
                last_output = time.monotonic()
                if count > max_bytes:
                    raise ObserveError('Observation byte limit exceeded')
                yield data
            if quiet_snapshot:
                if process.poll() not in (None, 0):
                    raise ObserveError('Observer CLI failed')
            elif process.wait(timeout=min(0.5, max(0, deadline - time.monotonic()))) != 0:
                raise ObserveError('Observer CLI failed')
            # A completed capture is not releasable if its owner disappeared or
            # another terminal claimed the same session during the capture.
            try:
                final_inventory = self.source.inventory()
            except Exception as exc:
                raise ObserveError('Source inventory unavailable') from exc
            if (not isinstance(final_inventory, dict) or final_inventory.get('complete') is not True
                    or final_inventory.get('errors', []) != []
                    or not isinstance(final_inventory.get('terminals'), list)):
                raise ObserveError('Source inventory incomplete')
            final_matches = [entry for entry in final_inventory['terminals']
                             if isinstance(entry, dict) and (entry.get('paneId') == target['paneId']
                                 or (entry.get('socketPath') == target['socketPath']
                                     and entry.get('terminalId') == target['terminalId']))]
            final_owners = [entry for entry in final_inventory['terminals']
                            if isinstance(entry, dict) and entry.get('agentKind') == 'hermes'
                            and entry.get('sessionId') == target['sessionId']
                            and entry.get('profile') == target['profile']]
            if (len(final_matches) != 1 or len(final_owners) != 1
                    or any(final_matches[0].get(k) != target[k] for k in _IDENTITY)
                    or time.monotonic() >= deadline):
                raise ObserveError('Terminal owner changed during observation')
        except OSError as exc:
            raise ObserveError('Observer CLI unavailable') from exc
        except subprocess.TimeoutExpired as exc:
            raise ObserveError('Observer CLI did not exit') from exc
        finally:
            if slave is not None:
                os.close(slave)
            if master is not None:
                os.close(master)
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
            self._capacity.release()
