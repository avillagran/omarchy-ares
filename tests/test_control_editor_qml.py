"""Real Qt pointer/lifecycle checks for the extracted production Control Room editor."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path('/usr/lib/qt6/bin/qmltestrunner')


def function(source, name):
    start = source.index('  function ' + name + '(')
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


@unittest.skipUnless(RUNNER.is_file(), 'Qt Quick Test unavailable')
class ControlEditorQmlTests(unittest.TestCase):
    def test_close_button_and_navigation_preserve_draft(self):
        source = (ROOT / 'ControlRoom.qml').read_text()
        select = function(source, 'selectActionRoom')
        request = function(source, 'requestRoomAction')
        lifecycle = source[source.index('  onVisibleChanged: {'):source.index('  Timer { interval: 5000')]
        heading_start = source.index('          RowLayout {\n            Layout.fillWidth: true\n            spacing: Style.space(4)')
        heading = source[heading_start:source.index('          ScrollView {', heading_start)]
        button = source[source.index('  component ControlButton: Rectangle {'):source.rfind('\n}')]
        with tempfile.TemporaryDirectory(prefix='ares-editor-', dir=os.environ.get('TMPDIR')) as tmp:
            path = Path(tmp)
            (path / 'qmldir').write_text('singleton Color 1.0 Color.qml\nsingleton Style 1.0 Style.qml\n')
            (path / 'Color.qml').write_text('pragma Singleton\nimport QtQuick\nQtObject { property color accent: "orange" }\n')
            (path / 'Style.qml').write_text('pragma Singleton\nimport QtQuick\nQtObject { function space(n) { return n }\n property QtObject font: QtObject { property int caption: 12 } }\n')
            (path / 'tst_editor.qml').write_text('''import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtTest
import "."
Item {
 id: control; width: 320; height: 200
 property var controller: controllerMock
 property bool editorOpen: false
 property string selectedRoomId: ""
 property string selectedItemId: ""
 property string note: ""
 property var roomNotes: ({})
 property color foreground: "white"
 property color surface: "#262626"
 property color stroke: "#606060"
 property string fontFamily: "sans-serif"
 readonly property var selectedRoom: controller.rooms.find(function(room) { return room.id === selectedRoomId }) || null
 readonly property var selectedItem: null

 QtObject { id: controllerMock
  property string controlMode: "map"
  property var rooms: [{id:"a",name:"Chat A",status:"active",controlIncluded:true},
                       {id:"b",name:"Chat B",status:"active",controlIncluded:true}]
  property var controlItems: []
  property int refreshCount: 0
  function refreshControlItems() { refreshCount++ }
 }
 TextArea { id: decisionNote; text: control.note; onTextChanged: control.note = text }
 ''' + select + '\n' + request + '\n' + lifecycle + '''
 Rectangle {
  id: editor; objectName: "editorPanel"; y: 40; width: 300; height: 80
  visible: control.selectedRoom !== null && control.editorOpen
  ColumnLayout { anchors.fill: parent
 ''' + heading + '''
  }
 }
 ''' + button + '''
 TestCase {
  name: "ControlEditorLifecycle"; when: windowShown
  function test_open_close_switch_and_reenter() {
   verify(!control.editorOpen)
   verify(control.selectActionRoom("a"))
   verify(!editor.visible)
   control.requestRoomAction("a", "draft")
   tryCompare(editor,"visible",true)
   control.note = "unsent draft"
   var closeButton = findChild(control, "controlEditorClose")
   verify(closeButton !== null)
   mouseClick(closeButton)
   tryCompare(editor,"visible",false)
   compare(control.note,"unsent draft")
   control.requestRoomAction("a", "draft")
   tryCompare(editor,"visible",true)
   control.selectActionRoom("b")
   tryCompare(editor,"visible",false)
   control.requestRoomAction("b", "draft")
   tryCompare(editor,"visible",true)
   controller.controlMode = "visual"
   tryCompare(editor,"visible",false)
   control.requestRoomAction("b", "draft")
   control.visible = false
   compare(control.editorOpen,false)
   control.visible = true
   compare(editor.visible,false)
  }
 }
}
''')
            result = subprocess.run([str(RUNNER), '-input', str(path)], capture_output=True, text=True,
                env={**os.environ, 'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software'}, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
