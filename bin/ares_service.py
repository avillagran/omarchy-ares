#!/usr/bin/env python3
"""Ares's transactional project, room, event, and artifact store."""

from __future__ import annotations

import contextlib
import base64
import hashlib
import http.server
import json
import mimetypes
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

STATE_ROOT = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omarchy" / "ares"
DB_PATH = STATE_ROOT / "ares.db"
ARTIFACT_ROOT = STATE_ROOT / "artifacts"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
TRANSCRIPT_PAGE_EVENTS = 40


def now() -> float:
    return time.time()


def ident(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def require_text(value: str, label: str, maximum: int = 256) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    if len(text) > maximum:
        raise ValueError(f"{label} is too long")
    return text


def _profile_model_pair(profile_dir: Path) -> tuple[str, str] | None:
    config = profile_dir / "config.yaml"
    if not config.is_file():
        return None
    try:
        lines = config.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_model = False
    model, provider = "", ""
    for line in lines:
        if line and not line.startswith((" ", "\t")):
            in_model = line.split(":", 1)[0].strip() == "model"
            continue
        if not in_model:
            continue
        stripped = line.strip()
        if stripped.startswith("default:"):
            model = stripped.split(":", 1)[1].strip().strip("'\"")
        elif stripped.startswith("provider:"):
            provider = stripped.split(":", 1)[1].strip().strip("'\"")
    if model:
        return (provider, model)
    return None


def _write_profile_model(profile: str, model: str, provider: str) -> None:
    """Surgically update model.default/provider in a profile config.yaml.

    Line-based editing keeps comments and key order intact (a YAML round-trip
    would rewrite and strip them). Creates the block at the top when missing.
    """
    config = HERMES_HOME / "profiles" / profile / "config.yaml"
    if not config.is_file():
        return
    try:
        lines = config.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    def quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    model_idx = next(
        (i for i, line in enumerate(lines)
         if line and not line.startswith((" ", "\t")) and line.split(":", 1)[0].strip() == "model"),
        -1,
    )
    if model_idx < 0:
        lines[0:0] = ["model:", f"  default: {quoted(model)}", f"  provider: {quoted(provider)}", ""]
    else:
        block_end = next(
            (i for i in range(model_idx + 1, len(lines)) if lines[i] and not lines[i].startswith((" ", "\t"))),
            len(lines),
        )
        default_idx = next(
            (i for i in range(model_idx + 1, block_end) if lines[i].strip().startswith("default:")),
            -1,
        )
        provider_idx = next(
            (i for i in range(model_idx + 1, block_end) if lines[i].strip().startswith("provider:")),
            -1,
        )
        if default_idx >= 0:
            lines[default_idx] = f"  default: {quoted(model)}"
        else:
            lines.insert(model_idx + 1, f"  default: {quoted(model)}")
        if provider_idx >= 0:
            lines[provider_idx] = f"  provider: {quoted(provider)}"
        else:
            lines.insert(model_idx + 2, f"  provider: {quoted(provider)}")
    try:
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return


def _json_read(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _json_write(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _bot_sidecar_path(profile: str) -> Path:
    return HERMES_HOME / "profiles" / profile / "ares.json"


def _peers_sidecar_path() -> Path:
    return HERMES_HOME / "ares-peers.json"


def models_catalog(store: "AresStore") -> list[dict[str, str]]:
    """Known (provider, model) pairs: every local profile config plus the bots table."""
    pairs: set[tuple[str, str]] = set()
    profiles_dir = HERMES_HOME / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir()):
            if child.is_dir():
                pair = _profile_model_pair(child)
                if pair:
                    pairs.add(pair)
    try:
        rows = store._db.execute(
            "SELECT DISTINCT provider,model FROM bots WHERE model!=''").fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        pairs.add((str(row["provider"] or ""), str(row["model"] or "")))
    try:
        rows = store._db.execute("SELECT provider,model FROM known_models").fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        pairs.add((str(row["provider"] or ""), str(row["model"] or "")))
    return [
        {"provider": provider, "model": model}
        for provider, model in sorted(pairs, key=lambda item: (item[0], item[1]))
    ]


class AresStore:
    """SQLite WAL authority shared by desktop, web, and execution workers."""

    def __init__(self, path: Path = DB_PATH, artifact_root: Path = ARTIFACT_ROOT):
        self.path = Path(path)
        self.artifact_root = Path(artifact_root)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        os.chmod(self.artifact_root, 0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        self._recover_interrupted()
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_groups(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects(
              id TEXT PRIMARY KEY, group_id TEXT REFERENCES project_groups(id), name TEXT NOT NULL,
              node_id TEXT NOT NULL, path TEXT NOT NULL, location_key TEXT NOT NULL UNIQUE,
              repo_identity TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms(
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
              status TEXT NOT NULL, memory_release TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
              source TEXT NOT NULL DEFAULT 'ares', external_session_id TEXT NOT NULL DEFAULT '',
              external_profile TEXT NOT NULL DEFAULT '', external_updated_at REAL,
              deleted_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS room_members(
              room_id TEXT NOT NULL REFERENCES rooms(id), bot_id TEXT NOT NULL, position INTEGER NOT NULL,
              PRIMARY KEY(room_id,bot_id)
            );
            CREATE TABLE IF NOT EXISTS executions(
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), room_id TEXT NOT NULL REFERENCES rooms(id),
              bot_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, final_text TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, started_at REAL, finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS session_bindings(
              room_id TEXT NOT NULL REFERENCES rooms(id), bot_id TEXT NOT NULL, node_id TEXT NOT NULL,
              stored_session_id TEXT NOT NULL, runtime_session_id TEXT NOT NULL DEFAULT '',
              lifecycle_state TEXT NOT NULL DEFAULT 'resident', updated_at REAL NOT NULL,
              PRIMARY KEY(room_id,bot_id,node_id)
            );
            CREATE TABLE IF NOT EXISTS room_events(
              cursor INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
              project_id TEXT NOT NULL REFERENCES projects(id), room_id TEXT NOT NULL REFERENCES rooms(id),
              execution_id TEXT REFERENCES executions(id), sequence INTEGER NOT NULL, kind TEXT NOT NULL,
              author TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
              UNIQUE(room_id,sequence)
            );
            CREATE TABLE IF NOT EXISTS artifacts(
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), room_id TEXT NOT NULL REFERENCES rooms(id),
              execution_id TEXT REFERENCES executions(id), event_id TEXT REFERENCES room_events(id), author TEXT NOT NULL,
              name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
              path TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_artifacts(
              execution_id TEXT NOT NULL REFERENCES executions(id), artifact_id TEXT NOT NULL REFERENCES artifacts(id),
              delivery_state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
              PRIMARY KEY(execution_id,artifact_id)
            );
            CREATE TABLE IF NOT EXISTS command_receipts(
              scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
              response TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(scope,idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS bots(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL,
              node_id TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
              face_shape TEXT NOT NULL DEFAULT 'diamond', color_role TEXT NOT NULL DEFAULT 'accent', enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS peers(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, endpoint TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
              capabilities TEXT NOT NULL DEFAULT '{}', last_seen_at REAL, updated_at REAL NOT NULL,
              source_name TEXT NOT NULL DEFAULT '', color_role TEXT NOT NULL DEFAULT 'accent'
            );
            CREATE TABLE IF NOT EXISTS quota_providers(
              provider TEXT PRIMARY KEY, hour_tokens INTEGER NOT NULL DEFAULT 0, week_tokens INTEGER NOT NULL DEFAULT 0,
              hour_calls INTEGER NOT NULL DEFAULT 0, week_calls INTEGER NOT NULL DEFAULT 0,
              plan TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS known_models(
              provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
              PRIMARY KEY(provider, model)
            );
            CREATE TABLE IF NOT EXISTS quota_windows(
              provider TEXT NOT NULL, label TEXT NOT NULL, available_percent REAL NOT NULL,
              used_percent REAL NOT NULL, reset_at REAL, window_seconds INTEGER,
              status TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(provider,label)
            );
            CREATE TABLE IF NOT EXISTS preferences(
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts(
              device_id TEXT NOT NULL, room_id TEXT NOT NULL REFERENCES rooms(id), text TEXT NOT NULL,
              attachment_ids TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL, PRIMARY KEY(device_id,room_id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_room_cursor ON room_events(room_id,cursor);
            CREATE INDEX IF NOT EXISTS idx_executions_room ON executions(room_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_artifacts_room ON artifacts(room_id,created_at);
            """
        )
        room_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(rooms)")}
        for name, declaration in (
            ("source", "TEXT NOT NULL DEFAULT 'ares'"),
            ("external_session_id", "TEXT NOT NULL DEFAULT ''"),
            ("external_profile", "TEXT NOT NULL DEFAULT ''"),
            ("external_updated_at", "REAL"),
            ("external_activity", "TEXT NOT NULL DEFAULT ''"),
            ("external_activity_at", "REAL"),
            ("external_transcript_version", "INTEGER NOT NULL DEFAULT 0"),
            ("alias", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in room_columns:
                self._db.execute(f"ALTER TABLE rooms ADD COLUMN {name} {declaration}")
        peer_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(peers)")}
        for name, declaration in (
            ("source_name", "TEXT NOT NULL DEFAULT ''"),
            ("color_role", "TEXT NOT NULL DEFAULT 'accent'"),
        ):
            if name not in peer_columns:
                self._db.execute(f"ALTER TABLE peers ADD COLUMN {name} {declaration}")
        self._db.execute("UPDATE peers SET source_name=name WHERE source_name='' OR source_name IS NULL")
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_external_session "
            "ON rooms(external_profile,external_session_id) WHERE external_session_id!=''"
        )
        if self._db.execute("SELECT 1 FROM bots WHERE id='default'").fetchone() is None:
            self._db.execute(
                "INSERT INTO bots(id,name,description,profile,node_id,face_shape,color_role) VALUES(?,?,?,?,?,?,?)",
                ("default", "Hermes", "Default local Hermes agent", "default", "local", "diamond", "accent"),
            )
        for key, value in (
            ("homeQuotaVisible", True),
            ("homeQuotaCollapsed", False),
            ("hiddenHomeQuotaProviders", []),
        ):
            self._db.execute(
                "INSERT OR IGNORE INTO preferences(key,value) VALUES(?,?)",
                (key, compact(value)),
            )

    def _recover_interrupted(self) -> None:
        rows = self._db.execute("SELECT * FROM executions WHERE status IN ('queued','running')").fetchall()
        if not rows:
            return
        with self._transaction() as db:
            for row in rows:
                timestamp = now()
                db.execute("UPDATE executions SET status='interrupted',error='Ares service restarted',finished_at=? WHERE id=?", (timestamp, row["id"]))
                self._append_event(db, row["project_id"], row["room_id"], row["id"], "execution.interrupted", "ares", {"text": "Ares service restarted"})

    def _transaction(self):
        class Transaction:
            def __init__(self, store: "AresStore"):
                self.store = store
            def __enter__(self):
                self.store._lock.acquire()
                self.store._db.execute("BEGIN IMMEDIATE")
                return self.store._db
            def __exit__(self, kind, value, traceback):
                try:
                    self.store._db.execute("ROLLBACK" if kind else "COMMIT")
                finally:
                    self.store._lock.release()
        return Transaction(self)

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _project(self, project_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ValueError("project does not exist")
        return row

    def _room(self, room_id: str, include_deleted: bool = False) -> sqlite3.Row:
        sql = "SELECT * FROM rooms WHERE id=?" + ("" if include_deleted else " AND deleted_at IS NULL")
        row = self._db.execute(sql, (room_id,)).fetchone()
        if row is None:
            raise ValueError("room does not exist")
        return row

    def _owned_room(self, project_id: str, room_id: str, include_deleted: bool = False) -> sqlite3.Row:
        room = self._room(room_id, include_deleted)
        if room["project_id"] != project_id:
            raise ValueError("room does not belong to the project")
        return room

    def _receipt(self, db: sqlite3.Connection, scope: str, key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        key = require_text(key, "idempotency key", 160)
        digest = hashlib.sha256(compact(payload).encode()).hexdigest()
        row = db.execute(
            "SELECT payload_hash,response FROM command_receipts WHERE scope=? AND idempotency_key=?", (scope, key)
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != digest:
            raise ValueError("idempotency conflict")
        return json.loads(row["response"])

    def _save_receipt(self, db: sqlite3.Connection, scope: str, key: str, payload: dict[str, Any], response: dict[str, Any]) -> None:
        digest = hashlib.sha256(compact(payload).encode()).hexdigest()
        db.execute(
            "INSERT INTO command_receipts(scope,idempotency_key,payload_hash,response,created_at) VALUES(?,?,?,?,?)",
            (scope, key, digest, compact(response), now()),
        )

    def create_group(self, name: str, instructions: str = "") -> dict[str, Any]:
        group = {"id": ident("grp"), "name": require_text(name, "group name"), "instructions": instructions.strip(), "createdAt": now()}
        with self._transaction() as db:
            db.execute("INSERT INTO project_groups(id,name,instructions,created_at) VALUES(?,?,?,?)", tuple(group.values()))
        return group

    def create_project(self, name: str, path: str, node_id: str = "local", group_id: str | None = None,
                       virtual: bool = False) -> dict[str, Any]:
        node_id = require_text(node_id, "node", 128)
        raw_path = require_text(path, "project path", 2048)
        location = raw_path if (virtual or node_id != "local") else str(Path(raw_path).expanduser().resolve())
        if node_id == "local" and not virtual and not Path(location).is_dir():
            raise ValueError("project path must be an existing directory")
        if group_id and self._db.execute("SELECT 1 FROM project_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise ValueError("group does not exist")
        location_key = hashlib.sha256(f"{node_id}\0{location}".encode()).hexdigest()
        existing = self._db.execute("SELECT * FROM projects WHERE location_key=?", (location_key,)).fetchone()
        if existing:
            return self._project_dto(existing)
        project_id = ident("prj")
        created = now()
        repo_identity = ""
        git = Path(location) / ".git"
        if node_id == "local" and git.exists():
            repo_identity = hashlib.sha256(str(git.resolve()).encode()).hexdigest()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO projects(id,group_id,name,node_id,path,location_key,repo_identity,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, group_id, require_text(name, "project name"), node_id, location, location_key, repo_identity, created),
            )
        return self._project_dto(self._project(project_id))

    def create_room(self, project_id: str, name: str, participants: list[str]) -> dict[str, Any]:
        self._project(project_id)
        members = list(dict.fromkeys(require_text(item, "bot", 128) for item in participants))
        if not 1 <= len(members) <= 6:
            raise ValueError("a room requires one to six unique bots")
        room_id, created = ident("room"), now()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO rooms(id,project_id,name,status,memory_release,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (room_id, project_id, require_text(name, "room name"), "active", "not-requested", created, created),
            )
            for position, bot_id in enumerate(members):
                db.execute("INSERT INTO room_members(room_id,bot_id,position) VALUES(?,?,?)", (room_id, bot_id, position))
            self._append_event(db, project_id, room_id, None, "room.created", "ares", {"text": "Room created", "participants": members})
        return self._room_dto(self._room(room_id))

    def _append_event(self, db: sqlite3.Connection, project_id: str, room_id: str, execution_id: str | None,
                      kind: str, author: str, payload: dict[str, Any], stable_id: str | None = None) -> dict[str, Any]:
        self._owned_room(project_id, room_id, include_deleted=True)
        if stable_id:
            existing = db.execute("SELECT * FROM room_events WHERE id=?", (stable_id,)).fetchone()
            if existing:
                return self._event_dto(existing)
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM room_events WHERE room_id=?", (room_id,)).fetchone()[0]
        event_id = stable_id or ident("evt")
        created = now()
        cursor = db.execute(
            "INSERT INTO room_events(id,project_id,room_id,execution_id,sequence,kind,author,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?) RETURNING cursor",
            (event_id, project_id, room_id, execution_id, sequence, require_text(kind, "event kind", 128), require_text(author, "event author", 128), compact(payload), created),
        ).fetchone()[0]
        db.execute("UPDATE rooms SET updated_at=?,revision=revision+1 WHERE id=?", (created, room_id))
        return {"cursor": cursor, "id": event_id, "projectId": project_id, "roomId": room_id,
                "executionId": execution_id, "sequence": sequence, "kind": kind, "author": author,
                "payload": payload, "text": str(payload.get("text") or payload.get("delta") or ""), "createdAt": created}

    def append_event(self, project_id: str, room_id: str, execution_id: str | None, kind: str,
                     author: str, payload: dict[str, Any], stable_id: str | None = None) -> dict[str, Any]:
        with self._transaction() as db:
            return self._append_event(db, project_id, room_id, execution_id, kind, author, payload, stable_id)

    def upsert_live_hermes_event(self, profile: str, session_id: str, stable_id: str, kind: str,
                                 payload: dict[str, Any]) -> dict[str, Any] | None:
        """Insert or update one ephemeral streaming row without creating one event per token."""
        with self._transaction() as db:
            room = db.execute(
                "SELECT id,project_id FROM rooms WHERE source='hermes' AND external_profile=? "
                "AND external_session_id=? AND deleted_at IS NULL",
                (profile, session_id),
            ).fetchone()
            if room is None:
                return None
            safe_payload = sanitize_telemetry({**payload, "ephemeral": True})
            existing = db.execute("SELECT * FROM room_events WHERE id=? AND room_id=?", (stable_id, room["id"])).fetchone()
            if existing is None:
                return self._append_event(
                    db, room["project_id"], room["id"], None, kind, "assistant", safe_payload,
                    stable_id=stable_id,
                )
            timestamp = now()
            db.execute(
                "UPDATE room_events SET kind=?,author='assistant',payload=? WHERE id=?",
                (kind, compact(safe_payload), stable_id),
            )
            db.execute("UPDATE rooms SET updated_at=?,revision=revision+1 WHERE id=?", (timestamp, room["id"]))
            row = db.execute("SELECT * FROM room_events WHERE id=?", (stable_id,)).fetchone()
            return self._event_dto(row)

    def live_events(self, room_id: str) -> list[dict[str, Any]]:
        # The room validation must run under the lock too: the connection is
        # shared with the HTTP threads, and an unlocked execute racing a writer
        # fails with sqlite3.InterfaceError ("bad parameter or other API misuse").
        with self._lock:
            self._room(room_id, include_deleted=True)
            rows = self._db.execute(
                "SELECT * FROM room_events WHERE room_id=? AND json_extract(payload,'$.ephemeral')=1 "
                "ORDER BY created_at,sequence",
                (room_id,),
            ).fetchall()
        return [self._event_dto(row) for row in rows]

    def upsert_execution_event(self, project_id: str, room_id: str, execution_id: str, stable_id: str,
                               kind: str, author: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Insert or update one ephemeral execution stream row (one row per stream, never per delta)."""
        safe_payload = sanitize_telemetry({**payload, "ephemeral": True})
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM room_events WHERE id=? AND room_id=?",
                                  (stable_id, room_id)).fetchone()
            if existing is None:
                return self._append_event(db, project_id, room_id, execution_id, kind, author,
                                          safe_payload, stable_id=stable_id)
            timestamp = now()
            db.execute("UPDATE room_events SET kind=?,author=?,payload=? WHERE id=?",
                       (require_text(kind, "event kind", 128), require_text(author, "event author", 128),
                        compact(safe_payload), stable_id))
            db.execute("UPDATE rooms SET updated_at=?,revision=revision+1 WHERE id=?", (timestamp, room_id))
            row = db.execute("SELECT * FROM room_events WHERE id=?", (stable_id,)).fetchone()
            return self._event_dto(row)

    def clear_ephemeral_execution_events(self, room_id: str, execution_id: str) -> int:
        with self._transaction() as db:
            cursor = db.execute(
                "DELETE FROM room_events WHERE room_id=? AND execution_id=? "
                "AND json_extract(payload,'$.ephemeral')=1",
                (room_id, execution_id),
            )
            return int(cursor.rowcount or 0)

    def clear_live_hermes_events(self, profile: str, session_key: str, keep: tuple[str, ...] = ()) -> int:
        """Delete ephemeral mirror rows for a session except the given new-generation ids."""
        room = self._db.execute(
            "SELECT id FROM rooms WHERE source='hermes' AND external_profile=? "
            "AND external_session_id=? AND deleted_at IS NULL",
            (profile, session_key),
        ).fetchone()
        if room is None:
            return 0
        with self._transaction() as db:
            if keep:
                marks = ",".join("?" for _ in keep)
                cursor = db.execute(
                    f"DELETE FROM room_events WHERE room_id=? AND json_extract(payload,'$.ephemeral')=1 "
                    f"AND id NOT IN ({marks})",
                    (room["id"], *keep),
                )
            else:
                cursor = db.execute(
                    "DELETE FROM room_events WHERE room_id=? AND json_extract(payload,'$.ephemeral')=1",
                    (room["id"],),
                )
            return int(cursor.rowcount or 0)

    def cleanup_transient_delta_rows(self) -> int:
        """Remove legacy per-delta rows; live streams are ephemeral-flagged and survive."""
        with self._transaction() as db:
            cursor = db.execute(
                "DELETE FROM room_events WHERE kind IN ('message.delta','reasoning.delta','thinking.delta') "
                "AND COALESCE(json_extract(payload,'$.ephemeral'),0) != 1",
            )
            return int(cursor.rowcount or 0)

    def accept_prompt(self, project_id: str, room_id: str, bot_id: str, prompt: str, idempotency_key: str,
                      artifact_ids: list[str] | None = None) -> dict[str, Any]:
        room = self._owned_room(project_id, room_id)
        if room["status"] != "active":
            raise ValueError("room is not active")
        prompt = require_text(prompt, "prompt", 100_000)
        artifact_ids = list(dict.fromkeys(artifact_ids or []))
        payload = {"projectId": project_id, "roomId": room_id, "botId": bot_id, "prompt": prompt, "artifactIds": artifact_ids}
        with self._transaction() as db:
            replay = self._receipt(db, f"send:{room_id}", idempotency_key, payload)
            if replay:
                return replay
            execution = {"id": ident("exec"), "projectId": project_id, "roomId": room_id, "botId": bot_id,
                         "prompt": prompt, "status": "queued", "finalText": "", "error": "", "createdAt": now(),
                         "startedAt": None, "finishedAt": None}
            db.execute(
                "INSERT INTO executions(id,project_id,room_id,bot_id,prompt,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (execution["id"], project_id, room_id, bot_id, prompt, "queued", execution["createdAt"]),
            )
            self._append_event(db, project_id, room_id, execution["id"], "message.user", "user", {"text": prompt})
            for artifact_id in artifact_ids:
                artifact = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
                if artifact is None or artifact["project_id"] != project_id or artifact["room_id"] != room_id:
                    raise ValueError("artifact does not belong to the room")
                detail = "Waiting for the selected Hermes execution route"
                db.execute("INSERT INTO execution_artifacts(execution_id,artifact_id,delivery_state,detail,updated_at) VALUES(?,?,?,?,?)",
                           (execution["id"], artifact_id, "pending", detail, now()))
                self._append_event(db, project_id, room_id, execution["id"], "artifact.delivery.pending", "ares",
                                   {"text": detail, "artifactId": artifact_id, "mime": artifact["mime"]})
            self._append_event(db, project_id, room_id, execution["id"], "execution.queued", bot_id, {"text": "Queued"})
            self._save_receipt(db, f"send:{room_id}", idempotency_key, payload, execution)
        return execution

    def execution_artifacts(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT x.execution_id,x.artifact_id,x.delivery_state,x.detail,x.updated_at,a.name,a.mime,a.size,a.path "
            "FROM execution_artifacts x JOIN artifacts a ON a.id=x.artifact_id WHERE x.execution_id=? ORDER BY a.created_at",
            (execution_id,),
        ).fetchall()
        return [{"executionId": row["execution_id"], "artifactId": row["artifact_id"], "deliveryState": row["delivery_state"],
                 "detail": row["detail"], "updatedAt": row["updated_at"], "name": row["name"], "mime": row["mime"], "size": row["size"],
                 "path": row["path"]}
                for row in rows]

    def set_artifact_delivery(self, execution_id: str, artifact_id: str, state: str, detail: str) -> None:
        if state not in {"delivered", "failed", "unsupported"}:
            raise ValueError("artifact delivery state is invalid")
        execution = self.execution(execution_id)
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE execution_artifacts SET delivery_state=?,detail=?,updated_at=? WHERE execution_id=? AND artifact_id=?",
                (state, str(detail)[:1000], now(), execution_id, artifact_id),
            ).rowcount
            if changed != 1:
                raise ValueError("execution artifact does not exist")
            event_kind = "artifact.delivery.accepted" if state == "delivered" else f"artifact.delivery.{state}"
            self._append_event(
                db, execution["projectId"], execution["roomId"], execution_id, event_kind, "ares",
                {"text": str(detail)[:1000], "artifactId": artifact_id, "deliveryState": state},
            )

    def start_execution(self, execution_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None:
                raise ValueError("execution does not exist")
            started = now()
            db.execute("UPDATE executions SET status='running',started_at=? WHERE id=?", (started, execution_id))
            self._append_event(db, row["project_id"], row["room_id"], execution_id, "execution.started", row["bot_id"], {"text": "Started"})
        return self.execution(execution_id)

    def finish_execution(self, execution_id: str, status: str, text: str = "", error: str = "") -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("invalid execution status")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None:
                raise ValueError("execution does not exist")
            finished = now()
            db.execute("UPDATE executions SET status=?,final_text=?,error=?,finished_at=? WHERE id=?", (status, text, error, finished, execution_id))
            self._append_event(db, row["project_id"], row["room_id"], execution_id, f"execution.{status}", row["bot_id"], {"text": text or error or status})
        return self.execution(execution_id)

    def execution(self, execution_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        if row is None:
            raise ValueError("execution does not exist")
        return self._execution_dto(row)

    def bind_session(self, room_id: str, bot_id: str, node_id: str, stored_session_id: str, runtime_session_id: str = "") -> None:
        self._room(room_id, include_deleted=True)
        with self._transaction() as db:
            db.execute(
                "INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,runtime_session_id,lifecycle_state,updated_at) VALUES(?,?,?,?,?,'resident',?) "
                "ON CONFLICT(room_id,bot_id,node_id) DO UPDATE SET stored_session_id=excluded.stored_session_id,runtime_session_id=excluded.runtime_session_id,lifecycle_state='resident',updated_at=excluded.updated_at",
                (room_id, bot_id, node_id, stored_session_id, runtime_session_id, now()),
            )

    def sync_hermes_project(self, profile: str, project: dict[str, Any], node_id: str = "local",
                            virtual: bool = False) -> int:
        """Mirror public Hermes sessions into path-owned Ares rooms without duplicating them."""
        profile = require_text(profile or "default", "Hermes profile", 128)
        node_id = require_text(node_id, "node", 128)
        path_value = str(project.get("path") or "").strip()
        if not path_value:
            for repo in project.get("repos") or []:
                path_value = str(repo.get("path") or "").strip()
                if path_value:
                    break
        if node_id == "local" and not virtual:
            path = Path(path_value).expanduser().resolve()
            if not path.is_dir():
                return 0
            normalized_path = str(path)
            fallback_label = path.name or str(path)
        else:
            normalized_path = path_value
            fallback_label = node_id
        label = str(project.get("label") or fallback_label)[:256]
        target_project = self.create_project(label, normalized_path, node_id, virtual=virtual)
        sessions: dict[str, dict[str, Any]] = {}
        for repo in project.get("repos") or []:
            for group in repo.get("groups") or []:
                for session in group.get("sessions") or []:
                    session_id = str(session.get("id") or "").strip()
                    if session_id and str(session.get("source") or "") != "ares":
                        sessions[session_id] = session
        if not sessions:
            return 0
        bot = self._db.execute("SELECT id FROM bots WHERE id=? AND enabled=1", (profile,)).fetchone()
        bot_id = profile if bot else "default"
        created_count = 0
        with self._transaction() as db:
            for session_id, session in sessions.items():
                title = str(session.get("title") or session_id).strip()[:256] or session_id
                activity = float(session.get("last_active") or session.get("started_at") or now())
                existing = db.execute(
                    "SELECT * FROM rooms WHERE external_profile=? AND external_session_id=?",
                    (profile, session_id),
                ).fetchone()
                if existing is None:
                    room_id = "room_hermes_" + hashlib.sha256(f"{profile}\0{session_id}".encode()).hexdigest()[:32]
                    created = now()
                    db.execute(
                        "INSERT INTO rooms(id,project_id,name,status,memory_release,source,external_session_id,"
                        "external_profile,external_updated_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (room_id, target_project["id"], title, "active", "not-requested", "hermes",
                         session_id, profile, activity, created, activity),
                    )
                    db.execute("INSERT INTO room_members(room_id,bot_id,position) VALUES(?,?,0)", (room_id, bot_id))
                    self._append_event(
                        db, target_project["id"], room_id, None, "room.connected", "ares",
                        {"text": "Connected to existing Hermes room", "sessionId": session_id,
                         "profile": profile, "source": str(session.get("source") or "")[:64]},
                    )
                    db.execute("UPDATE rooms SET updated_at=? WHERE id=?", (activity, room_id))
                    created_count += 1
                else:
                    room_id = existing["id"]
                    db.execute(
                        "UPDATE rooms SET project_id=?,name=?,external_updated_at=?,updated_at=? WHERE id=?",
                        (target_project["id"], title, activity, activity, room_id),
                    )
                db.execute(
                    "INSERT INTO session_bindings(room_id,bot_id,node_id,stored_session_id,runtime_session_id,"
                    "lifecycle_state,updated_at) VALUES(?,?,?,?, '', 'resident',?) "
                    "ON CONFLICT(room_id,bot_id,node_id) DO UPDATE SET stored_session_id=excluded.stored_session_id,"
                    "updated_at=excluded.updated_at",
                    (room_id, bot_id, node_id, session_id, now()),
                )
        return created_count

    def sync_hermes_transcript(self, profile: str, session_id: str, messages: list[dict[str, Any]],
                               metadata: dict[str, Any] | None = None) -> int:
        """Mirror the recent persisted Hermes event stream without taking session ownership."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM rooms WHERE external_profile=? AND external_session_id=? AND deleted_at IS NULL",
                (profile, session_id),
            ).fetchone()
        if row is None:
            return 0
        metadata = metadata or {}
        activity = str(metadata.get("last_activity_description") or "").strip()[:500]
        activity_at = metadata.get("last_activity_at")
        try:
            activity_at = float(activity_at) if activity_at is not None else None
        except (TypeError, ValueError):
            activity_at = None

        def message_text(message: dict[str, Any]) -> str:
            content = message.get("display_content")
            if content is None:
                content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if not isinstance(content, list):
                return str(content or "").strip()
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
                    parts.append(str(part.get("text") or ""))
            return "\n".join(item for item in parts if item).strip()

        def visible_user_text(text: str) -> str:
            if text.startswith("[CONTEXT COMPACTION — REFERENCE ONLY]"):
                return ""
            marker = "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool output and not a new delivery when replayed from conversation history]"
            if marker in text and "[/OUT-OF-BAND USER MESSAGE]" in text:
                return text.split(marker, 1)[1].split("[/OUT-OF-BAND USER MESSAGE]", 1)[0].strip()
            return text

        def decoded_arguments(call: dict[str, Any]) -> tuple[str, Any]:
            function_value = call.get("function")
            function: dict[str, Any] = function_value if isinstance(function_value, dict) else {}
            name = str(function.get("name") or call.get("name") or "tool")[:160]
            raw = function.get("arguments") if "arguments" in function else call.get("arguments")
            if isinstance(raw, str):
                try:
                    arguments = json.loads(raw)
                except (TypeError, ValueError):
                    arguments = raw
            else:
                arguments = raw if raw is not None else {}
            return name, sanitize_telemetry(arguments)

        def tool_call_text(name: str, arguments: Any) -> str:
            if name == "terminal" and isinstance(arguments, dict) and isinstance(arguments.get("command"), str):
                return "$ " + arguments["command"]
            rendered = json.dumps(arguments, ensure_ascii=False, indent=2) if not isinstance(arguments, str) else arguments
            return name + ("\n" + rendered if rendered else "")

        def decoded_tool_result(message: dict[str, Any], text: str) -> tuple[str, dict[str, Any]]:
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = text
            safe = sanitize_telemetry(decoded)
            payload: dict[str, Any] = {
                "toolName": str(message.get("tool_name") or "tool")[:160],
                "callId": str(message.get("tool_call_id") or "")[:256],
                "mirrored": True,
            }
            if isinstance(safe, dict):
                if "exit_code" in safe:
                    payload["exitCode"] = safe.get("exit_code")
                if safe.get("success") is True:
                    payload["status"] = "ok"
                elif safe.get("success") is False:
                    payload["status"] = "failed"
                if safe.get("error"):
                    payload["error"] = str(safe["error"])
                    payload["status"] = "failed"
                return _tool_result_display_text(safe), payload
            return _tool_result_display_text(safe), payload

        facets: list[tuple[str, str, str, dict[str, Any], float]] = []
        call_started: dict[str, float] = {}
        for message in messages[-200:]:
            if not isinstance(message, dict) or message.get("display_kind") == "hidden":
                continue
            role = str(message.get("role") or "")
            message_id = str(message.get("id") or "").strip()
            try:
                created = float(message.get("timestamp") or activity_at or row["external_updated_at"] or now())
            except (TypeError, ValueError):
                created = now()
            base = {"externalMessageId": message_id, "externalSessionId": session_id, "mirrored": True}
            if role == "assistant" and (message.get("reasoning") or message.get("reasoning_content")
                                         or message.get("reasoning_details")):
                tokens = message.get("token_count")
                thinking = f"Thinking · {tokens} tokens" if isinstance(tokens, int) and tokens > 0 else "Thinking…"
                facets.append(("reasoning", "reasoning.status", "assistant", {**base, "text": thinking,
                               "tokenCount": tokens, "sourceOrder": 0}, created))
            text = message_text(message)
            if role == "user":
                text = visible_user_text(text)
            if role in {"user", "assistant"} and text:
                facets.append(("message", f"message.{role}", role, {
                    **base, "text": sanitize_telemetry(text), "sourceOrder": 1,
                }, created))
            tool_calls = message.get("tool_calls")
            if role == "assistant" and isinstance(tool_calls, list):
                for index, call in enumerate(tool_calls):
                    if not isinstance(call, dict):
                        continue
                    name, arguments = decoded_arguments(call)
                    call_id = str(call.get("id") or f"{message_id}:{index}")[:256]
                    call_started[call_id] = created
                    facets.append((f"tool-call:{call_id}", "tool.call", "assistant", {
                        **base, "text": tool_call_text(name, arguments), "toolName": name,
                        "callId": call_id, "arguments": arguments, "sourceOrder": 2 + (index / 1000),
                    }, created))
            if role == "tool" and text:
                result_text, result_payload = decoded_tool_result(message, text)
                call_id = result_payload["callId"]
                if call_id in call_started:
                    result_payload["durationSeconds"] = max(0, created - call_started[call_id])
                facets.append((f"tool-result:{call_id or message_id}", "tool.result", "tool",
                               {**base, **result_payload, "text": result_text, "sourceOrder": 3}, created))

        inserted = 0
        with self._transaction() as db:
            if facets:
                latest_source_timestamp = max(facet[4] for facet in facets)
                db.execute(
                    "DELETE FROM room_events WHERE room_id=? AND created_at<=? "
                    "AND json_extract(payload,'$.ephemeral')=1 "
                    "AND json_extract(payload,'$.externalSessionId')=?",
                    (row["id"], latest_source_timestamp, session_id),
                )
            bot = db.execute(
                "SELECT bot_id FROM room_members WHERE room_id=? ORDER BY position LIMIT 1", (row["id"],)
            ).fetchone()
            bot_id = bot[0] if bot else "default"
            for facet, kind, source_author, payload, created in facets:
                message_id = str(payload.get("externalMessageId") or "")
                identity = message_id or compact({"kind": kind, "timestamp": created, "payload": payload})
                if facet != "message":
                    identity += "\0" + facet
                stable_id = "hermes_msg_" + hashlib.sha256(
                    f"{profile}\0{session_id}\0{identity}".encode()).hexdigest()
                if db.execute("SELECT 1 FROM room_events WHERE id=?", (stable_id,)).fetchone():
                    continue
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM room_events WHERE room_id=?", (row["id"],)
                ).fetchone()[0]
                author = "user" if source_author == "user" else ("tool" if source_author == "tool" else bot_id)
                db.execute(
                    "INSERT INTO room_events(id,project_id,room_id,execution_id,sequence,kind,author,payload,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (stable_id, row["project_id"], row["id"], None, sequence, kind, author,
                     compact(sanitize_telemetry(payload)), created),
                )
                inserted += 1
            if inserted:
                db.execute("DELETE FROM room_events WHERE room_id=? AND kind='room.connected'", (row["id"],))
            effective_activity = activity_at if activity_at is not None else row["external_updated_at"]
            db.execute(
                "UPDATE rooms SET external_activity=?,external_activity_at=?,external_updated_at=COALESCE(?,external_updated_at),"
                "external_transcript_version=2,"
                "updated_at=MAX(updated_at,COALESCE(?,updated_at)),revision=revision+? WHERE id=?",
                (activity, activity_at, activity_at, effective_activity, 1 if inserted else 0, row["id"]),
            )
        return inserted

    def sync_profiles(self, profiles: list[dict[str, Any]]) -> int:
        synced = 0
        shapes = ("diamond", "circle", "square", "hexagon")
        with self._transaction() as db:
            for item in profiles:
                profile = str(item.get("name") or item.get("profile") or "").strip()
                if not profile or len(profile) > 128:
                    continue
                display = str(item.get("display_name") or item.get("title") or profile).strip()[:160]
                description = str(item.get("description") or "")[:500]
                model = str(item.get("model") or "")[:160]
                provider = str(item.get("provider") or "")[:160]
                existing = db.execute("SELECT * FROM bots WHERE id=?", (profile,)).fetchone()
                sidecar = _json_read(_bot_sidecar_path(profile))
                if sidecar is None and existing is not None and (
                    existing["name"] != display
                    or (existing["description"] or "") != description
                    or (existing["model"] or "") != model
                ):
                    sidecar = {"name": existing["name"], "description": existing["description"],
                               "model": existing["model"], "provider": existing["provider"],
                               "face_shape": existing["face_shape"], "color_role": existing["color_role"]}
                    _json_write(_bot_sidecar_path(profile), sidecar)
                if sidecar:
                    display = str(sidecar.get("name") or display)[:160]
                    description = str(sidecar.get("description") or description)[:500]
                    model = str(sidecar.get("model") or model)[:160]
                    provider = str(sidecar.get("provider") or provider)[:160]
                shape = shapes[int(hashlib.sha256(profile.encode()).hexdigest()[:2], 16) % len(shapes)]
                color = "accent"
                if sidecar:
                    shape = str(sidecar.get("face_shape") or shape)
                    color = str(sidecar.get("color_role") or color)
                db.execute(
                    "INSERT INTO bots(id,name,description,profile,node_id,model,provider,face_shape,color_role,enabled) VALUES(?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,profile=excluded.profile,model=excluded.model,provider=excluded.provider,enabled=1",
                    (profile, display, description, profile, "local",
                     model, provider, shape, color),
                )
                if model.strip():
                    db.execute(
                        "INSERT OR IGNORE INTO known_models(provider,model) VALUES(?,?)",
                        (provider.strip()[:160], model.strip()[:160]),
                    )
                synced += 1
        return synced

    def sync_peers(self, peers: list[dict[str, Any]]) -> int:
        timestamp = now()
        peers_sidecar = _json_read(_peers_sidecar_path()) or {}
        with self._transaction() as db:
            for item in peers:
                peer_id = str(item.get("name") or item.get("id") or "").strip()
                if not peer_id or len(peer_id) > 128:
                    continue
                status = str(item.get("status") or "unknown")[:64]
                capabilities = {"authenticated": bool(item.get("authenticated")), "lanAccess": bool(item.get("lanAccess")),
                                "version": str(item.get("version") or ""), "agents": item.get("agents") or []}
                source_name = str(item.get("title") or peer_id).strip()[:160]
                entry = peers_sidecar.get(peer_id)
                existing = db.execute("SELECT name,color_role FROM peers WHERE id=?", (peer_id,)).fetchone()
                if entry is None and existing is not None and (
                    existing["name"] != source_name or (existing["color_role"] or "accent") != "accent"
                ):
                    entry = {"name": existing["name"], "color_role": existing["color_role"]}
                    peers_sidecar[peer_id] = entry
                display_name = str(entry.get("name") or source_name)[:160] if entry else source_name
                color_role = str(entry.get("color_role") or "accent") if entry else "accent"
                db.execute(
                    "INSERT INTO peers(id,name,endpoint,status,capabilities,last_seen_at,updated_at,source_name,color_role) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_name=excluded.source_name,"
                    "endpoint=excluded.endpoint,status=excluded.status,capabilities=excluded.capabilities,"
                    "last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at",
                    (peer_id, display_name, str(item.get("url") or "")[:500], status,
                     compact(capabilities), timestamp if status in {"ok", "degraded"} else None, timestamp,
                     source_name, color_role),
                )
        if peers_sidecar:
            _json_write(_peers_sidecar_path(), peers_sidecar)
        return len(peers)

    def update_peer_preferences(self, peer_id: str, name: str, color_role: str) -> dict[str, Any]:
        peer_id = require_text(peer_id, "peer", 128)
        display_name = require_text(name, "peer name", 160)
        allowed_roles = {"accent", "success", "warning", "urgent", "info", "muted"}
        if color_role not in allowed_roles:
            raise ValueError("unsupported peer color role")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM peers WHERE id=?", (peer_id,)).fetchone() is None:
                raise ValueError("peer does not exist")
            db.execute("UPDATE peers SET name=?,color_role=?,updated_at=? WHERE id=?",
                       (display_name, color_role, now(), peer_id))
            result = self._peer_dto(db.execute("SELECT * FROM peers WHERE id=?", (peer_id,)).fetchone())
        peers_sidecar = _json_read(_peers_sidecar_path()) or {}
        peers_sidecar[peer_id] = {"name": display_name, "color_role": color_role}
        _json_write(_peers_sidecar_path(), peers_sidecar)
        return result

    def update_bot(self, bot_id: str, *, name: str | None = None, description: str | None = None,
                   face_shape: str | None = None, color_role: str | None = None,
                   model: str | None = None, provider: str | None = None,
                   enabled: bool | None = None) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
        if row is None:
            return None
        allowed_roles = {"accent", "success", "warning", "urgent", "info", "muted"}
        allowed_shapes = {"oval", "square", "triangle", "diamond", "circle", "hexagon"}
        next_name = str(name).strip() if name is not None else row["name"]
        next_description = str(description).strip() if description is not None else row["description"]
        next_shape = str(face_shape).strip() if face_shape is not None else row["face_shape"]
        next_color = str(color_role).strip() if color_role is not None else row["color_role"]
        next_model = str(model).strip() if model is not None else row["model"]
        next_provider = str(provider).strip() if provider is not None else row["provider"]
        next_enabled = row["enabled"] if enabled is None else (1 if enabled else 0)
        if next_name == "":
            raise ValueError("bot name must not be empty")
        if next_shape not in allowed_shapes:
            raise ValueError("unsupported bot face shape")
        if next_color not in allowed_roles:
            raise ValueError("unsupported bot color role")
        if len(next_model) > 160 or len(next_provider) > 160:
            raise ValueError("model or provider too long")
        # Keep the historical shape names intact; the QML maps them for BotFace.
        with self._transaction() as db:
            db.execute(
                "UPDATE bots SET name=?,description=?,face_shape=?,color_role=?,model=?,provider=?,enabled=? WHERE id=?",
                (next_name[:160], next_description[:500], next_shape, next_color,
                 next_model[:160], next_provider[:160], next_enabled, bot_id),
            )
        if (model is not None or provider is not None) and row["node_id"] == "local":
            _write_profile_model(str(row["profile"]), next_model, next_provider)
        if next_model:
            with self._transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO known_models(provider,model) VALUES(?,?)",
                    (next_provider[:160], next_model[:160]),
                )
        if row["node_id"] == "local":
            current = self._db.execute(
                "SELECT name,description,model,provider,face_shape,color_role FROM bots WHERE id=?", (bot_id,)).fetchone()
            if current is not None:
                _json_write(_bot_sidecar_path(str(row["profile"])), {
                    "name": current["name"], "description": current["description"],
                    "model": current["model"], "provider": current["provider"],
                    "face_shape": current["face_shape"], "color_role": current["color_role"],
                })
        # A model/provider switch only lands on NEW Hermes sessions; bound
        # sessions keep the model they were created with. Unbind them so the
        # next send builds a fresh session that reads the updated config.
        model_changed = (model is not None and next_model != row["model"]) or \
                        (provider is not None and next_provider != row["provider"])
        unbound: list[dict[str, str]] = []
        if model_changed:
            rows = self._db.execute(
                "SELECT stored_session_id,runtime_session_id FROM session_bindings WHERE bot_id=?",
                (bot_id,)).fetchall()
            unbound = [{"storedSessionId": str(r["stored_session_id"] or ""),
                        "runtimeSessionId": str(r["runtime_session_id"] or "")} for r in rows]
            with self._transaction() as db:
                db.execute("DELETE FROM session_bindings WHERE bot_id=?", (bot_id,))
        dto = self._bot_dto(self._db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone())
        dto["unboundSessions"] = unbound
        return dto

    def sync_quotas(self, provider_usage: dict[str, Any]) -> int:
        updated = float(provider_usage.get("updatedAt") or now())
        count = 0
        with self._transaction() as db:
            db.execute("DELETE FROM quota_windows")
            db.execute("DELETE FROM quota_providers")
            for provider in provider_usage.get("providers") or []:
                provider_id = str(provider.get("provider") or "")[:128]
                if not provider_id:
                    continue
                quota = provider.get("quota") or {}
                db.execute(
                    "INSERT INTO quota_providers(provider,hour_tokens,week_tokens,hour_calls,week_calls,plan,details,status,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (provider_id, int(provider.get("hourTokens") or 0), int(provider.get("weekTokens") or 0),
                     int(provider.get("hourCalls") or 0), int(provider.get("weekCalls") or 0),
                     str(quota.get("plan") or "")[:160], compact(quota.get("details") or []),
                     str(quota.get("status") or "unknown")[:64], updated),
                )
                for window in quota.get("windows") or []:
                    db.execute(
                        "INSERT INTO quota_windows(provider,label,available_percent,used_percent,reset_at,window_seconds,status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (provider_id, str(window.get("label") or "Window")[:160], float(window.get("availablePercent") or 0),
                         float(window.get("usedPercent") or 0), window.get("resetAt"), window.get("windowSeconds"),
                         str(quota.get("status") or "unknown")[:64], updated),
                    )
                    count += 1
        return count

    def pause_room(self, project_id: str, room_id: str, idempotency_key: str) -> dict[str, Any]:
        payload = {"projectId": project_id, "roomId": room_id, "action": "pause"}
        with self._transaction() as db:
            room = self._owned_room(project_id, room_id)
            replay = self._receipt(db, f"lifecycle:{room_id}", idempotency_key, payload)
            if replay:
                return replay
            db.execute("UPDATE rooms SET status='paused',memory_release='unconfirmed',revision=revision+1,updated_at=? WHERE id=?", (now(), room_id))
            db.execute("UPDATE session_bindings SET lifecycle_state='unconfirmed',updated_at=? WHERE room_id=?", (now(), room_id))
            self._append_event(db, project_id, room_id, None, "room.pause.requested", "ares", {"text": "Pause requested", "memoryRelease": "unconfirmed"})
            sessions = [self._session_dto(row) for row in db.execute("SELECT * FROM session_bindings WHERE room_id=? ORDER BY bot_id,node_id", (room_id,))]
            result = {"roomId": room_id, "status": "paused", "memoryRelease": "unconfirmed", "sessions": sessions}
            self._save_receipt(db, f"lifecycle:{room_id}", idempotency_key, payload, result)
            return result

    def update_room_alias(self, room_id: str, alias: str) -> dict[str, Any]:
        with self._transaction() as db:
            room = db.execute("SELECT id FROM rooms WHERE id=? AND deleted_at IS NULL", (require_text(room_id, "room", 128),)).fetchone()
            if room is None:
                raise LookupError("Room not found")
            db.execute("UPDATE rooms SET alias=?,updated_at=?,revision=revision+1 WHERE id=?",
                       (require_text(alias, "alias", 120) if alias.strip() else "", now(), room_id))
            return self._room_dto(db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone())

    def record_pause_outcomes(self, project_id: str, room_id: str, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        with self._transaction() as db:
            self._owned_room(project_id, room_id)
            states: list[str] = []
            for outcome in outcomes:
                bot_id = str(outcome["botId"])
                node_id = str(outcome["nodeId"])
                release_state = str(outcome["releaseState"])
                states.append(release_state)
                db.execute(
                    "UPDATE session_bindings SET lifecycle_state=?,updated_at=? WHERE room_id=? AND bot_id=? AND node_id=?",
                    (release_state, now(), room_id, bot_id, node_id),
                )
                event_kind = "session.release.confirmed" if release_state == "released" else "session.release.pending"
                self._append_event(
                    db, project_id, room_id, None, event_kind, "ares",
                    {"text": f"{bot_id}@{node_id}: {release_state}", "botId": bot_id, "nodeId": node_id,
                     "releaseState": release_state, "detail": str(outcome.get("detail") or "")[:500]},
                )
            if outcomes and all(state == "released" for state in states):
                memory_release = "confirmed"
            elif any(state == "released" for state in states):
                memory_release = "partial"
            else:
                memory_release = "unconfirmed"
            db.execute(
                "UPDATE rooms SET memory_release=?,revision=revision+1,updated_at=? WHERE id=?",
                (memory_release, now(), room_id),
            )
            self._append_event(
                db, project_id, room_id, None, "room.pause.completed", "ares",
                {"text": f"Pause completed: {memory_release}", "memoryRelease": memory_release},
            )
            sessions = [self._session_dto(row) for row in db.execute(
                "SELECT * FROM session_bindings WHERE room_id=? ORDER BY bot_id,node_id", (room_id,))]
            return {"roomId": room_id, "status": "paused", "memoryRelease": memory_release, "sessions": sessions}

    def resume_room(self, project_id: str, room_id: str, idempotency_key: str) -> dict[str, Any]:
        payload = {"projectId": project_id, "roomId": room_id, "action": "resume"}
        with self._transaction() as db:
            self._owned_room(project_id, room_id)
            replay = self._receipt(db, f"lifecycle:{room_id}", idempotency_key, payload)
            if replay:
                return replay
            db.execute("UPDATE rooms SET status='active',memory_release='not-requested',revision=revision+1,updated_at=? WHERE id=?", (now(), room_id))
            self._append_event(db, project_id, room_id, None, "room.resume.requested", "ares", {"text": "Room resumed"})
            result = {"roomId": room_id, "status": "active", "memoryRelease": "not-requested"}
            self._save_receipt(db, f"lifecycle:{room_id}", idempotency_key, payload, result)
            return result

    def soft_delete_room(self, project_id: str, room_id: str, idempotency_key: str) -> dict[str, Any]:
        payload = {"projectId": project_id, "roomId": room_id, "action": "delete"}
        with self._transaction() as db:
            self._owned_room(project_id, room_id)
            replay = self._receipt(db, f"delete:{room_id}", idempotency_key, payload)
            if replay:
                return replay
            if db.execute("SELECT 1 FROM executions WHERE room_id=? AND status IN ('queued','running')", (room_id,)).fetchone():
                raise ValueError("room has unfinished executions")
            timestamp = now()
            db.execute("UPDATE rooms SET status='deleted',deleted_at=?,updated_at=?,revision=revision+1 WHERE id=?", (timestamp, timestamp, room_id))
            self._append_event(db, project_id, room_id, None, "room.deleted", "ares", {"text": "Room moved to recovery"})
            result = {"roomId": room_id, "status": "deleted", "recoverable": True}
            self._save_receipt(db, f"delete:{room_id}", idempotency_key, payload, result)
            return result

    def restore_room(self, project_id: str, room_id: str, idempotency_key: str) -> dict[str, Any]:
        payload = {"projectId": project_id, "roomId": room_id, "action": "restore"}
        with self._transaction() as db:
            self._owned_room(project_id, room_id, include_deleted=True)
            replay = self._receipt(db, f"restore:{room_id}", idempotency_key, payload)
            if replay:
                return replay
            db.execute("UPDATE rooms SET status='paused',deleted_at=NULL,memory_release='unconfirmed',updated_at=?,revision=revision+1 WHERE id=?", (now(), room_id))
            self._append_event(db, project_id, room_id, None, "room.restored", "ares", {"text": "Room restored in paused state"})
            result = {"roomId": room_id, "status": "paused", "memoryRelease": "unconfirmed"}
            self._save_receipt(db, f"restore:{room_id}", idempotency_key, payload, result)
            return result

    def store_artifact(self, project_id: str, room_id: str, name: str, mime: str, body: bytes, author: str,
                       execution_id: str | None = None, event_id: str | None = None) -> dict[str, Any]:
        self._owned_room(project_id, room_id, include_deleted=True)
        if not body or len(body) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact size is invalid")
        mime = require_text(mime, "MIME type", 160).lower()
        if not (mime.startswith("image/") or mime.startswith("video/")):
            raise ValueError("only image and video artifacts are supported")
        safe_name = Path(require_text(name, "artifact name", 255)).name
        artifact_id = ident("art")
        digest = hashlib.sha256(body).hexdigest()
        destination = self.artifact_root / artifact_id
        temporary = self.artifact_root / f".{artifact_id}.tmp"
        with temporary.open("wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        created = now()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO artifacts(id,project_id,room_id,execution_id,event_id,author,name,mime,size,sha256,path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, project_id, room_id, execution_id, event_id, require_text(author, "artifact author", 128), safe_name, mime, len(body), digest, str(destination), created),
            )
            self._append_event(db, project_id, room_id, execution_id, "artifact.available", author,
                               {"text": safe_name, "artifactId": artifact_id, "name": safe_name, "mime": mime, "size": len(body)})
        return {"id": artifact_id, "projectId": project_id, "roomId": room_id, "executionId": execution_id,
                "eventId": event_id, "author": author, "name": safe_name, "mime": mime, "size": len(body),
                "sha256": digest, "createdAt": created, "url": f"/v1/artifacts/{artifact_id}"}

    def read_artifact(self, artifact_id: str, start: int | None = None, end: int | None = None) -> tuple[bytes, int, int, int]:
        row = self._db.execute("SELECT path,size FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise ValueError("artifact does not exist")
        total = row["size"]
        first = 0 if start is None else max(0, start)
        last = total - 1 if end is None else min(total - 1, end)
        if first > last or first >= total:
            raise ValueError("artifact range is invalid")
        with Path(row["path"]).open("rb") as handle:
            handle.seek(first)
            return handle.read(last - first + 1), first, last, total

    def events_after(self, cursor: int, limit: int = 200, room_id: str | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1000))
        where, params = "cursor>?", [int(cursor)]
        if room_id:
            where += " AND room_id=?"
            params.append(room_id)
        rows = self._db.execute(f"SELECT * FROM room_events WHERE {where} ORDER BY cursor LIMIT ?", (*params, limit)).fetchall()
        minimum = self._db.execute("SELECT COALESCE(MIN(cursor),0) FROM room_events").fetchone()[0]
        maximum = self._db.execute("SELECT COALESCE(MAX(cursor),0) FROM room_events").fetchone()[0]
        return {"events": [self._event_dto(row) for row in rows], "nextCursor": rows[-1]["cursor"] if rows else max(cursor, maximum),
                "gap": bool(minimum and cursor < minimum - 1), "earliestCursor": minimum, "latestCursor": maximum}

    def events_before(self, room_id: str, before_cursor: int, limit: int = TRANSCRIPT_PAGE_EVENTS) -> dict[str, Any]:
        limit = max(1, min(int(limit), TRANSCRIPT_PAGE_EVENTS))
        with self._lock:
            self._room(room_id)
            rows = list(reversed(self._db.execute(
                "SELECT * FROM room_events WHERE room_id=? AND cursor<? ORDER BY cursor DESC LIMIT ?",
                (room_id, int(before_cursor), limit),
            ).fetchall()))
            minimum = self._db.execute(
                "SELECT COALESCE(MIN(cursor),0) FROM room_events WHERE room_id=?", (room_id,)
            ).fetchone()[0]
            earliest = rows[0]["cursor"] if rows else int(before_cursor)
            return {
                "events": [self._event_dto(row) for row in rows],
                "earliestCursor": earliest,
                "hasOlder": bool(rows and minimum < earliest),
            }

    def snapshot(self, include_deleted: bool = False, room_id: str | None = None,
                 event_room_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(include_deleted=include_deleted, room_id=room_id,
                                         event_room_id=event_room_id)

    def preferences(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "homeQuotaVisible": True,
            "homeQuotaCollapsed": False,
            "hiddenHomeQuotaProviders": [],
            "panelSize": "medium",
            "sidebarCollapsed": False,
            "sidebarWidth": 200,
        }
        rows = self._db.execute("SELECT key,value FROM preferences").fetchall()
        for row in rows:
            if row["key"] in defaults:
                defaults[row["key"]] = json.loads(row["value"])
        return defaults

    def update_preferences(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"homeQuotaVisible", "homeQuotaCollapsed", "hiddenHomeQuotaProviders",
                   "panelSize", "sidebarCollapsed", "sidebarWidth",
                   "selectedRoomId"}
        unsupported = set(values) - allowed
        if unsupported:
            raise ValueError("unsupported preference: " + sorted(unsupported)[0])
        for key in ("homeQuotaVisible", "homeQuotaCollapsed", "sidebarCollapsed"):
            if key in values and not isinstance(values[key], bool):
                raise ValueError(f"{key} must be boolean")
        if "panelSize" in values and str(values["panelSize"]) not in {"small", "medium", "large"}:
            raise ValueError("panelSize must be one of: small, medium, large")
        if "sidebarWidth" in values:
            width = values["sidebarWidth"]
            if not isinstance(width, int) or isinstance(width, bool) or not 120 <= width <= 480:
                raise ValueError("sidebarWidth must be an integer between 120 and 480")
        hidden = values.get("hiddenHomeQuotaProviders")
        if hidden is not None:
            if not isinstance(hidden, list) or any(
                not isinstance(provider, str) or not provider.strip() or len(provider) > 128
                for provider in hidden
            ):
                raise ValueError("hiddenHomeQuotaProviders must be a list of provider IDs")
            values = {**values, "hiddenHomeQuotaProviders": list(dict.fromkeys(
                provider.strip() for provider in hidden
            ))}
        with self._transaction() as db:
            for key, value in values.items():
                db.execute(
                    "INSERT INTO preferences(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, compact(value)),
                )
        return self.preferences()

    def save_draft(self, device_id: str, room_id: str, text: str) -> dict[str, Any]:
        device = require_text(device_id, "device", 128)
        with self._transaction() as db:
            room = db.execute("SELECT id FROM rooms WHERE id=?", (require_text(room_id, "room", 128),)).fetchone()
            if room is None:
                raise ValueError("room does not exist")
            if text.strip() == "":
                db.execute("DELETE FROM drafts WHERE device_id=? AND room_id=?", (device, room["id"]))
            else:
                db.execute(
                    "INSERT INTO drafts(device_id,room_id,text,attachment_ids,updated_at) VALUES(?,?,?,'[]',?) "
                    "ON CONFLICT(device_id,room_id) DO UPDATE SET text=excluded.text,updated_at=excluded.updated_at",
                    (device, room["id"], text[:20000], now()),
                )
        return {"roomId": room_id, "text": text, "updatedAt": now()}

    def device_drafts(self, device_id: str) -> list[dict[str, Any]]:
        device = require_text(device_id, "device", 128)
        rows = self._db.execute(
            "SELECT room_id,text,updated_at FROM drafts WHERE device_id=? ORDER BY updated_at DESC", (device,)
        ).fetchall()
        return [{"roomId": row["room_id"], "text": row["text"], "updatedAt": row["updated_at"]} for row in rows]

    def _snapshot_locked(self, include_deleted: bool = False, room_id: str | None = None,
                         event_room_id: str | None = None) -> dict[str, Any]:
        event_page = {"roomId": event_room_id or "", "earliestCursor": 0, "hasOlder": False,
                      "limit": TRANSCRIPT_PAGE_EVENTS}
        room_where = "1=1" if include_deleted else "deleted_at IS NULL"
        params: list[Any] = []
        if room_id:
            room_where += " AND id=?"
            params.append(room_id)
        rooms = self._db.execute(f"SELECT * FROM rooms WHERE {room_where} ORDER BY updated_at DESC", params).fetchall()
        visible_room_ids = [row["id"] for row in rooms]
        if room_id and not rooms:
            events: list[sqlite3.Row] = []
            executions: list[sqlite3.Row] = []
            artifacts: list[sqlite3.Row] = []
        elif room_id:
            events = self._db.execute("SELECT * FROM room_events WHERE room_id=? ORDER BY sequence", (room_id,)).fetchall()
            executions = self._db.execute("SELECT * FROM executions WHERE room_id=? ORDER BY created_at", (room_id,)).fetchall()
            artifacts = self._db.execute("SELECT * FROM artifacts WHERE room_id=? ORDER BY created_at", (room_id,)).fetchall()
        elif event_room_id is not None:
            events = []
            for visible_id in visible_room_ids:
                if visible_id == event_room_id:
                    rows = list(reversed(self._db.execute(
                        "SELECT * FROM room_events WHERE room_id=? ORDER BY cursor DESC LIMIT ?",
                        (visible_id, TRANSCRIPT_PAGE_EVENTS),
                    ).fetchall()))
                    total = self._db.execute(
                        "SELECT COUNT(*) FROM room_events WHERE room_id=?", (visible_id,)
                    ).fetchone()[0]
                    event_page = {
                        "roomId": visible_id,
                        "earliestCursor": rows[0]["cursor"] if rows else 0,
                        "hasOlder": total > len(rows),
                        "limit": TRANSCRIPT_PAGE_EVENTS,
                    }
                else:
                    rows = list(reversed(self._db.execute(
                        "SELECT * FROM room_events WHERE room_id=? ORDER BY created_at DESC,sequence DESC LIMIT 1",
                        (visible_id,),
                    ).fetchall()))
                events.extend(rows)
            events.sort(key=lambda item: item["cursor"])
            if visible_room_ids:
                marks = ",".join("?" for _ in visible_room_ids)
                executions = self._db.execute(
                    f"SELECT * FROM executions WHERE room_id IN ({marks}) ORDER BY created_at", visible_room_ids
                ).fetchall()
                artifacts = self._db.execute(
                    f"SELECT * FROM artifacts WHERE room_id IN ({marks}) ORDER BY created_at", visible_room_ids
                ).fetchall()
            else:
                executions, artifacts = [], []
        else:
            if visible_room_ids:
                marks = ",".join("?" for _ in visible_room_ids)
                events = self._db.execute(f"SELECT * FROM room_events WHERE room_id IN ({marks}) ORDER BY cursor", visible_room_ids).fetchall()
                executions = self._db.execute(f"SELECT * FROM executions WHERE room_id IN ({marks}) ORDER BY created_at", visible_room_ids).fetchall()
                artifacts = self._db.execute(f"SELECT * FROM artifacts WHERE room_id IN ({marks}) ORDER BY created_at", visible_room_ids).fetchall()
            else:
                events, executions, artifacts = [], [], []
        return {
            "ok": True, "version": 3,
            "groups": [self._group_dto(row) for row in self._db.execute("SELECT * FROM project_groups ORDER BY name")],
            "projects": [self._project_dto(row) for row in self._db.execute("SELECT * FROM projects ORDER BY name")],
            "rooms": [self._room_dto(row) for row in rooms],
            "archivedRooms": [self._room_dto(row) for row in self._db.execute(
                "SELECT * FROM rooms WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC")],
            "executions": [self._execution_dto(row) for row in executions],
            "events": [self._event_dto(row) for row in events],
            "artifacts": [self._artifact_dto(row) for row in artifacts],
            "bots": [self._bot_dto(row) for row in self._db.execute("SELECT * FROM bots WHERE enabled=1 ORDER BY name")],
            "peers": [self._peer_dto(row) for row in self._db.execute("SELECT * FROM peers ORDER BY name")],
            "quotas": [self._quota_dto(row) for row in self._db.execute("SELECT * FROM quota_windows ORDER BY provider,label")],
            "quotaProviders": self._quota_providers(),
            "preferences": self.preferences(),
            "latestCursor": self._db.execute("SELECT COALESCE(MAX(cursor),0) FROM room_events").fetchone()[0],
            "eventPage": event_page,
        }

    def _group_dto(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "instructions": row["instructions"], "createdAt": row["created_at"]}

    def _project_dto(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "groupId": row["group_id"], "name": row["name"], "node": row["node_id"],
                "path": row["path"], "locationKey": row["location_key"], "repoIdentity": row["repo_identity"], "createdAt": row["created_at"]}

    def _room_dto(self, row: sqlite3.Row) -> dict[str, Any]:
        members = [item[0] for item in self._db.execute("SELECT bot_id FROM room_members WHERE room_id=? ORDER BY position", (row["id"],))]
        count = self._db.execute("SELECT COUNT(*) FROM room_events WHERE room_id=?", (row["id"],)).fetchone()[0]
        return {"id": row["id"], "projectId": row["project_id"], "name": row["name"], "alias": row["alias"],
                "participants": members,
                "status": row["status"], "memoryRelease": row["memory_release"], "revision": row["revision"],
                "source": row["source"], "externalSessionId": row["external_session_id"],
                "externalProfile": row["external_profile"], "externalUpdatedAt": row["external_updated_at"],
                "externalActivity": row["external_activity"], "externalActivityAt": row["external_activity_at"],
                "eventCount": count, "createdAt": row["created_at"], "updatedAt": row["updated_at"], "deletedAt": row["deleted_at"]}

    @staticmethod
    def _execution_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "projectId": row["project_id"], "roomId": row["room_id"], "botId": row["bot_id"],
                "prompt": row["prompt"], "status": row["status"], "finalText": row["final_text"], "error": row["error"],
                "createdAt": row["created_at"], "startedAt": row["started_at"], "finishedAt": row["finished_at"]}

    @staticmethod
    def _event_dto(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        text = str(payload.get("text") or payload.get("delta") or "")
        if not text and row["kind"].startswith("tool."):
            # Gateway tool payloads often carry only a structured output; unwrap
            # it into a readable body instead of leaving the card empty.
            for key in ("output", "result"):
                if key in payload:
                    text = _tool_result_display_text(payload[key])
                    break
            if not text and payload.get("args"):
                # Tool-call cards carry their arguments in args, not output.
                text = _format_tool_arguments(payload["args"])
            if not text and payload.get("error"):
                text = str(payload["error"])
        elif row["kind"].startswith("tool.") and text:
            # Historical rows baked the raw JSON envelope into payload.text;
            # unwrap {"output": ...} / multimodal strings at read time. Strings
            # that are not envelopes pass through untouched.
            text = _tool_result_display_text(text)
        dto = {"cursor": row["cursor"], "id": row["id"], "projectId": row["project_id"], "roomId": row["room_id"],
               "executionId": row["execution_id"], "sequence": row["sequence"], "kind": row["kind"], "author": row["author"],
               "payload": payload, "text": text, "createdAt": row["created_at"]}
        if row["kind"] == "message.assistant":
            dto["html"] = markdown_to_html(text)
        return dto

    @staticmethod
    def _artifact_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "projectId": row["project_id"], "roomId": row["room_id"], "executionId": row["execution_id"],
                "eventId": row["event_id"], "author": row["author"], "name": row["name"], "mime": row["mime"], "size": row["size"],
                "sha256": row["sha256"], "createdAt": row["created_at"], "url": f"/v1/artifacts/{row['id']}"}

    @staticmethod
    def _session_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {"roomId": row["room_id"], "botId": row["bot_id"], "nodeId": row["node_id"],
                "storedSessionId": row["stored_session_id"], "runtimeSessionId": row["runtime_session_id"],
                "releaseState": row["lifecycle_state"], "updatedAt": row["updated_at"]}

    @staticmethod
    def _bot_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "description": row["description"], "profile": row["profile"],
                "nodeId": row["node_id"], "model": row["model"], "provider": row["provider"],
                "faceShape": row["face_shape"], "colorRole": row["color_role"]}

    @staticmethod
    def _peer_dto(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "endpoint": row["endpoint"], "status": row["status"],
                "sourceName": row["source_name"] or row["name"], "colorRole": row["color_role"],
                "capabilities": json.loads(row["capabilities"]), "lastSeenAt": row["last_seen_at"], "updatedAt": row["updated_at"]}

    def _quota_providers(self) -> list[dict[str, Any]]:
        windows: dict[str, list[dict[str, Any]]] = {}
        for row in self._db.execute("SELECT * FROM quota_windows ORDER BY provider,window_seconds,label"):
            windows.setdefault(row["provider"], []).append(self._quota_dto(row))
        return [
            {"provider": row["provider"], "hourTokens": row["hour_tokens"], "weekTokens": row["week_tokens"],
             "hourCalls": row["hour_calls"], "weekCalls": row["week_calls"], "plan": row["plan"],
             "details": json.loads(row["details"]), "status": row["status"], "updatedAt": row["updated_at"],
             "windows": windows.get(row["provider"], [])}
            for row in self._db.execute("SELECT * FROM quota_providers ORDER BY provider")
        ]

    @staticmethod
    def _quota_dto(row: sqlite3.Row) -> dict[str, Any]:
        seconds = row["window_seconds"]
        return {"provider": row["provider"], "label": row["label"], "availablePercent": row["available_percent"],
                "usedPercent": row["used_percent"], "resetAt": row["reset_at"], "windowSeconds": seconds,
                "dailySeparators": max(0, round(seconds / 86400) - 1) if seconds and seconds >= 86400 else 0,
                "status": row["status"], "updatedAt": row["updated_at"]}


def _tool_result_display_text(value: Any) -> str:
    """Human-readable tool output, mirroring the Hermes CLI multimodal summary.

    Unwraps {"output": ...} envelopes, multimodal content lists (text parts or
    text_summary), and keeps anything else as pretty JSON instead of a raw
    single-line dump.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError):
                return value
            if isinstance(decoded, dict) and ({"output", "_multimodal", "text_summary"} & set(decoded.keys())):
                return _tool_result_display_text(decoded)
        return value
    if isinstance(value, dict):
        if value.get("_multimodal") is True and isinstance(value.get("content"), list):
            if value.get("text_summary"):
                return str(value["text_summary"])
            parts = [str(p.get("text", "")) for p in value["content"]
                     if isinstance(p, dict) and p.get("type") == "text" and str(p.get("text", "")).strip()]
            return "\n".join(parts) if parts else "[multimodal tool result]"
        if "output" in value:
            return _tool_result_display_text(value["output"])
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(value)


def _format_tool_arguments(args: Any) -> str:
    """Readable body for a tool-call card: bare command when there is one,
    pretty JSON otherwise (matches how the CLI previews calls)."""
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        command = args.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
    try:
        return json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(args)


def sanitize_telemetry(value: Any) -> Any:
    """Remove credentials while retaining observable tool activity."""
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if re.search(r"(?i)(password|secret|token|api.?key|authorization|cookie)", str(key))
            else sanitize_telemetry(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_telemetry(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+\S+", "[redacted]", value)
        text = re.sub(r"\b(?:sk-|gh[pousr]_|github_pat_|AKIA|AIza|xox[baprs]-)[A-Za-z0-9_-]+", "[redacted]", text)
        return text[-100_000:]
    return value


def _escape_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


_SAFE_LINK_RE = re.compile(r"^(https?://|mailto:)\S+$", re.I)


def _inline_markdown(text: str) -> str:
    """Render inline markdown on already-escaped text, emitting theme placeholders."""
    escaped = _escape_html(text)
    code_spans: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_spans.append(
            '<code style="font-family:monospace; color:__ARES_ACCENT__">'
            + match.group(1) + "</code>")
        return "\x00CODE%d\x00" % (len(code_spans) - 1)

    escaped = re.sub(r"`([^`\n]+)`", stash_code, escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", escaped)
    escaped = re.sub(r"__([^_]+)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", escaped)
    escaped = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", escaped)

    def link_sub(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2).strip()
        if not _SAFE_LINK_RE.match(url):
            return label
        return '<a href="%s"><font color="__ARES_ACCENT__">%s</font></a>' % (url, label)

    escaped = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link_sub, escaped)
    for index, span in enumerate(code_spans):
        escaped = escaped.replace("\x00CODE%d\x00" % index, span)
    return escaped


def _table_row_cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def markdown_to_html(source: str) -> str:
    """Convert assistant markdown to Qt-RichText-safe HTML with theme placeholders.

    The source is escaped before any tag is generated, so raw HTML in model
    output can never inject active content. Colors are emitted as __ARES_*__
    placeholders resolved live by the QML transcript palette.
    """
    if not source or not source.strip():
        return ""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + _inline_markdown(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped == "":
            flush_paragraph()
            index += 1
            continue
        if stripped.startswith("```"):
            flush_paragraph()
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            index += 1  # consume the closing fence (or EOF)
            code = _escape_html("\n".join(code_lines))
            out.append(
                '<table width="100%" style="background-color:__ARES_CODE_BG__" cellspacing="0" cellpadding="4">'
                '<tr><td><pre style="margin:0; font-family:monospace">' + code
                + "</pre></td></tr></table>")
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            out.append("<h%d>" % level + _inline_markdown(heading.group(2)) + "</h%d>" % level)
            index += 1
            continue
        if re.match(r"^([-*_])(\s*\1){2,}\s*$", stripped):
            flush_paragraph()
            out.append("<hr/>")
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) \
                and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[index + 1]):
            flush_paragraph()
            headers = _table_row_cells(stripped)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_table_row_cells(lines[index]))
                index += 1
            cells = "".join(
                "<td>" + _inline_markdown(cell) + "</td>"
                for cell in headers)
            body = "".join(
                "<tr>" + "".join("<td>" + _inline_markdown(cell) + "</td>" for cell in row) + "</tr>"
                for row in rows)
            out.append('<table width="100%" cellspacing="0" cellpadding="3">'
                       '<tr bgcolor="__ARES_SURFACE__">' + cells + "</tr>" + body + "</table>")
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet or numbered:
            flush_paragraph()
            ordered = numbered is not None
            pattern = r"^\d+[.)]\s+(.*)$" if ordered else r"^[-*]\s+(.*)$"
            items: list[str] = []
            while index < len(lines):
                match = re.match(pattern, lines[index].strip())
                if not match:
                    break
                items.append(match.group(1))
                index += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>" % tag + "".join("<li>" + _inline_markdown(item) + "</li>" for item in items)
                       + "</%s>" % tag)
            continue
        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            flush_paragraph()
            quotes: list[str] = []
            while index < len(lines):
                match = re.match(r"^>\s?(.*)$", lines[index].strip())
                if not match:
                    break
                quotes.append(match.group(1))
                index += 1
            out.append('<p style="margin-left:10; font-style:italic; color:__ARES_FG_DIM__">'
                       + _inline_markdown(" ".join(quotes)) + "</p>")
            continue
        paragraph.append(stripped)
        index += 1
    flush_paragraph()
    return "\n".join(out)


_MEDIA_PATH_RE = re.compile(r"(/[A-Za-z0-9_./+@%=-]+\.(?:png|jpe?g|gif|webp|bmp|mp4|webm|mov|mkv))\b", re.I)


def discover_media_paths(payload: Any, allowed_roots: list[Path]) -> list[Path]:
    """Return existing media paths contained by explicit project/session roots."""
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    candidates: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            candidates.extend(match.group(1) for match in _MEDIA_PATH_RE.finditer(value))

    walk(payload)
    discovered: list[Path] = []
    seen: set[Path] = set()
    for raw in candidates:
        try:
            path = Path(raw).expanduser().resolve(strict=True)
            if path in seen or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
            if not any(path == root or root in path.parents for root in roots):
                continue
        except OSError:
            continue
        seen.add(path)
        discovered.append(path)
    return discovered


class InterventionManager:
    """Resolve exact Hermes intervention requests owned by a room execution."""

    def __init__(self, store: AresStore, bridge: Any):
        self.store = store
        self.bridge = bridge

    def approve(self, execution_id: str, request_id: str, choice: str) -> dict[str, Any]:
        execution = self.store.execution(execution_id)
        request_id = require_text(request_id, "approval request ID", 256)
        choice = require_text(choice, "approval choice", 32)
        if choice not in {"once", "always", "deny"}:
            raise ValueError("approval choice is invalid")
        binding = self.store._db.execute(
            "SELECT * FROM session_bindings WHERE room_id=? AND bot_id=? ORDER BY updated_at DESC LIMIT 1",
            (execution["roomId"], execution["botId"]),
        ).fetchone()
        if binding is None or not binding["runtime_session_id"]:
            raise ValueError("execution has no live Hermes session")
        params = {"session_id": binding["runtime_session_id"], "request_id": request_id, "choice": choice, "all": False}
        response = self.bridge.call("approval.respond", params, timeout=30)
        resolved = bool(response.get("ok") and (response.get("result") or {}).get("resolved"))
        if not resolved:
            raise ValueError(str(response.get("error") or "approval was not resolved"))
        self.store.append_event(
            execution["projectId"], execution["roomId"], execution_id, "approval.responded", "user",
            {"text": f"Approval response: {choice}", "requestId": request_id, "choice": choice},
        )
        return {"resolved": True, "requestId": request_id, "choice": choice}


class LifecycleManager:
    """Release room-owned live sessions and report each outcome."""

    def __init__(self, store: AresStore, bridge: Any):
        self.store = store
        self.bridge = bridge

    def pause_room(self, project_id: str, room_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        requested = self.store.pause_room(project_id, room_id, idempotency_key or ident("pause"))
        outcomes: list[dict[str, Any]] = []
        for session in requested["sessions"]:
            if session["releaseState"] == "released":
                outcomes.append(session)
                continue
            runtime_id = session.get("runtimeSessionId")
            node_id = session.get("nodeId") or "local"
            if node_id != "local":
                release_state, detail = "pending_remote", "Remote session release awaits an authenticated peer adapter"
            elif not runtime_id:
                release_state, detail = "released", "No live runtime session was allocated"
            else:
                with contextlib.suppress(Exception):
                    self.bridge.call("session.interrupt", {"session_id": runtime_id}, timeout=10)
                try:
                    response = self.bridge.call("session.close", {"session_id": runtime_id}, timeout=20)
                    closed = bool(response.get("ok") and (response.get("result") or {}).get("closed"))
                    if closed:
                        release_state, detail = "released", "Hermes confirmed session teardown"
                    else:
                        active_response = self.bridge.call("session.active_list", {}, timeout=10)
                        active_sessions = (active_response.get("result") or {}).get("sessions") or []
                        still_active = any(
                            runtime_id in {str(item.get("session_id") or ""), str(item.get("runtime_session_id") or ""), str(item.get("id") or "")}
                            for item in active_sessions if isinstance(item, dict)
                        )
                        if active_response.get("ok") and not still_active:
                            release_state, detail = "released", "Hermes confirmed the runtime session was already absent"
                        else:
                            release_state, detail = "failed", str(response.get("error") or "Hermes did not confirm teardown")
                except Exception as exc:
                    release_state, detail = "failed", str(exc)
            outcomes.append({"botId": session["botId"], "nodeId": node_id, "releaseState": release_state, "detail": detail})
        return self.store.record_pause_outcomes(project_id, room_id, outcomes)


class HermesBroker:
    """Persistent private NDJSON bridge to the authenticated Hermes Desktop WS."""

    def __init__(self, helper_path: Path | None = None):
        self.helper_path = helper_path or Path(__file__).with_name("hermes_desktop_rpc.py")
        self._process: subprocess.Popen[str] | None = None
        self._responses: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._listeners: list[Any] = []
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                return
            if not self.helper_path.is_file():
                raise RuntimeError("Hermes Desktop RPC helper is not installed")
            self._process = subprocess.Popen(
                [os.environ.get("PYTHON", "python3"), "-I", str(self.helper_path), "--broker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            threading.Thread(target=self._read, daemon=True, name="ares-hermes-events").start()

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def add_listener(self, listener: Any) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Any) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _read(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("event"), dict):
                with self._lock:
                    listeners = list(self._listeners)
                for listener in listeners:
                    try:
                        listener(item["event"])
                    except Exception:
                        continue
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                with self._lock:
                    target = self._responses.get(item["id"])
                if target:
                    target.put(item)
        with self._lock:
            waiting = list(self._responses.values())
        for target in waiting:
            target.put({"ok": False, "error": "Hermes Desktop connection closed"})

    def call(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        self.start()
        request_id = uuid.uuid4().hex
        target: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            process = self._process
            if process is None or process.stdin is None or process.poll() is not None:
                return {"ok": False, "error": "Hermes Desktop broker unavailable"}
            self._responses[request_id] = target
            try:
                process.stdin.write(compact({"id": request_id, "method": method, "params": params}) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._responses.pop(request_id, None)
                return {"ok": False, "error": "Hermes Desktop broker unavailable"}
        try:
            return target.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": "Hermes Desktop RPC timed out; outcome unknown"}
        finally:
            with self._lock:
                self._responses.pop(request_id, None)


class HermesLiveTranscriptMirror:
    """Project gateway token deltas into the matching imported room in real time."""

    def __init__(self, store: AresStore):
        self.store = store
        self._lock = threading.RLock()
        self._routes: dict[str, tuple[str, str]] = {}
        self._streams: dict[str, dict[str, Any]] = {}
        self._live_by_room: dict[str, dict[str, tuple[dict[str, Any], float | None]]] = {}
        self._now = time.monotonic

    def _remember(self, event: dict[str, Any] | None, complete: bool = False) -> None:
        if not event:
            return
        room_id = str(event.get("roomId") or "")
        event_id = str(event.get("id") or "")
        if room_id and event_id:
            expires = self._now() + (5 if complete else 30)
            self._live_by_room.setdefault(room_id, {})[event_id] = (event, expires)

    def live_events(self, room_id: str) -> list[dict[str, Any]]:
        with self._lock:
            current = self._now()
            cache = self._live_by_room.get(room_id, {})
            expired = [event_id for event_id, (_event, expires) in cache.items()
                       if expires is not None and expires <= current]
            for event_id in expired:
                cache.pop(event_id, None)
            # The DB is the authority: never serve a cached entry whose ephemeral
            # row was already reconciled away by the transcript sync.
            valid_ids = {str(event.get("id") or "") for event in self.store.live_events(room_id)}
            return [item[0] for item in sorted(cache.values(), key=lambda item: (
                float(item[0].get("createdAt") or 0), int(item[0].get("sequence") or 0),
            )) if str(item[0].get("id") or "") in valid_ids]

    def update_active_sessions(self, profile: str, sessions: list[dict[str, Any]]) -> None:
        routes: dict[str, tuple[str, str]] = {}
        for session in sessions:
            if not isinstance(session, dict):
                continue
            runtime_id = str(session.get("id") or session.get("session_id") or "").strip()
            session_key = str(session.get("session_key") or session.get("stored_session_id") or "").strip()
            if runtime_id and session_key:
                routes[runtime_id] = (profile, session_key)
        with self._lock:
            self._routes = {sid: route for sid, route in self._routes.items() if route[0] != profile}
            self._routes.update(routes)
            active_ids = set(self._routes)
            self._streams = {sid: stream for sid, stream in self._streams.items() if sid in active_ids}

    @staticmethod
    def _event_id(session_id: str, generation: str, facet: str) -> str:
        digest = hashlib.sha256(f"{session_id}\0{generation}\0{facet}".encode()).hexdigest()
        return f"hermes_live_{digest}"

    def observe(self, event: dict[str, Any]) -> None:
        runtime_id = str(event.get("session_id") or "")
        kind = str(event.get("type") or "")
        if not runtime_id or kind not in {
            "message.start", "message.delta", "message.interim", "message.complete",
            "reasoning.delta", "thinking.delta",
        }:
            return
        with self._lock:
            route = self._routes.get(runtime_id)
            if route is None:
                return
            payload_value = event.get("payload")
            payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
            if kind == "message.start" or runtime_id not in self._streams:
                generation = str(payload.get("message_id") or payload.get("id") or time.time_ns())
                profile, session_key = route
                keep = (
                    self._event_id(runtime_id, f"{generation}:0", "message"),
                    self._event_id(runtime_id, f"{generation}:0", "reasoning"),
                )
                self.store.clear_live_hermes_events(profile, session_key, keep=keep)
                room_row = self.store._db.execute(
                    "SELECT id FROM rooms WHERE source='hermes' AND external_profile=? "
                    "AND external_session_id=? AND deleted_at IS NULL",
                    (profile, session_key),
                ).fetchone()
                if room_row is not None:
                    cache = self._live_by_room.get(str(room_row["id"]), {})
                    for cached_id in list(cache.keys()):
                        if cached_id not in keep:
                            cache.pop(cached_id, None)
                self._streams[runtime_id] = {
                    "generation": generation, "segment": 0, "text": "", "thinking": False,
                }
            stream = self._streams[runtime_id]
            profile, session_key = route
            generation = f"{stream['generation']}:{stream['segment']}"
            message_id = self._event_id(runtime_id, generation, "message")
            reasoning_id = self._event_id(runtime_id, generation, "reasoning")
            if kind in {"reasoning.delta", "thinking.delta"}:
                stream["thinking"] = True
                self._remember(self.store.upsert_live_hermes_event(profile, session_key, reasoning_id, "reasoning.status", {
                    "text": "Thinking…", "streaming": True, "live": True,
                    "externalSessionId": session_key, "sourceOrder": 0,
                }))
                return
            text = str(payload.get("text") or payload.get("delta") or "")
            if kind == "message.delta":
                stream["text"] += text
            elif text:
                stream["text"] = text
            if stream["thinking"]:
                self._remember(self.store.upsert_live_hermes_event(profile, session_key, reasoning_id, "reasoning.status", {
                    "text": "Thought", "streaming": False, "live": False,
                    "externalSessionId": session_key, "sourceOrder": 0,
                }), complete=True)
                stream["thinking"] = False
            if stream["text"]:
                complete = kind in {"message.interim", "message.complete"}
                self._remember(self.store.upsert_live_hermes_event(profile, session_key, message_id, "message.assistant", {
                    "text": stream["text"], "streaming": not complete, "live": not complete,
                    "externalSessionId": session_key, "sourceOrder": 1,
                }), complete=complete)
            if kind == "message.interim":
                stream["segment"] += 1
                stream["text"] = ""
                stream["thinking"] = False
            elif kind == "message.complete":
                stream["text"] = ""


def sync_hermes_rooms(store: AresStore, bridge: Any, profiles: list[dict[str, Any]]) -> dict[str, int]:
    """Discover public Hermes sessions and mirror them under their authoritative project paths."""
    names = list(dict.fromkeys(
        str(item.get("name") or item.get("profile") or "").strip()
        for item in profiles if isinstance(item, dict)
    ))
    names = [name for name in names if name] or ["default"]
    project_count = 0
    room_count = 0
    for profile in names:
        tree_response = bridge.call(
            "projects.tree", {"profile": profile, "preview_limit": 0, "session_limit": 5000}, timeout=30)
        if tree_response.get("ok") is not True:
            continue
        for project in (tree_response.get("result") or {}).get("projects") or []:
            if (not isinstance(project, dict) or int(project.get("sessionCount") or 0) < 1):
                continue
            hydrated = bridge.call(
                "projects.project_sessions",
                {"profile": profile, "project_id": str(project.get("id") or ""), "session_limit": 5000},
                timeout=30,
            )
            value = (hydrated.get("result") or {}).get("project") if hydrated.get("ok") is True else None
            if not isinstance(value, dict):
                continue
            project_count += 1
            if project.get("isNoProject"):
                # The Desktop "Home" bucket holds every session not bound to a
                # code project; mirror it under a stable virtual project so
                # human<->bot chats started from a plain terminal still appear.
                value["label"] = str(project.get("label") or value.get("label") or "Home")[:256]
                value["path"] = "hermes://home"
                room_count += store.sync_hermes_project(profile, value, virtual=True)
            else:
                room_count += store.sync_hermes_project(profile, value)
    return {"profiles": len(names), "projects": project_count, "rooms": room_count}


def sync_remote_hermes_rooms(store: AresStore, sessions: list[dict[str, Any]]) -> dict[str, int]:
    """Mirror peer sessions under one synthetic, peer-owned project per host."""
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        peer = str(session.get("peer") or "").strip()
        session_id = str(session.get("id") or "").strip()
        if not peer or not session_id or not session_id.startswith(f"remote::{peer}::"):
            continue
        identity = str(session.get("profile") or f"{peer}/default")
        profile = identity.split("/", 1)[1] if identity.startswith(peer + "/") else "default"
        profile_key = f"remote::{peer}::{profile}"
        project_title = str(session.get("agentTitle") or ("Hermes" if profile == "default" else profile.title())).strip()
        project_title = project_title[:256] or profile
        grouped.setdefault((peer, profile, project_title, profile_key), []).append({
            "id": session_id,
            "title": str(session.get("title") or session_id),
            "source": "peer",
            "last_active": float(session.get("lastActivityTs") or 0),
        })
    peers: set[str] = set()
    created = 0
    for (peer, profile, project_title, profile_key), rows in grouped.items():
        peers.add(peer)
        project_path = f"peer://{peer}/{profile}"
        if profile == "default":
            legacy_key = hashlib.sha256(f"{peer}\0peer://{peer}".encode()).hexdigest()
            project_key = hashlib.sha256(f"{peer}\0{project_path}".encode()).hexdigest()
            with store._transaction() as db:
                legacy = db.execute(
                    "SELECT id FROM projects WHERE location_key=?",
                    (legacy_key,),
                ).fetchone()
                target = db.execute(
                    "SELECT id FROM projects WHERE location_key=?",
                    (project_key,),
                ).fetchone()
                if legacy is not None and target is None:
                    db.execute(
                        "UPDATE projects SET name=?,path=?,location_key=? WHERE id=?",
                        (project_title, project_path, project_key, legacy["id"]),
                    )
                elif legacy is not None and target is not None:
                    for table in ("rooms", "executions", "room_events", "artifacts"):
                        db.execute(
                            f"UPDATE {table} SET project_id=? WHERE project_id=?",
                            (target["id"], legacy["id"]),
                        )
                    remaining = sum(
                        int(db.execute(f"SELECT count(*) FROM {table} WHERE project_id=?", (legacy["id"],)).fetchone()[0])
                        for table in ("rooms", "executions", "room_events", "artifacts")
                    )
                    if remaining == 0:
                        db.execute("DELETE FROM projects WHERE id=?", (legacy["id"],))
        project = {
            "label": project_title,
            "path": project_path,
            "repos": [{"groups": [{"sessions": rows}]}],
        }
        created += store.sync_hermes_project(profile_key, project, node_id=peer)
    return {"peers": len(peers), "rooms": created}


def sync_remote_hermes_chat(store: AresStore, chat: dict[str, Any]) -> int:
    """Hydrate one mirrored peer room from the peer API's safe transcript."""
    reference = str(chat.get("id") or "").strip()
    parts = reference.split("::", 3)
    if len(parts) != 4 or parts[0] != "remote" or not all(parts[1:]):
        return 0
    peer, profile = parts[1], parts[2]
    messages: list[dict[str, Any]] = []
    for source in chat.get("messages") or []:
        if not isinstance(source, dict):
            continue
        message = dict(source)
        if message.get("toolName") and not message.get("tool_name"):
            message["tool_name"] = message["toolName"]
        if message.get("thinking") and not message.get("reasoning_content"):
            message["reasoning_content"] = message["thinking"]
        messages.append(message)
    return store.sync_hermes_transcript(
        f"remote::{peer}::{profile}",
        reference,
        messages,
        {
            "last_activity_at": chat.get("lastActivityTs"),
            "last_activity_description": chat.get("activity") or "",
        },
    )


def sync_recent_hermes_transcripts(store: AresStore, bridge: Any, limit: int = 8) -> dict[str, int]:
    """Refresh active chats first, then the most recently discovered persisted chats."""
    row_limit = max(1, min(int(limit), 32))
    with store._lock:
        candidates = store._db.execute(
            "SELECT r.external_profile,r.external_session_id,r.external_activity_at,r.external_transcript_version,"
            "EXISTS(SELECT 1 FROM room_events e WHERE e.room_id=r.id AND e.kind IN ('message.user','message.assistant')) AS has_messages,"
            "COALESCE((SELECT MAX(e.created_at) FROM room_events e WHERE e.room_id=r.id "
            "AND json_extract(e.payload,'$.externalMessageId') IS NOT NULL),0) AS transcript_at "
            "FROM rooms r "
            "WHERE r.source='hermes' AND r.deleted_at IS NULL AND r.external_session_id<>'' "
            "AND r.external_profile NOT LIKE 'remote::%' "
            "ORDER BY COALESCE(r.external_updated_at,r.updated_at) DESC LIMIT 128",
        ).fetchall()
    active_keys: set[tuple[str, str]] = set()
    for profile in sorted({str(row["external_profile"] or "default") for row in candidates}):
        try:
            response = bridge.call("session.active_list", {"profile": profile}, timeout=10)
        except Exception:
            continue
        if response.get("ok") is not True:
            continue
        for session in (response.get("result") or {}).get("sessions") or []:
            if not isinstance(session, dict):
                continue
            session_key = str(session.get("session_key") or "")
            if session_key:
                active_keys.add((profile, session_key))
    rows = sorted(
        candidates,
        key=lambda row: (
            (str(row["external_profile"] or "default"), str(row["external_session_id"])) not in active_keys,
        ),
    )[:row_limit]
    synced = 0
    inserted = 0
    for row in rows:
        response = bridge.call("session.messages", {
            "profile": row["external_profile"] or "default",
            "session_id": row["external_session_id"],
            "limit": 500,
            "since": float(row["transcript_at"] or 0)
                if row["has_messages"] and int(row["external_transcript_version"] or 0) >= 2 else 0,
        }, timeout=20)
        if response.get("ok") is not True:
            continue
        result = response.get("result") or {}
        messages = result.get("messages")
        if not isinstance(messages, list):
            continue
        inserted += store.sync_hermes_transcript(
            row["external_profile"] or "default", row["external_session_id"], messages,
            result.get("session") if isinstance(result.get("session"), dict) else {},
        )
        synced += 1
    return {"rooms": synced, "messages": inserted}


class ExecutionRunner:
    """One room-owned Hermes turn whose observable activity is durable first."""

    def __init__(self, store: AresStore, bridge: Any, poll_interval: float = 0.5, deadline_seconds: float = 1200):
        self.store = store
        self.bridge = bridge
        self.poll_interval = poll_interval
        self.deadline_seconds = deadline_seconds

    def run_execution(self, execution_id: str) -> dict[str, Any]:
        execution = self.store.execution(execution_id)
        project = self.store._project(execution["projectId"])
        bot = self.store._db.execute("SELECT * FROM bots WHERE id=?", (execution["botId"],)).fetchone()
        if bot is None:
            return self.store.finish_execution(execution_id, "failed", error="Bot is not configured")
        profile = bot["profile"]
        title = f"Ares: {execution['roomId']} · {execution['botId']}"
        binding = self.store._db.execute(
            "SELECT stored_session_id FROM session_bindings WHERE room_id=? AND bot_id=? AND node_id=?",
            (execution["roomId"], execution["botId"], bot["node_id"]),
        ).fetchone()
        resume_id = str(binding["stored_session_id"] or "") if binding else title
        runtime_id = ""
        stored_id = resume_id
        accumulated = ""
        accumulated_reasoning = ""
        gateway_event = 0
        captured_media: set[Path] = set()

        def observe(event: dict[str, Any]) -> None:
            nonlocal accumulated, accumulated_reasoning, gateway_event
            if not runtime_id or event.get("session_id") != runtime_id:
                return
            kind = str(event.get("type") or "")
            if kind not in {
                "message.delta", "reasoning.delta", "thinking.delta", "tool.start", "tool.generating",
                "tool.progress", "tool.complete", "tool.output_risk", "status", "approval.request", "clarify.request",
                "error",
            }:
                return
            gateway_event += 1
            payload = sanitize_telemetry(event.get("payload") or {})
            if kind == "message.delta":
                delta = str(payload.get("text") or payload.get("delta") or "")
                accumulated += delta
                if accumulated:
                    self.store.upsert_execution_event(
                        execution["projectId"], execution["roomId"], execution_id,
                        f"gw_stream_{execution_id}_message", "message.assistant", execution["botId"],
                        {"text": accumulated, "streaming": True},
                    )
                return
            if kind in {"reasoning.delta", "thinking.delta"}:
                accumulated_reasoning += str(payload.get("text") or payload.get("delta") or "")
                if accumulated_reasoning.strip():
                    self.store.upsert_execution_event(
                        execution["projectId"], execution["roomId"], execution_id,
                        f"gw_stream_{execution_id}_reasoning", "reasoning.status", execution["botId"],
                        {"text": accumulated_reasoning, "streaming": True},
                    )
                return
            if kind in {"tool.generating", "tool.progress"}:
                tool_key = str(payload.get("toolName") or payload.get("name") or payload.get("tool") or "tool")
                self.store.upsert_execution_event(
                    execution["projectId"], execution["roomId"], execution_id,
                    f"gw_tool_{execution_id}_{tool_key}", kind, execution["botId"],
                    {**payload, "streaming": kind == "tool.progress"},
                )
                return
            stable_id = f"gw_{hashlib.sha256(f'{execution_id}:{gateway_event}:{kind}:{compact(payload)}'.encode()).hexdigest()}"
            persisted = self.store.append_event(execution["projectId"], execution["roomId"], execution_id,
                                                kind, execution["botId"], payload, stable_id=stable_id)
            if kind == "tool.complete":
                for media_path in discover_media_paths(payload, [Path(project["path"])]):
                    if media_path in captured_media:
                        continue
                    captured_media.add(media_path)
                    mime = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
                    self.store.store_artifact(
                        execution["projectId"], execution["roomId"], media_path.name, mime,
                        media_path.read_bytes(), execution["botId"], event_id=persisted["id"],
                    )

        self.bridge.add_listener(observe)
        try:
            self.store.start_execution(execution_id)
            resumed = self.bridge.call("session.resume", {"session_id": resume_id, "profile": profile})
            if resumed.get("ok") is not True and resumed.get("code") == 4007 and not binding:
                resumed = self.bridge.call("session.create", {
                    "profile": profile, "title": title, "source": "ares", "hidden": True,
                    "cwd": project["path"], "room_plumbing": True, "follow_profile_config": True,
                    "close_on_disconnect": False,
                })
            if resumed.get("ok") is not True:
                return self.store.finish_execution(execution_id, "failed", error=str(resumed.get("error") or "Hermes session unavailable"))
            state = resumed.get("result") or {}
            runtime_id = str(state.get("session_id") or "")
            stored_id = str(state.get("session_key") or state.get("stored_session_id") or runtime_id or title)
            if not runtime_id:
                return self.store.finish_execution(execution_id, "failed", error="Hermes runtime session unavailable")
            before = len(state.get("messages") or [])
            self.store.bind_session(execution["roomId"], execution["botId"], bot["node_id"], stored_id, runtime_id)
            attachment_refs: list[str] = []
            for artifact in self.store.execution_artifacts(execution_id):
                is_image = str(artifact["mime"]).startswith("image/")
                method = "image.attach_bytes" if is_image else "file.attach"
                params = {"session_id": runtime_id, "path": artifact["path"], "name": artifact["name"]}
                if is_image:
                    params = {
                        "session_id": runtime_id,
                        "content_base64": base64.b64encode(Path(artifact["path"]).read_bytes()).decode("ascii"),
                        "filename": artifact["name"],
                    }
                attached = self.bridge.call(
                    method, params,
                    timeout=60,
                )
                result = attached.get("result") or {}
                accepted = bool(attached.get("ok") and result.get("attached"))
                if accepted:
                    detail = f"Hermes accepted {artifact['name']} via {method}"
                    ref = str(result.get("ref_text") or result.get("text") or "").strip()
                    if ref:
                        attachment_refs.append(ref)
                    self.store.set_artifact_delivery(execution_id, artifact["artifactId"], "delivered", detail)
                else:
                    detail = str(attached.get("error") or result.get("message") or f"Hermes rejected {artifact['name']}")
                    self.store.set_artifact_delivery(execution_id, artifact["artifactId"], "failed", detail)
            reference_text = ("\nAttachments:\n" + "\n".join(attachment_refs)) if attachment_refs else ""
            group_instructions = ""
            if project["group_id"]:
                group = self.store._db.execute("SELECT instructions FROM project_groups WHERE id=?", (project["group_id"],)).fetchone()
                if group and str(group["instructions"]).strip():
                    group_instructions = f"\nShared project-group instructions:\n{str(group['instructions']).strip()}\n"
            working_dir = ""
            if not str(project["path"]).startswith("hermes://"):
                working_dir = f"Working project directory: {project['path']}\n"
            project_context = f"{working_dir}{group_instructions}{reference_text}\n\n{execution['prompt']}"
            submitted = self.bridge.call("prompt.submit", {"session_id": runtime_id, "text": project_context})
            if submitted.get("ok") is not True:
                return self.store.finish_execution(execution_id, "failed", error=str(submitted.get("error") or "Hermes prompt rejected"))
            deadline = time.monotonic() + self.deadline_seconds
            while time.monotonic() < deadline:
                if self.poll_interval:
                    time.sleep(self.poll_interval)
                resumed = self.bridge.call("session.resume", {"session_id": stored_id, "profile": profile})
                if resumed.get("ok") is not True:
                    return self.store.finish_execution(execution_id, "failed", error=str(resumed.get("error") or "Hermes session lost"))
                state = resumed.get("result") or {}
                inflight = state.get("inflight")
                if isinstance(inflight, dict) and str(inflight.get("status") or "").lower() in {"error", "failed", "cancelled", "interrupted"}:
                    return self.store.finish_execution(execution_id, "failed", error=str(inflight.get("error") or inflight.get("message") or "Hermes provider failed"))
                inflight_active = inflight is True or (isinstance(inflight, dict) and str(inflight.get("status") or "").lower() in {"queued", "running", "working", "streaming"})
                if state.get("running") or inflight_active or state.get("pending_approval") or state.get("pending_clarify"):
                    continue
                messages = state.get("messages") or []
                final = ""
                for message in reversed(messages[before:]):
                    if message.get("role") != "assistant":
                        continue
                    content = message.get("content", message.get("text", ""))
                    if isinstance(content, list):
                        content = "".join(part if isinstance(part, str) else str(part.get("text") or "") for part in content)
                    final = str(content or "").strip()
                    break
                if len(messages) <= before and not accumulated:
                    continue
                if final:
                    self.store.append_event(execution["projectId"], execution["roomId"], execution_id,
                                            "message.assistant", execution["botId"], {"text": final, "reconcilesDeltas": True})
                return self.store.finish_execution(execution_id, "completed", text=final or accumulated)
            return self.store.finish_execution(execution_id, "failed", error="Hermes execution timed out")
        except Exception as error:
            return self.store.finish_execution(execution_id, "failed", error=f"Hermes execution failed: {type(error).__name__}")
        finally:
            self.bridge.remove_listener(observe)
            self.store.clear_ephemeral_execution_events(execution["roomId"], execution_id)


def create_http_server(store: AresStore, host: str, port: int, token: str, web_root: Path,
                       bridge: Any | None = None, live_events_provider: Any | None = None) -> http.server.ThreadingHTTPServer:
    """Build the shared desktop/mobile HTTP authority."""
    token = require_text(token, "access token", 512)
    root = Path(web_root).resolve()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "Ares/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return supplied.startswith("Bearer ") and hashlib.sha256(supplied[7:].encode()).digest() == hashlib.sha256(token.encode()).digest()

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = compact(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"ok": False, "error": message})

        def _require_api_auth(self) -> bool:
            if self._authorized():
                return True
            self._error(401, "authentication required")
            return False

        def _read_json(self, maximum: int = 2 * 1024 * 1024) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > maximum:
                raise ValueError("request size is invalid")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/v1/health":
                self._json(200, {"ok": True, "service": "ares"})
                return
            if parsed.path.startswith("/v1/"):
                if not self._require_api_auth():
                    return
                try:
                    query = parse_qs(parsed.query)
                    if parsed.path == "/v1/models":
                        self._json(200, {"ok": True, "models": models_catalog(store)})
                        return
                    if parsed.path == "/v1/state":
                        room_id = (query.get("roomId") or [None])[0]
                        event_room_id = (query.get("eventRoomId") or [None])[0]
                        if (query.get("compact") or [""])[0] == "1" and event_room_id is None:
                            event_room_id = ""
                        self._json(200, store.snapshot(room_id=room_id, event_room_id=event_room_id))
                        return
                    if parsed.path == "/v1/events":
                        before_value = (query.get("before") or [None])[0]
                        room_id = (query.get("roomId") or [None])[0]
                        limit = int((query.get("limit") or [str(TRANSCRIPT_PAGE_EVENTS)])[0])
                        if before_value is not None:
                            if not room_id:
                                raise ValueError("roomId is required for historical events")
                            self._json(200, {"ok": True, **store.events_before(room_id, int(before_value), limit)})
                            return
                        after = int((query.get("after") or ["0"])[0])
                        self._json(200, {"ok": True, **store.events_after(after, limit, room_id)})
                        return
                    if parsed.path == "/v1/live":
                        room_id = str((query.get("roomId") or [""])[0])
                        if not room_id:
                            raise ValueError("roomId is required")
                        events = live_events_provider(room_id) if live_events_provider else store.live_events(room_id)
                        self._json(200, {"ok": True, "events": events})
                        return
                    if parsed.path == "/v1/drafts":
                        device = str((query.get("device") or ["desktop"])[0])
                        self._json(200, {"ok": True, "drafts": store.device_drafts(device)})
                        return
                    if parsed.path.startswith("/v1/artifacts/"):
                        self._artifact(parsed.path.rsplit("/", 1)[-1])
                        return
                    self._error(404, "not found")
                except (ValueError, OSError) as error:
                    self._error(400, str(error))
                return
            self._static(parsed.path)

        def _artifact(self, artifact_id: str) -> None:
            row = store._db.execute("SELECT name,mime,size FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if row is None:
                self._error(404, "artifact does not exist")
                return
            start, end, partial = None, None, False
            header = self.headers.get("Range", "")
            if header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
                if not match or not match.group(1):
                    self._error(416, "invalid range")
                    return
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else None
                partial = True
            try:
                body, first, last, total = store.read_artifact(artifact_id, start, end)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{row['size']}")
                self.end_headers()
                return
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", row["mime"])
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{row['name']}")
            if partial:
                self.send_header("Content-Range", f"bytes {first}-{last}/{total}")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path: str) -> None:
            relative = "index.html" if path in ("", "/") else unquote(path).lstrip("/")
            target = (root / relative).resolve()
            if root not in target.parents and target != root:
                self._error(403, "forbidden")
                return
            if not target.is_file():
                target = root / "index.html"
            if not target.is_file():
                self._error(404, "not found")
                return
            body = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or "javascript" in mime else ""))
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/v1/") or not self._require_api_auth():
                return
            try:
                body = self._read_json()
                if parsed.path == "/v1/groups":
                    self._json(201, {"ok": True, "group": store.create_group(body.get("name", ""), body.get("instructions", ""))})
                    return
                if parsed.path == "/v1/projects":
                    self._json(201, {"ok": True, "project": store.create_project(body.get("name", ""), body.get("path", ""), body.get("nodeId", "local"), body.get("groupId"))})
                    return
                if parsed.path == "/v1/preferences":
                    self._json(200, {"ok": True, "preferences": store.update_preferences(body)})
                    return
                draft_match = re.fullmatch(r"/v1/drafts/([^/]+)", parsed.path)
                if draft_match:
                    device = str(body.get("device") or "desktop")
                    draft = store.save_draft(device, unquote(draft_match.group(1)), str(body.get("text") or ""))
                    self._json(200, {"ok": True, "draft": draft})
                    return
                if parsed.path == "/v1/rooms":
                    self._json(201, {"ok": True, "room": store.create_room(body.get("projectId", ""), body.get("name", ""), body.get("participants") or ["default"])})
                    return
                intervention = re.fullmatch(r"/v1/executions/([^/]+)/approval", parsed.path)
                if intervention:
                    if bridge is None:
                        raise ValueError("Hermes execution bridge is unavailable")
                    result = InterventionManager(store, bridge).approve(
                        intervention.group(1), body.get("requestId", ""), body.get("choice", "once"))
                    self._json(200, {"ok": True, **result})
                    return
                peer_match = re.fullmatch(r"/v1/peers/([^/]+)/preferences", parsed.path)
                if peer_match:
                    peer = store.update_peer_preferences(unquote(peer_match.group(1)), body.get("name", ""), body.get("colorRole", ""))
                    self._json(200, {"ok": True, "peer": peer})
                    return
                bot_match = re.fullmatch(r"/v1/bots/([^/]+)", parsed.path)
                if bot_match:
                    bot = store.update_bot(
                        unquote(bot_match.group(1)), name=body.get("name"), description=body.get("description"),
                        face_shape=body.get("faceShape"), color_role=body.get("colorRole"),
                        model=body.get("model"), provider=body.get("provider"),
                        enabled=body.get("enabled"))
                    if bot is None:
                        self._error(404, "bot not found")
                        return
                    # Best-effort teardown of sessions orphaned by a model
                    # switch; failures must not block the preference save.
                    for unbound in bot.get("unboundSessions") or []:
                        runtime_id = str(unbound.get("runtimeSessionId") or "")
                        if not runtime_id or bridge is None:
                            continue
                        with contextlib.suppress(Exception):
                            bridge.call("session.interrupt", {"session_id": runtime_id}, timeout=10)
                        with contextlib.suppress(Exception):
                            bridge.call("session.close", {"session_id": runtime_id}, timeout=20)
                    self._json(200, {"ok": True, "bot": bot})
                    return
                alias_match = re.fullmatch(r"/v1/rooms/([^/]+)/alias", parsed.path)
                if alias_match:
                    try:
                        room = store.update_room_alias(unquote(alias_match.group(1)), str(body.get("alias") or ""))
                    except LookupError:
                        self._error(404, "room not found")
                        return
                    self._json(200, {"ok": True, "room": room})
                    return
                match = re.fullmatch(r"/v1/rooms/([^/]+)/(send|pause|resume|archive|delete|restore)", parsed.path)
                if match:
                    room_id, action = match.groups()
                    project_id = body.get("projectId", "")
                    key = body.get("idempotencyKey", "")
                    if action == "send":
                        execution = store.accept_prompt(project_id, room_id, body.get("botId", "default"), body.get("text", ""), key, body.get("artifactIds") or [])
                        if bridge is not None and execution["status"] == "queued":
                            threading.Thread(target=ExecutionRunner(store, bridge).run_execution, args=(execution["id"],), daemon=True, name=f"ares-{execution['id'][:16]}").start()
                        self._json(202, {"ok": True, "accepted": True, "execution": execution})
                    elif action == "pause":
                        paused = LifecycleManager(store, bridge).pause_room(project_id, room_id, key) if bridge is not None else store.pause_room(project_id, room_id, key)
                        self._json(200, {"ok": True, **paused})
                    elif action == "resume":
                        self._json(200, {"ok": True, **store.resume_room(project_id, room_id, key)})
                    elif action in {"archive", "delete"}:
                        self._json(200, {"ok": True, **store.soft_delete_room(project_id, room_id, key)})
                    else:
                        self._json(200, {"ok": True, **store.restore_room(project_id, room_id, key)})
                    return
                self._error(404, "not found")
            except (ValueError, json.JSONDecodeError, OSError) as error:
                self._error(400, str(error))

        def do_PUT(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/v1/artifacts" or not self._require_api_auth():
                return
            try:
                query = parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_ARTIFACT_BYTES:
                    raise ValueError("artifact size is invalid")
                body = self.rfile.read(length)
                artifact = store.store_artifact(
                    (query.get("projectId") or [""])[0], (query.get("roomId") or [""])[0],
                    (query.get("name") or [""])[0], self.headers.get("Content-Type", "application/octet-stream"),
                    body, (query.get("author") or ["user"])[0], (query.get("executionId") or [None])[0],
                )
                self._json(201, {"ok": True, "artifact": artifact})
            except (ValueError, OSError) as error:
                self._error(400, str(error))

    return http.server.ThreadingHTTPServer((host, port), Handler)
