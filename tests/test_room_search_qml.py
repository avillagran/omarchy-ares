"""Exercise the real sidebar search control with keyboard events, offscreen."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"


@unittest.skipUnless(Path(RUNNER).is_file(), "Qt Quick Test runner unavailable")
class RoomSearchQmlTests(unittest.TestCase):
    def test_keyboard_search(self):
        with tempfile.TemporaryDirectory(prefix="ares-search-", dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory)
            for name in ("RoomSearch.qml", "RoomSearchModel.js"):
                shutil.copy2(ROOT / name, path / name)
            common = path / "imports" / "qs" / "Commons"
            common.mkdir(parents=True)
            (common / "qmldir").write_text("module qs.Commons\nsingleton Color 1.0 Color.qml\nsingleton Style 1.0 Style.qml\n")
            (common / "Color.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 property QtObject popups: QtObject { property color text: "white" }
 property color accent: "steelblue"
}
''')
            (common / "Style.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 function space(n) { return n }
 property QtObject font: QtObject { property string family: "sans-serif"; property int bodySmall: 12; property int caption: 10 }
}
''')
            (path / "tst_search.qml").write_text('''import QtQuick
import QtTest
Item {
 width: 320; height: 160
 RoomSearch {
  id: search; width: 300
  rooms: [
   {id:"a", projectId:"p", alias:"Uno", name:"Shared title"},
   {id:"b", projectId:"p", alias:"Dos", name:"Shared title"},
   {id:"c", projectId:"p", alias:"Tres", name:"Other"}
  ]
  projects: [{id:"p", node:"node"}]
 }
 SignalSpy { id: activated; target: search; signalName: "roomActivated" }
 TestCase {
  name: "RoomSearchKeyboard"; when: windowShown
  function init() {
   search.close(); activated.clear()
   search.rooms = search.rooms.slice().sort(function(a,b) { return a.id.localeCompare(b.id) })
  }
  function open() {
   mouseClick(findChild(search, "searchToggle"))
   verify(search.expanded)
   var field = findChild(search,"searchInput")
   tryCompare(field,"activeFocus",true)
   return field
  }
  function test_filter_and_first_enter() {
   var field = open()
   keyClick(Qt.Key_S); keyClick(Qt.Key_H)
   compare(search.results.length,2)
   compare(search.currentIndex,0)
   compare(activated.count,0)
   keyClick(Qt.Key_Return)
   compare(activated.count,1)
   compare(activated.signalArguments[0][0].id,"a")
   verify(!search.expanded)
  }
  function test_arrows_and_keypad_enter() {
   var field = open(); field.text = "shared"
   keyClick(Qt.Key_Down)
   compare(search.currentIndex,1)
   compare(activated.count,0)
   keyClick(Qt.Key_Up); compare(search.currentIndex,0)
   keyClick(Qt.Key_Down); keyClick(Qt.Key_Enter)
   compare(activated.signalArguments[0][0].id,"b")
  }
  function test_no_match_and_escape_restore() {
   var field = open(); field.text = "unknown"
   compare(search.results.length,0)
   keyClick(Qt.Key_Down); keyClick(Qt.Key_Return)
   compare(activated.count,0); verify(search.expanded)
   keyClick(Qt.Key_Escape)
   verify(!search.expanded); compare(field.text,"")
   field = open(); compare(search.results.length,3)
  }
  function test_edits_reset_selection_and_refresh_preserves_identity() {
   var field = open(); keyClick(Qt.Key_Down)
   compare(search.currentIndex,1)
   search.rooms = [search.rooms[1],search.rooms[0],search.rooms[2]]
   compare(search.currentIndex,0)
   compare(search.results[search.currentIndex].room.id,"b")
   field.text = "uno"
   compare(search.currentIndex,0)
   compare(search.results[0].room.id,"a")
   field.text = ""; keyClick(Qt.Key_Down)
   mouseClick(findChild(search,"searchToggle"))
   verify(!search.expanded); compare(activated.count,0)
  }
 }
}
''')
            result = subprocess.run(
                [RUNNER, "-input", str(path), "-import", str(path / "imports")],
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
