"""Real offscreen Qt input/geometry tests; no application services are loaded."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"

QML_TEST = '''import QtQuick
import QtQuick.Controls
import QtTest
Item {
 id: harness; width: 680; height: 560
 property bool showing: false
 property int leakedKeys: 0
 property bool closeOnActivation: false
 property string selectedId: ""
 function acceptRoom(room) {
  if (!closeOnActivation) return
  showing = false
  palette.rooms = []
  selectedId = room.id
 }
 Keys.onPressed: function(event) { leakedKeys++; event.accepted = true }
 TextField { id: draft; text: "untouched" }
 RoomSearchPalette {
  id: palette; opened: harness.showing
  rooms: []
  projects: [{id:"p", name:"Project", node:"host-one"}]
  onRoomActivated: function(room) { harness.acceptRoom(room) }
 }
 SignalSpy { id: activated; target: palette; signalName: "roomActivated" }
 SignalSpy { id: dismissed; target: palette; signalName: "dismissed" }
 TestCase {
  name: "SearchPalette"; when: windowShown
  function inventory() { return [
   {id:"a",projectId:"p",alias:"Uno",name:"Shared title",status:"idle"},
   {id:"b",projectId:"p",alias:"Dos",name:"Shared title",status:"working"},
   {id:"c",projectId:"p",alias:"Tres",name:"Other",status:"idle"}
  ] }
  function init() {
   harness.showing = false; harness.width = 680; harness.height = 560
   palette.rooms = inventory(); activated.clear(); dismissed.clear()
   harness.leakedKeys = 0; draft.text = "untouched"
   harness.closeOnActivation = false; harness.selectedId = ""
  }
  function open() {
   harness.showing = true
   var field = findChild(palette,"paletteInput")
   verify(field !== null); tryCompare(field,"activeFocus",true)
   return field
  }
  function test_keyboard_selection_and_modal_isolation() {
   var field = open()
   keyClick(Qt.Key_Up); compare(palette.currentIndex,0)
   keyClick(Qt.Key_Down); compare(palette.currentIndex,1)
   palette.rooms = [palette.rooms[1],palette.rooms[0],palette.rooms[2]]
   compare(palette.currentResult.room.id,"b"); compare(palette.currentIndex,0)
   keyClick(Qt.Key_Down); keyClick(Qt.Key_Down); keyClick(Qt.Key_Down)
   compare(palette.currentIndex,2)
   field.text = "shared"; compare(palette.currentIndex,0)
   keyClick(Qt.Key_Down); keyClick(Qt.Key_Tab); keyClick(Qt.Key_Backtab)
   verify(field.activeFocus); compare(harness.leakedKeys,0)
   compare(activated.count,0); keyClick(Qt.Key_Enter)
   compare(activated.signalArguments[0][0].id,"a")
   field.text = "missing"; compare(palette.currentIndex,-1)
   keyClick(Qt.Key_Down); keyClick(Qt.Key_Return); compare(activated.count,1)
   keyClick(Qt.Key_Escape); compare(dismissed.count,1); verify(palette.opened)
   harness.showing = false; verify(!palette.opened)
   open(); compare(field.text,""); compare(palette.results.length,3)
  }
  function test_list_preview_geometry_and_mouse() {
   var field = open()
   var card = findChild(palette,"paletteCard")
   var preview = findChild(palette,"palettePreview")
   var list = findChild(palette,"paletteResults")
   verify(card !== null); verify(preview !== null); verify(list !== null)
   for (var w of [360,680]) {
    harness.width = w; wait(10)
    var f = field.mapToItem(palette,0,field.height/2)
    var p = preview.mapToItem(palette,0,preview.height/2)
    verify(Math.abs(f.y-p.y)<2); verify(p.x >= f.x+field.width)
    verify(card.x>=0); verify(card.x+card.width<=harness.width)
    verify(preview.width>30); verify(field.width>90)
   }
   verify(preview.text.indexOf("Uno")>=0)
   verify(preview.text.indexOf("Project")>=0)
   verify(preview.text.indexOf("host-one")>=0)
   tryVerify(function() { return list.itemAtIndex(1) !== null })
   mouseClick(list.itemAtIndex(1))
   compare(activated.count,1); compare(activated.signalArguments[0][0].id,"b")
   mouseClick(card,2,2); compare(dismissed.count,0)
   mouseClick(palette,1,1); compare(dismissed.count,1); verify(palette.opened)
   mouseClick(findChild(palette,"paletteClose")); compare(dismissed.count,2)
   field.forceActiveFocus(); field.text = "nonexistent"
   verify(findChild(palette,"paletteEmpty").visible)
   compare(activated.count,1)
  }
  function test_bounded_list_scroll_and_refresh() {
   var many = []
   for (var i=0; i<20; i++) many.push({id:"room"+i,projectId:"p",name:"Room "+i})
   palette.rooms = many; open()
   var list = findChild(palette,"paletteResults")
   verify(list !== null)
   verify(list.height<=7*52); verify(list.contentHeight>list.height)
   for (var i=0; i<19; i++) keyClick(Qt.Key_Down)
   compare(palette.currentIndex,19)
   tryVerify(function() { return list.contentY>0 })
   compare(activated.count,0)
   harness.height = 180; wait(10)
   var card = findChild(palette,"paletteCard")
   verify(card.height<=harness.height); verify(card.y>=0)
   palette.rooms = []; compare(palette.currentIndex,-1)
   keyClick(Qt.Key_Enter); compare(activated.count,0)
  }
  function test_owner_closes_and_replaces_model_during_click() {
   open(); harness.closeOnActivation = true
   var list = findChild(palette,"paletteResults")
   tryVerify(function() { return list.itemAtIndex(1) !== null })
   mouseClick(list.itemAtIndex(1))
   compare(harness.selectedId,"b"); verify(!palette.opened)
   compare(activated.count,1)
   palette.rooms = inventory(); open(); verify(palette.opened)
  }
  function test_deferred_focus_does_not_steal_after_close() {
   harness.showing = true; harness.showing = false
   draft.forceActiveFocus(); wait(10)
   verify(draft.activeFocus)
  }
  function test_filter_first_enter() {
   var field = open()
   keyClick(Qt.Key_S); keyClick(Qt.Key_H)
   compare(palette.results.length,2); compare(palette.currentIndex,0)
   compare(activated.count,0); compare(draft.text,"untouched")
   keyClick(Qt.Key_Return)
   compare(activated.count,1); compare(activated.signalArguments[0][0].id,"a")
   verify(palette.opened)
  }
 }
}
'''

@unittest.skipUnless(Path(RUNNER).is_file(), "Qt Quick Test runner unavailable")
class SearchPaletteQmlTests(unittest.TestCase):
    def test_offscreen_palette(self):
        self.assertTrue((ROOT / "RoomSearchPalette.qml").is_file(), "Search palette component is missing")
        with tempfile.TemporaryDirectory(prefix="ares-palette-", dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory)
            for name in ("RoomSearchPalette.qml", "RoomSearchModel.js"):
                shutil.copy2(ROOT / name, path / name)
            common = path / "imports" / "qs" / "Commons"
            common.mkdir(parents=True)
            (common / "qmldir").write_text("module qs.Commons\nsingleton Color 1.0 Color.qml\nsingleton Style 1.0 Style.qml\n")
            (common / "Color.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 component PopupColors: QtObject { property color text: "white"; property color background: "black"; property color border: "gray" }
 property PopupColors popups: PopupColors {}
 property color accent: "steelblue"
}
''')
            (common / "Style.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 function space(n) { return n }
 property int cornerRadius: 8
 component Fonts: QtObject { property string family: "sans-serif"; property int bodySmall: 12; property int caption: 10 }
 property Fonts font: Fonts {}
}
''')
            lint = shutil.which("qmllint") or "/usr/lib/qt6/bin/qmllint"
            if Path(lint).is_file():
                checked = subprocess.run(
                    [lint, "-I", str(path / "imports"), str(path / "RoomSearchPalette.qml")],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                self.assertNotIn("Warning:", checked.stdout + checked.stderr)
            (path / "tst_palette.qml").write_text(QML_TEST)
            result = subprocess.run(
                [RUNNER, "-input", str(path), "-import", str(path / "imports")],
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("QWARN", result.stdout + result.stderr)
            print(result.stdout.strip())

if __name__ == "__main__":
    unittest.main()
