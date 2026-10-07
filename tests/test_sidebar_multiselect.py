"""Sidebar multi-selection behavior is exercised from the production QML functions."""
import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "Panel.qml"


def qml_function(name):
    source = PANEL.read_text()
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


class SidebarMultiSelectTests(unittest.TestCase):
    def test_shift_range_selects_visible_rooms_and_keeps_the_target_open(self):
        functions = "\n".join(qml_function(name) for name in (
            "visibleSidebarRoomIds", "sidebarRangeIds", "selectSidebarRoom", "extendSidebarSelection",
        ))
        script = functions + """
const root = {
  navigationRows: [
    {kind: 'section'},
    {kind: 'room', room: {id: 'one'}},
    {kind: 'room', room: {id: 'two'}},
    {kind: 'project'},
    {kind: 'room', room: {id: 'three'}},
    {kind: 'room', room: {id: 'two'}},
  ],
  rooms: [{id: 'one'}, {id: 'two'}, {id: 'three'}],
  selectedRoomId: '',
  sidebarSelectedRoomIds: [],
  sidebarSelectionAnchorId: '',
  sidebarSelectionCursorId: '',
  selectRoom(room) { this.selectedRoomId = room.id },
  selectRecentRoom(room) { this.selectedRoomId = room.id },
};
Object.assign(globalThis, root);
selectSidebarRoom(rooms[0], false, true);
extendSidebarSelection(1);
extendSidebarSelection(1);
console.log(JSON.stringify({selected: sidebarSelectedRoomIds, active: selectedRoomId}));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"selected": ["one", "two", "three"], "active": "three"})

    def test_multi_archive_only_archives_selected_active_rooms_then_clears_selection(self):
        function = qml_function("archiveSidebarSelection")
        script = function + """
const calls = [];
const root = {
  sidebarSelectedRoomIds: ['one', 'two'],
  sidebarSelectionAnchorId: 'one',
  sidebarSelectionCursorId: 'two',
  selectedRoomId: 'one',
  extendedRoomId: '',
  errorText: 'old',
  rooms: [
    {id: 'one', status: 'active', source: 'hermes'},
    {id: 'two', status: 'active', source: 'hermes'},
    {id: 'three', status: 'active', source: 'hermes'},
  ],
  bridge: {
    lifecycle(room, action, done) { calls.push([room.id, action]); done({ok: true}) },
    reload() { calls.push(['reload']) },
  },
};
Object.assign(globalThis, root);
archiveSidebarSelection();
console.log(JSON.stringify({calls, selected: sidebarSelectedRoomIds, active: root.selectedRoomId}));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "calls": [["one", "archive"], ["two", "archive"], ["reload"]],
            "selected": [],
            "active": "",
        })

    def test_sidebar_grip_cycles_base_then_fifteen_and_thirty_percent_wider(self):
        functions = "\n".join(qml_function(name) for name in ("setSidebarWidth", "cycleSidebarWidth"))
        script = functions + """
const root = {
  sidebarWidth: 200,
  sidebarWidthCycleBase: 200,
  sidebarPersist: { restart() {} },
};
Object.assign(globalThis, root);
cycleSidebarWidth(); const first = sidebarWidth;
cycleSidebarWidth(); const second = sidebarWidth;
cycleSidebarWidth(); const third = sidebarWidth;
console.log(JSON.stringify([first, second, third]));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [230, 260, 200])


if __name__ == "__main__":
    unittest.main()
