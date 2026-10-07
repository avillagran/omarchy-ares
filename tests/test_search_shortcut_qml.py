"""Exercise production window-shortcut and focus control with real Qt key delivery.

The overlay field is a minimal fixture; the actual palette has its own Qt tests.
No service, network, terminal, or user session is involved.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"
CATCHER = Path("/usr/share/omarchy/shell/Ui/PanelKeyCatcher.qml")


@unittest.skipUnless(Path(RUNNER).is_file() and CATCHER.is_file(), "Qt/Omarchy fixture dependencies unavailable")
class SearchShortcutQmlTests(unittest.TestCase):
    def test_window_shortcut_and_focus_return(self):
        source = (ROOT / "Panel.qml").read_text()
        def extract(pattern):
            match = re.search(pattern, source, re.S)
            assert match is not None, "Production search integration is missing"
            return match.group(0)

        functions = "\n".join(extract(r"  function " + name + r"\([^\n]*\) \{.*?\n  \}")
                              for name in ("openSearch", "closeSearch", "toggleSearch"))
        shortcut = extract(r"      Shortcut \{\n        objectName: \"roomSearchShortcut\".*?\n      \}")
        fixture = '''import QtQuick
import QtQuick.Controls
import QtTest
Item {
 id: root; width: 680; height: 400
 property bool opened: true
 property bool searchOpen: false
 property var searchReturnFocus: null
 property string searchShortcut: "Ctrl+Space"
 FUNCTIONS
 PanelKeyCatcher {
  id: keyCatcher; anchors.fill: parent
  blocked: root.searchOpen
  SHORTCUT
  TextField { id: draft; objectName: "draft"; text: "unsent draft"; enabled: !root.searchOpen }
 }
 FocusScope {
  id: overlay; anchors.fill: parent; visible: root.searchOpen
  onVisibleChanged: if (visible) query.forceActiveFocus()
  TextField {
   id: query
   Keys.onEscapePressed: function(event) { root.closeSearch(true); event.accepted = true }
  }
 }
 TestCase {
  name: "AresSearchShortcut"; when: windowShown
  function init() {
   root.closeSearch(false); root.opened = true; root.searchShortcut = "Ctrl+Space"
   query.text = ""; draft.text = "unsent draft"; draft.forceActiveFocus()
   tryCompare(draft,"activeFocus",true)
  }
  function test_ctrl_space_and_cancel_preserve_draft() {
   keyClick(Qt.Key_Space, Qt.ControlModifier)
   tryCompare(root,"searchOpen",true); tryCompare(query,"activeFocus",true)
   keyClick(Qt.Key_A); compare(query.text,"a"); compare(draft.text,"unsent draft")
   keyClick(Qt.Key_Escape)
   tryCompare(root,"searchOpen",false); tryCompare(draft,"activeFocus",true)
   compare(draft.text,"unsent draft")
  }
  function test_preference_selects_only_one_shortcut() {
   root.searchShortcut = "Alt+Space"
   keyClick(Qt.Key_Space, Qt.ControlModifier); verify(!root.searchOpen)
   keyClick(Qt.Key_Space, Qt.AltModifier); tryCompare(root,"searchOpen",true)
   keyClick(Qt.Key_Space, Qt.AltModifier); tryCompare(root,"searchOpen",false)
   tryCompare(draft,"activeFocus",true)
  }
  function test_closed_app_does_not_handle_shortcut() {
   root.opened = false
   keyClick(Qt.Key_Space, Qt.ControlModifier); verify(!root.searchOpen)
  }
 }
}
'''.replace("FUNCTIONS", functions).replace("SHORTCUT", shortcut)
        with tempfile.TemporaryDirectory(prefix="ares-search-shortcut-", dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory)
            shutil.copy2(CATCHER, path / CATCHER.name)
            for name in ("RoomSearchPalette.qml", "RoomSearchModel.js"):
                shutil.copy2(ROOT / name, path / name)
            common = path / "imports" / "qs" / "Commons"
            common.mkdir(parents=True)
            (common / "qmldir").write_text("module qs.Commons\nsingleton Color 1.0 Color.qml\nsingleton Style 1.0 Style.qml\n")
            (common / "Color.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 property QtObject popups: QtObject { property color text: "white"; property color background: "black"; property color border: "gray" }
 property color accent: "steelblue"
}
''')
            (common / "Style.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 function space(n) { return n }
 property int cornerRadius: 8
 property QtObject font: QtObject { property string family: "sans-serif"; property int bodySmall: 12; property int caption: 10 }
}
''')
            actual_palette = re.sub(r" FocusScope \{.*?\n TestCase \{", '''
 property var query: null
 Component.onCompleted: query = suite.findChild(overlay, "paletteInput")
 RoomSearchPalette {
  id: overlay
  opened: root.searchOpen
  onDismissed: root.closeSearch(true)
 }
 TestCase {
  id: suite
''', fixture, flags=re.S)
            for version, content in [("focus fixture", fixture), ("actual palette", actual_palette)]:
                with self.subTest(version=version):
                    (path / "tst_shortcut.qml").write_text(content)
                    result = subprocess.run([RUNNER, "-input", str(path), "-import", str(path / "imports")],
                                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("QWARN", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
