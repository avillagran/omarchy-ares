# Initial vertical slice (historical)

The initial slice deliberately implements the first durable product invariant before adapting Hermes transport: projects are code locations, rooms cannot exist without a project, and messages/events are persisted under their room.

The local helper validates project paths, creates stable identifiers, and stores room events atomically with restrictive permissions. The panel reads that one state model and can create a project, create a room for the selected project, and append a local event. It does not represent a local append as a Hermes execution.

This remains intentionally separate from `io.github.avillagran.omarchy-hermes` and does not import its sessions, rooms, or preferences.

The initial JSON helper is retained only as a migration/reference fixture. The active product now uses the SQLite/WAL service in `bin/ares_service.py`, authenticated HTTP transport, real Hermes Desktop sessions, live execution events, lifecycle confirmation, artifacts, and the responsive web client described in the repository README.
