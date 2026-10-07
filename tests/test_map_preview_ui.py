import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MapPreviewUiContract(unittest.TestCase):
    def test_eye_opens_bounded_read_only_preview_without_navigation(self):
        map_source = (ROOT / 'ControlMap.qml').read_text()
        preview = (ROOT / 'MapChatPreview.qml').read_text()
        control = (ROOT / 'ControlRoom.qml').read_text()
        self.assertIn('MapChatPreview {', map_source)
        self.assertIn('chatPreview.openRoom(actor.node.roomId, actor.node.roomName)', map_source)
        self.assertIn('label: "󰈈"', map_source)
        self.assertIn('bridge: control.bridge', control)
        self.assertIn('"/v1/rooms/" + encodeURIComponent(roomId) + "/preview"', preview)
        self.assertIn('bridge.request("GET",', preview)
        self.assertIn('.slice(-8)', preview)
        self.assertIn('Text.PlainText', preview)
        self.assertIn('role === "user" || role === "assistant"', preview)
        self.assertNotIn('roomAction(', preview)
        self.assertNotIn('attach', preview)
        self.assertNotIn('bridge.request("POST",', preview)
        self.assertNotIn('controller.section', preview)

    def test_project_drag_persists_only_on_release_and_does_not_pan(self):
        source = (ROOT / 'ControlMap.qml').read_text()
        self.assertIn('onTileOrderRequested:', (ROOT / 'ControlRoom.qml').read_text())
        self.assertIn('DragHandler {', source)
        self.assertIn('map.projectPositionRequested(', source)
        self.assertIn('map.draggedProjectId', source)
        self.assertIn('interactive: map.draggedProjectId === ""', source)

    def test_project_drag_handle_never_opens_a_tooltip_over_the_pointer(self):
        source = (ROOT / 'ControlMap.qml').read_text()
        drag_handle = source.split('id: projectDragHandle', 1)[1].split('DragHandler {', 1)[0]
        self.assertNotIn('ToolTip.visible:', drag_handle)
        self.assertNotIn('ToolTip.text:', drag_handle)
        self.assertNotIn('HoverHandler', drag_handle)


    def test_expanded_remote_title_explicitly_opens_bounded_source_preview(self):
        source = (ROOT / "ExtendedWorkspace.qml").read_text()
        self.assertIn('tileTerminalChat.openSourcePreview()', source)
        self.assertIn('focusTranscript.openSourcePreview()', source)
        self.assertIn('if (tile.remotePeerRoom) {', source)
        self.assertIn('extended.requestRemotePreview(tile.roomData.id)', source)
        self.assertIn('if (room && room.externalProfile', source)
        self.assertIn('Read-only paired-source preview', (ROOT / 'MapChatPreview.qml').read_text())
if __name__ == '__main__':
    unittest.main()

