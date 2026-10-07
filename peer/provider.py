"""Authentication-gated read-only bridge to the local Herdr/Hermes source."""
import asyncio
import base64
import re

from .observe import ObserveError

from .core import PeerError
from .herdr_source import SourceError
from .transport import _encode


def _bounded_catalog(value):
    """Fit a source page in one wire frame without skipping its remaining rows."""
    rows = value['data']
    low, high = 0, len(rows)
    while low < high:
        count = (low + high + 1) // 2
        page = {**value, 'data': rows[:count],
                'has_more': bool(value.get('has_more')) or count < len(rows)}
        try:
            _encode({'ok': True, 'result': page})
        except PeerError:
            high = count - 1
        else:
            low = count
    if rows and low == 0:
        raise PeerError('session entry exceeds wire limit')
    page = {**value, 'data': rows[:low],
            'has_more': bool(value.get('has_more')) or low < len(rows)}
    _encode({'ok': True, 'result': page})
    return page


def provider_handler(source, observer=None):
    """Create a remote handler; the transport authenticates before calling it."""
    async def handle(method, args):
        if not isinstance(args, dict):
            raise PeerError('invalid provider arguments')
        try:
            if method == 'observe':
                if observer is None:
                    raise PeerError('observer unavailable on source')
                if (set(args) != {'profile', 'session_id', 'cols', 'rows'}
                        or any(not isinstance(args[k], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', args[k])
                               for k in ('profile', 'session_id'))
                        or type(args['cols']) is not int or not 1 <= args['cols'] <= 4096
                        or type(args['rows']) is not int or not 1 <= args['rows'] <= 2048):
                    raise PeerError('invalid observation request')

                def snapshot():
                    inventory = source.inventory()
                    if (not isinstance(inventory, dict) or inventory.get('complete') is not True
                            or inventory.get('errors', []) != [] or not isinstance(inventory.get('terminals'), list)):
                        raise PeerError('source inventory unavailable')
                    matches = [t for t in inventory['terminals'] if isinstance(t, dict)
                               and t.get('profile') == args['profile']
                               and t.get('sessionId') == args['session_id']]
                    if (len(matches) != 1 or matches[0].get('agentKind') != 'hermes'
                            or matches[0].get('identitySource') != 'verified'):
                        raise PeerError('unique verified owner unavailable')
                    target = matches[0]
                    stream = observer.observe(target, scope='output-only', cols=args['cols'],
                                              rows=args['rows'], max_bytes=8192, timeout=2, snapshot=True)
                    try:
                        frame = b''.join(stream)
                    finally:
                        stream.close()
                    if len(frame) > 8192:
                        raise PeerError('observation too large')
                    result = {'profile': args['profile'], 'session_id': args['session_id'],
                              'terminal_id': target['terminalId'], 'encoding': 'base64',
                              'frame': base64.b64encode(frame).decode('ascii'),
                              'cols': args['cols'], 'rows': args['rows']}
                    _encode({'ok': True, 'result': result})
                    return result
                return await asyncio.wait_for(asyncio.to_thread(snapshot), timeout=3)
            if method == 'inventory' and not args:
                value = await asyncio.wait_for(asyncio.to_thread(source.inventory), timeout=5)
                if not isinstance(value, dict) or value.get('complete') is not True or not isinstance(value.get('terminals'), list):
                    raise PeerError('incomplete inventory')
                return {'ok': True, 'terminals': value['terminals'], 'errors': [], 'complete': True}
            if method == 'sessions' and set(args) <= {'profile', 'limit', 'offset'}:
                value = await asyncio.wait_for(asyncio.to_thread(source.sessions,
                    profile=args.get('profile', 'default'), limit=args.get('limit', 50),
                    offset=args.get('offset', 0)), timeout=5)
                if not isinstance(value, dict) or not isinstance(value.get('data'), list):
                    raise PeerError('invalid session catalog')
                return _bounded_catalog(value)
            if method == 'history' and set(args) in ({'profile', 'session_id', 'limit'},
                                                      {'profile', 'session_id', 'limit', 'before'}):
                value = await asyncio.wait_for(asyncio.to_thread(source.history,
                    args['profile'], args['session_id'], before=args.get('before'),
                    limit=args['limit']), timeout=5)
                if (not isinstance(value, dict) or set(value) != {'messages', 'next_before', 'has_more'}
                        or type(value['has_more']) is not bool
                        or not isinstance(value['messages'], list) or len(value['messages']) > 8
                        or any(not isinstance(row, dict) or set(row) != {'role', 'text', 'cursor'}
                               or row['role'] not in ('user', 'assistant')
                               or not isinstance(row['text'], str) or len(row['text']) > 500
                               or type(row['cursor']) is not int or row['cursor'] <= 0
                               for row in value['messages'])
                        or (value['next_before'] is not None and
                            (type(value['next_before']) is not int or value['next_before'] <= 0))
                        or (value['has_more'] and
                            (not value['messages'] or value['next_before'] != value['messages'][0]['cursor']))
                        or (not value['has_more'] and value['next_before'] is not None)):
                    raise PeerError('invalid history result')
                _encode({'ok': True, 'result': value})
                return value
            if method == 'preview' and set(args) == {'profile', 'session_id', 'limit'}:
                value = await asyncio.wait_for(asyncio.to_thread(source.preview,
                    args['profile'], args['session_id'], args['limit']), timeout=5)
                if (not isinstance(value, dict) or set(value) != {'messages'}
                        or not isinstance(value['messages'], list) or len(value['messages']) > 8
                        or any(not isinstance(row, dict) or set(row) != {'role', 'text'}
                               or row['role'] not in ('user', 'assistant')
                               or not isinstance(row['text'], str) or len(row['text']) > 500
                               for row in value['messages'])):
                    raise PeerError('invalid preview result')
                _encode({'ok': True, 'result': value})
                return value
            if method == 'read' and set(args) <= {'target', 'lines', 'max_bytes'} and 'target' in args:
                value = await asyncio.wait_for(asyncio.to_thread(source.read,
                    args['target'], lines=args.get('lines', 80), max_bytes=args.get('max_bytes', 8192)), timeout=5)
                if not isinstance(value, dict) or not isinstance(value.get('text'), str):
                    raise PeerError('invalid read result')
                return value
            raise PeerError('unknown operation')
        except (SourceError, ObserveError, asyncio.TimeoutError) as exc:
            raise PeerError('local provider unavailable') from exc
    return handle
