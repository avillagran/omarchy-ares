#!/usr/bin/env python3
"""Room adapter using the official Hermes Desktop JSON-RPC API."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
from contextlib import closing
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_ROOM_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _exchange(payload: dict[str, Any], timeout: float = 4.0) -> dict[str, Any]:
    from hermes_desktop_rpc import exchange
    return exchange(payload, timeout=timeout)


def parse_room_member_ref(value: str) -> tuple[str, str]:
    """Return ``(host, profile)`` for one UI identity.

    Bare profile names remain a compatibility spelling for ``local/<profile>``.
    Routing identities are deliberately separate from room member ids so a UI
    never has to expose where an agent executes.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("room member identity is required")
    parts = raw.split("/")
    if len(parts) == 1:
        host, profile = "local", parts[0]
    elif len(parts) == 2:
        host, profile = parts
    else:
        raise ValueError("invalid room member identity")
    if not _ROOM_IDENTIFIER.fullmatch(host) or not _ROOM_IDENTIFIER.fullmatch(profile):
        raise ValueError("invalid room member identity")
    return host, profile


def _peer_agents(peer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    agents = peer.get("agents") or {}
    if isinstance(agents, list):
        return {str(row.get("profile") or ""): row for row in agents
                if isinstance(row, dict) and row.get("profile")}
    return agents if isinstance(agents, dict) else {}


def _room_member_base(peer: dict[str, Any], profile: str) -> str:
    base = str(peer.get("url") or "").rstrip("/")
    if not base:
        raise ValueError("peer room endpoint is unavailable")
    if profile != "default":
        base += "/p/" + urllib.parse.quote(profile, safe="")
    return base


def _room_route_base(peer: dict[str, Any], profile: str,
                     catalog: dict[str, Any]) -> str:
    endpoint = catalog.get("endpoint") if isinstance(catalog, dict) else None
    advertised = str(endpoint.get("url") or "") if isinstance(endpoint, dict) and endpoint.get("available") is True else ""
    return _room_member_base({"url": advertised} if advertised else peer, profile)


def _room_http_json(base: str, path: str, *, method: str, authorization: str,
                    body: dict[str, Any] | None = None) -> dict[str, Any]:
    encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + path, data=encoded, method=method,
        headers={"Authorization": authorization, "Content-Type": "application/json",
                 "User-Agent": "omarchy-hermes/1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise RuntimeError("peer room response exceeded 1 MiB")
    result = json.loads(raw.decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("peer room response is invalid")
    return result


def _invite_room_member(peer: dict[str, Any], profile: str,
                        payload: dict[str, Any]) -> dict[str, Any]:
    key = str(peer.get("key") or "")
    if len(key) < 16:
        raise ValueError("peer room credential is unavailable")
    return _room_http_json(
        _room_member_base(peer, profile), "/v1/room-members/invitations",
        method="POST", authorization="Bearer " + key, body=payload)


def _revoke_room_member(peer: dict[str, Any], profile: str, grant: str) -> None:
    _room_http_json(
        _room_member_base(peer, profile), "/v1/room-members/grants/revoke",
        method="POST", authorization="HermesRoom " + grant, body={})


def _provision_peer_bot(peer: dict[str, Any], profile: str,
                        payload: dict[str, Any]) -> dict[str, Any]:
    control = peer.get("control") if isinstance(peer.get("control"), dict) else {}
    base = str(control.get("url") or "").rstrip("/")
    token = str(control.get("token") or "")
    parsed = urllib.parse.urlparse(base)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or len(token) < 32:
        raise ValueError("peer bot provisioning control plane is unavailable")
    result = _room_http_json(base, "/v1/bots/provision", method="POST",
                             authorization="Bearer " + token, body=payload)
    status = str(result.get("status") or result.get("result", {}).get("status") or "")
    target = str(result.get("profile") or result.get("result", {}).get("profile") or "")
    if status not in {"created", "already_present"} or target != profile:
        raise RuntimeError("peer bot provisioning was not confirmed")
    return {"status": status, "profile": target}


def create_distributed_room(
    room_id: str, name: str, member_refs: list[str], *,
    local_bots: dict[str, dict[str, Any]], peers: dict[str, dict[str, Any]],
    rpc=None, invite=None, revoke=None, provision=None,
) -> dict[str, Any]:
    """Create one room whose members execute on their owning Hermes hosts.

    The room authority stays behind the official ``groups.*`` RPC. Remote turns
    use Hermes RoomLink grants, so profiles, memories and provider credentials
    are never copied between hosts. Setup is fail-closed and tears down a room
    if a route cannot be registered.
    """
    rpc = rpc or room_request
    invite = invite or _invite_room_member
    revoke = revoke or _revoke_room_member
    provision = provision or _provision_peer_bot
    if not _ROOM_IDENTIFIER.fullmatch(str(room_id or "")):
        raise ValueError("invalid room id")
    clean_name = str(name or "").strip()
    if not clean_name or not 2 <= len(member_refs) <= 6:
        raise ValueError("room requires a name and 2–6 members")

    # Validate every route before touching either gateway.
    resolved = []
    seen = set()
    for ref in member_refs:
        host, profile = parse_room_member_ref(ref)
        identity = host + "/" + profile
        if identity in seen:
            raise ValueError("room members must be unique")
        seen.add(identity)
        if host == "local":
            if profile not in local_bots:
                raise ValueError("local room profile is unavailable")
            resolved.append((host, profile, local_bots[profile], None, False))
        else:
            peer = peers.get(host)
            if not isinstance(peer, dict):
                raise ValueError("peer room host is unavailable")
            agent = _peer_agents(peer).get(profile)
            needs_provision = not isinstance(agent, dict)
            if needs_provision:
                source = local_bots.get(profile)
                if not isinstance(source, dict):
                    raise ValueError("peer room profile is unavailable")
                agent = source
            resolved.append((host, profile, agent, peer, needs_provision))

    capabilities = rpc("capabilities")
    authority = str(capabilities.get("authority_gateway_id") or "")
    if not authority or capabilities.get("driver") is not True:
        raise RuntimeError("Hermes room authority is unavailable")

    descriptors = []
    routes = []
    for host, profile, agent, peer, needs_provision in resolved:
        title = str(agent.get("title") or agent.get("display_name") or profile)
        if host == "local":
            descriptors.append({
                "member_id": profile, "profile": profile, "handle": profile,
                "display_name": title, "target": {"kind": "local", "profile": profile}})
            continue
        if needs_provision:
            identity_payload = {
                "name": profile,
                "description": str(agent.get("description") or ""),
                "model": str(agent.get("model") or ""),
                "provider": str(agent.get("provider") or ""),
            }
            provisioned = provision(peer, profile, identity_payload)
            if (not isinstance(provisioned, dict)
                    or provisioned.get("status") not in {"created", "already_present"}
                    or provisioned.get("profile") != profile):
                raise RuntimeError("peer bot provisioning was not confirmed")
        member_id = host + "--" + profile
        invitation_payload = {
            "room_id": room_id, "home_install_id": authority,
            "authority_gateway_id": authority, "authority_epoch": 1,
            "member_id": member_id, "status_ttl_seconds": 30 * 24 * 60 * 60}
        invitation = invite(peer, profile, invitation_payload)
        catalog = invitation.get("catalog") if isinstance(invitation, dict) else None
        grant = str(invitation.get("grant") or "") if isinstance(invitation, dict) else ""
        installation = str((catalog or {}).get("installation_id") or "")
        digest = str((catalog or {}).get("catalog_digest") or "")
        if not grant or not installation or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("peer RoomLink invitation is invalid")
        descriptors.append({
            "member_id": member_id, "profile": profile, "handle": profile + "-" + host,
            "display_name": title,
            "target": {"kind": "peer", "peer_id": host, "installation_id": installation,
                       "profile": profile, "capability_digest": digest}})
        routes.append((peer, profile, member_id, grant, catalog))

    created = False
    try:
        result = rpc("create", room_id=room_id, name=clean_name, members=descriptors)
        created = True
        for peer, profile, member_id, grant, catalog in routes:
            rpc("peer.register", room_id=room_id, member_id=member_id,
                target_url=_room_route_base(peer, profile, catalog), target_profile=profile,
                grant=grant, catalog=catalog)
        return result
    except Exception:
        for peer, profile, _member_id, grant, _catalog in routes:
            try:
                revoke(peer, profile, grant)
            except Exception:
                pass
        if created:
            try:
                rpc("disband", room_id=room_id, cancel_id="omarchy-setup-rollback")
            except Exception:
                pass
        raise


def _require_writable(room_id: str) -> None:
    if str(room_id).startswith("desktop::"):
        raise RuntimeError("Desktop Group Chat is read-only here; use Hermes Desktop to change it.")


def native_room_activity(room):
    """Observe existing Desktop work without attaching to its session transport."""
    local = native_status(room['room_id'])
    if local.get('working'):
        return local  # Its owning persistent connection already supplies events.
    from hermes_room_observer import native_observe
    observed = native_observe(room.get('desktopRoomId') or '', room.get('members') or [])
    if observed.get('status') == 'unavailable':
        return local
    result = {**observed, 'members': dict(observed.get('members') or {}),
              'memberActivities': dict(observed.get('memberActivities') or {})}
    for key, detail in (local.get('memberActivities') or {}).items():
        if (result['members'].get(key) == 'unavailable'
                and any(detail.get(field) for field in ('text', 'reasoning', 'thinking', 'tool'))):
            result['memberActivities'][key] = detail
            result['members'][key] = (local.get('members') or {}).get(key, 'unknown')
    return result


def room_request(action: str, *, timeout: float = 4.0, **params: Any) -> dict[str, Any]:
    if action not in ("state", "log"):
        _require_writable(params.get("room_id") or "")
    if str(params.get("room_id") or "").startswith("desktop::"):
        room = next((r for r in desktop_rooms(timeout) if r["room_id"] == params["room_id"]), None)
        if room is None:
            raise RuntimeError("Desktop snapshot room not found")
        messages = [m for m in room["messages"] if m["seq"] > int(params.get("since_seq") or 0)]
        limit = max(0, int(params.get("limit", 200)))
        messages = messages[-limit:] if limit else []
        return {"room": room, "messages": messages, "events": [],
                "source": "desktop", "readOnly": False, "historyPartial": True,
                "driver_status": native_room_activity(room)}
    response = _exchange({"verb": "rooms", "action": action, **params}, timeout)
    if response.get("ok") is not True:
        raise RuntimeError(str(response.get("error") or "Hermes room request failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Hermes room result is invalid")
    if action == "state":
        driver = dict(result.get("driver_status") or {})
        driver["memberActivities"] = member_activities(
            str(params.get("room_id") or ""), result.get("room") or {}, driver
        )
        result = {**result, "driver_status": driver}
    return result


def desktop_rooms(timeout: float = 4.0) -> list[dict[str, Any]]:
    """Read renderer snapshots, never hosted groups or turns.

    Writes retain 16 messages of 1200 chars; merged snapshots may have more.
    None of these snapshots establish a complete transcript or live presence.
    """
    if HERMES_HOME.resolve() != Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser().resolve():
        raise RuntimeError("Desktop snapshot home does not match transport home")
    response = _exchange({"verb": "profiles", "action": "list", "include_sessions": False}, timeout)
    if response.get("ok") is not True:
        raise RuntimeError(str(response.get("error") or "Desktop snapshot unavailable"))
    result_data = response.get("result")
    if not isinstance(result_data, dict) or not isinstance(result_data.get("profiles"), list):
        raise RuntimeError("Invalid Desktop snapshot profiles")
    profiles = result_data["profiles"]
    profile = next((p for p in profiles if isinstance(p, dict) and p.get("name") == "default"), {})
    metadata = profile.get("ui_meta") or {}
    if not isinstance(metadata, dict):
        raise RuntimeError("Invalid Desktop snapshot metadata")
    snapshot = metadata.get("hermes-bots-groups")
    if snapshot is None:
        return []
    if (not isinstance(snapshot, dict) or snapshot.get("version") != 3
            or not isinstance(snapshot.get("rooms"), dict)
            or not isinstance(snapshot.get("deleted", {}), dict)):
        raise RuntimeError("Invalid or unsupported Desktop snapshot")
    result = []
    for key, raw in snapshot["rooms"].items():
        if not isinstance(raw, dict) or not isinstance(raw.get("log"), list):
            continue
        deleted = snapshot.get("deleted", {})
        if key in deleted:
            try:
                if key.startswith("id:") or float(deleted[key] or 0) >= float(raw.get("revision") or 0):
                    continue
            except (TypeError, ValueError):
                continue  # Corrupt tombstones must not resurrect rooms.
        messages = []
        for index, event in enumerate(raw["log"]):
            if not isinstance(event, dict) or not isinstance(event.get("from"), dict):
                continue
            actor = event["from"]
            if actor.get("kind") not in ("member", "user"):
                continue
            messages.append({"seq": index + 1, "id": event.get("id") or "",
                             "role": "assistant" if actor.get("kind") == "member" else "user",
                             "author": actor.get("name") if actor.get("kind") == "member" else "user",
                             "text": str(event.get("text") or ""), "time": _event_time(event),
                             "source": actor.get("source") or "", "thread": event.get("thread") or ""})
        raw_members = raw.get("members") if isinstance(raw.get("members"), list) else []
        members = [{**m, "profile": m.get("name") or "", "member_id": _native_member_key(m),
                    "display_name": m.get("name") or ""} for m in raw_members if isinstance(m, dict)]
        result.append({"room_id": "desktop::" + key, "desktopKey": key,
                       "desktopRoomId": raw.get("roomId") or "", "name": raw.get("name") or key,
                       "revision": raw.get("revision") or 0, "members": members, "messages": messages,
                       "source": "desktop", "readOnly": False, "historyPartial": True})
    return result


_NATIVE_KEY = "hermes-bots-groups"


def _native_snapshot():
    if HERMES_HOME.resolve() != Path(os.environ.get('HERMES_HOME') or '~/.hermes').expanduser().resolve():
        raise RuntimeError('Desktop snapshot home does not match transport home')
    response = _exchange({'verb': 'profiles', 'action': 'list', 'include_sessions': False}, 15)
    if response.get('ok') is not True:
        raise RuntimeError('Desktop profiles unavailable')
    profiles = (response.get('result') or {}).get('profiles') or []
    profile = next((p for p in profiles if p.get('name') == 'default'), {})
    snapshot = (profile.get('ui_meta') or {}).get(_NATIVE_KEY)
    revisions = profile.get('ui_meta_revisions')
    if (not isinstance(snapshot, dict) or snapshot.get('version') != 3
            or not isinstance(snapshot.get('rooms'), dict) or not isinstance(snapshot.get('deleted', {}), dict)):
        raise RuntimeError('Invalid or unsupported Desktop snapshot')
    if not isinstance(revisions, dict) or type(revisions.get(_NATIVE_KEY, 0)) is not int:
        raise RuntimeError('Desktop metadata CAS unavailable; update Hermes Desktop')
    return snapshot, revisions.get(_NATIVE_KEY, 0)


def _native_room(snapshot, room_id):
    if not room_id.startswith('desktop::'):
        raise ValueError('Native room identity required')
    key = room_id[len('desktop::'):]
    raw = snapshot['rooms'].get(key)
    if not isinstance(raw, dict) or not isinstance(raw.get('log'), list):
        raise RuntimeError('Desktop room not found')
    deleted = snapshot.get('deleted', {})
    if key in deleted and (key.startswith('id:') or float(deleted[key] or 0) >= float(raw.get('revision') or 0)):
        raise RuntimeError('Desktop room deleted')
    return raw


def native_append(room_id, event):
    """Append a real event via renderer CAS contract, preserving other rooms.

    Only explicit CAS conflicts are retried. Ambiguous transport outcomes are
    never replayed. Refuse oversized snapshots rather than dropping other rooms.
    """
    for _ in range(5):
        snapshot, revision = _native_snapshot()
        raw = _native_room(snapshot, room_id)
        if any(e.get('id') == event['id'] for e in raw['log']):
            return raw
        raw['log'].append(dict(event))
        raw['log'].sort(key=lambda e: (e.get('at', 0), e.get('id', '')))
        raw['revision'] = revision + 1
        snapshot['updatedAt'] = int(time.time() * 1000)
        # Same per-event limits as Hve in the packaged renderer. Keep existing
        # messages untouched and fail rather than evict another room's history.
        if len(json.dumps({_NATIVE_KEY: snapshot})) > 64000:
            raise RuntimeError('Desktop snapshot full; message was not persisted')
        response = _exchange({'verb': 'profiles', 'action': 'configure', 'name': 'default',
                              'ui_meta': {_NATIVE_KEY: snapshot},
                              'ui_meta_expected_revisions': {_NATIVE_KEY: revision}}, 15)
        if response.get('ok') is not True:
            raise RuntimeError('Desktop metadata write outcome unknown; not retried')
        applied = (response.get('result') or {}).get('applied') or {}
        if applied.get('ui_meta_conflicts'):
            continue
        if applied.get('ui_meta') is not True or (applied.get('ui_meta_revisions') or {}).get(_NATIVE_KEY) != revision + 1:
            raise RuntimeError('Desktop metadata write rejected')
        actual, current_revision = _native_snapshot()
        verified = _native_room(actual, room_id)
        if current_revision < revision + 1 or not any(e.get('id') == event['id'] and e.get('text') == event['text'] for e in verified['log']):
            raise RuntimeError('Desktop message read-back failed; not retried')
        return verified
    raise RuntimeError('Desktop metadata conflict; message was not persisted')


def _native_member_key(member):
    return ((str(member.get('connectionId') or '') + '::') if member.get('sourceScoped') or member.get('remoteSource') else '') + str(member.get('name') or '')


def _native_members(raw):
    members = raw.get('members') or []
    if not 2 <= len(members) <= 6:
        raise RuntimeError('Native room requires 2–6 routable identities')
    keys = set()
    for member in members:
        if not isinstance(member, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(member.get('name') or '')):
            raise RuntimeError('Invalid native member identity')
        scoped = member.get('sourceScoped') or member.get('remoteSource') or member.get('connectionId')
        if scoped and (member.get('connectionId') != 'local' or member.get('connectionKind', 'local') != 'local'):
            raise RuntimeError('Native member connection route unavailable; no local fallback')
        key = _native_member_key(member)
        if key in keys:
            raise RuntimeError('Duplicate native member identity')
        keys.add(key)
    return members


def _native_prompt(raw, member, delta):
    others = ', '.join('@' + m['name'] for m in raw['members'] if m != member)
    lines = []
    for event in delta[-24:]:
        actor = event.get('from') or {}
        who = actor.get('name') or 'You'
        if actor.get('kind') == 'user':
            who += ' (user)'
        elif who == member['name']:
            who += ' (you)'
        if actor.get('source'):
            who += ' [' + actor['source'] + ']'
        lines.append('  ' + who + ': ' + str(event.get('text') or ''))
    return '\n'.join([
        f'[Group chat: "{raw["name"]}"] You are @{member["name"]}, one participant in a group chat with {others or "no one else yet"} and the user.',
        '', 'New messages in the room since your last turn (oldest first):', *lines, '',
        'Rules for this room:',
        '- Reply with ONE conversational message ONLY if you have something new worth adding: build on what was just said, claim or hand off work, answer a question aimed at you, or report a real result. Keep chatter short (1-3 sentences) — but when you are delivering a result, an answer the user asked for, or substantive work, give it at full quality and length; never thin out real content to fit the room.',
        '- If you have nothing new to add, reply with exactly "(pass)". Passing is good — it lets the conversation settle.',
        '- Mention a teammate as @name to pull them in; mention @user only for a judgment call or a result the user needs. Do not repeat points already made.',
        '- Never reveal content from your private 1:1 chats. Your reply text goes to the room verbatim — no preamble, no meta-commentary.'
    ])


def _native_turn(raw, member, prompt, signal, room_id, thread):
    response = _exchange({'verb': 'native', 'action': 'turn', 'profile': member['name'],
                          'title': 'Group: ' + (raw.get('roomId') or raw['name']), 'text': prompt,
                          'telemetry_path': str(_native_state_path(room_id).with_suffix('.live.json')),
                          'member_key': _native_member_key(member), 'thread': thread}, 1240)
    if response.get('ok') is not True:
        raise RuntimeError('Native Desktop turn failed or timed out; no automatic resend')
    result = response.get('result') or {}
    if isinstance(result.get('detail'), dict):
        signal('idle', result.get('session_id') or '', result['detail'])
    return str(result.get('text') or '')

def run_native_room(room_id, thread, progress=None):
    """Renderer-style bounded rotating rounds; only real completed replies sync.

    Watermarks are per thread/member, passing settles, maximum three rounds and
    ten public replies. Session state, not canonical Bot Chat state, drives activity.
    """
    raw = _native_room(_native_snapshot()[0], room_id)
    members = _native_members(raw)
    status = {'status': 'running', 'working': True, 'members': {_native_member_key(m): 'queued' for m in members}, 'memberActivities': {}}
    emit = progress or (lambda state: None)
    watermarks = {}
    replies = 0
    for round_index in range(3):
        added = 0
        raw = _native_room(_native_snapshot()[0], room_id)
        thread_log = [e for e in raw['log'] if e.get('thread') == thread]
        last_user = next((i for i in range(len(thread_log) - 1, -1, -1) if thread_log[i].get('from', {}).get('kind') == 'user'), 0)
        mentions = set(re.findall(r'@([a-z0-9][a-z0-9._-]*)', '\n'.join(str(e.get('text') or '') for e in thread_log[last_user:]), re.I))
        mentions = {m.lower() for m in mentions}
        selected = [m for m in members if m['name'].lower() in mentions or str(m.get('handle') or '').lower() in mentions]
        targets = members if mentions.intersection({'all', 'everyone'}) or not selected else selected
        ordered = targets[round_index % len(targets):] + targets[:round_index % len(targets)]
        for member in ordered:
            raw = _native_room(_native_snapshot()[0], room_id)
            log = [e for e in raw['log'] if e.get('thread') == thread]
            name = _native_member_key(member)
            delta = log[watermarks.get(name, 0):]
            if not delta:
                continue
            def signal(state, sid='', detail=None):
                status['members'][name] = state
                if detail is not None:
                    previous = status['memberActivities'].get(name) or {}
                    is_pass = re.fullmatch(r'\(?\s*pass\s*\)?\.?', str(detail.get('text') or '').strip(), re.I)
                    if is_pass and previous.get('text'):
                        detail = {**previous, 'state': 'idle', 'lastPassAt': detail.get('updatedAt', 0)}
                    status['memberActivities'][name] = detail
                else:
                    status['memberActivities'].setdefault(name, {'status': 'unavailable', 'activity': '', 'updatedAt': 0, 'sessionId': sid})
                emit(status)
            signal('working')
            reply = _native_turn(raw, member, _native_prompt(raw, member, delta), signal, room_id, thread)
            watermarks[name] = len(log)
            if reply and not re.fullmatch(r'\(?\s*pass\s*\)?\.?', reply, re.I):
                event = {'id': 'omarchy-' + uuid.uuid4().hex, 'at': max(int(time.time() * 1000), int(raw['log'][-1].get('at') or 0) + 1 if raw['log'] else 0),
                         'from': {'kind': 'member', 'name': member['name']}, 'text': reply, 'thread': thread}
                updated = native_append(room_id, event)
                watermarks[name] = len([e for e in updated['log'] if e.get('thread') == thread])
                added += 1
                replies += 1
            status['members'][name] = 'idle'
            emit(status)
            if replies >= 10:
                break
        if not added or replies >= 10:
            break
    status.update(status='capped' if added else 'settled', working=False)
    emit(status)
    return status


def _native_state_path(room_id):
    import hashlib
    key = hashlib.sha256((str(HERMES_HOME.resolve()) + '\0' + room_id).encode()).hexdigest()
    return Path(os.environ.get('XDG_STATE_HOME') or '~/.local/state').expanduser() / 'omarchy-hermes' / 'native-rooms' / (key + '.json')


def _native_save_status(room_id, state):
    import tempfile
    path = _native_state_path(room_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.native-')
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({**state, 'room_id': room_id, 'pid': os.getpid(), 'updatedAt': time.time()}, handle)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def native_status(room_id):
    try:
        state = json.loads(_native_state_path(room_id).read_text())
        if state.get('room_id') != room_id:
            raise ValueError
        if state.get('working'):
            try:
                os.kill(int(state['pid']), 0)
            except (OSError, ValueError, KeyError):
                state.update(status='interrupted', working=False, error='Plugin worker stopped; last turn may still be running in Desktop')
                state['members'] = {k: 'unknown' for k in state.get('members', {})}
        try:
            detail = json.loads(_native_state_path(room_id).with_suffix('.live.json').read_text())
            key = detail.get('memberKey')
            if detail.get('thread') == state.get('thread') and key in state.get('members', {}):
                if state.get('working') or key not in state.get('memberActivities', {}):
                    state.setdefault('memberActivities', {})[key] = detail
                if state.get('working') and detail.get('state') in ('working', 'waiting', 'idle', 'error'):
                    state['members'][key] = detail['state']
        except (OSError, ValueError, TypeError):
            pass
        return state
    except (OSError, ValueError, TypeError):
        return {'status': 'unavailable', 'members': {}, 'memberActivities': {}}


def native_worker(room_id, thread):
    state = {'status': 'queued', 'working': True, 'thread': thread, 'members': {}, 'memberActivities': {}}
    def progress(update):
        state.update(update)
        _native_save_status(room_id, state)
    progress(state)
    try:
        run_native_room(room_id, thread, progress)
    except Exception:
        state.update(status='failed', working=False, error='Native orchestration stopped; inspect the group sessions in Desktop. No automatic resend.')
        state['members'] = {k: 'unknown' if v in ('working', 'waiting') else v for k, v in state.get('members', {}).items()}
        progress(state)


def _launch_native(room_id, thread, lock_fd):
    import sys
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--native-worker', room_id, thread],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, pass_fds=(lock_fd,))
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = native_status(room_id)
        if status.get('pid') == child.pid and status.get('thread') == thread:
            return status
        if child.poll() is not None:
            break
        time.sleep(0.05)
    raise RuntimeError('User message persisted; native worker start unconfirmed. Do not resend automatically.')


def _safe_activity(text: str) -> str:
    """Scrub credential-shaped text before applying the display budget."""
    text = re.sub(r"https?://\S+", "[redacted]", text, flags=re.I)
    text = re.sub(
        r'''(?i)\b(?:[\w-]*(?:api[_-]?key|token|secret|password)|authorization|cookie)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
        "[redacted]", text,
    )
    text = re.sub(
        r"(?i)\b(?:Bearer|Basic)\s+\S+|\b(?:sk-|gh[pousr]_|github_pat_|AKIA|AIza|xox[baprs]-)[A-Za-z0-9_-]+|\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2}",
        "[redacted]", text,
    )
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    return " ".join(text.split())[:240]


def member_activities(room_id: str, room: dict, driver: dict) -> dict:
    """Activity is scoped to the driver's member keys, never Bot Chat presence.

    Installed hosted_room_driver resolves local sessions by exact
    ``Group: <room_id>`` title. Require its bot_room source as extra evidence.
    Keys exactly mirror driver.members. ``status`` describes observation quality
    (available/stale/unavailable), not presence; presence stays in driver.members.
    updatedAt is epoch seconds, zero when unavailable. Only working members with
    an open, fresh (<=180s) exact session expose activity (<=240 chars) and an
    optional validated sessionId. No profile-wide/latest-session heuristic.

    Missing title stamps, remote transports, ambiguous titles, old schemas, and
    ended/compressed roots without a proven live association fail closed. In
    particular a new in-memory hosted session can exist before its pending title
    or first activity heartbeat reaches SQLite. Reads must not create/resume it.
    """
    activities = {}
    for key in (driver.get("members") or {}):
        activities[key] = {"activity": "", "updatedAt": 0, "status": "unavailable"}
        if driver["members"][key] != "working":
            continue
        matches = [m for m in room.get("members", []) if isinstance(m, dict)
                   and (m.get("member_id") or m.get("profile")) == key]
        if len(matches) != 1:
            continue
        member = matches[0]
        target = member.get("target") or {}
        profile = member.get("profile")
        if (not isinstance(target, dict) or target.get("kind", "local") != "local"
                or not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile)):
            continue
        path = HERMES_HOME / "state.db" if profile == "default" else HERMES_HOME / "profiles" / profile / "state.db"
        try:
            with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
                rows = db.execute(
                    "SELECT substr(id, 1, 129), substr(last_activity_description, 1, 4097), "
                    "last_activity_at, ended_at FROM sessions "
                    "WHERE title = ? AND source = ? LIMIT 2", (f"Group: {room_id}", "bot_room")
                ).fetchall()
        except (sqlite3.Error, OSError, ValueError):
            continue
        if len(rows) != 1:
            continue
        row = rows[0]
        try:
            updated = float(row[2] or 0)
        except (ValueError, TypeError):
            continue
        if (row[3] is not None or not isinstance(row[1], str) or len(row[1]) > 4096
                or not _safe_activity(row[1]) or "completed" in row[1].lower()
                or not math.isfinite(updated) or updated <= 0 or updated > time.time() + 5):
            continue
        if time.time() - updated > 180:
            activities[key] = {"activity": "", "updatedAt": updated, "status": "stale"}
            continue
        activities[key] = {"activity": _safe_activity(row[1]), "updatedAt": updated, "status": "available"}
        if isinstance(row[0], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row[0]) and _safe_activity(row[0]) == row[0]:
            activities[key]["sessionId"] = row[0]
    return activities


def member_descriptors(names: list[str], bots: dict[str, dict[str, Any]] | None = None) -> list[dict[str, str]]:
    bots = bots or {}
    members: list[dict[str, str]] = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        title = str((bots.get(name) or {}).get("title") or name)
        members.append({
            "member_id": name,
            "profile": name,
            "handle": name,
            "display_name": title,
        })
    return members


def _event_time(event: dict[str, Any]) -> float:
    for key in ("created_at", "at", "timestamp"):
        try:
            value = float(event.get(key) or 0)
            if value > 10_000_000_000:
                value /= 1000.0
            if value:
                return value
        except (TypeError, ValueError):
            pass
    return 0.0


def messages_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in events or []:
        kind = str(event.get("kind") or "")
        if kind not in {"message.user", "message.member"}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
        if kind == "message.user":
            role, author = "user", "user"
        else:
            role = "assistant"
            author = str(
                actor.get("id") or actor.get("member_id") or actor.get("profile")
                or actor.get("handle") or "bot"
            )
        seq = int(event.get("seq") or 0)
        event_id = str(event.get("event_id") or event.get("id") or "")
        message = {
            "seq": seq,
            "id": event_id,
            "interventionId": event_id or str(seq),
            "revision": seq,
            "thread": str(payload.get("thread_id") or payload.get("thread") or ""),
            "status": "completed",
            "role": role,
            "author": author,
            "text": text,
            "time": _event_time(event),
        }
        if payload.get("image_path"):
            message["image"] = str(payload["image_path"])
        messages.append(message)
    return messages


def create_room(room_id: str, name: str, members: list[dict[str, str]]) -> dict[str, Any]:
    return room_request("create", room_id=room_id, name=name, members=members)


def _remove_member_capability_error(capabilities: dict[str, Any], *, remote: bool) -> str | None:
    """Require an explicit upstream advertisement; never infer ``groups.members``."""
    members = capabilities.get("members") if isinstance(capabilities, dict) else None
    if not isinstance(members, dict) or not all(members.get(flag) is True
                                                for flag in ("remove", "expected_revision", "idempotency")):
        return "removeMember CAS is not advertised"
    if remote:
        peer_revoke = capabilities.get("peer_revoke")
        if not isinstance(peer_revoke, dict) or not all(peer_revoke.get(flag) is True
                                                        for flag in ("remove", "idempotency")):
            return "distributed peer revoke is not advertised"
    return None


def _removal_member(room: dict[str, Any], member_id: str) -> dict[str, Any] | None:
    members = [member for member in room.get("members", []) if isinstance(member, dict)]
    exact = [member for member in members if str(member.get("member_id") or "") == member_id]
    if exact:
        return exact[0]
    # Local hosted descriptors historically use profile as their member id. Only
    # accept that compatibility spelling when it cannot select two routes.
    profile = [member for member in members if str(member.get("profile") or "") == member_id]
    return profile[0] if len(profile) == 1 else None


def _removal_result(room_id: str, member_id: str, revision: int, key: str, *, idempotent: bool = False) -> dict[str, Any]:
    result = {"ok": True, "status": "removed", "roomId": room_id, "memberId": member_id,
              "revision": revision, "idempotencyKey": key}
    if idempotent:
        result["idempotent"] = True
    return result


def remove_room_member(room_id: str, member_id: str, *, expected_revision: int,
                       idempotency_key: str, rpc=None) -> dict[str, Any]:
    """Request/reconcile one legacy hosted-room removal through advertised RPCs.

    The returned ``removing`` receipt is intentionally serializable: callers can
    pass it to :func:`reconcile_remove_member` after a peer reconnect or process
    restart. The adapter stores no mutation journal and never retries an
    operation unless the caller presents its same idempotency key.
    """
    room_id, member_id, key = str(room_id or ""), str(member_id or ""), str(idempotency_key or "")
    if room_id.startswith("desktop::"):
        return {"ok": False, "status": "unsupported", "roomId": room_id, "memberId": member_id,
                "error": "Desktop member removal is unsupported"}
    if not _ROOM_IDENTIFIER.fullmatch(room_id) or not _ROOM_IDENTIFIER.fullmatch(member_id) or not key:
        raise ValueError("room, member, and idempotency key are required")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise ValueError("expected revision is required")
    rpc = rpc or room_request
    capabilities = rpc("capabilities")
    capability_error = _remove_member_capability_error(capabilities, remote=False)
    if capability_error:
        return {"ok": False, "status": "unsupported", "roomId": room_id, "memberId": member_id,
                "error": capability_error}
    state = rpc("state", room_id=room_id)
    room = state.get("room") if isinstance(state, dict) else None
    if not isinstance(room, dict):
        return {"ok": False, "status": "error", "roomId": room_id, "memberId": member_id,
                "error": "room state is invalid"}
    try:
        revision = int(room.get("revision"))
    except (TypeError, ValueError):
        return {"ok": False, "status": "error", "roomId": room_id, "memberId": member_id,
                "error": "room revision is unavailable"}
    member = _removal_member(room, member_id)
    if member is None:
        return _removal_result(room_id, member_id, revision, key, idempotent=True)
    if revision != expected_revision:
        return {"ok": False, "status": "conflict", "roomId": room_id, "memberId": member_id,
                "expectedRevision": expected_revision, "revision": revision}
    remote = isinstance(member.get("target"), dict) and member["target"].get("kind") == "peer"
    capability_error = _remove_member_capability_error(capabilities, remote=remote)
    if capability_error:
        return {"ok": False, "status": "unsupported", "roomId": room_id, "memberId": member_id,
                "error": capability_error}
    if remote:
        try:
            rpc("peer.revoke", room_id=room_id, member_id=str(member.get("member_id") or member_id), event_id=key)
        except Exception as exc:
            return {"ok": False, "status": "removing", "retryable": True, "roomId": room_id,
                    "memberId": member_id, "expectedRevision": expected_revision,
                    "idempotencyKey": key, "error": str(exc)[:240]}
    members = [item for item in room.get("members", []) if isinstance(item, dict) and item is not member]
    try:
        rpc("members", room_id=room_id, members=members, expected_revision=expected_revision, event_id=key)
    except Exception as exc:
        return {"ok": False, "status": "removing", "retryable": True, "roomId": room_id,
                "memberId": member_id, "expectedRevision": expected_revision,
                "idempotencyKey": key, "error": str(exc)[:240]}
    readback = rpc("state", room_id=room_id)
    observed = readback.get("room") if isinstance(readback, dict) else None
    if not isinstance(observed, dict):
        return {"ok": False, "status": "error", "roomId": room_id, "memberId": member_id,
                "error": "room removal readback is invalid"}
    try:
        observed_revision = int(observed.get("revision"))
    except (TypeError, ValueError):
        return {"ok": False, "status": "error", "roomId": room_id, "memberId": member_id,
                "error": "room removal revision is unavailable"}
    if _removal_member(observed, member_id) is not None:
        return {"ok": False, "status": "removing", "retryable": True, "roomId": room_id,
                "memberId": member_id, "expectedRevision": expected_revision,
                "idempotencyKey": key, "error": "room removal is not yet visible"}
    return _removal_result(room_id, member_id, observed_revision, key)


def reconcile_remove_member(event: dict[str, Any], *, rpc=None) -> dict[str, Any]:
    """Resume a serializable ``removeMember`` receipt without a local cache."""
    if not isinstance(event, dict) or event.get("status") not in {"removing", "removed"}:
        raise ValueError("removeMember event is required")
    if event.get("status") == "removed":
        return dict(event)
    return remove_room_member(str(event.get("roomId") or ""), str(event.get("memberId") or ""),
                              expected_revision=event.get("expectedRevision"),
                              idempotency_key=str(event.get("idempotencyKey") or ""), rpc=rpc)


def add_room_member(room_id: str, member: dict[str, str]) -> dict[str, Any]:
    _require_writable(room_id)
    state = room_request("state", room_id=room_id)
    current = list((state.get("room") or {}).get("members") or [])
    profile = str(member.get("profile") or member.get("member_id") or "").strip()
    if not profile:
        raise ValueError("member profile is required")
    if any(str(item.get("profile") or item.get("member_id") or "") == profile
           for item in current if isinstance(item, dict)):
        return {"room": state.get("room"), "unchanged": True}
    current.append(dict(member))
    return room_request(
        "members",
        room_id=room_id,
        event_id=f"omarchy-members-{uuid.uuid4().hex}",
        members=current,
    )


def stage_image(path: str) -> str:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in _IMAGE_EXTENSIONS:
        raise ValueError("unsupported or missing image")
    target_dir = HERMES_HOME / "images" / "omarchy"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / f"image-{uuid.uuid4().hex}{source.suffix.lower()}"
    shutil.copyfile(source, target)
    target.chmod(0o600)
    return str(target)


def save_clipboard_image() -> str:
    listed = subprocess.run(
        ["wl-paste", "--list-types"], capture_output=True, text=True, timeout=4
    )
    available = set(listed.stdout.splitlines())
    choices = (
        ("image/png", ".png"), ("image/jpeg", ".jpg"),
        ("image/webp", ".webp"), ("image/gif", ".gif"),
        ("image/bmp", ".bmp"),
    )
    mime, suffix = next(((mime, suffix) for mime, suffix in choices if mime in available), ("", ""))
    if not mime:
        raise ValueError("No hay una imagen en el portapapeles")
    copied = subprocess.run(
        ["wl-paste", "--no-newline", "--type", mime], capture_output=True, timeout=8
    )
    if copied.returncode != 0 or not copied.stdout:
        raise RuntimeError("No se pudo leer la imagen del portapapeles")
    target_dir = HERMES_HOME / "images" / "omarchy"
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = target_dir / f"clipboard-{uuid.uuid4().hex}{suffix}"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(copied.stdout)
        handle.flush()
        os.fsync(handle.fileno())
    return str(target)


def dispatch_native_room(room_id: str, thread: str) -> dict[str, Any]:
    """Launch orchestration only after an external CAS write is readable."""
    if not str(room_id).startswith('desktop::') or not re.fullmatch(r'omarchy-[a-f0-9]{32}|[A-Za-z0-9_-]{1,128}', str(thread or '')):
        raise ValueError('Native room and thread identity required')
    import fcntl
    path = _native_state_path(room_id).with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Native plugin room worker already active; persisted message not dispatched') from None
        raw = _native_room(_native_snapshot()[0], room_id)
        _native_members(raw)
        if not any(event.get('thread') == thread and event.get('from', {}).get('kind') == 'user'
                   for event in raw['log'] if isinstance(event, dict)):
            raise RuntimeError('Native user message is not persisted; worker not launched')
        return _launch_native(room_id, thread, lock_fd=fd)
    finally:
        os.close(fd)


def send_room_message(room_id: str, text: str, image_path: str | None = None) -> dict[str, Any]:
    if str(room_id).startswith('desktop::'):
        if image_path:
            raise RuntimeError('Native room images are not supported here; no message sent')
        if not str(text or '').strip():
            raise ValueError('room message is required')
        import fcntl
        path = _native_state_path(room_id).with_suffix('.lock')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Native plugin room worker already active; message not sent') from None
            raw = _native_room(_native_snapshot()[0], room_id)
            _native_members(raw)
            thread = 'omarchy-' + uuid.uuid4().hex
            event = {'id': 'omarchy-' + uuid.uuid4().hex, 'at': int(time.time() * 1000),
                     'from': {'kind': 'user', 'name': 'You'}, 'text': text, 'thread': thread}
            native_append(room_id, event)
            status = _launch_native(room_id, thread, lock_fd=fd)
        finally:
            os.close(fd)  # Child inherits the open lock, held for the entire run.
        return {'accepted': True, 'room_id': room_id, 'event_id': event['id'], 'thread': thread,
                'source': 'desktop', 'driver_status': status}
    _require_writable(room_id)
    token = uuid.uuid4().hex
    payload = {"text": text, "thread_id": f"omarchy-{token}"}
    if image_path:
        payload["image_path"] = stage_image(image_path)
    return room_request(
        "send",
        room_id=room_id,
        event_id=f"omarchy-{token}",
        payload=payload,
    )


def rename_room(room_id: str, name: str) -> dict[str, Any]:
    _require_writable(room_id)
    name = str(name or "").strip()
    if not name:
        raise ValueError("room name is required")
    return room_request(
        "rename",
        room_id=room_id,
        event_id=f"omarchy-rename-{uuid.uuid4().hex}",
        name=name,
    )


def room_log(room_id: str, since_seq: int = 0, limit: int = 200) -> dict[str, Any]:
    result = room_request("log", room_id=room_id, since_seq=since_seq, limit=limit)
    if result.get("source") == "desktop":
        return result
    result["messages"] = messages_from_events(result.get("events") or [])
    return result


def new_room_id(name: str) -> str:
    slug = "-".join(part for part in "".join(
        ch.lower() if ch.isalnum() else " " for ch in name
    ).split() if part)[:64] or "room"
    return f"{slug}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == '--native-worker':
        native_worker(sys.argv[2], sys.argv[3])
