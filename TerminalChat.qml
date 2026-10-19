import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "TerminalConnectionModel.js" as Connection
import QMLTermWidget 2.0 as Terminal
import qs.Commons

// Attach directly to this room's agent, not to herdr's whole workspace UI.
Item {
  id: terminalChat
  required property var controller
  required property var bridge
  required property string roomId
  property var eventModel: []
  property color foreground: Color.popups.text
  property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.52)
  property color surface: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.045)
  property color surfaceStrong: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.09)
  property color accentWash: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.14)
  property string fontFamily: "monospace"
  property bool active: false
  property bool allowLegacyTranscript: true
  property bool focusOnAttach: true
  property bool previewReadOnly: false
  property bool hideComposer: false
  property bool historyOpen: false
  // Set by the tile when its bounded source preview is already on screen; a
  // read-only observation error banner on top of real content is noise.
  property bool previewContentShown: false
  signal previewActivated()
  property var connection: Connection.initial()
  property bool surfacePainted: false
  property bool detailResolutionRequested: false
  property string explicitBindingRoomId: ""
  property string selectedGroupMemberKey: ""
  readonly property string error: connection.error
  readonly property bool targetResolved: active && connection.roomId === roomId && Connection.loadable(connection)
    && (!nativeGroupRoom || selectedGroupMemberKey !== "")
  readonly property bool terminalReady: connection.phase === "output"
  readonly property bool awaitingBinding: connection.phase === "unbound"
  // These names describe local PTY readiness, never verified remote attachment.
  readonly property bool terminalAttached: targetResolved && connection.started && connection.outputObserved
  // A source-side writer grant is still required for exclusive control. A user-requested
  // exact SSH attachment is ordinary shared terminal access, never a takeover.
  readonly property bool verifiedWriter: false
  readonly property bool sharedSshTerminal: remotePeerRoom && connection.target
    && connection.target.transport === "ssh" && connection.target.sessionName === "terminal"
  readonly property bool terminalInteractive: terminalAttached && (verifiedWriter || sharedSshTerminal)
    && !previewReadOnly && !nativeGroupRoom && !readOnlyPeer && !localHermesRoom
  readonly property bool terminalOwnsInput: targetResolved
  readonly property bool legacyTranscriptNeeded: active && !nativeGroupRoom && allowLegacyTranscript && !terminalAttached
    && !terminalRoom
  onTerminalAttachedChanged: if (!terminalAttached) surfacePainted = false

  readonly property var roomRecord: controller.rooms.find(function(item) { return item.id === roomId }) || null
  readonly property bool nativeGroupRoom: !!roomRecord && roomRecord.source === "hermes-native-group"
  readonly property bool remotePeerRoom: !!roomRecord && roomRecord.source === "hermes"
    && String(roomRecord.externalProfile || "").startsWith("remote::")
  readonly property bool localHermesRoom: !!roomRecord && roomRecord.source === "hermes" && !remotePeerRoom
  readonly property bool readOnlyPeer: !!connection.target && connection.target.transport === "peer"
  readonly property bool terminalRoom: {
    var room = controller.rooms.find(function(item) { return item.id === roomId })
    return !!room && room.source !== "control" && !nativeGroupRoom && !orchestratorRoom
  }
  // The orchestrator is a standing Hermes chat (Desktop-backed), never a PTY:
  // render the GUI transcript + composer instead of the terminal attach gate.
  readonly property bool orchestratorRoom: {
    var room = roomRecord
    if (!room || room.name !== "Orchestrator" || room.source !== "hermes") return false
    var project = controller.projects.find(function(item) { return item.id === room.projectId })
    return !!project && project.name === "Orchestrator"
  }
  property bool chooserOpen: false
  property int chooserAttempt: 0
  property string chooserRoomId: ""
  property bool chooserBusy: false
  property var chooserRows: []
  property string chooserSelection: ""
  property string chooserError: ""

  onVisibleChanged: {
    if (!visible) { cancelChooser(); historyOpen = false }
    else promoteVisibleDetail()
  }
  onPreviewReadOnlyChanged: promoteVisibleDetail()
  onActiveChanged: {
    if (active) focusRoom()
    else { historyOpen = false; resetConnection() }
  }
  onRoomIdChanged: {
    if (historyContent.item) historyContent.item.clearHistory()
    historyOpen = false
    resetConnection()
    explicitBindingRoomId = ""
    selectedGroupMemberKey = ""
    if (active) focusRoom()
  }
  onTerminalRoomChanged: {
    if (active && terminalRoom) focusRoom()
    else if (!terminalRoom) resetConnection()
  }
  Component.onCompleted: if (active) focusRoom()
  Connections {
    target: terminalChat.bridge
    function onConnectionChanged(online) {
      if (!online) terminalChat.historyOpen = false
      if (online && terminalChat.active) terminalChat.focusRoom()
    }
  }

  function selectGroupMember(memberKey) {
    if (!active || !nativeGroupRoom || previewReadOnly || !memberKey) return
    // Selection is deliberate; the server independently rechecks the member.
    resetConnection()
    selectedGroupMemberKey = memberKey
    focusRoom(true)
  }

  function leaveGroupMember() {
    resetConnection()
    selectedGroupMemberKey = ""
  }

  function resetConnection() {
    startupTimeout.stop()
    connection = Connection.deactivate(connection)
    detailResolutionRequested = false
    surfacePainted = false
    cancelChooser()
    sourcePreview.dismiss()
  }

  function promoteVisibleDetail() {
    if (active && visible && !previewReadOnly && !nativeGroupRoom && !targetResolved && !detailResolutionRequested)
      focusRoom(true)
  }

  function focusRoom(force) {
    if (!active || (!terminalRoom && !(nativeGroupRoom && selectedGroupMemberKey !== ""))
        || roomId === "" || chooserOpen) return
    if (!force && connection.roomId === roomId && connection.phase !== "idle"
        && connection.phase !== "unbound") return
    connection = Connection.begin(connection, roomId)
    surfacePainted = false
    var attempt = connection.attempt
    var requested = roomId
    detailResolutionRequested = visible && !previewReadOnly
    startupTimeout.attempt = connection
    startupTimeout.interval = 12000
    startupTimeout.restart()
    if (!bridge.available) {
      connection = Connection.resolve(connection, attempt, requested, { ok: false, error: "Terminal service unavailable. Retry when connected." })
      return
    }
    var memberKey = selectedGroupMemberKey
    var groupAttach = nativeGroupRoom
    var peerObserve = remotePeerRoom && terminalChat.previewReadOnly
    var action = terminalChat.previewReadOnly ? "/ensure" : "/focus"
    var url = groupAttach
      ? "/v1/tty/groups/" + encodeURIComponent(requested) + "/members/" + encodeURIComponent(memberKey) + "/terminal"
      : "/v1/tty/rooms/" + encodeURIComponent(requested) + (peerObserve ? "/observe?cols=80&rows=24" : action)
    bridge.request(groupAttach || peerObserve ? "GET" : "POST", url,
      groupAttach || peerObserve ? null : { openIfMissing: !!(terminalChat.remotePeerRoom || terminalChat.localHermesRoom) && !terminalChat.previewReadOnly }, function(value) {
      if (peerObserve) value = value && value.ok === true && value.transport === "peer"
        && value.encoding === "base64" && typeof value.frame === "string" && value.terminalId
        ? {ok:true, tty:{sessionName:"peer-observe",agentName:requested,agentStarted:true,readOnly:true,transport:"peer"}}
        : {ok:false,error:"Verified peer observation unavailable. Click the chat name for a bounded source preview."}
      if (!terminalChat) return
      if (!terminalChat.active || terminalChat.roomId !== requested
          || (groupAttach && terminalChat.selectedGroupMemberKey !== memberKey)) return
      terminalChat.connection = Connection.resolve(terminalChat.connection, attempt, requested, value)
      if (terminalChat.connection.attempt === attempt && Connection.loadable(terminalChat.connection)) {
        startupTimeout.interval = 12000
        startupTimeout.restart()
      }
    })
  }

  function openSourcePreview() {
    if (remotePeerRoom && active && roomId !== "")
      sourcePreview.openRoom(roomId, roomRecord ? roomRecord.name : "Chat")
  }

  function showChooser() {
    if (!active || !terminalRoom || previewReadOnly || remotePeerRoom) return
    chooserRoomId = roomId
    chooserOpen = true
    refreshChooser()
  }

  function refreshChooser() {
    if (!chooserOpen || chooserRoomId !== roomId) return
    var requested = chooserRoomId
    var attempt = ++chooserAttempt
    chooserBusy = true
    chooserRows = []
    chooserSelection = ""
    chooserError = ""
    if (!bridge.available) {
      chooserBusy = false
      chooserError = "Terminal service unavailable. Refresh when connected."
      return
    }
    bridge.request("GET", "/v1/tty/rooms/" + encodeURIComponent(requested) + "/terminals", null, function(value) {
      if (!terminalChat || !terminalChat.active || !terminalChat.chooserOpen
          || terminalChat.roomId !== requested || terminalChat.chooserAttempt !== attempt) return
      terminalChat.chooserBusy = false
      terminalChat.chooserRows = value.ok === true && Array.isArray(value.terminals) ? value.terminals : []
      terminalChat.chooserError = value.ok !== true ? String(value.error || "Unable to list terminals")
        : (value.errors || []).map(function(item) { return typeof item === "string" ? item : JSON.stringify(item) }).join("; ")
      if (value.binding) terminalChat.explicitBindingRoomId = requested
    })
  }

  function cancelChooser() {
    chooserAttempt++
    chooserOpen = false
    chooserBusy = false
    chooserSelection = ""
    chooserRows = []
  }

  function confirmTerminal(confirmed) {
    if (confirmed !== true || !active || !chooserOpen || chooserBusy || chooserRoomId !== roomId
        || !chooserSelection || !bridge.available) return
    var candidate = chooserRows.find(function(item) { return item.id === chooserSelection })
    if (!candidate) return
    var requested = chooserRoomId
    var selected = String(candidate.id)
    var attempt = ++chooserAttempt
    chooserBusy = true
    chooserError = ""
    // A failed/uncertain binding write must not reopen the Desktop send path.
    explicitBindingRoomId = requested
    bridge.request("POST", "/v1/tty/rooms/" + encodeURIComponent(requested) + "/binding",
      { terminalId: selected, confirmed: true }, function(value) {
      if (!terminalChat || !terminalChat.active || !terminalChat.chooserOpen
          || terminalChat.roomId !== requested || terminalChat.chooserAttempt !== attempt) return
      terminalChat.chooserBusy = false
      if (value.ok !== true) {
        terminalChat.chooserError = String(value.error || "Binding not confirmed. Refresh before trying again.")
        return
      }
      terminalChat.cancelChooser()
      terminalChat.focusRoom(true)
    })
  }

  function sessionEvent(attempt, kind) {
    if (!attempt || !active || roomId !== attempt.roomId) return
    if (nativeGroupRoom && (!selectedGroupMemberKey || connection.attempt !== attempt.attempt)) return
    connection = Connection.event(connection, attempt.attempt, attempt.roomId, kind)
  }

  Timer {
    id: startupTimeout
    property var attempt: null
    interval: 12000
    repeat: false
    onTriggered: terminalChat.sessionEvent(attempt, "timeout")
  }
  onConnectionChanged: {
    if (connection.phase === "output" || connection.phase === "failed" || connection.phase === "unbound")
      startupTimeout.stop()
  }

  function captureFrame(view) {
    if (remotePeerRoom || !terminalAttached || !view || view.width < 160 || view.height < 100
        || !controller.cacheTerminalFrame) return
    var capturedRoom = roomId
    var capturedAttempt = connection.attempt
    var capturedAt = Date.now()
    view.grabToImage(function(image) {
      if (terminalChat && terminalChat.roomId === capturedRoom && terminalChat.terminalAttached
          && terminalChat.connection.attempt === capturedAttempt)
        terminalChat.controller.cacheTerminalFrame(capturedRoom, image, capturedAt)
    })
  }

  // A new Ares room obtains its stored Hermes session only after its first
  // message. Attach the agent to the existing tab once that binding appears.
  Timer {
    interval: 1400
    repeat: true
    running: terminalChat.active && terminalChat.awaitingBinding && terminalChat.bridge.available && terminalChat.error === ""
    onTriggered: terminalChat.focusRoom()
  }

  Loader {
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: statusBar.visible ? statusBar.bottom : parent.top
    anchors.bottom: parent.bottom
    active: terminalChat.targetResolved
    sourceComponent: Component {
      Item {
        clip: terminalChat.previewReadOnly
        Terminal.QMLTermWidget {
          id: termView
          property bool shellStarted: false
          property bool startRequested: false
          property var attachedAttempt: null
          Component.onCompleted: attachedAttempt = terminalChat.connection
          opacity: terminalChat.surfacePainted ? 1 : 0.01
          Behavior on opacity { NumberAnimation { duration: 130 } }
          width: parent.width
          height: parent.height
          enabled: terminalChat.terminalInteractive
          localSelection: true
          readOnlyInput: terminalChat.previewReadOnly || terminalChat.nativeGroupRoom || terminalChat.readOnlyPeer || terminalChat.localHermesRoom || terminalChat.hideComposer || !terminalChat.terminalInteractive
          passivePreviewWheel: terminalChat.previewReadOnly
          font.pointSize: terminalChat.controller.terminalFontSize
          colorScheme: "DarkPastels"
          terminalBackground: terminalChat.controller.terminalBackgroundColor
          backgroundOpacity: terminalChat.controller.terminalBackgroundOpacity
          previewHideInput: terminalChat.previewReadOnly || terminalChat.nativeGroupRoom || terminalChat.readOnlyPeer || terminalChat.localHermesRoom || terminalChat.hideComposer || !terminalChat.terminalInteractive
          onImagePainted: {
            if (!termView.shellStarted || !terminalChat.terminalAttached) return
            if (!terminalChat.surfacePainted) {
              if (!firstPaintDelay.running) firstPaintDelay.start()
            } else if (!contentRefresh.running) contentRefresh.start()
          }
          Keys.priority: Keys.BeforeItem
          Keys.onPressed: function(event) {
            if (!(event.modifiers & Qt.ControlModifier) || (event.modifiers & (Qt.AltModifier | Qt.MetaModifier))) return
            if (event.key === Qt.Key_Plus || event.key === Qt.Key_Equal) {
              terminalChat.controller.setTerminalFontSize(terminalChat.controller.terminalFontSize + 1)
              event.accepted = true
            } else if (event.key === Qt.Key_Minus || event.key === Qt.Key_Underscore) {
              terminalChat.controller.setTerminalFontSize(terminalChat.controller.terminalFontSize - 1)
              event.accepted = true
            }
          }
          TapHandler { enabled: terminalChat.terminalInteractive && !terminalChat.hideComposer; onTapped: termView.forceActiveFocus() }
          session: Terminal.QMLTermSession {
            id: termSession
            // qmltermwidget needs the executable and argv as separate properties.
            shellProgram: terminalChat.connection.target && terminalChat.connection.target.sessionName === "peer-observe"
              ? terminalChat.controller.pluginDir + "/bin/ares-peer-observe-client"
              : terminalChat.controller.pluginDir + "/bin/ares-herdr-client"
            shellProgramArgs: termView.attachedAttempt ? Connection.argumentsFor(termView.attachedAttempt.target) : []
            onStarted: {
              termView.shellStarted = true
              terminalChat.sessionEvent(termView.attachedAttempt, "started")
            }
            onFinished: terminalChat.sessionEvent(termView.attachedAttempt, "finished")
          }
          Connections {
            target: termSession
            ignoreUnknownSignals: true
            function onOutputReceived() {
              terminalChat.sessionEvent(termView.attachedAttempt, "output")
            }
          }
          // Starting before the widget has a PTY grid leaves a dead pane.
          Timer {
            interval: 50
            running: termView.attachedAttempt !== null && !termView.startRequested && termView.width >= 160 && termView.height >= 100
            onTriggered: {
              termView.startRequested = true
              termSession.startShellProgram()
              if (terminalChat.focusOnAttach && terminalChat.terminalInteractive && !terminalChat.hideComposer) termView.forceActiveFocus()
            }
          }
          Timer {
            id: firstPaintDelay
            interval: 140
            onTriggered: {
              if (!terminalChat.terminalAttached) return
              terminalChat.surfacePainted = true
              terminalChat.captureFrame(termView)
            }
          }
          Timer {
            interval: 850
            running: terminalChat.surfacePainted && termView.visible && terminalChat.active
            onTriggered: terminalChat.captureFrame(termView)
          }
          Timer {
            id: contentRefresh
            interval: 800
            onTriggered: terminalChat.captureFrame(termView)
          }
          Timer {
            interval: 4500
            repeat: true
            running: terminalChat.surfacePainted && termView.visible && terminalChat.active
            onTriggered: terminalChat.captureFrame(termView)
          }
        }
      }
    }
  }

  Loader {
    anchors.fill: parent
    active: terminalChat.active && terminalChat.nativeGroupRoom && terminalChat.selectedGroupMemberKey === ""
    sourceComponent: Component {
      NativeGroupDetails {
        bridge: terminalChat.bridge
        roomId: terminalChat.roomId
        group: terminalChat.roomRecord ? terminalChat.roomRecord.nativeGroup || ({}) : ({})
        foreground: terminalChat.foreground
        dim: terminalChat.dim
        surface: terminalChat.surface
        fontFamily: terminalChat.fontFamily
        preview: terminalChat.previewReadOnly
        onMemberSelected: function(memberKey) { terminalChat.selectGroupMember(memberKey) }
      }
    }
  }

  // Only build the GUI transcript in an active view that actually needs it.
  // Inactive tiles and terminal rooms awaiting attach must not render it.
  Loader {
    anchors.fill: parent
    active: terminalChat.legacyTranscriptNeeded
    sourceComponent: Component {
      ChatTranscript {
        controller: terminalChat.controller
        bridge: terminalChat.bridge
        roomId: terminalChat.roomId
        eventModel: terminalChat.eventModel
        foreground: terminalChat.foreground
        dim: terminalChat.dim
        surface: terminalChat.surface
        surfaceStrong: terminalChat.surfaceStrong
        accentWash: terminalChat.accentWash
        fontFamily: terminalChat.fontFamily
      }
    }
  }

  Popup {
    id: historyPanel
    parent: Overlay.overlay
    x: parent ? (parent.width - width) / 2 : 0
    y: parent ? (parent.height - height) / 2 : 0
    width: parent ? Math.min(850, parent.width - 24) : 700
    height: parent ? Math.min(620, parent.height - 24) : 520
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    onClosed: terminalChat.historyOpen = false
    Connections {
      target: terminalChat
      function onHistoryOpenChanged() {
        if (terminalChat.historyOpen && terminalChat.active && terminalChat.visible
            && !terminalChat.previewReadOnly && (terminalChat.remotePeerRoom || terminalChat.localHermesRoom))
          historyPanel.open()
        else historyPanel.close()
      }
    }
    contentItem: Loader {
      id: historyContent
      objectName: "historyLoader"
      active: historyPanel.opened
      sourceComponent: Component {
        Rectangle {
          color: Color.popups.background
          function clearHistory() { boundedHistory.clearHistory() }
          RemoteRoomHistory {
            id: boundedHistory
            anchors.fill: parent
            bridge: terminalChat.bridge
            roomId: terminalChat.roomId
            eligible: terminalChat.active && terminalChat.visible
              && (terminalChat.remotePeerRoom || terminalChat.localHermesRoom)
              && !terminalChat.previewReadOnly && terminalChat.historyOpen
            foreground: terminalChat.foreground
            dim: terminalChat.dim
            surface: terminalChat.surface
            fontFamily: terminalChat.fontFamily
            onClosed: terminalChat.historyOpen = false
          }
        }
      }
    }
  }

  MouseArea {
    anchors.fill: parent
    visible: terminalChat.previewReadOnly && terminalChat.active
    acceptedButtons: Qt.LeftButton
    scrollGestureEnabled: false
    z: 1
    cursorShape: Qt.PointingHandCursor
    onClicked: terminalChat.previewActivated()
    onWheel: function(wheel) { wheel.accepted = false }
  }

  Rectangle {
    id: statusBar
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.top: parent.top
    height: statusColumn.implicitHeight + 12
    visible: terminalChat.active && (terminalChat.terminalRoom || (terminalChat.nativeGroupRoom && terminalChat.selectedGroupMemberKey !== ""))
      && !(terminalChat.previewReadOnly && terminalChat.previewContentShown && terminalChat.error !== "")
      && (!terminalChat.terminalAttached || terminalChat.error !== "")
    color: Color.popups.background
    z: 2
    ColumnLayout {
      id: statusColumn
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.margins: 6
      y: 6
      spacing: 4
      Text {
        Layout.fillWidth: true
        text: terminalChat.nativeGroupRoom ? "Read-only member " + terminalChat.selectedGroupMemberKey + " · "
          + Connection.status(terminalChat.connection) : Connection.status(terminalChat.connection)
          + (terminalChat.sharedSshTerminal
            ? " · Shared SSH terminal access. Another client may be typing." : "")
        textFormat: Text.PlainText
        color: terminalChat.error ? Color.urgent : terminalChat.dim
        wrapMode: Text.WordWrap
        maximumLineCount: terminalChat.previewReadOnly ? 1 : 3
        elide: Text.ElideRight
      }
      RowLayout {
        visible: !terminalChat.previewReadOnly
        Button {
          visible: terminalChat.nativeGroupRoom
          text: "Members"
          onClicked: terminalChat.leaveGroupMember()
        }

        Button {
          visible: terminalChat.remotePeerRoom && !terminalChat.targetResolved
          text: "Bounded source preview"
          onClicked: terminalChat.openSourcePreview()
        }
        Button {
          visible: !terminalChat.nativeGroupRoom && !terminalChat.remotePeerRoom
          text: "Choose terminal"
          onClicked: terminalChat.showChooser()
        }
      }
    }
  }

  MapChatPreview {
    id: sourcePreview
    anchors.fill: parent
    z: 5
    bridge: terminalChat.bridge
    sourcePreviewLabel: true
  }

  Popup {
    id: terminalChooser
    parent: Overlay.overlay
    x: parent ? (parent.width - width) / 2 : 0
    y: parent ? (parent.height - height) / 2 : 0
    width: parent ? Math.min(720, parent.width - 24) : 600
    height: parent ? Math.min(520, parent.height - 24) : 440
    modal: true
    focus: true
    visible: terminalChat.chooserOpen
    closePolicy: Popup.CloseOnEscape
    onClosed: if (terminalChat.chooserOpen) terminalChat.cancelChooser()
    background: Rectangle { color: Color.popups.background; border.color: Color.accent; radius: 6 }
    contentItem: ColumnLayout {
      spacing: 8
      Text {
        text: "Choose an existing terminal"
        color: terminalChat.foreground
        font.bold: true
      }
      Text {
        Layout.fillWidth: true
        text: "Select a terminal, then confirm. This binds the room; it does not start an agent or send a prompt."
        color: terminalChat.dim
        wrapMode: Text.WordWrap
      }
      Text {
        Layout.fillWidth: true
        visible: text !== ""
        text: terminalChat.chooserBusy ? "Loading…" : terminalChat.chooserError
        textFormat: Text.PlainText
        color: terminalChat.chooserError ? Color.urgent : terminalChat.dim
        wrapMode: Text.WordWrap
      }
      ListView {
        id: terminalList
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        spacing: 4
        model: terminalChat.chooserRows
        ScrollBar.vertical: ScrollBar {}
        delegate: ItemDelegate {
          required property var modelData
          width: terminalList.width
          highlighted: terminalChat.chooserSelection === modelData.id
          enabled: !terminalChat.chooserBusy
          onClicked: terminalChat.chooserSelection = String(modelData.id)
          contentItem: Text {
            text: "Host: " + String(modelData.host || "unknown")
              + " · Workspace: " + String(modelData.workspace || "—")
              + " · Tab: " + String(modelData.label || "—")
              + "\nSession: " + String(modelData.herdrSession || "—")
              + " · Pane: " + String(modelData.paneId || "—")
              + " · Kind: " + String(modelData.agentKind || "unknown")
              + " · Status: " + String(modelData.status || "unknown")
            textFormat: Text.PlainText
            color: terminalChat.foreground
            wrapMode: Text.WordWrap
          }
        }
        Text {
          anchors.centerIn: parent
          visible: !terminalChat.chooserBusy && terminalChat.chooserRows.length === 0
          text: "No existing terminals found."
          color: terminalChat.dim
        }
      }
      RowLayout {
        Layout.fillWidth: true
        Button { text: "Refresh"; enabled: !terminalChat.chooserBusy; onClicked: terminalChat.refreshChooser() }
        Item { Layout.fillWidth: true }
        Button { text: "Cancel"; onClicked: terminalChat.cancelChooser() }
        Button {
          text: "Confirm selection"
          enabled: !terminalChat.chooserBusy && terminalChat.chooserSelection !== ""
          onClicked: terminalChat.confirmTerminal(true)
        }
      }
    }
  }
}
