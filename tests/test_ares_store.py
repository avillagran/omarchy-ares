import importlib.util
from importlib.machinery import SourceFileLoader
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "bin" / "ares-store"
LOADER = SourceFileLoader("ares_store", str(SCRIPT))
SPEC = importlib.util.spec_from_loader("ares_store", LOADER)
assert SPEC is not None
ares_store = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(ares_store)


class AresStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_state_has_no_orphan_rooms(self):
        state = ares_store.load_state(self.state)
        self.assertEqual(state["projects"], [])
        self.assertEqual(state["rooms"], [])
        self.assertEqual(state["events"], [])

    def test_save_is_valid_json_with_private_permissions(self):
        state = ares_store.empty_state()
        ares_store.save_state(state, self.state)
        self.assertEqual(json.loads(self.state.read_text())["version"], 2)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_room_lookup_requires_matching_project(self):
        state = ares_store.empty_state()
        state["projects"].append({"id": "prj_one", "name": "One", "path": "/tmp", "node": "local", "createdAt": 1})
        state["rooms"].append({"id": "room_one", "projectId": "prj_one", "name": "One room", "participants": ["default"], "status": "active", "createdAt": 1})
        self.assertIsNotNone(ares_store.project_by_id(state, "prj_one"))
        self.assertIsNotNone(ares_store.room_by_id(state, "room_one"))
        self.assertIsNone(ares_store.project_by_id(state, "prj_missing"))

    def test_blank_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "project name is required"):
            ares_store.require_text("   ", "project name")

    def test_v1_state_migrates_to_room_owned_monotonic_events(self):
        self.state.write_text(json.dumps({
            "version": 1,
            "projects": [{"id": "prj_one", "name": "One", "path": "/tmp", "node": "local", "createdAt": 1}],
            "rooms": [{"id": "room_one", "projectId": "prj_one", "name": "One room", "participants": ["default"], "status": "active", "createdAt": 1}],
            "events": [{"id": "evt_one", "roomId": "room_one", "kind": "room.created", "author": "ares", "text": "Room created", "at": 1}],
        }))
        state = ares_store.load_state(self.state)
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["events"][0]["sequence"], 1)
        self.assertEqual(state["nextSequence"], 2)
        self.assertEqual(state["rooms"][0]["status"], "active")

    def test_pause_records_requested_state_without_claiming_memory_released(self):
        state = ares_store.empty_state()
        state["projects"].append({"id": "prj_one", "name": "One", "path": "/tmp", "node": "local", "createdAt": 1})
        state["rooms"].append({"id": "room_one", "projectId": "prj_one", "name": "One room", "participants": ["default"], "status": "active", "createdAt": 1})
        ares_store.pause_room(state, "prj_one", "room_one")
        self.assertEqual(state["rooms"][0]["status"], "paused")
        self.assertEqual(state["rooms"][0]["memoryRelease"], "unconfirmed")
        self.assertEqual(state["events"][-1]["kind"], "room.pause.requested")

    def test_events_are_monotonic_and_cannot_cross_project_boundaries(self):
        state = ares_store.empty_state()
        state["projects"].extend([
            {"id": "prj_one", "name": "One", "path": "/tmp", "node": "local", "createdAt": 1},
            {"id": "prj_two", "name": "Two", "path": "/tmp/two", "node": "local", "createdAt": 1},
        ])
        state["rooms"].append({"id": "room_one", "projectId": "prj_one", "name": "One room", "participants": ["default"], "status": "active", "createdAt": 1})
        first = ares_store.append_event(state, "prj_one", "room_one", "message.user", "user", "First")
        second = ares_store.append_event(state, "prj_one", "room_one", "message.user", "user", "Second")
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
        with self.assertRaisesRegex(ValueError, "room does not belong"):
            ares_store.append_event(state, "prj_two", "room_one", "message.user", "user", "Rejected")


if __name__ == "__main__":
    unittest.main()
