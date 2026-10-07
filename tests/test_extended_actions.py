"""Room-scoped Extended actions must never act on the selected sibling room."""
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "Panel.qml"
WORKSPACE = ROOT / "AresWorkspace.qml"
EXTENDED = ROOT / "ExtendedWorkspace.qml"


def qml_function(name):
    source = PANEL.read_text()
    match = re.search(r"^  function " + re.escape(name) + r"\([^)]*\) \{", source, re.M)
    if not match:
        raise AssertionError(f"missing QML function {name}")
    depth = 0
    for end in range(match.start(), len(source)):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():end + 1]
    raise AssertionError("unterminated function")


class ExtendedActionTests(unittest.TestCase):
    def test_pause_targets_tile_not_selected_room(self):
        fn = qml_function("toggleRoomPause")
        script = fn + """
            const calls = [];
            const root = {
              selectedProject: {id:'project'}, selectedRoom: {id:'selected',status:'active'},
              bridge: {lifecycle(room, action, done) {
                calls.push([room.id, action]); done({ok:true});
              }, reload() { calls.push(['reload']); }},
              activeRooms: [{id:'selected'}, {id:'tile'}], extendedRoomId: 'selected'
            };
            const bridge = root.bridge;
            delete root.bridge;
            toggleRoomPause.call(root, {id:'tile',status:'active'});
            console.log(JSON.stringify(calls));
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [["tile", "pause"], ["reload"]])

    def test_archive_unselected_tile_preserves_selected_room(self):
        fn = qml_function("archiveRoom")
        script = fn + """
            const calls = [];
            const root = {
              selectedRoomId: 'selected', extendedRoomId: 'selected',
              selectedRoom: {id:'selected'}, activeRooms: [{id:'selected'}, {id:'tile'}],
              bridge: {lifecycle(room, action, done) {
                calls.push([room.id, action]); done({ok:true});
              }, reload() { calls.push(['reload']); }}
            };
            const bridge = root.bridge;
            delete root.bridge;
            archiveRoom.call(root, {id:'tile'});
            console.log(JSON.stringify({calls, selected:root.selectedRoomId, master:root.extendedRoomId}));
        """
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"calls":[["tile", "archive"], ["reload"]],
            "selected":"selected", "master":"selected"})

    def test_extended_tiles_and_focus_have_per_room_lifecycle_controls(self):
        source = EXTENDED.read_text()
        tile = source.split("component RoomTile: Rectangle {", 1)[1]
        self.assertIn("controller.toggleRoomPause(tile.roomData)", tile)
        self.assertIn("controller.archiveRoom(tile.roomData)", tile)
        focus = source.split("id: focusTranscript", 1)[0]
        self.assertIn("controller.toggleRoomPause(extended.currentRoom)", focus)
        self.assertIn("controller.archiveRoom(extended.currentRoom)", focus)

    def test_visible_extended_tiles_attach_without_an_activation_click(self):
        source = (ROOT / 'ExtendedWorkspace.qml').read_text()
        self.assertIn('active: extended.controller.opened && extended.controller.extendedMode', source)
        self.assertIn('&& tile.warmViewport && (tile.detailed', source)
        self.assertNotIn('&& tile.inViewport && (tile.detailed', source)
        self.assertIn('cacheBuffer: cellHeight * 2', source)

    def test_edit_state_is_shared_across_compact_and_extended(self):
        panel = PANEL.read_text()
        compact = WORKSPACE.read_text()
        expanded = EXTENDED.read_text()
        self.assertIn("property bool editMode: false", panel)
        self.assertIn("controller.editMode = !controller.editMode", compact)
        self.assertIn("controller.editMode", expanded)
        self.assertIn("controller.appendLocalEvent(true, true)", expanded)
        self.assertIn("controller.roomText", expanded)
        self.assertIn("terminalPromptConfirmOpen", expanded)
        self.assertNotIn("onActivated: controller.appendLocalEvent()", expanded)

    def test_extended_editor_keeps_image_clipboard_and_text_paste(self):
        expanded = EXTENDED.read_text()
        self.assertIn("id: expandedPasteProc", expanded)
        self.assertIn("controller.addPendingImage", expanded)
        self.assertIn("expandedInput.paste()", expanded)
        self.assertIn("expandedArea.paste()", expanded)


if __name__ == "__main__":
    unittest.main()
