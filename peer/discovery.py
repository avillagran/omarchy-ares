"""Untrusted LAN announcements; never treat TXT records as authorization."""
import asyncio
import contextlib
import ipaddress
import socket
import sys

from .core import PeerError

from zeroconf import ServiceInfo, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

SERVICE = '_ares-peer._tcp.local.'
RETRY_SECONDS = 3


def _addresses(host):
    if host != '0.0.0.0':
        return [socket.inet_aton(host)]
    addresses = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(('224.0.0.251', 5353))  # Finds an interface; sends no datagram.
            addresses.add(route.getsockname()[0])
    except OSError:
        pass
    try:
        addresses.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None,
            family=socket.AF_INET, type=socket.SOCK_DGRAM))
    except OSError:
        pass
    routable = sorted(address for address in addresses if not ipaddress.ip_address(address).is_loopback
                      and not ipaddress.ip_address(address).is_unspecified)
    if not routable:
        raise PeerError('no IPv4 LAN address for mDNS advertisement')
    return [socket.inet_aton(address) for address in routable]


class Discovery:
    def __init__(self, state, host, port):
        self.state, self.host, self.port = state, host, port
        self.zc = None
        self.info = None
        self.retry_task = None

    async def start(self):
        try:
            addresses = _addresses(self.host)
        except PeerError:
            print('Ares Peer: waiting for an IPv4 LAN address before mDNS advertisement', file=sys.stderr)
            self.retry_task = asyncio.create_task(self._retry())
            return
        await self._register(addresses)

    async def _retry(self):
        while True:
            await asyncio.sleep(RETRY_SECONDS)
            try:
                addresses = _addresses(self.host)
            except PeerError:
                continue
            try:
                await self._register(addresses)
            except Exception as exc:
                print(f'Ares Peer: mDNS advertisement failed; retrying: {exc}', file=sys.stderr)
                continue
            return

    async def _register(self, addresses):
        self.zc = AsyncZeroconf()
        try:
            self.info = ServiceInfo(SERVICE, f'{self.state.identity.peer_id}.{SERVICE}',
                addresses=addresses,
                port=self.port, properties={'peer_id': self.state.identity.peer_id,
                'fingerprint': self.state.identity.fingerprint}, server=socket.gethostname() + '.local.')
            await self.zc.async_register_service(self.info)
        except Exception:
            await self.zc.async_close()
            self.zc = None
            self.info = None
            raise

    async def close(self):
        if self.retry_task:
            self.retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.retry_task
            self.retry_task = None
        if self.zc:
            await self.zc.async_unregister_service(self.info)
            await self.zc.async_close()
            self.zc = None

    async def discover(self, timeout=2):
        names = set()
        zc = self.zc or AsyncZeroconf()
        def on_service(*, zeroconf, service_type, name, state_change):
            names.add(name)
        browser = AsyncServiceBrowser(zc.zeroconf, SERVICE, handlers=[on_service])
        try:
            await asyncio.sleep(min(max(timeout, 0), 5))
            peers = []
            for name in sorted(names):
                info = AsyncServiceInfo(SERVICE, name)
                if await info.async_request(zc.zeroconf, 1000):
                    addresses = info.parsed_addresses()
                    if addresses:
                        peers.append({'name': name, 'address': addresses[0], 'port': info.port,
                                      'peer_id': info.properties.get(b'peer_id', b'').decode('ascii', 'replace'),
                                      'fingerprint': info.properties.get(b'fingerprint', b'').decode('ascii', 'replace'),
                                      'trusted': info.properties.get(b'peer_id', b'').decode('ascii', 'replace') in self.state.trusted})
            return peers
        finally:
            await browser.async_cancel()
            if zc is not self.zc:
                await zc.async_close()
