"""Behavior contracts for the internal Ares tile order/navigation (no shell needed)."""
import json
import re
import subprocess
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "ExtendedWorkspace.qml"


def qml_function(source, name):
    match = re.search(r"\bfunction " + re.escape(name) + r"\([^)]*\)\s*\{", source)
    if not match:
        raise AssertionError(f"Missing QML function {name}")
    depth = 1
    pos = match.end()
    while depth:
        depth += (source[pos] == "{") - (source[pos] == "}")
        pos += 1
    return source[match.start():pos]


def run_qml_functions(names, body):
    source = SOURCE.read_text()
    # QML resolves root properties lexically; Node needs an explicit receiver.
    script = "\n".join(re.sub(r"\b(overviewRoomIds|controller)\b", r"this.\1",
                             qml_function(source, name)) for name in names)
    script += "\n" + body
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


class ExtendedNavigationTests(unittest.TestCase):
    def test_reorder_keeps_identity_and_survives_selection_and_poll(self):
        result = run_qml_functions(["moveRoomBefore", "syncVisibleRoomIds"], """
            const extended = { overviewRoomIds: ['a', 'b', 'c', 'd'],
              controller: {activeRooms: [{id:'a'}, {id:'b'}, {id:'c'}, {id:'d'}]} };
            extended.moveRoomBefore = moveRoomBefore;
            moveRoomBefore.call(extended, 'd', 'b');
            const moved = extended.overviewRoomIds.slice();
            syncVisibleRoomIds.call(extended);
            console.log(JSON.stringify({ moved, afterPoll: extended.overviewRoomIds,
              grownStack: extended.overviewRoomIds.filter(id => id !== 'c'),
              tilesAgain: extended.overviewRoomIds }));
        """)
        self.assertEqual(result, {"moved": ["a", "d", "b", "c"],
            "afterPoll": ["a", "d", "b", "c"],
            "grownStack": ["a", "d", "b"], "tilesAgain": ["a", "d", "b", "c"]})
    def test_header_selects_primary_without_capture_of_terminal_selection(self):
        source = SOURCE.read_text()
        tile = source.split("component RoomTile: Rectangle {", 1)[1]
        self.assertIn("id: headerSelect", tile)
        self.assertIn("onTapped: tile.growRequested(tile.roomData)", tile)
        self.assertIn("onPreviewActivated: tile.growRequested(tile.roomData)", tile)
        self.assertIn("previewReadOnly: !tile.detailed", tile)
        self.assertNotIn("previewReadOnly: !tile.detailed &&", tile)
        self.assertIn("id: stackGrid", source)
        self.assertIn("ScrollBar.vertical: ScrollBar { policy: ScrollBar.AlwaysOn }", source)
        self.assertIn("cacheBuffer: cellHeight * 2", source)
        self.assertNotIn("anchors.fill: tile", tile)

    def test_drag_handle_reorders_internal_tiles_without_grabbing_terminal(self):
        source = SOURCE.read_text()
        tile = source.split("component RoomTile: Rectangle {", 1)[1]
        self.assertIn("id: reorderHandle", tile)
        self.assertIn("onPositionChanged: function(mouse)", tile)
        self.assertIn("onReleased: function(mouse)", tile)
        self.assertIn("extended.moveRoomBefore", tile)
        self.assertIn("view.indexAt", tile)
        self.assertIn("tile.reorderView", tile)
        self.assertIn("cacheBuffer: cellHeight * 2", source)
        self.assertIn("onPreviewActivated: tile.growRequested(tile.roomData)", tile)

    def test_read_only_preview_excludes_terminal_from_input_and_scrolls_outer_grid(self):
        terminal = (SOURCE.parent / "TerminalChat.qml").read_text()
        overlay = terminal.split("visible: terminalChat.previewReadOnly && terminalChat.active", 1)[1].split("\n  }", 1)[0]
        self.assertIn("acceptedButtons: Qt.LeftButton", overlay)
        self.assertIn("onClicked: terminalChat.previewActivated()", overlay)
        self.assertIn("scrollGestureEnabled: false", overlay)
        self.assertIn("onWheel: function(wheel) { wheel.accepted = false }", overlay)
        self.assertIn("enabled: terminalChat.terminalInteractive", terminal)
        self.assertIn("passivePreviewWheel: terminalChat.previewReadOnly", terminal)
        self.assertNotIn("TapHandler { enabled: terminalChat.previewReadOnly", terminal)

    def test_tiled_views_keep_a_bounded_warm_band_when_switching_modes(self):
        source = SOURCE.read_text()
        self.assertIn("model: controller.opened ? extended.overviewRoomIds : []", source)
        self.assertIn("model: controller.opened ? extended.stackRoomIds : []", source)
        self.assertEqual(source.count("cacheBuffer: cellHeight * 2"), 2)
        self.assertEqual(source.count("warmViewport: y + height > GridView.view.contentY - GridView.view.cacheBuffer"), 2)
        tile = source.split("component RoomTile: Rectangle {", 1)[1]
        active = tile.split("id: tileTerminalChat", 1)[1].split("\n      }", 1)[0]
        self.assertIn("tile.warmViewport", active)
        self.assertNotIn("tile.inViewport", active)
        self.assertNotIn("tile.visible && extended.visible", active)

    def test_one_terminal_owner_per_room_across_tiled_modes(self):
        source = SOURCE.read_text()
        self.assertIn('previewMode: "overview"', source)
        self.assertIn('previewMode: "grown"', source)
        tile = source.split("component RoomTile: Rectangle {", 1)[1]
        active = tile.split("id: tileTerminalChat", 1)[1].split("\n      }", 1)[0]
        self.assertRegex(active, r'tile\.detailed\s*\? extended\.controller\.extendedViewMode === "grown"')
        self.assertIn('extended.controller.extendedViewMode === tile.previewMode', active)

    def test_fallback_preview_passes_wheel_to_outer_grid_and_grows_on_click(self):
        tile = SOURCE.read_text().split("component RoomTile: Rectangle {", 1)[1]
        fallback = tile.split("visible: tile.previewFallback", 1)[1].split("\n      Text {", 1)[0]
        self.assertIn("scrollGestureEnabled: false", fallback)
        self.assertIn("onWheel: function(wheel) { wheel.accepted = false }", fallback)
        self.assertIn("onClicked: tile.growRequested(tile.roomData)", fallback)


if __name__ == "__main__":
    unittest.main()
