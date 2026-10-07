"""Run production attachment controls with isolated theme tokens and real Qt input."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = shutil.which("qmltestrunner") or "/usr/lib/qt6/bin/qmltestrunner"


def qml_blocks(source, type_name):
    """Extract exact production instances, without importing process/terminal owners."""
    result = []
    for match in re.finditer(r"\b" + type_name + r"\s*\{", source):
        depth = 1
        quote = None
        escaped = False
        for position in range(match.end(), len(source)):
            char = source[position]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    result.append(source[match.start():position + 1])
                    break
    return result


@unittest.skipUnless(Path(RUNNER).is_file(), "Qt Quick Test runner unavailable")
class ComposerAttachmentsQmlTests(unittest.TestCase):
    def run_qml(self, body, components=("ComposerAttachments.qml",)):
        for name in components:
            self.assertTrue((ROOT / name).is_file(), f"Missing production component: {name}")
        with tempfile.TemporaryDirectory(prefix="ares-attachments-test-", dir=os.environ.get("TMPDIR")) as directory:
            path = Path(directory)
            for name in components:
                shutil.copy2(ROOT / name, path / name)
            common = path / "imports" / "qs" / "Commons"
            common.mkdir(parents=True)
            (common / "qmldir").write_text("module qs.Commons\nsingleton Color 1.0 Color.qml\nsingleton Style 1.0 Style.qml\n")
            (common / "Color.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 property QtObject popups: QtObject { property color text: "white"; property color background: "#202020" }
 property color accent: "steelblue"
 property color urgent: "tomato"
}
''')
            (common / "Style.qml").write_text('''pragma Singleton
import QtQuick
QtObject {
 function space(n) { return n }
 property real cornerRadius: 6
 property QtObject font: QtObject { property string family: "sans-serif"; property int body: 14; property int caption: 10 }
}
''')
            (path / "wide.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100"><rect width="400" height="100" fill="blue"/></svg>')
            (path / "tst_attachments.qml").write_text("import QtQuick\nimport QtQuick.Controls\nimport QtTest\n" + body)
            result = subprocess.run(
                [RUNNER, "-input", str(path), "-import", str(path / "imports")],
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
                capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("ReferenceError", result.stderr)
            self.assertNotIn("TypeError", result.stderr)
            self.assertNotIn("QWARN", result.stdout + result.stderr)

    def test_horizontal_cards_and_independent_actions(self):
        self.run_qml('''Item {
 width: 500; height: 300
 ComposerAttachments {
  id: gallery; width: 470
  images: [
   {url: Qt.resolvedUrl("wide.svg").toString(), name: "first.png", uploading: true},
   {url: Qt.resolvedUrl("wide.svg").toString(), name: "second.png", error: "Disk full"},
   {url: Qt.resolvedUrl("wide.svg").toString(), name: "third.png", artifactId: "saved"}
  ]
 }
 SignalSpy { id: previews; target: gallery; signalName: "previewRequested" }
 SignalSpy { id: removals; target: gallery; signalName: "removeRequested" }
 TestCase {
  name: "ComposerGallery"; when: windowShown
  function test_cards() {
   var first = findChild(gallery, "attachmentCard0")
   var second = findChild(gallery, "attachmentCard1")
   verify(first !== null); verify(second !== null)
   compare(first.width, second.width); compare(first.y, second.y)
   verify(second.x >= first.x + first.width)
   var image = findChild(first, "attachmentThumbnail")
   var label = findChild(first, "attachmentFilename")
   verify(label.y >= image.y + image.height)
   compare(label.text, "first.png")
   compare(findChild(first, "attachmentStatus").text, "Saving…")
   compare(findChild(second, "attachmentStatus").text, "Save failed")
   mouseClick(image)
   compare(previews.count, 1); compare(previews.signalArguments[0][1], "first.png")
   compare(removals.count, 0)
   mouseClick(findChild(first, "attachmentRemove"))
   compare(removals.count, 1); compare(removals.signalArguments[0][0], 0)
   compare(previews.count, 1)
   gallery.removalEnabled = false
   mouseClick(findChild(second, "attachmentRemove"))
   compare(removals.count, 1)
  }
 }
}
''')

    def test_narrow_scroll_keyboard_and_warning(self):
        self.run_qml('''Item {
 width: 220; height: 320
 ComposerAttachments {
  id: gallery; width: 180
  warningText: "Images are saved in this draft. Sending images from Edit to Herdr is not supported. Nothing has been sent."
  images: [0,1,2,3,4].map(function(n) {
   return {url: Qt.resolvedUrl("wide.svg").toString(), name: "image" + n + ".png"}
  })
 }
 SignalSpy { id: previews; target: gallery; signalName: "previewRequested" }
 TestCase {
  name: "NarrowGallery"; when: windowShown
  function test_scroll_and_keyboard() {
   var list = findChild(gallery, "attachmentList")
   verify(list.contentWidth > list.width)
   verify(list.clip)
   mouseWheel(list, 60, 50, 0, -120)
   tryVerify(function() { return list.contentX > 0 })
   list.positionViewAtEnd(); wait(100)
   var last = findChild(gallery, "attachmentCard4")
   verify(last !== null)
   var thumb = findChild(last, "attachmentThumbnail")
   mouseClick(thumb); compare(previews.count, 1)
   compare(previews.signalArguments[0][1], "image4.png")
   thumb.forceActiveFocus(); keyClick(Qt.Key_Return)
   compare(previews.count, 2)
   var warning = findChild(gallery, "attachmentWarning")
   verify(warning.visible); verify(warning.height > 20)
   verify(gallery.implicitHeight >= warning.y + warning.height)
  }
 }
}
''')

    def test_gallery_keeps_its_height_in_a_constrained_editor(self):
        self.run_qml('''import QtQuick.Layouts
Item {
 width: 320; height: 320
 ColumnLayout {
  anchors.fill: parent
  Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 360; color: "gray" }
  ComposerAttachments {
   id: gallery; Layout.fillWidth: true
   images: [{url: Qt.resolvedUrl("wide.svg").toString(), name: "image.png"}]
   warningText: "Images are saved in this draft. Sending images from Edit to Herdr is not supported. Nothing has been sent."
  }
  Button { text: "Keep draft" }
 }
 TestCase {
  name: "ConstrainedEditor"; when: windowShown
  function test_height() {
   var warning = findChild(gallery, "attachmentWarning")
   verify(gallery.height >= warning.y + warning.height)
  }
 }
}
''')

    def test_all_four_workspace_sites(self):
        for filename, owner, inline_editor, modal_editor, modal_flag, owns_input in (
            ("AresWorkspace.qml", "workspace", "composerInput", "extendedArea", "extendedEditorOpen", "selectedTerminalOwnsInput"),
            ("ExtendedWorkspace.qml", "extended", "expandedInput", "expandedArea", "expandedEditorOpen", "currentTerminalOwnsInput"),
        ):
            source = (ROOT / filename).read_text()
            galleries = qml_blocks(source, "ComposerAttachments")
            self.assertEqual(len(galleries), 2, filename + " must integrate both composer sites")
            previews = qml_blocks(source, "ImagePreview")
            self.assertEqual(len(previews), 1)
            self.assertIn("\n  ImagePreview {", source, "Preview must be a workspace-root sibling")
            for index, gallery in enumerate(galleries):
                with self.subTest(workspace=filename, composer=index):
                    body = '''Item {
 id: OWNER; width: 700; height: 560
 property bool MODAL_FLAG: MODAL_OPEN
 property bool editMode: true
 property bool OWNS_INPUT: true
 property var currentRoom: ({id: "room-a"})
 QtObject {
  id: controller
  property string selectedRoomId: "room-a"
  property string section: "room"
  property string extendedViewMode: "grown"
  property bool opened: true
  property bool editMode: true
  property bool sendingImage: false
  property var pendingImages: [{url: Qt.resolvedUrl("wide.svg").toString(), name: "restored.png", path: "/restored"}]
  function removePendingImage(index) { var next = pendingImages.slice(); next.splice(index, 1); pendingImages = next }
 }
 TextArea { id: INLINE_EDITOR; y: 350; width: 500; height: 80; text: "Keep draft" }
 TextArea { id: MODAL_EDITOR; y: 440; width: 500; height: 80; text: "Keep draft" }
 ColumnLayout { width: 650; GALLERY }
 PREVIEW
 TestCase {
  name: "WorkspaceAttachments"; when: windowShown
  function test_connected_gallery() {
   var gallery = findChild(OWNER, "GALLERY_NAME")
   verify(gallery.visible)
   verify(gallery.warningText.indexOf("Nothing has been sent") >= 0)
   var thumb = findChild(gallery, "attachmentThumbnail")
   mouseClick(thumb); verify(attachmentPreview.opened)
   keyClick(Qt.Key_Escape); verify(!attachmentPreview.opened)
   tryCompare(FOCUS_EDITOR, "activeFocus", true)
   compare(FOCUS_EDITOR.text, "Keep draft")
   mouseClick(thumb); controller.selectedRoomId = "room-b"
   verify(!attachmentPreview.opened)
   mouseClick(thumb); controller.opened = false; verify(!attachmentPreview.opened)
   controller.opened = true
   mouseClick(thumb); controller.pendingImages = []; verify(!attachmentPreview.opened)
   controller.pendingImages = [{url: Qt.resolvedUrl("wide.svg").toString(), name: "remove.png", path: "/remove"}]
   wait(20); mouseClick(findChild(gallery, "attachmentRemove"))
   compare(controller.pendingImages.length, 0)
  }
 }
}
'''
                    replacements = {
                        "OWNER": owner, "MODAL_FLAG": modal_flag, "MODAL_OPEN": "true" if index else "false",
                        "OWNS_INPUT": owns_input, "INLINE_EDITOR": inline_editor, "MODAL_EDITOR": modal_editor,
                        "GALLERY_NAME": "attachmentExpandedGallery" if index else "attachmentCompactGallery",
                        "FOCUS_EDITOR": modal_editor if index else inline_editor,
                        "GALLERY": gallery, "PREVIEW": previews[0],
                    }
                    for token, value in replacements.items():
                        body = body.replace(token, value)
                    self.run_qml("import QtQuick.Layouts\n" + body, ("ComposerAttachments.qml", "ImagePreview.qml"))

    def test_send_enablement_preserves_transport_limits(self):
        for filename, owner, button_type, owns_input in (
            ("AresWorkspace.qml", "workspace", "QuietButton", "selectedTerminalOwnsInput"),
            ("ExtendedWorkspace.qml", "extended", "ExtendedButton", "currentTerminalOwnsInput"),
        ):
            buttons = [block for block in qml_blocks((ROOT / filename).read_text(), button_type)
                       if "submitEditPrompt()" in block and "enabled:" in block]
            self.assertEqual(len(buttons), 2, filename)
            for button in buttons:
                expression = button.split("enabled:", 1)[1].split("onActivated:", 1)[0].strip()
                body = '''Item {
 id: OWNER; width: 300; height: 100
 property bool OWNS_INPUT: false
 QtObject {
  id: controller
  property string roomText: ""
  property bool sendingImage: false
  property var pendingImages: [{artifactId: "saved"}]
 }
 Button { id: send; enabled: EXPRESSION }
 TestCase {
  name: "ImageSendSafety"; when: windowShown
  function test_enablement() {
   verify(send.enabled, "Non-terminal image-only drafts can send")
   OWNER.OWNS_INPUT = true
   verify(!send.enabled, "Terminal images remain unsupported")
   controller.roomText = "caption"
   verify(!send.enabled, "A caption cannot enable terminal image sends")
   OWNER.OWNS_INPUT = false; verify(send.enabled)
   controller.sendingImage = true; verify(!send.enabled)
   controller.sendingImage = false
   controller.pendingImages = [{uploading: true}]; verify(!send.enabled)
   controller.pendingImages = [{error: "Disk full"}]; verify(!send.enabled)
  }
 }
}
'''.replace("OWNER", owner).replace("OWNS_INPUT", owns_input).replace("EXPRESSION", expression)
                self.run_qml(body, ())

    def test_preview_dismissal_focus_and_lifecycle(self):
        self.run_qml('''Item {
 id: root; width: 700; height: 500
 TextArea { id: editor; x: 10; y: 280; width: 600; height: 180; text: "Keep this draft" }
 Rectangle { anchors.fill: parent; z: 40; color: "transparent" }
 ImagePreview {
  id: preview; anchors.fill: parent
  attachments: [
   {url: Qt.resolvedUrl("wide.svg").toString(), path: "/restored/image", name: "first.png", artifactId: "restored"},
   {url: Qt.resolvedUrl("wide.svg").toString(), token: "second", name: "second.png"}
  ]
  contextKey: "room-a"; viewActive: true
 }
 TestCase {
  name: "AttachmentPreview"; when: windowShown
  function init() {
   preview.dismiss(false); preview.contextKey = "room-a"; preview.viewActive = true
   preview.attachments = [
    {url: Qt.resolvedUrl("wide.svg").toString(), path: "/restored/image", name: "first.png", artifactId: "restored"},
    {url: Qt.resolvedUrl("wide.svg").toString(), token: "second", name: "second.png"}
   ]
   editor.forceActiveFocus()
  }
  function open() {
   preview.open(preview.attachments[0].url, "first.png", editor)
   verify(preview.opened); verify(preview.z > 40)
   tryCompare(preview, "activeFocus", true)
  }
  function test_aspect_fit_and_escape() {
   open()
   var image = findChild(preview, "previewImage")
   tryCompare(image, "status", Image.Ready)
   compare(image.fillMode, Image.PreserveAspectFit)
   verify(image.paintedWidth <= image.width); verify(image.paintedHeight <= image.height)
   fuzzyCompare(image.paintedWidth / image.paintedHeight, 4, 0.01)
   keyClick(Qt.Key_Escape)
   verify(!preview.opened); tryCompare(editor, "activeFocus", true)
   compare(editor.text, "Keep this draft")
  }
  function test_close_backdrop_and_content() {
   open(); mouseClick(findChild(preview, "previewImage")); verify(preview.opened)
   mouseClick(findChild(preview, "previewClose"))
   verify(!preview.opened); tryCompare(editor, "activeFocus", true)
   open(); mouseClick(preview, 2, 2)
   verify(!preview.opened); tryCompare(editor, "activeFocus", true)
   compare(editor.text, "Keep this draft")
  }
  function test_context_view_and_removal() {
   open(); preview.contextKey = "room-b"; verify(!preview.opened)
   open(); preview.viewActive = false; verify(!preview.opened)
   preview.viewActive = true; open()
   // Fresh DTO objects and removal of another attachment must preserve the preview.
   preview.attachments = [Object.assign({}, preview.attachments[0])]
   verify(preview.opened)
   preview.attachments = []; verify(!preview.opened)
   tryCompare(editor, "activeFocus", true)
  }
 }
}
''', ("ImagePreview.qml",))


if __name__ == "__main__":
    unittest.main()
