# Ares Phase 1 — Rust core engine (drop-in replacement)

## Mission

Port the Omarchy Ares service core from Python to Rust. You are working in the
git repo at `~/work/omarchy-ares`. Create a NEW Cargo project at
`server/` (mirror the layout of `~/work/omarchy-hermes/server/`,
which is the previous Rust engine — reuse its dependency stack and patterns).

**Do NOT modify any existing file** (Python, QML, web, tests). **Do NOT commit.**
All work lands in `server/`. The host is aarch64 Linux; `cargo` is installed.
This is a large port (~2.700 lines of Python) — work through it methodically
and do not stop until `cargo build --release` succeeds and the acceptance
checks pass. Ending the turn with only a plan or a skeleton is a failure.

## Reference implementation (read these first — all INSIDE this repo; do NOT
read paths outside `~/work/omarchy-ares`, the sandbox
auto-rejects them)

- `bin/ares_service.py` — `AresStore` (SQLite store, lines ~187-1535), HTTP
  server (`create_http_server`, lines ~2433-2734), `models_catalog`,
  `_profile_model_pair`, `_write_profile_model`, sidecar helpers.
- `bin/ares-server` — process supervision CLI (450 lines): `serve`,
  `connection`, `status`, `stop`, `token`, `upload`. Port ALL of it.

Use exactly this dependency set in `server/Cargo.toml` (this is the proven
stack from the previous Rust engine; edition 2021 is fine):

```toml
[package]
name = "ares-server"
version = "0.1.0"
edition = "2021"

[dependencies]
axum = { version = "0.7", features = ["json", "query"] }
bytes = "1"
hex = "0.4"
libc = "0.2"
mime_guess = "2"
rusqlite = { version = "0.32", features = ["bundled"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
sha2 = "0.10"
tokio = { version = "1", features = ["macros", "rt-multi-thread", "process", "signal", "sync", "time", "net", "fs"] }
tower-http = { version = "0.6", features = ["fs"] }
uuid = { version = "1", features = ["v4"] }
url = "2"

[dev-dependencies]
tempfile = "3"
```

## Deliverable: `server/` binary `ares-server`

### CLI (byte-compatible JSON outputs with the Python version)

- `serve --host 0.0.0.0 --port 9420` — run the HTTP server; write
  `server.json` `{"pid","port","host","startedAt"}` (mode 0600, atomic
  tmp+rename); on SIGTERM/SIGINT remove server.json if it is ours and exit 0.
- `connection` — if not running, spawn self `serve` detached (stdin/stdout/stderr
  to `server.log`, new session) and wait ≤8 s for `server.json`; print
  `{"ok":true,"running":true,"pid","endpoint":"http://127.0.0.1:PORT",
  "mobileEndpoint":"http://<lan-ip>:PORT","artifactRoot","token"}`.
  `lan-ip` = local address from a UDP socket `connect("192.0.2.1:9")`,
  fallback `127.0.0.1`. If startup fails:
  `{"ok":false,"running":false,"error":"Ares service unavailable"}`, exit 1.
- `status` — like `connection` but never starts the server.
- `stop` — SIGTERM the pid in `server.json`, wait ≤5 s, print
  `{"ok":bool,"running":bool}`.
- `token` / `token --set VALUE` / `token --regenerate` — show/set/regenerate
  `access.token` (file mode 0600, atomic write). `--set` validates
  `^[A-Za-z0-9_-]{32,256}$` → error JSON
  `{"ok":false,"error":"Token must be 32-256 letters, digits, '_' or '-'"}`, exit 1.
  `--regenerate` = 32 urlsafe random bytes (base64url, no padding). When the
  server is running, a change must: capture host/port from `server.json`,
  SIGTERM it, respawn with the SAME `--host/--port`, then poll
  `GET /v1/health` every 500 ms until 200 (≤60 s). Output:
  `{"ok":true,"token":"...","restarted":bool}`.
- `upload --project P --room R --path F` — image files only (mime `image/*`),
  size 0 < n ≤ MAX_ARTIFACT_BYTES (same constant as Python), PUT to
  `http://127.0.0.1:PORT/v1/artifacts?projectId&roomId&name&author=user` with
  `Authorization: Bearer <token>`; print the server JSON response; map HTTP
  errors to `{"ok":false,"error":...}`.
- `process_alive(pid)`: `/proc/<pid>/cmdline` contains `ares-server` and ` serve`.

State dir: `$XDG_STATE_HOME/omarchy/ares/` (default `~/.local/state/omarchy/ares/`),
files: `ares.db` (SQLite WAL), `artifacts/`, `access.token`, `server.json`,
`server.log`. Create dirs with mode 0700.

### SQLite store — identical schema

Copy every `CREATE TABLE IF NOT EXISTS` statement VERBATIM from
`ares_service.py` (projects, project_groups, rooms, room_members, room_events,
artifacts, execution_artifacts, executions, interventions, drafts, bots,
peers, quota_providers, preferences… whatever is there). The Rust binary MUST
open the existing Python-created DB without migration. Port all store methods
the HTTP layer calls, preserving DTO field names exactly (camelCase keys):
snapshot(room_id, event_room_id) with `compact=1` semantics, events_before,
events_after, live_events, device_drafts, save_draft, create_group,
create_project, create_room (members + `room.created` event), update_preferences,
update_peer_preferences, update_bot (+ bot sidecar JSON write + profile model
sidecar write as in Python; on model switch include `unboundSessions` in the
DTO but do NOT call any bridge), update_room_alias, accept_prompt (idempotency
key dedup, execution row + events, artifact links), pause_room, resume_room,
soft_delete_room, restore_room, store_artifact (sha256, artifact root file
write), read_artifact (range), models_catalog (reads Hermes profile dirs as
`_profile_model_pair` does).

### HTTP API — exact surface (auth: `Authorization: Bearer <token>`, compare
sha256 hex digests; 401 `{"ok":false,"error":"authentication required"}`)

Every response JSON with `Content-Type: application/json; charset=utf-8` and
headers `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`X-Frame-Options: DENY`, `Cache-Control: no-store`. JSON body reads capped at
2 MiB → 400 `{"ok":false,"error":"request size is invalid"}`. Handler errors
→ 400 `{"ok":false,"error":"<message>"}`; unknown route → 404.

- `GET /v1/health` (public): `{"ok":true,"service":"ares"}`
- `GET /v1/models` → `{"ok":true,"models":[...]}`
- `GET /v1/state?compact=1&roomId=&eventRoomId=` → `store.snapshot(...)`
- `GET /v1/events?roomId=&before=&limit=` or `?after=&limit=&roomId=`
- `GET /v1/live?roomId=` → `{"ok":true,"events":store.live_events(roomId)}`
- `GET /v1/drafts?device=desktop` → `{"ok":true,"drafts":[...]}`
- `GET /v1/artifacts/{id}` — bytes with `Content-Type`, `Accept-Ranges: bytes`,
  `Content-Disposition: inline; filename*=UTF-8''<name>`; `Range: bytes=a-b`
  → 206 with `Content-Range`; bad/unsatisfiable range → 416
  (`Content-Range: bytes */<size>`); unknown id → 404.
- `POST /v1/groups` `{name,instructions}` → 201 `{"ok":true,"group":...}`
- `POST /v1/projects` `{name,path,nodeId,groupId?}` → 201 `{"ok":true,"project":...}`
- `POST /v1/preferences` → 200 `{"ok":true,"preferences":...}`
- `POST /v1/drafts/{roomId}` `{device,text}` → 200 `{"ok":true,"draft":...}`
- `POST /v1/rooms` `{projectId,name,participants[]}` → 201 `{"ok":true,"room":...}`
- `POST /v1/executions/{id}/approval` → Phase 1 has NO Hermes bridge: always
  400 `{"ok":false,"error":"Hermes execution bridge is unavailable"}` (matches
  Python behavior when bridge is None).
- `POST /v1/peers/{id}/preferences` `{name,colorRole}` → 200 `{"ok":true,"peer":...}`
- `POST /v1/bots/{id}` → 200 `{"ok":true,"bot":...}` or 404 `bot not found`
- `POST /v1/rooms/{id}/alias` `{alias}` → 200 / 404 `room not found`
- `POST /v1/rooms/{id}/{send|pause|resume|archive|delete|restore}`:
  - send: `store.accept_prompt(projectId, roomId, botId, text, idempotencyKey,
    artifactIds)` → 202 `{"ok":true,"accepted":true,"execution":...}`.
    Phase 1 does NOT dispatch execution (no runner); status stays `queued`.
  - pause → `store.pause_room` (bridge-less fallback), resume →
    `store.resume_room`, archive/delete → `store.soft_delete_room`,
    restore → `store.restore_room`; each 200 `{"ok":true, ...}`.
- `PUT /v1/artifacts?projectId&roomId&name&author&executionId?` — raw body,
  `Content-Length` 0 < n ≤ MAX_ARTIFACT_BYTES → 201 `{"ok":true,"artifact":...}`.
- Static files (no auth): `GET /` → `index.html`; path traversal outside web
  root → 403; missing file → fall back to `index.html` (SPA); add
  `Content-Security-Policy: default-src 'self'; img-src 'self' blob: data:;
  media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'`
  and `; charset=utf-8` for text/javascript types. Web root = `../web`
  relative to the repo (serve from `WEB_ROOT` constant like Python).

Threading model: axum on tokio multi-thread; rusqlite behind a `Mutex` (or
`r2d2`-free single connection guarded, matching Python's single-writer lock).
WAL mode + `PRAGMA foreign_keys=ON` as Python does.

## Acceptance

1. `cargo build --release` clean on this host (aarch64), no warnings about
   unused cruft; `cargo fmt` applied; `cargo clippy` no errors.
2. `cargo test` — add unit tests for: token validation regex, range parsing,
   idempotency dedup in accept_prompt, schema creation on a temp DB, DTO key
   names (spot-check via serde_json on a fixture).
3. Do not break anything outside `server/`. Do not commit.
4. Write `server/README.md`: build/run instructions and a table mapping each
   Python function/endpoint to its Rust counterpart, marking what is Phase 2
   (Hermes broker, ExecutionRunner dispatch, live mirror, sync loops).
