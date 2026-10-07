# Omarchy Ares

Ares is an [Omarchy](https://omarchy.org) desktop plugin (Hyprland + Quickshell)
that turns [Hermes Agent](https://github.com/NousResearch/hermes-agent) work into
project-owned rooms: every room belongs to a code project, a project is identified
by its host plus its source-directory path, and rooms stay reachable as live
terminals, mirrored transcripts, and a searchable sidebar tree — on the local
machine and on paired remote hosts.

## What it is

A mission-control surface for running many AI agent sessions at once:

- A **bar widget and popup** with animated per-room bot faces, working/reply-needed
  states, provider quota bars, and a project → room tree sorted by recent activity.
- A **full-monitor workspace** with overview / master-stack / focus modes where each
  tile embeds the room's real terminal (read-only observation for remote peers).
- A **persistent service** that keeps an auditable local journal (SQLite WAL) of
  rooms, events, executions, and approvals, independent of any UI being open.
- An **authenticated web client** (`web/`) so a phone can read rooms, approve
  actions, and send drafts.

## How it works

```text
┌────────────────────────────┐        ┌──────────────────────────┐
│ Omarchy shell (Quickshell) │        │ Remote host              │
│  QML plugin (this repo)    │        │  ares-peer agent (peer/) │
│   bar widget / workspaces  │        │   inventory + observe    │
└─────────────┬──────────────┘        └────────────┬─────────────┘
              │ authenticated HTTP (/v1/*)         │ token-authenticated link
┌─────────────▼──────────────┐        ┌────────────▼─────────────┐
│ ares-server (Rust)         │        │ Hermes + herdr           │
│  SQLite WAL store          │◀──────▶│  agent CLIs in terminals │
│  token-authenticated API   │  WS    │  (herdr multiplexes TUIs)│
└─────────────┬──────────────┘        └──────────────────────────┘
              │ serves
┌─────────────▼──────────────┐
│ web/ browser client        │
└────────────────────────────┘
```

- **Discovery, not duplication.** Ares discovers existing Hermes sessions (CLI in
  herdr terminals, Desktop sessions) through the Desktop `projects.tree` RPC and a
  herdr terminal inventory, then mirrors them as rooms. Opening a room reuses its
  exact live terminal; it never silently starts a parallel conversation.
- **Ownership is fail-closed.** A terminal attachment must prove identity
  (profile + session + live pane) before Ares shows it, and input paths are gated:
  terminal-owned rooms never receive Desktop bridge sends, remote rooms are
  read-only unless an explicit SSH mapping exists, and composer sends to a
  herdr-owned session go through the herdr socket with a visible confirmation.
- **Rooms own sessions.** New rooms created from Ares carry their project path into
  Hermes, so future discovery groups them under the same project again.
- **The service owns the truth.** All state lives in a private SQLite store under
  `~/.local/state/omarchy/ares/`; the UI, CLI (`bin/ares`), and web client are
  interchangeable views over the same token-authenticated API.
- **Peers are observed, never trusted.** `peer/` is a small Python agent that
  answers authenticated inventory/observe requests on a remote host; every remote
  frame and claimed identity is re-validated locally before it is shown.

## Implemented product surface

- Omarchy bar widget with animated per-room bot faces, explicit working/reply-needed states, a recent-activity-sorted project/room tree, a full-monitor tiled workspace with overview/master-stack/focus modes, an embedded herdr terminal for active local and SSH peer chats, image selection and preview in the composer, bot selection, pause/resume, provider quota bars, daily quota separators, and mobile-access controls. Unresolved terminal rooms show retry/association controls instead of silently substituting the legacy chat.
- Authenticated responsive web client for phones and desktop browsers with project groups, room deep links, durable drafts, live event recovery, approvals, image previews, video playback, ranged downloads, and reader-preserving scrolling.
- Private SQLite/WAL service with project groups, projects, rooms, members, executions, session bindings, monotonic room events, stable global cursors, idempotency receipts, recoverable deletion, peers, bots, quota windows, artifacts, checksums, and byte-range media delivery.
- Real Hermes Desktop execution using room-owned sessions, plus discovery and continuation of existing public Hermes sessions grouped by their canonical code-project paths. New sessions carry their project path into Hermes so subsequent discovery preserves the same grouping.
- Existing Hermes rooms keep their stored session identity and profile when opened from Ares; continuing one resumes that session instead of creating a parallel conversation.
- Persisted Hermes transcripts are mirrored read-only into their matching Ares rooms without resuming or taking ownership of the source session. The forensic timeline preserves user and assistant messages, thinking-status markers, exact redacted tool arguments, tool results, exit codes, durations, timestamps, and causal ordering. Working sessions refresh ahead of merely recent rooms; long results stay compact until expanded, so live work remains readable without losing its full detail.
- Verified room pause semantics through `session.interrupt`, `session.close`, and authoritative `session.active_list` readback. Ares reports `confirmed`, `partial`, or `unconfirmed`; it never infers release from a requested pause.
- Image delivery to Hermes through `image.attach_bytes`; other files and videos use `file.attach`. Delivery state is recorded independently from storage state.
- Bot/tool-produced images and videos inside the project directory are imported into the room artifact journal automatically.
- Hermes profile, peer, and provider-quota synchronization continues while the panel is closed through `Service.qml` and the persistent Ares service.

## Sidebar search

Click **Search chats** at the bottom of the chat sidebar to expand the search field.
Results update locally on every keystroke, matching both alias and original title,
ignoring case and accents. Multiple words narrow the results together. Search spans
all hosts and collapsed projects, with one result per room in recent-activity order.
Use **Up/Down** to highlight a result and **Enter** to open it (the first result is
highlighted initially). **Esc** or the close button clears search and restores the
normal tree. Typing and arrow navigation never open rooms or send terminal input.

## Composer images and Edit delivery

Images saved in a GUI draft are not necessarily delivered to its agent. Herdr's
current prompt API submits terminal text, not structured image attachments. Ares
therefore preserves the entire draft and explicitly rejects image-bearing Edit
submissions to Herdr; it never sends just the caption or resumes the CLI-owned
session through Desktop. Use the owning CLI's native image flow for those messages.
The verified local text-only Edit route still requires visible-composer review and
an acknowledged socket submission. Remote and explicitly associated terminal rooms
currently require native input. Neither a saved thumbnail nor PTY output proves
attachment delivery. Full GUI image delivery needs an owner-scoped attachment
transport; removing the existing guards is not sufficient.

## Room search

While Ares has keyboard focus, press `Ctrl+Space` to open the centered search
palette. Preferences → General → Search shortcut can switch this to `Alt+Space`;
the preference is persisted by the Rust service and does not change Hyprland
bindings. The sidebar's Search chats button opens the same palette.

Filtering by alias or title is immediate and local. Up/Down selects a result;
Enter opens the first or highlighted room; Escape or a click outside dismisses
search. The selected room's title, project, and host appear beside the input.
Searching never starts or resumes a session, and the overlay isolates keyboard
input from terminal panes and unsent drafts. Opening an unresolved historical
room still respects the existing ownership and resume safety gates.

## Native bot-group discovery

The Rust engine imports the native `hermes-bots-groups` catalog separately from
ordinary chats. Each group remains one Ares room with independent member profiles,
hosts/connections, stored session IDs, and resolved compression-tip IDs. Exact
`Group: <roomId>` lookups are read-only; discovery never creates or resumes agents.
Unknown connections remain unsupported rather than falling back to the local host.

Verified members previously imported as ordinary chats are retained with their
history, aliases, bindings, and receipts, but hidden from the scalar chat catalog
and blocked from scalar execution. Native deletion is tracked separately from
local archive intent. Stale snapshots and inconsistent group identities cannot
overwrite verified associations.

The desktop shows each group's member/session catalog without a legacy transcript
or composer. Selecting a verified local member may display that member's existing,
uniquely identified Herdr terminal in a read-only QMLTermWidget. The server
rechecks the exact profile/session and live terminal inventory; missing or
ambiguous terminals fail closed. Discovery never creates or resumes sessions.
Group messaging and lifecycle execution remain disabled. Stored Desktop sessions
without a live Herdr terminal appear in the catalog but cannot be viewed as a TUI.

## Ares CLI

`bin/ares` is a local, authenticated client of the versioned Ares API. It reads
the private service token from the user's state directory; do not pass tokens
on the command line. Add `--json` for stable machine-readable output.

```sh
bin/ares --json projects list
bin/ares rooms list --active --project PROJECT_ID
bin/ares rooms show ROOM_ID
bin/ares rooms alias ROOM_ID "Display name"
bin/ares rooms archive ROOM_ID --project PROJECT_ID --idempotency-key REQUEST_ID
bin/ares projects map-color set PROJECT_ID '#336699'
bin/ares terminals list --room ROOM_ID
bin/ares terminals attach --room ROOM_ID
bin/ares terminals attach --group GROUP_ID --member MEMBER_KEY
```

`rooms rename` is an alias for `rooms alias`: it does not change the canonical
source title. Room lifecycle commands require a caller-chosen idempotency key.
Terminal attachment requires a TTY and a complete, uniquely bound live Herdr
inventory; it never starts, resumes, or takes over a session. `terminals capture`
fails explicitly until a bounded read-only capture API is available. Future
clients should use the same service API rather than invoke this CLI as a shell.

## Local development

Install by cloning (or symlinking) this repository into your Omarchy plugins
directory, keeping the directory name equal to the plugin id:

```text
~/.config/omarchy/plugins/io.github.avillagran.omarchy-ares -> /path/to/omarchy-ares
```

The terminal view uses the Qt 6 QMLTermWidget plugin (a C++/Qt renderer) and the `herdr`
command. Build QMLTermWidget locally, then run `bin/setup-local-terminal
/path/to/build/QMLTermWidget`. This links the host-specific module into the
plugin and makes its QML import path available to the Omarchy shell via the
Hyprland user config. Restart the Omarchy shell after setup. The module is not
distributed with this repository; the setup step is required on each host.
Build the Rust service with `cargo build --release --manifest-path server/Cargo.toml`
before starting Ares. `bin/ares-server` is a regular-file launcher that runs
the resulting executable and reports a missing build clearly;
build artifacts and the local QMLTermWidget module are not committed.
Opening a room first inspects existing Herdr sessions and reuses its exact
terminal instance. Local and SSH rooms use the same inventory and attachment
contract, including named sessions other than `default` or `ares`. Historical
CLI opening is suspended: the HTTP API does not honor `openIfMissing`, even
for detailed views. All navigation stays read-only and never focuses the source tab.
Hermes registry entries only enrich identity after process validation; missing
entries mean unknown identity, not a stopped CLI. Automatic matching requires
a complete inventory and an exact agent-kind/profile/session match. If identity
is unresolved, choose an observed terminal explicitly. The association is durable,
host-scoped, and tied to the terminal instance, not its title or reusable pane ID.
Pending executions prevent association; associated rooms use native terminal input.

The experimental historical launcher remains unapproved. Holding a Telegram routing
lease does not prove that the child acquired its stored-session lease or restored
the requested history. Native CLI startup can continue after those failures, so
post-launch identity checks are not sufficient containment. Enabling this path
requires a supported fail-closed startup contract before the CLI accepts input;
no Hermes core files or existing leases may be edited to bypass it.

The generic transport supports observed Herdr terminals for other CLI kinds,
including Claude and Kimi. It does not yet import their chat histories into Ares.
Terminal output remains in the native TUI; do not assume persistent logs beyond
what the underlying CLI and Herdr retain.

The QMLTermWidget fork must expose the payload-free `QMLTermSession.outputReceived`
signal. Target resolution, client startup, and received output are distinct states.
PTY bytes are not an attachment acknowledgment: the UI labels output as unverified,
and failures/timeouts retain visible retry and terminal-selection actions.
Compact, grown, focus, and overview terminal views never substitute stored chat
summaries while resolving or after failure. Historical previews are reserved for
nonterminal rooms; terminal errors remain on the terminal/status surface.
Unknown or failed terminals cannot fall through to Desktop prompt submission.
Only a genuinely new local Ares room without a stored session allows the legacy
first-message flow. Local Edit sends use the same complete inventory and profile
identity as attachment, then recheck the terminal instance and composer safety.

Configure the
explicit SSH destination for each peer in `~/.config/omarchy-ares/ssh-peers.json`:

```json
{"host":"user@192.0.2.10"}
```

The key is the Ares peer ID, not a display name. SSH must already trust the
host key and authenticate without interaction. Discovery ships a read-only probe
over stdin, without installation. An explicit historical resume may stage private
foreground launcher templates under `~/.local/state/ares/terminal-open` on its
owning host; it never replaces an existing agent or bypasses unknown ownership.
This is a partial migration: terminal-owned chats cannot send through the Ares
Desktop bridge, including its image attachment and approval flows. Sending to
them through that bridge fails before a request is queued; use the terminal
until one ownership path handles both modes.

Run the service manually when testing outside Omarchy:

```bash
./bin/ares-server connection
./bin/ares-server status
./bin/ares-server stop
```

The service stores private state under `~/.local/state/omarchy/ares/`. The browser access token has mode `0600`. The panel's `Copy access` action copies a link with the token embedded in the URL fragment (`#token=…`), so a phone opens the web workspace already signed in; the web app moves it into tab-scoped session storage and strips it from the address bar. Preferences can show, replace with a custom value, or regenerate the token (`ares-server token [--set VALUE | --regenerate]`); a change restarts the service and invalidates every previously issued link.

## Verification

## Laya decision-training data

Control Room keeps every proposed action as an audit event and never sends it to the source room automatically. `GET /v1/control/training` returns local `ares-laya-decision-v1` examples derived only from those recorded decisions. Each example is intentionally marked `unreviewed` and `approved: false`: review and curate a separate dataset before fine-tuning, and do not feed private room material to a training run without an explicit retention decision.

The exported question is a typed `choice` over the safe Control Room actions (`propose-prompt`, `ask-user`, `defer`, and `review-option`). A Laya recommendation remains a draft; Ares keeps authorization and execution on the existing explicit user-confirmed routes.

```bash
python3 -m unittest discover -s tests -v
node --test web/tests/core.test.mjs tests/draft_lifecycle.test.mjs tests/image_drafts.test.mjs
node --check web/app.mjs
cargo fmt --check --manifest-path server/Cargo.toml
cargo clippy --locked --manifest-path server/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path server/Cargo.toml --all-targets
/usr/lib/qt6/bin/qmllint --bare BarWidget.qml Panel.qml AresBridge.qml AresWorkspace.qml ExtendedWorkspace.qml TerminalChat.qml BotFace.qml PinnedKeyboardPanel.qml Service.qml
```

Final acceptance also requires the live Omarchy gate: open the actual popup, execute a Hermes turn with a tool, pause the room, test authenticated browser access, upload and deliver an image, and visually inspect the popup and web client.

## Experimental sources

`app/` is an abandoned GTK/VTE proof of concept; `term/` is an unbuilt tmux
launcher experiment. The QML plugin and Rust service above are the active path.
`spikes/` records the validated herdr-in-terminal prototype. None of these
experiments is installed by the plugin.
