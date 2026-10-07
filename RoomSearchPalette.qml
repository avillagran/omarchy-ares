pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import qs.Commons
import "RoomSearchModel.js" as Search

FocusScope {
  id: root
  anchors.fill: parent
  // The owner controls lifetime and selection; this component only emits intent.
  property bool opened: false
  property var rooms: []
  property var projects: []
  property string shortcut: "Ctrl+Space"
  property alias query: field.text
  property var results: []
  property int currentIndex: -1
  property string highlightedRoomId: ""
  readonly property var searchIndex: Search.build(rooms, projects)
  readonly property var currentResult: currentIndex >= 0 && currentIndex < results.length ? results[currentIndex] : null
  readonly property color foreground: Color.popups.text
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.55)
  signal dismissed()
  signal roomActivated(var room)
  visible: opened
  enabled: opened
  focus: opened
  z: 1000

  function reveal() {
    if (currentIndex >= 0) list.positionViewAtIndex(currentIndex, ListView.Contain)
  }
  function refresh(reset) {
    var next = Search.filter(searchIndex, field.text)
    results = next
    currentIndex = Search.preserve(next, reset ? "" : highlightedRoomId)
    highlightedRoomId = currentResult ? currentResult.room.id : ""
    Qt.callLater(function() { if (root.opened) root.reveal() })
  }
  function move(delta) {
    currentIndex = Search.move(currentIndex, delta, results.length)
    highlightedRoomId = currentResult ? currentResult.room.id : ""
    reveal()
  }
  function activate() {
    if (currentResult) root.roomActivated(currentResult.room)
  }
  function title(row) { return String(row.room.alias || row.room.name || qsTr("Untitled chat")) }
  function host(row) { return String(row.room.hostName || row.project.nodeName || row.project.node || qsTr("Local")) }
  function activity(row) { return Search.activityLabel(row.room) }
  Keys.onPressed: function(event) { event.accepted = true }
  Keys.onReleased: function(event) { event.accepted = true }
  onSearchIndexChanged: refresh(false)
  onOpenedChanged: {
    if (opened) {
      field.text = ""
      refresh(true)
      Qt.callLater(function() { if (root.opened) field.forceActiveFocus() })
    }
  }
  Component.onCompleted: {
    refresh(true)
    if (opened) Qt.callLater(function() { if (root.opened) field.forceActiveFocus() })
  }

  Rectangle {
    anchors.fill: parent
    color: Color.popups.background
    opacity: 0.65
  }
  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.AllButtons
    onClicked: root.dismissed()
    onWheel: function(wheel) { wheel.accepted = true }
  }
  Rectangle {
    id: card
    objectName: "paletteCard"
    anchors.centerIn: parent
    width: Math.min(Style.space(680), Math.max(0, parent.width - Style.space(24)))
    height: Math.min(Math.max(0, parent.height - Style.space(24)), Style.space(64 + 28 + Math.max(1, Math.min(7, root.results.length)) * 52))
    color: Color.popups.background
    radius: Style.cornerRadius
    border.color: Color.popups.border
    clip: true
    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.AllButtons
      onWheel: function(wheel) { wheel.accepted = true }
    }
    Item {
      id: header
      x: Style.space(12); y: Style.space(12)
      width: parent.width - Style.space(24); height: Style.space(40)
      TextField {
        id: field
        objectName: "paletteInput"
        width: Math.max(0, (parent.width - closeButton.width - Style.space(16)) * 0.54)
        height: parent.height
        color: root.foreground
        placeholderText: qsTr("Search chats…")
        placeholderTextColor: root.dim
        selectedTextColor: root.foreground
        selectionColor: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.35)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        selectByMouse: true
        Accessible.name: qsTr("Search chats by alias or title")
        background: Rectangle {
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.045)
          radius: Style.space(4)
          border.color: field.activeFocus ? Color.accent : Color.popups.border
        }
        onTextChanged: root.refresh(true)
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
            root.move(event.key === Qt.Key_Down ? 1 : -1)
            event.accepted = true
          } else if (event.key === Qt.Key_Escape) {
            root.dismissed()
            event.accepted = true
          } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
            event.accepted = true
          } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            // Do not turn composition confirmation into navigation.
            if (!inputMethodComposing) root.activate()
            event.accepted = true
          }
        }
      }
      Text {
        id: preview
        objectName: "palettePreview"
        anchors.left: field.right; anchors.leftMargin: Style.space(8)
        anchors.right: closeButton.left; anchors.rightMargin: Style.space(8)
        height: parent.height
        text: root.currentResult ? root.title(root.currentResult) + " · " + String(root.currentResult.project.name || "") + " · " + root.host(root.currentResult) : qsTr("No selection")
        textFormat: Text.PlainText
        color: root.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        maximumLineCount: 1
        Accessible.name: text
      }
      Button {
        id: closeButton
        objectName: "paletteClose"
        anchors.right: parent.right
        width: Style.space(28); height: parent.height
        focusPolicy: Qt.NoFocus
        text: "×"
        Accessible.name: qsTr("Close search")
        onClicked: root.dismissed()
        contentItem: Text {
          text: closeButton.text; color: root.dim
          font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
          horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
          radius: Style.space(4)
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, closeButton.hovered ? 0.09 : 0)
        }
      }
    }
    Rectangle {
      x: 1; y: Style.space(63); width: parent.width - 2; height: 1
      color: Color.popups.border
    }
    ListView {
      id: list
      objectName: "paletteResults"
      x: Style.space(6); y: Style.space(64)
      width: parent.width - Style.space(12)
      height: Math.max(0, parent.height - y - Style.space(28))
      model: root.results
      currentIndex: root.currentIndex
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      delegate: Rectangle {
        id: row
        required property var modelData
        required property int index
        width: list.width; height: Style.space(52)
        radius: Style.space(4)
        color: index === root.currentIndex ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18) : "transparent"
        Accessible.role: Accessible.ListItem
        Accessible.name: root.title(modelData) + " · " + root.host(modelData)
        Text {
          x: Style.space(10); y: Style.space(7); width: parent.width - Style.space(24)
          text: root.title(row.modelData)
          textFormat: Text.PlainText; elide: Text.ElideRight
          color: root.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
        }
        Text {
          x: Style.space(10); y: Style.space(29); width: parent.width - Style.space(24)
          text: (row.modelData.room.alias ? String(row.modelData.room.name || "") + " · " : "") + root.host(row.modelData) + " · " + root.activity(row.modelData)
          textFormat: Text.PlainText; elide: Text.ElideRight
          color: root.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption
        }
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        TapHandler {
          acceptedButtons: Qt.LeftButton
          gesturePolicy: TapHandler.ReleaseWithinBounds
          // Emit last: the stable owner may synchronously destroy this delegate.
          onTapped: root.roomActivated(row.modelData.room)
        }
      }
    }
    Text {
      objectName: "paletteEmpty"
      visible: root.results.length === 0
      x: list.x; y: list.y; width: list.width; height: list.height
      text: qsTr("No matching chats")
      color: root.dim; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
    }
    Text {
      x: Style.space(12); width: parent.width - Style.space(24)
      anchors.bottom: parent.bottom; height: Style.space(28)
      text: root.shortcut + " · " + qsTr("↑/↓ select · Enter opens · Esc closes")
      color: root.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption
      elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
    }
  }
}
