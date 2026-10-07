import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import "ControlTilesModel.js" as TilesModel
import "ControlMapModel.js" as MapModel

// One spatial map: fixed project slots, continuous camera zoom, progressive detail.
Item {
  id: tiles
  required property var controller
  required property var bridge
  signal roomActivated(string roomId)
  signal roomAction(string roomId, string action)
  signal tileOrderRequested(var ids)

  property bool showArchived: false
  property bool groupsOpen: false
  property string assignProjectId: ""
  property string removeArmedId: ""
  property real lastWheelZoomAt: 0
  property bool overChatList: false
  property real zoom: 1
  property bool mapReady: false
  property var collapsedGroupIds: ({})
  property var draggingSlot: null
  property real dragDx: 0
  property real dragDy: 0
  readonly property int archivedCount: controller.archivedRooms.length + controller.rooms.filter(function(room) {
    return room.externalArchived === true && room.status === "active" && room.controlIncluded === true
  }).length
  readonly property var orderedProjects: TilesModel.order(controller.projects, controller.controlTileOrder)
  readonly property var mapCategories: controller.controlMapCategories || ({groups: [], assignments: ({})})
  readonly property var palette: {
    var byId = Object.create(null), ids = []
    for (var i = 0; i < orderedProjects.length; i++) {
      var project = orderedProjects[i]
      ids.push(project.id)
      byId[project.id] = project
    }
    return MapModel.projectColors(ids, byId)
  }
  readonly property var layout: TilesModel.mapLayout(orderedProjects, viewport.width, viewport.height, mapCategories, collapsedGroupIds)
  readonly property real fitScale: TilesModel.fitZoom(layout, viewport.width, viewport.height)
  readonly property string zoomLevel: TilesModel.zoomLevel(zoom, fitScale)
  readonly property var visibleSlots: TilesModel.visibleTiles(layout, zoom,
    viewport.contentX, viewport.contentY, viewport.width, viewport.height)
  readonly property color ink: Color.popups.text
  readonly property color quiet: Color.muted
  readonly property color stroke: Qt.rgba(ink.r, ink.g, ink.b, 0.18)
  readonly property var hoveredSlot: draggingSlot ? dropTarget() : null

  Component.onCompleted: Qt.callLater(fitMap)
  onFitScaleChanged: {
    if (!mapReady) Qt.callLater(fitMap)
  }


  function fitMap() {
    if (viewport.width < 1 || viewport.height < 1 || orderedProjects.length === 0) return
    zoom = fitScale
    viewport.contentX = 0
    viewport.contentY = 0
    mapReady = true
  }
  function zoomAt(factor, x, y) {
    if (!mapReady || !(factor > 0)) return
    var view = TilesModel.zoomAt({zoom:zoom, x:viewport.contentX, y:viewport.contentY},
      factor, x, y, layout, viewport.width, viewport.height)
    zoom = view.zoom
    viewport.contentX = view.x
    viewport.contentY = view.y
  }
  function wheelZoom(event, sourceItem) {
    var delta = event.pixelDelta.y !== 0 ? event.pixelDelta.y : event.angleDelta.y
    if (delta === 0) { event.accepted = false; return }
    var cursor = sourceItem.mapToItem(viewport, event.x, event.y)
    lastWheelZoomAt = Date.now()
    zoomAt(Math.pow(1.0015, delta), cursor.x, cursor.y)
    event.accepted = true
  }
  function wheelGestureActive() { return Date.now() - lastWheelZoomAt < 650 }
  function focusSlot(slot) {
    if (!slot) return
    var targetZoom = Math.max(0.9, Math.min(4, zoom * 2))
    zoom = targetZoom
    viewport.contentX = Math.max(0, Math.min(Math.max(0, layout.width * zoom - viewport.width),
      (slot.x + slot.width / 2) * zoom - viewport.width / 2))
    viewport.contentY = Math.max(0, Math.min(Math.max(0, layout.height * zoom - viewport.height),
      (slot.y + slot.height / 2) * zoom - viewport.height / 2))
  }
  function orderedIds() { return orderedProjects.map(function(p) { return p.id }) }
  function moveTile(id, step) {
    var current = layout.slots.find(function(slot) { return slot.project.id === id })
    if (!current) return
    var members = layout.slots.filter(function(slot) { return slot.groupId === current.groupId })
    var at = members.findIndex(function(slot) { return slot.project.id === id })
    if (at + step < 0 || at + step >= members.length) return
    tileOrderRequested(TilesModel.swap(orderedIds(), id, members[at + step].project.id))
  }
  function beginDrag(slot) { draggingSlot = slot; dragDx = 0; dragDy = 0 }
  function updateDrag(dx, dy) { dragDx = dx / zoom; dragDy = dy / zoom }
  function dropTarget() {
    if (!draggingSlot) return null
    return TilesModel.slotAt(layout, draggingSlot.x + draggingSlot.width / 2 + dragDx,
      draggingSlot.y + 20 / zoom + dragDy)
  }
  function dropGroup() {
    if (!draggingSlot) return null
    var x = draggingSlot.x + draggingSlot.width / 2 + dragDx
    var y = draggingSlot.y + 20 / zoom + dragDy
    return layout.groups.find(function(group) {
      return x >= group.x && x < group.x + group.width && y >= group.y && y < group.y + group.height
    }) || null
  }
  function finishDrag() {
    if (!draggingSlot) return
    var target = dropTarget()
    var group = dropGroup()
    if (group && group.id !== draggingSlot.groupId) {
      assignProjectToGroup(draggingSlot.project.id, group.id)
    } else if (target && target.groupId === draggingSlot.groupId && target.index !== draggingSlot.index) {
      tileOrderRequested(TilesModel.swap(orderedIds(), draggingSlot.project.id, target.project.id))
    }
    draggingSlot = null
    dragDx = 0
    dragDy = 0
  }
  function worldFont(pixels) { return Math.min(48, Math.max(3, pixels / Math.max(0.1, zoom))) }
  function categoriesSnapshot() {
    return {groups:(mapCategories.groups || []).map(function(group) {
      return {id:group.id, name:group.name, color:group.color}
    }), assignments:Object.assign({}, mapCategories.assignments || {})}
  }
  function addCategory(name) {
    var clean = String(name || "").trim()
    if (!clean || clean.length > 64 || controller.categoriesSaving) return
    var next = categoriesSnapshot()
    var colors = ["#65a5e0", "#e09665", "#81bf8c", "#b890d7", "#dfb454", "#6bbfbb"]
    next.groups.push({id:"cat-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8),
      name:clean, color:colors[next.groups.length % colors.length]})
    controller.setControlMapCategories(next)
  }
  function renameCategory(id, name) {
    var clean = String(name || "").trim()
    if (!clean || clean.length > 64 || controller.categoriesSaving) return
    var next = categoriesSnapshot()
    next.groups.forEach(function(group) { if (group.id === id) group.name = clean })
    controller.setControlMapCategories(next)
  }
  function removeCategory(id) {
    if (controller.categoriesSaving) return
    var next = categoriesSnapshot()
    next.groups = next.groups.filter(function(group) { return group.id !== id })
    for (var projectId in next.assignments)
      if (next.assignments[projectId] === id) delete next.assignments[projectId]
    controller.setControlMapCategories(next)
  }
  function assignCategory(projectId) { assignProjectId = projectId; groupsOpen = false }
  function assignProjectToGroup(projectId, groupId) {
    if (!projectId || controller.categoriesSaving) return
    var next = categoriesSnapshot()
    if (groupId) next.assignments[projectId] = groupId
    else delete next.assignments[projectId]
    controller.setControlMapCategories(next)
  }
  function applyAssignment(groupId) {
    if (!assignProjectId || controller.categoriesSaving) return
    assignProjectToGroup(assignProjectId, groupId)
    assignProjectId = ""
  }
  function toggleGroup(groupId) {
    var next = Object.assign({}, collapsedGroupIds)
    if (next[groupId] === true) delete next[groupId]
    else next[groupId] = true
    collapsedGroupIds = next
  }

  Rectangle { anchors.fill: parent; color: Color.popups.background; radius: Style.cornerRadius; border.color: tiles.stroke }
  RowLayout {
    id: toolbar
    anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.space(8) }
    height: Style.space(30)
    spacing: Style.space(6)
    Text {
      text: "Map · " + tiles.orderedProjects.length + " projects"
      color: tiles.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true
      visible: viewport.width > 500
    }
    Text {
      text: "Map"
      color: tiles.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true
      visible: viewport.width <= 500
    }
    Item { Layout.fillWidth: true }
    TileButton { label: "−"; enabled: tiles.zoom > tiles.fitScale + 0.0001; onActivated: tiles.zoomAt(1 / 1.35, viewport.width / 2, viewport.height / 2) }
    Text { text: Math.round(tiles.zoom * 100) + "%"; color: tiles.quiet; font.pixelSize: Style.font.caption; visible: viewport.width > 500 }
    TileButton { label: "+"; enabled: tiles.zoom < 4; onActivated: tiles.zoomAt(1.35, viewport.width / 2, viewport.height / 2) }
    TileButton { label: "Fit"; onActivated: tiles.fitMap() }
    TileButton { label: "Groups"; onActivated: { tiles.assignProjectId = ""; tiles.groupsOpen = true } }
    TileButton {
      label: tiles.showArchived ? "Hide archived" : (viewport.width < 540 ? "Archive (" : "Show archived (") + tiles.archivedCount + ")"
      onActivated: tiles.showArchived = !tiles.showArchived
    }
  }
  Flickable {
    id: viewport
    anchors { left: parent.left; right: parent.right; top: toolbar.bottom; bottom: parent.bottom; margins: Style.space(8) }
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    contentWidth: tiles.layout.width * tiles.zoom
    contentHeight: tiles.layout.height * tiles.zoom
    Item {
      id: world
      width: tiles.layout.width
      height: tiles.layout.height
      x: Math.max(0, (viewport.width - viewport.contentWidth) / 2)
      y: Math.max(0, (viewport.height - viewport.contentHeight) / 2)
      scale: tiles.zoom
      transformOrigin: Item.TopLeft
      Repeater {
        model: tiles.layout.columns + 1
        Rectangle {
          visible: tiles.layout.groups.length === 0
          x: index * TilesModel.CELL_WIDTH
          width: 1; height: world.height
          color: Qt.rgba(tiles.ink.r, tiles.ink.g, tiles.ink.b, 0.055)
        }
      }
      Repeater {
        model: tiles.layout.rows + 1
        Rectangle {
          visible: tiles.layout.groups.length === 0
          y: index * TilesModel.CELL_HEIGHT
          width: world.width; height: 1
          color: Qt.rgba(tiles.ink.r, tiles.ink.g, tiles.ink.b, 0.055)
        }
      }
      Repeater {
        model: tiles.layout.groups
        Rectangle {
          required property var modelData
          readonly property color groupTint: modelData.color || "#808080"
          x: modelData.x; y: modelData.y
          width: modelData.width; height: modelData.height
          radius: 12; border.color: groupTint; border.width: 2
          color: Qt.rgba(groupTint.r, groupTint.g, groupTint.b, 0.08)
          RowLayout {
            anchors { top: parent.top; left: parent.left; right: parent.right; margins: 12 }
            height: 38
            Text {
              Layout.fillWidth: true
              text: (modelData.collapsed ? "▸  " : "▾  ") + modelData.name + " · " + modelData.count
              textFormat: Text.PlainText
              color: tiles.ink; font.family: Style.font.family
              font.pixelSize: tiles.worldFont(Style.font.body); font.bold: true
              verticalAlignment: Text.AlignVCenter
            }
            TileButton { label: modelData.collapsed ? "Expand" : "Collapse"; inWorld: true; onActivated: tiles.toggleGroup(modelData.id) }
          }
        }
      }
      Repeater {
        model: tiles.visibleSlots
        Rectangle {
          id: projectTile
          required property var modelData
          readonly property var slot: modelData
          readonly property var project: slot.project
          readonly property color tint: tiles.palette[project.id] || Color.accent
          readonly property var roomRows: TilesModel.projectRooms(tiles.controller.rooms, tiles.controller.archivedRooms,
            project.id, tiles.showArchived)
          readonly property bool isDragged: tiles.draggingSlot && tiles.draggingSlot.project.id === project.id
          readonly property bool isDropTarget: tiles.hoveredSlot && tiles.hoveredSlot.project.id === project.id && !isDragged
          x: slot.x + 6 + (isDragged ? tiles.dragDx : 0)
          y: slot.y + 6 + (isDragged ? tiles.dragDy : 0)
          width: slot.width - 12
          height: slot.height - 12
          z: isDragged ? 10 : 1
          radius: 8
          clip: true
          color: tiles.zoomLevel === "overview" ? Qt.rgba(tint.r, tint.g, tint.b, 0.18) : Color.popups.background
          border.color: isDragged || isDropTarget ? Color.accent : tint
          border.width: isDragged || isDropTarget ? 3 : 1
          Rectangle {
            id: tileHeader
            anchors { top: parent.top; left: parent.left; right: parent.right }
            height: Math.min(projectTile.height * 0.48, Math.max(18, 38 / Math.max(0.1, tiles.zoom)))
            color: Qt.rgba(projectTile.tint.r, projectTile.tint.g, projectTile.tint.b, 0.22)
            RowLayout {
              anchors { fill: parent; leftMargin: 6; rightMargin: 5 }
              spacing: 4
              Text {
                Layout.fillWidth: true
                text: "⠿  " + (projectTile.project.path && /^peer:\/\/[^/]+\/[^/]+$/.test(projectTile.project.path)
                  ? "Unassigned / Home" : projectTile.project.name)
                textFormat: Text.PlainText
                color: tiles.ink; font.family: Style.font.family
                font.pixelSize: tiles.worldFont(Style.font.caption); font.bold: true
                elide: Text.ElideRight
              }
              TileButton { label: "←"; inWorld: true; visible: tiles.zoomLevel !== "overview"; onActivated: tiles.moveTile(projectTile.project.id, -1) }
              TileButton { label: "→"; inWorld: true; visible: tiles.zoomLevel !== "overview"; onActivated: tiles.moveTile(projectTile.project.id, 1) }
              TileButton { label: "Group"; inWorld: true; visible: tiles.zoomLevel !== "overview"; onActivated: tiles.assignCategory(projectTile.project.id) }
            }
            DragHandler {
              target: null
              cursorShape: Qt.OpenHandCursor
              onActiveChanged: {
                if (active) tiles.beginDrag(projectTile.slot)
                else if (tiles.draggingSlot && tiles.draggingSlot.project.id === projectTile.project.id) tiles.finishDrag()
              }
              onActiveTranslationChanged: if (active) tiles.updateDrag(activeTranslation.x, activeTranslation.y)
              onCanceled: { tiles.draggingSlot = null; tiles.dragDx = 0; tiles.dragDy = 0 }
            }
            WheelHandler {
              target: null
              acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
              onWheel: function(event) { tiles.wheelZoom(event, tileHeader) }
            }
          }
          Text {
            id: projectLocation
            anchors { top: tileHeader.bottom; left: parent.left; right: parent.right; leftMargin: 8; rightMargin: 8 }
            visible: tiles.zoomLevel === "detail" && !!projectTile.project.path &&
              !/^peer:\/\/[^/]+\/[^/]+$/.test(projectTile.project.path)
            height: visible ? Math.max(16, 27 / Math.max(0.1, tiles.zoom)) : 0
            verticalAlignment: Text.AlignVCenter
            text: projectTile.project.path || ""
            textFormat: Text.PlainText
            elide: Text.ElideMiddle
            color: tiles.quiet
            font.pixelSize: tiles.worldFont(Style.font.caption * 0.85)
          }
          Item {
            anchors { top: tileHeader.bottom; bottom: parent.bottom; left: parent.left; right: parent.right }
            visible: tiles.zoomLevel === "overview"
            Text {
              anchors.centerIn: parent
              text: projectTile.roomRows.length + " chats"
              textFormat: Text.PlainText
              color: tiles.ink; font.family: Style.font.family
              font.pixelSize: tiles.worldFont(Style.font.caption); font.bold: true
            }
            HoverHandler { cursorShape: Qt.PointingHandCursor }
            TapHandler {
              acceptedButtons: Qt.LeftButton
              onTapped: tiles.focusSlot(projectTile.slot)
            }
            DragHandler {
              target: null
              enabled: tiles.zoomLevel === "overview"
              cursorShape: Qt.OpenHandCursor
              onActiveChanged: {
                if (active) tiles.beginDrag(projectTile.slot)
                else if (tiles.draggingSlot && tiles.draggingSlot.project.id === projectTile.project.id) tiles.finishDrag()
              }
              onActiveTranslationChanged: if (active) tiles.updateDrag(activeTranslation.x, activeTranslation.y)
              onCanceled: { tiles.draggingSlot = null; tiles.dragDx = 0; tiles.dragDy = 0 }
            }
          }
          ListView {
            id: chatList
            visible: tiles.zoomLevel !== "overview"
            anchors { top: projectLocation.visible ? projectLocation.bottom : tileHeader.bottom;
              bottom: parent.bottom; left: parent.left; right: parent.right; margins: 5 }
            clip: true
            spacing: 3 / Math.max(0.1, tiles.zoom)
            boundsBehavior: Flickable.StopAtBounds
            model: visible ? projectTile.roomRows : []
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            delegate: Rectangle {
              id: chatEntry
              required property var modelData
              width: chatList.width
              height: (tiles.zoomLevel === "detail" ? 55 : 39) / Math.max(0.1, tiles.zoom)
              radius: 4
              color: Qt.rgba(tiles.ink.r, tiles.ink.g, tiles.ink.b, 0.06)
              readonly property bool archived: modelData.externalArchived === true || modelData.status !== "active"
              property bool archiveArmed: false
              Timer { id: archiveConfirmTimeout; interval: 3500; onTriggered: chatEntry.archiveArmed = false }
              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                  if (chatEntry.modelData.status !== "active") tiles.controller.section = "archived"
                  else { tiles.roomActivated(chatEntry.modelData.id); tiles.roomAction(chatEntry.modelData.id, "chat") }
                }
              }
              RowLayout {
                anchors { fill: parent; leftMargin: 6 / Math.max(0.1, tiles.zoom); rightMargin: 5 / Math.max(0.1, tiles.zoom) }
                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: 0
                  Text {
                    Layout.fillWidth: true
                    text: chatEntry.modelData.name || "Chat"
                    textFormat: Text.PlainText
                    color: tiles.ink; font.family: Style.font.family
                    font.pixelSize: tiles.worldFont(Style.font.caption)
                    elide: Text.ElideRight
                  }
                  Text {
                    Layout.fillWidth: true
                    visible: tiles.zoomLevel === "detail"
                    text: String(chatEntry.modelData.source || "room") + " · " +
                      Number(chatEntry.modelData.eventCount || 0) + " events" +
                      (chatEntry.modelData.externalUpdatedAt
                        ? " · " + Qt.formatDateTime(new Date(chatEntry.modelData.externalUpdatedAt * 1000), "MMM d, yyyy h:mm AP")
                        : "") + (chatEntry.archived ? " · ARCHIVED" : "")
                    textFormat: Text.PlainText
                    color: tiles.quiet; font.pixelSize: tiles.worldFont(Style.font.caption * 0.9)
                    elide: Text.ElideRight
                  }
                }
                Text {
                  visible: chatEntry.archived && tiles.zoomLevel !== "detail"
                  text: "ARCHIVED"; color: tiles.quiet; font.pixelSize: tiles.worldFont(Style.font.caption * 0.8)
                }
                TileButton {
                  label: "󰈈"; inWorld: true
                  onActivated: chatPreview.openRoom(chatEntry.modelData.id, chatEntry.modelData.name)
                }
                TileButton {
                  label: chatEntry.archiveArmed ? "Confirm" : "Archive"
                  inWorld: true
                  visible: !chatEntry.archived
                  onActivated: {
                    if (!chatEntry.archiveArmed) {
                      chatEntry.archiveArmed = true
                      archiveConfirmTimeout.restart()
                      return
                    }
                    archiveConfirmTimeout.stop()
                    chatEntry.archiveArmed = false
                    tiles.controller.archiveRoom(chatEntry.modelData)
                  }
                }
              }
            }
          }
          WheelHandler {
            target: null
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onWheel: function(event) {
              if (tiles.wheelGestureActive() || (event.modifiers & Qt.ControlModifier))
                tiles.wheelZoom(event, chatList)
              else event.accepted = false
            }
          }
          HoverHandler { target: null; onHoveredChanged: tiles.overChatList = hovered }
        }
      }
    }
    WheelHandler {
      target: null
      acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
      onWheel: function(event) {
        if (tiles.overChatList && !tiles.wheelGestureActive() && !(event.modifiers & Qt.ControlModifier)) {
          event.accepted = false
          return
        }
        tiles.wheelZoom(event, viewport)
      }
    }
    PinchHandler {
      target: null
      property real previousScale: 1
      onActiveChanged: previousScale = 1
      onScaleChanged: {
        if (!active || scale <= 0 || previousScale <= 0) return
        tiles.zoomAt(scale / previousScale, centroid.position.x, centroid.position.y)
        previousScale = scale
      }
    }
  }
  Text {
    anchors.centerIn: viewport
    visible: tiles.orderedProjects.length === 0
    text: "No projects"
    color: tiles.quiet; font.family: Style.font.family; font.pixelSize: Style.font.body
  }
  Rectangle {
    anchors.fill: parent
    visible: tiles.groupsOpen || tiles.assignProjectId !== ""
    color: Qt.rgba(0, 0, 0, 0.52)
    z: 35
    MouseArea { anchors.fill: parent; onClicked: { tiles.groupsOpen = false; tiles.assignProjectId = "" } }
    Rectangle {
      id: categoryDialog
      anchors.centerIn: parent
      width: Math.min(parent.width - 24, 420)
      height: Math.min(parent.height - 24, 480)
      radius: Style.cornerRadius
      color: Color.popups.background
      border.color: tiles.stroke
      MouseArea { anchors.fill: parent; onClicked: mouse.accepted = true }
      ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.space(12)
        spacing: Style.space(8)
        RowLayout {
          Layout.fillWidth: true
          Text {
            Layout.fillWidth: true
            text: tiles.groupsOpen ? "Map categories" : "Assign project category"
            color: tiles.ink; font.family: Style.font.family; font.pixelSize: Style.font.body; font.bold: true
          }
          TileButton { label: "×"; onActivated: { tiles.groupsOpen = false; tiles.assignProjectId = "" } }
        }
        Text {
          Layout.fillWidth: true
          visible: tiles.groupsOpen
          text: "Create categories, then assign projects with Group on a tile."
          wrapMode: Text.WordWrap
          color: tiles.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
        }
        RowLayout {
          Layout.fillWidth: true
          visible: tiles.groupsOpen
          TextField {
            id: newCategoryName
            Layout.fillWidth: true
            placeholderText: "New category"
            color: tiles.ink; font.family: Style.font.family
            maximumLength: 64
            background: Rectangle { color: Color.popups.background; border.color: tiles.stroke; radius: 4 }
            onAccepted: { tiles.addCategory(text); text = "" }
          }
          TileButton { label: "Add"; enabled: !tiles.controller.categoriesSaving; onActivated: { tiles.addCategory(newCategoryName.text); newCategoryName.text = "" } }
        }
        TileButton {
          Layout.fillWidth: true
          visible: !tiles.groupsOpen
          label: "Unassigned"
          enabled: !tiles.controller.categoriesSaving
          onActivated: tiles.applyAssignment("")
        }
        Item {
          Layout.fillWidth: true; Layout.fillHeight: true
          ListView {
            anchors.fill: parent
            clip: true
            visible: tiles.groupsOpen
            spacing: 6
            model: tiles.mapCategories.groups || []
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
              required property var modelData
              width: ListView.view.width
              height: 38
              color: "transparent"
              RowLayout {
                anchors.fill: parent
                spacing: 5
                Rectangle { width: 12; height: 12; radius: 6; color: modelData.color || tiles.quiet }
                TextField {
                  id: categoryName
                  Layout.fillWidth: true
                  text: modelData.name
                  color: tiles.ink; font.family: Style.font.family
                  maximumLength: 64
                  background: Rectangle { color: Color.popups.background; border.color: tiles.stroke; radius: 4 }
                  onAccepted: tiles.renameCategory(modelData.id, text)
                }
                TileButton { label: "Save"; enabled: !tiles.controller.categoriesSaving; onActivated: tiles.renameCategory(modelData.id, categoryName.text) }
                TileButton {
                  label: tiles.removeArmedId === modelData.id ? "Confirm" : "Delete"
                  enabled: !tiles.controller.categoriesSaving
                  onActivated: {
                    if (tiles.removeArmedId !== modelData.id) { tiles.removeArmedId = modelData.id; removeCategoryTimer.restart(); return }
                    removeCategoryTimer.stop(); tiles.removeArmedId = ""; tiles.removeCategory(modelData.id)
                  }
                }
              }
            }
          }
          ListView {
            anchors.fill: parent
            clip: true
            visible: !tiles.groupsOpen
            spacing: 6
            model: tiles.mapCategories.groups || []
            ScrollBar.vertical: ScrollBar {}
            delegate: TileButton {
              required property var modelData
              width: ListView.view.width
              label: modelData.name
              enabled: !tiles.controller.categoriesSaving
              onActivated: tiles.applyAssignment(modelData.id)
            }
          }
        }
      }
    }
    Timer { id: removeCategoryTimer; interval: 3500; onTriggered: tiles.removeArmedId = "" }
  }
  MapChatPreview { id: chatPreview; anchors.fill: parent; z: 30; bridge: tiles.bridge }

  component TileButton: Rectangle {
    property string label: ""
    property bool inWorld: false
    signal activated()
    implicitWidth: inWorld
      ? Math.max(Style.space(25) / Math.max(0.1, tiles.zoom),
          buttonText.implicitWidth + Style.space(10) / Math.max(0.1, tiles.zoom))
      : Math.max(Style.space(25), buttonText.implicitWidth + Style.space(10))
    implicitHeight: Style.space(23) / (inWorld ? Math.max(0.1, tiles.zoom) : 1)
    radius: Style.space(4) / (inWorld ? Math.max(0.1, tiles.zoom) : 1)
    color: Color.popups.background
    border.color: tiles.stroke
    opacity: enabled ? 1 : 0.45
    Text {
      id: buttonText
      anchors.centerIn: parent
      text: parent.label
      color: tiles.ink; font.family: Style.font.family
      font.pixelSize: inWorld ? tiles.worldFont(Style.font.caption) : Style.font.caption
    }
    MouseArea { anchors.fill: parent; enabled: parent.enabled; cursorShape: Qt.PointingHandCursor; onClicked: parent.activated() }
  }
}
