import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ControlMapUiContract(unittest.TestCase):
    def test_map_is_a_separate_control_mode_without_replacing_visual(self):
        room = (ROOT / 'ControlRoom.qml').read_text()
        self.assertIn('label: "Map"', room)
        self.assertIn('controller.controlMode === "map"', room)
        self.assertIn('ControlConstellation {', room)
        self.assertIn('ControlTiles {', room)
        self.assertIn('MapModel.build(controller.rooms, controller.controlItems, controller.bots, controller.projects)', room)
        panel = (ROOT / 'Panel.qml').read_text()
        self.assertIn('function controlMap()', panel)

    def test_map_has_bounded_navigation_and_room_actions_only(self):
        source = (ROOT / 'ControlMap.qml').read_text()
        for fragment in ('Flickable {', 'WheelHandler {', 'PinchHandler {',
                         'MapModel.zoomAt(', 'MapModel.fit(', 'MapModel.reset(',
                         'boundsBehavior: Flickable.StopAtBounds', 'BotFace {',
                         'model: roster', 'roomActivated(actor.node.roomId)',
                         'controller.botColorForRole(actor.node.colorRole)',
                         'node.roomName', 'node.projectName', 'node.additionalMembers',
                         'node.activity', 'node.parentWorking',
                         'visible: actor.node.botId !== ""'):
            self.assertIn(fragment, source)
        for fragment in ('signal roomAction(string roomId, string action)',
                         'HoverHandler {', '"Go to chat"', '"Draft"',
                         '"Archive"', '"Focus work"',
                         'MapModel.focus(map.mapNodes, viewport.width, viewport.height)'):
            self.assertIn(fragment, source)
        for forbidden in ('agent.prompt', 'appendLocalEvent(', 'archiveRoom(', 'hermes send'):
            self.assertNotIn(forbidden, source)

    def test_map_groups_rooms_by_project_with_host_as_metadata(self):
        source = (ROOT / 'ControlMap.qml').read_text()
        model = (ROOT / 'ControlMapModel.js').read_text()
        self.assertIn('function territories(nodes, projects)', model)
        self.assertIn('function focusBounds(t, viewportWidth, viewportHeight)', model)
        for fragment in ('id: territoryCanvas', 'onTerritoriesChanged: territoryCanvas.requestPaint()',
                         'MapModel.territories(map.mapNodes, map.controller.projects)',
                         'MapModel.focusBounds(t, viewport.width, viewport.height)',
                         'function fitTo(t)', 'id: territoryBar', '"All projects"',
                         't.name', 't.roomCount', 'modelData.node'):
            self.assertIn(fragment, source)
        self.assertNotIn('kind === "host"', source)


    def test_dense_overview_fresh_activity_focus_and_label_disclosure(self):
        source = (ROOT / 'ControlMap.qml').read_text()
        model = (ROOT / 'ControlMapModel.js').read_text()
        for fragment in ('MapModel.build(controller.rooms, controller.controlItems,',
                         'activityClock', 'Timer { interval: 30000',
                         'enabled: map.hasWork', 'No working or waiting rooms',
                         'if (view)', 'MapModel.showRoomCards(map.zoom)',
                         'area.workingCount', 'area.waitingCount',
                         'map.resetView()', 'map.territories))', 'view.zoom = 0.65',
                         'projectTitle.truncated', 'selectorTitle.truncated',
                         'Unassigned / Home', 't.kind === "unassigned"', 't.kind === "project"'):
            self.assertIn(fragment, source + model)
        self.assertNotIn('|| nodes[0]', model)

    def test_archive_toggle_and_viewport_roster_are_map_only_and_read_only(self):
        source = (ROOT / 'ControlMap.qml').read_text()
        control = (ROOT / 'ControlRoom.qml').read_text()
        self.assertIn('property bool showArchived: false', source)
        self.assertIn('controller.archivedRooms', source)
        self.assertIn('map.controller.archivedRooms.length + map.sourceArchivedCount', source)
        self.assertIn('map.showArchived)', source)
        self.assertIn('node.externalArchived === true', source)
        self.assertIn('MapModel.visibleNodes(map.dragVisibleNodes, map.zoom, viewport.contentX, viewport.contentY,', source)
        self.assertIn('var desired = map.visibleMapNodes', source)
        self.assertIn('visible: map.mapNodes.length === 0', source)
        self.assertIn('visible: !actor.node.archived && (actorHover.hovered || map.selectedRoomId === actor.node.roomId)', source)
        self.assertIn('text: actor.node.archived ? "ARCHIVED"', source)
        self.assertIn('map.controller.section = "archived"', source)
        self.assertIn('actor.node.sourceArchived ? "Open chat" : "Open Archived"', source)
        self.assertIn('visible: actor.node.archived', source)
        self.assertIn('if (actor.node.archived)', source)
        self.assertNotIn('map.roomAction(actor.node.roomId, "attach")', source)
        self.assertIn('onRoomActivated: function(roomId) { control.selectActionRoom(roomId) }', control)


if __name__ == '__main__':
    unittest.main()
