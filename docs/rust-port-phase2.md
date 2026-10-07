# Ares Phase 2 — Rust Hermes integration (bridge, runner, live mirror, sync)

## Mission

Continue the Rust port in the git repo `~/work/omarchy-ares`.
Phase 1 already landed in `server/` (Rust store + HTTP API + CLI, see
`server/README.md` for the Python→Rust mapping and the declared Phase 2
boundaries). Now implement the Hermes integration in Rust so the engine can
REPLACE the Python service in production.

**Rules**: work only inside `server/`. Do NOT modify Python/QML/web/tests.
Do NOT commit. Read paths ONLY inside this repo (external reads are
auto-rejected by the sandbox). Do not stop until `cargo build --release`,
`cargo test` and `cargo clippy --all-targets -- -D warnings` all pass.

## What to port (Python reference — read these files)

From `bin/ares_service.py`:
- `HermesBroker` (~line 1845): JSON-RPC 2.0 over websocket to the Hermes
  Desktop. Bootstrap: HTTP GET the desktop base URL, extract
  `__HERMES_SESSION_TOKEN__` from the HTML, then
  `ws://HOST:PORT/api/ws?token=…`. `call(method, params, timeout)` with
  id-matched responses; `add_listener` for unsolicited `event` notifications;
  reconnect on failure. Security rules from the Python version: never follow
  websocket redirects (the session token rides in the URI), disable proxy env
  for the ws connect, cap message/bootstrap sizes, and never log the token.
- `InterventionManager` (~1769) and `LifecycleManager` (~1800): approval
  resolution and pause-room through the bridge, with the store fallback when
  the bridge is down.
- `HermesLiveTranscriptMirror` (~1939): observes broker events and upserts
  ephemeral live rows (`upsert_live_hermes_event`) — one row per streaming
  turn, updated in place, driving `GET /v1/live`.
- `ExecutionRunner` (~2254): takes a `queued` execution, dispatches the prompt
  to the right Hermes profile/session via the broker, streams
  `message.delta`/`reasoning.delta`/tool events into room_events, handles
  approvals, and completes/fails the execution exactly as the Python DTOs do.
- Sync functions: `sync_hermes_rooms`, `sync_remote_hermes_rooms`,
  `sync_remote_hermes_chat`, `sync_recent_hermes_transcripts`
  (~2066-2254), plus the `serve()` runtime loops in `bin/ares-server`:
  `runtime_loop` (every 60 s: profiles.list → sync_profiles, sync_hermes_rooms,
  sync_recent_hermes_transcripts, sync_remote_runtime, peers, quota) and
  `transcript_loop` (every 500 ms: session.active_list per hermes profile →
  live mirror; every 2 s persisted transcript sync).
- `bin/ares-server` `serve()` also spawns `hermes serve --port 0 --skip-build`
  when `profiles.list` fails (owned child, terminated on shutdown), with the
  12 s retry loop — port that too.

Supporting Python modules used by the above (read as needed; you may shell
out to them instead of reimplementing where noted):
- `bin/hermes_desktop_rpc.py` — the ws bootstrap/RPC policy. PREFER porting
  the protocol to Rust directly (tokio-tungstenite is allowed; add it and
  `url`/`futures` to Cargo.toml).
- `bin/ares-hermes-runtime` — KEEP AS SUBPROCESS: invoke it exactly like the
  Python `runtime_json()`/`sync_runtime()` helpers do (no args → sessions
  JSON; `--session ID --limit N`; `--discover`; `--sync-peers`; `--peers`;
  `--usage`), parse stdout JSON, feed `store.sync_remote_*` / `sync_peers` /
  `sync_quotas`. Do not port its internals in this phase.

## Wiring into the existing Rust code

- `serve` must construct the broker (spawning the desktop if needed), the live
  mirror, and the two sync loops as tokio tasks, and pass the bridge into the
  HTTP state so the Phase 1 fallbacks upgrade to real behavior:
  - `POST /v1/rooms/{id}/send` → after `accept_prompt` returns `queued`,
    spawn `ExecutionRunner::run_execution` (tokio task, same thread-name
    convention irrelevant).
  - `POST /v1/executions/{id}/approval` → `InterventionManager::approve`
    (bridge error only when the broker is genuinely down).
  - `POST /v1/rooms/{id}/pause` → `LifecycleManager::pause_room` with store
    fallback when the bridge is down.
  - `POST /v1/bots/{id}` model switch → for each `unboundSessions` entry,
    best-effort `session.interrupt` then `session.close` (suppress errors).
  - `GET /v1/live?roomId=` → live mirror events when available, else
    `store.live_events`.
- Keep every DTO field name and event payload shape byte-compatible with the
  Python version — `tests/conformance_rust.py` (read it) must keep passing,
  and the QML panel + web app consume these shapes.

## Acceptance

1. `cargo build --release`, `cargo fmt --check`, `cargo test`,
   `cargo clippy --all-targets -- -D warnings` all green.
2. Unit tests for: JSON-RPC id matching, ws URL bootstrap token extraction
   (regex against a fixture HTML), redirect-refusal policy, reconnect
   backoff, delta upsert coalescing into one live row.
3. Update `server/README.md`: move the Phase 2 items to done, document the
   runtime-helper subprocess boundary and any remaining Phase 3 work
   (`ares-hermes-runtime` port, multiarch packaging).
4. Do not run the Rust binary against the live state dir
   (`~/.local/state/omarchy/ares`) — a production Python service is using it.
   Tests must use temp dirs.
