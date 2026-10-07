"""Run the production sidebar row builder in Qt as rooms arrive and disappear."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_extended_navigation import qml_function

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path('/usr/lib/qt6/bin/qmltestrunner')


@unittest.skipUnless(RUNNER.exists(), 'Qt Quick Test unavailable')
class SidebarEmptyProjectsQmlTests(unittest.TestCase):
    def test_only_projects_with_chats_and_their_hosts_are_listed(self):
        source = (ROOT / 'Panel.qml').read_text()
        functions = '\n'.join(qml_function(source, name) for name in (
            'roomActivity', 'roomsForProject', 'sortedRooms', 'projectActivity',
            'sortedProjects', 'projectsForNode', 'sortedNodes', 'isHostExpanded',
            'isProjectExpanded', 'buildNavigationRows'))
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'), prefix='ares-sidebar-') as directory:
            path = Path(directory)
            (path / 'tst_sidebar.qml').write_text('''import QtQuick
import QtTest
Item {
 id: root
 width: 240; height: 160
 property var projects: [
  {id:"empty-local",node:"local",createdAt:40},
  {id:"used-local",node:"local",createdAt:10},
  {id:"empty-peer",node:"peer",createdAt:50},
  {id:"used-peer",node:"peer",createdAt:20},
  {id:"control",node:"control",createdAt:30}]
 property var rooms: [
  {id:"local-chat",projectId:"used-local",source:"hermes",externalActivityAt:100},
  {id:"peer-chat",projectId:"used-peer",source:"hermes",externalActivityAt:200},
  {id:"control-room",projectId:"control",source:"control"}]
 property bool recentExpanded: false
 property int recentRoomLimit: 6
 property bool projectsCollapsed: false
 property var collapsedHosts: ({})
 property var expandedProjects: ({})
 property int navigationRevision: 0
 readonly property var navigationRows: {
   var revision = navigationRevision
   return buildNavigationRows()
 }
''' + functions + '''
 TestCase {
  name: "SidebarEmptyProjects"
  when: windowShown
  function kinds(kind) {
   return root.navigationRows.filter(function(row) { return row.kind === kind })
  }
  function test_live_inventory() {
   compare(kinds("project").map(function(row) { return row.project.id }).join(","), "used-peer,used-local")
   compare(kinds("host").map(function(row) { return row.nodeId }).join(","), "peer,local")
   compare(root.navigationRows.filter(function(row) { return row.label === "PROJECTS" })[0].count, 2)
   root.rooms = root.rooms.filter(function(room) { return room.id !== "peer-chat" })
   compare(kinds("project").map(function(row) { return row.project.id }).join(","), "used-local")
   compare(kinds("host").map(function(row) { return row.nodeId }).join(","), "local")
   compare(root.navigationRows.filter(function(row) { return row.label === "PROJECTS" })[0].count, 1)
   root.rooms = [{id:"only-control",projectId:"empty-peer",source:"control"}]
   compare(kinds("project").length, 0)
   compare(kinds("host").length, 0)
   compare(root.navigationRows.filter(function(row) { return row.label === "PROJECTS" })[0].count, 0)
   root.rooms = [{id:"new-chat",projectId:"empty-peer",source:"hermes",externalActivityAt:300}]
   compare(kinds("project").map(function(row) { return row.project.id }).join(","), "empty-peer")
   compare(kinds("host").map(function(row) { return row.nodeId }).join(","), "peer")
  }
 }
}
''')
            result = subprocess.run([str(RUNNER), '-input', str(path)], capture_output=True,
                                    text=True, timeout=30, env={**os.environ,
                                    'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
