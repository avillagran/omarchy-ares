import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Ui
import "ControlSceneModel.js" as SceneModel
import "ControlMapModel.js" as MapModel
import "ControlRoomActions.js" as ActionModel

Item {
  id: control
  required property var controller
  required property var bridge
  property string selectedItemId: ""
  property string selectedRoomId: ""
  property string archiveTargetRoomId: ""
  property string note: ""
  property bool editorOpen: false
  property var roomNotes: ({})
  property string newRoomName: ""
  property int projectIndex: -1
  property string notifyStatus: ""
  readonly property var localProjects: controller.projects.filter(function(project) {
    return project.node === "local" && String(project.path || "").indexOf("hermes://") !== 0
  })
  readonly property var selectedItem: controller.controlItems.find(function(item) {
    return item.id === selectedItemId
  }) || null
  readonly property var selectedRoom: controller.rooms.find(function(room) {
    return room.id === selectedRoomId && room.status === "active" && room.externalArchived !== true
  }) || null
  readonly property var archiveTargetRoom: controller.rooms.find(function(room) {
    return room.id === archiveTargetRoomId
  }) || null
  readonly property var selectedProject: projectIndex >= 0 && projectIndex < localProjects.length
    ? localProjects[projectIndex] : null
  readonly property var scenePeople: SceneModel.build(controller.rooms, controller.controlItems, controller.bots, controller.projects, controller.controlLogs, 12)
  readonly property var mapNodes: MapModel.build(controller.rooms, controller.controlItems, controller.bots, controller.projects)
  readonly property color foreground: Color.popups.text
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.56)
  readonly property color surface: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.065)
  readonly property color stroke: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.2)
  readonly property string fontFamily: Style.font.family

  function selectActionRoom(roomId) {
    var room = controller.rooms.find(function(item) { return item.id === roomId && item.status === "active" && item.externalArchived !== true && item.controlIncluded === true })
    if (!room) return false
    if (control.selectedRoomId !== roomId) {
      control.editorOpen = false
      var notes = Object.assign({}, control.roomNotes)
      if (control.selectedRoomId !== "") notes[control.selectedRoomId] = control.note
      control.roomNotes = notes
      control.note = notes[roomId] || ""
      control.selectedRoomId = roomId
    }
    var pending = controller.controlItems.find(function(item) { return item.roomId === roomId })
    control.selectedItemId = pending ? pending.id : ""
    return true
  }
  function openActionChat(room) {
    if (!room || room.status !== "active") return
    var mapSlots = controller.controlMode === "map" ? projectMap.layout.slots.map(function(slot) {
      return {projectId: slot.project.id, x: slot.x, y: slot.y, width: slot.width, height: slot.height}
    }) : []
    controller.section = "home"
    controller.selectRoom(room)
    controller.openExtended(mapSlots)
  }
  function requestRoomAction(roomId, action) {
    var room = controller.rooms.find(function(item) { return item.id === roomId && item.status === "active" })
    if (!room) return
    if (room.externalArchived === true && action !== "chat") return
    if (room.externalArchived === true) { openActionChat(room); return }
    if (!selectActionRoom(roomId)) return
    if (action === "chat") openActionChat(control.selectedRoom)
    else if (action === "draft") {
      control.editorOpen = true
      Qt.callLater(function() { if (control.editorOpen) decisionNote.forceActiveFocus() })
    }
    else if (action === "archive") control.archiveTargetRoomId = roomId
  }
  function confirmArchive() {
    var room = control.archiveTargetRoom
    control.archiveTargetRoomId = ""
    if (!ActionModel.canArchive(room, controller.controlItems)) {
      control.notifyStatus = "Room changed or is busy; archive cancelled"
      return
    }
    controller.archiveRoom(room)
    control.selectedRoomId = ""
    control.selectedItemId = ""
  }

  onVisibleChanged: {
    if (!visible) control.editorOpen = false
    else controller.refreshControlItems()
  }
  Connections {
    target: control.controller
    function onControlModeChanged() { control.editorOpen = false }
  }
  Timer { interval: 5000; repeat: true; running: control.visible; onTriggered: controller.refreshControlItems() }

  Process {
    id: telegramAlert
    // A generic, user-triggered alert: no prompt, room title, token or private content in argv.
    command: ["hermes", "send", "--to", "telegram", "Ares Control Room: a decision needs your review. Open Control Room on the desktop."]
    onExited: function(code) { control.notifyStatus = code === 0 ? "Telegram alert delivered" : "Telegram delivery failed" }
  }

  Rectangle {
    anchors.fill: parent
    color: Color.popups.background
    radius: Style.cornerRadius

    ColumnLayout {
      anchors.fill: parent
      anchors.margins: Style.space(12)
      spacing: Style.space(8)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)
        Text {
          Layout.fillWidth: true
          text: "CONTROL ROOM"
          color: Color.accent
          font.family: control.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        ControlButton { label: "Text"; selected: controller.controlMode === "text"; onActivated: controller.controlMode = "text" }
        ControlButton { label: "Visual"; selected: controller.controlMode === "visual"; onActivated: controller.controlMode = "visual" }
        ControlButton { label: "Map"; selected: controller.controlMode === "map"; onActivated: controller.controlMode = "map" }
        ControlButton {
          visible: selectedRoom !== null
          label: control.editorOpen ? "Hide editor" : "Show editor"
          onActivated: control.editorOpen = !control.editorOpen
        }
        ControlButton { label: "Refresh"; onActivated: { controller.refreshControlItems(); controller.refreshLayaTraining() } }
        ControlButton { label: "Back"; onActivated: controller.section = "home" }
      }
      Text {
        Layout.fillWidth: true
        text: controller.controlItems.length + " pending · Review and draft here; actions in source rooms still require confirmation"
        color: control.dim
        font.family: control.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
      Text {
        Layout.fillWidth: true
        text: "Laya dataset: " + controller.layaExampleCount + " examples · manual review required"
        color: control.dim
        font.family: control.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
      Rectangle { Layout.fillWidth: true; height: 1; color: control.stroke }

      Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true

        Text {
          anchors.centerIn: parent
          visible: controller.controlMode === "text" && controller.controlItems.length === 0
          text: "No pending work from included rooms"
          color: control.dim
          font.family: control.fontFamily
          font.pixelSize: Style.font.body
        }

        ListView {
          anchors.fill: parent
          visible: controller.controlMode === "text"
          model: visible ? controller.controlItems : []
          spacing: Style.space(3)
          clip: true
          delegate: Rectangle {
            id: line
            required property var modelData
            width: ListView.view.width
            height: Style.space(42)
            radius: Style.space(5)
            color: control.selectedItemId === modelData.id ? control.surface : "transparent"
            border.color: control.selectedItemId === modelData.id ? Color.accent : control.stroke
            RowLayout {
              anchors.fill: parent
              anchors.margins: Style.space(6)
              spacing: Style.space(6)
              Text {
                text: line.modelData.status === "working" || line.modelData.kind === "execution" ? "●" : "!"
                color: line.modelData.status === "working" || line.modelData.kind === "execution" ? Color.accent : Color.urgent
                font.pixelSize: Style.font.body
              }
              ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Text { Layout.fillWidth: true; text: line.modelData.roomName + " · " + line.modelData.projectName; color: control.foreground; font.family: control.fontFamily; font.pixelSize: Style.font.caption; font.bold: true; elide: Text.ElideRight }
                Text { Layout.fillWidth: true; text: line.modelData.title; color: control.dim; font.family: control.fontFamily; font.pixelSize: Style.font.caption; elide: Text.ElideRight }
              }
              Text { text: String(line.modelData.status).toUpperCase(); color: control.dim; font.family: control.fontFamily; font.pixelSize: Style.font.caption }
            }
            MouseArea { anchors.fill: parent; onClicked: {
              control.selectActionRoom(line.modelData.roomId)
              control.selectedItemId = line.modelData.id
            } }
          }
        }

        ControlConstellation {
          anchors.fill: parent
          visible: controller.controlMode === "visual"
          controller: control.controller
          snapshot: control.scenePeople
          onRoomActivated: function(roomId) { control.selectActionRoom(roomId) }
          onRoomAction: function(roomId, action) { control.requestRoomAction(roomId, action) }
        }
        ControlTiles {
          id: projectMap
          anchors.fill: parent
          visible: controller.controlMode === "map"
          controller: control.controller
          bridge: control.bridge
          onRoomActivated: function(roomId) { control.selectActionRoom(roomId) }
          onRoomAction: function(roomId, action) { control.requestRoomAction(roomId, action) }
          onTileOrderRequested: function(ids) { controller.setControlTileOrder(ids) }
        }
      }

      Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: selectedRoom ? Math.min(Style.space(280), Math.max(Style.space(200), control.height * 0.38)) : 0
        Layout.minimumHeight: selectedRoom ? actionFlow.implicitHeight + Style.space(100) : 0
        visible: selectedRoom !== null && control.editorOpen
        radius: Style.cornerRadius
        color: control.surface
        border.color: control.stroke
        ColumnLayout {
          anchors.fill: parent
          anchors.margins: Style.space(7)
          spacing: Style.space(4)
          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(4)
            Text { Layout.fillWidth: true; text: selectedRoom ? selectedRoom.name + (selectedItem ? " · " + selectedItem.kind : " · no pending decision") : ""; color: control.foreground; font.family: control.fontFamily; font.pixelSize: Style.font.caption; font.bold: true; elide: Text.ElideRight }
            ControlButton { objectName: "controlEditorClose"; label: "×"; onActivated: control.editorOpen = false }
          }
          ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            TextArea {
              id: decisionNote
              placeholderText: "Draft a prompt, describe an option, or ask the user…"
              text: control.note
              onTextChanged: control.note = text
              color: control.foreground
              font.family: control.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: TextArea.Wrap
              background: Rectangle { color: Color.popups.background; radius: Style.space(4); border.color: control.stroke }
            }
          }
          Flow {
            id: actionFlow
            Layout.fillWidth: true
            Layout.preferredHeight: implicitHeight
            spacing: Style.space(4)
            ControlButton { label: "Open chat"; onActivated: control.openActionChat(control.selectedRoom) }
            ControlButton { label: "Prepare in chat"; onActivated: controller.stageControlInstruction(control.selectedRoomId, control.note) }
            ControlButton { visible: selectedItem !== null; label: "Record option"; onActivated: controller.recordControlDecision(control.selectedRoomId, "review-option", control.note) }
            ControlButton { visible: selectedItem !== null; label: "Ask user"; onActivated: controller.recordControlDecision(control.selectedRoomId, "ask-user", control.note) }
            ControlButton { label: "Archive"; onActivated: control.archiveTargetRoomId = control.selectedRoomId }
            ControlButton { label: "Exclude chat"; onActivated: if (control.selectedRoom) controller.toggleControlIncluded(control.selectedRoom) }
          }
        }
      }

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(5)
        TextField {
          id: roomNameField
          Layout.fillWidth: true
          placeholderText: "New work-room title (no prompt sent)"
          text: control.newRoomName
          onTextChanged: control.newRoomName = text
          color: control.foreground
          font.family: control.fontFamily
          font.pixelSize: Style.font.caption
        }
        ControlButton {
          label: selectedProject ? selectedProject.name : "Select a code project"
          onActivated: if (control.localProjects.length > 0) control.projectIndex = (control.projectIndex + 1) % control.localProjects.length
        }
        ControlButton {
          label: "Create room"
          onActivated: controller.createControlWorkRoom(selectedProject ? selectedProject.id : "", control.newRoomName,
            control.selectedItem ? control.selectedItem.roomId : "")
        }
        ControlButton { label: "Telegram alert"; onActivated: if (!telegramAlert.running) { control.notifyStatus = "Sending…"; telegramAlert.running = true } }
      }
      Text {
        Layout.fillWidth: true
        visible: controller.errorText !== "" || control.notifyStatus !== ""
        text: controller.errorText || control.notifyStatus
        color: controller.errorText !== "" ? Color.urgent : control.dim
        font.family: control.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }
  }

  Rectangle {
    anchors.fill: parent
    z: 20
    visible: control.archiveTargetRoomId !== ""
    color: Qt.rgba(0, 0, 0, 0.67)
    MouseArea { anchors.fill: parent; onClicked: control.archiveTargetRoomId = "" }
    Rectangle {
      anchors.centerIn: parent
      width: Math.min(parent.width - Style.space(30), Style.space(360))
      implicitHeight: archiveContent.implicitHeight + Style.space(24)
      radius: Style.cornerRadius
      color: Color.popups.background
      border.color: control.stroke
      ColumnLayout {
        id: archiveContent
        anchors { fill: parent; margins: Style.space(12) }
        spacing: Style.space(10)
        Text { Layout.fillWidth: true; text: "Archive this room locally?"; color: control.foreground; font.family: control.fontFamily; font.pixelSize: Style.font.subtitle; font.bold: true }
        Text { Layout.fillWidth: true; text: control.archiveTargetRoom ? control.archiveTargetRoom.name : "Room no longer available"; elide: Text.ElideRight; color: control.foreground; font.family: control.fontFamily; font.pixelSize: Style.font.body }
        Text {
          Layout.fillWidth: true
          text: ActionModel.canArchive(control.archiveTargetRoom, controller.controlItems)
            ? "Recoverable in Archived. This does not stop the Hermes session or its subagents."
            : "Room changed or has ongoing work. Archive is unavailable."
          wrapMode: Text.Wrap; color: control.dim; font.family: control.fontFamily; font.pixelSize: Style.font.caption
        }
        RowLayout {
          Layout.fillWidth: true
          Item { Layout.fillWidth: true }
          ControlButton { label: "Cancel"; onActivated: control.archiveTargetRoomId = "" }
          ControlButton {
            label: "Archive locally"
            enabled: ActionModel.canArchive(control.archiveTargetRoom, controller.controlItems)
            onActivated: control.confirmArchive()
          }
        }
      }
    }
  }

  component ControlButton: Rectangle {
    property string label: ""
    property bool selected: false
    signal activated()
    implicitWidth: Math.max(Style.space(42), labelText.implicitWidth + Style.space(14))
    implicitHeight: Style.space(26)
    opacity: enabled ? 1 : 0.4
    radius: Style.space(5)
    color: selected ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.2) : control.surface
    border.color: selected ? Color.accent : control.stroke
    Text { id: labelText; anchors.centerIn: parent; text: parent.label; color: control.foreground; font.family: control.fontFamily; font.pixelSize: Style.font.caption; font.bold: parent.selected }
    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: parent.activated() }
  }
}
