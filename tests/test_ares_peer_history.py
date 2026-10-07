"""Bounded source-local Hermes history and paired read RPC tests."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import peer.herdr_source as herdr_source

from peer.control import LocalControl
from peer.core import PeerError, PeerState
from peer.herdr_source import LocalSource, SourceError
from peer.provider import provider_handler
from peer.transport import MAX_MESSAGE, PeerClient, PeerServer, _encode

SID = '20260925_001441_887074'


class HistorySourceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'state.db'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, profile_name TEXT, hidden INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, parent_session_id TEXT)')
            conn.execute('INSERT INTO sessions (id,profile_name) VALUES (?,?)', (SID, 'default'))
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, display_order INTEGER, display_identity BLOB, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, _compressed_summary INTEGER DEFAULT 0, display_kind TEXT, display_metadata TEXT, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT, reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT, codex_reasoning_items TEXT)')
            conn.execute('CREATE INDEX idx_messages_display_page ON messages(session_id, display_order, active DESC, id DESC) WHERE active = 1 OR compacted = 1')
        self.source = LocalSource({'default': self.db}, [])

    def add(self, role, content, *, session=SID, order=None, **kwargs):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            cols = {'session_id': session, 'role': role, 'content': content, 'display_identity': b'identity', **kwargs}
            if order is not None:
                cols['display_order'] = order
            else:
                cols['display_order'] = conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM messages').fetchone()[0]
            conn.execute('INSERT INTO messages (' + ','.join(cols) + ') VALUES (' + ','.join('?' for _ in cols) + ')', tuple(cols.values()))

    def test_exact_paginated_history(self):
        for i in range(19):
            self.add('user' if i % 2 else 'assistant', f'message-{i}')
        newest = self.source.history('default', SID, limit=8)
        self.assertEqual([m['text'] for m in newest['messages']], [f'message-{i}' for i in range(11, 19)])
        self.assertTrue(newest['has_more'])
        self.assertEqual(newest['next_before'], newest['messages'][0]['cursor'])
        second = self.source.history('default', SID, before=newest['next_before'], limit=8)
        self.assertEqual([m['text'] for m in second['messages']], [f'message-{i}' for i in range(3, 11)])
        oldest = self.source.history('default', SID, before=second['next_before'], limit=8)
        self.assertEqual([m['text'] for m in oldest['messages']], [f'message-{i}' for i in range(3)])
        self.assertEqual((oldest['next_before'], oldest['has_more']), (None, False))

    def test_compaction_copies_filter_before_page_and_no_duplicates(self):
        self.add('user', 'original', order=1, active=0, compacted=1)
        self.add('assistant', 'old answer', order=2, active=0, compacted=1)
        self.add('assistant', 'tool secret', order=3, tool_calls='[{}]')
        self.add('system', 'system secret', order=4)
        self.add('assistant', 'reason secret', order=5, reasoning='thought')
        self.add('user', 'original', order=1, active=1)
        self.add('assistant', 'old answer', order=2, active=1)
        self.add('assistant', 'summary secret', order=6, _compressed_summary=1)
        self.add('assistant', 'model secret', order=7, display_metadata='{"model_only":true}')
        self.add('user', 'new', order=8)
        page = self.source.history('default', SID, limit=2)
        self.assertEqual([(m['cursor'], m['text']) for m in page['messages']], [(2, 'old answer'), (8, 'new')])
        self.assertTrue(page['has_more'])
        old = self.source.history('default', SID, before=page['next_before'], limit=2)
        self.assertEqual([(m['cursor'], m['text']) for m in old['messages']], [(1, 'original')])
        self.assertFalse(old['has_more'])

    def test_invalid_identity_hidden_archive_and_cursor(self):
        self.add('user', 'safe')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE sessions SET archived=1 WHERE id=?', (SID,))
        self.assertEqual(len(self.source.history('default', SID)['messages']), 1)
        for args in ({'profile': 'other', 'session_id': SID},
                     {'profile': 'default', 'session_id': SID + '-descendant'},
                     {'profile': 'default', 'session_id': '../bad'},
                     *({'profile': 'default', 'session_id': SID, 'before': bad}
                       for bad in (0, -1, True, '1', 2**63)),
                     *({'profile': 'default', 'session_id': SID, 'limit': bad}
                       for bad in (0, 9, True, '8'))):
            with self.subTest(args=args), self.assertRaises(SourceError):
                self.source.history(**args)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE sessions SET hidden=1 WHERE id=?', (SID,))
        with self.assertRaises(SourceError):
            self.source.history('default', SID)

    def test_missing_index_or_unbackfilled_display_order_fails_closed(self):
        self.add('user', 'safe')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE messages SET display_order=NULL')
        with self.assertRaises(SourceError):
            self.source.history('default', SID)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE messages SET display_order=1')
            conn.execute('DROP INDEX idx_messages_display_page')
        with self.assertRaises(SourceError):
            self.source.history('default', SID)

    def test_ascii_wire_limit_and_private_text_redaction(self):
        for i in range(8):
            self.add('assistant', chr(1) * 500 if i else 'Authorization: Bearer private')
        page = self.source.history('default', SID)
        self.assertEqual(page['messages'][0]['text'], '[redacted]')
        self.assertLessEqual(len(_encode({'ok': True, 'result': page})), MAX_MESSAGE)

    def test_hide_during_page_construction_refuses_private_page(self):
        self.add('user', 'private transcript')
        search = herdr_source._PRIVATE_PREVIEW.search
        changed = False
        def hide_during_projection(text):
            nonlocal changed
            if not changed:
                changed = True
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('UPDATE sessions SET hidden=1 WHERE id=?', (SID,))
            return search(text)
        with patch.object(herdr_source, '_PRIVATE_PREVIEW', SimpleNamespace(search=hide_during_projection)):
            with self.assertRaises(SourceError) as caught:
                self.source.history('default', SID)
        self.assertTrue(changed)
        self.assertNotIn('private transcript', str(caught.exception))

    def test_profile_change_during_page_construction_refuses_private_page(self):
        self.add('user', 'private transcript')
        search = herdr_source._PRIVATE_PREVIEW.search
        changed = False
        def move_during_projection(text):
            nonlocal changed
            if not changed:
                changed = True
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('UPDATE sessions SET profile_name=? WHERE id=?', ('other', SID))
            return search(text)
        with patch.object(herdr_source, '_PRIVATE_PREVIEW', SimpleNamespace(search=move_during_projection)):
            with self.assertRaises(SourceError) as caught:
                self.source.history('default', SID)
        self.assertTrue(changed)
        self.assertNotIn('private transcript', str(caught.exception))

    def test_session_removed_during_page_construction_refuses_private_page(self):
        self.add('user', 'private transcript')
        search = herdr_source._PRIVATE_PREVIEW.search
        changed = False
        def remove_during_projection(text):
            nonlocal changed
            if not changed:
                changed = True
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('DELETE FROM sessions WHERE id=?', (SID,))
            return search(text)
        with patch.object(herdr_source, '_PRIVATE_PREVIEW', SimpleNamespace(search=remove_during_projection)):
            with self.assertRaises(SourceError) as caught:
                self.source.history('default', SID)
        self.assertTrue(changed)
        self.assertNotIn('private transcript', str(caught.exception))


class HistoryWireTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db = root / 'state.db'
        self.db = db
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, hidden INTEGER DEFAULT 0)')
            conn.execute('INSERT INTO sessions(id) VALUES (?)', (SID,))
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, display_order INTEGER, display_identity BLOB, active INTEGER, compacted INTEGER, display_kind TEXT)')
            conn.execute('CREATE INDEX idx_messages_display_page ON messages(session_id,display_order,active DESC,id DESC) WHERE active=1 OR compacted=1')
            conn.execute('INSERT INTO messages(session_id,role,content,display_order,display_identity,active,compacted) VALUES (?,?,?,?,?,1,0)', (SID, 'user', 'hello', 1, b'id'))
        self.owner, self.remote = PeerState(root / 'owner'), PeerState(root / 'remote')
        self.server = PeerServer(self.owner, provider_handler(LocalSource({'default': db}, [])), host='127.0.0.1')
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.url = f'wss://127.0.0.1:{self.server.port}'

    async def test_pair_revocation_and_strict_history_rpc(self):
        request = {'profile': 'default', 'session_id': SID, 'limit': 8}
        with self.assertRaises(PeerError):
            await PeerClient(self.remote).request(self.owner.identity.peer_id, self.url, 'history', request)
        peer = await PeerClient.pair(self.remote, self.url, self.owner.identity.fingerprint, self.owner.arm())
        expected = {'messages': [{'role': 'user', 'text': 'hello', 'cursor': 1}], 'next_before': None, 'has_more': False}
        self.assertEqual(await PeerClient(self.remote).request(peer, self.url, 'history', request), expected)
        for args in (dict(request, path='/etc/passwd'), dict(request, before=0),
                     {'profile': 'default', 'session_id': SID}, dict(request, session_id='unknown')):
            with self.subTest(args=args), self.assertRaises(PeerError):
                await PeerClient(self.remote).request(peer, self.url, 'history', args)
        self.owner.revoke(self.remote.identity.peer_id)
        with self.assertRaises(PeerError):
            await PeerClient(self.remote).request(peer, self.url, 'history', request)

    async def test_revocation_during_history_request_releases_no_page(self):
        peer = await PeerClient.pair(self.remote, self.url, self.owner.identity.fingerprint, self.owner.arm())
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(method, args):
            entered.set()
            await release.wait()
            return {'messages': [{'role': 'user', 'text': 'private', 'cursor': 1}],
                    'next_before': None, 'has_more': False}
        self.server.handler = held
        task = asyncio.create_task(PeerClient(self.remote).request(peer, self.url, 'history',
            {'profile': 'default', 'session_id': SID, 'limit': 8}))
        await asyncio.wait_for(entered.wait(), 2)
        self.owner.revoke(self.remote.identity.peer_id)
        release.set()
        with self.assertRaises(PeerError):
            await task

    async def test_hide_after_page_construction_before_response_refuses_private_page(self):
        peer = await PeerClient.pair(self.remote, self.url, self.owner.identity.fingerprint, self.owner.arm())
        entered, release = asyncio.Event(), asyncio.Event()
        provider = self.server.handler
        async def held(method, args):
            result = await provider(method, args)
            entered.set()
            await release.wait()
            return result
        self.server.handler = held
        task = asyncio.create_task(PeerClient(self.remote).request(peer, self.url, 'history',
            {'profile': 'default', 'session_id': SID, 'limit': 8}))
        await asyncio.wait_for(entered.wait(), 2)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE sessions SET hidden=1 WHERE id=?', (SID,))
        release.set()
        with self.assertRaises(PeerError) as caught:
            await task
        self.assertNotIn('hello', str(caught.exception))

    async def test_private_control_socket_forwards_only_trusted_history(self):
        peer = await PeerClient.pair(self.remote, self.url, self.owner.identity.fingerprint, self.owner.arm())
        self.remote.trusted[peer]['alias'] = 'source'
        self.remote.save()
        control = LocalControl(self.remote)
        await control.start()
        self.addAsyncCleanup(control.close)
        async def send(payload):
            reader, writer = await asyncio.open_unix_connection(str(control.path))
            writer.write(json.dumps(payload).encode() + b'\n')
            await writer.drain()
            result = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            return result
        req = {'op': 'rpc', 'peer': 'source', 'request': {'op': 'history', 'profile': 'default', 'session_id': SID, 'limit': 8}}
        self.assertEqual((await send(req))['result']['messages'][0]['text'], 'hello')
        self.assertFalse((await send({**req, 'peer': 'unknown'}))['ok'])
        self.assertFalse((await send({**req, 'request': dict(req['request'], path='/etc/passwd')}))['ok'])


if __name__ == '__main__':
    unittest.main()
