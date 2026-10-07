"""Synthetic read-only chat preview and authenticated RPC contracts."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from peer.control import LocalControl
from peer.core import PeerError, PeerState
from peer.herdr_source import LocalSource, SourceError
from peer.provider import provider_handler
from peer.transport import MAX_MESSAGE, PeerClient, PeerServer, _encode

SID = '20260925_001441_887074'


class PreviewSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, hidden INTEGER DEFAULT 0)')
            conn.execute('INSERT INTO sessions VALUES (?, 0)', (SID,))
            conn.execute('INSERT INTO sessions VALUES (?, 1)', ('hidden',))
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, reasoning TEXT, reasoning_content TEXT, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, display_kind TEXT)')
        self.source = LocalSource({'default': self.db}, [])

    def add(self, session, role, text, **kw):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO messages (session_id,role,content,reasoning,reasoning_content,tool_calls,tool_call_id,tool_name,active,compacted,display_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         (session, role, text, kw.get('reasoning'), kw.get('reasoning_content'), kw.get('tool_calls'), kw.get('tool_call_id'), kw.get('tool_name'), kw.get('active', 1), kw.get('compacted', 0), kw.get('display_kind')))

    def test_exact_identity_hidden_and_unknown_denied(self):
        self.add(SID, 'user', 'Visible')
        self.add('hidden', 'user', 'HIDDEN-SECRET')
        self.assertEqual(self.source.preview('default', SID), {'messages': [{'role': 'user', 'text': 'Visible'}]})
        for profile, sid in [('other', SID), ('../default', SID), ('default', 'hidden'), ('default', 'missing'), ('default', '../state.db'), ('default', ''), ('default', SID + '\x00')]:
            with self.subTest(profile=profile, sid=sid), self.assertRaises(SourceError):
                self.source.preview(profile, sid)

    def test_recorded_profile_must_match_requested_profile(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('ALTER TABLE sessions ADD COLUMN profile_name TEXT')
            conn.execute('UPDATE sessions SET profile_name=? WHERE id=?', ('other', SID))
        with self.assertRaises(SourceError):
            self.source.preview('default', SID)

    def test_last_eight_paging_order_and_size(self):
        for i in range(16):
            self.add(SID, 'user' if i % 2 else 'assistant', str(i) + '😀' * 600)
        rows = self.source.preview('default', SID, limit=8)['messages']
        self.assertEqual(len(rows), 8)
        self.assertEqual([m['text'].split('😀')[0] for m in rows], [str(i) for i in range(8, 16)])
        self.assertTrue(all(len(m['text']) <= 500 for m in rows))
        self.assertEqual(len(self.source.preview('default', SID, limit=2)['messages']), 2)
        for bad in [0, 9, -1, True, '8']:
            with self.subTest(bad=bad), self.assertRaises(SourceError):
                self.source.preview('default', SID, limit=bad)
        self.assertLessEqual(len(_encode({'ok': True, 'result': {'messages': rows}})), MAX_MESSAGE)

    def test_excludes_tool_reasoning_system_and_inactive(self):
        for role, text, extra in [('system', 'SYSTEM-SECRET', {}), ('tool', 'TOOL-SECRET', {}),
                                  ('assistant', 'CALL-SECRET', {'tool_calls': '[{}]'}),
                                  ('assistant', 'TOOL-ID-SECRET', {'tool_call_id': 'call1'}),
                                  ('assistant', 'TOOL-NAME-SECRET', {'tool_name': 'shell'}),
                                  ('assistant', 'REASON-SECRET', {'reasoning': 'internal'}),
                                  ('assistant', 'REASON-CONTENT', {'reasoning_content': 'internal'}),
                                  ('assistant', 'HIDDEN-REASON', {'display_kind': 'reasoning'}),
                                  ('assistant', 'INACTIVE-SECRET', {'active': 0}),
                                  ('assistant', 'COMPACTED-SECRET', {'compacted': 1})]:
            self.add(SID, role, text, **extra)
        self.add(SID, 'assistant', 'Safe answer')
        self.assertEqual(self.source.preview('default', SID), {'messages': [{'role': 'assistant', 'text': 'Safe answer'}]})

    def test_obvious_credentials_codes_and_logs_are_redacted(self):
        for text in ('Authorization: Bearer abc123secret', 'api_key=secret-value',
                     'Pairing code: 12345678', '2026-09-27 12:00:00 INFO private log'):
            self.add(SID, 'assistant', text)
        messages = self.source.preview('default', SID)['messages']
        self.assertEqual(len(messages), 4)
        self.assertTrue(all(m['text'] == '[redacted]' for m in messages))

    def test_missing_messages_table_is_empty(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DROP TABLE messages')
        self.assertEqual(self.source.preview('default', SID), {'messages': []})
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name='messages'").fetchone()[0], 0)


class PreviewWireTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        db = root / 'state.db'
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, hidden INTEGER DEFAULT 0)')
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, reasoning TEXT, reasoning_content TEXT, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT, active INTEGER, compacted INTEGER, display_kind TEXT)')
            conn.execute('INSERT INTO sessions VALUES (?, 0)', (SID,))
            conn.execute('INSERT INTO messages (session_id,role,content,active,compacted) VALUES (?, ?, ?, 1, 0)', (SID, 'user', 'Hello'))
        self.source = LocalSource({'default': db}, [])
        self.owner, self.remote = PeerState(root / 'owner'), PeerState(root / 'remote')
        self.server = PeerServer(self.owner, provider_handler(self.source), host='127.0.0.1')
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.url = f'wss://127.0.0.1:{self.server.port}'

    async def test_pairing_auth_and_strict_preview_rpc(self):
        request = {'profile': 'default', 'session_id': SID, 'limit': 8}
        with self.assertRaises(PeerError):
            await PeerClient(self.remote).request(self.owner.identity.peer_id, self.url, 'preview', request)
        peer = await PeerClient.pair(self.remote, self.url, self.owner.identity.fingerprint, self.owner.arm())
        self.assertEqual(await PeerClient(self.remote).request(peer, self.url, 'preview', request),
                         {'messages': [{'role': 'user', 'text': 'Hello'}]})
        for args in (dict(request, path='/etc/passwd'), {'profile': 'default', 'session_id': SID},
                     dict(request, session_id='hidden')):
            with self.subTest(args=args), self.assertRaises(PeerError):
                await PeerClient(self.remote).request(peer, self.url, 'preview', args)
        self.owner.revoke(self.remote.identity.peer_id)
        with self.assertRaises(PeerError):
            await PeerClient(self.remote).request(peer, self.url, 'preview', request)
    async def test_owner_control_socket_forwards_only_paired_preview(self):
        peer = await PeerClient.pair(self.remote, self.url, self.owner.identity.fingerprint, self.owner.arm())
        self.remote.trusted[peer]['alias'] = 'source'
        self.remote.save()
        control = LocalControl(self.remote)
        await control.start()
        self.addAsyncCleanup(control.close)

        async def request(payload):
            reader, writer = await asyncio.open_unix_connection(str(control.path))
            writer.write(json.dumps(payload).encode() + b'\n')
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            return response

        req = {'op': 'rpc', 'peer': 'source', 'request': {'op': 'preview', 'profile': 'default',
                                                     'session_id': SID, 'limit': 8}}
        self.assertEqual(await request(req), {'ok': True, 'result': {'messages': [{'role': 'user', 'text': 'Hello'}]}})
        self.assertEqual((await request({**req, 'peer': 'unknown'}))['error'], 'denied')
        self.assertEqual((await request({**req, 'request': {**req['request'], 'path': '/etc/passwd'}}))['error'], 'denied')


if __name__ == '__main__':
    unittest.main()
