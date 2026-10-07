"""Explicitly paired Ares peers; no pairing credential in argv or output."""

import getpass
import re
import sys
from urllib.parse import quote

_ALIAS = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PEER_ID = re.compile(r"[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-fA-F]{64}\Z")
_CODE = re.compile(r"[0-9]{8}\Z")
_PEER_FIELDS = frozenset(("id", "peerId", "peer_id", "alias", "name", "address", "port",
                          "fingerprint", "paired", "online", "trusted", "status"))
_SESSION_FIELDS = frozenset(("id", "profile", "source", "title", "message_count",
                             "started_at", "last_activity_at", "cwd", "git_repo_root"))


def register(subparsers):
    peers = subparsers.add_parser("peers", help="Discover, pair, revoke, and inspect peers")
    actions = peers.add_subparsers(dest="peers_action", required=True)
    actions.add_parser("list", help="Show discovered and trusted peers")
    pair = actions.add_parser("pair", help="Pair a discovered peer with an independently verified pin")
    pair.add_argument("alias", metavar="ALIAS")
    pair.add_argument("--peer-id", required=True, metavar="ID")
    pair.add_argument("--fingerprint", required=True, metavar="SHA256")
    pair.add_argument("--code-stdin", action="store_true", help="Read the pairing code from stdin instead of a masked prompt")
    revoke = actions.add_parser("revoke", help="Remove peer trust")
    revoke.add_argument("alias", metavar="ALIAS")
    revoke.add_argument("--confirm", action="store_true", help="Explicitly authorize revocation")
    actions.add_parser("sessions", help="List peer sessions (read-only)").add_argument("alias", metavar="ALIAS")


def _alias(value):
    if not _ALIAS.fullmatch(value) or value == "local":
        raise ValueError("Peer alias must be 1–64 ASCII letters, digits, _ or - (not local)")
    return quote(value, safe="")


def _read_code(from_stdin):
    if from_stdin:
        code = sys.stdin.readline(65)
        if code.endswith("\n"):
            code = code[:-1]
    else:
        if not sys.stdin.isatty():
            raise ValueError("Pairing requires an interactive terminal or --code-stdin")
        try:
            code = getpass.getpass("Pairing code: ")
        except (EOFError, OSError):
            raise ValueError("Could not read pairing code") from None
    if not _CODE.fullmatch(code):
        raise ValueError("Pairing code must be exactly eight digits")
    return code


def execute(args, request):
    action = args.peers_action
    if action == "list":
        response = request("GET", "/v1/peers")
        if not isinstance(response.get("peers"), list) or not all(
                isinstance(p, dict) for p in response["peers"]):
            raise ValueError("Peer inventory unavailable")
        return {"ok": True, "peers": [
            {key: value for key, value in peer.items() if key in _PEER_FIELDS}
            for peer in response["peers"]]}
    alias = _alias(args.alias)
    if action == "sessions":
        response = request("GET", "/v1/peers/" + alias + "/sessions")
        if not isinstance(response.get("sessions"), list) or not all(
                isinstance(s, dict) for s in response["sessions"]):
            raise ValueError("Peer sessions unavailable")
        return {"ok": True, "sessions": [
            {key: value for key, value in session.items() if key in _SESSION_FIELDS}
            for session in response["sessions"]]}
    if action == "revoke":
        if not args.confirm:
            raise ValueError("Revocation requires --confirm")
        request("POST", "/v1/peers/" + alias + "/revoke", {"confirmed": True})
        return {"ok": True, "alias": args.alias, "revoked": True}
    if action != "pair":
        raise ValueError("Unknown peer action")
    if not _PEER_ID.fullmatch(args.peer_id):
        raise ValueError("Peer ID must be 32 lowercase hex digits")
    if not _FINGERPRINT.fullmatch(args.fingerprint):
        raise ValueError("Fingerprint must be 64 SHA-256 hex digits, verified independently at the source")
    code = _read_code(args.code_stdin)
    try:
        request("POST", "/v1/peers/pair", {"peerId": args.peer_id, "alias": args.alias,
                                              "fingerprint": args.fingerprint.lower(), "code": code})
    except Exception:
        # The server may reflect the code in its error. Never print its text.
        raise ValueError("Pairing rejected or unavailable") from None
    return {"ok": True, "alias": args.alias, "peerId": args.peer_id}


def human(result):
    if "peers" in result:
        return "\n".join(f"{p.get('alias') or p.get('name') or p.get('peerId', '?')}  "
                         f"{'paired' if p.get('paired') is True or p.get('trusted') is True else 'untrusted'}  "
                         f"{p.get('status') or {True: 'online', False: 'offline'}.get(p.get('online'), 'unknown')}  "
                         f"{p.get('peerId', '')}" for p in result["peers"] if isinstance(p, dict))
    if "sessions" in result:
        return "\n".join(f"{s.get('id', '?')}  {s.get('profile', '')}" for s in result["sessions"]
                         if isinstance(s, dict))
    if result.get("revoked"):
        return f"Revoked {result['alias']}"
    return f"Paired {result['alias']} ({result['peerId']})"
