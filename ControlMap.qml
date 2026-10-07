import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "ControlMapModel.js" as MapModel

Item {
  id: map
  required property var controller
  required property var bridge
  required property var snapshot
  signal roomActivated(string roomId)
  signal roomAction(string roomId, string action)
  signal projectColorRequested(string projectId, string color)
  signal projectPositionRequested(string projectId, int x, int y)
  property string colorProjectId: ""
  property string editColor: ""
  property string colorError: ""
  property real zoom: 1
  property bool initialized: false
  property bool showArchived: false
  property string draggedProjectId: ""
  property real dragDx: 0
  property real dragDy: 0
  readonly property int sourceArchivedCount: controller.rooms.filter(function(node) { return node.externalArchived === true && node.status === "active" && node.controlIncluded === true; }).length
  property bool userNavigated: false
  property string selectedRoomId: ""
  property real activityClock: Date.now()
  Timer { interval: 30000; running: map.visible; repeat: true; onTriggered: map.activityClock = Date.now() }
  readonly property var mapNodes: MapModel.build(controller.rooms, controller.controlItems, controller.bots, controller.projects,
    activityClock, map.showArchived ? controller.archivedRooms : [], map.showArchived)
  readonly property var dragVisibleNodes: map.draggedProjectId === "" ? map.mapNodes : map.mapNodes.map(function(node) {
    return node.projectId === map.draggedProjectId
      ? Object.assign({}, node, {x: node.x + map.dragDx, y: node.y + map.dragDy}) : node
  })
  readonly property var visibleMapNodes: MapModel.visibleNodes(map.dragVisibleNodes, map.zoom, viewport.contentX, viewport.contentY,
    viewport.width, viewport.height, map.selectedRoomId)
  readonly property bool hasWork: MapModel.focusTarget(map.mapNodes) !== null
  readonly property var territories: MapModel.territories(map.mapNodes, map.controller.projects)
  readonly property color ink: Color.popups.text
  readonly property color quiet: Qt.rgba(ink.r, ink.g, ink.b, 0.58)
  readonly property color borderInk: Qt.rgba(ink.r, ink.g, ink.b, 0.2)

  function applyView(view) {
    map.zoom = view.zoom
    viewport.contentX = view.x
    viewport.contentY = view.y
    viewport.returnToBounds()
  }
  function resetView() { map.userNavigated = true; applyView(MapModel.reset(viewport.width, viewport.height, map.territories)) }
  function fitView() { map.userNavigated = true; applyView(MapModel.fit(viewport.width, viewport.height)) }
  function overviewView() { applyView(MapModel.overview(map.territories, viewport.width, viewport.height)) }
  function focusWork() {
    var view = MapModel.focus(map.mapNodes, viewport.width, viewport.height)
    if (view) { map.userNavigated = true; applyView(view) }
  }
  function fitTo(t) {
    map.userNavigated = true
    var view = MapModel.focusBounds(t, viewport.width, viewport.height)
    if (t.roomCount > 10 && !MapModel.showRoomCards(view.zoom)) {
      view.zoom = 0.65
      view.x = MapModel.clamp((t.x + t.width / 2) * view.zoom - viewport.width / 2,
        0, Math.max(0, MapModel.WORLD_WIDTH * view.zoom - viewport.width))
      view.y = MapModel.clamp((t.y + t.height / 2) * view.zoom - viewport.height / 2,
        0, Math.max(0, MapModel.WORLD_HEIGHT * view.zoom - viewport.height))
    }
    applyView(view)
  }
  function zoomAt(factor, x, y) {
    map.userNavigated = true
    applyView(MapModel.zoomAt({zoom: map.zoom, x: viewport.contentX, y: viewport.contentY},
      factor, x, y, viewport.width, viewport.height))
  }
  function beginProjectDrag(projectId) {
    map.draggedProjectId = String(projectId)
    map.dragDx = 0
    map.dragDy = 0
    map.userNavigated = true
  }
  function updateProjectDrag(dx, dy) {
    var area = map.territories.find(function(t) { return t.id === map.draggedProjectId })
    if (!area) return
    map.dragDx = MapModel.clamp(dx, -area.x, MapModel.WORLD_WIDTH - area.width - area.x)
    map.dragDy = MapModel.clamp(dy, -area.y, MapModel.WORLD_HEIGHT - area.height - area.y)
  }
  function finishProjectDrag() {
    var area = map.territories.find(function(t) { return t.id === map.draggedProjectId })
    if (area && (Math.abs(map.dragDx) >= 2 || Math.abs(map.dragDy) >= 2))
      map.projectPositionRequested(area.id, Math.round(area.x + map.dragDx), Math.round(area.y + map.dragDy))
    cancelProjectDrag()
  }
  function cancelProjectDrag() {
    map.draggedProjectId = ""
    map.dragDx = 0
    map.dragDy = 0
  }
  onDragDxChanged: territoryCanvas.requestPaint()
  onDragDyChanged: territoryCanvas.requestPaint()

  ListModel { id: roster; dynamicRoles: true }
  function syncRoster() {
    var desired = map.visibleMapNodes
    var ids = Object.create(null)
    for (var i = 0; i < desired.length; ++i) ids[desired[i].roomId] = true
    for (var old = roster.count - 1; old >= 0; --old)
      if (!ids[roster.get(old).key]) roster.remove(old)
    for (var j = 0; j < desired.length; ++j) {
      var node = desired[j]
      var payload = JSON.stringify(node)
      var at = -1
      for (var n = 0; n < roster.count; ++n)
        if (roster.get(n).key === node.roomId) { at = n; break }
      if (at < 0) roster.append({key: node.roomId, payload: payload})
      else if (roster.get(at).payload !== payload) roster.setProperty(at, "payload", payload)
    }
  }
  onVisibleMapNodesChanged: syncRoster()
  onMapNodesChanged: {
    if (map.selectedRoomId && !map.mapNodes.some(function(node) { return node.roomId === map.selectedRoomId; })) map.selectedRoomId = ""
    if (map.visible && map.initialized && !map.userNavigated && viewport.width > 0 && viewport.height > 0)
      map.overviewView()
  }
  onTerritoriesChanged: territoryCanvas.requestPaint()
  onVisibleChanged: if (visible && viewport.width > 0 && viewport.height > 0 && !map.initialized) {
    map.initialized = true; map.overviewView()
  }
  Component.onCompleted: syncRoster()

  Rectangle { anchors.fill: parent; color: Color.popups.background; radius: Style.cornerRadius; border.color: map.borderInk }
  Flickable {
    id: viewport
    anchors.fill: parent
    anchors.topMargin: Style.space(34)
    anchors.leftMargin: territoryBar.width + Style.space(4)
    clip: true
    interactive: map.draggedProjectId === ""
    boundsBehavior: Flickable.StopAtBounds
    contentWidth: MapModel.WORLD_WIDTH * map.zoom
    contentHeight: MapModel.WORLD_HEIGHT * map.zoom
    onWidthChanged: if (map.visible && !map.initialized && width > 0 && height > 0) { map.initialized = true; map.overviewView() }
    onHeightChanged: if (map.visible && !map.initialized && width > 0 && height > 0) { map.initialized = true; map.overviewView() }
    onMovementStarted: map.userNavigated = true

    Item {
      id: world
      width: MapModel.WORLD_WIDTH
      height: MapModel.WORLD_HEIGHT
      scale: map.zoom
      transformOrigin: Item.TopLeft
      Canvas {
        id: territoryCanvas
        anchors.fill: parent
        onPaint: {
          var ctx = getContext("2d")
          ctx.clearRect(0, 0, width, height)
          for (var i = 0; i < map.territories.length; ++i) {
            var t = map.territories[i]
            var dx = t.id === map.draggedProjectId ? map.dragDx : 0
            var dy = t.id === map.draggedProjectId ? map.dragDy : 0
            var x = t.x + dx, y = t.y + dy
            var r = 18
            ctx.beginPath()
            ctx.moveTo(x + r, y)
            ctx.arcTo(x + t.width, y, x + t.width, y + t.height, r)
            ctx.arcTo(x + t.width, y + t.height, x, y + t.height, r)
            ctx.arcTo(x, y + t.height, x, y, r)
            ctx.arcTo(x, y, x + t.width, y, r)
            ctx.closePath()
            ctx.globalAlpha = 0.12
            ctx.fillStyle = t.color
            ctx.fill()
            ctx.globalAlpha = 0.84
            ctx.lineWidth = 2.5
            ctx.strokeStyle = t.color
            ctx.stroke()
            ctx.globalAlpha = 0.94
            ctx.fillStyle = t.color
            ctx.font = "bold " + Style.font.caption * 1.35 + "px " + Style.font.family
            ctx.fillText(t.name + " · " + t.roomCount + (t.node === "local" ? "" : " · " + t.node), x + 12, y + 20)
            ctx.globalAlpha = 1
          }
        }
      }
      Repeater {
        model: map.territories.filter(function(t) { return t.roomCount > 10 && !MapModel.showRoomCards(map.zoom); })
        Rectangle {
          required property var modelData
          readonly property var area: modelData
          x: area.x + 12 + (area.id === map.draggedProjectId ? map.dragDx : 0)
          y: area.y + area.height / 2 - height / 2 + (area.id === map.draggedProjectId ? map.dragDy : 0)
          width: area.width - 24; height: Math.min(area.height - 80, 110 / map.zoom)
          radius: 12 / map.zoom
          color: Qt.rgba(map.ink.r, map.ink.g, map.ink.b, 0.09)
          border.color: area.color
          Column {
            anchors.centerIn: parent
            width: parent.width - 16 / map.zoom
            spacing: 3 / map.zoom
            Text { width: parent.width; text: area.name; elide: Text.ElideRight; color: map.ink; font.pixelSize: 15 / map.zoom; font.bold: true; horizontalAlignment: Text.AlignHCenter }
            Text { width: parent.width; text: area.roomCount + " rooms · " + area.workingCount + " working · " + area.waitingCount + " waiting"; elide: Text.ElideRight; color: map.ink; font.pixelSize: 12 / map.zoom; horizontalAlignment: Text.AlignHCenter }
            Text { width: parent.width; text: "Click to explore rooms"; color: map.quiet; font.pixelSize: 11 / map.zoom; horizontalAlignment: Text.AlignHCenter }
          }
          MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: map.fitTo(parent.area) }
        }
      }
      Repeater {
        model: map.territories
        Item {
          id: projectDragHandle
          required property var modelData
          x: modelData.x + modelData.width - width - 10
          y: modelData.y + 5 / map.zoom
          width: Math.min(modelData.width * 0.42, 72 / map.zoom)
          height: Math.min(modelData.height * 0.42, 26 / map.zoom)
          z: 8
          Rectangle {
            x: projectDragHandle.modelData.id === map.draggedProjectId ? map.dragDx : 0
            y: projectDragHandle.modelData.id === map.draggedProjectId ? map.dragDy : 0
            width: parent.width
            height: parent.height
            color: Color.popups.background
            border.color: projectDragHandle.modelData.color
            radius: Style.space(4)
            Text {
              anchors.centerIn: parent
              text: "⠿ Drag"
              color: map.ink
              font.family: Style.font.family
              font.pixelSize: Math.min(Style.font.caption, projectDragHandle.width * map.zoom / 5) / map.zoom
            }
          }
          DragHandler {
            target: null
            cursorShape: Qt.OpenHandCursor
            onActiveChanged: {
              if (active) map.beginProjectDrag(projectDragHandle.modelData.id)
              else if (map.draggedProjectId === projectDragHandle.modelData.id) map.finishProjectDrag()
            }
            onActiveTranslationChanged: if (active) map.updateProjectDrag(activeTranslation.x, activeTranslation.y)
            onCanceled: map.cancelProjectDrag()
          }
        }
      }
      Repeater {
        model: roster
        Rectangle {
          id: actor
          required property string key
          required property string payload
          readonly property var node: JSON.parse(payload)
          readonly property color projectTint: node.projectColor
          x: node.x
          y: node.y
          width: MapModel.NODE_WIDTH - 6
          height: MapModel.NODE_HEIGHT - 6
          radius: Style.space(6)
          color: Qt.rgba(projectTint.r, projectTint.g, projectTint.b, 0.12)
          border.color: actor.projectTint
          border.width: node.activity === "idle" ? 1 : 2
          ColumnLayout {
            anchors { fill: parent; margins: Style.space(6) }
            spacing: Style.space(2)
            Text {
              id: roomTitle
              Layout.fillWidth: true
              Layout.rightMargin: Style.space(30)
              text: actor.node.roomName
              textFormat: Text.PlainText
              color: map.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true
              elide: Text.ElideRight
              HoverHandler { id: roomHover }
              ToolTip.visible: roomHover.hovered && roomTitle.truncated
              ToolTip.text: actor.node.roomName
              ToolTip.delay: 450
            }
            Text {
              id: projectTitle
              Layout.fillWidth: true
              text: actor.node.projectName
              textFormat: Text.PlainText
              color: map.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
              elide: Text.ElideRight
              HoverHandler { id: projectHover }
              ToolTip.visible: projectHover.hovered && projectTitle.truncated
              ToolTip.text: actor.node.projectName
              ToolTip.delay: 450
            }
            RowLayout {
              Layout.fillWidth: true
              spacing: Style.space(5)
              BotFace {
                visible: actor.node.botId !== ""
                size: Style.space(27)
                shapeName: actor.node.shape
                colorHex: String(map.controller.botColorForRole(actor.node.colorRole))
                working: actor.node.activity === "working"
                waiting: actor.node.activity === "waiting"
                notificationColor: Color.urgent
              }
              Text {
                Layout.fillWidth: true
                text: actor.node.botName + (actor.node.additionalMembers > 0 ? " +" + actor.node.additionalMembers : "")
                textFormat: Text.PlainText
                color: map.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
              Text {
                text: actor.node.archived ? "ARCHIVED" : actor.node.parentWorking ? "PARENT" : actor.node.activity.toUpperCase()
                color: actor.node.activity === "waiting" ? Color.urgent : actor.node.activity === "working" ? Color.accent : map.quiet
                font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true
              }
            }
          }
          HoverHandler { id: actorHover }
          MapButton {
            anchors { top: parent.top; right: parent.right; margins: Style.space(3) }
            z: 4
            label: "󰈈"
            hint: "Preview chat without opening it"
            onActivated: chatPreview.openRoom(actor.node.roomId, actor.node.roomName)
          }
          Row {
            anchors { bottom: parent.bottom; horizontalCenter: parent.horizontalCenter; bottomMargin: Style.space(4) }
            spacing: Style.space(2)
            visible: !actor.node.archived && (actorHover.hovered || map.selectedRoomId === actor.node.roomId)
            z: 2
            MapButton { label: "Go to chat"; onActivated: map.roomAction(actor.node.roomId, "chat") }
            MapButton { label: "Draft"; onActivated: map.roomAction(actor.node.roomId, "draft") }
            MapButton { label: "Archive"; onActivated: map.roomAction(actor.node.roomId, "archive") }
          }
          MapButton {
            anchors { bottom: parent.bottom; horizontalCenter: parent.horizontalCenter; bottomMargin: Style.space(4) }
            visible: actor.node.archived
            label: actor.node.sourceArchived ? "Open chat" : "Open Archived"
            onActivated: {
              if (actor.node.sourceArchived) map.roomAction(actor.node.roomId, "chat")
              else map.controller.section = "archived"
            }
          }
          TapHandler {
            acceptedButtons: Qt.LeftButton
            onTapped: {
              if (actor.node.archived) {
                if (actor.node.sourceArchived) map.roomAction(actor.node.roomId, "chat")
                else map.controller.section = "archived"
                return
              }
              map.selectedRoomId = actor.node.roomId
              map.roomActivated(actor.node.roomId)
            }
          }
        }
      }
    }
    WheelHandler {
      target: null
      acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
      onWheel: function(event) {
        var delta = event.angleDelta.y !== 0 ? event.angleDelta.y : event.pixelDelta.y
        if (delta !== 0) map.zoomAt(Math.pow(1.0015, delta), event.x, event.y)
        event.accepted = true
      }
    }
    PinchHandler {
      target: null
      property real previousScale: 1
      onActiveChanged: previousScale = 1
      onScaleChanged: {
        if (!active || scale <= 0 || previousScale <= 0) return
        map.zoomAt(scale / previousScale, centroid.position.x, centroid.position.y)
        previousScale = scale
      }
    }
  }
  Row {
    anchors { top: parent.top; right: parent.right; margins: Style.space(5) }
    spacing: Style.space(5)
    MapButton { label: "−"; onActivated: map.zoomAt(1 / 1.25, viewport.width / 2, viewport.height / 2) }
    MapButton { label: "+"; onActivated: map.zoomAt(1.25, viewport.width / 2, viewport.height / 2) }
    MapButton { label: "Reset"; onActivated: map.resetView() }
    MapButton { label: "Fit"; onActivated: map.fitView() }
    MapButton { label: "Focus work"; enabled: map.hasWork; hint: "No working or waiting rooms"; onActivated: map.focusWork() }
    MapButton {
      label: map.showArchived ? "Hide archived" : "Show archived (" + (map.controller.archivedRooms.length + map.sourceArchivedCount) + ")"
      onActivated: map.showArchived = !map.showArchived
    }
  }
  Text {
    anchors { top: parent.top; left: parent.left; margins: Style.space(10) }
    text: map.mapNodes.length + " included rooms · drag to pan · wheel/pinch to zoom"
    color: map.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
  }
  Flickable {
    id: territoryBar
    anchors { left: parent.left; top: parent.top; topMargin: Style.space(30); bottom: parent.bottom; leftMargin: Style.space(4) }
    width: Style.space(180)
    clip: true
    contentHeight: territoryColumn.implicitHeight + Style.space(10)
    Rectangle { anchors.fill: parent; color: Color.popups.background; opacity: 0.72; radius: Style.cornerRadius }
    Column {
      id: territoryColumn
      width: territoryBar.width - Style.space(8)
      x: Style.space(4)
      topPadding: Style.space(4)
      SelectorRow { label: "All projects"; header: true; onActivated: map.overviewView() }
      Repeater {
        model: map.territories.filter(function(t) { return t.kind === "unassigned"; })
        SelectorRow {
          required property var modelData
          width: territoryColumn.width
          label: modelData.name + " · " + modelData.roomCount
          onActivated: map.fitTo(modelData)
        }
      }
      SelectorRow { label: "Projects"; header: true }
      Repeater {
        model: map.territories.filter(function(t) { return t.kind === "project"; })
        SelectorRow {
          id: projectRow
          required property var modelData
          width: territoryColumn.width
          label: modelData.name + " · " + modelData.roomCount + (modelData.node === "local" ? "" : " · " + modelData.node)
          onActivated: map.fitTo(modelData)
          Rectangle {
            anchors { right: parent.right; rightMargin: Style.space(4); verticalCenter: parent.verticalCenter }
            width: Style.space(14); height: Style.space(14)
            radius: Style.space(3)
            color: projectRow.modelData.color
            border.color: map.borderInk
            z: 2
            ToolTip.visible: colorClick.containsMouse
            ToolTip.text: "Set project map color"
            MouseArea {
              id: colorClick
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                map.colorProjectId = projectRow.modelData.id
                map.editColor = projectRow.modelData.color
                map.colorError = ""
                colorInput.forceActiveFocus()
              }
            }
          }
        }
      }
    }
  }
  Text {
    anchors.centerIn: viewport
    visible: map.mapNodes.length === 0
    text: "No included rooms"
    color: map.quiet; font.family: Style.font.family; font.pixelSize: Style.font.body
  }
  MapChatPreview {
    id: chatPreview
    anchors.fill: parent
    z: 30
    bridge: map.bridge
  }
  Rectangle {
    anchors.fill: parent
    z: 10
    visible: map.colorProjectId !== ""
    color: Qt.rgba(0, 0, 0, 0.65)
    MouseArea { anchors.fill: parent; onClicked: map.colorProjectId = "" }
    Rectangle {
      anchors.centerIn: parent
      width: Math.min(parent.width - Style.space(20), Style.space(340))
      height: colorColumn.implicitHeight + Style.space(24)
      color: Color.popups.background
      radius: Style.cornerRadius
      border.color: map.borderInk
      ColumnLayout {
        id: colorColumn
        anchors { fill: parent; margins: Style.space(12) }
        spacing: Style.space(8)
        Text {
          Layout.fillWidth: true
          text: "Project map color"
          color: map.ink; font.family: Style.font.family; font.pixelSize: Style.font.body; font.bold: true
        }
        TextField {
          id: colorInput
          Layout.fillWidth: true
          text: map.editColor
          placeholderText: "#RRGGBB"
          maximumLength: 7
          color: map.ink
          font.family: Style.font.family
          onTextChanged: map.editColor = text
          onAccepted: saveColor.activated()
        }
        Text {
          Layout.fillWidth: true
          visible: map.colorError !== ""
          text: map.colorError
          color: Color.urgent; font.family: Style.font.family; font.pixelSize: Style.font.caption
        }
        RowLayout {
          Layout.fillWidth: true
          MapButton { label: "Reset"; onActivated: { map.projectColorRequested(map.colorProjectId, ""); map.colorProjectId = "" } }
          Item { Layout.fillWidth: true }
          MapButton { label: "Cancel"; onActivated: map.colorProjectId = "" }
          MapButton {
            id: saveColor
            label: "Save"
            onActivated: {
              if (!/^#[0-9a-fA-F]{6}$/.test(map.editColor)) {
                map.colorError = "Enter a six-digit hex color"; return
              }
              map.projectColorRequested(map.colorProjectId, map.editColor.toUpperCase())
              map.colorProjectId = ""
            }
          }
        }
      }
    }
  }
  component MapButton: Rectangle {
    property string label: ""
    property string hint: ""
    signal activated()
    implicitWidth: Math.max(Style.space(40), caption.implicitWidth + Style.space(12))
    implicitHeight: Style.space(24)
    radius: Style.space(4)
    color: Color.popups.background
    opacity: enabled ? 1 : 0.45
    border.color: map.borderInk
    Text { id: caption; anchors.centerIn: parent; text: parent.label; color: map.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption }
    ToolTip.visible: buttonHover.hovered && !enabled && hint !== ""
    ToolTip.text: hint
    HoverHandler { id: buttonHover }
    MouseArea { anchors.fill: parent; enabled: parent.enabled; cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor; onClicked: parent.activated() }
  }
  component SelectorRow: Rectangle {
    property string label: ""
    property bool header: false
    signal activated()
    width: parent ? parent.width : Style.space(170)
    height: Style.space(header ? 24 : 21)
    color: header ? "transparent" : Qt.rgba(map.ink.r, map.ink.g, map.ink.b, 0.05)
    radius: Style.space(3)
    Text {
      id: selectorTitle
      anchors { fill: parent; leftMargin: Style.space(header ? 4 : 14); rightMargin: Style.space(4) }
      text: parent.label
      textFormat: Text.PlainText
      elide: Text.ElideRight
      verticalAlignment: Text.AlignVCenter
      color: parent.header ? map.ink : map.quiet
      font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: parent.header
      HoverHandler { id: selectorHover }
      ToolTip.visible: selectorHover.hovered && selectorTitle.truncated
      ToolTip.text: selectorTitle.text
      ToolTip.delay: 450
    }
    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: parent.activated() }
  }
}
