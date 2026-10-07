import QtQuick
import QtQuick.Controls
import qs.Commons

FocusScope {
  id: preview
  property var attachments: []
  property string contextKey: ""
  property bool viewActive: true
  property bool opened: false
  property string source: ""
  property string imageName: ""
  property string attachmentKey: ""
  property Item returnFocusItem: null
  readonly property color foreground: Color.popups.text
  z: 1000
  visible: opened
  enabled: opened

  function keyFor(image) {
    return String(image.token || image.artifactId || image.path || image.url || "")
  }
  function open(imageSource, name, editor) {
    if (!viewActive) return
    source = imageSource
    imageName = name
    attachmentKey = ""
    for (var i = 0; i < attachments.length; ++i) {
      if (String(attachments[i].url || "") === imageSource && String(attachments[i].name || "Image") === name) {
        attachmentKey = keyFor(attachments[i])
        break
      }
    }
    returnFocusItem = editor
    opened = true
    forceActiveFocus()
  }
  function dismiss(restoreFocus) {
    if (!opened) return
    opened = false
    source = ""
    attachmentKey = ""
    var editor = returnFocusItem
    returnFocusItem = null
    if (restoreFocus !== false && editor && editor.visible && editor.enabled)
      editor.forceActiveFocus()
  }
  onContextKeyChanged: dismiss(false)
  onViewActiveChanged: if (!viewActive) dismiss(false)
  onAttachmentsChanged: {
    if (!opened || !attachmentKey) return
    for (var i = 0; i < attachments.length; ++i) {
      if (keyFor(attachments[i]) === attachmentKey) return
    }
    dismiss(true)
  }
  Keys.onEscapePressed: function(event) { dismiss(true); event.accepted = true }
  Keys.onTabPressed: function(event) { closeButton.forceActiveFocus(); event.accepted = true }
  Keys.onBacktabPressed: function(event) { closeButton.forceActiveFocus(); event.accepted = true }

  Rectangle {
    anchors.fill: parent
    color: Qt.rgba(0, 0, 0, 0.72)
    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.AllButtons
      onClicked: preview.dismiss(true)
      onWheel: function(wheel) { wheel.accepted = true }
    }
  }
  Rectangle {
    id: frame
    anchors.centerIn: parent
    width: Math.max(1, Math.min(parent.width - Style.space(32), Style.space(1000)))
    height: Math.max(1, parent.height - Style.space(48))
    radius: Style.cornerRadius
    color: Color.popups.background
    border.color: Color.accent
    // Absorb content clicks without dismissing or reaching the editor below.
    MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }
    Text {
      anchors.left: parent.left
      anchors.right: closeButton.left
      anchors.top: parent.top
      anchors.margins: Style.space(12)
      text: preview.imageName
      textFormat: Text.PlainText
      elide: Text.ElideMiddle
      color: preview.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }
    ToolButton {
      id: closeButton
      objectName: "previewClose"
      anchors.top: parent.top
      anchors.right: parent.right
      anchors.margins: Style.space(6)
      width: Style.space(32); height: width
      text: "×"
      Accessible.name: "Close image preview"
      ToolTip.visible: hovered
      ToolTip.text: "Close image preview (Esc)"
      contentItem: Text {
        text: closeButton.text
        color: preview.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
      }
      background: Rectangle {
        radius: Style.space(4)
        color: closeButton.hovered || closeButton.activeFocus ? Color.accent : "transparent"
      }
      onClicked: preview.dismiss(true)
      Keys.onTabPressed: function(event) { event.accepted = true }
      Keys.onBacktabPressed: function(event) { event.accepted = true }
    }
    Image {
      id: image
      objectName: "previewImage"
      anchors.fill: parent
      anchors.margins: Style.space(12)
      anchors.topMargin: Style.space(46)
      source: preview.source
      fillMode: Image.PreserveAspectFit
      asynchronous: true
      cache: false
      Text {
        anchors.centerIn: parent
        visible: image.status !== Image.Ready
        text: image.status === Image.Loading ? "Loading image…" : "Image preview unavailable"
        color: preview.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }
    }
  }
}
