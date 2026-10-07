"""Project context-menu archive behavior from the production QML functions."""
import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "Panel.qml"
WORKSPACE = ROOT / "AresWorkspace.qml"


def qml_function(name, source=None):
    source = (source or PANEL.read_text())
    match = re.search(r"^  function " + re.escape(name) + r"\([^)]*\) \{", source, re.M)
    if not match:
        raise AssertionError(f"missing QML function {name}")
    depth = 0
    for end in range(match.start(), len(source)):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():end + 1]
    raise AssertionError("unterminated function")


ROOMS = """
const root = {
  errorText: '',
  selectedRoomId: '',
  extendedRoomId: '',
  rooms: [
    {id: 'old-one', projectId: 'p1', status: 'active', source: 'hermes', externalActivityAt: 500, externalUpdatedAt: 400},
    {id: 'old-two', projectId: 'p1', status: 'active', source: 'ares', updatedAt: 900, createdAt: 100},
    {id: 'fresh', projectId: 'p1', status: 'active', source: 'hermes', externalActivityAt: 5000, externalUpdatedAt: 4000},
    {id: 'paused', projectId: 'p1', status: 'paused', source: 'ares', updatedAt: 300, createdAt: 100},
    {id: 'control', projectId: 'p1', status: 'active', source: 'control', updatedAt: 200, createdAt: 100},
    {id: 'other-project', projectId: 'p2', status: 'active', source: 'ares', updatedAt: 300, createdAt: 100},
  ],
  bridge: {
    calls: [],
    lifecycle(room, action, done) { this.calls.push([room.id, action]); done({ok: true}) },
    reload() { this.calls.push(['reload']) },
  },
};
Object.assign(globalThis, root);
const project = {id: 'p1'};
"""


class ProjectArchiveOlderTests(unittest.TestCase):
    def functions(self, *names):
        return "\n".join(qml_function(name) for name in names)

    def run_node(self, script):
        return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)

    def test_project_rooms_older_than_filters_by_activity_cutoff(self):
        functions = self.functions("projectRoomsOlderThan", "roomActivity")
        script = functions + ROOMS + """
console.log(JSON.stringify(projectRoomsOlderThan(project, 1000).map(function(room) { return room.id })));
"""
        result = self.run_node(script)
        self.assertEqual(json.loads(result.stdout), ["old-one", "old-two"])

    def test_archive_older_than_archives_only_idle_rooms(self):
        functions = self.functions("archiveProjectRoomsOlderThan", "archiveRoomQueue",
                                   "projectRoomsOlderThan", "roomActivity")
        script = functions + ROOMS + """
archiveProjectRoomsOlderThan(project, 1000);
console.log(JSON.stringify(bridge.calls));
"""
        result = self.run_node(script)
        self.assertEqual(json.loads(result.stdout),
                         [["old-one", "archive"], ["old-two", "archive"], ["reload"]])

    def test_archive_older_than_with_no_matches_does_nothing(self):
        functions = self.functions("archiveProjectRoomsOlderThan", "archiveRoomQueue",
                                   "projectRoomsOlderThan", "roomActivity")
        script = functions + ROOMS + """
archiveProjectRoomsOlderThan(project, 50);
console.log(JSON.stringify(bridge.calls));
"""
        result = self.run_node(script)
        self.assertEqual(json.loads(result.stdout), [])


class ProjectContextMenuContractTests(unittest.TestCase):
    def setUp(self):
        self.workspace = WORKSPACE.read_text()

    def run_node(self, script):
        return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)

    def test_right_click_on_project_opens_the_menu(self):
        self.assertIn("acceptedButtons: Qt.LeftButton | Qt.RightButton", self.workspace)
        self.assertIn("if (mouse.button === Qt.RightButton) {", self.workspace)
        self.assertIn("workspace.openProjectMenu(treeRow.projectData, treeMouse, mouse.x, mouse.y)", self.workspace)

    def test_menu_offers_archive_all_and_archive_older_with_units(self):
        self.assertIn('label: "Archive all"', self.workspace)
        self.assertIn('label: "Archive older than…"', self.workspace)
        self.assertIn('({ label: "hours", seconds: 3600 })', self.workspace)
        self.assertIn('({ label: "days", seconds: 86400 })', self.workspace)
        self.assertIn('({ label: "weeks", seconds: 604800 })', self.workspace)
        self.assertIn("controller.archiveProjectRoomsOlderThan(project, cutoff)", self.workspace)

    def test_menu_close_clears_state_and_archive_keeps_sidebar_scroll(self):
        self.assertIn("function closeProjectMenu()", self.workspace)
        self.assertIn("function openProjectMenu(project, item, x, y)", self.workspace)
        self.assertIn("id: projectContextMenu", self.workspace)
        self.assertIn("workspace.retainSidebarScroll()\n          controller.archiveProjectRooms(project)",
                      self.workspace)

    def test_expand_and_collapse_retain_sidebar_scroll(self):
        self.assertIn("{ workspace.retainSidebarScroll(); controller.toggleNavigationSection(treeRow.modelData.label) }",
                      self.workspace)
        self.assertIn("{ workspace.retainSidebarScroll(); controller.toggleHost(treeRow.modelData.nodeId) }",
                      self.workspace)
        self.assertIn("{ workspace.retainSidebarScroll(); controller.toggleProject(treeRow.projectData.id) }",
                      self.workspace)

    def test_older_cutoff_clamps_invalid_amounts_to_one_unit(self):
        functions = qml_function("projectMenuOlderCutoff", source=self.workspace)
        script = functions + """
const workspaceState = {
  projectMenuOlderAmount: 'abc',
  projectMenuOlderUnitIndex: 1,
  projectMenuOlderUnits: [{label: 'hours', seconds: 3600}, {label: 'days', seconds: 86400}, {label: 'weeks', seconds: 604800}],
};
Object.assign(globalThis, workspaceState);
const before = projectMenuOlderCutoff();
projectMenuOlderAmount = '3';
projectMenuOlderUnitIndex = 0;
const threeHours = projectMenuOlderCutoff();
console.log(JSON.stringify({clampedHours: Math.round((Date.now() / 1000 - before) / 3600), threeHours: Math.round((Date.now() / 1000 - threeHours) / 3600)}));
"""
        result = self.run_node(script)
        self.assertEqual(json.loads(result.stdout), {"clampedHours": 24, "threeHours": 3})


if __name__ == "__main__":
    unittest.main()
