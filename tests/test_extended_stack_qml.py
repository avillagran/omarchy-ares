"""Exercise production stack bindings and map-open context with real Qt clicks."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_extended_navigation import qml_function

ROOT = Path(__file__).resolve().parents[1]
RUNNER = '/usr/lib/qt6/bin/qmltestrunner'

@unittest.skipUnless(Path(RUNNER).exists(), 'Qt Quick Test unavailable')
class ExtendedStackQmlTests(unittest.TestCase):
    def test_map_context_and_stable_stack_bindings(self):
        source = (ROOT / 'ExtendedWorkspace.qml').read_text()
        bindings = source.split('  property var stackRoomIds: []', 1)[1].split('  // Order belongs', 1)[0]
        action = qml_function((ROOT / 'ControlRoom.qml').read_text(), 'openActionChat')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'), prefix='ares-stack-') as directory:
            path = Path(directory)
            shutil.copy2(ROOT / 'ExtendedStackModel.js', path)
            (path / 'tst_stack.qml').write_text('''import QtQuick
import QtTest
import "ExtendedStackModel.js" as ExtendedStackModel
Item {
 id: control; width: 300; height: 180
 QtObject {
  id: controller
  property string controlMode: "map"
  property string section: "control"
  property var extendedMapSlots: []
  property var activeRooms: [
   {id:"a",projectId:"p",status:"active"},
   {id:"b",projectId:"p",status:"active"},
   {id:"f",projectId:"far",status:"active"},
   {id:"n",projectId:"near",status:"active"}]
  function selectRoom(room) { extended.currentRoom = room }
  function openExtended(slots) { extendedMapSlots = slots }
 }
 QtObject { id: projectMap; property var layout: ({slots:[
  {project:{id:"p"},x:0,y:0,width:100,height:100},
  {project:{id:"far"},x:900,y:0,width:100,height:100},
  {project:{id:"near"},x:100,y:0,width:100,height:100}]}) }
 ''' + action + '''
 Item {
  id: extended
  property var currentRoom: null
  property var overviewRoomIds: ["f","n","a","b"]
  property var stackRoomIds: []
 ''' + bindings + '''
 }
 Rectangle { id: card; width: 80; height: 40
  MouseArea { anchors.fill: parent; onClicked: control.openActionChat(controller.activeRooms[0]) }
 }
 SignalSpy { id: changes; target: extended; signalName: "stackRoomIdsChanged" }
 TestCase {
  name: "MapStackContext"; when: windowShown
  function test_open_poll_and_selection() {
   mouseClick(card)
   compare(controller.section,"home")
   compare(controller.extendedMapSlots.length,3)
   compare(extended.stackRoomIds.join(","),"b")
   changes.clear()
   controller.activeRooms = JSON.parse(JSON.stringify(controller.activeRooms))
   compare(changes.count,0)
   controller.activeRooms = controller.activeRooms.filter(function(r) { return r.id !== "b" })
   compare(extended.stackRoomIds.join(","),"n,f")
   controller.selectRoom(controller.activeRooms.find(function(r) { return r.id === "n" }))
   compare(extended.stackRoomIds.join(","),"a,f")
  }
 }
}
''')
            result = subprocess.run([RUNNER, '-input', str(path)], capture_output=True, text=True,
                env={**os.environ, 'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software'}, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__':
    unittest.main()
