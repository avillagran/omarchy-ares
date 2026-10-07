pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import qs.Commons

Item {
  id: gallery
  property var images: []
  property bool removalEnabled: true
  property string warningText: ""
  readonly property color foreground: Color.popups.text
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.56)
  signal removeRequested(int index)
  signal previewRequested(string source, string name)

  visible: images.length > 0
  implicitHeight: visible ? cards.height + (warning.visible ? warning.implicitHeight + Style.space(5) : 0) : 0

  ListView {
    id: cards
    objectName: "attachmentList"
    width: parent.width
    height: Style.space(150)
    orientation: ListView.Horizontal
    spacing: Style.space(8)
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    model: gallery.images
    ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AsNeeded }
    WheelHandler {
      target: null
      onWheel: function(event) {
        var delta = event.pixelDelta.x || event.pixelDelta.y
          || event.angleDelta.x / 2 || event.angleDelta.y / 2
        cards.contentX = Math.max(0, Math.min(cards.contentWidth - cards.width, cards.contentX - delta))
        event.accepted = true
      }
    }

    delegate: Rectangle {
      id: card
      required property var modelData
      required property int index
      objectName: "attachmentCard" + index
      width: Style.space(144)
      height: Style.space(138)
      radius: Style.cornerRadius
      color: Qt.rgba(gallery.foreground.r, gallery.foreground.g, gallery.foreground.b, 0.06)
      border.color: Qt.rgba(gallery.foreground.r, gallery.foreground.g, gallery.foreground.b, 0.18)

      Item {
        id: thumbnail
        objectName: "attachmentThumbnail"
        x: Style.space(5); y: Style.space(5)
        width: parent.width - Style.space(10)
        height: Style.space(84)
        clip: true
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: "Preview " + String(card.modelData.name || "Image")
        function preview() { gallery.previewRequested(String(card.modelData.url || ""), String(card.modelData.name || "Image")) }
        Keys.onReturnPressed: preview()
        Keys.onEnterPressed: preview()
        Keys.onSpacePressed: preview()
        Image {
          id: image
          anchors.fill: parent
          source: card.modelData.url || ""
          fillMode: Image.PreserveAspectFit
          asynchronous: true
          cache: false
        }
        Text {
          anchors.centerIn: parent
          visible: image.status !== Image.Ready
          text: image.status === Image.Loading ? "Loading…" : "No preview"
          color: gallery.dim
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
        Rectangle {
          anchors.fill: parent
          color: "transparent"
          border.color: thumbnail.activeFocus ? Color.accent : "transparent"
          radius: Style.space(3)
        }
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        TapHandler {
          acceptedButtons: Qt.LeftButton
          gesturePolicy: TapHandler.ReleaseWithinBounds
          onTapped: thumbnail.preview()
        }
      }
      Text {
        objectName: "attachmentFilename"
        x: Style.space(5); y: thumbnail.y + thumbnail.height + Style.space(5)
        width: parent.width - Style.space(10)
        text: String(card.modelData.name || "Image")
        textFormat: Text.PlainText
        color: gallery.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        elide: Text.ElideMiddle
      }
      Text {
        objectName: "attachmentStatus"
        x: Style.space(5); y: thumbnail.y + thumbnail.height + Style.space(23)
        width: parent.width - Style.space(10)
        text: card.modelData.uploading ? "Saving…" : card.modelData.error ? "Save failed" : card.modelData.artifactId ? "Saved in draft" : "In draft"
        color: card.modelData.error ? Color.urgent : gallery.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
      ToolButton {
        id: removeButton
        objectName: "attachmentRemove"
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: Style.space(3)
        width: Style.space(26); height: width
        enabled: gallery.removalEnabled
        text: "×"
        Accessible.name: "Remove " + String(card.modelData.name || "image")
        ToolTip.visible: hovered
        ToolTip.text: Accessible.name
        contentItem: Text {
          text: removeButton.text
          color: gallery.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          horizontalAlignment: Text.AlignHCenter
          verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
          color: Color.popups.background
          radius: Style.space(4)
          border.color: removeButton.activeFocus || removeButton.hovered ? Color.accent : "transparent"
          opacity: removeButton.enabled ? 0.95 : 0.45
        }
        onClicked: gallery.removeRequested(card.index)
      }
    }
  }
  Text {
    id: warning
    objectName: "attachmentWarning"
    anchors.top: cards.bottom
    anchors.topMargin: Style.space(5)
    width: parent.width
    visible: text !== ""
    text: gallery.warningText
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    color: Color.urgent
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
  }
}
