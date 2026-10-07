"""Owner-private persistent identity and authentication state for Ares Peer."""
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import stat
import subprocess
import time


class PeerError(Exception):
    """A fail-closed peer operation failure (without credential detail)."""


_WRITE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_WRITE_FIELDS = ('socketPath', 'paneId', 'tabId', 'terminalId', 'agentKind',
                 'sessionId', 'profile', 'identitySource', 'terminalGeneration')


def _verified_write_target(source, profile, session_id, terminal_id):
    try:
        inventory = source.inventory()
    except Exception as exc:
        raise PeerError('source inventory unavailable') from exc
    if (not isinstance(inventory, dict) or inventory.get('complete') is not True
            or inventory.get('errors', []) != [] or not isinstance(inventory.get('terminals'), list)):
        raise PeerError('source inventory incomplete')
    items = inventory['terminals']
    matches = [t for t in items if isinstance(t, dict) and
               t.get('sessionId') == session_id and t.get('profile') == profile]
    if len(matches) != 1 or matches[0].get('terminalId') != terminal_id:
        raise PeerError('unique owner unavailable')
    target = matches[0]
    if (target.get('agentKind') != 'hermes' or target.get('identitySource') != 'verified'
            or not all(isinstance(target.get(k), str) and target[k]
                       for k in _WRITE_FIELDS)
            or not _WRITE_ID.fullmatch(target['terminalGeneration'])
            or any(t is not target and isinstance(t, dict) and
                   (t.get('terminalId') == terminal_id or
                    (t.get('socketPath') == target['socketPath'] and
                     t.get('paneId') == target['paneId'])) for t in items)):
        raise PeerError('terminal instance unverified')
    return {k: target[k] for k in _WRITE_FIELDS}


def _private_path(path, mode):
    if path.is_symlink() or not path.exists() or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != mode:
        raise PeerError('unsafe peer state permissions')


def _write_private(path, content):
    if path.exists() or path.is_symlink():
        _private_path(path, 0o600)
    temp = path.with_name('.' + path.name + '-' + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


class Identity:
    def __init__(self, root):
        self.root = Path(root)
        self.key = self.root / 'identity.key'
        self.cert = self.root / 'identity.crt'
        self.id_file = self.root / 'peer-id'
        if not self.key.exists() and not self.cert.exists() and not self.id_file.exists():
            peer_id = secrets.token_hex(16)
            # openssl writes no credentials to command arguments or output.
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes',
                            '-keyout', str(self.key), '-out', str(self.cert), '-days', '3650',
                            '-subj', '/CN=ares-peer-' + peer_id], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.chmod(self.key, 0o600)
            os.chmod(self.cert, 0o600)
            _write_private(self.id_file, peer_id.encode('ascii'))
        for path in (self.key, self.cert, self.id_file):
            _private_path(path, 0o600)
        self.peer_id = self.id_file.read_text('ascii')
        if len(self.peer_id) != 32 or any(c not in '0123456789abcdef' for c in self.peer_id):
            raise PeerError('invalid identity')
        import ssl
        self.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.cert.read_text('ascii'))).hexdigest()


class PeerState:
    def __init__(self, root):
        self.root = Path(root)
        if not self.root.exists():
            self.root.mkdir(mode=0o700, parents=True)
        _private_path(self.root, 0o700)
        self.identity = Identity(self.root)
        self.state_file = self.root / 'state.json'
        if self.state_file.exists() or self.state_file.is_symlink():
            _private_path(self.state_file, 0o600)
            data = json.loads(self.state_file.read_text())
        else:
            data = {'tokens': {}, 'trusted': {}, 'armed': None, 'attempts': [], 'locked_until': 0}
            _write_private(self.state_file, json.dumps(data).encode())
        self.observe_peers = set(data.get('observe_peers', []))
        # Deliberately not serialized: restart drops all write consent.
        self.write_grant = None
        self.tokens = data['tokens']
        self.trusted = data['trusted']
        self.armed = data['armed']
        self.attempts = data['attempts']
        self.locked_until = data['locked_until']

    @property
    def pair_attempts(self):
        return len(self.attempts)

    def save(self):
        _write_private(self.state_file, json.dumps({'tokens': self.tokens, 'trusted': self.trusted,
            'armed': self.armed, 'attempts': self.attempts, 'locked_until': self.locked_until,
            'observe_peers': sorted(self.observe_peers)},
            separators=(',', ':')).encode())

    def arm(self, ttl=120):
        code = f'{secrets.randbelow(100_000_000):08d}'
        self.armed = {'hash': hashlib.sha256(code.encode()).hexdigest(), 'expires': time.time() + min(ttl, 300)}
        self.save()
        return code

    def pair(self, peer_id, code):
        now = time.time()
        self.attempts = [t for t in self.attempts if t > now - 300]
        if now < self.locked_until or len(self.attempts) >= 5:
            raise PeerError('pairing denied')
        self.attempts.append(now)
        if len(self.attempts) >= 5:
            self.locked_until = now + 300
        valid_id = isinstance(peer_id, str) and len(peer_id) == 32 and all(c in '0123456789abcdef' for c in peer_id)
        valid_code = isinstance(code, str) and len(code) == 8 and code.isascii() and code.isdigit()
        valid = self.armed is not None and now < self.armed['expires'] and valid_code and hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), self.armed['hash'])
        if not valid_id or not valid:
            self.save()
            raise PeerError('pairing denied')
        token = secrets.token_urlsafe(32)
        self.observe_peers.discard(peer_id)  # Re-pair never inherits observation consent.
        if self.write_grant and self.write_grant['peer'] == peer_id:
            self.write_grant = None
        self.tokens[peer_id] = hashlib.sha256(token.encode()).hexdigest()
        self.armed = None
        self.attempts = []
        self.locked_until = 0
        self.save()
        return token

    def authorize(self, peer_id, token):
        expected = self.tokens.get(peer_id) if isinstance(peer_id, str) else None
        digest = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) and len(token) <= 256 else ''
        return isinstance(expected, str) and hmac.compare_digest(expected, digest)

    def observe_allowed(self, peer_id):
        return peer_id in self.observe_peers and peer_id in self.tokens

    def set_observe_permission(self, peer_id, allow):
        if (not isinstance(peer_id, str) or len(peer_id) != 32
                or any(c not in '0123456789abcdef' for c in peer_id)
                or type(allow) is not bool or peer_id not in self.tokens):
            raise PeerError('unknown paired peer')
        if allow:
            self.observe_peers.add(peer_id)
        else:
            self.observe_peers.discard(peer_id)
        self.save()

    def set_write_grant(self, peer_id, profile, session_id, terminal_id, allow, ttl, source):
        if (not isinstance(peer_id, str) or len(peer_id) != 32
                or any(c not in '0123456789abcdef' for c in peer_id)
                or peer_id not in self.tokens or type(allow) is not bool
                or type(ttl) is not int or not 1 <= ttl <= 300
                or any(not isinstance(v, str) or not _WRITE_ID.fullmatch(v)
                       for v in (profile, session_id, terminal_id))):
            raise PeerError('invalid write grant')
        if not allow:
            if self.write_grant and self.write_grant['peer'] == peer_id:
                self.write_grant = None
            return
        if self.write_grant and self.write_grant['expires'] > time.monotonic():
            if self.write_grant['peer'] != peer_id:
                raise PeerError('another write owner is active')
        target = _verified_write_target(source, profile, session_id, terminal_id)
        self.write_grant = {'peer': peer_id, 'token_digest': self.tokens[peer_id],
                            'target': target, 'expires': time.monotonic() + ttl}

    def write_allowed(self, peer_id, source):
        grant = self.write_grant
        if not grant:
            return False
        if (grant['expires'] <= time.monotonic() or
                self.tokens.get(grant['peer']) != grant['token_digest']):
            self.write_grant = None
            return False
        try:
            target = grant['target']
            current = _verified_write_target(source, target['profile'],
                                              target['sessionId'], target['terminalId'])
        except PeerError:
            self.write_grant = None
            return False
        if current != target:
            self.write_grant = None
            return False
        return peer_id == grant['peer']

    def revoke_authorized(self, peer_id, token):
        if not self.authorize(peer_id, token):
            raise PeerError('unauthorized')
        self.revoke(peer_id)

    def revoke(self, peer_id):
        if self.write_grant and self.write_grant['peer'] == peer_id:
            self.write_grant = None
        self.observe_peers.discard(peer_id)
        self.tokens.pop(peer_id, None)
        self.trusted.pop(peer_id, None)
        self.save()
