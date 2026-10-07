"""Isolated offscreen QML wiring tests: fake PTY module, no processes or network."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"


@unittest.skipUnless(Path(RUNNER).is_file(), "Qt Quick Test runner unavailable")
class TerminalQmlLifecycleTests(unittest.TestCase):
    def run_probe(self, output_signal):
        with tempfile.TemporaryDirectory(prefix="ares-tty-qml-", dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory)
            for name in ["TerminalChat.qml", "TerminalConnectionModel.js", "NativeGroupDetails.qml", "RemoteRoomHistory.qml"]:
                shutil.copy2(ROOT / name, path / name)
            (path / "MapChatPreview.qml").write_text("""import QtQuick
Item { property var bridge; property bool sourcePreviewLabel: false
  property string roomId: ""
  function openRoom(id, name) { roomId = id }
  function dismiss() { roomId = "" }
}
""")
            (path / "ChatTranscript.qml").write_text("""import QtQuick
Item {
 property var controller; property var bridge; property string roomId
 property var eventModel; property color foreground; property color dim
 property color surface; property color surfaceStrong; property color accentWash
 property string fontFamily
}
""")
            imports = path / "imports"
            common = imports / "qs" / "Commons"
            common.mkdir(parents=True)
            (common / "qmldir").write_text("module qs.Commons\nsingleton Color 1.0 Color.qml\n")
            (common / "Color.qml").write_text("""pragma Singleton
import QtQuick
QtObject {
 property QtObject popups: QtObject { property color text: "white"; property color background: "black" }
 property color accent: "blue"; property color urgent: "red"
}
""")
            module = imports / "QMLTermWidget"
            module.mkdir()
            (module / "qmldir").write_text("module QMLTermWidget\nQMLTermWidget 2.0 QMLTermWidget.qml\nQMLTermSession 2.0 QMLTermSession.qml\n")
            (module / "QMLTermWidget.qml").write_text("""import QtQuick
Item {
 objectName: "fakeTerminal"
 property var session
 property font font
 property bool localSelection
 property bool readOnlyInput
 property bool passivePreviewWheel
 property bool previewHideInput
 property string colorScheme
 property color terminalBackground
 property real backgroundOpacity
 signal imagePainted()
}
""")
            (module / "QMLTermSession.qml").write_text("""import QtQuick
QtObject {
 objectName: "fakeSession"
 property string shellProgram
 property var shellProgramArgs
 property int startCount: 0
 signal started()
 signal finished()
 %s
 function startShellProgram() { startCount++; started() }
}
""" % ("signal outputReceived()" if output_signal else ""))
            (path / "tst_lifecycle.qml").write_text("""import QtQuick
import QtTest
Item {
 width: 800; height: 600
 QtObject {
  id: fakeController
  property var rooms: [{id: "one", source: "codex"}, {id: "two", source: "kimi"},
    {id: "local-history", source: "hermes", externalProfile: "default", externalSessionId: "exact-one"},
    {id: "peer", source: "hermes", externalProfile: "remote::paired::default"},
    {id: "group", source: "hermes-native-group", nativeGroup: {members: [
      {profile: "builder", host: "local", resolution: "resolved", storedSessionId: "stored-one"},
      {profile: "cto", connectionId: "unverified", resolution: "unsupported", storedSessionId: null}]}}]
  property string pluginDir: "/does-not-exist"
  property int terminalFontSize: 10
  property color terminalBackgroundColor: "black"
  property real terminalBackgroundOpacity: 1
 }
 QtObject {
  id: fakeBridge
  property bool available: true
  property var requests: []
  signal connectionChanged(bool online)
  function request(method, path, body, callback) {
   var rows = requests.slice(); rows.push({method:method, path:path, body:body, callback:callback}); requests = rows
  }
 }
 Component { id: component; TerminalChat { controller: fakeController; bridge: fakeBridge; roomId: "one" } }
 TestCase {
  id: tc
  name: "TerminalLifecycle"
  when: windowShown
  property var chat: null
  function init() {
   fakeBridge.requests = []
   chat = createTemporaryObject(component, tc, {width:700, height:400, active:true})
   verify(chat !== null)
   compare(fakeBridge.requests.length, 1)
  }
  function cleanup() { chat.active = false }
  function test_group_catalog_never_requests_terminal_or_legacy_chat() {
   chat.active = false
   fakeBridge.requests = []
   chat.roomId = "group"
   chat.active = true
   wait(80)
   compare(chat.terminalRoom, false)
   compare(chat.terminalOwnsInput, false)
   compare(chat.legacyTranscriptNeeded, false)
   compare(findChild(chat, "fakeTerminal"), null)
   var details = findChild(chat, "nativeGroupDetails")
   verify(details !== null)
   compare(findChild(chat, "nativeGroupMembers").count, 2)
   compare(details.members[0].storedSessionId, "stored-one")
   compare(details.members[1].resolution, "unsupported")
   chat.previewReadOnly = true
   chat.previewReadOnly = false
   chat.active = false
   chat.active = true
   chat.showChooser()
   chat.focusRoom(true)
   wait(80)
   verify(fakeBridge.requests.every(function(call) { return call.method === "GET" && call.path.indexOf("/v1/groups/group/history") === 0 }))
   compare(chat.chooserOpen, false)
  }
  function test_local_herdr_room_opens_an_isolated_session_when_selected() {
   chat.active = false
   chat.parent = tc.parent
   chat.visible = true
   fakeBridge.requests = []
   chat.roomId = "local-history"
   chat.active = true
   compare(chat.localHermesRoom, true)
   compare(chat.terminalInteractive, false)
   compare(chat.terminalOwnsInput, false)
   compare(chat.legacyTranscriptNeeded, false)
   compare(fakeBridge.requests.length, 1)
   compare(fakeBridge.requests[0].body.openIfMissing, true)
   fakeBridge.requests[0].callback({ok:false,error:"No matching terminal"})
   chat.historyOpen = true

   tryVerify(function() { return findChild(chat, "historyLoader") !== null })
   var loader = findChild(chat, "historyLoader")
   tryVerify(function() { return loader.item !== null })
   var history = findChild(loader.item, "boundedHistory")
   verify(history !== null)
   compare(fakeBridge.requests.length, 1)
   history.loadHistory()
   compare(fakeBridge.requests[1].method, "GET")
   compare(fakeBridge.requests[1].path, "/v1/rooms/local-history/history")
   fakeBridge.requests[1].callback({ok:true,messages:[{role:"user",text:"hello",cursor:"10"}],has_more:false,next_before:null})
   compare(history.rows.length, 1)
   chat.roomId = "two"
   compare(history.rows.length, 0)
   compare(chat.historyOpen, false)
  }
  function test_peer_selection_is_terminal_first_and_blocked_without_owner() {
   chat.active = false
   fakeBridge.requests = []
   chat.roomId = "peer"
   chat.active = true
   compare(chat.terminalRoom, true)
   compare(chat.terminalOwnsInput, false)
   compare(chat.legacyTranscriptNeeded, false)
   compare(chat.terminalInteractive, false)
   compare(fakeBridge.requests.length, 1)
   compare(fakeBridge.requests[0].method, "POST")
   compare(fakeBridge.requests[0].path, "/v1/tty/rooms/peer/focus")
   compare(fakeBridge.requests[0].body.openIfMissing, true)
   fakeBridge.requests[0].callback({ok:false,error:"Unavailable"})
   compare(chat.targetResolved, false)
   compare(findChild(chat, "fakeTerminal"), null)
   compare(chat.terminalInteractive, false)
  }
  function resolve() {
   fakeBridge.requests[0].callback({ok:true, tty:{sessionName:"terminal", agentName:"term_x", sshTarget:"", remoteBin:"/bin/herdr", remoteSocket:"/run/herdr.sock"}})
   tryCompare(chat, "targetResolved", true)
   tryVerify(function() { return chat.connection.started })
   return findChild(chat, "fakeTerminal")
  }
  function test_spawn_and_resize_once() {
   var view = resolve()
   verify(view !== null)
   compare(view.session.startCount, 1)
   compare(view.session.shellProgramArgs, ["terminal","term_x","","/bin/herdr","/run/herdr.sock"])
   compare(chat.terminalInteractive, false)
   chat.width = 100; wait(70); chat.width = 700; wait(70)
   compare(view.session.startCount, 1)
   %s
   // Plain `terminal attach` output is not exclusive writer proof for Ares rooms.
   compare(chat.terminalInteractive, false)
   compare(view.enabled, false)
   compare(view.readOnlyInput, true)
   compare(view.previewHideInput, true)
   view.session.finished()
   compare(chat.connection.phase, "failed")
   compare(chat.terminalOwnsInput, false)
  }
  function test_failed_terminal_does_not_overlay_legacy_chat() {
   fakeBridge.requests[0].callback({ok:false,error:"No matching terminal"})
   compare(chat.connection.phase,"failed")
   compare(chat.terminalOwnsInput,false)
   compare(chat.legacyTranscriptNeeded,false)
  }
  function test_small_geometry_waits_without_claiming_started() {
   chat.width = 100
   fakeBridge.requests[0].callback({ok:true, tty:{sessionName:"terminal", agentName:"term_x", sshTarget:"", remoteBin:"/bin/herdr", remoteSocket:"/run/herdr.sock"}})
   wait(80)
   var view = findChild(chat, "fakeTerminal")
   verify(view !== null)
   compare(view.session.startCount, 0)
   compare(chat.terminalAttached, false)
   compare(chat.terminalOwnsInput, true)
   chat.width = 700
   tryCompare(view.session, "startCount", 1)
  }
  function test_preview_stays_passive() {
   chat.previewReadOnly = true
   chat.focusRoom(true)
   compare(fakeBridge.requests[1].path, "/v1/tty/rooms/one/ensure")
   fakeBridge.requests[1].callback({ok:true, tty:{sessionName:"terminal", agentName:"term_x", sshTarget:"", remoteBin:"/bin/herdr", remoteSocket:"/run/herdr.sock"}})
   tryVerify(function() { return chat.connection.started })
   var view = findChild(chat, "fakeTerminal")
   compare(view.enabled, false)
   compare(view.readOnlyInput, true)
   compare(chat.terminalInteractive, false)
   chat.showChooser()
   compare(chat.chooserOpen, false)
  }
  function test_chooser_is_explicit() {
   chat.showChooser()
   compare(fakeBridge.requests.length, 2)
   compare(fakeBridge.requests[1].method, "GET")
   fakeBridge.requests[1].callback({ok:true, host:"local", terminals:[{id:"opaque",terminalId:"term_raw",workspace:"work",label:"tab",paneId:"pane",agentKind:"claude"}], errors:[]})
   chat.chooserSelection = "opaque"
   chat.confirmTerminal(false)
   compare(fakeBridge.requests.length, 2)
   chat.confirmTerminal(true)
   compare(fakeBridge.requests.length, 3)
   compare(fakeBridge.requests[2].body, {terminalId:"opaque",confirmed:true})
   chat.cancelChooser()
  }
 }
}
""" % ("view.session.outputReceived(); compare(chat.connection.phase, 'output'); compare(chat.terminalInteractive, false)" if output_signal else "compare(chat.connection.phase, 'waiting-output')"))
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
            result = subprocess.run([RUNNER, "-input", str(path), "-import", str(imports)],
                                    capture_output=True, text=True, env=env, timeout=30)
            log = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, log)
            self.assertNotIn("ReferenceError", log)
            self.assertNotIn("TypeError", log)
            self.assertNotIn("Binding loop", log)

    def test_new_module(self):
        self.run_probe(True)

    def test_older_module_without_output_signal(self):
        self.run_probe(False)


if __name__ == "__main__":
    unittest.main()
