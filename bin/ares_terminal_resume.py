#!/usr/bin/env python3
"""Foreground route lease supervisor. Never runs detached; never sends prompts."""
import ctypes
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys


def supervise(home, route, argv, recheck, ready=None):
    """Keep the native route lease in a distinct PID until the child is reaped."""
    if sys.platform != 'linux':
        raise RuntimeError('Linux parent-death fencing is required')
    lease = None
    if route is not None:
        from hermes_cli.active_sessions import try_acquire_active_session
        lease, error = try_acquire_active_session(
            session_id=route, surface='ares:routed-resume', config={},
            registry_home=home, track_liveness=True)
        if error or lease is None or not lease.enabled:
            raise RuntimeError('Native route ownership is unavailable')
    child = None
    reported = False
    opener = None
    old = {}
    pending = []
    def forward(sig, frame):
        pending.append(sig)
        if child is not None and child.poll() is None:
            child.send_signal(sig)
    parent = os.getpid()
    libc = ctypes.CDLL(None, use_errno=True)
    def fence():
        # Arm before exec and then check parent: closes death-before-prctl race.
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            os._exit(126)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, forward)
        recheck(lease)
        if pending:
            raise RuntimeError('Launch interrupted before child start')
        if ready is None:
            child = subprocess.Popen(argv, preexec_fn=fence)
        else:
            reader, writer = os.pipe()
            try:
                child = subprocess.Popen([*argv, '--exact-resume-ready-fd', str(writer)],
                                         pass_fds=(writer,), preexec_fn=fence)
            finally:
                os.close(writer)
            try:
                if not select.select([reader], [], [], 60)[0]:
                    raise RuntimeError('Exact history acknowledgment timed out')
                raw = os.read(reader, 4097)
            finally:
                os.close(reader)
            if len(raw) > 4096:
                raise RuntimeError('Exact history acknowledgment was oversized')
            try:
                ack = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise RuntimeError('Exact history acknowledgment was invalid') from exc
            expected = {'protocol': 'hermes-exact-resume-ready-v1',
                        'sessionId': ready['sessionId'], 'source': ready['source'],
                        'pid': child.pid}
            if (not isinstance(ack, dict) or any(type(ack.get(k)) is not type(v) or ack[k] != v
                                                 for k, v in expected.items())
                    or not isinstance(ack.get('leaseId'), str) or not ack['leaseId']
                    or type(ack.get('messageCount')) is not int or ack['messageCount'] <= 0
                    or child.poll() is not None):
                raise RuntimeError('Exact history acknowledgment did not prove a live owner')
            import ares_terminal_open as opener
            opener.save_job(Path(ready['path']), {**ack, 'nonce': ready['nonce'],
                'paneId': ready['paneId'], 'tabId': ready['tabId']})
            opener.herdr(['pane', 'report-agent', ready['paneId'], '--source', 'ares:exact-resume',
                          '--agent', 'hermes', '--state', 'idle',
                          '--agent-session-id', ready['sessionId']])
            reported = True
            opener.herdr(['pane', 'report-agent-session', ready['paneId'],
                          '--source', 'herdr:hermes', '--agent', 'hermes',
                          '--agent-session-id', ready['sessionId']])
            observed = opener.snapshot()
            matches = [agent for agent in observed.get('agents', [])
                       if agent.get('agent') == 'hermes' and agent.get('pane_id') == ready['paneId']
                       and agent.get('tab_id') == ready['tabId']
                       and agent.get('terminal_id') == ready['terminalId']
                       and agent.get('agent_status') == 'idle'
                       and agent.get('agent_session') == {'kind':'id','agent':'hermes',
                           'source':'herdr:hermes','value':ready['sessionId']}]
            if len(matches) != 1 or child.poll() is not None:
                raise RuntimeError('Herdr did not confirm the exact ready agent pane')
        for sig in pending:
            if child.poll() is None:
                child.send_signal(sig)
        return child.wait()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        if reported and opener is not None and ready is not None:
            try:
                opener.herdr(['pane', 'release-agent', ready['paneId'], '--source', 'ares:exact-resume',
                              '--agent', 'hermes'])
            except Exception:
                print('Ares: unable to release Herdr agent marker', file=sys.stderr)
        if lease is not None:
            lease.release()
        for sig, handler in old.items():
            signal.signal(sig, handler)


def main(path):
    import ares_terminal_open as opener
    payload = json.loads(Path(path).read_text())
    request, row = payload['request'], payload['row']
    opener.validate_request(request)
    home = Path(payload['home'])
    if opener.profile_home(request) != home or os.environ.get('HERMES_HOME') != str(home):
        raise RuntimeError('Profile home changed before resume')
    expected = payload['pane']
    if any(os.environ.get(key) != expected[field] for key, field in (
            ('HERDR_PANE_ID', 'pane_id'), ('HERDR_TAB_ID', 'tab_id'))):
        raise RuntimeError('Not the newly created Ares pane')
    def recheck(lease):
        if opener.stored_session(request) != row:
            raise RuntimeError('Stored identity changed before resume')
        opener.preflight(request, opener.probe.read_inventory(), row,
                         ignored_lease=lease.lease_id if lease is not None else None)
    route = row.get('session_key')
    route = route if route and route != request['sessionId'] else None
    ready_path = Path(payload['ready'])
    if ready_path != Path(path).parent / 'ready.json' or ready_path.exists():
        raise RuntimeError('Unexpected exact-history acknowledgment target')
    nonce = payload['nonce']
    if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        raise RuntimeError('Invalid exact-history launch nonce')
    ready = {'path': str(ready_path), 'nonce': nonce, 'paneId': expected['pane_id'],
             'tabId': expected['tab_id'], 'terminalId': expected['terminal_id'],
             'sessionId': request['sessionId'], 'source': row['source']}
    return supervise(home, route,
        [payload['hermes'], '--profile', request['profile'], 'chat', '--cli', '--resume', request['sessionId'],
         '--exact-resume', '--source', row['source']], recheck, ready=ready)


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1]))
    except Exception:
        print('Ares: guarded resume failed; no automatic retry.', file=sys.stderr)
        sys.exit(1)
