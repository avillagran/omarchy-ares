# S1 — herdr client inside QMLTermWidget follows external tab focus

## Question

Given a QMLTermWidget hosting a `herdr --session ares` client, when Ares
switches rooms and drives `herdr tab focus <id>` over the socket from outside,
does the client rendered inside the terminal widget switch to the focused tab?

## Verdict: PASS ✅ (2026-09-23)

The embedded client follows external socket control. Round trip verified
visually: SALA ARES (prompt `~`) → external `tab focus w1:t2` → SALA CTO
highlighted with prompt `/tmp ›` → external `tab focus w1:t1` → SALA ARES
highlighted with prompt `~` again. Server-side `tab list` agrees at each step.

## Setup that worked

- herdr 0.9.1 named session, headless server:
  `setsid env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID herdr --session ares server`
  Socket: `~/.config/herdr/sessions/ares/herdr.sock`
- External control with `HERDR_SOCKET_PATH=<sock> herdr tab focus <id>`
  (rename label is positional: `herdr tab rename <id> <label>`).
- Tabs: `herdr tab create --workspace w1 --label "SALA CTO" --cwd /tmp --no-focus`
- Terminal window: qmltermwidget PoC runner (`~/Work/qmltermwidget/poc-ares`),
  shellProgram = `/bin/sh -c 'exec herdr --session ares'`.

## Pitfalls found (documented so they don't bite again)

1. **Env inheritance**: scrub `HERDR_ENV`, `HERDR_PANE_ID`, `HERDR_TAB_ID`,
   `HERDR_WORKSPACE_ID`, `HERDR_CLIENT_SOCKET_PATH` from the terminal launch
   env, or the client behaves as a nested pane. `HERDR_SOCKET_PATH` must be
   SET (to the named-session socket) for external control commands.
2. **Args drop**: spawning `herdr --session ares` directly as qmltermwidget's
   shellProgram produced a bare `herdr` process (fell back to the DEFAULT
   session and rendered the user's live `omarchy` session — very confusing).
   Passing the same command through `/bin/sh -c 'exec herdr --session ares'`
   spawned the correct argv and the client connected to the right socket
   (verified via `ss -xp`: peer inode = `sessions/ares/herdr-client.sock`).
   Root cause of the direct-spawn drop not fully explained; the `sh -c exec`
   wrapper is the robust pattern (it also centralizes env scrub).
3. **Window placement on a busy multi-monitor desktop**: find the window via
   `hyprctl clients -j` (class `ares-term`), then capture with
   `grim -g "<x>,<y> <w>x<h>"` (geometry is logical; grim multiplies by
   output scale). `hyprctl dispatch focuswindow address:...` is broken in the
   custom Hyprland Lua build — don't rely on it for verification.
4. **First tab**: `herdr workspace create` spawns a default tab (label `1`,
   cwd `$HOME`); rename it rather than creating a first tab from scratch.

## Evidence

- `/tmp/s1-initial.png` — client renders: tab bar SALA ARES/SALA CTO, sidebar
  (spaces `~`, empty agents), prompt `~`, Omarchy branding top-right.
  No render glitches, no missing glyphs.
- `/tmp/s1-after-focus-t2.png` — SALA CTO highlighted, prompt `/tmp ›`.
- `/tmp/s1-back-to-t1.png` — SALA ARES highlighted, prompt `~`.

## Impact

The chat-pane design is viable: Ares panel keeps rooms/composer/images
native; the chat render is a real herdr client in a terminal widget; room
selection maps 1:1 to `herdr tab focus`. Next: session/tab lifecycle driven
by the engine (tab per room + `agent start --kind hermes` with
profile/resume from `session_bindings`), launcher spawn/focus per host,
theme-aware colorscheme for the widget.
