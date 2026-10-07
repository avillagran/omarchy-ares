"""Run production sidebar handlers and frame visibility bindings in a real Qt scene."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"


def block(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@unittest.skipUnless(Path(RUNNER).is_file(), "Qt Quick Test runner unavailable")
class ChatSurfaceTests(unittest.TestCase):
    def test_terminal_tiles_never_fall_back_to_historical_text(self):
        source = (ROOT / "ExtendedWorkspace.qml").read_text()
        tile_source = source[source.index("component RoomTile:"):]

        def match(pattern, group=0):
            found = re.search(pattern, tile_source, re.S)
            assert found is not None, pattern
            return found.group(group)

        properties = "\n".join(
            match(r"readonly property bool " + name + r":.*?(?=\n    readonly property bool)")
            for name in ("liveTerminalCandidate", "remotePeerRoom")
        ) + "\n" + match(r"readonly property bool previewFallback:[^\n]+")
        events = match(
            r"readonly property var roomEvents:.*?(?=\n    readonly property bool activeWork)",
        )
        container_visible = match(r"visible: (tile\.detailed \|\|[^\n]+)", 1)
        fallback_start = tile_source.rfind("ListView {", 0, tile_source.index("visible: tile.previewFallback\n"))
        fallback = block(tile_source[fallback_start:], "ListView {").replace(
            "ListView {", 'ListView { objectName: "historyFallback"; width: 400; height: 180;', 1,
        )
        cold = ""
        if "model: tile.cachedPreview" in tile_source:
            cold_start = tile_source.rfind("ListView {", 0, tile_source.index("model: tile.cachedPreview"))
            cold = block(tile_source[cold_start:], "ListView {").replace(
                "ListView {", 'ListView { objectName: "coldSynopsis";', 1,
            )
        qml = '''import QtQuick
import QtQuick.Layouts
import QtTest
Item {
 id: extended; width: 500; height: 500
 property color foreground: "white"
 property color dim: "gray"
 property var controller: ctl
 function tilePreviewEvents(rows) { return rows }
 QtObject {
  id: styleStub
  property QtObject font: QtObject { property string family: "monospace"; property int caption: 10 }
  function space(value) { return value }
 }
 QtObject {
  id: ctl
  property bool extendedMode: true
  property string extendedViewMode: "grown"
  function eventsForRoom(id) { return [{summaryText:"STALE HISTORY",kind:"message.user"}] }
  function eventTitle(row) { return "History" }
  function eventBody(row) { return row.summaryText }
 }
 QtObject {
  id: tileTerminalChat
  property string error: ""
  property bool surfacePainted: false
  property bool legacyTranscriptNeeded: false
 }
 Item {
  id: tile; anchors.fill: parent
  property var roomData: ({id:"ohm",source:"hermes"})
  property bool detailed: false
  property bool inViewport: true
  property var cachedFrame: null
  property var cachedPreview: []
  signal growRequested(var room)
  TILE_PROPERTIES
  EVENT_PROPERTY
  Item {
   id: terminalBody; width: 400; height: 200
   visible: CONTAINER_VISIBLE
   COLD_LIST
  }
  FALLBACK_LIST
 }
 TestCase {
  name: "TerminalTileHistory"; when: windowShown
  function init() {
   tile.roomData = {id:"ohm",source:"hermes"}
   tile.detailed = false
   tile.cachedFrame = null
   tile.cachedPreview = []
   tileTerminalChat.error = ""
   tileTerminalChat.surfacePainted = false
   tileTerminalChat.legacyTranscriptNeeded = false
  }
  function test_terminal_states_data() {
   var rows = []
   for (var detailed of [false,true])
    for (var frame of [false,true])
     for (var phase of ["resolving","failed","output","unbound"])
      rows.push({tag:String(detailed)+"-"+String(frame)+"-"+phase,
                 detailed:detailed,frame:frame,phase:phase})
   return rows
  }
  function test_terminal_states(data) {
   tile.detailed = data.detailed
   tile.cachedFrame = data.frame ? {image:{url:""}} : null
   tileTerminalChat.error = data.phase === "failed" ? "Chat identity is not resolved on this host" : ""
   tileTerminalChat.surfacePainted = data.phase === "output"
   tileTerminalChat.legacyTranscriptNeeded = data.phase === "unbound" && data.detailed
   // A delayed HTTP/cache reply must not resurrect history after an error.
   tile.cachedPreview = [{summaryText:"STALE CACHED SYNOPSIS"}]
   wait(20)
   compare(terminalBody.visible,true,"Keep terminal/status surface visible")
   var fallback = findChild(tile,"historyFallback")
   compare(fallback.visible,false,"Terminal rooms must not display old chat")
   compare(fallback.count,0,"Hidden historical models must be empty")
   if (!tileTerminalChat.legacyTranscriptNeeded) compare(tile.roomEvents.length,0)
   var cold = findChild(tile,"coldSynopsis")
   verify(cold === null || (!cold.visible && cold.count === 0),"No cold historical synopsis in terminal body")
  }
  function test_nonterminal_preview_is_preserved() {
   tile.roomData = {id:"control",source:"control"}
   var fallback = findChild(tile,"historyFallback")
   tryCompare(fallback,"count",1)
   compare(fallback.visible,true)
   compare(terminalBody.visible,false)
  }
 }
}
'''.replace("TILE_PROPERTIES", properties).replace("EVENT_PROPERTY", events).replace(
            "CONTAINER_VISIBLE", container_visible,
        ).replace("COLD_LIST", cold).replace("FALLBACK_LIST", fallback).replace("Style.", "styleStub.")
        with tempfile.TemporaryDirectory(prefix="ares-tile-history-", dir=os.environ.get("TMPDIR")) as tmp:
            (Path(tmp) / "tst_history.qml").write_text(qml)
            result = subprocess.run([RUNNER, "-input", tmp], capture_output=True, text=True, timeout=25,
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("ReferenceError", result.stdout + result.stderr)

    def test_search_click_and_cached_background(self):
        source = (ROOT / "AresWorkspace.qml").read_text()
        mouse = block(source[source.index("id: treeMouse"):], "onClicked:")
        helper = block(source, "function activateSearchRoom(") if "function activateSearchRoom(" in source else ""
        frame_match = re.search(r"readonly property bool showSelectedFrame:.*?(?=\n  (?:readonly|property|function))", source, re.S)
        assert frame_match is not None
        frame = frame_match.group()
        tiles = (ROOT / "ExtendedWorkspace.qml").read_text()
        image = block(tiles[tiles.index('source: tile.cachedFrame') - 70:], 'Image {')
        visibility = re.search(r"visible: ([^\n]+)", image)
        tile_visible = visibility.group(1) if visibility else "true"
        qml = '''import QtQuick
import QtTest
Item {
 id: workspace; width: 500; height: 400
 property var controller: ctl
 property int cacheSlot: 0
 property var selectedFrame: ({image:{url:""}})
 FRAME_BINDING
 HELPER
 QtObject {
  id: ctl
  property string selectedRoomId: "original"
  property var selectedRoom: ({id:"original",source:"hermes"})
  function selectRecentRoom(room) { selectedRoomId = room.id }
 }
 QtObject { id: compactTranscriptA; property bool surfacePainted: false; property bool legacyTranscriptNeeded: false; property string error: "" }
 QtObject { id: compactTranscriptB; property bool surfacePainted: false; property bool legacyTranscriptNeeded: false; property string error: "" }
 QtObject { id: tile; property var cachedFrame: ({image:{url:""}}) }
 QtObject { id: tileTerminalChat; property bool surfacePainted: false; property bool legacyTranscriptNeeded: false; property string error: "" }
 Item { id: backdrop; visible: TILE_VISIBLE }
 QtObject {
  id: roomSearch; property bool expanded: false
  function close() { expanded = false }
 }
 ListView {
  id: list; width: 400; height: 200
  model: roomSearch.expanded ? [{room:{id:"chosen"}}] : []
  delegate: Item {
   id: treeRow; required property var modelData
   property bool roomItem: true
   property var roomData: modelData.room
   width: 400; height: 60
   MouseArea { anchors.fill: parent; MOUSE_HANDLER }
  }
 }
 TestCase {
  name: "ChatSurfaceRegressions"; when: windowShown
  function test_search_click_survives_delegate_destruction() {
   roomSearch.expanded = true
   tryCompare(list,"count",1)
   wait(30)
   mouseClick(list,50,30)
   compare(ctl.selectedRoomId,"chosen")
   verify(!roomSearch.expanded)
  }
  function test_compact_frame_disappears_when_live_or_legacy() {
   compactTranscriptA.surfacePainted = false
   compare(workspace.showSelectedFrame,true)
   compactTranscriptA.surfacePainted = true
   compare(workspace.showSelectedFrame,false)
   compactTranscriptA.surfacePainted = false
   compactTranscriptA.legacyTranscriptNeeded = true
   compare(workspace.showSelectedFrame,false)
  }
  function test_tile_frame_disappears_when_live_or_legacy() {
   compare(backdrop.visible,true)
   tileTerminalChat.surfacePainted = true
   compare(backdrop.visible,false)
   tileTerminalChat.surfacePainted = false
   tileTerminalChat.legacyTranscriptNeeded = true
   compare(backdrop.visible,false)
  }
 }
}
'''.replace("FRAME_BINDING", frame).replace("HELPER", helper).replace("MOUSE_HANDLER", mouse).replace("TILE_VISIBLE", tile_visible)
        with tempfile.TemporaryDirectory(prefix="ares-surfaces-", dir=os.environ.get("TMPDIR")) as tmp:
            (Path(tmp) / "tst_surfaces.qml").write_text(qml)
            result = subprocess.run([RUNNER, "-input", tmp], capture_output=True, text=True, timeout=25,
                                    env={**os.environ,"QT_QPA_PLATFORM":"offscreen","QT_QUICK_BACKEND":"software"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("ReferenceError", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
