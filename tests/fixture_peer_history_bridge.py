"""Disposable real Peer TLS/provider/control fixture for Rust HTTP integration tests."""
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from peer.control import LocalControl
from peer.core import PeerState
from peer.herdr_source import LocalSource
from peer.provider import provider_handler
from peer.transport import PeerClient, PeerServer


async def main():
    home, db, ready = map(Path, sys.argv[1:])
    source_state = PeerState(home / 'source-peer')
    control_state = PeerState(home / '.local/share/ares-peer')
    source = LocalSource({'default': db}, [])
    provider = provider_handler(source)
    async def held_history(method, args):
        page = await provider(method, args)
        if method == 'history' and (home / 'hold-history').exists():
            (home / 'history-held').write_text('held')
            while not (home / 'release-history').exists():
                await asyncio.sleep(.01)
        return page
    server = PeerServer(source_state, held_history, host='127.0.0.1')
    assert server.writer is None and server.enable_writer is False
    await server.start()
    control = None
    try:
        url = f'wss://127.0.0.1:{server.port}'
        peer_id = await PeerClient.pair(control_state, url, source_state.identity.fingerprint,
                                        source_state.arm())
        control_state.trusted[peer_id]['alias'] = 'node'
        control_state.save()
        control = LocalControl(control_state)
        assert control.broker is None
        await control.start()
        ready.write_text('ready')
        stop = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        await stop.wait()
    finally:
        if control is not None:
            await control.close()
        await server.close()


if __name__ == '__main__':
    asyncio.run(main())
