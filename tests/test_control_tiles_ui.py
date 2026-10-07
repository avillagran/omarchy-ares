import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ControlTilesUiContract(unittest.TestCase):
    def test_all_projects_live_on_one_spatial_map_not_paginated_workspaces(self):
        room = (ROOT / 'ControlRoom.qml').read_text()
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('ControlTiles {', room)
        self.assertNotIn('ControlMap {', room)
        self.assertIn('import "ControlTilesModel.js" as TilesModel', tiles)
        self.assertIn('Flickable {', tiles)
        self.assertIn('TilesModel.mapLayout(', tiles)
        self.assertIn('TilesModel.fitZoom(', tiles)
        self.assertIn('TilesModel.visibleTiles(', tiles)
        self.assertIn('model: tiles.visibleSlots', tiles)
        self.assertNotIn('workspaceIndex', tiles)
        self.assertNotIn('TilesModel.page(', tiles)
        self.assertIn('TilesModel.swap(', tiles)
        self.assertNotIn('Canvas {', tiles)
        self.assertIn('WheelHandler {', tiles)
        self.assertNotIn('ToolTip', tiles)

    def test_zoom_changes_camera_without_reflowing_project_slots(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('function zoomAt(factor, x, y)', tiles)
        self.assertIn('TilesModel.zoomAt(', tiles)
        self.assertIn('contentWidth: tiles.layout.width * tiles.zoom', tiles)
        self.assertIn('contentHeight: tiles.layout.height * tiles.zoom', tiles)
        self.assertIn('scale: tiles.zoom', tiles)
        self.assertIn('function fitMap()', tiles)
        self.assertIn('TilesModel.zoomLevel(zoom, fitScale)', tiles)
        self.assertIn('visible: tiles.zoomLevel === "overview"', tiles)
        self.assertIn('visible: tiles.zoomLevel !== "overview"', tiles)
        self.assertIn('label: "−"', tiles)
        self.assertIn('label: "+"', tiles)
        self.assertIn('label: "Fit"', tiles)
        self.assertIn('WheelHandler {', tiles)
        self.assertIn('PinchHandler {', tiles)

    def test_project_territories_use_the_existing_distinct_map_palette(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('import "ControlMapModel.js" as MapModel', tiles)
        self.assertIn('MapModel.projectColors(', tiles)
        self.assertIn('tiles.palette[project.id]', tiles)

    def test_close_detail_exposes_existing_room_metadata_without_auto_loading_messages(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('chatEntry.modelData.eventCount', tiles)
        self.assertIn('chatEntry.modelData.externalUpdatedAt', tiles)
        self.assertIn('projectTile.project.path', tiles)
        self.assertNotIn('bridge.request(', tiles)

    def test_close_zoom_keeps_text_and_eye_hit_targets_readable(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('Math.max(3, pixels / Math.max(0.1, zoom))', tiles)
        self.assertIn('buttonText.implicitWidth + Style.space(10) / Math.max(0.1, tiles.zoom)', tiles)

    def test_drag_swaps_projects_and_each_tile_has_virtualized_chats_with_eye(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        panel = (ROOT / 'Panel.qml').read_text()
        self.assertIn('DragHandler {', tiles)
        self.assertIn('signal tileOrderRequested(var ids)', tiles)
        self.assertIn('onTileOrderRequested:', (ROOT / 'ControlRoom.qml').read_text())
        self.assertIn('function setControlTileOrder(ids)', panel)
        self.assertIn('controlTileOrder', panel)
        self.assertIn('ListView {', tiles)
        self.assertIn('MapChatPreview {', tiles)
        self.assertIn('chatPreview.openRoom(', tiles)
        self.assertIn('property bool showArchived: false', tiles)
        self.assertIn('Show archived (', tiles)
        self.assertNotIn('map.projectPositionRequested(', tiles)

    def test_mini_chat_count_surface_supports_drag_without_losing_click_to_zoom(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        miniature = tiles.split('visible: tiles.zoomLevel === "overview"', 1)[1].split('ListView {', 1)[0]
        self.assertIn('projectTile.roomRows.length + " chats"', miniature)
        self.assertIn('onTapped: tiles.focusSlot(projectTile.slot)', miniature)
        self.assertIn('DragHandler {', miniature)
        self.assertIn('enabled: tiles.zoomLevel === "overview"', miniature)
        self.assertIn('if (active) tiles.beginDrag(projectTile.slot)', miniature)
        self.assertIn('tiles.finishDrag()', miniature)
        self.assertIn('tiles.updateDrag(activeTranslation.x, activeTranslation.y)', miniature)

    def test_room_row_has_preview_and_confirmed_archive_without_opening_chat(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('chatPreview.openRoom(chatEntry.modelData.id, chatEntry.modelData.name)', tiles)
        self.assertIn('controller.archiveRoom(chatEntry.modelData)', tiles)
        self.assertIn('archiveArmed', tiles)
        self.assertIn('visible: !chatEntry.archived', tiles)

    def test_wheel_zoom_uses_exact_cursor_on_every_tile_surface_without_mode_pause(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('function wheelZoom(event, sourceItem)', tiles)
        self.assertIn('sourceItem.mapToItem(viewport, event.x, event.y)', tiles)
        self.assertIn('tiles.wheelZoom(event, tileHeader)', tiles)
        self.assertIn('tiles.wheelZoom(event, chatList)', tiles)
        self.assertIn('tiles.wheelZoom(event, viewport)', tiles)
        self.assertNotIn('(projectTile.slot.x + projectTile.slot.width / 2) * tiles.zoom', tiles)

    def test_categories_are_persisted_and_assignable_in_visual_map_districts(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        panel = (ROOT / 'Panel.qml').read_text()
        self.assertIn('controller.controlMapCategories', tiles)
        self.assertIn('TilesModel.mapLayout(orderedProjects, viewport.width, viewport.height, mapCategories, collapsedGroupIds)', tiles)
        self.assertIn('model: tiles.layout.groups', tiles)
        self.assertIn('tiles.assignCategory(projectTile.project.id)', tiles)
        self.assertIn('function addCategory(name)', tiles)
        self.assertIn('function renameCategory(id, name)', tiles)
        self.assertIn('function removeCategory(id)', tiles)
        self.assertIn('property var controlMapCategories:', panel)
        self.assertIn('function setControlMapCategories(next)', panel)
        self.assertIn('preferences.controlMapCategories', panel)

    def test_category_assignment_keeps_the_camera_and_dragging_into_a_district_reassigns(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        model = (ROOT / 'ControlTilesModel.js').read_text()
        self.assertNotIn('onMapCategoriesChanged: if (mapReady) Qt.callLater(fitMap)', tiles)
        self.assertIn('function dropGroup()', tiles)
        self.assertIn('assignProjectToGroup(draggingSlot.project.id, group.id)', tiles)
        self.assertIn('function toggleGroup(groupId)', tiles)
        self.assertIn('collapsedGroupIds', tiles)
        self.assertIn('collapsed: collapsed', model)
        self.assertIn('height:header + (collapsed ? 0 : rows * CELL_HEIGHT)', model)

    def test_chat_scroll_remains_available_unless_a_zoom_gesture_is_in_progress(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('function wheelGestureActive()', tiles)
        self.assertIn('tiles.wheelGestureActive() || (event.modifiers & Qt.ControlModifier)', tiles)
        self.assertIn('event.accepted = false', tiles)

    def test_category_colors_are_parsed_as_qml_color_properties(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertNotIn('Qt.color(', tiles)
        self.assertIn('readonly property color groupTint: modelData.color || "#808080"', tiles)

    def test_category_dialog_prevents_clicks_from_falling_through_scrim(self):
        tiles = (ROOT / 'ControlTiles.qml').read_text()
        self.assertIn('id: categoryDialog', tiles)
        self.assertIn('MouseArea { anchors.fill: parent; onClicked: mouse.accepted = true }', tiles)


if __name__ == '__main__':
    unittest.main()
