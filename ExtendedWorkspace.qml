import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Ui
import "TerminalPreviewCache.js" as TerminalPreviewCache
import "ExtendedStackModel.js" as ExtendedStackModel

Item {
  id: extended

  required property var controller
  required property var bridge

  readonly property color foreground: Color.popups.text
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.56)
  readonly property color faint: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.09)
  readonly property color divider: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.18)
  readonly property color surface: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.045)
  readonly property color selectedSurface: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.14)
  readonly property var currentRoom: controller.extendedRoom
  readonly property bool currentNativeGroup: !!currentRoom && currentRoom.source === "hermes-native-group"
  property bool terminalPromptConfirmOpen: false
  property string terminalPromptRoomId: ""
  property bool expandedEditorOpen: false
  property string expandedEditorRoomId: ""
  property bool pastePending: false
  property string pasteRoomId: ""
  property string pasteTarget: "compact"
  readonly property bool currentTerminalInteractive: controller.extendedViewMode === "grown"
    ? masterTile.terminalInteractive : (controller.extendedViewMode === "focus"
      ? focusTranscript.terminalInteractive
      : false)
  readonly property bool currentTerminalOwnsInput: controller.extendedViewMode === "grown"
    ? masterTile.terminalOwnsInput : (controller.extendedViewMode === "focus"
      ? focusTranscript.terminalOwnsInput
      : false)
  onCurrentRoomChanged: {
    if (!currentRoom || currentRoom.id !== terminalPromptRoomId) terminalPromptConfirmOpen = false
    if (!currentRoom || currentRoom.id !== expandedEditorRoomId) expandedEditorOpen = false
  }

  ImagePreview {
    id: attachmentPreview
    objectName: "composerImagePreview"
    anchors.fill: parent
    attachments: controller.pendingImages
    contextKey: controller.selectedRoomId + ":" + controller.extendedViewMode + ":" + (extended.currentRoom ? extended.currentRoom.id : "")
      + ":" + extended.expandedEditorOpen + ":" + controller.editMode
    viewActive: extended.visible && extended.enabled && controller.opened
  }

  function submitEditPrompt() {
    if (!currentRoom || currentRoom.id !== controller.selectedRoomId) return
    expandedEditorOpen = false
    if (currentTerminalOwnsInput && !currentTerminalInteractive) {
      controller.errorText = "Terminal is not ready. Retry or choose a terminal; draft was not sent."
      return
    }
    if (!currentTerminalOwnsInput) {
      controller.appendLocalEvent()
      return
    }
    if (controller.pendingImages.length > 0) {
      controller.appendLocalEvent(true, true)
      return
    }
    if (controller.roomText.trim() === "") return
    if (controller.extendedViewMode === "overview") controller.growExtendedRoom(currentRoom)
    terminalPromptRoomId = currentRoom.id
    terminalPromptConfirmOpen = true
  }
  function requestPaste(target) {
    if (expandedPasteProc.running) return
    pasteRoomId = controller.selectedRoomId
    pasteTarget = target
    pastePending = true
    expandedPasteProc.running = true
  }
  Process {
    id: expandedPasteProc
    command: ["bash", "-c",
      'DIR="$HOME/.cache/omarchy-ares"; mkdir -p "$DIR"; '
      + 'T=$(wl-paste --list-types 2>/dev/null | grep -im1 "^image/"); '
      + 'if [ -n "$T" ]; then F="$DIR/paste-$(date +%s%3N).png"; wl-paste --type "$T" > "$F" && echo "ARES_PASTE:$F"; fi']
    stdout: SplitParser {
      onRead: function(line) {
        if (line.indexOf("ARES_PASTE:") !== 0) return
        controller.addPendingImage(line.substring("ARES_PASTE:".length), extended.pasteRoomId)
        extended.pastePending = false
      }
    }
    onExited: function(code) {
      if (!extended.pastePending) return
      extended.pastePending = false
      if (controller.selectedRoomId !== extended.pasteRoomId) {
        controller.errorText = "Room changed before paste; paste again in the original room"
        return
      }
      if (extended.pasteTarget === "modal") expandedArea.paste()
      else expandedInput.paste()
    }
  }
  property var overviewRoomIds: []
  property var roomPreviewCache: ({ rows: ({}), order: [] })
  property var previewRequests: ({})
  property string dragTargetRoomId: ""
  property var stackRoomIds: []
  readonly property var stackCandidates: ExtendedStackModel.stack(currentRoom,
    controller.activeRooms, overviewRoomIds, controller.extendedMapSlots)
  onStackCandidatesChanged: {
    if (JSON.stringify(stackCandidates) !== JSON.stringify(stackRoomIds))
      stackRoomIds = stackCandidates
  }

  // Order belongs to this Ares view, not to Hyprland or a backend preference.
  // Keep the selected master in its slot while reordering the filtered stack.
  function moveRoomBefore(sourceId, targetId) {
    var order = overviewRoomIds.slice()
    var sourceIndex = order.indexOf(sourceId)
    var targetIndex = order.indexOf(targetId)
    if (sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) return
    order.splice(sourceIndex, 1)
    order.splice(order.indexOf(targetId), 0, sourceId)
    overviewRoomIds = order
  }

  // Keep PTY delegates alive when only room activity changes between polls.
  function syncVisibleRoomIds() {
    var incoming = controller.activeRooms.map(function(room) { return room.id })
    var retained = overviewRoomIds.filter(function(id) { return incoming.indexOf(id) >= 0 })
    for (var i = 0; i < incoming.length; ++i)
      if (retained.indexOf(incoming[i]) < 0) retained.push(incoming[i])
    if (JSON.stringify(retained) !== JSON.stringify(overviewRoomIds)) overviewRoomIds = retained
  }
  function requestRoomPreview(roomId) {
    var room = controller.rooms.find(function(item) { return item.id === roomId })
    if (room && room.externalProfile && String(room.externalProfile).startsWith("remote::")) return
    if (!roomId || !controller.opened || !bridge.available
        || Object.prototype.hasOwnProperty.call(roomPreviewCache.rows, roomId)
        || previewRequests[roomId]) return
    var pending = Object.assign({}, previewRequests)
    pending[roomId] = true
    previewRequests = pending
    bridge.request("GET", "/v1/events?roomId=" + encodeURIComponent(roomId)
      + "&before=9223372036854775807&limit=12", null, function(value) {
      if (!extended) return
      var remaining = Object.assign({}, extended.previewRequests)
      delete remaining[roomId]
      extended.previewRequests = remaining
      if (value.ok === false || !Array.isArray(value.events)) return
      var lines = []
      for (var i = value.events.length - 1; i >= 0 && lines.length < 3; --i) {
        var event = value.events[i]
        var kind = String(event.kind || "")
        if (!(kind === "message.user" || kind === "message.assistant")) continue
        var body = extended.controller.eventBody(event).replace(/\s+/g, " ").trim()
        if (!body) continue
        lines.unshift({ summaryText: extended.controller.eventTitle(event).slice(0, 50)
          + " · " + body.slice(0, 240) })
      }
      extended.roomPreviewCache = TerminalPreviewCache.store(extended.roomPreviewCache, roomId, lines, 32)
    })
  }
  // Remote rooms cannot read local events; show the bounded paired-source
  // preview automatically while the read-only terminal attaches.
  function requestRemotePreview(roomId) {
    if (!roomId || !controller.opened || !bridge.available
        || Object.prototype.hasOwnProperty.call(roomPreviewCache.rows, roomId)
        || previewRequests[roomId]) return
    var pending = Object.assign({}, previewRequests)
    pending[roomId] = true
    previewRequests = pending
    bridge.request("GET", "/v1/rooms/" + encodeURIComponent(roomId) + "/preview", null, function(value) {
      if (!extended) return
      var remaining = Object.assign({}, extended.previewRequests)
      delete remaining[roomId]
      extended.previewRequests = remaining
      if (!value || value.ok !== true || !Array.isArray(value.messages)) return
      var lines = []
      for (var i = value.messages.length - 1; i >= 0 && lines.length < 3; --i) {
        var message = value.messages[i]
        var role = message && message.role
        var body = String(message && message.text || "").replace(/\s+/g, " ").trim()
        if ((role !== "user" && role !== "assistant") || !body) continue
        lines.unshift({ summaryText: (role === "user" ? "YOU · " : "ASSISTANT · ") + body.slice(0, 240) })
      }
      if (lines.length > 0)
        extended.roomPreviewCache = TerminalPreviewCache.store(extended.roomPreviewCache, roomId, lines, 32)
    })
  }
  Component.onCompleted: syncVisibleRoomIds()
  Connections {
    target: extended.controller
    function onActiveRoomsChanged() { extended.syncVisibleRoomIds() }
  }

  function tilePreviewEvents(items) {
    var selected = []
    var hasUser = false
    var hasAssistant = false
    var hasActivity = false
    for (var index = items.length - 1; index >= 0; --index) {
      var event = items[index]
      var kind = String(event.kind || "")
      if (kind === "message.user" && !hasUser) { selected.push(event); hasUser = true }
      else if (kind === "message.assistant" && !hasAssistant) { selected.push(event); hasAssistant = true }
      else if (kind !== "message.user" && kind !== "message.assistant" && !hasActivity) {
        selected.push(event); hasActivity = true
      }
      if (hasUser && hasAssistant && hasActivity) break
    }
    if (selected.length === 0) return items.slice(Math.max(0, items.length - 3))
    return selected.sort(function(a, b) {
      return Number(a.sequence || a.cursor || 0) - Number(b.sequence || b.cursor || 0)
    })
  }

  Rectangle {
    anchors.fill: parent
    color: Color.popups.background
    radius: Style.cornerRadius

    ColumnLayout {
      anchors.fill: parent
      spacing: Style.space(8)

      RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: Style.space(42)
        spacing: Style.space(9)

        BotFace {
          Layout.preferredWidth: Style.space(30)
          Layout.preferredHeight: Style.space(30)
          size: Style.space(30)
          colorHex: String(Color.accent)
          shapeName: "diamond"
          working: controller.working
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: 0
          Text {
            Layout.fillWidth: true
            text: "ARES EXTENDED"
            color: extended.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
          }
          Text {
            Layout.fillWidth: true
            text: controller.activeRooms.length + " active rooms · "
              + (controller.extendedViewMode === "overview" ? "tiled overview"
                 : controller.extendedViewMode === "grown" ? "master + stack" : "focused room")
              + " · Tab / Shift+Tab"
            color: extended.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        ExtendedButton {
          visible: controller.extendedViewMode !== "overview"
          glyph: "󰕰"
          hint: "Show all rooms"
          onActivated: controller.showExtendedOverview()
        }
        ExtendedButton {
          glyph: "󰁍"
          hint: "Return to compact panel"
          onActivated: controller.closeExtended()
        }
        ExtendedButton {
          glyph: "󰅖"
          hint: "Close Ares"
          onActivated: controller.close()
        }
      }

      Rectangle { Layout.fillWidth: true; height: 1; color: extended.divider }

      StackLayout {
        id: modeStack
        Layout.fillWidth: true
        Layout.fillHeight: true
        currentIndex: controller.extendedViewMode === "focus" ? 2
          : controller.extendedViewMode === "grown" ? 1 : 0

        Item {
          GridView {
            id: overviewGrid
            anchors.fill: parent
            clip: true
            model: controller.opened ? extended.overviewRoomIds : []
            boundsBehavior: Flickable.StopAtBounds
            cacheBuffer: cellHeight * 2
            readonly property int columns: Math.max(1, Math.min(3, Math.floor(width / Style.space(680))))
            readonly property int rows: Math.max(1, Math.ceil(Math.max(1, count) / columns))
            cellWidth: width / columns
            cellHeight: Math.max(Style.space(360), height / rows)
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AlwaysOn }

            populate: Transition {
              NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 170; easing.type: Easing.OutCubic }
            }

            delegate: RoomTile {
              required property var modelData
              width: GridView.view.cellWidth - Style.space(7)
              height: GridView.view.cellHeight - Style.space(7)
              roomData: controller.rooms.find(function(room) { return room.id === modelData }) || null
              inViewport: y + height > GridView.view.contentY
                && y < GridView.view.contentY + GridView.view.height
              warmViewport: y + height > GridView.view.contentY - GridView.view.cacheBuffer
                && y < GridView.view.contentY + GridView.view.height + GridView.view.cacheBuffer
              previewMode: "overview"
              detailed: false
              reorderView: GridView.view
              onGrowRequested: function(room) { controller.growExtendedRoom(room) }
              onFullscreenRequested: function(room) { controller.focusExtendedRoom(room) }
            }
          }

          Text {
            anchors.centerIn: parent
            visible: controller.activeRooms.length === 0
            text: "No active rooms"
            color: extended.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.subtitle
          }
        }

        Item {
          RoomTile {
            id: masterTile
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: parent.width * 0.62 - Style.space(5)
            roomData: extended.currentRoom
            detailed: true
            onGrowRequested: function(room) { controller.growExtendedRoom(room) }
            onFullscreenRequested: function(room) { controller.focusExtendedRoom(room) }
          }

          GridView {
            id: stackGrid
            anchors.left: masterTile.right
            anchors.leftMargin: Style.space(10)
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            clip: true
            model: controller.opened ? extended.stackRoomIds : []
            boundsBehavior: Flickable.StopAtBounds
            cacheBuffer: cellHeight * 2
            readonly property int columns: 1
            cellWidth: width / columns
            cellHeight: Math.max(Style.space(300), height / 3)
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AlwaysOn }

            delegate: RoomTile {
              required property var modelData
              width: GridView.view.cellWidth - Style.space(7)
              height: GridView.view.cellHeight - Style.space(7)
              roomData: controller.rooms.find(function(room) { return room.id === modelData }) || null
              inViewport: y + height > GridView.view.contentY
                && y < GridView.view.contentY + GridView.view.height
              warmViewport: y + height > GridView.view.contentY - GridView.view.cacheBuffer
                && y < GridView.view.contentY + GridView.view.height + GridView.view.cacheBuffer
              previewMode: "grown"
              detailed: false
              reorderView: GridView.view
              onGrowRequested: function(room) { controller.growExtendedRoom(room) }
              onFullscreenRequested: function(room) { controller.focusExtendedRoom(room) }
            }
          }
        }

        Rectangle {
          color: extended.surface
          radius: Style.cornerRadius
          border.color: Color.accent
          border.width: 1

          ColumnLayout {
            anchors.fill: parent
            anchors.margins: Style.space(10)
            spacing: Style.space(7)

            RowLayout {
              Layout.fillWidth: true
              spacing: Style.space(8)

              BotFace {
                Layout.preferredWidth: Style.space(28)
                Layout.preferredHeight: Style.space(28)
                size: Style.space(28)
                colorHex: String(Color.accent)
                shapeName: controller.botShapeForRoom(extended.currentRoom)
                working: extended.currentRoom && controller.roomWorking(extended.currentRoom.id)
                waiting: extended.currentRoom && controller.roomNeedsReply(extended.currentRoom.id)
                notificationColor: Color.urgent
              }
              PeerBadge {
                id: focusPeerBadge
                controller: extended.controller
                nodeId: String((extended.controller.projectForRoom(extended.currentRoom) || ({ node: "local" })).node || "local")
                Layout.preferredWidth: implicitWidth
                Layout.preferredHeight: implicitHeight
              }
              ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                MarqueeTitle {
                  Layout.fillWidth: true
                  text: extended.currentRoom ? extended.currentRoom.name : "No active room"
                  color: extended.foreground
                  fontFamily: Style.font.family
                  pixelSize: Style.font.title
                  bold: true
                  TapHandler {
                    enabled: focusTranscript.remotePeerRoom && !focusTranscript.targetResolved
                    onTapped: focusTranscript.openSourcePreview()
                  }
                }
                Text {
                  Layout.fillWidth: true
                  text: {
                    var project = controller.projectForRoom(extended.currentRoom)
                    return (project ? project.name + " · " : "") + "fullscreen focus"
                  }
                  color: extended.dim
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
              }
              ExtendedButton {
                glyph: "󰁌"
                hint: "Grow room"
                enabled: extended.currentRoom !== null
                onActivated: controller.growExtendedRoom(extended.currentRoom)
              }
              ExtendedButton {
                glyph: "󰕰"
                hint: "Show all rooms"
                onActivated: controller.showExtendedOverview()
              }
              ExtendedButton {
                glyph: "󰏤"
                hint: "Pause room"
                enabled: extended.currentRoom !== null && !extended.currentNativeGroup
                onActivated: controller.toggleRoomPause(extended.currentRoom)
              }
              ExtendedButton {
                glyph: "󰀼"
                hint: "Archive room"
                enabled: extended.currentRoom !== null
                onActivated: controller.archiveRoom(extended.currentRoom)
              }
            }

            Rectangle { Layout.fillWidth: true; height: 1; color: extended.divider }

            TerminalChat {
              id: focusTranscript
              focusOnAttach: !extended.controller.searchOpen
              Layout.fillWidth: true
              Layout.fillHeight: true
              controller: extended.controller
              bridge: extended.bridge
              roomId: extended.currentRoom ? extended.currentRoom.id : ""
              eventModel: []
              foreground: extended.foreground
              dim: extended.dim
              surface: extended.surface
              surfaceStrong: extended.faint
              accentWash: extended.selectedSurface
              fontFamily: Style.font.family
              active: extended.visible && extended.controller.opened
                && extended.controller.extendedMode && extended.controller.extendedViewMode === "focus"
              hideComposer: extended.controller.editMode && !extended.terminalPromptConfirmOpen
            }


          }
        }
      }
      ColumnLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: visible ? implicitHeight : 0
        visible: extended.currentRoom !== null && !extended.currentNativeGroup
        spacing: Style.space(5)

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)
          Text {
            Layout.fillWidth: true
            text: extended.currentRoom ? controller.roomDisplayName(extended.currentRoom) : ""
            color: extended.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
          ExtendedButton {
            glyph: "󰏫"
            hint: controller.editMode ? "Return to terminal (draft kept)" : "Edit this room"
            accent: controller.editMode
            onActivated: {
              controller.editMode = !controller.editMode
              if (controller.editMode) expandedInput.forceActiveFocus()
              else controller.persistDraftNow()
            }
          }
        }

        RowLayout {
          Layout.fillWidth: true
          Layout.preferredHeight: visible ? Math.min(Style.space(170), Math.max(Style.space(40), expandedInput.contentHeight + Style.space(16))) : 0
          visible: controller.editMode || !extended.currentTerminalOwnsInput
          spacing: Style.space(6)
          ExtendedButton { glyph: "󰋩"; hint: "Attach image"; onActivated: controller.chooseImage() }
          Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: extended.faint
            radius: Style.space(4)
            border.color: expandedInput.activeFocus ? Color.accent : extended.divider
            border.width: 1
            TextArea {
              id: expandedInput
              anchors.fill: parent
              anchors.margins: Style.space(5)
              text: controller.roomText
              placeholderText: "Message this room…"
              color: extended.foreground
              placeholderTextColor: extended.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              background: null
              wrapMode: TextEdit.Wrap
              onTextChanged: if (text !== controller.roomText) controller.roomText = text
              Keys.onPressed: function(event) {
                var pasteModifier = (event.modifiers & (Qt.ControlModifier | Qt.MetaModifier | Qt.GroupSwitchModifier)) !== 0
                  && !(event.modifiers & Qt.AltModifier)
                if (event.key === Qt.Key_V && pasteModifier && !controller.sendingImage) {
                  event.accepted = true
                  extended.requestPaste("compact")
                }
              }
              Keys.onReturnPressed: function(event) {
                if (event.modifiers & Qt.ShiftModifier) event.accepted = false
                else { extended.submitEditPrompt(); event.accepted = true }
              }
              Keys.onEnterPressed: function(event) {
                if (event.modifiers & Qt.ShiftModifier) event.accepted = false
                else { extended.submitEditPrompt(); event.accepted = true }
              }
            }
          }
          ExtendedButton {
            glyph: "󰊢"
            hint: "Open multiline editor"
            onActivated: {
              extended.expandedEditorRoomId = extended.currentRoom.id
              extended.expandedEditorOpen = true
            }
          }
          ExtendedButton {
            glyph: controller.sendingImage ? "󰔟" : "󰒊"
            hint: "Send (Enter)"
            accent: true
            enabled: !controller.sendingImage
              && (!extended.currentTerminalOwnsInput || controller.pendingImages.length === 0)
              && (controller.roomText.trim() !== "" || controller.pendingImages.length > 0)
              && !controller.pendingImages.some(function(image) { return image.uploading || image.error })
            onActivated: extended.submitEditPrompt()
          }
        }
        ComposerAttachments {
          objectName: "attachmentCompactGallery"
          Layout.fillWidth: true
          Layout.preferredHeight: visible ? implicitHeight : 0
          images: controller.pendingImages
          removalEnabled: !controller.sendingImage
          visible: (controller.editMode || !extended.currentTerminalOwnsInput) && images.length > 0
          warningText: extended.currentTerminalOwnsInput
            ? "Images are saved in this draft. Sending images from Edit to Herdr is not supported. Nothing has been sent." : ""
          onRemoveRequested: function(index) { controller.removePendingImage(index) }
          onPreviewRequested: function(source, name) { attachmentPreview.open(source, name, expandedInput) }
        }
      }
    }
  }

  Rectangle {
    anchors.fill: parent
    z: 40
    visible: opacity > 0
    enabled: extended.expandedEditorOpen && !extended.currentNativeGroup
    opacity: extended.expandedEditorOpen && !extended.currentNativeGroup ? 1 : 0
    color: Qt.rgba(0, 0, 0, 0.68)
    Behavior on opacity { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
    MouseArea { anchors.fill: parent; onClicked: extended.expandedEditorOpen = false }
    Rectangle {
      anchors.centerIn: parent
      width: Math.min(parent.width - Style.space(50), Style.space(720))
      height: Math.min(parent.height - Style.space(60), Style.space(530))
      radius: Style.cornerRadius * 2
      color: Color.popups.background
      border.color: Color.accent
      MouseArea { anchors.fill: parent }
      ColumnLayout {
        anchors.fill: parent
        anchors.margins: Style.space(17)
        spacing: Style.space(10)
        Text {
          Layout.fillWidth: true
          text: "EDIT · " + (extended.currentRoom ? controller.roomDisplayName(extended.currentRoom) : "")
          color: extended.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle
        }
        TextArea {
          id: expandedArea
          Layout.fillWidth: true
          Layout.fillHeight: true
          text: controller.roomText
          color: extended.foreground
          placeholderTextColor: extended.dim
          placeholderText: "Your draft is saved automatically…"
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          wrapMode: TextEdit.Wrap
          onTextChanged: if (text !== controller.roomText) controller.roomText = text
          Keys.onPressed: function(event) {
            var pasteModifier = (event.modifiers & (Qt.ControlModifier | Qt.MetaModifier | Qt.GroupSwitchModifier)) !== 0
              && !(event.modifiers & Qt.AltModifier)
            if (event.key === Qt.Key_V && pasteModifier && !controller.sendingImage) {
              event.accepted = true
              extended.requestPaste("modal")
            }
          }
        }
        ComposerAttachments {
          objectName: "attachmentExpandedGallery"
          Layout.fillWidth: true
          Layout.preferredHeight: visible ? implicitHeight : 0
          images: controller.pendingImages
          removalEnabled: !controller.sendingImage
          warningText: extended.currentTerminalOwnsInput
            ? "Images are saved in this draft. Sending images from Edit to Herdr is not supported. Nothing has been sent." : ""
          onRemoveRequested: function(index) { controller.removePendingImage(index) }
          onPreviewRequested: function(source, name) { attachmentPreview.open(source, name, expandedArea) }
        }
        RowLayout {
          Layout.fillWidth: true
          Item { Layout.fillWidth: true }
          Text { text: "Cancel (draft kept)"; color: extended.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption }
          ExtendedButton { glyph: "󰅖"; hint: "Close (draft kept)"; onActivated: extended.expandedEditorOpen = false }
          ExtendedButton {
            glyph: "󰒊"
            hint: "Send"
            accent: true
            enabled: !controller.sendingImage
              && (!extended.currentTerminalOwnsInput || controller.pendingImages.length === 0)
              && (controller.roomText.trim() !== "" || controller.pendingImages.length > 0)
              && !controller.pendingImages.some(function(image) { return image.uploading || image.error })
            onActivated: extended.submitEditPrompt()
          }
        }
      }
    }
  }

  Rectangle {
    anchors.fill: parent
    z: 50
    visible: opacity > 0
    enabled: extended.terminalPromptConfirmOpen
    opacity: extended.terminalPromptConfirmOpen ? 1 : 0
    color: Qt.rgba(0, 0, 0, 0.32)
    Behavior on opacity { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
    MouseArea { anchors.fill: parent; onClicked: extended.terminalPromptConfirmOpen = false }
    Rectangle {
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.top: parent.top
      anchors.topMargin: Style.space(80)
      width: Math.min(parent.width - Style.space(50), Style.space(470))
      height: confirmColumn.implicitHeight + Style.space(30)
      radius: Style.cornerRadius * 1.5
      color: Color.popups.background
      border.color: Color.accent
      MouseArea { anchors.fill: parent }
      ColumnLayout {
        id: confirmColumn
        anchors.fill: parent
        anchors.margins: Style.space(15)
        spacing: Style.space(10)
        Text {
          Layout.fillWidth: true
          text: "SEND TO TERMINAL"
          color: extended.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Text {
          Layout.fillWidth: true
          text: "Check the now-visible TUI input. Send only if it is empty: Herdr cannot detect unsent text. Your Ares draft is kept if sending fails."
          color: extended.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
        }
        RowLayout {
          Layout.fillWidth: true
          Item { Layout.fillWidth: true }
          ExtendedButton { glyph: "󰅖"; hint: "Cancel"; onActivated: extended.terminalPromptConfirmOpen = false }
          Text { text: "Input is empty · Send"; color: extended.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption }
          ExtendedButton {
            glyph: "󰒊"
            hint: "I checked the TUI input is empty; send"
            accent: true
            onActivated: {
              if (!extended.currentRoom || extended.terminalPromptRoomId !== extended.currentRoom.id
                  || extended.terminalPromptRoomId !== controller.selectedRoomId || !extended.currentTerminalInteractive) {
                extended.terminalPromptConfirmOpen = false
                controller.errorText = "The terminal changed; draft was not sent."
                return
              }
              extended.terminalPromptConfirmOpen = false
              controller.appendLocalEvent(true, true)
            }
          }
        }
      }
    }
  }

  component RoomTile: Rectangle {
    id: tile
    required property var roomData
    property bool detailed: false
    readonly property bool terminalInteractive: tileTerminalChat.terminalInteractive
    readonly property bool terminalOwnsInput: tileTerminalChat.terminalOwnsInput
    property bool inViewport: true
    property bool warmViewport: true
    property string previewMode: ""
    property var reorderView: null
    readonly property bool liveTerminalCandidate: roomData && roomData.source !== "control"
    readonly property bool remotePeerRoom: !!roomData && roomData.source === "hermes"
      && String(roomData.externalProfile || "").startsWith("remote::")
    readonly property bool previewFallback: !detailed && !liveTerminalCandidate
    readonly property var cachedFrame: roomData && extended.controller.terminalFrameCache
      && !(roomData.externalProfile && String(roomData.externalProfile).startsWith("remote::"))
      ? extended.controller.terminalFrameCache.frames[roomData.id] || null : null
    readonly property var cachedPreview: roomData && !(roomData.externalProfile
      && String(roomData.externalProfile).startsWith("remote::"))
      ? extended.roomPreviewCache.rows[roomData.id] || [] : []
    readonly property var remoteCachedPreview: remotePeerRoom
      ? extended.roomPreviewCache.rows[roomData.id] || [] : []
    function requestPreview() {
      if (tile.remotePeerRoom) {
        if (tile.warmViewport && tile.roomData) extended.requestRemotePreview(tile.roomData.id)
        return
      }
      if (tile.roomData && tile.roomData.externalProfile
          && String(tile.roomData.externalProfile).startsWith("remote::")) return
      if (tile.liveTerminalCandidate) return
      if (tile.warmViewport && tile.roomData) extended.requestRoomPreview(tile.roomData.id)
    }
    Component.onCompleted: requestPreview()
    onWarmViewportChanged: requestPreview()
    onRoomDataChanged: requestPreview()
    signal growRequested(var room)
    signal fullscreenRequested(var room)
    readonly property var projectData: extended.controller.projectForRoom(roomData)
    readonly property var roomEvents: roomData && extended.visible && extended.controller.extendedMode
      && tile.previewFallback && tile.inViewport
      && !(roomData.externalProfile && String(roomData.externalProfile).startsWith("remote::"))
      ? extended.controller.eventsForRoom(roomData.id) : []
    readonly property bool activeWork: roomData ? extended.controller.roomWorking(roomData.id) : false
    readonly property bool needsReply: roomData ? extended.controller.roomNeedsReply(roomData.id) : false

    color: extended.controller.extendedRoomId === (roomData ? roomData.id : "") ? extended.selectedSurface : extended.surface
    radius: Style.space(5)
    border.color: extended.dragTargetRoomId === (roomData ? roomData.id : "") ? Color.accent
      : needsReply ? Color.urgent
      : (activeWork || extended.controller.extendedRoomId === (roomData ? roomData.id : "")
        ? Color.accent : extended.divider)
    border.width: extended.dragTargetRoomId === (roomData ? roomData.id : "")
      || activeWork || needsReply ? 2 : 1
    clip: true

    Behavior on color { ColorAnimation { duration: 150 } }

    ColumnLayout {
      anchors.fill: parent
      anchors.margins: Style.space(7)
      spacing: Style.space(5)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(5)
        Rectangle {
          id: reorderHandle
          visible: !tile.detailed
          Layout.preferredWidth: visible ? Style.space(27) : 0
          Layout.preferredHeight: Style.space(27)
          color: handleMouse.pressed ? extended.selectedSurface : extended.faint
          radius: Style.space(4)
          border.color: extended.divider
          Text {
            anchors.centerIn: parent
            text: "⋮⋮"
            color: extended.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }
          MouseArea {
            id: handleMouse
            anchors.fill: parent
            hoverEnabled: true
            preventStealing: true
            cursorShape: Qt.OpenHandCursor
            property string dropRoomId: ""
            onPressed: function(mouse) { dropRoomId = "" }
            onPositionChanged: function(mouse) {
              if (!pressed || !tile.reorderView || !tile.roomData) return
              var view = tile.reorderView
              var viewportPoint = mapToItem(view, mouse.x, mouse.y)
              if (viewportPoint.x < 0 || viewportPoint.x >= view.width
                  || viewportPoint.y < 0 || viewportPoint.y >= view.height) {
                dropRoomId = ""
              } else {
                var point = mapToItem(view.contentItem, mouse.x, mouse.y)
                var index = view.indexAt(point.x, point.y)
                dropRoomId = index >= 0 ? String(view.model[index]) : ""
              }
              extended.dragTargetRoomId = dropRoomId
            }
            onReleased: function(mouse) {
              var target = dropRoomId
              dropRoomId = ""
              extended.dragTargetRoomId = ""
              if (target && tile.roomData && target !== tile.roomData.id)
                extended.moveRoomBefore(tile.roomData.id, target)
            }
            onCanceled: {
              dropRoomId = ""
              extended.dragTargetRoomId = ""
            }
          }
          PanelToolTip {
            visible: handleMouse.containsMouse
            text: "Drag to reorder Ares tiles (temporary)"
            fontFamily: Style.font.family
          }
        }
        BotFace {
          Layout.preferredWidth: Style.space(tile.detailed ? 26 : 21)
          Layout.preferredHeight: Style.space(tile.detailed ? 26 : 21)
          size: Style.space(tile.detailed ? 26 : 21)
          colorHex: String(Color.accent)
          shapeName: extended.controller.botShapeForRoom(tile.roomData)
          working: tile.activeWork
          waiting: tile.needsReply
          notificationColor: Color.urgent
        }
        PeerBadge {
          id: tilePeerBadge
          controller: extended.controller
          nodeId: String((tile.projectData || ({ node: "local" })).node || "local")
          compact: !tile.detailed
          Layout.preferredWidth: implicitWidth
          Layout.preferredHeight: implicitHeight
        }
        ColumnLayout {
          Layout.fillWidth: true
          spacing: 0
          MarqueeTitle {
            Layout.fillWidth: true
            visible: tile.detailed
            Layout.preferredHeight: visible ? implicitHeight : 0
            text: tile.roomData ? tile.roomData.name : "Room unavailable"
            color: extended.foreground
            fontFamily: Style.font.family
            pixelSize: Style.font.subtitle
            bold: true
            TapHandler {
              enabled: tileTerminalChat.remotePeerRoom && !tileTerminalChat.targetResolved
              onTapped: tileTerminalChat.openSourcePreview()
            }
          }
          Text {
            Layout.fillWidth: true
            visible: !tile.detailed
            Layout.preferredHeight: visible ? implicitHeight : 0
            text: tile.roomData ? tile.roomData.name : "Room unavailable"
            color: extended.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            elide: Text.ElideRight
            maximumLineCount: 1
            ToolTip.visible: compactTitleHover.hovered && truncated
            ToolTip.text: text
            ToolTip.delay: 450
            HoverHandler { id: compactTitleHover }
          }
          Text {
            Layout.fillWidth: true
            text: (tile.projectData ? tile.projectData.name : "Unknown project")
              + " · " + extended.controller.roomStatusLabel(tile.roomData).toLowerCase()
            color: tile.needsReply ? Color.urgent : (tile.activeWork ? Color.accent : extended.dim)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
          TapHandler {
            id: headerSelect
            enabled: !tile.detailed
            onTapped: tile.growRequested(tile.roomData)
          }
        }
      }

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(5)
        Text {
          Layout.fillWidth: true
          text: tile.needsReply ? "REPLY NEEDED" : (tile.activeWork ? "LIVE" : "")
          color: tile.needsReply ? Color.urgent : Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.bold: true
        }
        ExtendedButton {
          glyph: "󰁌"
          hint: "Grow room"
          onActivated: tile.growRequested(tile.roomData)
        }
        ExtendedButton {
          glyph: "󰊓"
          hint: "Focus fullscreen"
          onActivated: tile.fullscreenRequested(tile.roomData)
        }
        ExtendedButton {
          glyph: "󰏤"
          hint: "Pause room"
          enabled: tile.roomData !== null && tile.roomData.source !== "hermes-native-group"
          onActivated: controller.toggleRoomPause(tile.roomData)
        }
        ExtendedButton {
          glyph: "󰀼"
          hint: "Archive room"
          enabled: tile.roomData !== null
          onActivated: controller.archiveRoom(tile.roomData)
        }
      }

      Rectangle { Layout.fillWidth: true; height: 1; color: extended.divider }

      Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: tile.detailed || (tile.liveTerminalCandidate && !tile.previewFallback)

        Image {
          anchors.fill: parent
          source: tile.cachedFrame ? tile.cachedFrame.image.url : ""
          visible: !!tile.cachedFrame && !tileTerminalChat.surfacePainted && !tileTerminalChat.legacyTranscriptNeeded && tileTerminalChat.error === ""
          fillMode: Image.Stretch
          smooth: false
        }

        Text {
          anchors.centerIn: parent
          width: parent.width - Style.space(16)
          visible: !tile.cachedFrame && !tileTerminalChat.surfacePainted
            && !(tile.remotePeerRoom && tile.remoteCachedPreview.length > 0)
          text: tile.roomData ? extended.controller.roomDisplayName(tile.roomData) : ""
          color: extended.dim
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          horizontalAlignment: Text.AlignHCenter
          elide: Text.ElideRight
        }
        ListView {
          anchors.fill: parent
          anchors.margins: Style.space(4)
          visible: tile.remotePeerRoom && !tileTerminalChat.surfacePainted
            && tile.remoteCachedPreview.length > 0
          clip: true
          interactive: false
          model: tile.remoteCachedPreview
          spacing: Style.space(3)
          delegate: Text {
            required property var modelData
            width: ListView.view.width
            text: modelData.summaryText || ""
            textFormat: Text.PlainText
            color: extended.foreground
            opacity: String(modelData.summaryText || "").indexOf("YOU · ") === 0 ? 1 : 0.72
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
            maximumLineCount: 2
            wrapMode: Text.WrapAtWordBoundaryOrAnywhere
          }
        }
        TerminalChat {
          id: tileTerminalChat
          anchors.fill: parent
          controller: extended.controller
          bridge: extended.bridge
          roomId: tile.roomData ? tile.roomData.id : ""
          eventModel: []
          allowLegacyTranscript: tile.detailed
          focusOnAttach: tile.detailed && !extended.controller.searchOpen
          previewReadOnly: !tile.detailed
          previewContentShown: tile.remoteCachedPreview.length > 0
          hideComposer: tile.detailed && extended.controller.editMode && !extended.terminalPromptConfirmOpen
          onPreviewActivated: tile.growRequested(tile.roomData)
          foreground: extended.foreground
          dim: extended.dim
          surface: extended.surface
          surfaceStrong: extended.faint
          accentWash: extended.selectedSurface
          fontFamily: Style.font.family
          active: extended.controller.opened && extended.controller.extendedMode
            && tile.warmViewport && (tile.detailed
              ? extended.controller.extendedViewMode === "grown"
              : (tile.liveTerminalCandidate && extended.controller.extendedViewMode === tile.previewMode))
        }
      }

      ListView {
        Layout.fillWidth: true
        Layout.fillHeight: true
        visible: tile.previewFallback
        clip: true
        interactive: false
        model: tile.previewFallback
          ? (tile.roomEvents.length > 0 ? extended.tilePreviewEvents(tile.roomEvents) : tile.cachedPreview) : []
        spacing: Style.space(3)
        boundsBehavior: Flickable.StopAtBounds

        delegate: Text {
          required property var modelData
          width: ListView.view.width
          text: modelData.summaryText || (extended.controller.eventTitle(modelData) + " · "
            + extended.controller.eventBody(modelData).replace(/\s+/g, " "))
          textFormat: Text.PlainText
          color: extended.foreground
          opacity: String(modelData.kind || "") === "message.user" ? 1 : 0.72
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          maximumLineCount: 1
        }
        MouseArea {
          anchors.fill: parent
          scrollGestureEnabled: false
          cursorShape: Qt.PointingHandCursor
          onClicked: tile.growRequested(tile.roomData)
          onWheel: function(wheel) { wheel.accepted = false }
        }
      }

      Text {
        Layout.fillWidth: true
        text: (tile.roomData ? tile.roomData.eventCount : 0) + " events"
        color: extended.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignRight
      }
    }
  }

  component ExtendedButton: Rectangle {
    id: button
    property string glyph: ""
    property string hint: ""
    property bool accent: false
    signal activated()
    implicitWidth: Style.space(28)
    implicitHeight: Style.space(25)
    radius: Style.space(3)
    color: !enabled ? extended.faint
      : (accent ? Color.accent : (buttonMouse.containsMouse ? extended.selectedSurface : extended.faint))
    border.color: accent ? Color.accent : extended.divider
    border.width: 1
    opacity: enabled ? 1 : 0.45
    Behavior on color { ColorAnimation { duration: 140 } }

    Text {
      id: buttonLabel
      anchors.centerIn: parent
      text: button.glyph
      color: button.accent ? Color.background : extended.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.icon
    }
    MouseArea {
      id: buttonMouse
      anchors.fill: parent
      enabled: button.enabled
      hoverEnabled: true
      cursorShape: button.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      onClicked: button.activated()
    }
    PanelToolTip {
      visible: buttonMouse.containsMouse && button.hint !== ""
      text: button.hint
      fontFamily: Style.font.family
    }
  }
}
