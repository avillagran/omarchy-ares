"""Terminal inventory and exact-identity Herdr attachment for the Ares CLI.

The injected client implements request(method, path, body=None) -> dict.
No terminal input is sent through the API. Capture is deliberately unsupported.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MEMBER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL_ID = _ID
_SSH = re.compile(r"[A-Za-z0-9_-]+@[A-Za-z0-9.-]+\Z")
_PATH = re.compile(r"/[A-Za-z0-9/._-]+\Z")


def _target(parser):
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--room", metavar="ID")
    choice.add_argument("--group", metavar="ID")
    parser.add_argument("--member", metavar="KEY")


def register(subparsers):
    """Add `terminals` to the root argparse subparsers."""
    parser = subparsers.add_parser("terminals", help="Inspect or attach existing Herdr terminals")
    actions = parser.add_subparsers(dest="terminals_action", required=True)
    listing = actions.add_parser("list", help="List existing room terminals")
    listing.add_argument("--room", required=True, metavar="ID")
    attach = actions.add_parser("attach", help="Attach to an existing terminal")
    _target(attach)
    capture = actions.add_parser("capture", help="Read-only capture (currently unavailable)")
    _target(capture)
    return parser


def _ident(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid room, group, or member identifier")
    return quote(value, safe="")


def _member(value):
    if not isinstance(value, str) or not _MEMBER.fullmatch(value):
        raise ValueError("Invalid group member key")
    return quote(value, safe="")


def _validate_scope(args):
    if bool(args.group) != bool(args.member):
        raise ValueError("--group and --member must be supplied together")
    if args.room:
        return "/v1/tty/rooms/" + _ident(args.room) + "/terminals"
    return "/v1/tty/groups/" + _ident(args.group) + "/members/" + _member(args.member) + "/terminal"


def _terminal_descriptor(value):
    """Reject incomplete, synthetic, or unsafe execution identities."""
    if not isinstance(value, dict):
        raise ValueError("Terminal descriptor unavailable")
    candidate = value.get("tty", value)
    if not isinstance(candidate, dict):
        raise ValueError("Terminal descriptor unavailable")
    if "tty" in value:
        if (candidate.get("sessionName") != "terminal"
                or candidate.get("agentName") != candidate.get("terminalId")
                or candidate.get("agentStarted") is not True
                or candidate.get("created") is not False):
            raise ValueError("No verified existing Herdr terminal")
    terminal_id = candidate.get("terminalId")
    host = candidate.get("host")
    ssh = candidate.get("sshTarget")
    binary = candidate.get("remoteBin" if "tty" in value else "herdrBin")
    socket = candidate.get("remoteSocket" if "tty" in value else "socketPath")
    if not isinstance(terminal_id, str) or not _TERMINAL_ID.fullmatch(terminal_id):
        raise ValueError("Terminal identity unavailable")
    if not isinstance(host, str) or not _ID.fullmatch(host):
        raise ValueError("Terminal host identity unavailable")
    if not isinstance(ssh, str) or (ssh and not _SSH.fullmatch(ssh)):
        raise ValueError("Invalid SSH target")
    if (host == "local") != (ssh == ""):
        raise ValueError("Conflicting terminal host and SSH identity")
    if not all(isinstance(path, str) and _PATH.fullmatch(path)
               and "." not in path.split("/") and ".." not in path.split("/")
               for path in (binary, socket)) or not socket.endswith("/herdr.sock"):
        raise ValueError("Invalid Herdr executable or socket path")
    return terminal_id, ssh, binary, socket


def dispatch(args, client):
    """Return 0 on success, 2 on unsafe/unavailable operations.

    The client's request method is the only API dependency; it must implement
    request(method, path, body=None) -> dict. Attach invokes the bundled helper
    with argv, never a shell, and inherits the caller's interactive TTY.
    """
    try:
        action = args.terminals_action
        if action == "capture":
            _validate_scope(args)
            raise ValueError("Read-only terminal capture is unsupported: no verified bounded capture API is available")
        if action == "list":
            response = client.request("GET", "/v1/tty/rooms/" + _ident(args.room) + "/terminals")
            if (not isinstance(response, dict) or response.get("ok") is False
                    or not isinstance(response.get("terminals"), list)):
                raise ValueError("Terminal inventory unavailable")
            print(json.dumps(response, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":") if getattr(args, "json", False) else None))
            return 0
        if action != "attach":
            raise ValueError("Unknown terminal action")
        path = _validate_scope(args)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise ValueError("Terminal attachment requires an interactive TTY")
        response = client.request("GET", path)
        if not isinstance(response, dict) or response.get("ok") is False:
            raise ValueError("Terminal discovery failed")
        if args.room:
            if (response.get("complete") is not True
                    or response.get("errors") != []
                    or not isinstance(response.get("terminals"), list)):
                raise ValueError("Terminal inventory incomplete")
            binding = response.get("binding")
            if (not isinstance(binding, dict)
                    or not isinstance(binding.get("terminal_key"), str)
                    or not _KEY.fullmatch(binding["terminal_key"])
                    or binding.get("host") != response.get("host")):
                raise ValueError("Room has no verified terminal association")
            terminals = [item for item in response["terminals"]
                         if isinstance(item, dict) and item.get("id") == binding["terminal_key"]]
            if len(terminals) != 1 or terminals[0].get("host") != response["host"]:
                raise ValueError("Associated terminal is unavailable or ambiguous")
            descriptor = terminals[0]
        else:
            tty = response.get("tty")
            if (not isinstance(tty, dict) or tty.get("roomId") != args.group
                    or tty.get("memberKey") != args.member or tty.get("readOnly") is not True):
                raise ValueError("Group member terminal identity mismatch")
            descriptor = response
        identity = _terminal_descriptor(descriptor)
        helper = Path(__file__).resolve().with_name("ares-herdr-client")
        if not helper.is_file():
            raise ValueError("Herdr attach helper unavailable")
        return subprocess.run([str(helper), "terminal", *identity], check=False).returncode
    except (ValueError, OSError) as error:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")))
        else:
            print(f"ares terminals: {error}", file=sys.stderr)
        return 2
