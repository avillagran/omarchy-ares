"""Sidebar project expansion keeps sibling projects open and never moves scroll."""
import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "Panel.qml"
WORKSPACE = ROOT / "AresWorkspace.qml"


def qml_function(source, name):
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


class ProjectExpansionTests(unittest.TestCase):
    def test_expanding_one_project_keeps_the_others_open(self):
        panel = PANEL.read_text()
        functions = "\n".join(qml_function(panel, name) for name in (
            "isProjectExpanded", "setProjectExpanded", "toggleProject",
        ))
        script = functions + """
const root = {
  expandedProjects: ({}),
  navigationRevision: 0,
  selectedProjectId: '',
  selectedRoom: null,
  selectedRoomId: '',
  roomsForProject: function() { return [] },
};
Object.assign(globalThis, root);
toggleProject('alpha')
const afterFirst = Object.keys(expandedProjects).sort()
toggleProject('beta')
const afterSecond = Object.keys(expandedProjects).sort()
toggleProject('beta')
const afterCollapse = Object.keys(expandedProjects).sort()
console.log(JSON.stringify({afterFirst, afterSecond, afterCollapse}))
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["afterFirst"], ["alpha"])
        self.assertEqual(payload["afterSecond"], ["alpha", "beta"])
        self.assertEqual(payload["afterCollapse"], ["alpha"])


class ScrollRetentionContractTests(unittest.TestCase):
    def setUp(self):
        self.workspace = WORKSPACE.read_text()

    def test_expand_and_collapse_retain_scroll(self):
        self.assertIn("function retainSidebarScroll()", self.workspace)
        self.assertIn("workspace.retainSidebarScroll(); controller.toggleProject(treeRow.projectData.id)", self.workspace)
        self.assertIn("workspace.retainSidebarScroll(); controller.toggleHost(treeRow.modelData.nodeId)", self.workspace)
        self.assertIn("workspace.retainSidebarScroll(); controller.toggleNavigationSection(treeRow.modelData.label)", self.workspace)

    def test_restore_retries_until_content_height_settles(self):
        self.assertIn("sidebarScrollRestoreAttempts += 1", self.workspace)
        self.assertIn("workspace.sidebarRestoreContentY <= maxY + 1 || workspace.sidebarScrollRestoreAttempts >= 6",
                      self.workspace)


if __name__ == "__main__":
    unittest.main()
