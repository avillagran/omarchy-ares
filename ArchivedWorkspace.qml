import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "ArchivedWorkspaceModel.js" as ArchiveModel

Rectangle {
  id: archived
  required property var controller
  readonly property var groups: ArchiveModel.groups(controller.archivedRooms, controller.projects)
  property var collapsedProjects: ({})

  color: Color.popups.background
  radius: Style.cornerRadius

  function toggleGroup(projectId) {
    var next = Object.assign({}, collapsedProjects)
    if (next[projectId] === true) delete next[projectId]
    else next[projectId] = true
    collapsedProjects = next
  }

  ColumnLayout {
    anchors.fill: parent
    spacing: Style.space(10)

    RowLayout {
      Layout.fillWidth: true
      Layout.preferredHeight: Style.space(50)
      spacing: Style.space(10)
      Text { text: "󰀼"; color: Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.title }
      ColumnLayout {
        Layout.fillWidth: true
        spacing: 0
        Text { text: "ARCHIVED CHATS"; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.subtitle; font.bold: true }
        Text { text: controller.archivedRooms.length + " recoverable chat" + (controller.archivedRooms.length === 1 ? "" : "s"); color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56); font.family: Style.font.family; font.pixelSize: Style.font.caption }
      }
      ArchiveButton { glyph: "󰁍"; hint: "Back to chats"; onActivated: controller.section = "home" }
    }

    Rectangle { Layout.fillWidth: true; height: 1; color: Color.popups.border }

    ListView {
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: archived.groups.length > 0
      clip: true
      spacing: Style.space(9)
      model: archived.groups
      boundsBehavior: Flickable.StopAtBounds
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      delegate: Item {
        id: groupItem
        required property var modelData
        readonly property bool expanded: archived.collapsedProjects[String(modelData.projectId)] !== true
        width: ListView.view.width
        height: groupColumn.implicitHeight + Style.space(4)

        ColumnLayout {
          id: groupColumn
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.space(5)

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(40)
            radius: Style.space(4)
            color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.11)
            border.color: Color.popups.border
            border.width: 1
            RowLayout {
              anchors.fill: parent
              anchors.margins: Style.space(9)
              spacing: Style.space(7)
              Text { text: groupItem.expanded ? "▾" : "▸"; color: Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.body }
              Text {
                Layout.fillWidth: true
                text: groupItem.modelData.name
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                font.bold: true
              }
              Text {
                text: String(groupItem.modelData.count)
                color: Color.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }
            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: archived.toggleGroup(groupItem.modelData.projectId)
            }
          }

          Repeater {
            model: groupItem.expanded ? groupItem.modelData.rooms : []
            delegate: Rectangle {
              required property var modelData
              Layout.fillWidth: true
              Layout.preferredHeight: Style.space(66)
              radius: Style.cornerRadius
              color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.045)
              border.color: Color.popups.border
              border.width: 1
              RowLayout {
                anchors.fill: parent
                anchors.margins: Style.space(10)
                spacing: Style.space(9)
                Text { text: "󰀼"; color: Color.muted; font.family: Style.font.family; font.pixelSize: Style.font.subtitle }
                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: 1
                  Text { Layout.fillWidth: true; text: modelData.name; textFormat: Text.PlainText; elide: Text.ElideRight; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body; font.bold: true }
                  Text { Layout.fillWidth: true; text: modelData.eventCount + " events"; elide: Text.ElideRight; color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.54); font.family: Style.font.family; font.pixelSize: Style.font.caption }
                }
                ArchiveButton { glyph: "󰑐"; hint: "Restore chat in paused state"; onActivated: controller.restoreRoom(modelData) }
              }
            }
          }
        }
      }
    }

    Text {
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: controller.archivedRooms.length === 0
      text: "Archived chats remain recoverable and keep their complete history."
      horizontalAlignment: Text.AlignHCenter
      color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      verticalAlignment: Text.AlignVCenter
    }
  }

  component ArchiveButton: Rectangle {
    id: archiveButton
    property string glyph: ""
    property string hint: ""
    signal activated()
    implicitWidth: Style.space(32)
    implicitHeight: Style.space(32)
    radius: Style.space(4)
    color: archiveMouse.containsMouse ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18) : "transparent"
    border.color: Color.popups.border
    border.width: 1
    Text { anchors.centerIn: parent; text: parent.glyph; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body }
    ToolTip.visible: archiveMouse.containsMouse
    ToolTip.text: archiveButton.hint
    MouseArea { id: archiveMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.activated() }
  }
}
