import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ControlRoomUiContract(unittest.TestCase):
    def test_control_room_is_separate_from_terminal_tiles_and_has_two_views(self):
        panel = (ROOT / "Panel.qml").read_text()
        workspace = (ROOT / "ControlRoom.qml").read_text()
        self.assertIn('root.section === "control"', panel)
        self.assertIn('function openControlRoom()', panel)
        self.assertIn('if (online && root.section === "control") root.ensureControlRoom()', panel)
        self.assertIn('room.source !== "control"', panel)
        self.assertIn('controller.controlMode === "text"', workspace)
        self.assertIn('controller.controlMode === "visual"', workspace)
        self.assertIn('ListView {', workspace)
        self.assertIn('ControlConstellation {', workspace)
        self.assertIn('SceneModel.build(controller.rooms, controller.controlItems, controller.bots, controller.projects, controller.controlLogs, 12)', workspace)
        scene = (ROOT / "ControlConstellation.qml").read_text()
        self.assertIn('BotFace {', scene)
        self.assertIn('shapeName: actor.shape', scene)
        self.assertIn('scene.controller.botColorForRole(actor.colorRole)', scene)
        self.assertIn('actor.projectName', scene)
        self.assertIn('required property string logLines', scene)
        self.assertIn('JSON.parse(logLines)', scene)
        self.assertIn('readonly property var currentLogs:', scene)
        self.assertIn('text: String(modelData.text)', scene)
        self.assertIn('ToolTip.visible: logHover.hovered && logText.truncated', scene)
        self.assertIn('Behavior on x', scene)
        self.assertIn('Behavior on y', scene)
        self.assertIn('transform: Translate {', scene)
        self.assertIn('ListModel { id: roster', scene)
        self.assertIn('Canvas {', scene)
        self.assertIn('Item {\n      id: studio', scene)
        self.assertIn('Item {\n      id: commons', scene)
        self.assertIn('Item {\n      id: signalZone', scene)
        self.assertNotIn('☕', scene)
        self.assertIn('No included rooms', scene)
        self.assertIn('signal roomAction(string roomId, string action)', scene)
        self.assertIn('onHoveredChanged: if (hovered) scene.hoverRoomId = actor.roomId', scene)
        for label in ('"Go to chat"', '"Draft"', '"Archive"'):
            self.assertIn(label, scene)
        self.assertGreaterEqual(workspace.count('onRoomAction: function(roomId, action)'), 2)

    def test_log_feed_is_bounded_and_read_only(self):
        panel = (ROOT / "Panel.qml").read_text()
        server = (ROOT / "server/src/http.rs").read_text()
        self.assertIn('property var controlLogs: ({})', panel)
        self.assertIn('controlLogs = value.logs || ({})', panel)
        self.assertIn('store.control_logs(', server)

    def test_control_endpoint_observes_remote_delegations_without_prompting(self):
        server = (ROOT / "server/src/http.rs").read_text()
        self.assertIn('crate::remote::observed_remote_activity(', server)
        self.assertIn('store.control_remote_items(', server)

    def test_room_membership_and_decisions_are_explicit_not_auto_sends(self):
        panel = (ROOT / "Panel.qml").read_text()
        workspace = (ROOT / "ControlRoom.qml").read_text()
        source = (ROOT / "AresWorkspace.qml").read_text()
        self.assertIn('function toggleControlIncluded(room)', panel)
        self.assertIn('function recordControlDecision(roomId, action, note)', panel)
        self.assertIn('if (action === "propose-prompt") stageControlInstruction(roomId, note)', panel)
        self.assertNotIn('roomText = note }', panel)
        self.assertIn('controller.toggleControlIncluded(controller.selectedRoom)', source)
        self.assertIn('controller.recordControlDecision(', workspace)
        self.assertIn('controller.createControlWorkRoom(', workspace)
        self.assertNotIn('appendLocalEvent(', workspace)
        self.assertNotIn('agent.prompt', workspace)

    def test_control_room_surfaces_the_review_only_laya_dataset_state(self):
        panel = (ROOT / "Panel.qml").read_text()
        workspace = (ROOT / "ControlRoom.qml").read_text()
        self.assertIn('property int layaExampleCount: 0', panel)
        self.assertIn('function refreshLayaTraining()', panel)
        self.assertIn('"/v1/control/training"', panel)
        self.assertIn('Laya dataset:', workspace)
        self.assertIn('manual review required', workspace)
        self.assertIn('ControlButton { label: "Refresh"', workspace)
        self.assertNotIn('ControlButton { label: "↻"', workspace)

    def test_map_actions_use_explicit_draft_and_confirmed_local_archive(self):
        workspace = (ROOT / "ControlRoom.qml").read_text()
        self.assertIn('onRoomAction: function(roomId, action)', workspace)
        self.assertIn('controller.stageControlInstruction(control.selectedRoomId, control.note)', workspace)
        self.assertIn('ActionModel.canArchive(', workspace)
        self.assertIn('archiveTargetRoomId', workspace)
        self.assertIn('controller.archiveRoom(room)', workspace)
        self.assertIn('label: "Cancel"', workspace)
        self.assertIn('label: "Archive locally"', workspace)
        self.assertNotIn('onRoomActivated: function(roomId) {\n            var pending', workspace)


    def test_map_editor_can_be_hidden_without_clearing_selection_or_draft(self):
        workspace = (ROOT / "ControlRoom.qml").read_text()
        self.assertIn('property bool editorOpen: false', workspace)
        self.assertIn('label: control.editorOpen ? "Hide editor" : "Show editor"', workspace)
        self.assertIn('control.editorOpen = !control.editorOpen', workspace)
        self.assertIn('visible: selectedRoom !== null && control.editorOpen', workspace)
        self.assertIn('else if (action === "draft")', workspace)
        self.assertIn('control.editorOpen = true', workspace)
        self.assertIn('decisionNote.forceActiveFocus()', workspace)
        self.assertIn('text: control.note', workspace)

    def test_editor_closes_when_leaving_map_or_changing_room_and_has_its_own_close_button(self):
        workspace = (ROOT / "ControlRoom.qml").read_text()
        self.assertIn('onVisibleChanged: {', workspace)
        self.assertIn('if (!visible) control.editorOpen = false', workspace)
        self.assertIn('function onControlModeChanged() { control.editorOpen = false }', workspace)
        selected = workspace.split('function selectActionRoom(roomId)', 1)[1].split('function openActionChat', 1)[0]
        self.assertIn('control.editorOpen = false', selected)
        editor = workspace.split('id: decisionNote', 1)[0].rsplit('Rectangle {', 1)[-1]
        self.assertIn('label: "×"', editor)
        self.assertIn('onActivated: control.editorOpen = false', editor)

    def test_communication_panel_reserves_adaptive_draft_space(self):
        workspace = (ROOT / "ControlRoom.qml").read_text()
        panel = workspace.split('id: decisionNote', 1)[0].rsplit('Rectangle {', 1)[-1]
        self.assertNotIn('Style.space(130)', panel)
        self.assertIn('Layout.preferredHeight: selectedRoom ? Math.min(', panel)
        self.assertIn('control.height', panel)
        self.assertIn('Layout.minimumHeight: selectedRoom ? actionFlow.implicitHeight + Style.space(100) : 0', panel)
        self.assertIn('id: actionFlow', workspace)
        self.assertIn('ScrollView {', workspace)
        self.assertIn('id: decisionNote', workspace)

    def test_communication_actions_wrap_without_changing_dispatch(self):
        workspace = (ROOT / "ControlRoom.qml").read_text()
        actions = workspace.split('id: decisionNote', 1)[1].split('RowLayout {', 1)[0]
        self.assertIn('Flow {', actions)
        self.assertIn('Layout.fillWidth: true', actions)
        self.assertIn('Layout.preferredHeight: implicitHeight', actions)
        for label in ('Open chat', 'Prepare in chat', 'Record option', 'Ask user', 'Archive', 'Exclude chat'):
            self.assertIn('label: "' + label + '"', actions)
        self.assertIn('controller.stageControlInstruction(control.selectedRoomId, control.note)', actions)
        self.assertIn('controller.recordControlDecision(control.selectedRoomId, "review-option", control.note)', actions)
        self.assertIn('controller.recordControlDecision(control.selectedRoomId, "ask-user", control.note)', actions)
        self.assertIn('control.archiveTargetRoomId = control.selectedRoomId', actions)
        self.assertIn('controller.toggleControlIncluded(control.selectedRoom)', actions)


    def test_source_archived_map_actions_are_read_only(self):
        workspace = (ROOT / "ControlRoom.qml").read_text()
        self.assertIn('if (room.externalArchived === true && action !== "chat") return', workspace)
        self.assertIn('if (room.externalArchived === true) { openActionChat(room); return }', workspace)
        self.assertIn('room.id === selectedRoomId && room.status === "active" && room.externalArchived !== true', workspace)


if __name__ == "__main__":
    unittest.main()
