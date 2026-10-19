import QtQuick
import QtQuick.Dialogs
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "TerminalFrameCache.js" as TerminalFrameCache
import "ControlMapModel.js" as ControlMapModel
import "RoomSearchModel.js" as RoomSearchModel

Panel {
  id: root
  moduleName: "io.github.avillagran.omarchy-ares"
  ipcTarget: "io.github.avillagran.omarchy-ares"
  manageIpc: false

  property var anchorItem: null
  property var bar: null
  property var projects: []
  property var controlTileOrder: []
  property var controlMapCategories: ({groups: [], assignments: ({})})
  property bool categoriesSaving: false
  property var rooms: []
  property var controlItems: []
  property var controlLogs: ({})
  property int layaExampleCount: 0
  property string controlRoomId: ""
  property string controlMode: "visual"
  property var events: []
  property var artifacts: []
  property var bots: []
  property var peers: []
  property var quotaWindows: []
  property var quotaProviders: []
  property bool homeQuotaVisible: true
  property bool homeQuotaCollapsed: false
  property string panelSize: "medium"
  property bool sidebarCollapsed: false
  property int sidebarWidth: 200
  property int sidebarWidthCycleBase: 200
  property bool sidebarWidthSaving: false
  property string searchShortcut: "Ctrl+Space"
  property bool searchOpen: false
  property var searchReturnFocus: null
  property string pendingSearchShortcut: ""
  property bool searchPreferenceInFlight: false
  property int terminalFontSize: 10
  property string terminalBackgroundRole: "popups.background"
  property real terminalBackgroundOpacity: 1
  property bool terminalBlur: false
  property bool terminalPreviewReadOnly: true
  property var terminalFrameCache: ({ frames: ({}), order: [] })
  property var pendingTerminalPreferences: ({})
  property bool terminalPreferencesInFlight: false
  readonly property color terminalBackgroundColor: {
    if (terminalBackgroundRole === "background") return Color.background
    if (terminalBackgroundRole === "accent") return Color.accent
    if (terminalBackgroundRole === "muted") return Color.muted
    return Color.popups.background
  }
  readonly property real panelSizeFraction: panelSize === "small" ? 0.30 : (panelSize === "large" ? 0.70 : 0.50)
  property var roomDrafts: ({})
  property var roomDraftImages: ({})
  property var draftPendingByRoom: ({})
  property var draftWritesInFlight: ({})
  property bool draftRestoring: false
  property var imageStagingQueue: []
  property bool imageStagingBusy: false
  property var hiddenHomeQuotaProviders: []
  property var models: []
  readonly property var visibleQuotaProviders: quotaProviders.filter(function(provider) {
    return hiddenHomeQuotaProviders.indexOf(String(provider.provider || "")) < 0
  })
  property var archivedRooms: []
  property string section: "home"
  property string selectedProjectId: ""
  property string selectedRoomId: ""
  property var sidebarSelectedRoomIds: []
  property string sidebarSelectionAnchorId: ""
  property string sidebarSelectionCursorId: ""
  onSelectedRoomIdChanged: {
    persistDraftNow()
    markRoomSeen(selectedRoomId)
    roomSendBotId = ""
    restoreSelectedDraft()
  }
  property string pendingDraftRoomId: ""
  property string pendingDraftText: ""
  property string roomSendBotId: ""
  property var roomSendKeys: ({})
  onRoomTextChanged: if (!draftRestoring) scheduleDraftSave()
  property var expandedProjects: ({})
  property var collapsedHosts: ({})
  property bool recentExpanded: false
  property bool projectsCollapsed: false
  property var seenRoomCursors: ({})
  property int navigationRevision: 0
  readonly property int recentRoomLimit: 6
  property string projectName: ""
  property string projectPath: ""
  property string roomName: ""
  property string roomBotId: "default"
  property string roomText: ""
  property bool controlDraftsLoaded: false
  property var pendingImages: [] // [{path, url, name}] — queued composer attachments
  property string imageDialogRoomId: ""
  property string errorText: ""
  property bool sendingImage: false
  property bool projectFormOpen: false
  property bool roomFormOpen: false
  property bool extendedMode: false
  property bool editMode: false
  property string extendedViewMode: "overview"
  property string extendedRoomId: ""
  property var extendedMapSlots: []
  readonly property string viewCycleHint: "Compact → Extended → Tiles · Tab / Shift+Tab"
  property bool opened: panel.open
  property double presenceNow: Date.now()
  onOpenedChanged: {
    if (opened) presenceNow = Date.now()
    else closeSearch(false)
  }
  readonly property bool working: (bridge.state.executions || []).some(function(item) { return item.status === "queued" || item.status === "running" })
  readonly property var activeRooms: rooms.filter(function(room) { return room.status === "active" && room.source !== "control" }).sort(function(a, b) {
    return roomActivity(b) - roomActivity(a)
  })
  readonly property var extendedRoom: rooms.find(function(room) { return room.id === root.extendedRoomId }) || null
  readonly property string pluginDir: (typeof manifest !== "undefined" && manifest.__sourceDir)
    ? manifest.__sourceDir.replace(/\/$/, "")
    : Qt.resolvedUrl(".").toString().replace("file://", "")
  readonly property string serverPath: pluginDir + "/bin/ares-server"
  readonly property var selectedProject: projects.find(function(project) { return project.id === root.selectedProjectId }) || null
  readonly property var selectedRoom: rooms.find(function(room) { return room.id === root.selectedRoomId }) || null
  readonly property var roomEvents: reconcileEvents(events.filter(function(event) { return event.roomId === root.selectedRoomId }))
  readonly property var projectRooms: roomsForProject(root.selectedProjectId)
  readonly property var navigationRows: {
    var revision = navigationRevision
    return buildNavigationRows()
  }
  readonly property var displayEvents: roomEvents.filter(function(event) {
    var body = eventBody(event).trim()
    if (String(event.kind || "").endsWith(".delta") && body.length < 2) return false
    return body !== "" || String(event.kind || "").indexOf("tool.") === 0 || String(event.kind || "").indexOf("execution.") === 0
  })
  function reconcileEvents(items) {
    var output = []
    var positions = ({})
    var ordered = items.slice().sort(eventChronology)
    for (var index = 0; index < ordered.length; ++index) {
      var event = ordered[index]
      var execution = String(event.executionId || "")
      var payload = event.payload || ({})
      var kind = String(event.kind || "")
      var text = String(payload.text || payload.delta || "")
      if (kind === "message.delta" && execution) {
        var assistantKey = "assistant:" + execution
        var assistant = Object.assign({}, event, { kind: "message.assistant", payload: Object.assign({}, payload, { text: text }) })
        if (positions[assistantKey] !== undefined) output[positions[assistantKey]] = assistant
        else { positions[assistantKey] = output.length; output.push(assistant) }
        continue
      }
      if (kind === "message.assistant" && execution) {
        var finalKey = "assistant:" + execution
        if (positions[finalKey] !== undefined) output[positions[finalKey]] = event
        else { positions[finalKey] = output.length; output.push(event) }
        continue
      }
      if (kind.endsWith(".delta") && execution) {
        if (text.trim() === "") continue
        var deltaKey = kind + ":" + execution
        var merged = text
        if (positions[deltaKey] !== undefined) {
          var previousPayload = output[positions[deltaKey]].payload || ({})
          var previous = String(previousPayload.text || "")
          merged = text.indexOf(previous) === 0 ? text : previous + text
        }
        var deltaEvent = Object.assign({}, event, { payload: Object.assign({}, payload, { text: merged }) })
        if (positions[deltaKey] !== undefined) output[positions[deltaKey]] = deltaEvent
        else { positions[deltaKey] = output.length; output.push(deltaEvent) }
        continue
      }
      if (kind === "execution.completed" && execution) {
        var completedKey = "assistant:" + execution
        var assistantText = positions[completedKey] === undefined ? "" : String((output[positions[completedKey]].payload || ({})).text || "")
        if (text === assistantText) event = Object.assign({}, event, { payload: Object.assign({}, payload, { text: "Completed" }) })
      }
      output.push(event)
    }
    return output.sort(eventChronology)
  }
  function eventChronology(a, b) {
    var timeDelta = Number(a.createdAt || 0) - Number(b.createdAt || 0)
    if (timeDelta !== 0) return timeDelta
    var orderDelta = Number((a.payload || ({})).sourceOrder === undefined ? 1 : (a.payload || ({})).sourceOrder)
      - Number((b.payload || ({})).sourceOrder === undefined ? 1 : (b.payload || ({})).sourceOrder)
    return orderDelta !== 0 ? orderDelta : Number(a.sequence || 0) - Number(b.sequence || 0)
  }
  function sameData(current, incoming) {
    return JSON.stringify(current || []) === JSON.stringify(incoming || [])
  }

  function open() { panel.open = true; reload(); loadDrafts(); loadModels() }
  function openSearch() {
    if (!opened || searchOpen) return
    var window = keyCatcher.Window.window
    searchReturnFocus = window ? window.activeFocusItem : null
    searchOpen = true
  }
  function closeSearch(restoreFocus) {
    var previous = searchReturnFocus
    searchReturnFocus = null
    searchOpen = false
    if (restoreFocus) Qt.callLater(function() {
      if (root.opened && !root.searchOpen && previous && previous.visible && previous.enabled)
        previous.forceActiveFocus()
    })
  }
  function toggleSearch() {
    if (searchOpen) closeSearch(true)
    else openSearch()
  }
  function activateSearchRoom(candidate) {
    var room = candidate && rooms.find(function(item) { return item.id === candidate.id })
    if (!room) return
    closeSearch(false)
    section = "home"
    if (extendedMode) focusExtendedRoom(room)
    else selectRecentRoom(room)
  }
  function cacheTerminalFrame(roomId, image, capturedAt) {
    terminalFrameCache = TerminalFrameCache.store(terminalFrameCache, roomId, image, capturedAt, 24)
  }
  function close() { persistDraftNow(); panel.open = false; extendedMode = false; extendedViewMode = "overview"; section = "home" }
  function toggle() { if (panel.open) close(); else open() }
  function reload() { bridge.reload() }
  function openExtended(mapSlots) {
    extendedMapSlots = Array.isArray(mapSlots) ? mapSlots : []
    var room = selectedRoom && selectedRoom.status === "active" ? selectedRoom
      : (extendedRoom && extendedRoom.status === "active" ? extendedRoom
         : (activeRooms.length > 0 ? activeRooms[0] : null))
    if (room) {
      selectRoom(room)
      extendedRoomId = room.id
    }
    extendedMode = true
    extendedViewMode = "grown"
    panel.open = true
  }
  function closeExtended() { extendedMode = false }
  function showExtendedOverview() { extendedViewMode = "overview" }
  function cycleView(direction) {
    var current = !extendedMode ? 0 : (extendedViewMode === "overview" ? 2 : 1)
    var next = (current + (direction < 0 ? -1 : 1) + 3) % 3
    if (next === 0) {
      closeExtended()
      return
    }
    openExtended()
    if (next === 2) showExtendedOverview()
  }
  function growExtendedRoom(room) {
    if (!room) return
    selectRoom(room)
    extendedRoomId = room.id
    extendedViewMode = "grown"
  }
  function focusExtendedRoom(room) {
    if (!room) return
    selectRoom(room)
    extendedRoomId = room.id
    extendedViewMode = "focus"
  }
  function roomActivity(room) {
    if (!room) return 0
    if (room.source === "hermes")
      return Math.max(Number(room.externalActivityAt || 0), Number(room.externalUpdatedAt || 0))
    return Math.max(Number(room.updatedAt || 0), Number(room.createdAt || 0))
  }
  function roomActivityAge(room) {
    return RoomSearchModel.activityLabel(room, Date.now() / 1000)
  }
  function roomsForProject(projectId) {
    return rooms.filter(function(room) { return room.projectId === projectId }).sort(function(a, b) {
      return roomActivity(b) - roomActivity(a)
    })
  }
  function sortedRooms() {
    return rooms.filter(function(room) { return room.source !== "control" }).sort(function(a, b) { return roomActivity(b) - roomActivity(a) })
  }
  function projectActivity(project) {
    var childRooms = roomsForProject(project.id)
    return childRooms.length > 0 ? roomActivity(childRooms[0]) : Number(project.createdAt || 0)
  }
  function sortedProjects() {
    var projectIdsWithChats = Object.create(null)
    for (var index = 0; index < rooms.length; ++index) {
      var room = rooms[index]
      if (room.source !== "control") projectIdsWithChats[room.projectId] = true
    }
    return projects.filter(function(project) {
      return project.node !== "control" && projectIdsWithChats[project.id] === true
    }).sort(function(a, b) { return projectActivity(b) - projectActivity(a) })
  }
  function projectsForNode(nodeId) {
    return sortedProjects().filter(function(project) { return String(project.node || "local") === nodeId })
  }
  function sortedNodes() {
    var nodes = []
    var seen = ({})
    var orderedProjects = sortedProjects()
    for (var index = 0; index < orderedProjects.length; ++index) {
      var nodeId = String(orderedProjects[index].node || "local")
      if (seen[nodeId]) continue
      seen[nodeId] = true
      nodes.push(nodeId)
    }
    return nodes
  }
  function isHostExpanded(nodeId) { return collapsedHosts[nodeId] !== true }
  function setHostExpanded(nodeId, expanded) {
    var next = Object.assign({}, collapsedHosts)
    if (expanded) delete next[nodeId]
    else next[nodeId] = true
    collapsedHosts = next
    navigationRevision += 1
  }
  function toggleHost(nodeId) { setHostExpanded(nodeId, !isHostExpanded(nodeId)) }
  function toggleNavigationSection(label) {
    if (label === "RECENT") recentExpanded = !recentExpanded
    else if (label === "PROJECTS") projectsCollapsed = !projectsCollapsed
    navigationRevision += 1
  }
  function isProjectExpanded(projectId) { return expandedProjects[projectId] === true }
  function setProjectExpanded(projectId, expanded) {
    // Multi-expand: expanding one project must not collapse the others.
    var next = Object.assign({}, expandedProjects)
    if (expanded) next[projectId] = true
    else delete next[projectId]
    expandedProjects = next
    navigationRevision += 1
  }
  function buildNavigationRows() {
    var rows = []
    var allRecentRooms = sortedRooms()
    var recentRooms = allRecentRooms.slice(0, recentExpanded ? allRecentRooms.length : recentRoomLimit)
    if (allRecentRooms.length > 0) {
      rows.push({ kind: "section", label: "RECENT", expanded: recentExpanded, count: allRecentRooms.length })
      for (var recentIndex = 0; recentIndex < recentRooms.length; ++recentIndex) {
        var recentRoom = recentRooms[recentIndex]
        var recentProject = projects.find(function(project) { return project.id === recentRoom.projectId })
        if (recentProject)
          rows.push({ kind: "room", project: recentProject, room: recentRoom, direct: true, recent: true, depth: 0 })
      }
    }
    rows.push({ kind: "section", label: "PROJECTS", expanded: !projectsCollapsed, count: sortedProjects().length })
    if (projectsCollapsed) return rows
    var nodes = sortedNodes()
    for (var nodeIndex = 0; nodeIndex < nodes.length; ++nodeIndex) {
      var nodeId = nodes[nodeIndex]
      var hostProjects = projectsForNode(nodeId)
      var hostExpanded = isHostExpanded(nodeId)
      rows.push({ kind: "host", nodeId: nodeId, projectCount: hostProjects.length, expanded: hostExpanded, depth: 0 })
      if (!hostExpanded) continue
      for (var projectIndex = 0; projectIndex < hostProjects.length; ++projectIndex) {
        var project = hostProjects[projectIndex]
        var childRooms = roomsForProject(project.id)
        var expanded = isProjectExpanded(project.id)
        rows.push({ kind: "project", project: project, roomCount: childRooms.length, expanded: expanded, depth: 1 })
        if (!expanded) continue
        for (var roomIndex = 0; roomIndex < childRooms.length; ++roomIndex)
          rows.push({ kind: "room", project: project, room: childRooms[roomIndex], direct: false, depth: 2 })
      }
    }
    return rows
  }
  function selectProject(projectId) {
    selectedProjectId = projectId
    var project = projects.find(function(item) { return item.id === projectId })
    if (project) setHostExpanded(String(project.node || "local"), true)
    setProjectExpanded(projectId, true)
    var first = roomsForProject(projectId)[0]
    selectedRoomId = first ? first.id : ""
  }
  function toggleProject(projectId) {
    var expanded = !isProjectExpanded(projectId)
    setProjectExpanded(projectId, expanded)
    selectedProjectId = projectId
    if (expanded && (!selectedRoom || selectedRoom.projectId !== projectId)) {
      var first = roomsForProject(projectId)[0]
      selectedRoomId = first ? first.id : ""
    }
  }
  function selectRoom(room) {
    if (!room) return
    selectedProjectId = room.projectId
    selectedRoomId = room.id
    markRoomSeen(room.id)
    setProjectExpanded(room.projectId, true)
  }
  function selectRecentRoom(room) {
    if (!room) return
    selectedProjectId = room.projectId
    selectedRoomId = room.id
    markRoomSeen(room.id)
    var project = projects.find(function(item) { return item.id === room.projectId })
    if (project) setHostExpanded(String(project.node || "local"), true)
  }
  function visibleSidebarRoomIds() {
    var ids = []
    var seen = ({})
    var rows = navigationRows
    for (var index = 0; index < rows.length; ++index) {
      var row = rows[index]
      if (!row || row.kind !== "room" || !row.room || seen[row.room.id]) continue
      seen[row.room.id] = true
      ids.push(row.room.id)
    }
    return ids
  }
  function sidebarRangeIds(firstId, lastId) {
    var ids = visibleSidebarRoomIds()
    var first = ids.indexOf(firstId)
    var last = ids.indexOf(lastId)
    if (first < 0 || last < 0) return [lastId]
    return ids.slice(Math.min(first, last), Math.max(first, last) + 1)
  }
  function selectSidebarRoom(room, extend, recent) {
    if (!room) return
    if (extend) {
      if (!sidebarSelectionAnchorId) sidebarSelectionAnchorId = sidebarSelectionCursorId || selectedRoomId || room.id
      sidebarSelectedRoomIds = sidebarRangeIds(sidebarSelectionAnchorId, room.id)
    } else {
      sidebarSelectedRoomIds = [room.id]
      sidebarSelectionAnchorId = room.id
    }
    sidebarSelectionCursorId = room.id
    if (recent) selectRecentRoom(room)
    else selectRoom(room)
  }
  function extendSidebarSelection(delta) {
    var ids = visibleSidebarRoomIds()
    if (ids.length === 0) return
    var currentId = sidebarSelectionCursorId || selectedRoomId || ids[0]
    var at = ids.indexOf(currentId)
    at = Math.max(0, Math.min(ids.length - 1, (at < 0 ? 0 : at) + delta))
    var room = rooms.find(function(item) { return item.id === ids[at] })
    selectSidebarRoom(room, true, false)
  }
  function openPeers() {
    extendedMode = false
    section = "peers"
    open()
  }
  function openControlRoom() {
    extendedMode = false
    section = "control"
    open()
    if (bridge.available) ensureControlRoom()
  }
  function ensureControlRoom() {
    bridge.request("POST", "/v1/control/ensure", {}, function(value) {
      if (!value.ok || !value.room) { errorText = value.error || "Control Room is unavailable"; return }
      controlRoomId = value.room.id
      bridge.reload()
      refreshControlItems()
      refreshLayaTraining()
    })
  }
  function orchestratorRoom() {
    for (var i = 0; i < rooms.length; i++) {
      var room = rooms[i]
      if (room.name !== "Orchestrator") continue
      for (var p = 0; p < projects.length; p++) {
        if (projects[p].id === room.projectId && projects[p].name === "Orchestrator") return room
      }
    }
    return null
  }
  function openOrchestrator() {
    extendedMode = false
    section = "home"
    open()
    if (bridge.available) { ensureOrchestratorRoom(); return }
    var room = orchestratorRoom()
    if (room) selectRoom(room)
  }
  function ensureOrchestratorRoom() {
    bridge.request("POST", "/v1/orchestrator/ensure", {}, function(value) {
      if (!value.ok || !value.room) { errorText = value.error || "Orchestrator is unavailable"; return }
      bridge.reload()
      selectRoom(value.room)
    })
  }
  function refreshControlItems() {
    bridge.request("GET", "/v1/control", null, function(value) {
      if (!value.ok) { errorText = value.error || "Could not load Control Room"; return }
      var next = value.items || []
      if (!sameData(controlItems, next)) controlItems = next
      if (!sameData(controlLogs, value.logs || ({}))) controlLogs = value.logs || ({})
    })
  }
  function refreshLayaTraining() {
    bridge.request("GET", "/v1/control/training", null, function(value) {
      if (!value.ok) { errorText = value.error || "Could not load Laya training data"; return }
      layaExampleCount = (value.examples || []).length
    })
  }
  function setProjectMapColor(projectId, color) {
    bridge.request("POST", "/v1/projects/" + encodeURIComponent(projectId) + "/map-color",
      { color: color }, function(value) {
        if (!value.ok || !value.project) { errorText = value.error || "Could not save project color"; return }
        errorText = ""
        bridge.reload()
      })
  }
  function setProjectMapPosition(projectId, x, y) {
    bridge.request("POST", "/v1/projects/" + encodeURIComponent(projectId) + "/map-position",
      { x: x, y: y }, function(value) {
        if (!value.ok || !value.project) { errorText = "Could not move project block"; return }
        errorText = ""
        bridge.reload()
      })
  }
  function setControlTileOrder(ids) {
    bridge.request("POST", "/v1/preferences", { controlTileOrder: ids }, function(value) {
      if (!value.ok || !value.preferences) { errorText = value.error || "Could not reorder project tiles"; return }
      controlTileOrder = value.preferences.controlTileOrder || []
      errorText = ""
    })
  }
  function setControlMapCategories(next) {
    if (categoriesSaving) return
    categoriesSaving = true
    bridge.request("POST", "/v1/preferences", { controlMapCategories: next }, function(value) {
      categoriesSaving = false
      if (!value.ok || !value.preferences) { errorText = value.error || "Could not save map categories"; return }
      controlMapCategories = value.preferences.controlMapCategories || ({groups: [], assignments: ({})})
      errorText = ""
    })
  }
  function toggleControlIncluded(room) {
    if (!room || room.source === "control") return
    bridge.request("POST", "/v1/rooms/" + encodeURIComponent(room.id) + "/control",
      { included: room.controlIncluded !== true }, function(value) {
        if (!value.ok) { errorText = value.error || "Could not change inclusion"; return }
        bridge.reload()
        refreshControlItems()
      })
  }
  function recordControlDecision(roomId, action, note) {
    if (!note || !note.trim()) { errorText = "Write a decision note first"; return }
    bridge.request("POST", "/v1/control/decisions", { roomId: roomId, action: action, note: note }, function(value) {
      if (!value.ok) { errorText = value.error || "Could not save decision"; return }
      bridge.reload()
      refreshControlItems()
      if (action === "propose-prompt") stageControlInstruction(roomId, note)
    })
  }
  function stageControlInstruction(roomId, note) {
    var room = rooms.find(function(item) { return item.id === roomId && item.status === "active" && item.source !== "control" })
    if (!room) { errorText = "This room is no longer active"; return }
    if (!controlDraftsLoaded) { errorText = "Wait for drafts to finish loading"; return }
    if (!note || !note.trim()) { errorText = "Write an instruction first"; return }
    if (draftForRoom(roomId).trim() || (selectedRoomId === roomId && roomText.trim())) {
      errorText = "This chat already has a draft; open it before replacing anything"
      return
    }
    section = "home"
    selectRoom(room)
    draftRestoring = true
    roomText = note.trim()
    draftRestoring = false
    scheduleDraftSave()
    errorText = "Instruction staged as a draft. Review it in the chat before sending."
  }
  function createControlWorkRoom(projectId, name, sourceRoomId) {
    if (!projectId || !name || !name.trim()) { errorText = "Select a project and name the work room"; return }
    bridge.request("POST", "/v1/rooms", { projectId: projectId, name: name.trim(), participants: ["default"] }, function(value) {
      if (!value.ok || !value.room) { errorText = value.error || "Could not start the work room"; return }
      var created = value.room
      if (sourceRoomId) recordControlDecision(sourceRoomId, "review-option", "Started work in " + created.name)
      bridge.reload()
      section = "home"
      selectedProjectId = created.projectId
      selectedRoomId = created.id
    })
  }
  function peerForNode(nodeId) {
    return peers.find(function(peer) { return peer.id === nodeId }) || null
  }
  function peerBadgeForNode(nodeId) {
    var peer = peerForNode(nodeId)
    return peer ? peer.name : (nodeId === "local" ? "LOCAL" : String(nodeId || "").toUpperCase())
  }
  function peerColor(role) {
    if (role === "urgent") return Color.urgent
    if (role === "muted") return Color.muted
    if (role === "success") return Color.pick("notifications.success", Color.accent)
    if (role === "warning") return Color.pick("notifications.warning", Color.urgent)
    if (role === "info") return Color.pick("notifications.info", Color.popups.text)
    return Color.accent
  }
  function applyState(data) {
    var nextProjects = data.projects || []
    var nextRooms = data.rooms || []
    var nextEvents = data.events || []
    var nextArtifacts = data.artifacts || []
    var nextBots = data.bots || []
    var nextPeers = data.peers || []
    var nextQuotas = data.quotas || []
    var nextQuotaProviders = data.quotaProviders || []
    var nextArchivedRooms = data.archivedRooms || []
    if (!sameData(projects, nextProjects)) projects = nextProjects
    if (!sameData(rooms, nextRooms)) rooms = nextRooms
    var special = nextRooms.find(function(item) { return item.source === "control" })
    if (special) controlRoomId = special.id
    if (!sameData(events, nextEvents)) events = nextEvents
    if (!sameData(artifacts, nextArtifacts)) artifacts = nextArtifacts
    if (!sameData(bots, nextBots)) bots = nextBots
    if (!sameData(peers, nextPeers)) peers = nextPeers
    if (!sameData(quotaWindows, nextQuotas)) quotaWindows = nextQuotas
    if (!sameData(quotaProviders, nextQuotaProviders)) quotaProviders = nextQuotaProviders
    var preferences = data.preferences || ({})
    if (!sameData(controlTileOrder, preferences.controlTileOrder || [])) controlTileOrder = preferences.controlTileOrder || []
    if (!categoriesSaving && !sameData(controlMapCategories, preferences.controlMapCategories || ({groups: [], assignments: ({})})))
      controlMapCategories = preferences.controlMapCategories || ({groups: [], assignments: ({})})
    homeQuotaVisible = preferences.homeQuotaVisible !== false
    homeQuotaCollapsed = preferences.homeQuotaCollapsed === true
    hiddenHomeQuotaProviders = preferences.hiddenHomeQuotaProviders || []
    panelSize = preferences.panelSize === "small" || preferences.panelSize === "large" ? preferences.panelSize : "medium"
    sidebarCollapsed = preferences.sidebarCollapsed === true
    if (!sidebarWidthSaving) {
      sidebarWidth = preferences.sidebarWidth >= 120 && preferences.sidebarWidth <= 480 ? preferences.sidebarWidth : 200
      sidebarWidthCycleBase = sidebarWidth
    }
    if (!sameData(archivedRooms, nextArchivedRooms)) archivedRooms = nextArchivedRooms
    applySearchPreference(preferences)
    applyTerminalAppearance(preferences)
    baselineNewRooms()
    markRoomSeen(selectedRoomId)
    if (!bots.some(function(bot) { return bot.id === roomBotId })) roomBotId = bots.length > 0 ? bots[0].id : "default"
    if (!projects.some(function(project) { return project.id === selectedProjectId }) && projects.length > 0) {
      var recent = sortedRooms()[0]
      if (recent) selectRecentRoom(recent)
    }
    if (selectedRoomId !== "" && !selectedRoom) selectedRoomId = ""
    errorText = bridge.error
  }
  function createProject() {
    if (projectName.trim() === "" || projectPath.trim() === "") { errorText = "Name and existing code path are required"; return }
    bridge.request("POST", "/v1/projects", { name: projectName, path: projectPath, nodeId: "local" }, function(value) {
      if (!value.ok) { errorText = value.error; return }
      projectFormOpen = false; projectName = ""; projectPath = ""; bridge.reload()
    })
  }
  function createRoom() {
    if (!selectedProject) { errorText = "Select a project before creating a room"; return }
    if (roomName.trim() === "") { errorText = "Room name is required"; return }
    bridge.request("POST", "/v1/rooms", { projectId: selectedProject.id, name: roomName, participants: [roomBotId] }, function(value) {
      if (!value.ok) { errorText = value.error; return }
      roomFormOpen = false; roomName = ""; selectedRoomId = value.room.id; bridge.reload()
    })
  }
  function appendLocalEvent(throughTerminal, confirmedEmptyComposer) {
    if (!selectedProject || !selectedRoom || sendingImage) return
    if (selectedRoom.source === "hermes-native-group") {
      errorText = "Native bot group discovery is read-only. Your draft was not sent."
      return
    }
    if (roomText.trim() === "" && pendingImages.length === 0) return
    if (throughTerminal && pendingImages.length > 0) {
      errorText = "Sending images from Edit to Herdr is not supported. Nothing was sent; your text and images are kept in the draft. Use the native CLI to attach and send images."
      return
    }
    if (throughTerminal && confirmedEmptyComposer !== true) {
      errorText = "Review the visible terminal input and confirm it is empty before sending."
      return
    }
    if (pendingImages.some(function(image) { return image.uploading })) {
      errorText = "Wait for images to finish saving"
      return
    }
    if (pendingImages.some(function(image) { return image.error })) {
      errorText = "Remove and reattach images that failed to save"
      return
    }
    var targetRoom = selectedRoom
    var targetRoomId = targetRoom.id
    var draftText = roomText
    var images = pendingImages.slice()
    var imageCount = images.length
    var pending = draftText.trim() !== "" ? draftText
      : imageCount > 1 ? "Please inspect the attached images." : "Please inspect the attached image."
    var sendBot = sendBotForRoom(targetRoom)
    var sendBotId = sendBot ? sendBot.id : (targetRoom.participants && targetRoom.participants[0]) || "default"
    var signature = JSON.stringify({ text: pending, images: images.map(function(image) { return image.path }), botId: sendBotId })
    var previous = roomSendKeys[targetRoomId]
    var key = previous && previous.signature === signature ? previous.key
      : "qml-send-" + Date.now() + "-" + Math.random().toString(16).slice(2)
    roomSendKeys = Object.assign({}, roomSendKeys, { [targetRoomId]: { signature: signature, key: key } })
    sendingImage = true
    var finishSend = function(artifactIds) {
      var onSent = function(value) {
        sendingImage = false
        if (!value.ok || (throughTerminal && value.accepted !== true)) {
          errorText = value.error || "Terminal delivery was not confirmed. Draft kept; check the terminal before retrying."
          return
        }
        if (roomSendKeys[targetRoomId] && roomSendKeys[targetRoomId].key === key) {
          var keys = Object.assign({}, roomSendKeys)
          delete keys[targetRoomId]
          roomSendKeys = keys
        }
        if (selectedRoomId === targetRoomId && roomText === draftText
            && pendingImages.length === images.length
            && pendingImages.every(function(image, index) { return image.path === images[index].path })) {
          draftRestoring = true
          roomText = ""
          draftRestoring = false
          clearSelectedImage()
          clearDraft(targetRoomId)
        }
        bridge.reload()
      }
      if (throughTerminal) bridge.sendTerminal(targetRoom, pending, artifactIds, onSent, key, confirmedEmptyComposer)
      else bridge.send(targetRoom, pending, artifactIds, onSent, sendBotId, key)
    }
    if (imageCount === 0) { finishSend([]); return }
    // Upload attachments one at a time, then send a single message with all.
    var artifactIds = []
    var index = 0
    var uploadNext = function() {
      if (index >= imageCount) { finishSend(artifactIds); return }
      if (images[index].artifactId) {
        artifactIds.push(images[index].artifactId)
        index += 1
        uploadNext()
        return
      }
      bridge.uploadImage(targetRoom, images[index].path, function(value) {
        if (!value.ok || !value.artifact) {
          sendingImage = false
          errorText = value.error || "Image upload failed"
          return
        }
        artifactIds.push(value.artifact.id)
        index += 1
        uploadNext()
      })
    }
    uploadNext()
  }
  function chooseImage() { imageDialogRoomId = selectedRoomId; imageDialog.open() }
  function imagesForRoom(roomId) {
    return roomId && roomDraftImages[roomId] ? roomDraftImages[roomId] : []
  }
  function restoreSelectedDraft() {
    draftRestoring = true
    roomText = draftForRoom(selectedRoomId)
    pendingImages = imagesForRoom(selectedRoomId)
    draftRestoring = false
  }
  function setRoomImages(roomId, images) {
    var next = Object.assign({}, roomDraftImages)
    next[roomId] = images
    roomDraftImages = next
    if (selectedRoomId === roomId) pendingImages = images
    queueDraftSave(roomId, draftForRoom(roomId), images)
  }
  function addPendingImage(path, targetRoomId) {
    var room = targetRoomId ? rooms.find(function(item) { return item.id === targetRoomId }) : selectedRoom
    if (!room) return
    var token = "pending-" + Date.now() + "-" + Math.random().toString(16).slice(2)
    var item = { path: path, url: "file://" + encodeURI(path),
      name: path.substring(path.lastIndexOf("/") + 1), token: token, uploading: true }
    setRoomImages(room.id, imagesForRoom(room.id).concat([item]))
    imageStagingQueue = imageStagingQueue.concat([{ room: room, path: path, token: token }])
    stageNextDraftImage()
  }
  function stageNextDraftImage() {
    if (imageStagingBusy || imageStagingQueue.length === 0) return
    var task = imageStagingQueue[0]
    imageStagingQueue = imageStagingQueue.slice(1)
    imageStagingBusy = true
    bridge.uploadImage(task.room, task.path, function(value) {
      imageStagingBusy = false
      var images = imagesForRoom(task.room.id)
      var index = images.findIndex(function(image) { return image.token === task.token })
      if (index >= 0) {
        var next = images.slice()
        if (!value.ok || !value.artifact) {
          errorText = value.error || "Image could not be saved"
          next[index] = Object.assign({}, next[index], { uploading: false, error: true })
        } else {
          var artifact = value.artifact
          var storedPath = bridge.artifactRoot + "/" + artifact.id
          next[index] = Object.assign({}, next[index], {
            path: storedPath, url: "file://" + encodeURI(storedPath),
            artifactId: artifact.id, uploading: false, error: false
          })
        }
        setRoomImages(task.room.id, next)
      }
      stageNextDraftImage()
    })
  }
  function removePendingImage(index) {
    var next = pendingImages.slice()
    next.splice(index, 1)
    setRoomImages(selectedRoomId, next)
  }
  function clearSelectedImage() { setRoomImages(selectedRoomId, []) }
  function artifactForEvent(event) {
    if (!event || String(event.kind || "") !== "artifact.available") return null
    var artifactId = String((event.payload || ({})).artifactId || "")
    return artifacts.find(function(artifact) {
      return artifact.id === artifactId && String(artifact.mime || "").indexOf("image/") === 0
    }) || null
  }
  function artifactSource(event) {
    var artifact = artifactForEvent(event)
    if (!artifact || bridge.artifactRoot === "") return ""
    return "file://" + encodeURI(bridge.artifactRoot + "/" + artifact.id)
  }
  function roomDisplayName(room) {
    if (!room) return ""
    return room.alias ? String(room.alias) : String(room.name || "")
  }
  function updateRoomAlias(room, alias, callback) {
    if (!room) return
    bridge.request("POST", "/v1/rooms/" + encodeURIComponent(room.id) + "/alias", { alias: alias || "" }, function(value) {
      if (!value || !value.room) return
      var nextRooms = []
      for (var i = 0; i < root.rooms.length; ++i) {
        if (root.rooms[i].id === room.id) nextRooms.push(value.room)
        else nextRooms.push(root.rooms[i])
      }
      root.rooms = nextRooms
      navigationRevision += 1
      if (callback) callback(value.room)
    })
  }
  function toggleRoomPause(room) {
    var target = room || root.selectedRoom
    if (!target) return
    bridge.lifecycle(target, target.status === "paused" ? "resume" : "pause", function(value) {
      if (!value.ok) { root.errorText = value.error; return }
      if (target.id === root.extendedRoomId && target.status !== "paused") {
        var next = root.activeRooms.find(function(item) { return item.id !== target.id })
        root.extendedRoomId = next ? next.id : ""
        if (root.extendedMode && next) root.selectRoom(next)
      }
      bridge.reload()
    })
  }
  function archiveRoom(room) {
    if (!room) return
    bridge.lifecycle(room, "archive", function(value) {
      if (!value.ok) { root.errorText = value.error; return }
      if (root.extendedRoomId === room.id) {
        var next = root.activeRooms.find(function(item) { return item.id !== room.id })
        root.extendedRoomId = next ? next.id : ""
        if (root.selectedRoomId === room.id && root.extendedMode && next) root.selectRoom(next)
      }
      if (root.selectedRoomId === room.id) root.selectedRoomId = ""
      bridge.reload()
    })
  }
  function archiveSidebarSelection() {
    var ids = sidebarSelectedRoomIds.slice()
    var queue = rooms.filter(function(room) {
      return ids.indexOf(room.id) >= 0 && room.status === "active" && room.source !== "control"
    })
    if (queue.length === 0) {
      sidebarSelectedRoomIds = []
      sidebarSelectionAnchorId = ""
      sidebarSelectionCursorId = ""
      return
    }
    var archiveNext = function() {
      if (queue.length === 0) {
        sidebarSelectedRoomIds = []
        sidebarSelectionAnchorId = ""
        sidebarSelectionCursorId = ""
        root.errorText = ""
        bridge.reload()
        return
      }
      var room = queue.shift()
      bridge.lifecycle(room, "archive", function(value) {
        if (!value.ok) {
          root.errorText = value.error || ("Could not archive " + room.name)
          bridge.reload()
          return
        }
        if (root.selectedRoomId === room.id) root.selectedRoomId = ""
        if (root.extendedRoomId === room.id) root.extendedRoomId = ""
        archiveNext()
      })
    }
    archiveNext()
  }
  function archiveProjectRooms(project) {
    if (!project) return
    var queue = rooms.filter(function(room) {
      return room.projectId === project.id && room.status === "active" && room.source !== "control"
    })
    archiveRoomQueue(queue)
  }
  function projectRoomsOlderThan(project, cutoffSeconds) {
    if (!project) return []
    return rooms.filter(function(room) {
      return room.projectId === project.id && room.status === "active" && room.source !== "control"
        && roomActivity(room) < cutoffSeconds
    })
  }
  function archiveProjectRoomsOlderThan(project, cutoffSeconds) {
    archiveRoomQueue(projectRoomsOlderThan(project, cutoffSeconds))
  }
  function archiveRoomQueue(queue) {
    queue = queue.slice()
    if (queue.length === 0) return
    var archiveNext = function() {
      if (queue.length === 0) {
        root.errorText = ""
        bridge.reload()
        return
      }
      var room = queue.shift()
      bridge.lifecycle(room, "archive", function(value) {
        if (!value.ok) {
          root.errorText = value.error || ("Could not archive " + room.name)
          bridge.reload()
          return
        }
        if (root.selectedRoomId === room.id) root.selectedRoomId = ""
        if (root.extendedRoomId === room.id) root.extendedRoomId = ""
        archiveNext()
      })
    }
    archiveNext()
  }
  function archiveSelectedRoom() { root.archiveRoom(root.selectedRoom) }
  function restoreRoom(room) {
    if (!room) return
    bridge.lifecycle(room, "restore", function(value) {
      if (!value.ok) { errorText = value.error; return }
      section = "home"
      selectedProjectId = room.projectId
      selectedRoomId = room.id
      bridge.reload()
    })
  }
  function updatePeerPreferences(peer, name, colorRole) {
    bridge.request("POST", "/v1/peers/" + encodeURIComponent(peer.id) + "/preferences", {
      name: name, colorRole: colorRole
    }, function(value) {
      if (!value.ok) { errorText = value.error; return }
      bridge.reload()
    })
  }
  function updateBotPreferences(bot, name, description, faceShape, colorRole, modelChoice) {
    var payload = {
      name: name, description: description, faceShape: faceShape, colorRole: colorRole
    }
    if (modelChoice && modelChoice.model) {
      payload.model = modelChoice.model
      payload.provider = modelChoice.provider || ""
    }
    bridge.request("POST", "/v1/bots/" + encodeURIComponent(bot.id), payload, function(value) {
      if (!value.ok) { errorText = value.error; return }
      loadModels()
      bridge.reload()
    })
  }
  function loadModels() {
    bridge.request("GET", "/v1/models", null, function(value) {
      if (!value.ok) return
      models = value.models || []
    })
  }
  function botColorForRole(role) { return peerColor(role) }
  function updatePreferences(values) {
    bridge.request("POST", "/v1/preferences", values, function(value) {
      if (!value.ok) { errorText = value.error; return }
      var preferences = value.preferences || ({})
      homeQuotaVisible = preferences.homeQuotaVisible !== false
      homeQuotaCollapsed = preferences.homeQuotaCollapsed === true
      hiddenHomeQuotaProviders = preferences.hiddenHomeQuotaProviders || []
      panelSize = preferences.panelSize === "small" || preferences.panelSize === "large" ? preferences.panelSize : "medium"
      sidebarCollapsed = preferences.sidebarCollapsed === true
      if (!sidebarWidthSaving)
        sidebarWidth = preferences.sidebarWidth >= 120 && preferences.sidebarWidth <= 480 ? preferences.sidebarWidth : 200
      applyTerminalAppearance(preferences)
      applySearchPreference(preferences)
    })
  }
  function applySearchPreference(preferences) {
    if (pendingSearchShortcut !== "") return
    searchShortcut = preferences.searchShortcut === "Alt+Space" ? "Alt+Space" : "Ctrl+Space"
  }
  function setSearchShortcut(value) {
    if (value !== "Ctrl+Space" && value !== "Alt+Space") return
    if (value === searchShortcut && pendingSearchShortcut === "") return
    searchShortcut = value
    pendingSearchShortcut = value
    persistSearchShortcut()
  }
  function persistSearchShortcut() {
    if (searchPreferenceInFlight || pendingSearchShortcut === "") return
    var sent = pendingSearchShortcut
    searchPreferenceInFlight = true
    bridge.request("POST", "/v1/preferences", { searchShortcut: sent }, function(value) {
      searchPreferenceInFlight = false
      if (!value.ok) { errorText = value.error; return }
      if (pendingSearchShortcut === sent) pendingSearchShortcut = ""
      if (pendingSearchShortcut !== "") persistSearchShortcut()
      else applySearchPreference(value.preferences || ({ searchShortcut: sent }))
    })
  }
  function applyTerminalAppearance(preferences) {
    var pending = pendingTerminalPreferences
    if (!Object.prototype.hasOwnProperty.call(pending, "terminalFontSize"))
      terminalFontSize = preferences.terminalFontSize >= 7 && preferences.terminalFontSize <= 28 ? preferences.terminalFontSize : 10
    if (!Object.prototype.hasOwnProperty.call(pending, "terminalBackgroundRole"))
      terminalBackgroundRole = preferences.terminalBackgroundRole || "popups.background"
    if (!Object.prototype.hasOwnProperty.call(pending, "terminalBackgroundOpacity"))
      terminalBackgroundOpacity = preferences.terminalBackgroundOpacity === undefined ? 1 : preferences.terminalBackgroundOpacity
    if (!Object.prototype.hasOwnProperty.call(pending, "terminalBlur"))
      terminalBlur = preferences.terminalBlur === true
    if (!Object.prototype.hasOwnProperty.call(pending, "terminalPreviewReadOnly"))
      terminalPreviewReadOnly = preferences.terminalPreviewReadOnly !== false
  }
  function setTerminalPreference(key, value) {
    root[key] = value
    pendingTerminalPreferences = Object.assign({}, pendingTerminalPreferences, { [key]: value })
    if (!terminalPreferencesInFlight) terminalPrefsPersist.restart()
  }
  function setTerminalFontSize(size) {
    var clamped = Math.max(7, Math.min(28, Math.round(size)))
    if (terminalFontSize !== clamped) setTerminalPreference("terminalFontSize", clamped)
  }
  function setTerminalBackgroundRole(role) {
    if (["popups.background", "background", "accent", "muted"].indexOf(role) >= 0 && terminalBackgroundRole !== role)
      setTerminalPreference("terminalBackgroundRole", role)
  }
  function setTerminalBackgroundOpacity(opacity) {
    var clamped = Math.max(0, Math.min(1, Math.round(opacity * 20) / 20))
    if (terminalBackgroundOpacity !== clamped) setTerminalPreference("terminalBackgroundOpacity", clamped)
  }
  function setTerminalBlur(blur) {
    if (terminalBlur !== blur) setTerminalPreference("terminalBlur", blur)
  }
  function setTerminalPreviewReadOnly(readOnly) {
    if (terminalPreviewReadOnly !== readOnly) setTerminalPreference("terminalPreviewReadOnly", readOnly)
  }
  function persistTerminalPreferences() {
    if (terminalPreferencesInFlight || Object.keys(pendingTerminalPreferences).length === 0) return
    var sent = Object.assign({}, pendingTerminalPreferences)
    terminalPreferencesInFlight = true
    bridge.request("POST", "/v1/preferences", sent, function(value) {
      terminalPreferencesInFlight = false
      if (!value.ok) { errorText = value.error; return }
      var remaining = Object.assign({}, pendingTerminalPreferences)
      for (var key in sent) if (remaining[key] === sent[key]) delete remaining[key]
      pendingTerminalPreferences = remaining
      applyTerminalAppearance(value.preferences || ({}))
      if (Object.keys(remaining).length > 0) terminalPrefsPersist.restart()
    })
  }
  function cyclePanelSize() {
    updatePreferences({ panelSize: panelSize === "large" ? "medium" : "large" })
  }
  function setSidebarWidth(width, fromDrag) {
    var clamped = Math.max(120, Math.min(480, Math.round(width)))
    if (clamped === sidebarWidth) return
    sidebarWidth = clamped
    if (fromDrag === true) sidebarWidthCycleBase = clamped
    sidebarPersist.restart()
  }
  function cycleSidebarWidth() {
    var base = Math.max(120, Math.min(480, sidebarWidthCycleBase || sidebarWidth))
    var widths = [base, Math.min(480, Math.round(base * 1.15)), Math.min(480, Math.round(base * 1.30))]
    var index = widths.indexOf(sidebarWidth)
    if (index < 0) {
      sidebarWidthCycleBase = sidebarWidth
      base = sidebarWidth
      widths = [base, Math.min(480, Math.round(base * 1.15)), Math.min(480, Math.round(base * 1.30))]
      index = 0
    }
    setSidebarWidth(widths[(index + 1) % widths.length])
  }
  function persistSidebarWidth() {
    if (sidebarWidthSaving) return
    sidebarWidthSaving = true
    var width = sidebarWidth
    bridge.request("POST", "/v1/preferences", { sidebarWidth: width }, function(value) {
      sidebarWidthSaving = false
      if (!value.ok || !value.preferences) {
        errorText = value.error || "Could not save sidebar width"
        return
      }
      sidebarWidth = value.preferences.sidebarWidth >= 120 && value.preferences.sidebarWidth <= 480
        ? value.preferences.sidebarWidth : width
    })
  }
  function draftForRoom(roomId) {
    if (!roomId) return ""
    var value = roomDrafts[roomId]
    return value === undefined || value === null ? "" : String(value)
  }
  function loadDrafts() {
    bridge.request("GET", "/v1/drafts", null, function(value) {
      if (!value.ok) return
      var next = Object.assign({}, roomDrafts)
      var nextImages = Object.assign({}, roomDraftImages)
      var restoreSelected = selectedRoomId !== "" && next[selectedRoomId] === undefined
      var drafts = value.drafts || []
      for (var index = 0; index < drafts.length; ++index) {
        var draft = drafts[index]
        if (next[draft.roomId] === undefined) next[draft.roomId] = String(draft.text || "")
        if (nextImages[draft.roomId] === undefined) {
          nextImages[draft.roomId] = (draft.attachments || []).map(function(artifact) {
            var path = bridge.artifactRoot + "/" + artifact.id
            return { path: path, url: "file://" + encodeURI(path),
              name: artifact.name, artifactId: artifact.id, uploading: false }
          })
        }
      }
      roomDrafts = next
      roomDraftImages = nextImages
      controlDraftsLoaded = true
      if (restoreSelected) {
        draftRestoring = true
        roomText = draftForRoom(selectedRoomId)
        pendingImages = imagesForRoom(selectedRoomId)
        draftRestoring = false
      }
    })
  }
  function scheduleDraftSave() {
    if (selectedRoomId === "") return
    var next = Object.assign({}, roomDrafts)
    next[selectedRoomId] = roomText
    roomDrafts = next
    queueDraftSave(selectedRoomId, roomText, imagesForRoom(selectedRoomId))
  }
  function queueDraftSave(roomId, text, images) {
    if (!roomId) return
    var queued = Object.assign({}, draftPendingByRoom)
    queued[roomId] = { text: text, artifactIds: images.filter(function(image) { return !!image.artifactId })
      .map(function(image) { return image.artifactId }) }
    draftPendingByRoom = queued
    pendingDraftRoomId = roomId
    pendingDraftText = text
    draftSave.restart()
  }
  function persistDraftNow() {
    var queue = Object.assign({}, draftPendingByRoom)
    for (var id in queue) {
      if (draftWritesInFlight[id]) continue
      var pending = Object.assign({}, draftPendingByRoom)
      var snapshot = pending[id]
      if (snapshot === undefined) continue
      delete pending[id]
      draftPendingByRoom = pending
      draftWritesInFlight[id] = true
      bridge.request("POST", "/v1/drafts/" + encodeURIComponent(id), {
        device: "desktop", text: snapshot.text, artifactIds: snapshot.artifactIds
      }, (function(target, failedSnapshot) {
        return function(value) {
          delete draftWritesInFlight[target]
          if (!value.ok) {
            errorText = value.error || "Draft save failed"
            var latest = Object.assign({}, draftPendingByRoom)
            if (latest[target] === undefined) latest[target] = failedSnapshot
            draftPendingByRoom = latest
            pendingDraftRoomId = target
            draftRetry.restart()
          } else if (draftPendingByRoom[target] !== undefined) persistDraftNow()
        }
      })(id, snapshot))
    }
    if (pendingDraftRoomId !== "" && draftPendingByRoom[pendingDraftRoomId] === undefined)
      pendingDraftRoomId = Object.keys(draftPendingByRoom)[0] || ""
  }
  function clearDraft(roomId) {
    if (!roomId) return
    var next = Object.assign({}, roomDrafts)
    next[roomId] = ""
    roomDrafts = next
    var nextImages = Object.assign({}, roomDraftImages)
    nextImages[roomId] = []
    roomDraftImages = nextImages
    queueDraftSave(roomId, "", [])
    persistDraftNow()
  }
  function toggleSidebar() {
    updatePreferences({ sidebarCollapsed: !sidebarCollapsed })
  }
  function sendBotForRoom(room) {
    if (!room) return null
    var chosen = roomSendBotId !== "" ? roomSendBotId
      : (room.participants && room.participants.length > 0 ? room.participants[0] : "default")
    return bots.find(function(bot) { return bot.id === chosen }) || null
  }
  function setHomeQuotaVisible(visible) {
    updatePreferences({ homeQuotaVisible: visible })
  }
  function toggleHomeQuotaCollapsed() {
    updatePreferences({ homeQuotaCollapsed: !homeQuotaCollapsed })
  }
  function homeQuotaProviderVisible(providerId) {
    return hiddenHomeQuotaProviders.indexOf(String(providerId || "")) < 0
  }
  function setHomeQuotaProviderVisible(providerId, visible) {
    var id = String(providerId || "")
    if (id === "") return
    var hidden = hiddenHomeQuotaProviders.filter(function(item) { return item !== id })
    if (!visible) hidden.push(id)
    updatePreferences({ hiddenHomeQuotaProviders: hidden })
  }
  function eventBody(event) {
    var payload = event && event.payload ? event.payload : ({})
    return String((event && event.text) || payload.text || payload.delta || payload.name || "")
  }
  function eventTitle(event) {
    var kind = String((event && event.kind) || "")
    var payload = event && event.payload ? event.payload : ({})
    if (kind === "message.user") return "YOU"
    if (kind === "message.assistant") return String(event.author || "ASSISTANT").toUpperCase()
    if (kind === "reasoning.status") return "󰔟  " + String(payload.text || "THINKING").toUpperCase()
    if (kind === "tool.call") return "󰆍  " + String(payload.toolName || "TOOL").toUpperCase()
    if (kind === "tool.result") {
      var result = "󰆍  " + String(payload.toolName || "TOOL").toUpperCase()
      if (payload.exitCode !== undefined && payload.exitCode !== null) result += "  [exit " + payload.exitCode + "]"
      else if (payload.status) result += "  [" + String(payload.status).toUpperCase() + "]"
      if (payload.durationSeconds !== undefined) result += "  " + Number(payload.durationSeconds).toFixed(1) + "s"
      return result
    }
    if (kind.indexOf("reasoning") === 0 || kind.indexOf("thinking") === 0)
      return String(payload.text || "THINKING").toUpperCase()
    if (kind.indexOf("tool.") === 0) {
      var tool = String(payload.toolName || payload.name || "TOOL").toUpperCase()
      if (kind === "tool.start") return "󰆍  " + tool
      if (kind === "tool.generating") return "󰆍  " + tool + "  [PREPARING]"
      if (kind === "tool.progress") return "󰆍  " + tool + "  [RUNNING]"
      if (kind === "tool.output_risk") return "󰆍  " + tool + "  [OUTPUT RISK]"
      return "󰆍  " + tool + "  [COMPLETE]"
    }
    if (kind === "approval.request") return "NEEDS APPROVAL"
    if (kind === "execution.completed") return "DONE"
    return kind.replace(/\./g, " ").toUpperCase()
  }
  function eventTimestamp(event) {
    var timestamp = Number(event && event.createdAt ? event.createdAt : 0)
    return timestamp > 0 ? Qt.formatTime(new Date(timestamp * 1000), "HH:mm:ss") : ""
  }
  function eventsForRoom(roomId) {
    var merged = reconcileEvents(events.filter(function(event) { return event.roomId === roomId }))
    return merged.filter(function(event) {
      var body = eventBody(event).trim()
      if (String(event.kind || "").endsWith(".delta") && body.length < 2) return false
      return body !== "" || String(event.kind || "").indexOf("tool.") === 0
        || String(event.kind || "").indexOf("execution.") === 0
    })
  }
  function projectForRoom(room) {
    return room ? (projects.find(function(project) { return project.id === room.projectId }) || null) : null
  }
  function latestRoomMessage(roomId) {
    var latest = null
    var latestOrder = -1
    for (var index = 0; index < events.length; ++index) {
      var event = events[index]
      var kind = String(event.kind || "")
      if (event.roomId !== roomId || (kind !== "message.user" && kind !== "message.assistant")) continue
      var order = Number(event.sequence || event.cursor || 0)
      if (order >= latestOrder) { latestOrder = order; latest = event }
    }
    return latest
  }
  function latestAssistantCursor(roomId) {
    var latest = 0
    for (var index = 0; index < events.length; ++index) {
      var event = events[index]
      if (event.roomId === roomId && event.kind === "message.assistant")
        latest = Math.max(latest, Number(event.cursor || event.sequence || 0))
    }
    return latest
  }
  function markRoomSeen(roomId) {
    if (!roomId) return
    var cursor = latestAssistantCursor(roomId)
    if (cursor <= Number(seenRoomCursors[roomId] || 0)) return
    var next = Object.assign({}, seenRoomCursors)
    next[roomId] = cursor
    seenRoomCursors = next
  }
  function baselineNewRooms() {
    var next = Object.assign({}, seenRoomCursors)
    var changed = false
    for (var index = 0; index < rooms.length; ++index) {
      var roomId = rooms[index].id
      if (next[roomId] !== undefined) continue
      next[roomId] = latestAssistantCursor(roomId)
      changed = true
    }
    if (changed) seenRoomCursors = next
  }
  function roomWorking(roomId) {
    var room = rooms.find(function(item) { return item.id === roomId })
    var executions = bridge.state.executions || []
    if (executions.some(function(item) {
      return item.roomId === roomId && (item.status === "queued" || item.status === "running")
    })) return true
    if (room && ControlMapModel.freshExternalActivity(room, presenceNow)) return true
    // Imported Hermes messages are durable history, not a live pending-turn signal.
    if (!room || room.source !== "ares") return false
    var message = latestRoomMessage(roomId)
    if (!message || message.kind !== "message.user") return false
    var executionId = String(message.executionId || "")
    if (!executionId) return true // Legacy local messages may lack an execution.
    var execution = executions.find(function(item) { return item.id === executionId && item.roomId === roomId })
    if (execution) return execution.status === "queued" || execution.status === "running"
    return !events.some(function(item) {
      return item.roomId === roomId && item.executionId === executionId
        && ["execution.completed", "execution.failed", "execution.cancelled", "execution.interrupted"].indexOf(item.kind) >= 0
    })
  }
  function roomNeedsReply(roomId) {
    var roomItems = events.filter(function(event) { return event.roomId === roomId }).slice().sort(eventChronology)
    var pending = ({})
    for (var index = 0; index < roomItems.length; ++index) {
      var event = roomItems[index]
      var kind = String(event.kind || "")
      var payload = event.payload || ({})
      var family = kind.indexOf("clarify.") === 0 ? "clarify" : "approval"
      var key = String(payload.requestId || payload.request_id || event.executionId || "")
      if (kind === "approval.request" || kind === "clarify.request") pending[family + ":" + key] = true
      if (kind === "approval.responded" || kind === "clarify.responded") {
        delete pending[family + ":" + key]
        if (key === "") {
          for (var pendingKey in pending)
            if (pendingKey.indexOf(family + ":") === 0) delete pending[pendingKey]
        }
      }
    }
    if (Object.keys(pending).length > 0) return true
    return latestAssistantCursor(roomId) > Number(seenRoomCursors[roomId] || 0)
  }
  function botForRoom(room) {
    if (!room) return null
    var botId = room.participants && room.participants.length > 0 ? room.participants[0] : "default"
    return bots.find(function(bot) { return bot.id === botId }) || null
  }
  function botShapeForRoom(room) {
    var bot = botForRoom(room)
    var shape = String(bot ? bot.faceShape : "diamond")
    if (shape === "circle") return "oval"
    if (shape === "hexagon") return "square"
    return ["oval", "square", "triangle", "diamond"].indexOf(shape) >= 0 ? shape : "diamond"
  }
  function roomStatusLabel(room) {
    if (!room) return ""
    if (roomNeedsReply(room.id)) return "WAITING FOR YOU"
    if (roomWorking(room.id)) return "WORKING"
    return room.status === "paused" ? "PAUSED" : "IDLE"
  }
  function roomPreview(roomId) {
    for (var i = events.length - 1; i >= 0; --i) {
      if (events[i].roomId !== roomId) continue
      var text = eventBody(events[i]).replace(/\s+/g, " ").trim()
      if (text.length > 1) {
        if (/^HTTP 4\d\d:/i.test(text)) return "Request failed"
        if (/bot is not configured/i.test(text)) return "Bot unavailable"
        return text
      }
    }
    return "No activity yet"
  }
  function roomStateColor(room) {
    if (!room || room.status === "paused") return Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.34)
    return Color.accent
  }
  function moveRoom(delta) {
    if (projectRooms.length === 0) return
    var at = projectRooms.findIndex(function(room) { return room.id === selectedRoomId })
    at = Math.max(0, Math.min(projectRooms.length - 1, (at < 0 ? 0 : at) + delta))
    selectedRoomId = projectRooms[at].id
  }

  AresBridge {
    id: bridge
    launcherPath: root.serverPath
    eventRoomId: root.selectedRoomId
    onStateChangedByServer: root.applyState(bridge.state)
    onConnectionChanged: function(online) {
      if (!online) root.errorText = bridge.error
      if (online && root.section === "control") root.ensureControlRoom()
    }
  }

  Timer { interval: 30000; running: root.opened; repeat: true; onTriggered: root.presenceNow = Date.now() }
  Timer { id: draftSave; interval: 600; onTriggered: persistDraftNow() }
  Timer { id: draftRetry; interval: 3000; onTriggered: persistDraftNow() }
  Timer { id: sidebarPersist; interval: 700; onTriggered: persistSidebarWidth() }
  Timer { id: terminalPrefsPersist; interval: 300; onTriggered: persistTerminalPreferences() }

  Shortcut {
    sequence: "Shift+Down"
    context: Qt.WindowShortcut
    enabled: root.opened && !root.searchOpen && !root.extendedMode && root.section === "home"
    onActivated: root.extendSidebarSelection(1)
  }
  Shortcut {
    sequence: "Shift+Up"
    context: Qt.WindowShortcut
    enabled: root.opened && !root.searchOpen && !root.extendedMode && root.section === "home"
    onActivated: root.extendSidebarSelection(-1)
  }

  IpcHandler {
    target: root.ipcTarget
    function open() { root.open() }
    function close() { root.close() }
    function toggle() { root.toggle() }
    function refresh() { root.reload() }
    function search() { root.openSearch() }
    function searchState(): string { return JSON.stringify({ opened: root.searchOpen, shortcut: root.searchShortcut }) }
    function extended() { root.openExtended() }
    function overview() { root.showExtendedOverview() }
    function preferences() { root.extendedMode = false; root.section = "preferences"; root.open() }
    function archived() { root.extendedMode = false; root.section = "archived"; root.open() }
    function control() { root.openControlRoom() }
    function orchestrator() { root.openOrchestrator() }
    function peers() { root.openPeers() }
    function controlText() { root.controlMode = "text"; root.openControlRoom() }
    function controlVisual() { root.controlMode = "visual"; root.openControlRoom() }
    function controlMap() { root.controlMode = "map"; root.openControlRoom() }
    function home() { root.extendedMode = false; root.section = "home"; root.open() }
    function cycleView(direction: int) { root.cycleView(direction) }
    function growRoom(roomId: string) {
      root.growExtendedRoom(root.rooms.find(function(room) { return room.id === roomId }) || null)
    }
    function focusRoom(roomId: string) {
      root.focusExtendedRoom(root.rooms.find(function(room) { return room.id === roomId }) || null)
    }
    function selectProject(projectId: string) { root.selectProject(projectId) }
    function selectRoom(roomId: string) {
      var room = root.rooms.find(function(item) { return item.id === roomId })
      root.selectRoom(room)
    }
    function state(): string { return JSON.stringify({ opened: root.opened, anchorReady: root.anchorItem !== null, barReady: root.bar !== null, projects: root.projects.length, rooms: root.rooms.length, archivedRooms: root.archivedRooms.length, providers: root.quotaProviders.length, visibleProviders: root.visibleQuotaProviders.length, section: root.section, controlRoomId: root.controlRoomId, controlItems: root.controlItems.length, controlMode: root.controlMode, selectedProjectId: root.selectedProjectId, selectedRoomId: root.selectedRoomId, extendedRoomId: root.extendedRoomId, extendedMode: root.extendedMode, extendedViewMode: root.extendedViewMode, terminalFontSize: root.terminalFontSize, terminalBackgroundRole: root.terminalBackgroundRole, terminalBackgroundOpacity: root.terminalBackgroundOpacity, terminalBlur: root.terminalBlur, terminalPreviewReadOnly: root.terminalPreviewReadOnly, terminalFrameCount: Object.keys(root.terminalFrameCache.frames).length }) }
  }

  Process { id: openWebProc }
  Process { id: copyTokenProc }

  FileDialog {
    id: imageDialog
    title: "Attach image"
    fileMode: FileDialog.OpenFile
    options: FileDialog.DontUseNativeDialog
    nameFilters: ["Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp)"]
    onAccepted: {
      var url = selectedFile.toString()
      var path = url.indexOf("file://") === 0 ? decodeURIComponent(url.substring(7)) : url
      root.addPendingImage(path, root.imageDialogRoomId)
    }
  }

  function openWeb() {
    openWebProc.command = ["xdg-open", bridge.endpoint]
    openWebProc.running = true
  }
  function copyAccess() {
    copyTokenProc.command = ["wl-copy", bridge.mobileEndpoint + "/#token=" + bridge.token]
    copyTokenProc.running = true
  }

  readonly property string accessTokenMasked: {
    var value = bridge.token
    if (value.length === 0) return ""
    if (value.length <= 10) return "••••••"
    return value.slice(0, 6) + "…" + value.slice(-4)
  }
  property string accessTokenStatus: ""

  function setAccessToken(value) {
    if (tokenProc.running) return
    accessTokenStatus = "Saving access token…"
    tokenProc._output = ""
    tokenProc.command = [root.serverPath, "token", "--set", value]
    tokenProc.running = true
  }
  function regenerateAccessToken() {
    if (tokenProc.running) return
    accessTokenStatus = "Generating a new access token…"
    tokenProc._output = ""
    tokenProc.command = [root.serverPath, "token", "--regenerate"]
    tokenProc.running = true
  }
  function finishAccessToken(code) {
    var text = tokenProc._output
    try {
      var value = JSON.parse(text)
      if (code === 0 && value.ok === true) {
        accessTokenStatus = value.restarted === true
          ? "Access token updated · service restarted with the new token"
          : "Access token updated"
        bridge.start() // re-read the fresh token through the connection handshake
        return
      }
      accessTokenStatus = String(value.error || "Could not update the access token")
    } catch (_) {
      accessTokenStatus = "Invalid service response while updating the token"
    }
  }

  Process {
    id: tokenProc
    property string _output: ""
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: tokenProc._output = text }
    onExited: function(code) { root.finishAccessToken(code) }
  }

  PinnedKeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root
    bar: root.bar
    focusTarget: root.searchOpen ? searchPalette : keyCatcher
    margin: root.extendedMode ? Style.space(6) : Style.gapsOut
    padding: root.extendedMode ? Style.space(8) : Style.spacing.popupPadding
    centerOnBar: root.extendedMode
    dismissOnOutsideClick: !root.extendedMode
    contentWidth: root.extendedMode ? Math.round(availableCardWidth) : Math.max(Style.space(360), Math.round(availableCardWidth * root.panelSizeFraction))
    contentHeight: root.extendedMode ? Math.round(availableCardHeight) : Math.max(Style.space(300), Math.round(availableCardHeight * root.panelSizeFraction))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.searchOpen
      Shortcut {
        objectName: "roomSearchShortcut"
        sequence: root.searchShortcut
        context: Qt.WindowShortcut
        enabled: root.opened
        autoRepeat: false
        onActivated: root.toggleSearch()
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.cycleView(direction) }
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveRoom(dy > 0 ? 1 : -1) }
      onTextKey: function(text) {
        if (text === "r" || text === "R") root.reload()
        else if (text === "w" || text === "W") root.openWeb()
      }

      AresWorkspace {
        anchors.fill: parent
        controller: root
        bridge: bridge
        enabled: !root.searchOpen && !root.extendedMode && root.section === "home"
        opacity: !root.extendedMode && root.section === "home" ? 1 : 0
        visible: (!root.extendedMode && root.section === "home") || opacity > 0
        Behavior on opacity { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
      }

      PreferencesWorkspace {
        anchors.fill: parent
        controller: root
        enabled: !root.searchOpen
        visible: !root.extendedMode && root.section === "preferences"
      }

      ArchivedWorkspace {
        anchors.fill: parent
        controller: root
        enabled: !root.searchOpen
        visible: !root.extendedMode && root.section === "archived"
      }

      PeersWorkspace {
        anchors.fill: parent
        controller: root
        bridge: bridge
        enabled: !root.searchOpen
        visible: !root.extendedMode && root.section === "peers"
      }

      ControlRoom {
        anchors.fill: parent
        controller: root
        bridge: bridge
        enabled: !root.searchOpen
        visible: !root.extendedMode && root.section === "control"
      }

      ExtendedWorkspace {
        anchors.fill: parent
        controller: root
        bridge: bridge
        enabled: root.extendedMode && !root.searchOpen
        opacity: root.extendedMode ? 1 : 0
        visible: root.extendedMode || opacity > 0
        Behavior on opacity { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
      }
    }
    RoomSearchPalette {
      id: searchPalette
      anchors.fill: parent
      z: 1000
      opened: root.searchOpen
      rooms: root.sortedRooms()
      projects: root.projects
      shortcut: root.searchShortcut
      onDismissed: root.closeSearch(true)
      onRoomActivated: function(room) { root.activateSearchRoom(room) }
    }
  }

  component AresButton: Rectangle {
    property string label: ""
    signal activated()
    implicitWidth: Math.max(Style.space(48), labelText.implicitWidth + Style.space(16))
    implicitHeight: Style.space(28)
    radius: Style.cornerRadius
    color: mouse.pressed ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.36) : Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.2)
    border.color: Color.accent
    border.width: 1
    Text { id: labelText; anchors.centerIn: parent; text: parent.label; textFormat: Text.PlainText; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true }
    MouseArea { id: mouse; anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: parent.activated() }
  }

  component AresField: Rectangle {
    property string placeholder: ""
    property string value: ""
    signal edited(string text)
    implicitHeight: Style.space(32)
    color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.05)
    border.color: field.activeFocus ? Color.accent : Color.popups.border
    border.width: 1
    radius: Style.cornerRadius
    TextInput { id: field; anchors.fill: parent; anchors.margins: Style.space(8); text: parent.value; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.caption; clip: true; onTextEdited: parent.edited(text) }
    Text { anchors.verticalCenter: parent.verticalCenter; anchors.left: parent.left; anchors.leftMargin: Style.space(8); visible: field.text === "" && !field.activeFocus; text: parent.placeholder; textFormat: Text.PlainText; color: Color.popups.text; opacity: 0.48; font.family: Style.font.family; font.pixelSize: Style.font.caption }
  }
}
