import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Item {
  id: workspace

  required property var controller
  required property var bridge

  readonly property color foreground: Color.popups.text
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.52)
  readonly property color faint: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.10)
  readonly property color divider: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.18)
  readonly property color surface: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.045)
  readonly property color surfaceStrong: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.09)
  readonly property color accentWash: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.14)
  readonly property color paceHealthy: Color.pick("quota.on-pace",
    Qt.hsla(0.36, Math.max(0.55, Color.accent.hslSaturation),
      Math.max(0.38, Math.min(0.58, Color.accent.hslLightness)), 1))
  readonly property color paceBehind: Color.pick("quota.behind-pace", Color.urgent)
  readonly property string fontFamily: Style.font.family
  readonly property int waitingCount: controller.displayEvents.filter(function(event) { return event.kind === "approval.request" }).length
  property bool botPickerOpen: false
  property bool extendedEditorOpen: false
  readonly property bool editMode: controller.editMode
  property bool terminalPromptConfirmOpen: false
  property string terminalPromptRoomId: ""
  property bool dictating: false
  property bool aliasEditorOpen: false
  property string aliasEditorText: ""
  property bool archiveSidebarSelectionConfirmOpen: false
  property real sidebarRestoreContentY: -1
  property int sidebarScrollRestoreAttempts: 0
  function retainSidebarScroll() {
    sidebarRestoreContentY = treeList.contentY
    sidebarScrollRestoreAttempts = 0
  }
  function restoreSidebarScroll() {
    if (sidebarRestoreContentY < 0) return
    sidebarScrollRestoreTimer.restart()
  }
  function requestArchiveSidebarSelection(room) {
    retainSidebarScroll()
    var selected = controller.sidebarSelectedRoomIds || []
    if (selected.indexOf(room.id) < 0 || selected.length < 2) {
      controller.archiveRoom(room)
      return
    }
    archiveSidebarSelectionConfirmOpen = true
  }
  property var projectMenuProject: null
  property bool projectMenuOlderMode: false
  property string projectMenuOlderAmount: "7"
  property int projectMenuOlderUnitIndex: 1
  readonly property var projectMenuOlderUnits: [
    ({ label: "hours", seconds: 3600 }),
    ({ label: "days", seconds: 86400 }),
    ({ label: "weeks", seconds: 604800 })
  ]
  function openProjectMenu(project, item, x, y) {
    projectMenuProject = project
    projectMenuOlderMode = false
    var point = item.mapToItem(workspace, x, y)
    projectContextMenu.x = Math.max(0, Math.min(point.x, workspace.width - projectContextMenu.width - Style.space(8)))
    projectContextMenu.y = Math.max(0, Math.min(point.y, workspace.height - projectContextMenu.height - Style.space(8)))
    projectContextMenu.visible = true
  }
  function closeProjectMenu() {
    projectContextMenu.visible = false
    projectMenuProject = null
    projectMenuOlderMode = false
  }
  function projectMenuOlderCutoff() {
    var amount = parseInt(projectMenuOlderAmount, 10)
    if (!isFinite(amount) || amount < 1) amount = 1
    return Date.now() / 1000 - amount * projectMenuOlderUnits[projectMenuOlderUnitIndex].seconds
  }
  readonly property int projectMenuOlderCount: projectMenuProject
    ? controller.projectRoomsOlderThan(projectMenuProject, projectMenuOlderCutoff()).length : 0
  // Keep the two most recently opened PTYs attached while navigating rooms.
  property string cachedRoomA: ""
  property string cachedRoomB: ""
  property int cacheSlot: 0
  readonly property var selectedFrame: controller.selectedRoomId && controller.terminalFrameCache
    ? controller.terminalFrameCache.frames[controller.selectedRoomId] || null : null
  readonly property bool showSelectedFrame: controller.selectedRoom && controller.selectedRoom.source === "hermes"
    && selectedFrame && (cacheSlot === 0 ? compactTranscriptA.error : compactTranscriptB.error) === ""
    && !(cacheSlot === 0 ? compactTranscriptA.surfacePainted : compactTranscriptB.surfacePainted)
    && !(cacheSlot === 0 ? compactTranscriptA.legacyTranscriptNeeded : compactTranscriptB.legacyTranscriptNeeded)
  readonly property bool terminalCacheActive: workspace.visible && workspace.enabled && controller.opened
  readonly property bool selectedTerminalInteractive: cacheSlot === 0
    ? compactTranscriptA.terminalInteractive : compactTranscriptB.terminalInteractive
  readonly property bool selectedNativeGroup: !!controller.selectedRoom && controller.selectedRoom.source === "hermes-native-group"
  readonly property bool selectedTerminalOwnsInput: cacheSlot === 0
    ? compactTranscriptA.terminalOwnsInput : compactTranscriptB.terminalOwnsInput
  readonly property var selectedTerminal: cacheSlot === 0 ? compactTranscriptA : compactTranscriptB

  function activateSearchRoom(room) {
    // Closing search destroys its delegates; finish navigation in this stable owner.
    roomSearch.close()
    controller.selectRecentRoom(room)
  }
  function cacheSelectedRoom() {
    var id = controller.selectedRoomId
    if (!id) return
    if (id === cachedRoomA) { cacheSlot = 0; return }
    if (id === cachedRoomB) { cacheSlot = 1; return }
    if (cacheSlot === 0) { cachedRoomB = id; cacheSlot = 1 }
    else { cachedRoomA = id; cacheSlot = 0 }
  }
  function submitEditPrompt() {
    if (workspace.selectedTerminalOwnsInput && !workspace.selectedTerminalInteractive) {
      controller.errorText = "Terminal is not ready. Retry or choose a terminal; draft was not sent."
      return
    }
    if (workspace.selectedTerminalInteractive) {
      if (controller.pendingImages.length > 0) {
        controller.appendLocalEvent(true, true)
        return
      }
      if (controller.roomText.trim() === "") return
      workspace.extendedEditorOpen = false
      workspace.terminalPromptRoomId = controller.selectedRoomId
      workspace.terminalPromptConfirmOpen = true
      return
    }
    workspace.extendedEditorOpen = false
    controller.appendLocalEvent()
  }
  Component.onCompleted: cacheSelectedRoom()
  Connections {
    target: workspace.controller
    function onSelectedRoomIdChanged() {
      workspace.extendedEditorOpen = false
      workspace.terminalPromptConfirmOpen = false
      workspace.cacheSelectedRoom()
    }
  }

  // Omarchy dictation (voxtype) state + toggle. The status follower is a
  // direct child of this popup, so Quickshell reaps it on restart.
  Process {
    id: voxStatusProc
    command: ["omarchy-voxtype-status"]
    running: true
    stdout: SplitParser {
      onRead: function(line) {
        try {
          var state = JSON.parse(line)
          workspace.dictating = state["class"] === "recording" || state["class"] === "transcribing"
        } catch (_) {}
      }
    }
  }

  Process {
    id: voxToggleProc
    command: ["voxtype", "record", "toggle"]
  }

  // Clipboard image paste: Ctrl+V in the composer first checks for an image
  // in the Wayland clipboard (wl-paste). If none, falls back to a normal
  // text paste so regular copy/paste keeps working untouched.
  property bool pastePending: false
  property string pasteTarget: "compact"
  property string pasteRoomId: ""
  Process {
    id: pasteImageProc
    command: ["bash", "-c",
      'DIR="$HOME/.cache/omarchy-ares"; mkdir -p "$DIR"; '
      + 'T=$(wl-paste --list-types 2>/dev/null | grep -im1 "^image/"); '
      + 'if [ -n "$T" ]; then F="$DIR/paste-$(date +%s%3N).png"; wl-paste --type "$T" > "$F" && echo "ARES_PASTE:$F"; fi']
    stdout: SplitParser {
      onRead: function(line) {
        if (line.indexOf("ARES_PASTE:") !== 0) return
        controller.addPendingImage(line.substring("ARES_PASTE:".length), workspace.pasteRoomId)
        workspace.pastePending = false
      }
    }
    onExited: function(code) {
      if (!workspace.pastePending) return
      workspace.pastePending = false
      // No image in the clipboard: fall back to a plain text paste in the
      // input that actually requested it.
      if (controller.selectedRoomId !== workspace.pasteRoomId) {
        controller.errorText = "Room changed before paste; paste again in the original room"
        return
      }
      if (workspace.pasteTarget === "extended") extendedArea.paste()
      else composerInput.paste()
    }
  }

  function botChipLabel() {
    var bot = controller.sendBotForRoom(controller.selectedRoom)
    if (!bot) return "default"
    var model = String(bot.model || "").trim()
    if (model !== "") return model
    var provider = String(bot.provider || "").trim()
    if (provider !== "") return provider
    return String(bot.name || bot.id)
  }

  function saveAlias() {
    if (controller.selectedRoom)
      controller.updateRoomAlias(controller.selectedRoom, workspace.aliasEditorText)
    workspace.aliasEditorOpen = false
  }

  function quotaLabel(item) {
    return String(item.label || item.provider || "quota")
  }
  function providerLabel(provider) {
    var value = String(provider || "provider").replace(/-/g, " ")
    return value.charAt(0).toUpperCase() + value.slice(1)
  }
  function compactNumber(value) {
    var number = Number(value || 0)
    if (number >= 1000000) return (number / 1000000).toFixed(number >= 10000000 ? 0 : 1) + "M"
    if (number >= 1000) return (number / 1000).toFixed(number >= 10000 ? 0 : 1) + "K"
    return String(Math.round(number))
  }
  function weeklyWindow(item) {
    var windows = item && item.windows ? item.windows : []
    for (var index = 0; index < windows.length; ++index) {
      var label = String(windows[index].label || "").toLowerCase()
      if (label === "weekly" || label === "semanal") return windows[index]
    }
    var weekly = null
    for (var windowIndex = 0; windowIndex < windows.length; ++windowIndex) {
      if (!weekly || Number(windows[windowIndex].windowSeconds || 0) > Number(weekly.windowSeconds || 0))
        weekly = windows[windowIndex]
    }
    return weekly
  }
  function fiveHourWindow(item) {
    var windows = item && item.windows ? item.windows : []
    for (var index = 0; index < windows.length; ++index) {
      var label = String(windows[index].label || "").toLowerCase().replace(/\s/g, "")
      if (label === "5h" || Math.abs(Number(windows[index].windowSeconds || 0) - 18000) < 60)
        return windows[index]
    }
    return null
  }
  function expectedAvailablePercent(window) {
    if (!window || !window.resetAt || !window.windowSeconds) return -1
    var reset = Number(window.resetAt)
    if (!isFinite(reset)) reset = Date.parse(String(window.resetAt)) / 1000
    if (!isFinite(reset)) return -1
    if (reset > 1000000000000) reset /= 1000
    var remaining = Math.max(0, Math.min(Number(window.windowSeconds), reset - Date.now() / 1000))
    return remaining * 100 / Number(window.windowSeconds)
  }
  function providerPaceColor(item) {
    var weekly = weeklyWindow(item)
    if (!weekly) return item && item.status === "ok" ? workspace.dim : workspace.paceBehind
    var expected = expectedAvailablePercent(weekly)
    var available = Number(weekly.availablePercent || 0)
    return expected < 0 || available >= expected ? workspace.paceHealthy : workspace.paceBehind
  }
  function providerSummary(item) {
    var windows = (item.windows || []).map(function(window) {
      return String(window.label || "quota") + " " + Math.round(Number(window.availablePercent || 0)) + "%"
    }).join(" · ")
    var activity = "1h " + compactNumber(item.hourTokens) + " tok / " + compactNumber(item.hourCalls) + " calls"
      + " · 7d " + compactNumber(item.weekTokens) + " tok / " + compactNumber(item.weekCalls) + " calls"
    return providerLabel(item.provider) + " — " + (windows || String(item.status || "unknown").toUpperCase()) + " · " + activity
  }

  Rectangle {
    anchors.fill: parent
    color: Color.popups.background
    radius: Style.cornerRadius

    ColumnLayout {
      anchors.fill: parent
      spacing: 0

      // Compact hero: one identity, one status sentence, quiet actions.
      RowLayout {
        id: hero
        Layout.fillWidth: true
        Layout.preferredHeight: Style.space(42)
        spacing: Style.space(6)

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
          spacing: Style.space(1)

          Text {
            Layout.fillWidth: true
            text: "ARES"
            textFormat: Text.PlainText
            color: workspace.dim
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: Style.space(1)
          }

          Text {
            Layout.fillWidth: true
            text: controller.projects.length + " project" + (controller.projects.length === 1 ? "" : "s")
              + " · " + controller.rooms.length + " room" + (controller.rooms.length === 1 ? "" : "s")
              + (workspace.waitingCount > 0 ? " · " + workspace.waitingCount + " waiting on you" : "")
              + " · " + controller.peers.length + " peers"
            textFormat: Text.PlainText
            color: workspace.foreground
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
          }
        }

        ActionButton {
          glyph: "󰍹"
          hint: controller.viewCycleHint
          onActivated: controller.openExtended()
        }
        ActionButton {
          glyph: "󰑐"
          hint: "Refresh (R)"
          spinning: controller.working
          onActivated: controller.reload()
        }
        ActionButton {
          glyph: "󰖟"
          hint: "Open web workspace (W)"
          onActivated: controller.openWeb()
        }
        ActionButton {
          glyph: "󰆏"
          hint: "Copy mobile access link (token included)"
          onActivated: controller.copyAccess()
        }
        ActionButton {
          glyph: "󰀼"
          hint: "Archived chats"
          onActivated: controller.section = "archived"
        }
        ActionButton {
          glyph: "󰒍"
          hint: "Control Room — pending work and decisions"
          onActivated: controller.openControlRoom()
        }
        ActionButton {
          glyph: "♛"
          hint: "Orchestrator — one chat that manages everything"
          onActivated: controller.openOrchestrator()
        }
        ActionButton {
          glyph: "󰒙"
          hint: "Peer services — discover, pair and review activity"
          onActivated: controller.openPeers()
        }
        ActionButton {
          glyph: "󰒓"
          hint: "Preferences"
          onActivated: controller.section = "preferences"
        }
        ActionButton {
          glyph: "󰅖"
          hint: "Close"
          onActivated: controller.close()
        }
      }

      Rectangle { Layout.fillWidth: true; height: 1; color: workspace.divider }

      ColumnLayout {
        id: providerQuotaStrip
        Layout.fillWidth: true
        Layout.preferredHeight: controller.homeQuotaCollapsed
          ? Style.space(30)
          : Style.space(28 + controller.visibleQuotaProviders.length * 46)
        visible: controller.homeQuotaVisible && controller.visibleQuotaProviders.length > 0
        spacing: Style.space(4)

        RowLayout {
          Layout.fillWidth: true
          Layout.preferredHeight: Style.space(26)
          visible: !controller.homeQuotaCollapsed
          Text {
            Layout.fillWidth: true
            text: "PROVIDER USAGE"
            color: workspace.dim
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: Style.space(1)
          }
          ActionButton {
            compact: true
            glyph: controller.homeQuotaCollapsed ? "󰁃" : "󰁅"
            hint: controller.homeQuotaCollapsed ? "Expand provider usage" : "Collapse to marquee"
            onActivated: controller.toggleHomeQuotaCollapsed()
          }
        }

        Rectangle {
          id: quotaMarqueeClip
          Layout.fillWidth: true
          Layout.preferredHeight: Style.space(30)
          visible: controller.homeQuotaCollapsed
          clip: true
          radius: Style.space(4)
          color: workspace.surface
          border.color: workspace.divider
          border.width: 1

          RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Style.space(6)
            anchors.rightMargin: Style.space(3)
            spacing: Style.space(4)
            Item {
              id: quotaMarqueeViewport
              Layout.fillWidth: true
              Layout.fillHeight: true
              clip: true
              Row {
                id: quotaMarquee
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(14)
                Repeater {
                  model: controller.visibleQuotaProviders
                  delegate: Text {
                    required property var modelData
                    text: workspace.providerSummary(modelData) + "     •"
                    textFormat: Text.PlainText
                    color: workspace.providerPaceColor(modelData)
                    font.family: workspace.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                  }
                }
                NumberAnimation on x {
                  from: quotaMarqueeViewport.width
                  to: -quotaMarquee.width
                  duration: Math.max(7000, (quotaMarquee.width + quotaMarqueeViewport.width) * 24)
                  loops: Animation.Infinite
                  running: controller.homeQuotaCollapsed && quotaMarquee.width > quotaMarqueeViewport.width
                }
              }
            }
            ActionButton {
              compact: true
              glyph: "󰁃"
              hint: "Expand provider usage"
              onActivated: controller.toggleHomeQuotaCollapsed()
            }
          }
        }

        ColumnLayout {
          Layout.fillWidth: true
          visible: !controller.homeQuotaCollapsed
          spacing: Style.space(2)
          Repeater {
            model: controller.visibleQuotaProviders
            delegate: Rectangle {
              id: providerQuotaRow
              required property var modelData
              readonly property var weekly: workspace.weeklyWindow(modelData)
              readonly property var shortWindow: workspace.fiveHourWindow(modelData)
              readonly property real weeklyAvailable: weekly ? Number(weekly.availablePercent || 0) : -1
              readonly property real weeklyExpected: workspace.expectedAvailablePercent(weekly)
              readonly property bool weeklyOnPace: weeklyExpected < 0 || weeklyAvailable >= weeklyExpected
              Layout.fillWidth: true
              Layout.preferredHeight: Style.space(44)
              radius: Style.space(4)
              color: workspace.surface
              border.color: modelData.status === "ok" ? workspace.divider : Color.urgent
              border.width: 1

              RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Style.space(5)
                anchors.rightMargin: Style.space(5)
                anchors.topMargin: Style.space(2)
                anchors.bottomMargin: Style.space(2)
                spacing: Style.space(6)
                Text {
                  Layout.preferredWidth: Style.space(84)
                  text: workspace.providerLabel(modelData.provider)
                  color: workspace.foreground
                  font.family: workspace.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  elide: Text.ElideRight
                }

                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: Style.space(1)

                  RowLayout {
                    Layout.fillWidth: true
                    visible: providerQuotaRow.weekly !== null
                    spacing: Style.space(6)
                    Text {
                      Layout.preferredWidth: Style.space(62)
                      text: "WEEK " + Math.round(providerQuotaRow.weeklyAvailable) + "%"
                      color: providerQuotaRow.weeklyOnPace
                        ? workspace.paceHealthy : workspace.paceBehind
                      font.family: workspace.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                    }
                    Rectangle {
                      id: weeklyQuotaTrack
                      Layout.fillWidth: true
                      Layout.preferredHeight: Style.space(9)
                      radius: height / 2
                      color: workspace.faint
                      Rectangle {
                        width: parent.width * Math.max(0, Math.min(100, providerQuotaRow.weeklyAvailable)) / 100
                        height: parent.height
                        radius: parent.radius
                        color: providerQuotaRow.weeklyOnPace
                          ? workspace.paceHealthy : workspace.paceBehind
                        Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                      }
                      Rectangle {
                        id: weeklyPaceMarker
                        visible: providerQuotaRow.weeklyExpected >= 0
                        x: Math.max(0, Math.min(parent.width - width,
                          parent.width * providerQuotaRow.weeklyExpected / 100 - width / 2))
                        y: -Style.space(2)
                        width: Math.max(2, Style.space(1))
                        height: parent.height + Style.space(4)
                        radius: width / 2
                        color: workspace.foreground
                      }
                    }
                    Text {
                      Layout.preferredWidth: Style.space(58)
                      text: providerQuotaRow.weeklyExpected >= 0
                        ? "pace " + Math.round(providerQuotaRow.weeklyExpected) + "%" : ""
                      color: workspace.dim
                      font.family: workspace.fontFamily
                      font.pixelSize: Math.max(8, Style.font.caption - 1)
                    }
                  }

                  RowLayout {
                    Layout.fillWidth: true
                    visible: providerQuotaRow.weekly !== null
                    spacing: Style.space(6)
                    Text {
                      Layout.preferredWidth: Style.space(62)
                      text: providerQuotaRow.shortWindow
                        ? "5H " + Math.round(Number(providerQuotaRow.shortWindow.availablePercent || 0)) + "%"
                        : "5H N/A"
                      color: workspace.dim
                      font.family: workspace.fontFamily
                      font.pixelSize: Math.max(8, Style.font.caption - 1)
                      font.bold: true
                    }
                    Rectangle {
                      Layout.fillWidth: true
                      Layout.preferredHeight: Style.space(7)
                      radius: height / 2
                      color: workspace.faint
                      Rectangle {
                        width: parent.width * Math.max(0, Math.min(100, Number(providerQuotaRow.shortWindow
                          ? providerQuotaRow.shortWindow.availablePercent : 0))) / 100
                        height: parent.height
                        radius: parent.radius
                        color: Color.accent
                        Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                      }
                    }
                    Item { Layout.preferredWidth: Style.space(58); Layout.preferredHeight: 1 }
                  }

                  Text {
                    Layout.fillWidth: true
                    visible: providerQuotaRow.weekly === null
                    text: (modelData.plan ? modelData.plan + " · " : "")
                      + String(modelData.status || "unknown").toUpperCase()
                      + (modelData.details.length > 0 ? " · " + String(modelData.details[0]) : "")
                    color: modelData.status === "ok" ? workspace.dim : Color.urgent
                    font.family: workspace.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }
              }
            }
          }
        }
      }

      Rectangle {
        Layout.fillWidth: true
        height: controller.homeQuotaVisible && controller.visibleQuotaProviders.length > 0 ? 1 : 0
        color: workspace.divider
      }

      Text {
        Layout.fillWidth: true
        visible: controller.errorText !== ""
        Layout.topMargin: visible ? Style.space(7) : 0
        Layout.bottomMargin: visible ? Style.space(7) : 0
        text: controller.errorText
        textFormat: Text.PlainText
        color: Color.urgent
        font.family: workspace.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.topMargin: Style.space(4)
        Layout.bottomMargin: Style.space(3)
        spacing: Style.space(8)

        // Compact project -> room tree keeps the complete workspace visible.
        Rectangle {
          id: sidebarCard
          Layout.preferredWidth: controller.sidebarCollapsed ? 0 : Style.space(controller.sidebarWidth)
          Layout.minimumWidth: controller.sidebarCollapsed ? 0 : Style.space(120)
          Layout.maximumWidth: controller.sidebarCollapsed ? 0 : Style.space(480)
          visible: !controller.sidebarCollapsed
          Layout.fillHeight: true
          radius: Style.cornerRadius
          color: workspace.surface

          MouseArea {
            id: sidebarGrip
            visible: !controller.sidebarCollapsed
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: Style.space(6)
            z: 10
            cursorShape: Qt.SizeHorCursor
            hoverEnabled: true
            property real dragStartX: 0
            property real dragStartWidth: 0
            property bool resized: false
            onPressed: function(mouse) {
              dragStartX = mouse.x
              dragStartWidth = sidebarCard.width
              resized = false
            }
            onPositionChanged: function(mouse) {
              if (!sidebarGrip.pressed) return
              if (Math.abs(mouse.x - dragStartX) >= Style.space(2)) resized = true
              var newRealWidth = dragStartWidth + (mouse.x - dragStartX)
              var scale = Style.effectiveSpacingScale > 0 ? Style.effectiveSpacingScale : 1
              controller.setSidebarWidth(Math.round(newRealWidth / scale), true)
            }
            onClicked: if (!resized) controller.cycleSidebarWidth()
            Rectangle {
              anchors.centerIn: parent
              width: Math.max(2, Style.space(2))
              height: parent.height * 0.24
              radius: width / 2
              color: parent.containsMouse || parent.pressed
                ? Color.accent
                : Qt.rgba(workspace.foreground.r, workspace.foreground.g, workspace.foreground.b, 0.34)
              Behavior on color { ColorAnimation { duration: 140 } }
            }
            PanelToolTip {
              delay: 3000
              visible: sidebarGrip.containsMouse && !sidebarGrip.pressed
              text: "Drag to resize · click for 3 sizes"
              fontFamily: workspace.fontFamily
            }
          }

          ColumnLayout {
            anchors.fill: parent
            anchors.margins: Style.space(5)
            spacing: Style.space(3)

            RowLayout {
              Layout.fillWidth: true
              SectionLabel {
                Layout.fillWidth: true
                label: "CHATS  " + (roomSearch.expanded ? roomSearch.results.length : controller.rooms.length)
              }
              ActionButton {
                compact: true
                glyph: "󰍞"
                hint: "Collapse sidebar"
                onActivated: controller.toggleSidebar()
              }
              ActionButton {
                compact: true
                glyph: "󰉖"
                hint: "Add project"
                onActivated: { controller.projectFormOpen = true; controller.roomFormOpen = false }
              }
              ActionButton {
                compact: true
                glyph: "󰻞"
                hint: "Add room"
                enabled: controller.selectedProject !== null
                onActivated: { controller.roomFormOpen = true; controller.projectFormOpen = false }
              }
            }

            ListView {
              id: treeList
              Layout.fillWidth: true
              Layout.fillHeight: true
              clip: true
              model: roomSearch.expanded ? roomSearch.results : controller.navigationRows
              currentIndex: roomSearch.expanded ? roomSearch.currentIndex : -1
              highlightMoveDuration: 0
              spacing: 0
              boundsBehavior: Flickable.StopAtBounds
              onModelChanged: workspace.restoreSidebarScroll()
              onContentHeightChanged: workspace.restoreSidebarScroll()
              ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
              add: Transition {
                enabled: !roomSearch.expanded
                ParallelAnimation {
                  NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 150; easing.type: Easing.OutCubic }
                  NumberAnimation { property: "y"; from: -Style.space(5); duration: 150; easing.type: Easing.OutCubic }
                }
              }
              remove: Transition {
                enabled: !roomSearch.expanded && workspace.sidebarRestoreContentY < 0
                NumberAnimation { property: "opacity"; to: 0; duration: 110; easing.type: Easing.InCubic }
              }
              displaced: Transition {
                enabled: !roomSearch.expanded
                NumberAnimation { properties: "y"; duration: 160; easing.type: Easing.OutCubic }
              }

              delegate: Item {
                id: treeRow
                required property var modelData
                required property int index
                readonly property bool sectionItem: modelData.kind === "section"
                readonly property bool hostItem: modelData.kind === "host"
                readonly property bool projectItem: modelData.kind === "project"
                readonly property bool roomItem: modelData.kind === "room"
                readonly property bool directRoom: roomItem && modelData.direct === true
                readonly property int depth: Number(modelData.depth || 0)
                readonly property var projectData: roomItem || projectItem ? modelData.project : null
                readonly property var roomData: roomItem ? modelData.room : null
                readonly property bool needsAttention: roomItem && roomData
                  && controller.roomNeedsReply(roomData.id)
                readonly property bool sidebarSelected: roomItem
                  && controller.sidebarSelectedRoomIds.indexOf(roomData.id) >= 0
                readonly property bool selectedItem: roomSearch.expanded
                  ? index === roomSearch.currentIndex
                  : (roomItem && controller.selectedRoomId === roomData.id
                    || sidebarSelected
                    || projectItem && modelData.expanded && controller.selectedProjectId === projectData.id)
                width: ListView.view.width
                height: Style.space(treeRow.sectionItem ? 20 : (treeRow.roomItem && treeRow.roomData && treeRow.roomData.alias ? 38 : 26))

                Rectangle {
                  visible: treeRow.sectionItem
                  anchors.fill: parent
                  radius: Style.space(3)
                  color: treeMouse.containsMouse ? workspace.surfaceStrong : "transparent"
                  Behavior on color { ColorAnimation { duration: 140 } }
                }

                Rectangle {
                  visible: !treeRow.sectionItem
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(treeRow.depth * 12)
                  radius: Style.space(3)
                  color: treeRow.hostItem ? (treeMouse.containsMouse ? workspace.surfaceStrong : "transparent")
                    : treeRow.projectItem
                    ? (treeRow.selectedItem ? workspace.accentWash : "transparent")
                    : (treeRow.selectedItem ? workspace.accentWash
                      : (treeMouse.containsMouse ? workspace.surfaceStrong : "transparent"))
                  Behavior on color { ColorAnimation { duration: 140 } }
                }

                Rectangle {
                  id: attentionGlow
                  visible: treeRow.needsAttention
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(treeRow.depth * 12)
                  radius: Style.space(3)
                  color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.08)
                  border.color: Color.urgent
                  border.width: 1
                  opacity: 0.65
                  SequentialAnimation on opacity {
                    running: attentionGlow.visible
                    loops: Animation.Infinite
                    NumberAnimation { from: 0.35; to: 0.85; duration: 1400; easing.type: Easing.InOutSine }
                    NumberAnimation { from: 0.85; to: 0.35; duration: 1400; easing.type: Easing.InOutSine }
                  }
                }

                Text {
                  visible: treeRow.sectionItem
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.bottom: parent.bottom
                  anchors.leftMargin: Style.space(5)
                  anchors.rightMargin: Style.space(20)
                  anchors.bottomMargin: Style.space(3)
                  text: !treeRow.modelData ? "" : treeRow.modelData.label === "RECENT"
                    ? "RECENT  " + (treeRow.modelData.expanded ? treeRow.modelData.count : Math.min(controller.recentRoomLimit, treeRow.modelData.count))
                      + "/" + treeRow.modelData.count
                    : "PROJECTS  " + treeRow.modelData.count
                  textFormat: Text.PlainText
                  color: workspace.dim
                  font.family: workspace.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  font.letterSpacing: Style.space(1)
                  elide: Text.ElideRight
                }

                Text {
                  visible: treeRow.sectionItem
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(5)
                  anchors.verticalCenter: parent.verticalCenter
                  text: "›"
                  color: Color.accent
                  font.family: workspace.fontFamily
                  font.pixelSize: Style.font.body
                  rotation: treeRow.modelData && treeRow.modelData.expanded ? 90 : 0
                  Behavior on rotation { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                }

                RowLayout {
                  z: 1
                  visible: !treeRow.sectionItem
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(5 + treeRow.depth * 12)
                  anchors.rightMargin: Style.space(5)
                  spacing: Style.space(5)

                  Text {
                    visible: treeRow.hostItem || treeRow.projectItem
                    Layout.preferredWidth: Style.space(12)
                    text: "›"
                    color: workspace.dim
                    font.family: workspace.fontFamily
                    font.pixelSize: Style.font.body
                    rotation: treeRow.modelData && treeRow.modelData.expanded ? 90 : 0
                    Behavior on rotation { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
                  }

                  BotFace {
                    visible: treeRow.roomItem
                    Layout.preferredWidth: visible ? Style.space(18) : 0
                    Layout.preferredHeight: visible ? Style.space(18) : 0
                    size: Style.space(18)
                    colorHex: String(Color.accent)
                    shapeName: treeRow.roomData ? controller.botShapeForRoom(treeRow.roomData) : "diamond"
                    working: treeRow.roomData ? controller.roomWorking(treeRow.roomData.id) : false
                    waiting: treeRow.roomData ? controller.roomNeedsReply(treeRow.roomData.id) : false
                    notificationColor: Color.urgent
                  }

                  PeerBadge {
                    visible: treeRow.roomItem
                    controller: workspace.controller
                    nodeId: String(treeRow.projectData ? (treeRow.projectData.node || "local") : "local")
                    compact: true
                    Layout.preferredWidth: visible ? implicitWidth : 0
                    Layout.preferredHeight: visible ? implicitHeight : 0
                  }

                  ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    Text {
                      id: treeTitle
                      Layout.fillWidth: true
                      readonly property string baseLabel: !treeRow.modelData ? "" : treeRow.hostItem
                        ? controller.peerBadgeForNode(treeRow.modelData.nodeId) + " (" + treeRow.modelData.projectCount + ")"
                        : treeRow.projectItem && treeRow.projectData
                        ? treeRow.projectData.name + " (" + treeRow.modelData.roomCount + ")"
                        : (!treeRow.roomData ? "" : treeRow.modelData.recent ? treeRow.roomData.name
                          : treeRow.directRoom && treeRow.projectData
                          ? treeRow.projectData.name + (treeRow.roomData.name === treeRow.projectData.name ? "" : " · " + treeRow.roomData.name)
                          : treeRow.roomData.name)
                      readonly property bool hasAlias: treeRow.roomItem && treeRow.roomData
                        && treeRow.roomData.alias !== undefined && treeRow.roomData.alias !== ""
                      text: hasAlias ? treeRow.roomData.alias : baseLabel
                      textFormat: Text.PlainText
                      color: treeRow.roomItem && controller.roomNeedsReply(treeRow.roomData.id)
                        ? Color.urgent : (treeRow.hostItem
                          ? controller.peerColor((controller.peerForNode(treeRow.modelData.nodeId) || ({ colorRole: "muted" })).colorRole)
                          : workspace.foreground)
                      font.family: workspace.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.bold: treeRow.hostItem || treeRow.projectItem || (treeRow.roomItem
                        && (controller.selectedRoomId === treeRow.roomData.id
                          || controller.roomNeedsReply(treeRow.roomData.id)
                          || controller.roomWorking(treeRow.roomData.id)))
                      elide: Text.ElideRight
                      maximumLineCount: 1
                      ToolTip.visible: treeMouse.containsMouse && treeTitle.truncated
                      ToolTip.text: treeTitle.text
                      ToolTip.delay: 3000
                    }

                    Text {
                      Layout.fillWidth: true
                      visible: treeRow.roomItem && treeRow.roomData && treeRow.roomData.alias
                        && treeRow.roomData.name !== treeRow.roomData.alias
                      text: visible ? treeRow.roomData.name : ""
                      textFormat: Text.PlainText
                      color: workspace.dim
                      font.family: workspace.fontFamily
                      font.pixelSize: Math.max(8, Style.font.bodySmall - 2)
                      elide: Text.ElideRight
                      maximumLineCount: 1
                    }
                  }

                  Text {
                    visible: treeRow.roomItem && treeRow.roomData && treeRow.roomData.source === "hermes"
                    text: "↗"
                    color: Color.accent
                    font.family: workspace.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                  }
                  Text {
                    visible: treeRow.roomItem
                    text: treeRow.roomData && controller.roomNeedsReply(treeRow.roomData.id)
                      ? (treeRow.modelData.recent ? "!" : controller.roomStatusLabel(treeRow.roomData))
                      : (treeRow.roomData && controller.roomWorking(treeRow.roomData.id)
                        ? (treeRow.modelData.recent ? "●" : controller.roomStatusLabel(treeRow.roomData))
                      : (treeRow.roomData ? (treeRow.modelData.recent
                        ? controller.roomActivityAge(treeRow.roomData)
                        : String(treeRow.roomData.eventCount || 0)) : "")
                        )
                    color: treeRow.roomData && controller.roomNeedsReply(treeRow.roomData.id)
                      ? Color.urgent : (treeRow.roomData && controller.roomWorking(treeRow.roomData.id)
                        ? Color.accent : workspace.dim)
                    font.family: workspace.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                  ActionButton {
                    compact: true
                    visible: treeRow.roomItem && treeRow.modelData.recent
                    glyph: "󰀼"
                    hint: "Archive chat"
                    onActivated: workspace.requestArchiveSidebarSelection(treeRow.roomData)
                  }
                  ActionButton {
                    compact: true
                    visible: treeRow.projectItem
                    glyph: "󰀼"
                    hint: "Archive this group and every chat inside"
                    onActivated: { workspace.retainSidebarScroll(); controller.archiveProjectRooms(treeRow.projectData) }
                  }
                }

                MouseArea {
                  id: treeMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  z: 0
                  cursorShape: Qt.PointingHandCursor
                  acceptedButtons: Qt.LeftButton | Qt.RightButton
                  onClicked: function(mouse) {
                    if (mouse.button === Qt.RightButton) {
                      if (treeRow.projectItem && !roomSearch.expanded)
                        workspace.openProjectMenu(treeRow.projectData, treeMouse, mouse.x, mouse.y)
                      return
                    }
                    if (roomSearch.expanded && treeRow.roomItem) {
                      workspace.activateSearchRoom(treeRow.roomData)
                    }
                    else if (treeRow.sectionItem) { workspace.retainSidebarScroll(); controller.toggleNavigationSection(treeRow.modelData.label) }
                    else if (treeRow.hostItem) { workspace.retainSidebarScroll(); controller.toggleHost(treeRow.modelData.nodeId) }
                    else if (treeRow.projectItem) { workspace.retainSidebarScroll(); controller.toggleProject(treeRow.projectData.id) }
                    else if (treeRow.roomItem) controller.selectSidebarRoom(treeRow.roomData, (mouse.modifiers & Qt.ShiftModifier) !== 0, treeRow.modelData.recent)
                  }
                }
              }
            }

            Text {
              Layout.fillWidth: true
              visible: roomSearch.expanded ? roomSearch.results.length === 0 : controller.sortedProjects().length === 0
              text: roomSearch.expanded ? qsTr("No chats found")
                : (controller.projects.length === 0 ? "Add a code project to create its first room." : "No chats in projects yet.")
              color: workspace.dim
              font.family: workspace.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            QuietButton {
              id: archivedSidebarButton
              Layout.fillWidth: true
              glyph: "󰀼"
              label: "Archived (" + controller.archivedRooms.length + ")"
              hint: "Browse archived chats grouped by project"
              onActivated: { controller.extendedMode = false; controller.section = "archived" }
            }

            RoomSearch {
              id: roomSearch
              Layout.fillWidth: true
              paletteMode: true
              shortcut: controller.searchShortcut
              onPaletteRequested: controller.openSearch()
              rooms: controller.sortedRooms()
              projects: controller.projects
              onRoomActivated: function(room) { workspace.activateSearchRoom(room) }
              onRevealCurrent: Qt.callLater(function() {
                if (roomSearch.expanded && roomSearch.currentIndex >= 0)
                  treeList.positionViewAtIndex(roomSearch.currentIndex, ListView.Contain)
              })
            }
          }
        }

        Rectangle { width: controller.sidebarCollapsed ? 0 : 1; Layout.fillHeight: true; color: workspace.divider }

        // Transcript-first room workspace.
        ColumnLayout {
          Layout.fillWidth: true
          Layout.fillHeight: true
          spacing: 0

          RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(40)
            spacing: Style.space(6)

            ActionButton {
              visible: controller.sidebarCollapsed
              compact: true
              glyph: "󰍟"
              hint: "Expand sidebar"
              onActivated: controller.toggleSidebar()
            }

            Rectangle {
              Layout.preferredWidth: Style.space(8)
              Layout.preferredHeight: Style.space(8)
              radius: width / 2
              color: controller.roomStateColor(controller.selectedRoom)
              SequentialAnimation on opacity {
                running: controller.selectedRoom !== null && controller.selectedRoom.status === "active" && controller.working
                loops: Animation.Infinite
                NumberAnimation { to: 0.35; duration: 520; easing.type: Easing.InOutSine }
                NumberAnimation { to: 1.0; duration: 520; easing.type: Easing.InOutSine }
              }
            }

            PeerBadge {
              id: selectedRoomPeerBadge
              visible: controller.selectedRoom !== null
              controller: workspace.controller
              nodeId: String((controller.selectedProject || ({ node: "local" })).node || "local")
              Layout.preferredWidth: visible ? implicitWidth : 0
              Layout.preferredHeight: visible ? implicitHeight : 0
            }

            Rectangle {
              visible: controller.selectedRoom !== null && !workspace.selectedNativeGroup && controller.bots.length > 0
              Layout.preferredHeight: Style.space(24)
              implicitWidth: botChipText.implicitWidth + Style.space(14)
              radius: Style.space(4)
              color: botChipMouse.containsMouse ? workspace.surfaceStrong : workspace.surface
              border.color: workspace.botPickerOpen ? Color.accent : workspace.divider
              border.width: 1
              Text {
                id: botChipText
                anchors.centerIn: parent
                text: workspace.botChipLabel()
                textFormat: Text.PlainText
                color: workspace.foreground
                font.family: workspace.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
                maximumLineCount: 1
                width: Math.min(implicitWidth, Style.space(150))
              }
              MouseArea {
                id: botChipMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: workspace.botPickerOpen = !workspace.botPickerOpen
              }
              PanelToolTip {
                visible: botChipMouse.containsMouse
                text: "Model for this chat — click to change"
                fontFamily: workspace.fontFamily
              }
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0
              MarqueeTitle {
                Layout.fillWidth: true
                text: controller.selectedRoom ? controller.roomDisplayName(controller.selectedRoom) : "Select a room"
                color: workspace.foreground
                fontFamily: workspace.fontFamily
                pixelSize: Style.font.subtitle
                bold: true
              }
              Text {
                Layout.fillWidth: true
                text: controller.selectedRoom
                  ? (controller.selectedRoom.alias ? controller.selectedRoom.name + " · " : "")
                    + (controller.selectedRoom.source === "hermes" ? "Hermes room · " : "")
                    + controller.selectedRoom.status + " · " + controller.displayEvents.length + " visible events"
                  : "Rooms belong to the selected code project"
                color: workspace.dim
                font.family: workspace.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            QuietButton {
              visible: controller.selectedRoom !== null && workspace.selectedTerminal.terminalRoom
              glyph: "󰑐"
              label: ""
              hint: "Retry terminal attachment"
              onActivated: workspace.selectedTerminal.focusRoom(true)
            }
            QuietButton {
              visible: controller.selectedRoom !== null
              glyph: "󰏫"
              label: ""
              hint: "Edit room alias"
              onActivated: {
                workspace.aliasEditorText = controller.selectedRoom ? (controller.selectedRoom.alias || "") : ""
                workspace.aliasEditorOpen = true
              }
            }
            QuietButton {
              visible: controller.selectedRoom !== null && (workspace.selectedTerminal.remotePeerRoom || workspace.selectedTerminal.localHermesRoom)
              glyph: "󰋚"
              label: ""
              hint: "Open bounded history"
              enabled: !workspace.selectedTerminal.historyOpen
              onActivated: workspace.selectedTerminal.historyOpen = true
            }
            QuietButton {
              visible: controller.selectedRoom !== null && controller.selectedRoom.source !== "control" && !workspace.selectedNativeGroup
              glyph: controller.selectedRoom && controller.selectedRoom.controlIncluded === true ? "󰈈" : "󰈉"
              label: ""
              hint: controller.selectedRoom && controller.selectedRoom.controlIncluded === true
                ? "Exclude this chat from Control Room" : "Include this chat in Control Room"
              onActivated: controller.toggleControlIncluded(controller.selectedRoom)
            }
            QuietButton {
              visible: controller.selectedRoom !== null && !workspace.selectedNativeGroup
              glyph: controller.selectedRoom && controller.selectedRoom.status === "paused" ? "󰐊" : "󰏤"
              label: ""
              hint: controller.selectedRoom && controller.selectedRoom.status === "paused" ? "Resume room" : "Pause room"
              onActivated: controller.toggleRoomPause()
            }
            QuietButton {
              visible: controller.selectedRoom !== null
              glyph: "󰀼"
              label: ""
              hint: "Archive room"
              onActivated: controller.archiveSelectedRoom()
            }
          }

          Rectangle { Layout.fillWidth: true; height: 1; color: workspace.divider }

          Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.topMargin: Style.space(4)
            Layout.bottomMargin: Style.space(4)
            Image {
              anchors.fill: parent
              source: workspace.selectedFrame ? workspace.selectedFrame.image.url : ""
              visible: workspace.showSelectedFrame
              fillMode: Image.Stretch
              smooth: false
            }
            TerminalChat {
              id: compactTranscriptA
              anchors.fill: parent
              visible: workspace.cacheSlot === 0 && controller.selectedRoomId === roomId
              controller: workspace.controller
              bridge: workspace.bridge
              roomId: workspace.cachedRoomA
              eventModel: legacyTranscriptNeeded && visible ? controller.displayEvents : []
              allowLegacyTranscript: visible
              focusOnAttach: visible && !roomSearch.expanded && !controller.searchOpen
              hideComposer: visible && workspace.editMode && !workspace.terminalPromptConfirmOpen
              foreground: workspace.foreground
              dim: workspace.dim
              surface: workspace.surface
              surfaceStrong: workspace.surfaceStrong
              accentWash: workspace.accentWash
              fontFamily: workspace.fontFamily
              active: workspace.terminalCacheActive && roomId !== ""
            }
            TerminalChat {
              id: compactTranscriptB
              anchors.fill: parent
              visible: workspace.cacheSlot === 1 && controller.selectedRoomId === roomId
              controller: workspace.controller
              bridge: workspace.bridge
              roomId: workspace.cachedRoomB
              eventModel: legacyTranscriptNeeded && visible ? controller.displayEvents : []
              allowLegacyTranscript: visible
              focusOnAttach: visible && !roomSearch.expanded && !controller.searchOpen
              hideComposer: visible && workspace.editMode && !workspace.terminalPromptConfirmOpen
              foreground: workspace.foreground
              dim: workspace.dim
              surface: workspace.surface
              surfaceStrong: workspace.surfaceStrong
              accentWash: workspace.accentWash
              fontFamily: workspace.fontFamily
              active: workspace.terminalCacheActive && roomId !== ""
            }
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: composerForm.implicitHeight + Style.space(12)
            visible: controller.selectedRoom !== null && !workspace.selectedNativeGroup
              && (!workspace.selectedTerminalOwnsInput || workspace.editMode)
            radius: Style.cornerRadius
            color: workspace.surfaceStrong
            border.color: composerInput.activeFocus ? Color.accent : workspace.divider
            border.width: 1
            Behavior on Layout.preferredHeight { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }

            ColumnLayout {
              id: composerForm
              anchors.fill: parent
              anchors.margins: Style.space(6)
              spacing: Style.space(5)

              ComposerAttachments {
                objectName: "attachmentCompactGallery"
                Layout.fillWidth: true
                Layout.preferredHeight: visible ? implicitHeight : 0
                images: controller.pendingImages
                removalEnabled: !controller.sendingImage
                warningText: workspace.selectedTerminalOwnsInput
                  ? "Images are saved in this draft. Sending images from Edit to Herdr is not supported. Nothing has been sent." : ""
                onRemoveRequested: function(index) { controller.removePendingImage(index) }
                onPreviewRequested: function(source, name) { attachmentPreview.open(source, name, composerInput) }
              }

              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(8)

                ActionButton {
                  glyph: "󰋩"
                  hint: "ATTACH IMAGE"
                  enabled: !controller.sendingImage
                  onActivated: controller.chooseImage()
                }

                Item {
                  Layout.fillWidth: true
                  Layout.preferredHeight: Math.min(Style.space(112), Math.max(Style.space(26), composerInput.contentHeight + Style.space(10)))

                  TextArea {
                    id: composerInput
                    anchors.fill: parent
                    text: controller.roomText
                    wrapMode: TextEdit.Wrap
                    color: workspace.foreground
                    selectionColor: Color.accent
                    selectedTextColor: Color.background
                    font.family: workspace.fontFamily
                    font.pixelSize: Style.font.body
                    clip: true
                    background: Item {}
                    placeholderText: controller.pendingImages.length === 0 ? "Message this room…" : "Add a message (optional)…"
                    placeholderTextColor: workspace.dim
                    enabled: !controller.sendingImage
                    onTextEdited: controller.roomText = text
                    Keys.onPressed: function(event) {
                      // Ctrl+V keeps normal text paste; image paste works with
                      // Ctrl+V or SUPER+V (Meta on Wayland; GroupSwitch on some
                      // Qt builds). Both queue a clipboard image attachment.
                      var superOnly = (event.modifiers & (Qt.MetaModifier | Qt.GroupSwitchModifier)) !== 0
                        && (event.modifiers & (Qt.ControlModifier | Qt.AltModifier)) === 0
                      var wantsImage = (event.modifiers & Qt.ControlModifier) !== 0 || superOnly
                      if (event.key === Qt.Key_V && wantsImage && !controller.sendingImage) {
                        if (pasteImageProc.running) { event.accepted = true; return }
                        workspace.pasteTarget = "compact"
                        workspace.pasteRoomId = controller.selectedRoomId
                        workspace.pastePending = true
                        event.accepted = true
                        pasteImageProc.running = true
                      }
                    }
                    Keys.onReturnPressed: function(event) {
                      if (event.modifiers & Qt.ShiftModifier) event.accepted = false
                      else { workspace.submitEditPrompt(); event.accepted = true }
                    }
                    Keys.onEnterPressed: function(event) {
                      if (event.modifiers & Qt.ShiftModifier) event.accepted = false
                      else { workspace.submitEditPrompt(); event.accepted = true }
                    }
                  }
                }

                ActionButton {
                  glyph: "󰍬"
                  hint: workspace.dictating ? "Stop dictation (voxtype)" : "Dictate with Omarchy voxtype"
                  spinning: workspace.dictating
                  enabled: !controller.sendingImage
                  onActivated: {
                    // Dictation types into the focused widget: make sure the
                    // composer owns focus before toggling the recorder.
                    composerInput.forceActiveFocus()
                    voxToggleProc.running = true
                  }
                }

                ActionButton {
                  glyph: "󰊢"
                  hint: "Extend editor — write with room"
                  enabled: !controller.sendingImage
                  onActivated: workspace.extendedEditorOpen = true
                }

                QuietButton {
                  glyph: controller.sendingImage ? "󰔟" : "󰒊"
                  label: ""
                  hint: controller.sendingImage ? "Sending…" : "Send (Enter)"
                  spinning: controller.sendingImage
                  accent: true
                  enabled: !controller.sendingImage
                    && (!workspace.selectedTerminalOwnsInput || controller.pendingImages.length === 0)
                    && (controller.roomText.trim() !== "" || controller.pendingImages.length > 0)
                    && !controller.pendingImages.some(function(image) { return image.uploading || image.error })
                  onActivated: workspace.submitEditPrompt()
                }
              }
            }
          }
        }
      }

      RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: visible ? Style.space(28) : 0
        visible: controller.selectedRoom !== null && !workspace.selectedNativeGroup
        spacing: Style.space(6)
        Item { Layout.fillWidth: true }
        QuietButton {
          glyph: "󰏫"
          label: workspace.editMode ? "Terminal" : "Editar"
          hint: workspace.editMode ? "Return to terminal (draft kept)" : "Write a multiline draft"
          accent: workspace.editMode
          onActivated: {
            controller.editMode = !controller.editMode
            if (workspace.editMode) composerInput.forceActiveFocus()
            else {
              workspace.extendedEditorOpen = false
              controller.persistDraftNow()
            }
          }
        }
      }

      Rectangle { Layout.fillWidth: true; height: 1; color: workspace.divider }

      RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: Style.space(26)
        spacing: Style.space(5)
        Text {
          Layout.fillWidth: true
          text: workspace.selectedTerminalInteractive
            ? "Drag select  ·  SUPER+C copy  ·  SUPER+V paste  ·  Esc close"
            : "↑/↓ rooms  ·  Enter send  ·  Shift+Enter newline  ·  Tab views  ·  Esc close"
          color: workspace.dim
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
        ActionButton {
          compact: true
          glyph: "󰩨"
          hint: "Panel size: " + (controller.panelSize === "large" ? "70%" : "50%") + " — click to change"
          onActivated: controller.cyclePanelSize()
        }
        Text {
          text: bridge.available ? "● LIVE" : "○ RECONNECTING"
          color: bridge.available ? Color.accent : Color.urgent
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
      }
    }

    // Creation is deliberate and focused; it no longer expands the dashboard.
    Rectangle {
      anchors.fill: parent
      visible: controller.projectFormOpen || controller.roomFormOpen
      z: 20
      color: Qt.rgba(0, 0, 0, 0.62)
      opacity: visible ? 1 : 0
      Behavior on opacity { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }

      MouseArea {
        anchors.fill: parent
        onClicked: { controller.projectFormOpen = false; controller.roomFormOpen = false }
      }

      Rectangle {
        width: Math.min(parent.width - Style.space(48), Style.space(390))
        height: formColumn.implicitHeight + Style.space(28)
        anchors.centerIn: parent
        radius: Style.cornerRadius * 1.5
        color: Color.popups.background
        border.color: Color.accent
        border.width: 1

        MouseArea { anchors.fill: parent }

        ColumnLayout {
          id: formColumn
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(14)
          anchors.rightMargin: Style.space(14)
          spacing: Style.space(10)

          Text {
            Layout.fillWidth: true
            text: controller.projectFormOpen ? "NEW PROJECT" : "NEW ROOM"
            color: workspace.foreground
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }
          Text {
            Layout.fillWidth: true
            text: controller.projectFormOpen
              ? "Connect an existing source directory."
              : "Create inside " + (controller.selectedProject ? controller.selectedProject.name : "the selected project") + "."
            color: workspace.dim
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          SoftField {
            Layout.fillWidth: true
            placeholder: controller.projectFormOpen ? "Project name" : "Room name"
            value: controller.projectFormOpen ? controller.projectName : controller.roomName
            onEdited: function(text) {
              if (controller.projectFormOpen) controller.projectName = text
              else controller.roomName = text
            }
          }
          SoftField {
            Layout.fillWidth: true
            visible: controller.projectFormOpen
            placeholder: "Existing code path"
            value: controller.projectPath
            onEdited: function(text) { controller.projectPath = text }
          }

          Flow {
            Layout.fillWidth: true
            visible: controller.roomFormOpen
            spacing: Style.space(5)
            Repeater {
              model: controller.bots
              delegate: Rectangle {
                required property var modelData
                width: botName.implicitWidth + Style.space(16)
                height: Style.space(25)
                radius: height / 2
                color: controller.roomBotId === modelData.id ? workspace.accentWash : workspace.surface
                border.color: controller.roomBotId === modelData.id ? Color.accent : workspace.divider
                Text {
                  id: botName
                  anchors.centerIn: parent
                  text: modelData.name
                  color: workspace.foreground
                  font.family: workspace.fontFamily
                  font.pixelSize: Style.font.caption
                }
                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: controller.roomBotId = modelData.id
                }
              }
            }
          }

          RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            QuietButton {
              glyph: "󰜺"
              label: ""
              hint: "Cancel"
              onActivated: { controller.projectFormOpen = false; controller.roomFormOpen = false }
            }
            QuietButton {
              glyph: "󰐕"
              label: ""
              hint: controller.projectFormOpen ? "Create project" : "Create room"
              accent: true
              onActivated: {
                if (controller.projectFormOpen) controller.createProject()
                else controller.createRoom()
              }
            }
          }
        }
      }
    }
  }

  // Alias editor modal: give the selected room a friendly display name.
  Rectangle {
    anchors.fill: parent
    visible: workspace.aliasEditorOpen
    z: 31
    color: Qt.rgba(0, 0, 0, 0.62)
    opacity: visible ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }

    MouseArea {
      anchors.fill: parent
      onClicked: workspace.aliasEditorOpen = false
    }

    Rectangle {
      width: Math.min(parent.width - Style.space(48), Style.space(400))
      height: aliasColumn.implicitHeight + Style.space(28)
      anchors.centerIn: parent
      radius: Style.cornerRadius * 1.5
      color: Color.popups.background
      border.color: Color.accent
      border.width: 1

      MouseArea { anchors.fill: parent }

      ColumnLayout {
        id: aliasColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Style.space(14)
        spacing: Style.space(8)

        Text {
          Layout.fillWidth: true
          text: "ROOM ALIAS"
          color: workspace.foreground
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Text {
          Layout.fillWidth: true
          text: controller.selectedRoom
            ? "Shown above and instead of \"" + controller.selectedRoom.name + "\" in the sidebar. Leave empty to clear."
            : "Select a room first."
          color: workspace.dim
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        TextField {
          id: aliasInput
          Layout.fillWidth: true
          text: workspace.aliasEditorText
          placeholderText: "e.g. Weekend research"
          color: workspace.foreground
          font.family: workspace.fontFamily
          selectByMouse: true
          onTextEdited: workspace.aliasEditorText = text
          onAccepted: workspace.saveAlias()
          background: Rectangle {
            radius: Style.space(4)
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.06)
            border.color: parent.activeFocus ? Color.accent : Color.popups.border
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          QuietButton {
            glyph: "󰆴"
            label: ""
            hint: "Clear alias"
            visible: controller.selectedRoom && controller.selectedRoom.alias !== ""
            onActivated: {
              controller.updateRoomAlias(controller.selectedRoom, "")
              workspace.aliasEditorOpen = false
            }
          }

          Item { Layout.fillWidth: true }

          QuietButton {
            glyph: "󰜎"
            label: ""
            hint: "Cancel"
            onActivated: workspace.aliasEditorOpen = false
          }

          QuietButton {
            glyph: "󰄬"
            label: ""
            hint: "Save alias"
            onActivated: workspace.saveAlias()
          }
        }
      }
    }
  }

  // Model picker modal: choose which bot/profile answers this chat next.
  Rectangle {
    anchors.fill: parent
    visible: workspace.botPickerOpen
    z: 30
    color: Qt.rgba(0, 0, 0, 0.62)
    opacity: visible ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }

    MouseArea {
      anchors.fill: parent
      onClicked: workspace.botPickerOpen = false
    }

    Rectangle {
      width: Math.min(parent.width - Style.space(48), Style.space(400))
      height: Math.min(parent.height - Style.space(80), botPickerColumn.implicitHeight + Style.space(28))
      anchors.centerIn: parent
      radius: Style.cornerRadius * 1.5
      color: Color.popups.background
      border.color: Color.accent
      border.width: 1

      MouseArea { anchors.fill: parent }

      ColumnLayout {
        id: botPickerColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Style.space(14)
        spacing: Style.space(8)

        Text {
          Layout.fillWidth: true
          text: "CHAT MODEL"
          color: workspace.foreground
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Text {
          Layout.fillWidth: true
          text: controller.selectedRoom
            ? "Pick the bot that answers \"" + controller.selectedRoom.name + "\" from now on."
            : "Select a room first."
          color: workspace.dim
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
          elide: Text.ElideRight
          maximumLineCount: 2
        }

        ListView {
          id: botModelList
          Layout.fillWidth: true
          Layout.preferredHeight: Math.min(Style.space(300), botModelList.count * Style.space(44))
          clip: true
          spacing: Style.space(4)
          model: controller.bots
          delegate: Rectangle {
            required property var modelData
            readonly property bool currentBot: (controller.sendBotForRoom(controller.selectedRoom) || ({})).id === modelData.id
            width: botModelList.width
            height: Style.space(40)
            radius: Style.space(4)
            color: botRowMouse.containsMouse ? workspace.surfaceStrong
              : (currentBot ? workspace.accentWash : workspace.surface)
            border.color: currentBot ? Color.accent : workspace.divider
            border.width: 1
            RowLayout {
              anchors.fill: parent
              anchors.margins: Style.space(8)
              spacing: Style.space(8)
              Text {
                Layout.fillWidth: true
                text: {
                  var provider = String(modelData.provider || "").trim()
                  var model = String(modelData.model || "").trim()
                  var label = provider !== "" && model !== "" ? provider + " / " + model
                    : (model !== "" ? model : (provider !== "" ? provider : ""))
                  return String(modelData.name || modelData.id) + (label !== "" ? "   ·   " + label : "")
                }
                textFormat: Text.PlainText
                color: workspace.foreground
                font.family: workspace.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
                maximumLineCount: 1
              }
              Text {
                visible: currentBot
                text: "●"
                color: Color.accent
                font.family: workspace.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
            MouseArea {
              id: botRowMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                controller.roomSendBotId = modelData.id
                workspace.botPickerOpen = false
              }
            }
          }
        }
      }
    }
  }

  // Show the TUI input for review before sending into another process. Herdr
  // does not expose the contents of an unsent multiline CLI draft.
  Rectangle {
    anchors.fill: parent
    visible: opacity > 0
    enabled: workspace.terminalPromptConfirmOpen
    z: 35
    color: Qt.rgba(0, 0, 0, 0.32)
    opacity: workspace.terminalPromptConfirmOpen ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }

    MouseArea { anchors.fill: parent; onClicked: workspace.terminalPromptConfirmOpen = false }

    Rectangle {
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.top: parent.top
      anchors.topMargin: Style.space(85)
      width: Math.min(parent.width - Style.space(40), Style.space(440))
      height: terminalConfirmColumn.implicitHeight + Style.space(30)
      radius: Style.cornerRadius * 1.5
      color: Color.popups.background
      border.color: Color.accent

      MouseArea { anchors.fill: parent }
      ColumnLayout {
        id: terminalConfirmColumn
        anchors.fill: parent
        anchors.margins: Style.space(15)
        spacing: Style.space(10)
        Text {
          Layout.fillWidth: true
          text: "SEND TO TERMINAL"
          color: workspace.foreground
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Text {
          Layout.fillWidth: true
          text: "Check the now-visible TUI input. Send only if it is empty: Herdr cannot detect unsent text, and this may append to it. Your Ares draft is kept if sending fails."
          color: workspace.foreground
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.body
          wrapMode: Text.WordWrap
        }
        RowLayout {
          Layout.fillWidth: true
          Item { Layout.fillWidth: true }
          QuietButton {
            glyph: "󰅖"
            label: "Cancel"
            onActivated: workspace.terminalPromptConfirmOpen = false
          }
          QuietButton {
            glyph: "󰒊"
            label: "Input is empty · Send"
            accent: true
            onActivated: {
              if (workspace.terminalPromptRoomId !== controller.selectedRoomId
                  || !workspace.selectedTerminalInteractive) {
                workspace.terminalPromptConfirmOpen = false
                controller.errorText = "The terminal changed; draft was not sent."
                return
              }
              workspace.terminalPromptConfirmOpen = false
              controller.appendLocalEvent(true, true)
            }
          }
        }
      }
    }
  }

  // Extended composer: a calm, full-size editor bound to the same draft.
  Rectangle {
    id: extendedOverlay
    anchors.fill: parent
    visible: workspace.extendedEditorOpen && !workspace.selectedNativeGroup
    z: 30
    color: Qt.rgba(0, 0, 0, 0.62)
    opacity: visible ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
    onVisibleChanged: if (visible) extendedArea.forceActiveFocus()

    MouseArea {
      anchors.fill: parent
      onClicked: workspace.extendedEditorOpen = false
    }

    Rectangle {
      width: Math.min(parent.width - Style.space(40), Style.space(560))
      // Grows with the draft until it reaches the screen cap, then the
      // TextArea scrolls internally while keeping the cursor visible.
      height: Math.min(parent.height - Style.space(60), Math.max(Style.space(230), extendedColumn.implicitHeight + Style.space(28)))
      anchors.centerIn: parent
      radius: Style.cornerRadius * 1.5
      color: Color.popups.background
      border.color: Color.accent
      border.width: 1

      MouseArea { anchors.fill: parent }

      ColumnLayout {
        id: extendedColumn
        anchors.fill: parent
        anchors.margins: Style.space(14)
        spacing: Style.space(8)

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)
          Text {
            Layout.fillWidth: true
            text: "EXTENDED MESSAGE"
            color: workspace.foreground
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }
          ActionButton {
            compact: true
            glyph: "󰅖"
            hint: "Close editor (draft is kept)"
            onActivated: workspace.extendedEditorOpen = false
          }
        }

        Rectangle {
          Layout.fillWidth: true
          // contentHeight excludes the TextArea's own vertical padding, so add
          // generous slack (top/bottom anchors.margins + control padding) to
          // keep the last line fully visible.
          Layout.preferredHeight: Math.min(Style.space(360), Math.max(Style.space(96), extendedArea.contentHeight + Style.space(34)))
          Layout.maximumHeight: Style.space(360)
          radius: Style.space(4)
          color: workspace.surfaceStrong
          border.color: extendedArea.activeFocus ? Color.accent : workspace.divider
          border.width: 1
          TextArea {
            id: extendedArea
            anchors.fill: parent
            anchors.margins: Style.space(8)
            text: controller.roomText
            wrapMode: TextEdit.Wrap
            color: workspace.foreground
            selectionColor: Color.accent
            selectedTextColor: Color.background
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.body
            background: Item {}
            placeholderText: controller.pendingImages.length === 0 ? "Message this room…" : "Add a message (optional)…"
            placeholderTextColor: workspace.dim
            onTextEdited: controller.roomText = text
            Keys.onPressed: function(event) {
              var superOnly = (event.modifiers & (Qt.MetaModifier | Qt.GroupSwitchModifier)) !== 0
                && (event.modifiers & (Qt.ControlModifier | Qt.AltModifier)) === 0
              var wantsImage = (event.modifiers & Qt.ControlModifier) !== 0 || superOnly
              if (event.key === Qt.Key_V && wantsImage && !controller.sendingImage) {
                if (pasteImageProc.running) { event.accepted = true; return }
                workspace.pasteTarget = "extended"
                workspace.pasteRoomId = controller.selectedRoomId
                workspace.pastePending = true
                event.accepted = true
                pasteImageProc.running = true
              }
            }
          }
        }

        ComposerAttachments {
          objectName: "attachmentExpandedGallery"
          Layout.fillWidth: true
          Layout.preferredHeight: visible ? implicitHeight : 0
          images: controller.pendingImages
          removalEnabled: !controller.sendingImage
          warningText: workspace.selectedTerminalOwnsInput
            ? "Images are saved in this draft. Sending images from Edit to Herdr is not supported. Nothing has been sent." : ""
          onRemoveRequested: function(index) { controller.removePendingImage(index) }
          onPreviewRequested: function(source, name) { attachmentPreview.open(source, name, extendedArea) }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)
          ActionButton {
            glyph: "󰋩"
            hint: "Attach image to this draft"
            enabled: !controller.sendingImage
            onActivated: controller.chooseImage()
          }
          Text {
            Layout.fillWidth: true
            text: "Paste or attach images · saved with the draft"
            color: workspace.dim
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)
          Text {
            Layout.fillWidth: true
            text: "Draft saves automatically · Shift+Enter for a new line"
            color: workspace.dim
            font.family: workspace.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
          QuietButton {
            glyph: "󰅖"
            label: ""
            hint: "Close (draft is kept)"
            onActivated: workspace.extendedEditorOpen = false
          }
          QuietButton {
            glyph: "󰒊"
            label: ""
            hint: "Send (Enter)"
            accent: true
            enabled: !controller.sendingImage
              && (!workspace.selectedTerminalOwnsInput || controller.pendingImages.length === 0)
              && (controller.roomText.trim() !== "" || controller.pendingImages.length > 0)
              && !controller.pendingImages.some(function(image) { return image.uploading || image.error })
            onActivated: {
              workspace.submitEditPrompt()
            }
          }
        }
      }
    }
  }

  ImagePreview {
    id: attachmentPreview
    objectName: "composerImagePreview"
    anchors.fill: parent
    attachments: controller.pendingImages
    contextKey: controller.selectedRoomId + ":" + controller.section
      + ":" + workspace.extendedEditorOpen + ":" + controller.editMode
    viewActive: workspace.visible && workspace.enabled && controller.opened
  }

  MouseArea {
    anchors.fill: parent
    visible: projectContextMenu.visible
    z: 44
    onClicked: workspace.closeProjectMenu()
  }

  Rectangle {
    id: projectContextMenu
    objectName: "projectContextMenu"
    visible: false
    z: 45
    width: Style.space(230)
    height: projectMenuColumn.implicitHeight + Style.space(12)
    radius: Style.cornerRadius
    color: Color.popups.background
    border.color: Color.popups.border
    border.width: 1
    MouseArea { anchors.fill: parent }
    ColumnLayout {
      id: projectMenuColumn
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.margins: Style.space(6)
      spacing: Style.space(3)
      Text {
        Layout.fillWidth: true
        text: workspace.projectMenuProject ? workspace.projectMenuProject.name : ""
        textFormat: Text.PlainText
        color: workspace.dim
        font.family: workspace.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        elide: Text.ElideRight
      }
      QuietButton {
        Layout.fillWidth: true
        visible: !workspace.projectMenuOlderMode
        glyph: "󰀼"
        label: "Archive all"
        hint: "Archive this group and every chat inside"
        onActivated: {
          var project = workspace.projectMenuProject
          workspace.closeProjectMenu()
          workspace.retainSidebarScroll()
          controller.archiveProjectRooms(project)
        }
      }
      QuietButton {
        Layout.fillWidth: true
        visible: !workspace.projectMenuOlderMode
        glyph: ""
        label: "Archive older than…"
        hint: "Archive only chats idle longer than a chosen age"
        onActivated: workspace.projectMenuOlderMode = true
      }
      RowLayout {
        Layout.fillWidth: true
        visible: workspace.projectMenuOlderMode
        spacing: Style.space(5)
        TextField {
          id: projectMenuOlderField
          objectName: "projectMenuOlderField"
          Layout.preferredWidth: Style.space(52)
          text: workspace.projectMenuOlderAmount
          color: workspace.foreground
          font.family: workspace.fontFamily
          selectByMouse: true
          inputMethodHints: Qt.ImhDigitsOnly
          onTextEdited: workspace.projectMenuOlderAmount = text
          background: Rectangle {
            radius: Style.space(4)
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.06)
            border.color: parent.activeFocus ? Color.accent : Color.popups.border
          }
        }
        QuietButton {
          Layout.fillWidth: true
          glyph: ""
          label: workspace.projectMenuOlderUnits[workspace.projectMenuOlderUnitIndex].label
          hint: "Switch unit: hours, days, weeks"
          onActivated: workspace.projectMenuOlderUnitIndex = (workspace.projectMenuOlderUnitIndex + 1) % workspace.projectMenuOlderUnits.length
        }
      }
      Text {
        Layout.fillWidth: true
        visible: workspace.projectMenuOlderMode
        text: "Archives " + workspace.projectMenuOlderCount + " chat" + (workspace.projectMenuOlderCount === 1 ? "" : "s")
          + " idle longer. Restorable later."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: workspace.dim
        font.family: workspace.fontFamily
        font.pixelSize: Style.font.caption
      }
      RowLayout {
        Layout.fillWidth: true
        visible: workspace.projectMenuOlderMode
        spacing: Style.space(5)
        QuietButton {
          glyph: "󰅖"
          label: "Back"
          onActivated: workspace.projectMenuOlderMode = false
        }
        Item { Layout.fillWidth: true }
        QuietButton {
          glyph: "󰀼"
          label: "Archive"
          accent: true
          enabled: workspace.projectMenuOlderCount > 0
          onActivated: {
            var project = workspace.projectMenuProject
            var cutoff = workspace.projectMenuOlderCutoff()
            workspace.closeProjectMenu()
            workspace.retainSidebarScroll()
            controller.archiveProjectRoomsOlderThan(project, cutoff)
          }
        }
      }
    }
  }

  Rectangle {
    id: archiveSidebarSelectionDialog
    anchors.fill: parent
    visible: workspace.archiveSidebarSelectionConfirmOpen
    z: 40
    color: Qt.rgba(0, 0, 0, 0.62)
    MouseArea {
      anchors.fill: parent
      onClicked: workspace.archiveSidebarSelectionConfirmOpen = false
    }
    Rectangle {
      width: Math.min(parent.width - Style.space(48), Style.space(360))
      height: archiveSelectionColumn.implicitHeight + Style.space(28)
      anchors.centerIn: parent
      radius: Style.cornerRadius * 1.5
      color: Color.popups.background
      border.color: Color.accent
      border.width: 1
      MouseArea { anchors.fill: parent }
      ColumnLayout {
        id: archiveSelectionColumn
        anchors.fill: parent
        anchors.margins: Style.space(14)
        spacing: Style.space(10)
        Text {
          Layout.fillWidth: true
          text: "ARCHIVE SELECTED CHATS?"
          color: workspace.foreground
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Text {
          Layout.fillWidth: true
          text: "Archive " + controller.sidebarSelectedRoomIds.length + " selected chats? This can be restored later."
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          color: workspace.foreground
          font.family: workspace.fontFamily
          font.pixelSize: Style.font.body
        }
        RowLayout {
          Layout.fillWidth: true
          Item { Layout.fillWidth: true }
          QuietButton {
            glyph: "󰅖"
            label: "Cancel"
            onActivated: workspace.archiveSidebarSelectionConfirmOpen = false
          }
          QuietButton {
            glyph: "󰀼"
            label: "Archive"
            accent: true
            onActivated: {
              workspace.archiveSidebarSelectionConfirmOpen = false
              controller.archiveSidebarSelection()
            }
          }
        }
      }
    }
  }

  Timer {
    id: sidebarScrollRestoreTimer
    interval: 0
    repeat: false
    onTriggered: {
      if (workspace.sidebarRestoreContentY < 0) return
      workspace.sidebarScrollRestoreAttempts += 1
      var maxY = Math.max(0, treeList.contentHeight - treeList.height)
      treeList.contentY = Math.max(0, Math.min(workspace.sidebarRestoreContentY, maxY))
      // Keep the target while content is still growing toward it; a bounded
      // number of retries covers async delegate layout after model rebuilds.
      if (workspace.sidebarRestoreContentY <= maxY + 1 || workspace.sidebarScrollRestoreAttempts >= 6)
        workspace.sidebarRestoreContentY = -1
    }
  }

  component SectionLabel: Text {
    property string label: ""
    text: label
    color: workspace.dim
    font.family: workspace.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
    font.letterSpacing: Style.space(0.8)
  }

  component ActionButton: Rectangle {
    id: action
    property string glyph: ""
    property string hint: ""
    property bool compact: false
    property bool spinning: false
    signal activated()
    implicitWidth: Style.space(compact ? 24 : 30)
    implicitHeight: implicitWidth
    radius: Style.space(4)
    color: actionMouse.containsMouse ? workspace.surfaceStrong : "transparent"
    opacity: enabled ? 1 : 0.35
    Behavior on color { ColorAnimation { duration: 140 } }
    Text {
      anchors.centerIn: parent
      text: action.glyph
      color: workspace.foreground
      font.family: workspace.fontFamily
      font.pixelSize: action.compact ? Style.font.body : Style.font.icon
      font.bold: action.compact
      NumberAnimation on rotation {
        running: action.spinning
        from: 0
        to: 360
        duration: 900
        loops: Animation.Infinite
      }
    }
    MouseArea {
      id: actionMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      enabled: action.enabled
      onClicked: action.activated()
    }
    PanelToolTip {
      focus: false
      delay: 3000
      visible: actionMouse.containsMouse && action.hint !== ""
      text: action.hint
      fontFamily: workspace.fontFamily
    }
  }

  component QuietButton: Rectangle {
    id: quiet
    property string glyph: ""
    property string label: ""
    property string hint: ""
    property bool accent: false
    property bool spinning: false
    signal activated()
    implicitWidth: quiet.label === "" ? Style.space(28) : quietText.implicitWidth + Style.space(16)
    implicitHeight: Style.space(28)
    radius: Style.space(4)
    color: !enabled ? workspace.faint
      : (quietMouse.pressed ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.30)
      : (accent ? workspace.accentWash : (quietMouse.containsMouse ? workspace.surfaceStrong : "transparent")))
    border.color: accent ? Color.accent : workspace.divider
    border.width: accent ? 1 : 0
    opacity: enabled ? 1 : 0.45
    Behavior on color { ColorAnimation { duration: 140 } }
    Text {
      id: quietText
      anchors.centerIn: parent
      text: quiet.glyph + (quiet.label === "" ? "" : "  " + quiet.label)
      color: quiet.accent ? Color.accent : workspace.foreground
      font.family: workspace.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      font.letterSpacing: Style.space(0.4)
      NumberAnimation on rotation {
        running: quiet.spinning
        from: 0
        to: 360
        duration: 900
        loops: Animation.Infinite
      }
    }
    MouseArea {
      id: quietMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: quiet.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      enabled: quiet.enabled
      onClicked: quiet.activated()
    }
    PanelToolTip {
      focus: false
      delay: 3000
      visible: quietMouse.containsMouse && quiet.hint !== "" && quiet.label === ""
      text: quiet.hint
      fontFamily: workspace.fontFamily
    }
  }

  component SoftField: Rectangle {
    id: soft
    property string placeholder: ""
    property string value: ""
    signal edited(string text)
    implicitHeight: Style.space(38)
    radius: Style.space(4)
    color: workspace.surfaceStrong
    border.color: input.activeFocus ? Color.accent : workspace.divider
    border.width: 1
    TextInput {
      id: input
      anchors.fill: parent
      anchors.margins: Style.space(9)
      text: soft.value
      color: workspace.foreground
      selectionColor: Color.accent
      selectedTextColor: Color.background
      font.family: workspace.fontFamily
      font.pixelSize: Style.font.body
      clip: true
      onTextEdited: soft.edited(text)
    }
    Text {
      anchors.left: parent.left
      anchors.leftMargin: Style.space(9)
      anchors.verticalCenter: parent.verticalCenter
      visible: input.text === "" && !input.activeFocus
      text: soft.placeholder
      color: workspace.dim
      font.family: workspace.fontFamily
      font.pixelSize: Style.font.body
    }
  }
}
