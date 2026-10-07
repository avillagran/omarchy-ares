import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import "RoomSearchModel.js" as Search

RowLayout {
  id: root
  property var rooms: []
  property var projects: []
  property bool expanded: false
  property bool paletteMode: false
  property string shortcut: "Ctrl+Space"
  signal paletteRequested()
  property var results: []
  property int currentIndex: -1
  property string highlightedRoomId: ""
  readonly property var searchIndex: Search.build(rooms, projects)
  readonly property color foreground: Color.popups.text
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.52)
  signal roomActivated(var room)
  signal revealCurrent()
  spacing: Style.space(4)
  implicitHeight: Style.space(30)

  function refresh(reset) {
    var next = Search.filter(searchIndex, field.text)
    results = next
    currentIndex = Search.preserve(next, reset ? "" : highlightedRoomId)
    highlightedRoomId = currentIndex < 0 ? "" : next[currentIndex].room.id
    revealCurrent()
  }
  function open() {
    expanded = true
    refresh(true)
    field.forceActiveFocus()
  }
  function close() {
    expanded = false
    field.text = ""
    field.focus = false
  }
  function move(delta) {
    currentIndex = Search.move(currentIndex, delta, results.length)
    highlightedRoomId = currentIndex < 0 ? "" : results[currentIndex].room.id
    revealCurrent()
  }
  function activate() {
    if (currentIndex < 0 || currentIndex >= results.length) return
    var room = results[currentIndex].room
    close()
    roomActivated(room)
  }
  function activity(row) { return Search.activityLabel(row.room) }
  onSearchIndexChanged: refresh(false)
  Component.onCompleted: refresh(true)

  TextField {
    id: field
    objectName: "searchInput"
    visible: root.expanded
    Layout.fillWidth: true
    Layout.minimumWidth: 0
    Layout.preferredHeight: root.implicitHeight
    placeholderText: qsTr("Alias or title…")
    placeholderTextColor: root.dim
    color: root.foreground
    selectedTextColor: root.foreground
    selectionColor: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.35)
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    selectByMouse: true
    leftPadding: Style.space(7)
    rightPadding: Style.space(7)
    Accessible.name: qsTr("Search chats by alias or title")
    background: Rectangle {
      radius: Style.space(3)
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.045)
      border.color: field.activeFocus ? Color.accent : root.dim
      border.width: 1
    }
    onTextChanged: root.refresh(true)
    Keys.onPressed: function(event) {
      if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
        root.move(event.key === Qt.Key_Down ? 1 : -1)
        event.accepted = true
      } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
        // Composition confirmation must not open a room or leak into a terminal.
        if (!inputMethodComposing) root.activate()
        event.accepted = true
      } else if (event.key === Qt.Key_Escape) {
        root.close()
        event.accepted = true
      }
    }
  }
  Button {
    id: toggle
    objectName: "searchToggle"
    Layout.fillWidth: !root.expanded
    Layout.preferredWidth: root.expanded ? Style.space(28) : implicitWidth
    Layout.preferredHeight: root.implicitHeight
    text: root.expanded ? "×" : qsTr("Search chats")
    Accessible.name: root.expanded ? qsTr("Close search") : qsTr("Search chats")
    onClicked: {
      if (root.paletteMode) root.paletteRequested()
      else if (root.expanded) root.close()
      else root.open()
    }
    contentItem: Text {
      text: toggle.text
      color: root.dim
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignHCenter
      verticalAlignment: Text.AlignVCenter
      elide: Text.ElideRight
    }
    background: Rectangle {
      radius: Style.space(3)
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, toggle.hovered ? 0.09 : 0.045)
      border.color: toggle.activeFocus ? Color.accent : "transparent"
    }
    ToolTip.visible: hovered
    ToolTip.delay: 450
    ToolTip.text: (root.paletteMode ? root.shortcut + " · " : "") + qsTr("↑/↓ select · Enter opens · Esc closes")
  }
}
