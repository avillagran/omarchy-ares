            CREATE TABLE IF NOT EXISTS project_groups(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects(
              id TEXT PRIMARY KEY, group_id TEXT REFERENCES project_groups(id), name TEXT NOT NULL,
              node_id TEXT NOT NULL, path TEXT NOT NULL, location_key TEXT NOT NULL UNIQUE,
              repo_identity TEXT NOT NULL DEFAULT '', map_color TEXT NOT NULL DEFAULT '', map_x INTEGER, map_y INTEGER, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms(
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
              status TEXT NOT NULL, memory_release TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
              source TEXT NOT NULL DEFAULT 'ares', external_session_id TEXT NOT NULL DEFAULT '',
              external_profile TEXT NOT NULL DEFAULT '', external_updated_at REAL,
               external_archived INTEGER NOT NULL DEFAULT 0,
              deleted_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS native_group_authorities(
              authority_host TEXT PRIMARY KEY, snapshot_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS native_groups(
              authority_host TEXT NOT NULL, desktop_key TEXT NOT NULL, room_id TEXT UNIQUE,
              desktop_room_id TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
              snapshot_revision INTEGER NOT NULL DEFAULT 0, tombstone TEXT,
              PRIMARY KEY(authority_host,desktop_key)
            );
            CREATE TABLE IF NOT EXISTS native_group_members(
              room_id TEXT NOT NULL REFERENCES rooms(id), position INTEGER NOT NULL,
              member_key TEXT NOT NULL, host TEXT NOT NULL, connection_id TEXT NOT NULL,
              connection_kind TEXT NOT NULL, profile TEXT NOT NULL,
              stored_session_id TEXT NOT NULL, resolved_session_id TEXT NOT NULL,
              resolution TEXT NOT NULL, PRIMARY KEY(room_id,position)
            );
            CREATE TABLE IF NOT EXISTS room_members(
              room_id TEXT NOT NULL REFERENCES rooms(id), bot_id TEXT NOT NULL, position INTEGER NOT NULL,
              PRIMARY KEY(room_id,bot_id)
            );
            CREATE TABLE IF NOT EXISTS terminal_bindings(
              room_id TEXT PRIMARY KEY REFERENCES rooms(id), host TEXT NOT NULL,
              terminal_key TEXT NOT NULL, agent_kind TEXT NOT NULL, updated_at REAL NOT NULL
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
            CREATE INDEX IF NOT EXISTS idx_events_ephemeral ON room_events(room_id) WHERE json_extract(payload,'$.ephemeral')=1;
            -- hermes_rooms computes MAX(created_at) of transcript-synced events
            -- per room every sync tick; without this it full-scans every room.
            CREATE INDEX IF NOT EXISTS idx_events_extmsg ON room_events(room_id,created_at) WHERE json_extract(payload,'$.externalMessageId') IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_executions_room ON executions(room_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_artifacts_room ON artifacts(room_id,created_at);
