"""Stable local entrypoint: python -m peer <command>."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from .core import PeerError, PeerState
from .transport import PeerServer, MAX_MESSAGE, MAX_RESPONSE
from .control import LocalControl
from .discovery import Discovery
from .herdr_source import LocalSource, _trusted_ancestors
from .session_binding import SessionBinding
from .provider import provider_handler
from .observe import ObserveAdapter


def default_root():
    return Path(os.environ.get('ARES_PEER_STATE', Path.home() / '.local/share/ares-peer')).expanduser()


def trusted_herdr_executable():
    """Optional source-owner private pin; missing/invalid means no write proof."""
    import stat
    config = default_root() / 'herdr-executable'
    try:
        parent, file = config.parent.lstat(), config.lstat()
        if (not _trusted_ancestors(config) or parent.st_uid != os.getuid() or parent.st_mode & 0o077
                or not stat.S_ISDIR(parent.st_mode) or file.st_uid != os.getuid()
                or stat.S_IMODE(file.st_mode) != 0o600 or not stat.S_ISREG(file.st_mode)
                or file.st_size > 4096):
            return None
        value = config.read_text(encoding='utf-8').strip()
        path = Path(value)
        return path if value and path.is_absolute() and '\n' not in value else None
    except (OSError, UnicodeError):
        return None


def local_source():
    home = Path.home() / '.hermes'
    database = home / 'state.db'
    proof = SessionBinding({'default': (home, database)})
    return LocalSource(databases={'default': database},
                       sockets=[str(Path.home() / '.config/herdr/herdr.sock')],
                       optional_sockets=[str(Path.home() / '.config/herdr/sessions/ares/herdr.sock')],
                       verified_metadata=proof,
                       trusted_herdr_executable=trusted_herdr_executable())


def observer_herdr_executable():
    """First executable herdr binary; the private pin wins when installed."""
    candidates = []
    pin = trusted_herdr_executable()
    if pin is not None:
        candidates.append(pin)
    candidates += [Path.home() / '.local/bin/herdr', Path.home() / '.cargo/bin/herdr',
                   Path('/usr/local/bin/herdr'), Path('/usr/bin/herdr')]
    for path in candidates:
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return None


async def serve(args):
    state = PeerState(default_root())
    source = local_source()
    herdr_bin = observer_herdr_executable()
    server = PeerServer(state, handler=provider_handler(source,
        observer=ObserveAdapter(source, herdr_bin) if herdr_bin else None), host=args.host, port=args.port)
    await server.start()
    discovery = Discovery(state, args.host, server.port)
    control = LocalControl(state, discovery=discovery, source=source)
    try:
        await control.start()
        await discovery.start()
        print(json.dumps({'peer_id': state.identity.peer_id, 'fingerprint': state.identity.fingerprint,
                          'port': server.port, 'socket': str(control.path)}), flush=True)
        await asyncio.Future()
    finally:
        await discovery.close()
        await control.close()
        await server.close()


async def command(args):
    path = default_root() / 'control.sock'
    if args.action == 'pair':
        if not sys.stdin.isatty():
            raise PeerError('pairing code must be entered on local interactive stdin')
        import getpass
        code = getpass.getpass('Source-local armed code: ')
        payload = {'op': 'pair', 'peer': args.peer, 'fingerprint': args.fingerprint, 'code': code,
                   'alias': args.alias}
    elif args.action == 'observe-permission':
        payload = {'op': 'observe_permission', 'peer': args.peer, 'allow': args.decision == 'allow'}
    elif args.action == 'write-grant':
        payload = {'op': 'write_grant', 'peer': args.peer, 'profile': args.profile,
                   'session_id': args.session_id, 'terminal_id': args.terminal_id,
                   'allow': args.decision == 'allow', 'ttl': args.ttl}
    elif args.action == 'revoke':
        payload = {'op': 'revoke', 'peer': args.peer}
    elif args.action in ('inventory', 'sessions', 'read', 'observe'):
        request = {'op': args.action}
        if args.action == 'sessions':
            request.update(profile=args.profile, limit=args.limit, offset=args.offset)
        if args.action == 'read':
            if sys.stdin.isatty():
                raise PeerError('read target must be supplied as JSON on stdin')
            raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE:
                raise PeerError('read target too large')
            target = json.loads(raw)
            if not isinstance(target, dict) or set(target) != {'target'} or not isinstance(target['target'], dict):
                raise PeerError('invalid read target')
            request.update(target=target['target'], lines=args.lines, max_bytes=args.max_bytes)
        if args.action == 'observe':
            request.update(profile=args.profile, session_id=args.session_id, cols=args.cols, rows=args.rows)
        payload = {'op': 'rpc', 'peer': args.peer, 'request': request}
    else:
        payload = {'op': args.action}
    message = json.dumps(payload, separators=(',', ':')).encode() + b'\n'
    if len(message) > MAX_MESSAGE:
        raise PeerError('message too large')
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(message)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=15)
        if len(raw) > MAX_RESPONSE or not raw.endswith(b'\n'):
            raise PeerError('invalid local response')
        response = json.loads(raw)
        if response.get('ok') is not True:
            raise PeerError(response.get('error', 'denied'))
        print(json.dumps(response['result'], separators=(',', ':')))
    finally:
        writer.close()
        await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser(description='Ares Peer local control')
    sub = parser.add_subparsers(dest='action', required=True)
    service = sub.add_parser('serve')
    service.add_argument('--bind', '--host', dest='host', default='0.0.0.0')
    service.add_argument('--port', type=int, default=9431)
    for name in ('arm', 'discover', 'status'):
        sub.add_parser(name)
    pair = sub.add_parser('pair')
    pair.add_argument('peer', help='peer ID returned by local discover')
    pair.add_argument('fingerprint', help='independently verified source-local SHA-256 certificate pin')
    pair.add_argument('alias', help='local alias for the paired peer')
    permission = sub.add_parser('observe-permission')
    permission.add_argument('peer', help='exact 32-hex paired peer ID')
    permission.add_argument('decision', choices=('allow', 'deny'))
    grant = sub.add_parser('write-grant', help='source-owner-only ephemeral consent; no network input')
    grant.add_argument('peer', help='exact 32-lowercase-hex paired peer ID')
    grant.add_argument('profile')
    grant.add_argument('session_id')
    grant.add_argument('terminal_id')
    grant.add_argument('decision', choices=('allow', 'deny'))
    grant.add_argument('--ttl', type=int, default=60, help='seconds (1..300)')
    revoke = sub.add_parser('revoke')
    revoke.add_argument('peer')
    for name in ('inventory', 'sessions', 'read', 'observe'):
        p = sub.add_parser(name)
        p.add_argument('peer')
        if name == 'sessions':
            p.add_argument('--profile', default='default')
            p.add_argument('--limit', type=int, default=50)
            p.add_argument('--offset', type=int, default=0)
        elif name == 'observe':
            p.add_argument('--profile', default='default')
            p.add_argument('--session-id', required=True)
            p.add_argument('--cols', type=int, default=80)
            p.add_argument('--rows', type=int, default=24)
        elif name == 'read':
            p.add_argument('--lines', type=int, default=80)
            p.add_argument('--max-bytes', type=int, default=8192)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args) if args.action == 'serve' else command(args))
    except (PeerError, OSError, ValueError) as exc:
        print('Ares Peer: ' + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
