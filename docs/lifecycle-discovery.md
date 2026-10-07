# Hermes lifecycle discovery

Ares uses the Desktop Gateway session lifecycle rather than the global Hermes emergency stop.

- `hermes pause` is global. It blocks new cron, kanban, and gateway work; it does not implement room pause.
- A Ares room pause first records `room.pause.requested`, then attempts `session.interrupt` and `session.close` for every local binding.
- A successful `session.close` confirms release. A session already absent from the authoritative `session.active_list` also confirms release.
- A failed close with a still-active session remains failed. Offline or unimplemented remote-node release remains pending; the room reports `partial` or `unconfirmed`, never a fabricated success.
- Resume keeps the persisted session key. The next execution uses `session.resume`; if the live runtime was released, Hermes rebuilds it from persisted history.
- Executions interrupted by a Ares service restart are marked `interrupted` during database recovery.

The room event journal records each release outcome and is the product authority. Gateway replay is used for execution observation, not as permanent history.
