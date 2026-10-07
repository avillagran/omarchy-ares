import hashlib
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "ares_service.py"
spec = importlib.util.spec_from_file_location("ares_service", MODULE_PATH)
service = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(service)


class AresStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = service.AresStore(self.root / "ares.db", self.root / "artifacts")
        self.project_dir = self.root / "code"
        self.project_dir.mkdir()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_complete_project_room_execution_event_contract(self):
        group = self.db.create_group("Omarchy plugins")
        project = self.db.create_project("Ares", str(self.project_dir), "local", group["id"])
        room = self.db.create_room(project["id"], "Complete product", ["default"])
        execution = self.db.accept_prompt(
            project["id"], room["id"], "default", "Build it", "send-1"
        )
        self.db.append_event(
            project["id"], room["id"], execution["id"], "message.delta", "default",
            {"delta": "Done", "text": "Done"}, stable_id="gateway-1"
        )
        self.db.finish_execution(execution["id"], "completed", "Done")

        snapshot = self.db.snapshot(room_id=room["id"])
        self.assertEqual(snapshot["groups"][0]["name"], "Omarchy plugins")
        self.assertEqual(snapshot["projects"][0]["groupId"], group["id"])
        self.assertEqual(snapshot["rooms"][0]["projectId"], project["id"])
        self.assertEqual(snapshot["executions"][0]["status"], "completed")
        self.assertEqual(
            [event["kind"] for event in snapshot["events"]],
            ["room.created", "message.user", "execution.queued", "message.delta", "execution.completed"],
        )
        sequences = [event["sequence"] for event in snapshot["events"]]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_compact_workspace_snapshot_keeps_only_the_selected_tail_and_pages_history(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        selected = self.db.create_room(project["id"], "Selected", ["default"])
        other = self.db.create_room(project["id"], "Other", ["default"])
        for index in range(140):
            self.db.append_event(project["id"], selected["id"], None, "tool.result", "tool", {"text": f"selected-{index}"})
        for index in range(4):
            self.db.append_event(project["id"], other["id"], None, "tool.result", "tool", {"text": f"other-{index}"})

        snapshot = self.db.snapshot(event_room_id=selected["id"])
        selected_events = [event for event in snapshot["events"] if event["roomId"] == selected["id"]]
        other_events = [event for event in snapshot["events"] if event["roomId"] == other["id"]]
        self.assertEqual(len(snapshot["rooms"]), 2)
        self.assertEqual(len(selected_events), 40)
        self.assertTrue(snapshot["eventPage"]["hasOlder"])
        self.assertEqual(snapshot["eventPage"]["earliestCursor"], selected_events[0]["cursor"])
        self.assertEqual([event["text"] for event in other_events], ["other-3"])

        collected = selected_events
        before = snapshot["eventPage"]["earliestCursor"]
        has_older = True
        while has_older:
            older = self.db.events_before(selected["id"], before, 120)
            self.assertLessEqual(len(older["events"]), 40)
            collected = older["events"] + collected
            before = older["earliestCursor"]
            has_older = older["hasOlder"]
        self.assertEqual(
            [event["sequence"] for event in collected],
            list(range(1, 142)),
        )

    def test_idempotent_send_never_creates_duplicate_execution(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Idempotency", ["default"])
        first = self.db.accept_prompt(project["id"], room["id"], "default", "Hello", "same-key")
        second = self.db.accept_prompt(project["id"], room["id"], "default", "Hello", "same-key")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "idempotency conflict"):
            self.db.accept_prompt(project["id"], room["id"], "default", "Changed", "same-key")

    def test_pause_reports_each_session_and_never_invents_release(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Lifecycle", ["default", "scout"])
        self.db.bind_session(room["id"], "default", "local", "stored-a", "runtime-a")
        self.db.bind_session(room["id"], "scout", "peer-a", "stored-b", "runtime-b")
        result = self.db.pause_room(project["id"], room["id"], "pause-1")
        self.assertEqual(result["status"], "paused")
        self.assertEqual(
            {item["releaseState"] for item in result["sessions"]}, {"unconfirmed"}
        )
        self.assertEqual(self.db.snapshot(room_id=room["id"])["rooms"][0]["memoryRelease"], "unconfirmed")

    def test_lifecycle_manager_confirms_release_only_after_session_close(self):
        project = self.db.create_project("Project", self.project_dir)
        room = self.db.create_room(project["id"], "Room", ["default"])
        self.db.bind_session(room["id"], "default", "local", "stored", "runtime")

        class ClosingBridge:
            def call(self, method, params, timeout=30):
                if method == "session.interrupt":
                    return {"ok": True, "result": {"interrupted": True}}
                if method == "session.close":
                    return {"ok": True, "result": {"closed": True}}
                raise AssertionError(method)

        result = service.LifecycleManager(self.db, ClosingBridge()).pause_room(project["id"], room["id"])
        self.assertEqual(result["memoryRelease"], "confirmed")
        self.assertEqual(result["sessions"][0]["releaseState"], "released")
        snapshot = self.db.snapshot(room_id=room["id"])
        self.assertEqual(snapshot["rooms"][0]["memoryRelease"], "confirmed")
        self.assertIn("session.release.confirmed", [event["kind"] for event in snapshot["events"]])

    def test_lifecycle_manager_reports_partial_release_without_claiming_success(self):
        project = self.db.create_project("Project", self.project_dir)
        room = self.db.create_room(project["id"], "Room", ["a", "b"])
        self.db.bind_session(room["id"], "a", "local", "stored-a", "runtime-a")
        self.db.bind_session(room["id"], "b", "peer", "stored-b", "runtime-b")

        class PartialBridge:
            def call(self, method, params, timeout=30):
                closed = params.get("session_id") == "runtime-a"
                return {"ok": closed, "result": {"closed": closed}}

        result = service.LifecycleManager(self.db, PartialBridge()).pause_room(project["id"], room["id"])
        self.assertEqual(result["memoryRelease"], "partial")
        self.assertEqual({item["releaseState"] for item in result["sessions"]}, {"released", "pending_remote"})

    def test_lifecycle_manager_confirms_runtime_already_absent_from_active_list(self):
        project = self.db.create_project("Project", self.project_dir)
        room = self.db.create_room(project["id"], "Room", ["default"])
        self.db.bind_session(room["id"], "default", "local", "stored", "runtime-gone")

        class AbsentBridge:
            def call(self, method, params, timeout=30):
                if method == "session.close":
                    return {"ok": True, "result": {"closed": False}}
                if method == "session.active_list":
                    return {"ok": True, "result": {"sessions": [{"session_id": "some-other-session"}]}}
                return {"ok": True, "result": {}}

        result = service.LifecycleManager(self.db, AbsentBridge()).pause_room(project["id"], room["id"])
        self.assertEqual(result["memoryRelease"], "confirmed")
        self.assertEqual(result["sessions"][0]["releaseState"], "released")

    def test_intervention_manager_resolves_exact_approval_and_records_it(self):
        project = self.db.create_project("Project", self.project_dir)
        room = self.db.create_room(project["id"], "Room", ["default"])
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Run it", "approval-turn")
        self.db.bind_session(room["id"], "default", "local", "stored", "runtime")

        class ApprovalBridge:
            def call(self, method, params, timeout=30):
                self.method, self.params = method, params
                return {"ok": True, "result": {"resolved": 1}}

        bridge = ApprovalBridge()
        result = service.InterventionManager(self.db, bridge).approve(execution["id"], "request-1", "once")
        self.assertTrue(result["resolved"])
        self.assertEqual(bridge.method, "approval.respond")
        self.assertEqual(bridge.params, {"session_id": "runtime", "request_id": "request-1", "choice": "once", "all": False})
        kinds = [event["kind"] for event in self.db.snapshot(room_id=room["id"])["events"]]
        self.assertIn("approval.responded", kinds)

    def test_soft_delete_preserves_history_and_restore_recovers_room(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Recoverable", ["default"])
        self.db.soft_delete_room(project["id"], room["id"], "delete-1")
        self.assertEqual(self.db.snapshot()["rooms"], [])
        deleted = self.db.snapshot(include_deleted=True, room_id=room["id"])
        self.assertEqual(deleted["rooms"][0]["status"], "deleted")
        self.assertIn("room.deleted", [event["kind"] for event in deleted["events"]])
        self.db.restore_room(project["id"], room["id"], "restore-1")
        self.assertEqual(self.db.snapshot(room_id=room["id"])["rooms"][0]["status"], "paused")

    def test_snapshot_separates_archived_rooms_from_home(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        kept = self.db.create_room(project["id"], "Kept", ["default"])
        archived = self.db.create_room(project["id"], "Archived", ["default"])
        self.db.soft_delete_room(project["id"], archived["id"], "archive-1")

        snapshot = self.db.snapshot()
        self.assertEqual([room["id"] for room in snapshot["rooms"]], [kept["id"]])
        self.assertEqual([room["id"] for room in snapshot["archivedRooms"]], [archived["id"]])

    def test_peer_preferences_survive_runtime_rediscovery(self):
        self.db.sync_peers([{"name": "host-a", "title": "Host A", "url": "http://a", "status": "ok"}])
        customized = self.db.update_peer_preferences("host-a", "Studio", "warning")
        self.assertEqual(customized["name"], "Studio")
        self.assertEqual(customized["colorRole"], "warning")

        self.db.sync_peers([{"name": "host-a", "title": "Renamed upstream", "url": "http://b", "status": "ok"}])
        peer = self.db.snapshot()["peers"][0]
        self.assertEqual(peer["name"], "Studio")
        self.assertEqual(peer["sourceName"], "Renamed upstream")
        self.assertEqual(peer["colorRole"], "warning")
        self.assertEqual(peer["endpoint"], "http://b")

    def test_quota_snapshot_keeps_every_connected_provider(self):
        self.db.sync_quotas({"updatedAt": 1000, "providers": [
            {"provider": "kimi-coding", "weekTokens": 1200, "weekCalls": 12,
             "quota": {"status": "ok", "windows": [{"label": "Weekly", "usedPercent": 43, "availablePercent": 57}] }},
            {"provider": "nous", "weekTokens": 0, "weekCalls": 0,
             "quota": {"status": "ok", "plan": "Free", "details": ["US$0.10 available"], "windows": []}},
            {"provider": "opencode-free", "weekTokens": 0, "weekCalls": 0,
             "quota": {"status": "unsupported", "windows": []}},
        ]})

        providers = self.db.snapshot()["quotaProviders"]
        self.assertEqual([item["provider"] for item in providers], ["kimi-coding", "nous", "opencode-free"])
        self.assertEqual(providers[0]["windows"][0]["availablePercent"], 57)
        self.assertEqual(providers[1]["plan"], "Free")
        self.assertEqual(providers[2]["status"], "unsupported")

    def test_home_quota_preferences_are_persisted_in_the_shared_snapshot(self):
        self.assertEqual(self.db.snapshot()["preferences"], {
            "homeQuotaVisible": True,
            "homeQuotaCollapsed": False,
            "hiddenHomeQuotaProviders": [],
            "panelSize": "medium",
            "sidebarCollapsed": False,
            "sidebarWidth": 200,
        })
        updated = self.db.update_preferences({
            "homeQuotaVisible": False,
            "homeQuotaCollapsed": True,
            "hiddenHomeQuotaProviders": ["nous", "openai-codex", "nous"],
            "panelSize": "large",
            "sidebarCollapsed": True,
            "sidebarWidth": 260,
        })
        self.assertEqual(updated, {
            "homeQuotaVisible": False,
            "homeQuotaCollapsed": True,
            "hiddenHomeQuotaProviders": ["nous", "openai-codex"],
            "panelSize": "large",
            "sidebarCollapsed": True,
            "sidebarWidth": 260,
        })
        self.assertEqual(self.db.snapshot()["preferences"], updated)
        with self.assertRaisesRegex(ValueError, "panelSize"):
            self.db.update_preferences({"panelSize": "huge"})
        with self.assertRaisesRegex(ValueError, "sidebarWidth"):
            self.db.update_preferences({"sidebarWidth": 90})
        with self.assertRaisesRegex(ValueError, "unsupported preference"):
            self.db.update_preferences({"unknown": True})
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.db.update_preferences({"homeQuotaVisible": "false"})
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.db.update_preferences({"homeQuotaVisible": "yes"})
        with self.assertRaisesRegex(ValueError, "list of provider IDs"):
            self.db.update_preferences({"hiddenHomeQuotaProviders": "nous"})

    def test_execution_streaming_upserts_ephemeral_rows_and_cleans_them_up(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Streaming", ["default"])
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Go", "stream-1")
        self.db.bind_session(room["id"], "default", "local", "stored", "runtime")
        streamed_counts = []

        def db_counts():
            rows = self.db.snapshot(room_id=room["id"])["events"]
            return {
                "ephemeral": len([event for event in rows if (event.get("payload") or {}).get("ephemeral")]),
                "delta_rows": len([event for event in rows if event["kind"] in {"message.delta", "reasoning.delta", "thinking.delta"}]),
            }

        class StreamingBridge:
            def __init__(self):
                self.listeners = []
                self.resumes = 0

            def add_listener(self, listener):
                self.listeners.append(listener)

            def remove_listener(self, listener):
                self.listeners.remove(listener)

            def emit(self, event):
                for listener in list(self.listeners):
                    listener(event)

            def call(self, method, params, timeout=30):
                if method == "session.resume":
                    self.resumes += 1
                    if self.resumes == 1:
                        return {"ok": True, "result": {"session_id": "runtime", "session_key": "stored", "messages": []}}
                    if self.resumes == 2:
                        self.emit({"session_id": "runtime", "type": "reasoning.delta", "payload": {"text": "pien"}})
                        self.emit({"session_id": "runtime", "type": "reasoning.delta", "payload": {"text": "so "}})
                        self.emit({"session_id": "runtime", "type": "message.delta", "payload": {"text": "Hel"}})
                        self.emit({"session_id": "runtime", "type": "message.delta", "payload": {"text": "lo"}})
                        streamed_counts.append(db_counts())
                        return {"ok": True, "result": {"running": True, "messages": []}}
                    return {"ok": True, "result": {"messages": [
                        {"role": "user", "content": "Go"},
                        {"role": "assistant", "content": "Hello final"},
                    ]}}
                if method == "prompt.submit":
                    return {"ok": True, "result": {}}
                raise AssertionError(method)

        bridge = StreamingBridge()
        result = service.ExecutionRunner(self.db, bridge, poll_interval=0, deadline_seconds=10).run_execution(execution["id"])
        self.assertEqual(result["status"], "completed")
        # During streaming: exactly one ephemeral message row plus one ephemeral
        # reasoning row, and zero raw per-delta rows persisted.
        self.assertEqual(streamed_counts, [{"ephemeral": 2, "delta_rows": 0}])
        rows = self.db.snapshot(room_id=room["id"])["events"]
        self.assertEqual([event for event in rows if (event.get("payload") or {}).get("ephemeral")], [])
        assistants = [event for event in rows if event["kind"] == "message.assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["payload"]["text"], "Hello final")

    def test_live_mirror_evicts_stale_entries_and_validates_against_store(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        with self.db._transaction() as db:
            db.execute(
                "INSERT INTO rooms(id,project_id,name,status,memory_release,created_at,updated_at,"
                "source,external_profile,external_session_id) VALUES(?,?,?,'active','unconfirmed',?,?,'hermes','default','sess1')",
                ("room_h1", project["id"], "Imported", 1.0, 1.0),
            )
        mirror = service.HermesLiveTranscriptMirror(self.db)
        mirror.update_active_sessions("default", [{"id": "runtime1", "session_key": "sess1"}])
        mirror.observe({"session_id": "runtime1", "type": "message.start", "payload": {"message_id": "m1"}})
        mirror.observe({"session_id": "runtime1", "type": "message.delta", "payload": {"text": "hola"}})
        live = mirror.live_events("room_h1")
        self.assertEqual([event["payload"]["text"] for event in live], ["hola"])

        # Facet sync deleted the ephemeral DB row: the cache must not serve it.
        with self.db._transaction() as db:
            db.execute("DELETE FROM room_events WHERE room_id='room_h1'")
        self.assertEqual(mirror.live_events("room_h1"), [])

        # A new generation must evict the previous generation's cached entries.
        mirror.observe({"session_id": "runtime1", "type": "message.start", "payload": {"message_id": "m2"}})
        mirror.observe({"session_id": "runtime1", "type": "message.delta", "payload": {"text": "chau"}})
        live = mirror.live_events("room_h1")
        self.assertEqual([event["payload"]["text"] for event in live], ["chau"])

    def test_markdown_to_html_renders_structure_and_strips_scripts(self):
        source = (
            "# Title\n\n"
            "Some **bold** and `code` text.\n\n"
            "- one\n- two\n\n"
            "```python\nprint(1)\n```\n\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "<script>alert(1)</script>\n\n"
            "[click](javascript:alert(1))\n"
        )
        html = service.markdown_to_html(source)
        self.assertIn("<h1", html)
        self.assertIn("Title", html)
        self.assertIn("<b>bold</b>", html)
        self.assertIn("<pre", html)
        self.assertIn("print(1)", html)
        self.assertIn("<table", html)
        self.assertIn("<li>", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("javascript:", html)
        # Raw HTML in the source is escaped, never passed through.
        self.assertIn("&lt;script&gt;", html)

    def test_cleanup_transient_delta_rows_keeps_ephemeral_streams(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Cleanup", ["default"])
        self.db.append_event(project["id"], room["id"], None, "message.delta", "bot", {"text": "old garbage"})
        self.db.append_event(project["id"], room["id"], None, "reasoning.delta", "bot", {"text": "old noise"})
        self.db.append_event(project["id"], room["id"], None, "message.assistant", "bot",
                             {"text": "stream", "ephemeral": True}, stable_id="stream-1")
        removed = self.db.cleanup_transient_delta_rows()
        self.assertEqual(removed, 2)
        rows = self.db.snapshot(room_id=room["id"])["events"]
        self.assertEqual([event["kind"] for event in rows],
                         ["room.created", "message.assistant"])

    def test_room_drafts_are_saved_and_cleared_per_device(self):
        group = self.db.create_group("Omarchy plugins")
        project = self.db.create_project("Ares", str(self.project_dir), "local", group["id"])
        room = self.db.create_room(project["id"], "Drafts", ["default"])

        self.assertEqual(self.db.device_drafts("desktop"), [])
        self.db.save_draft("desktop", room["id"], "first line")
        self.db.save_draft("desktop", room["id"], "first line\nsecond line")
        self.db.save_draft("phone", room["id"], "other device")
        drafts = {draft["roomId"]: draft["text"] for draft in self.db.device_drafts("desktop")}
        self.assertEqual(drafts[room["id"]], "first line\nsecond line")
        phones = {draft["roomId"]: draft["text"] for draft in self.db.device_drafts("phone")}
        self.assertEqual(phones[room["id"]], "other device")

        self.db.save_draft("desktop", room["id"], "")
        self.assertEqual(self.db.device_drafts("desktop"), [])

    def test_bot_preferences_round_trip_via_update_endpoint(self):
        self.db.create_project("Ares", str(self.project_dir), "local")
        bot = self.db.update_bot("default", name="Scribe", description="Markdown specialist",
                                 face_shape="circle", color_role="info")
        self.assertEqual(bot["name"], "Scribe")
        self.assertEqual(bot["description"], "Markdown specialist")
        self.assertEqual(bot["faceShape"], "circle")
        self.assertEqual(bot["colorRole"], "info")

        server = service.create_http_server(self.db, "127.0.0.1", 0, "test-token", web_root=self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            update = urllib.request.Request(
                base + "/v1/bots/default",
                data=json.dumps({"name": "Scribe II", "description": "d", "faceShape": "hexagon",
                                 "colorRole": "warning"}).encode(),
                headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(update) as response:
                payload = json.load(response)
            self.assertEqual(payload["ok"], True)
            self.assertEqual(payload["bot"]["name"], "Scribe II")
            self.assertEqual(payload["bot"]["faceShape"], "hexagon")

            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(urllib.request.Request(
                    base + "/v1/bots/ghost",
                    data=json.dumps({"name": "Ghost"}).encode(),
                    headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
                    method="POST",
                ))
            self.assertEqual(missing.exception.code, 404)
            missing.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

        with self.assertRaises(ValueError):
            self.db.update_bot("default", face_shape="blob")

    def test_room_alias_round_trip_via_endpoint(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Streaming", ["default"])
        self.assertEqual(room["alias"], "")

        self.db.update_room_alias(room["id"], "Mi alias")
        self.assertEqual(self.db.snapshot(room_id=room["id"])["rooms"][0]["alias"], "Mi alias")

        self.db.update_room_alias(room["id"], "   ")
        self.assertEqual(self.db.snapshot(room_id=room["id"])["rooms"][0]["alias"], "")

        server = service.create_http_server(self.db, "127.0.0.1", 0, "test-token", web_root=self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            def post_alias(room_id, alias):
                request = urllib.request.Request(
                    base + f"/v1/rooms/{room_id}/alias",
                    data=json.dumps({"alias": alias}).encode(),
                    headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(request) as response:
                    return json.load(response)

            self.assertEqual(post_alias(room["id"], "Desde HTTP")["room"]["alias"], "Desde HTTP")
            self.assertEqual(post_alias(room["id"], "")["room"]["alias"], "")
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(urllib.request.Request(
                    base + "/v1/rooms/room_missing/alias",
                    data=json.dumps({"alias": "x"}).encode(),
                    headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
                    method="POST"))
            self.assertNotEqual(missing.exception.code, 200)
            missing.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_models_catalog_and_bot_model_write(self):
        hermes_home = self.root / "hermes"
        profile_dir = hermes_home / "profiles" / "scribe"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text(
            "# profile config\nmodel:\n  default: old-model\n  provider: old-provider\n  base_url: ''\nagent:\n  loop: true\n")
        (hermes_home / "profiles" / "empty").mkdir(parents=True)
        (hermes_home / "profiles" / "empty" / "config.yaml").write_text("agent:\n  loop: false\n")

        self.db.sync_profiles([
            {"name": "scribe", "display_name": "Scribe", "model": "old-model", "provider": "old-provider"},
            {"name": "empty", "display_name": "Empty", "model": "", "provider": ""},
        ])

        original_home = service.HERMES_HOME
        service.HERMES_HOME = hermes_home
        try:
            catalog = service.models_catalog(self.db)
            pairs = {(m["provider"], m["model"]) for m in catalog}
            self.assertIn(("old-provider", "old-model"), pairs)

            bot = self.db.update_bot("scribe", model="kimi-k3", provider="kimi-coding")
            self.assertEqual(bot["model"], "kimi-k3")
            self.assertEqual(bot["provider"], "kimi-coding")

            written = (profile_dir / "config.yaml").read_text()
            self.assertIn("default: 'kimi-k3'", written)
            self.assertIn("provider: 'kimi-coding'", written)
            self.assertIn("base_url: ''", written)
            self.assertIn("# profile config", written)

            catalog = service.models_catalog(self.db)
            pairs = {(m["provider"], m["model"]) for m in catalog}
            self.assertIn(("kimi-coding", "kimi-k3"), pairs)
            self.assertIn(("old-provider", "old-model"), pairs)

            with self.assertRaises(ValueError):
                self.db.update_bot("scribe", model="x" * 200)
        finally:
            service.HERMES_HOME = original_home

    def test_artifact_is_room_owned_checksum_verified_and_range_readable(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Media", ["default"])
        body = b"0123456789"
        artifact = self.db.store_artifact(
            project["id"], room["id"], "clip.mp4", "video/mp4", body, "user"
        )
        self.assertEqual(artifact["sha256"], hashlib.sha256(body).hexdigest())
        content, start, end, total = self.db.read_artifact(artifact["id"], 2, 5)
        self.assertEqual((content, start, end, total), (b"2345", 2, 5, 10))
        self.assertEqual(self.db.snapshot(room_id=room["id"])["artifacts"][0]["roomId"], room["id"])

    def test_cursor_backfill_has_stable_ids_and_explicit_gap(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Realtime", ["default"])
        for index in range(5):
            self.db.append_event(project["id"], room["id"], None, "status", "ares", {"text": str(index)})
        page = self.db.events_after(2, limit=2)
        self.assertEqual([row["cursor"] for row in page["events"]], [3, 4])
        self.assertFalse(page["gap"])
        self.assertTrue(all(row["id"] for row in page["events"]))
        gap = self.db.events_after(-50, limit=2)
        self.assertTrue(gap["gap"])

    def test_project_path_identity_includes_node(self):
        local = self.db.create_project("Local", str(self.project_dir), "local")
        remote = self.db.create_project("Remote", str(self.project_dir), "peer-a")
        self.assertNotEqual(local["id"], remote["id"])
        self.assertNotEqual(local["locationKey"], remote["locationKey"])

    def test_hermes_sessions_become_rooms_grouped_by_project_path(self):
        second_dir = self.root / "second-code"
        second_dir.mkdir()
        first = {
            "id": str(self.project_dir), "label": "Code", "path": str(self.project_dir),
            "repos": [{"groups": [{"sessions": [
                {"id": "session-a", "title": "Existing room", "source": "cli", "last_active": 42},
            ]}]}],
        }
        second = {
            "id": str(second_dir), "label": "Second", "path": str(second_dir),
            "repos": [{"groups": [{"sessions": [
                {"id": "session-b", "title": "Other room", "source": "cli", "last_active": 43},
            ]}]}],
        }

        self.assertEqual(self.db.sync_hermes_project("default", first), 1)
        self.assertEqual(self.db.sync_hermes_project("default", second), 1)
        self.assertEqual(self.db.sync_hermes_project("default", first), 0)

        snapshot = self.db.snapshot()
        projects = {project["path"]: project["id"] for project in snapshot["projects"]}
        rooms = {room["externalSessionId"]: room for room in snapshot["rooms"]}
        self.assertEqual(rooms["session-a"]["projectId"], projects[str(self.project_dir.resolve())])
        self.assertEqual(rooms["session-b"]["projectId"], projects[str(second_dir.resolve())])
        self.assertEqual(rooms["session-a"]["source"], "hermes")
        self.assertEqual(rooms["session-a"]["externalProfile"], "default")
        binding = self.db._db.execute(
            "SELECT stored_session_id FROM session_bindings WHERE room_id=?", (rooms["session-a"]["id"],)
        ).fetchone()
        self.assertEqual(binding["stored_session_id"], "session-a")

    def test_hermes_room_sync_hydrates_path_projects_and_mirrors_home_bucket(self):
        class DiscoveryBridge:
            def __init__(self): self.calls = []
            def call(self, method, params, timeout=60):
                self.calls.append((method, dict(params)))
                if method == "projects.tree":
                    return {"ok": True, "result": {"projects": [
                        {"id": "__no_project__", "isNoProject": True, "label": "Home", "sessionCount": 2},
                        {"id": str(self_outer.project_dir), "path": str(self_outer.project_dir),
                         "label": "Code", "sessionCount": 1},
                    ]}}
                if method == "projects.project_sessions":
                    if params.get("project_id") == "__no_project__":
                        return {"ok": True, "result": {"project": {
                            "id": "__no_project__", "path": None, "label": "Home",
                            "repos": [{"groups": [{"sessions": [
                                {"id": "home-a", "title": "Terminal chat", "source": "cli", "last_active": 21},
                                {"id": "home-b", "title": "Telegram chat", "source": "telegram", "last_active": 20},
                            ]}]}],
                        }}}
                    return {"ok": True, "result": {"project": {
                        "id": str(self_outer.project_dir), "path": str(self_outer.project_dir),
                        "label": "Code", "repos": [{"groups": [{"sessions": [
                            {"id": "existing", "title": "Existing", "source": "cli", "last_active": 12},
                        ]}]}],
                    }}}
                raise AssertionError(method)

        self_outer = self
        bridge = DiscoveryBridge()
        result = service.sync_hermes_rooms(self.db, bridge, [{"name": "default"}])
        self.assertEqual(result, {"profiles": 1, "projects": 2, "rooms": 3})
        self.assertEqual([method for method, _params in bridge.calls],
                         ["projects.tree", "projects.project_sessions", "projects.project_sessions"])
        snapshot = self.db.snapshot()
        home = next(project for project in snapshot["projects"] if project["path"] == "hermes://home")
        self.assertEqual(home["name"], "Home")
        home_rooms = [room for room in snapshot["rooms"] if room["projectId"] == home["id"]]
        self.assertEqual({room["externalSessionId"] for room in home_rooms}, {"home-a", "home-b"})
        self.assertTrue(all(room["source"] == "hermes" for room in home_rooms))

    def test_remote_hermes_sessions_become_peer_owned_rooms(self):
        legacy_project_id = self.db.create_project(
            "Node", "peer://host", node_id="host"
        )["id"]
        rows = [{
            "id": "remote::host::default::node-chat",
            "remoteSessionId": "node-chat",
            "peer": "host",
            "peerTitle": "Node",
            "agentTitle": "Hermes",
            "profile": "host/default",
            "title": "Working on Node",
            "lastActivityTs": 123,
            "messageCount": 4,
        }]

        result = service.sync_remote_hermes_rooms(self.db, rows)

        self.assertEqual(result, {"peers": 1, "rooms": 1})
        snapshot = self.db.snapshot()
        peer_projects = [item for item in snapshot["projects"] if item["node"] == "host"]
        self.assertEqual(len(peer_projects), 1)
        project = peer_projects[0]
        self.assertEqual(project["id"], legacy_project_id)
        room = next(item for item in snapshot["rooms"] if item["externalSessionId"] == rows[0]["id"])
        self.assertEqual(project["name"], "Hermes")
        self.assertEqual(project["path"], "peer://host/default")
        self.assertEqual(room["projectId"], project["id"])
        self.assertEqual(room["externalProfile"], "remote::host::default")
        binding = self.db._db.execute(
            "SELECT node_id,stored_session_id FROM session_bindings WHERE room_id=?", (room["id"],)
        ).fetchone()
        self.assertEqual((binding["node_id"], binding["stored_session_id"]), ("host", rows[0]["id"]))

    def test_remote_hermes_chat_hydrates_its_peer_room(self):
        reference = "remote::host::default::node-chat"
        service.sync_remote_hermes_rooms(self.db, [{
            "id": reference, "peer": "host", "peerTitle": "Node", "profile": "host/default",
            "title": "Working on Node", "lastActivityTs": 123,
        }])
        chat = {
            "id": reference,
            "peer": "host",
            "profile": "host/default",
            "lastActivityTs": 125,
            "activity": "remote agent working",
            "messages": [
                {"id": "remote-user", "role": "user", "content": "Node prompt", "timestamp": 124},
                {"id": "remote-assistant", "role": "assistant", "content": "Node reply", "timestamp": 125},
            ],
        }

        self.assertEqual(service.sync_remote_hermes_chat(self.db, chat), 2)
        self.assertEqual(service.sync_remote_hermes_chat(self.db, chat), 0)
        room = next(item for item in self.db.snapshot()["rooms"] if item["externalSessionId"] == reference)
        events = self.db.snapshot(room_id=room["id"])["events"]
        self.assertEqual([event["text"] for event in events[-2:]], ["Node prompt", "Node reply"])
        self.assertEqual(self.db.snapshot(room_id=room["id"])["rooms"][0]["externalActivity"],
                         "remote agent working")

    def test_hermes_transcript_mirror_is_idempotent_and_tracks_live_activity(self):
        project = {
            "id": str(self.project_dir), "label": "Code", "path": str(self.project_dir),
            "repos": [{"groups": [{"sessions": [
                {"id": "current-chat", "title": "Current chat", "source": "cli", "last_active": 42},
            ]}]}],
        }
        self.db.sync_hermes_project("default", project)
        messages = [
            {"id": "message-user", "role": "user", "content": "Show this chat", "timestamp": 40},
            {
                "id": "message-call", "role": "assistant", "content": "", "timestamp": 41,
                "reasoning_content": "private chain of thought", "token_count": 999,
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "terminal", "arguments": json.dumps({
                        "command": "python3 -m unittest discover -s tests -v",
                        "password": "do-not-display",
                    })},
                }],
            },
            {
                "id": "message-tool", "role": "tool", "tool_name": "terminal", "tool_call_id": "call-1",
                "content": json.dumps({"output": "Ran 38 tests\nOK", "exit_code": 0, "error": None}),
                "timestamp": 42.25,
            },
            {"id": "message-assistant", "role": "assistant", "content": "Mirrored in Ares", "timestamp": 42.5},
        ]
        metadata = {"last_activity_at": 43, "last_activity_description": "tool running: terminal"}

        self.assertEqual(self.db.sync_hermes_transcript("default", "current-chat", messages, metadata), 5)
        self.assertEqual(self.db.sync_hermes_transcript("default", "current-chat", messages, metadata), 0)
        room = next(room for room in self.db.snapshot()["rooms"] if room["externalSessionId"] == "current-chat")
        snapshot = self.db.snapshot(room_id=room["id"])
        self.assertEqual([event["kind"] for event in snapshot["events"][-5:]], [
            "message.user", "reasoning.status", "tool.call", "tool.result", "message.assistant",
        ])
        reasoning, call, result = snapshot["events"][-4:-1]
        self.assertEqual(reasoning["text"], "Thinking · 999 tokens")
        self.assertNotIn("private chain of thought", json.dumps(reasoning))
        self.assertEqual(call["text"], "$ python3 -m unittest discover -s tests -v")
        self.assertEqual(call["payload"]["arguments"]["password"], "[redacted]")
        self.assertEqual(result["text"], "Ran 38 tests\nOK")
        self.assertEqual(result["payload"]["exitCode"], 0)
        self.assertAlmostEqual(result["payload"]["durationSeconds"], 1.25)
        self.assertEqual(snapshot["events"][-1]["text"], "Mirrored in Ares")
        self.assertEqual(snapshot["rooms"][0]["externalActivity"], "tool running: terminal")

    def test_live_transcript_sync_prioritizes_working_session_over_stale_discovery_order(self):
        project = {
            "id": str(self.project_dir), "label": "Code", "path": str(self.project_dir),
            "repos": [{"groups": [{"sessions": [
                {"id": "recent-session", "title": "Recent", "source": "cli", "last_active": 200},
                {"id": "active-session", "title": "Active", "source": "cli", "last_active": 100},
            ]}]}],
        }
        self.db.sync_hermes_project("default", project)
        self.db.sync_hermes_transcript(
            "default", "active-session",
            [{"id": "active-user", "role": "user", "content": "Run", "timestamp": 50}],
            {"last_activity_at": 300, "last_activity_description": "working"},
        )

        class Bridge:
            def __init__(self):
                self.message_sessions = []
                self.since = []

            def call(self, method, params, timeout=0):
                if method == "session.active_list":
                    return {"ok": True, "result": {"sessions": [
                        {"session_key": "active-session", "status": "working"},
                    ]}}
                if method == "session.messages":
                    self.message_sessions.append(params["session_id"])
                    self.since.append(params["since"])
                    return {"ok": True, "result": {"messages": [], "session": {}}}
                raise AssertionError(method)

        bridge = Bridge()
        result = service.sync_recent_hermes_transcripts(self.db, bridge, limit=1)

        self.assertEqual({"rooms": 1, "messages": 0}, result)
        self.assertEqual(["active-session"], bridge.message_sessions)
        self.assertEqual([50.0], bridge.since)

    def test_live_transcript_mirror_reconciles_token_deltas_into_one_streaming_message(self):
        project = {
            "id": str(self.project_dir), "label": "Code", "path": str(self.project_dir),
            "repos": [{"groups": [{"sessions": [
                {"id": "live-chat", "title": "Live chat", "source": "cli", "last_active": 42},
            ]}]}],
        }
        self.db.sync_hermes_project("default", project)
        mirror = service.HermesLiveTranscriptMirror(self.db)
        mirror.update_active_sessions("default", [
            {"id": "runtime-live", "session_key": "live-chat", "status": "working"},
        ])

        mirror.observe({"session_id": "runtime-live", "type": "message.start", "payload": {}})
        mirror.observe({"session_id": "runtime-live", "type": "reasoning.delta",
                        "payload": {"text": "private reasoning must never render"}})
        mirror.observe({"session_id": "runtime-live", "type": "message.delta", "payload": {"text": "Hello "}})
        mirror.observe({"session_id": "runtime-live", "type": "message.delta", "payload": {"text": "world"}})

        room = next(room for room in self.db.snapshot()["rooms"] if room["externalSessionId"] == "live-chat")
        live = self.db.live_events(room["id"])
        self.assertEqual(["reasoning.status", "message.assistant"], [event["kind"] for event in live])
        self.assertEqual("Hello world", live[-1]["text"])
        self.assertTrue(live[-1]["payload"]["streaming"])
        self.assertNotIn("private reasoning", json.dumps(live))

        mirror.observe({"session_id": "runtime-live", "type": "message.complete",
                        "payload": {"text": "Hello world"}})
        completed = self.db.live_events(room["id"])[-1]
        self.assertEqual("Hello world", completed["text"])
        self.assertFalse(completed["payload"]["streaming"])
        self.assertEqual(
            [event["id"] for event in self.db.live_events(room["id"])],
            [event["id"] for event in mirror.live_events(room["id"])],
        )

    def test_media_discovery_accepts_project_outputs_and_rejects_paths_outside_allowed_roots(self):
        output = self.project_dir / "render.mp4"
        output.write_bytes(b"video")
        outside = self.root / "secret.png"
        outside.write_bytes(b"secret")
        paths = service.discover_media_paths(
            {"result": {"output": f"created {output}", "other": str(outside)}}, [self.project_dir])
        self.assertEqual(paths, [output.resolve()])

    def test_execution_runner_persists_live_gateway_activity_and_final_reply(self):
        group = self.db.create_group("Omarchy", "Use the project test conventions.")
        project = self.db.create_project("Ares", str(self.project_dir), "local", group["id"])
        room = self.db.create_room(project["id"], "Real execution", ["default"])
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Reply", "turn-1")
        generated = self.project_dir / "bot-output.png"
        generated.write_bytes(b"generated-image")

        class FakeBridge:
            def __init__(self):
                self.listener = None
                self.resumes = 0
                self.submitted_text = ""

            def add_listener(self, listener):
                self.listener = listener

            def remove_listener(self, listener):
                self.listener = None

            def call(self, method, params, timeout=60):
                if method == "session.resume":
                    self.resumes += 1
                    if self.resumes == 1:
                        return {"ok": False, "code": 4007, "error": "absent"}
                    return {
                        "ok": True,
                        "result": {
                            "session_id": "runtime-a",
                            "stored_session_id": "stored-a",
                            "running": False,
                            "messages": [
                                {"role": "user", "content": "Reply"},
                                {"role": "assistant", "content": "Live answer"},
                            ],
                        },
                    }
                if method == "session.create":
                    return {"ok": True, "result": {"session_id": "runtime-a", "stored_session_id": "stored-a", "messages": []}}
                if method == "prompt.submit":
                    self.submitted_text = params["text"]
                    self.listener({"type": "tool.start", "session_id": "runtime-a", "payload": {"name": "terminal", "password": "secret"}})
                    self.listener({"type": "approval.request", "session_id": "runtime-a", "payload": {"request_id": "approval-1", "tool": "terminal"}})
                    self.listener({"type": "message.delta", "session_id": "runtime-a", "payload": {"text": "Live "}})
                    self.listener({"type": "message.delta", "session_id": "runtime-a", "payload": {"text": "answer"}})
                    self.listener({"type": "tool.complete", "session_id": "runtime-a", "payload": {"name": "terminal", "result": {"output": str(generated)}}})
                    return {"ok": True, "result": {}}
                raise AssertionError(method)

        bridge = FakeBridge()
        runner = service.ExecutionRunner(self.db, bridge, poll_interval=0)
        result = runner.run_execution(execution["id"])
        self.assertEqual(result["status"], "completed")
        snapshot = self.db.snapshot(room_id=room["id"])
        kinds = [event["kind"] for event in snapshot["events"]]
        self.assertIn("tool.start", kinds)
        self.assertIn("approval.request", kinds)
        self.assertNotIn("message.delta", kinds)
        self.assertNotIn("reasoning.delta", kinds)
        self.assertIn("tool.complete", kinds)
        self.assertIn("message.assistant", kinds)
        assistants = [event for event in snapshot["events"] if event["kind"] == "message.assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["payload"]["text"], "Live answer")
        self.assertTrue(assistants[0]["html"])
        tool = next(event for event in snapshot["events"] if event["kind"] == "tool.start")
        self.assertEqual(tool["payload"]["password"], "[redacted]")
        self.assertEqual(snapshot["executions"][0]["finalText"], "Live answer")
        self.assertEqual(snapshot["artifacts"][0]["name"], "bot-output.png")
        self.assertEqual(snapshot["artifacts"][0]["author"], "default")
        self.assertIn("Use the project test conventions.", bridge.submitted_text)

    def test_execution_runner_resumes_the_existing_session_bound_to_an_imported_room(self):
        project = self.db.create_project("Existing", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Existing room", ["default"])
        self.db.bind_session(room["id"], "default", "local", "existing-session")
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Continue", "existing-turn")

        class ExistingBridge:
            def __init__(self):
                self.listener = None
                self.calls = []
                self.resumes = 0
            def add_listener(self, listener): self.listener = listener
            def remove_listener(self, _listener): self.listener = None
            def call(self, method, params, timeout=60):
                self.calls.append((method, dict(params)))
                if method == "session.resume":
                    self.resumes += 1
                    messages = [{"role": "assistant", "content": "Previous answer"}]
                    if self.resumes > 1:
                        messages += [{"role": "user", "content": "Continue"},
                                     {"role": "assistant", "content": "Continued answer"}]
                    return {"ok": True, "result": {"session_id": "runtime-existing",
                                                       "stored_session_id": "existing-session",
                                                       "running": False, "messages": messages}}
                if method == "prompt.submit": return {"ok": True, "result": {"status": "streaming"}}
                raise AssertionError(method)

        bridge = ExistingBridge()
        result = service.ExecutionRunner(self.db, bridge, poll_interval=0).run_execution(execution["id"])
        self.assertEqual(result["finalText"], "Continued answer")
        self.assertEqual(bridge.calls[0], ("session.resume", {"session_id": "existing-session", "profile": "default"}))
        self.assertNotIn("session.create", [method for method, _params in bridge.calls])

    def test_http_api_requires_auth_and_serves_byte_ranges(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Web parity", ["default"])
        artifact = self.db.store_artifact(project["id"], room["id"], "clip.mp4", "video/mp4", b"0123456789", "user")
        server = service.create_http_server(self.db, "127.0.0.1", 0, "test-token", web_root=self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + "/v1/state")
            self.assertEqual(denied.exception.code, 401)
            denied.exception.close()
            request = urllib.request.Request(base + "/v1/state", headers={"Authorization": "Bearer test-token"})
            with urllib.request.urlopen(request) as response:
                state = json.load(response)
            self.assertEqual(state["rooms"][0]["id"], room["id"])

            request = urllib.request.Request(
                base + artifact["url"],
                headers={"Authorization": "Bearer test-token", "Range": "bytes=2-5"},
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
                self.assertEqual(response.read(), b"2345")

            upload = urllib.request.Request(
                base + "/v1/artifacts?" + urllib.parse.urlencode({
                    "projectId": project["id"],
                    "roomId": room["id"],
                    "name": "from-qml.png",
                    "author": "user",
                }),
                data=b"qml-image",
                headers={"Authorization": "Bearer test-token", "Content-Type": "image/png"},
                method="PUT",
            )
            with urllib.request.urlopen(upload) as response:
                uploaded = json.load(response)["artifact"]
            self.assertEqual(uploaded["name"], "from-qml.png")
            self.assertEqual(uploaded["mime"], "image/png")
            self.assertEqual(self.db.read_artifact(uploaded["id"])[0], b"qml-image")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_http_api_resolves_an_execution_approval(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Approval", ["default"])
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Run", "approval-http")
        self.db.bind_session(room["id"], "default", "local", "stored", "runtime")

        class Bridge:
            def call(self, method, params, timeout=30):
                return {"ok": True, "result": {"resolved": 1}}

        server = service.create_http_server(self.db, "127.0.0.1", 0, "test-token", web_root=self.root, bridge=Bridge())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/v1/executions/{execution['id']}/approval",
                data=json.dumps({"requestId": "approval-1", "choice": "once"}).encode(),
                headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertTrue(result["resolved"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_execution_runner_finishes_provider_inflight_error_instead_of_polling_forever(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Provider error", ["default"])
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Reply", "turn-error")

        class ErrorBridge:
            def add_listener(self, _listener): pass
            def remove_listener(self, _listener): pass
            def call(self, method, _params, timeout=60):
                if method == "session.resume":
                    return {"ok": True, "result": {"session_id": "runtime-error", "stored_session_id": "stored-error", "messages": [], "running": False,
                                                           "inflight": {"status": "error", "error": "provider rejected model"}}}
                if method == "prompt.submit": return {"ok": True, "result": {}}
                raise AssertionError(method)

        result = service.ExecutionRunner(self.db, ErrorBridge(), poll_interval=0, deadline_seconds=1).run_execution(execution["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "provider rejected model")

    def test_reopen_marks_unfinished_execution_interrupted(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Recovery", ["default"])
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Reply", "recover-1")
        path, artifacts = self.db.path, self.db.artifact_root
        self.db.close()
        self.db = service.AresStore(path, artifacts)
        recovered = self.db.execution(execution["id"])
        self.assertEqual(recovered["status"], "interrupted")
        kinds = [event["kind"] for event in self.db.snapshot(room_id=room["id"])["events"]]
        self.assertIn("execution.interrupted", kinds)

    def test_attachment_delivery_starts_pending_until_the_execution_route_accepts_it(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Attachment route", ["default"])
        artifact = self.db.store_artifact(project["id"], room["id"], "image.png", "image/png", b"not-a-real-png", "user")
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Inspect it", "media-1", [artifact["id"]])
        linked = self.db.execution_artifacts(execution["id"])
        self.assertEqual(linked[0]["deliveryState"], "pending")
        events = self.db.snapshot(room_id=room["id"])["events"]
        delivery = next(event for event in events if event["kind"] == "artifact.delivery.pending")
        self.assertEqual(delivery["payload"]["artifactId"], artifact["id"])

    def test_execution_runner_delivers_image_to_hermes_before_prompt_submission(self):
        project = self.db.create_project("Ares", str(self.project_dir), "local")
        room = self.db.create_room(project["id"], "Image", ["default"])
        artifact = self.db.store_artifact(project["id"], room["id"], "image.png", "image/png", b"not-a-real-png", "user")
        execution = self.db.accept_prompt(project["id"], room["id"], "default", "Inspect it", "media-run", [artifact["id"]])

        class MediaBridge:
            def __init__(self):
                self.listener = None
                self.resumes = 0
                self.attached_bytes = b""
            def add_listener(self, listener): self.listener = listener
            def remove_listener(self, _listener): self.listener = None
            def call(self, method, params, timeout=60):
                if method == "session.resume":
                    self.resumes += 1
                    if self.resumes == 1: return {"ok": False, "code": 4007}
                    return {"ok": True, "result": {"session_id": "runtime", "stored_session_id": "stored", "running": False,
                                                       "messages": [{"role": "assistant", "content": "I saw it"}]}}
                if method == "session.create": return {"ok": True, "result": {"session_id": "runtime", "stored_session_id": "stored", "messages": []}}
                if method == "image.attach_bytes":
                    import base64
                    self.attached_bytes = base64.b64decode(params["content_base64"])
                    return {"ok": True, "result": {"attached": True, "path": "/session/image.png"}}
                if method == "prompt.submit": return {"ok": True, "result": {"status": "streaming"}}
                raise AssertionError(method)

        bridge = MediaBridge()
        result = service.ExecutionRunner(self.db, bridge, poll_interval=0).run_execution(execution["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(bridge.attached_bytes, b"not-a-real-png")
        self.assertEqual(self.db.execution_artifacts(execution["id"])[0]["deliveryState"], "delivered")
        kinds = [event["kind"] for event in self.db.snapshot(room_id=room["id"])["events"]]
        self.assertIn("artifact.delivery.accepted", kinds)

    def test_bot_sidecar_survives_profile_resync(self):
        hermes_home = self.root / "hermes"
        original_home = service.HERMES_HOME
        service.HERMES_HOME = hermes_home
        try:
            self.db.sync_profiles([{"name": "design", "display_name": "design",
                                    "description": "Desktop description", "model": "gpt-5.6-terra",
                                    "provider": "openai-codex"}])
            first = self.db.update_bot("design", name="Diseñador", description="Experto UI",
                                       face_shape="hexagon", color_role="accent")
            self.assertEqual(first["name"], "Diseñador")

            # Simulate service/plugin restart: a fresh store re-syncs from the Desktop.
            fresh = service.AresStore(self.root / "ares2.db", self.root / "artifacts2")
            try:
                fresh.sync_profiles([{"name": "design", "display_name": "design",
                                      "description": "Desktop description", "model": "gpt-5.6-terra",
                                      "provider": "openai-codex"}])
                again = next(bot for bot in fresh.snapshot()["bots"] if bot["id"] == "design")
                self.assertEqual(again["name"], "Diseñador")
                self.assertEqual(again["description"], "Experto UI")
                self.assertEqual(again["faceShape"], "hexagon")
                self.assertEqual(again["colorRole"], "accent")
            finally:
                fresh.close()

            # Adopt-on-differ: an existing customized row without sidecar keeps its values.
            service.HERMES_HOME = self.root / "hermes-legacy"
            legacy = service.AresStore(self.root / "ares3.db", self.root / "artifacts3")
            try:
                legacy._db.execute(
                    "INSERT INTO bots(id,name,description,profile,node_id,model,provider,face_shape,color_role,enabled) "
                    "VALUES('design','Legado','Desc legado','design','local','m1','p1','square','accent',1) "
                    "ON CONFLICT(id) DO NOTHING")
                legacy.sync_profiles([{"name": "design", "display_name": "design",
                                       "description": "Desktop description", "model": "m2", "provider": "p2"}])
                adopted = next(bot for bot in legacy.snapshot()["bots"] if bot["id"] == "design")
                self.assertEqual(adopted["name"], "Legado")
                self.assertEqual(adopted["description"], "Desc legado")
                self.assertEqual(adopted["model"], "m1")
            finally:
                legacy.close()
        finally:
            service.HERMES_HOME = original_home

    def test_tool_result_text_unwraps_json_and_multimodal(self):
        store = self.db
        group = store.create_group("G")
        project = store.create_project("P", str(self.project_dir), "local", group["id"])
        room = store.create_room(project["id"], "R", ["default"])
        cases = [
            ('{"output": "plain out"}', "plain out"),
            ('{"_multimodal": true, "text_summary": "summary here", "content": []}', "summary here"),
            ('{"_multimodal": true, "content": [{"type": "text", "text": "part one"},'
             ' {"type": "image", "url": "x"}, {"type": "text", "text": "part two"}]}', "part one\npart two"),
            ('plain string result', "plain string result"),
            ('{"output": {"nested": [1, 2]}}', '{\n  "nested": [\n    1,\n    2\n  ]\n}'),
            ('{"unrelated": "not an envelope"}', '{"unrelated": "not an envelope"}'),
        ]
        for index, (raw, expected) in enumerate(cases):
            decoded = json.loads(raw) if raw.startswith("{") else raw
            if index % 2 == 0:
                # Structured payload: the service unwraps output at read time.
                payload = {"output": decoded, "toolName": "terminal"}
            else:
                # Historical row: the raw JSON envelope is baked into text.
                payload = {"text": raw, "toolName": "terminal"}
            event = store.append_event(project["id"], room["id"], None, "tool.complete", "tool", payload)
            dto = next(e for e in store.snapshot(room_id=room["id"])["events"] if e["id"] == event["id"])
            self.assertEqual(dto["text"], expected, msg=raw[:40])

    def test_tool_start_arguments_become_card_body(self):
        store = self.db
        group = store.create_group("G")
        project = store.create_project("P", str(self.project_dir), "local", group["id"])
        room = store.create_room(project["id"], "R", ["default"])
        cases = [
            ({"toolName": "terminal", "args": {"command": "ls -la /tmp"}}, "ls -la /tmp"),
            ({"toolName": "search_files", "args": {"pattern": "TODO"}}, '{\n  "pattern": "TODO"\n}'),
            ({"toolName": "terminal", "args": "raw arg string"}, "raw arg string"),
        ]
        for payload, expected in cases:
            event = store.append_event(project["id"], room["id"], None, "tool.start", "tool", payload)
            dto = next(e for e in store.snapshot(room_id=room["id"])["events"] if e["id"] == event["id"])
            self.assertEqual(dto["text"], expected, msg=str(payload)[:60])

    def test_model_change_unbinds_bot_sessions(self):
        hermes_home = self.root / "hermes"
        profile_dir = hermes_home / "profiles" / "scribe"
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text("model:\n  default: old-model\n  provider: old-provider\n")
        (hermes_home / "profiles" / "empty").mkdir(parents=True)
        (hermes_home / "profiles" / "empty" / "config.yaml").write_text("agent:\n  loop: false\n")
        self.db.sync_profiles([
            {"name": "scribe", "display_name": "Scribe", "model": "old-model", "provider": "old-provider"},
            {"name": "empty", "display_name": "Empty", "model": "", "provider": ""},
        ])
        original_home = service.HERMES_HOME
        service.HERMES_HOME = hermes_home
        try:
            project = self.db.create_project("P", str(self.project_dir))
            room = self.db.create_room(project["id"], "R", ["scribe"])
            other_room = self.db.create_room(project["id"], "R2", ["empty"])
            self.db.bind_session(room["id"], "scribe", "local", "stored-scribe-1", "runtime-scribe-1")
            self.db.bind_session(other_room["id"], "empty", "local", "stored-empty-1", "runtime-empty-1")

            updated = self.db.update_bot("scribe", model="kimi-k3", provider="kimi-coding")
            self.assertEqual(updated["model"], "kimi-k3")
            unbound = {item["storedSessionId"] for item in updated["unboundSessions"]}
            self.assertEqual(unbound, {"stored-scribe-1"})

            rows = self.db._db.execute("SELECT bot_id FROM session_bindings ORDER BY bot_id").fetchall()
            self.assertEqual([row["bot_id"] for row in rows], ["empty"])

            # Renaming must NOT reset sessions; only a model/provider switch does.
            updated = self.db.update_bot("scribe", name="Scribe Renamed")
            self.assertEqual(updated["unboundSessions"], [])
        finally:
            service.HERMES_HOME = original_home

    def test_peer_sidecar_survives_peer_resync(self):
        hermes_home = self.root / "hermes"
        original_home = service.HERMES_HOME
        service.HERMES_HOME = hermes_home
        try:
            self.db.sync_peers([{"name": "lab-peer", "title": "lab-peer", "status": "ok",
                                 "url": "http://192.0.2.20:8642"}])
            self.db.update_peer_preferences("lab-peer", "Lab Peer", "accent")

            fresh = service.AresStore(self.root / "ares2.db", self.root / "artifacts2")
            try:
                fresh.sync_peers([{"name": "lab-peer", "title": "lab-peer", "status": "ok",
                                   "url": "http://192.0.2.20:8642"}])
                again = next(peer for peer in fresh.snapshot()["peers"] if peer["id"] == "lab-peer")
                self.assertEqual(again["name"], "Lab Peer")
            finally:
                fresh.close()
        finally:
            service.HERMES_HOME = original_home


if __name__ == "__main__":
    unittest.main()
