"""Real Qt pointer delivery through the production map, without user state."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"


@unittest.skipUnless(Path(RUNNER).is_file(), "Qt Quick Test runner unavailable")
class ControlTilesDragQmlTests(unittest.TestCase):
    def test_miniature_pointer_gestures(self):
        with tempfile.TemporaryDirectory(prefix="ares-map-drag-", dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory)
            source = (ROOT / "ControlTiles.qml").read_text()
            source = source.replace("id: projectTile", 'id: projectTile; objectName: "tile-" + project.id')
            source = source.replace("id: viewport", 'id: viewport; objectName: "viewport"')
            (path / "ControlTiles.qml").write_text(source)
            for name in ("ControlTilesModel.js", "ControlMapModel.js"):
                shutil.copy2(ROOT / name, path / name)
            (path / "MapChatPreview.qml").write_text('import QtQuick\nItem { property var bridge; function openRoom(id, name) {} }\n')
            common = path / "imports" / "qs" / "Commons"
            common.mkdir(parents=True)
            (common / "qmldir").write_text("module qs.Commons\nsingleton Color 1.0 Color.qml\nsingleton Style 1.0 Style.qml\n")
            (common / "Color.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 property QtObject popups: QtObject { property color text: "white"; property color background: "black" }
 property color accent: "steelblue"
 property color muted: "gray"
}
''')
            (common / "Style.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 function space(n) { return n }
 property int cornerRadius: 6
 property QtObject font: QtObject { property string family: "sans-serif"; property int body: 12; property int caption: 10 }
}
''')
            (path / "tst_drag.qml").write_text('''import QtQuick
import QtTest
Item {
 id: scene
 width: 800; height: 600
 QtObject {
  id: controller
  property var projects: [{id:"a",name:"Alpha"},{id:"b",name:"Beta"},{id:"c",name:"Gamma"},{id:"d",name:"Delta"}]
  property var rooms: []
  property var archivedRooms: []
  property var controlTileOrder: []
  property var controlMapCategories: ({groups:[],assignments:({})})
  property bool categoriesSaving: false
  function setControlMapCategories(next) { controlMapCategories = next }
 }
 ControlTiles {
  id: map; anchors.fill: parent; controller: controller; bridge: null
  onTileOrderRequested: function(ids) { controller.controlTileOrder = ids }
 }
 TestCase {
  name: "MapMiniDrag"; when: windowShown
  function init() {
   map.draggingSlot = null; map.dragDx = 0; map.dragDy = 0
   controller.controlTileOrder = []
   controller.controlMapCategories = ({groups:[],assignments:({})})
   map.zoom = 0.25
   wait(30)
   compare(map.zoomLevel, "overview")
  }
  function test_drag_data() {
   return [{tag:"count",fraction:0.75,zoom:0.25}, {tag:"header",fraction:0.2,zoom:0.25},
           {tag:"count_far",fraction:0.75,zoom:0.14}, {tag:"header_far",fraction:0.2,zoom:0.14}]
  }
  function test_drag(data) {
   map.zoom = data.zoom; wait(20)
   var tile = findChild(map, "tile-a")
   verify(tile !== null)
   var start = tile.mapToItem(scene, tile.width / 2, tile.height * data.fraction)
   mousePress(scene, start.x, start.y, Qt.LeftButton)
   mouseMove(scene, start.x + 20, start.y, 30)
   mouseMove(scene, start.x + 40, start.y, 30)
   verify(map.draggingSlot !== null, "Drag must activate from " + data.tag)
   fuzzyCompare(map.dragDx, 40 / map.zoom, 0.01, "Tile must follow the pointer at miniature zoom")
   mouseRelease(scene, start.x + 40, start.y, Qt.LeftButton)
   compare(map.zoom, data.zoom, "Drag release must not zoom")
   compare(map.draggingSlot, null)
  }
  function dragBetween(source, destination) {
   var start = source.mapToItem(scene, source.width / 2, source.height * 0.75)
   var end = destination.mapToItem(scene, destination.width / 2, destination.height * 0.75)
   mousePress(scene, start.x, start.y, Qt.LeftButton)
   for (var step = 1; step <= 8; step++)
    mouseMove(scene, start.x + (end.x-start.x)*step/8, start.y + (end.y-start.y)*step/8, 20)
   verify(map.draggingSlot !== null)
   mouseRelease(scene, end.x, end.y, Qt.LeftButton)
   wait(30)
   compare(map.zoom, 0.25)
  }
  function test_drop_reorders() {
   dragBetween(findChild(map,"tile-a"), findChild(map,"tile-b"))
   compare(controller.controlTileOrder[0], "b")
   compare(controller.controlTileOrder[1], "a")
  }
  function test_drop_changes_group() {
   controller.controlMapCategories = ({groups:[{id:"one",name:"One"},{id:"two",name:"Two"}],
    assignments:({a:"one",b:"two",c:"two",d:"two"})})
   wait(30)
   dragBetween(findChild(map,"tile-a"), findChild(map,"tile-b"))
   compare(controller.controlMapCategories.assignments.a, "two")
  }
  function test_click_still_zooms() {
   var tile = findChild(map,"tile-a")
   mouseClick(tile, tile.width / 2, tile.height * 0.75)
   verify(map.zoom > 0.25)
  }
  function cleanup() { mouseRelease(scene, 0, 0, Qt.LeftButton); wait(20) }
 }
}
''')
            result = subprocess.run([RUNNER, "-input", str(path), "-import", str(path / "imports")],
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
                                    text=True, capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
