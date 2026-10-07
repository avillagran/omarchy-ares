# Ares Rust engine — Phases 1 and 2

Native `ares-server` replacement for the Python service's SQLite authority,
HTTP API, static web serving, process-supervision CLI, and Hermes integration.
The Desktop bridge, execution runner, live mirror, and synchronization producers
are native Rust using Axum/Tokio/rusqlite and bundled SQLite. Peer discovery and
provider usage still invoke the packaged `bin/ares-hermes-runtime` subprocess;
that helper retains its Python/runtime dependencies until Phase 3.

## Build and verify

From this directory:

```sh
cargo build --release
cargo fmt --check
cargo test
cargo clippy --all-targets -- -D warnings
ARES_NO_HERMES=1 python3 tests/acceptance.py
cargo test --test hermes python_transcript_conformance -- --ignored
```

Binary: `server/target/release/ares-server` relative to the repository root.
`Cargo.lock` pins the resolved dependency stack. The release build is verified
on the aarch64 Linux host.

The Rust tests cover token validation, ranges, schema/DTO contracts, receipt
replay/conflicts and rollback, artifact delivery and event pagination,
lifecycle/restart recovery, and profile sidecars/session unbinding. Phase 2 adds
bootstrap HTML/token extraction, JSON-RPC ID matching, HTTP/WS redirect refusal,
capped reconnect backoff, streaming-row coalescing/reconciliation, and real
loopback mock-Desktop tests for execution, attachments, approvals, pause,
persisted HTTP transcripts, reconnect without mutation replay, and remote
transcript facets. The optional Python-reference test compares persisted event
DTOs, including stable IDs, numeric source message IDs, reasoning and tool facets.

`tests/acceptance.py` runs a bridge-less Python HTTP server beside the release
binary on ephemeral loopback ports. It creates a Python database, opens a copy
in Rust, checks the schema verbatim, compares HTTP results, and exercises real
CLI processes. Fixtures, sidecars, logs, and state stay inside the ignored
`server/test-work/` directory and are cleaned up. It does not load the user's
live database or Hermes profiles. Generated IDs and timestamps are normalized
for cross-implementation comparisons; idempotency replays are also compared
without normalization. When port 9420 is occupied, the default-port startup
check verifies the unavailable-service response; detached startup is still
exercised by token rotation on an isolated ephemeral port.

`ARES_NO_HERMES=1` explicitly disables Desktop discovery/spawning, the runner,
and runtime-helper polling for isolated bridge-less tests. Normal `serve` always
enables integration. The repository-root `tests/conformance_rust.py` was reviewed;
its default live-database-copy scenario is not needed for these isolated checks.

## Run

```sh
./target/release/ares-server serve --host 0.0.0.0 --port 9420
./target/release/ares-server connection
./target/release/ares-server status
./target/release/ares-server token
./target/release/ares-server token --set 'a_custom_token_with_at_least_32_chars'
./target/release/ares-server token --regenerate
./target/release/ares-server upload --project PROJECT_ID --room ROOM_ID --path image.png
./target/release/ares-server stop
```

`connection` starts a detached copy of this executable if necessary; `status`
only inspects the current service. Token changes restart a running service on
its original host and port, then poll the public health endpoint for readiness.
`serve --port 0` supports an OS-selected port published in `server.json`.
SIGTERM and SIGINT exit successfully and remove only the process's own state
file. CLI JSON uses Python's field order, separator spacing, and ASCII escaping;
HTTP JSON uses sorted compact objects and UTF-8.

For a separate instance, set both environment variables before running commands:

```sh
export XDG_STATE_HOME="$PWD/test-work/manual-state"
export HERMES_HOME="$PWD/test-work/manual-hermes"
./target/release/ares-server serve --host 127.0.0.1 --port 19420
```

State defaults to `$XDG_STATE_HOME/omarchy/ares`, or
`~/.local/state/omarchy/ares` when XDG_STATE_HOME is unset:

| Path | Purpose |
| --- | --- |
| `ares.db` | Existing Python-compatible SQLite database, WAL, foreign keys, five-second busy timeout |
| `artifacts/` | SHA-256-described image/video bodies, private atomic writes |
| `access.token` | Private bearer token, atomic replacement |
| `server.json` | Private atomic PID/host/port/start-time record |
| `server.log` | Detached process output |

State and artifact directories use mode 0700; database, token, state record,
and artifact files use mode 0600. Profile catalog/sidecars use `HERMES_HOME`,
defaulting to `~/.hermes`. `WEB_ROOT` is the repository's `web/`, resolved from
the compile-time Cargo manifest directory, independent of the launch directory.

## Design and compatibility

* Axum runs on Tokio's multithreaded runtime. Bridge-less HTTP CRUD and artifact
  work runs in Tokio's blocking pool. A single `Mutex<Store>` serializes access to
  the rusqlite connection. Compound mutations use `BEGIN IMMEDIATE` with
  commit/rollback, including receipt checks and event insertion. Runtime producers
  use the same lock and release it before any asynchronous network wait.
* `src/schema.sql` copies all 17 Python `CREATE TABLE IF NOT EXISTS` statements
  verbatim. Initialization adds only the same compatibility columns, index,
  default bot, and preferences that Python initializes. An already-current
  Python database needs no conversion or replacement.
* Startup recovers queued/running executions as `interrupted`, matching Python,
  and removes legacy non-ephemeral delta rows. Accepted sends launch an execution
  task. An atomic queued-to-running claim prevents receipt replays from submitting
  a second prompt. Ambiguous/disconnected RPC mutations are never replayed.
* API authentication compares SHA-256 hex digests of bearer tokens. Health and
  static assets are public. All responses have the common security/no-store
  headers; JSON requests are limited to 2 MiB, artifacts to 512 MiB.
* Explicit-start single byte ranges match Python. Suffix and multipart ranges
  are rejected. All 416 responses include `Content-Range: bytes */SIZE`, as
  required by the port specification (Python omits it on malformed syntax).
* Compact snapshots retain the selected room's last 40 events and the latest
  event of every other visible room. Room-only snapshots retain the full
  transcript. Historical assistant DTOs include Qt-safe rendered HTML, and
  tool results retain Python's readable envelope/multimodal formatting.
* Bot edits persist local `ares.json` and model/provider config fields.
  Model changes remove session bindings, return `unboundSessions`, and best-effort
  interrupt/close the orphaned runtime sessions. Peer preferences persist
  `ares-peers.json`.

## Hermes runtime and subprocess boundary

`broker.rs` discovers same-user, default-profile `hermes serve` processes for the
exact `HERMES_HOME`, joining their file descriptors to loopback kernel listeners.
It fetches `/`, extracts the JSON-encoded `__HERMES_SESSION_TOKEN__`, and opens
`/api/ws?token=…` on a direct TCP connection. HTTP and WebSocket redirects are
refused; environment proxies are disabled. Bootstrap bodies are capped at 2 MiB,
and RPC/transcript bodies at 40 MiB. Credentials stay in memory and transport
failures use fixed messages. Reconnection uses 250 ms exponential backoff capped
at 5 seconds, failing outstanding requests without replaying them.

`session.messages` follows the Python helper's special contract: authenticated
HTTP reads of `/api/sessions/{id}` and `/api/sessions/{id}/messages`, with profile,
limit, latest-order, and compaction query parameters. It never resumes the session.
The runner does use `session.resume`/`session.create`, binds runtime ownership,
delivers attachments, submits once, and observes deltas/tools/interventions while
polling completion. Transient rows are cleared when a turn finishes. Imported
sessions use the live mirror's stable per-turn/per-segment IDs, 30-second live
and 5-second completed cache lifetimes, with database-authoritative reconciliation.

If the initial `profiles.list` probe fails, `serve` owns a child launched as
`hermes serve --port 0 --skip-build`, retries for 12 seconds, and terminates/reaps
that child on shutdown (SIGTERM, then kill after five seconds). Existing Desktop
processes are not owned. Background tasks are stopped and the broker closed on
shutdown. A failed probe leaves the HTTP service usable; the broker reconnects.

The runtime loop runs immediately, then sleeps 60 seconds between passes:
profiles, local project/session discovery, persisted transcripts, remote runtime,
peer discovery/sync, and quotas. The transcript loop polls active sessions for
each imported local Hermes profile every 500 ms and refreshes persisted transcripts
every two seconds, prioritizing active sessions. Individual polling failures do
not terminate subsequent passes.

`sync.rs` invokes `../bin/ares-hermes-runtime` relative to the compile-time server
manifest directory, with precisely these argument contracts:

| Invocation | Purpose / timeout |
| --- | --- |
| no arguments | Session inventory, remote room discovery / 25 s |
| `--session ID --limit 100` | Remote transcript (up to 16 sessions, concurrency 3) / 20 s |
| `--discover` | Peer discovery / 25 s |
| `--sync-peers` | Peer synchronization / 25 s |
| `--peers` | Peer DTO producer / 20 s |
| `--usage` | Quota provider/window producer / 30 s |

JSON stdout is bounded and parsed; failed helpers are isolated from other sync
stages. Timed-out/cancelled owned helpers are killed. The helper's internal peer
authentication, discovery, provider integrations, and remote transport remain
the subprocess's responsibility.

## Python-to-Rust map

### CLI and initialization

| Python function | Rust counterpart | Phase |
| --- | --- | --- |
| `main` argument dispatch | `cli::run`, `src/main.rs` | 1 |
| `write_private` | `write_private` in `lib.rs` | 1 |
| `token`, `CUSTOM_TOKEN_PATTERN` | `cli::token`, `cli::valid_token`, `cli::random_token` | 1 |
| `process_alive`, `state` | `cli::process_alive`, `cli::state` | 1 |
| `lan_address` | `cli::lan_address` | 1 |
| `ensure_started`, `wait_ready` | `cli::ensure_started`, `cli::wait_ready` | 1 |
| `connection`, `status` command | `cli::connection` with start flag | 1 |
| `configure_token` | `cli::configure_token` | 1 |
| `upload` | `cli::upload`, local HTTP client `cli::request` | 1 |
| `serve` core HTTP/signal ownership | `cli::serve`, `StateGuard` | 1 |
| `stop_server`, `stop` | `cli::stop_server`, `cli::run` stop branch | 1 |
| `now`, `ident`, `compact`, `require_text` | `now`, `ident`, sorted `Value::to_string`, `require` | 1 |
| `AresStore.__init__`, `_migrate`, `_recover_interrupted`, `close` | `Store::open`, `Store::migrate`, connection RAII | 1 |
| `_transaction`, `_dict`, `_project`, `_room`, `_owned_room` | `Store::transaction`, `rows`/`one`, `room`, `owned_room` | 1 |

### Store and endpoints

| Python function / endpoint | Rust counterpart | Phase |
| --- | --- | --- |
| `create_http_server` | `http::router`, `handle`, `get`, `post` | 1 |
| `_authorized`, `_json`, `_error`, `end_headers`, `_read_json` | `http::handle`, `json_response`, `error`, `response`, capped `to_bytes` | 1 |
| `GET /v1/health` | `http::handle` public health | 1 |
| `models_catalog`, `GET /v1/models` | `Store::models_catalog` in `sidecar.rs` | 1 |
| `_profile_model_pair`, `_write_profile_model` | `sidecar::profile_model_pair`, `write_profile_model` | 1 |
| `_json_read`, `_json_write`, bot/peer sidecar paths | `sidecar::json_read`, `json_write`, paths from `Store::hermes_home` | 1 |
| `snapshot`, `_snapshot_locked`, `GET /v1/state` | `Store::snapshot` | 1 |
| `events_before`, `events_after`, `GET /v1/events` | `Store::events_before`, `events_after` | 1 |
| `live_events`, `GET /v1/live` persisted fallback | `Store::live_events` | 1 |
| `device_drafts`, `GET /v1/drafts` | `Store::device_drafts` | 1 |
| `save_draft`, `POST /v1/drafts/{roomId}` | `Store::save_draft` | 1 |
| `create_group`, `POST /v1/groups` | `Store::create_group` | 1 |
| `create_project`, `POST /v1/projects` | `Store::create_project` | 1 |
| `create_room`, `POST /v1/rooms` | `Store::create_room` | 1 |
| `preferences`, `update_preferences`, `POST /v1/preferences` | `Store::preferences`, `update_preferences` | 1 |
| `update_peer_preferences`, `POST /v1/peers/{id}/preferences` | `Store::update_peer_preferences` in `sidecar.rs` | 1 |
| `update_bot`, `POST /v1/bots/{id}` | `Store::update_bot` in `sidecar.rs` | 1 |
| `update_room_alias`, `POST /v1/rooms/{id}/alias` | `Store::update_room_alias` | 1 |
| `accept_prompt`, `POST /v1/rooms/{id}/send` | `Store::accept_prompt`, `ExecutionRunner` | 1 + 2 |
| `_receipt`, `_save_receipt` | `Store::receipt`, `save_receipt` | 1 |
| `_append_event` | `Store::append_event` within mutation transactions | 1 |
| `pause_room`, `resume_room`, `soft_delete_room`, `restore_room` | `Store::lifecycle` | 1 |
| `POST /v1/rooms/{id}/{pause,resume,archive,delete,restore}` | `Store::lifecycle`, `LifecycleManager` for pause | 1 + 2 |
| `POST /v1/executions/{id}/approval` | `InterventionManager::approve` | 2 |
| `store_artifact`, `PUT /v1/artifacts` | `Store::store_artifact` | 1 |
| `read_artifact`, `_artifact`, `GET /v1/artifacts/{id}` | `Store::read_artifact`, `http::artifact`, `parse_range` | 1 |
| `_static`, `GET /` and SPA assets | `http::static_file`, traversal checks and CSP | 1 |
| `_group_dto`, `_project_dto`, `_room_dto`, `_execution_dto` | `Store::dto`, `dtos` | 1 |
| `_event_dto`, `_artifact_dto`, `_session_dto`, `_bot_dto`, `_peer_dto` | `Store::dto`, `dtos` | 1 |
| `_quota_dto`, `_quota_providers` | `Store::dto` quota branches | 1, reads persisted quotas |
| `_tool_result_display_text`, `_format_tool_arguments` | `render::tool_text`, `tool_args` | 1 |
| `markdown_to_html`, `_inline_markdown`, `_escape_html`, `_table_row_cells` | `render::markdown`, `inline`, `escape`, table-cell closure | 1 |

### Phase 2 integration — done

| Python functionality | Rust implementation |
| --- | --- |
| `HermesBroker`, RPC handshake, listeners | `broker::HermesBroker`, direct HTTP/WS transport and broadcast subscriptions |
| Owned Hermes desktop process | `sync::Runtime::start`, `shutdown`, child RAII |
| `ExecutionRunner.run_execution`, `start_execution`, `finish_execution`, `execution`, `bind_session` | `integration::ExecutionRunner`, `Store` producers in `producers.rs` |
| `execution_artifacts`, `set_artifact_delivery`, media discovery/registration | `Store` producers, `integration::discover_media_paths`, artifact event association |
| `InterventionManager.approve` | `integration::InterventionManager::approve` |
| `LifecycleManager`, `record_pause_outcomes` | `integration::LifecycleManager`, `Store::record_pause_outcomes`; store fallback when disconnected |
| `HermesLiveTranscriptMirror`, `upsert_live_hermes_event`, `clear_live_hermes_events` | `integration::HermesLiveTranscriptMirror`, `Store` producers |
| `upsert_execution_event`, `clear_ephemeral_execution_events`, telemetry sanitization | `Store::event` ephemeral upsert, cleanup, `producers::sanitize` |
| `sync_profiles`, `sync_peers`, `sync_quotas` | `Store` producers, preserving preference sidecars |
| `sync_hermes_project`, `sync_hermes_transcript` | `Store` producers, stable imported room/message/facet identities |
| `sync_hermes_rooms`, `sync_recent_hermes_transcripts` | `sync.rs` local synchronization |
| `runtime_json`, `sync_remote_runtime`, remote rooms/chat | `sync::runtime_json`, `sync_runtime`, `sync_remote_hermes_rooms`, `sync_remote_hermes_chat` |
| `sync_runtime`, runtime and transcript background loops | `sync::Runtime` Tokio tasks |

### Remaining Phase 3 work

* Port `ares-hermes-runtime` internals to remove the retained Python subprocess
  dependency, including authenticated peer adapters and provider integrations.
* Multiarch release builds, relocatable resource/helper discovery, packaging,
  installation/launcher cutover, and deployment verification on supported hosts.

No installation changes are required to build or test this directory. Choose
the Rust executable explicitly when running it; the existing launcher remains
the Python implementation until a later integration step.
