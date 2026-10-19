import QtQuick
import Quickshell.Io

Item {
  id: bridge
  visible: false

  property string launcherPath: ""
  property string endpoint: ""
  property string mobileEndpoint: ""
  property string artifactRoot: ""
  property string eventRoomId: ""
  property string token: ""
  property bool available: false
  property bool starting: false
  property bool requestActive: false
  property bool liveRequestActive: false
  property bool historyRequestActive: false
  property bool historyHasOlder: false
  property int historyEarliestCursor: 0
  property string historyRoomId: ""
  property var olderEvents: []
  property var baseEvents: []
  readonly property int historyWindowLimit: 240
  property string error: ""
  property int cursor: 0
  property var uploadCallback: null
  property var state: ({ ok: true, projects: [], rooms: [], events: [], executions: [], artifacts: [], bots: [], peers: [] })

  signal stateChangedByServer()
  signal connectionChanged(bool online)
  signal historyPageLoaded(string roomId, int count)

  function sameData(a, b) {
    try { return JSON.stringify(a) === JSON.stringify(b) } catch (_) { return false }
  }

  function mergeEvents(primary, secondary) {
    var merged = []
    var positions = ({})
    var sources = [secondary || [], primary || []]
    for (var sourceIndex = 0; sourceIndex < sources.length; sourceIndex++) {
      var source = sources[sourceIndex]
      for (var index = 0; index < source.length; index++) {
        var event = source[index]
        var key = String(event.id || "")
        if (key === "") continue
        if (positions[key] === undefined) {
          positions[key] = merged.length
          merged.push(event)
        } else {
          merged[positions[key]] = event
        }
      }
    }
    merged.sort(function(a, b) {
      var cursorDelta = Number(a.cursor || 0) - Number(b.cursor || 0)
      if (cursorDelta !== 0) return cursorDelta
      return Number(a.createdAt || 0) - Number(b.createdAt || 0)
    })
    return merged
  }

  function start() {
    if (starting || launcherPath === "") return
    starting = true
    connectionProc.command = [launcherPath, "connection"]
    connectionProc.running = true
  }

  function parseConnection(text) {
    starting = false
    try {
      var value = JSON.parse(text)
      if (value.ok === true && value.running === true && value.endpoint && value.token) {
        endpoint = String(value.endpoint).replace(/\/$/, "")
        mobileEndpoint = String(value.mobileEndpoint || endpoint)
        artifactRoot = String(value.artifactRoot || "")
        token = String(value.token)
        error = ""
        available = true
        connectionChanged(true)
        reload()
        return
      }
      error = value.error || "Ares service unavailable"
    } catch (_) { error = "Invalid Ares service response" }
    available = false
    connectionChanged(false)
    reconnect.restart()
  }

  function request(method, path, body, callback) {
    if (!available || endpoint === "") { if (callback) callback({ ok: false, error: "service unavailable" }); return }
    var requestOwner = this
    var xhr = new XMLHttpRequest()
    var completed = false
    var finish = function(value) {
      if (completed) return
      completed = true
      if (callback) callback(value)
    }
    xhr.open(method, endpoint + path)
    xhr.setRequestHeader("Authorization", "Bearer " + token)
    xhr.setRequestHeader("Accept", "application/json")
    if (body !== null && body !== undefined) xhr.setRequestHeader("Content-Type", "application/json")
    xhr.onreadystatechange = function() {
      if (completed || xhr.readyState !== XMLHttpRequest.DONE) return
      var value = ({ ok: false, error: "Invalid service response" })
      try { value = JSON.parse(xhr.responseText || "{}") } catch (_) {}
      if (xhr.status >= 200 && xhr.status < 300 && value.ok !== false) {
        requestOwner.error = ""
        finish(value)
      } else {
        requestOwner.error = value.error || ("HTTP " + xhr.status)
        if (xhr.status === 0 || xhr.status === 401) { requestOwner.available = false; connectionChanged(false); reconnect.restart() }
        finish({ ok: false, error: requestOwner.error })
      }
    }
    xhr.onerror = function() {
      if (completed) return
      requestOwner.available = false
      requestOwner.error = "Ares service connection lost"
      connectionChanged(false)
      reconnect.restart()
      finish({ ok: false, error: requestOwner.error })
    }
    xhr.send(body === null || body === undefined ? null : JSON.stringify(body))
  }

  function reload() {
    if (!available || requestActive) return
    requestActive = true
    request("GET", "/v1/state?compact=1&eventRoomId=" + encodeURIComponent(eventRoomId), null, function(value) {
      requestActive = false
      if (value.ok === false) return
      var page = value.eventPage || ({})
      if (String(page.roomId || "") === eventRoomId && olderEvents.length === 0) {
        historyRoomId = eventRoomId
        historyEarliestCursor = Number(page.earliestCursor || 0)
        historyHasOlder = Boolean(page.hasOlder)
      }
      var incoming = value.events || []
      // Identity-preserving reconcile: keep existing object references for
      // unchanged events so the transcript can diff rows in place instead of
      // rebuilding every delegate on each poll.
      var previous = state.events || []
      var byId = ({})
      for (var i = 0; i < previous.length; i++) byId[previous[i].id] = previous[i]
      var merged = []
      var seen = ({})
      for (var n = 0; n < incoming.length; n++) {
        var item = incoming[n]
        var prior = byId[item.id]
        merged.push(prior && sameData(prior, item) ? prior : item)
        seen[item.id] = true
      }
      var olderIds = ({})
      for (var o = 0; o < olderEvents.length; o++) olderIds[olderEvents[o].id] = true
      for (var p = 0; p < previous.length; p++) {
        var prevItem = previous[p]
        if (!seen[prevItem.id] && olderIds[prevItem.id]) merged.push(prevItem)
      }
      merged.sort(function(a, b) { return (a.cursor || 0) - (b.cursor || 0) })
      baseEvents = incoming
      state = Object.assign({}, value, { events: merged })
      cursor = Number(value.latestCursor || 0)
      stateChangedByServer()
    })
  }

  function loadOlder() {
    if (!available || eventRoomId === "" || historyRequestActive || !historyHasOlder
        || historyEarliestCursor <= 0 || historyRoomId !== eventRoomId) return
    var requestedRoom = eventRoomId
    var before = historyEarliestCursor
    historyRequestActive = true
    request("GET", "/v1/events?roomId=" + encodeURIComponent(requestedRoom)
      + "&before=" + before + "&limit=40", null, function(value) {
      historyRequestActive = false
      if (value.ok === false || requestedRoom !== eventRoomId || !Array.isArray(value.events)) return
      olderEvents = mergeEvents(olderEvents, value.events)
      if (olderEvents.length > historyWindowLimit)
        olderEvents = olderEvents.slice(0, historyWindowLimit)
      historyEarliestCursor = Number(value.earliestCursor || before)
      historyHasOlder = Boolean(value.hasOlder)
      state = Object.assign({}, state, { events: mergeEvents(baseEvents, olderEvents) })
      stateChangedByServer()
      historyPageLoaded(requestedRoom, value.events.length)
    })
  }

  function reloadLive() {
    if (!available || eventRoomId === "" || liveRequestActive) return
    liveRequestActive = true
    request("GET", "/v1/live?roomId=" + encodeURIComponent(eventRoomId), null, function(value) {
      liveRequestActive = false
      if (value.ok === false || !Array.isArray(value.events) || value.events.length === 0) return
      var events = (state.events || []).slice(0)
      var positions = ({})
      var changed = false
      for (var i = 0; i < events.length; i++) positions[String(events[i].id || "")] = i
      for (var j = 0; j < value.events.length; j++) {
        var liveEvent = value.events[j]
        var key = String(liveEvent.id || "")
        if (positions[key] === undefined) {
          positions[key] = events.length
          events.push(liveEvent)
          changed = true
        } else {
          var position = positions[key]
          if (JSON.stringify(events[position]) !== JSON.stringify(liveEvent)) {
            events[position] = liveEvent
            changed = true
          }
        }
      }
      if (!changed) return
      baseEvents = mergeEvents(baseEvents, value.events)
      state = Object.assign({}, state, { events: events })
      stateChangedByServer()
    })
  }

  function send(room, text, artifactIds, callback, botId, idempotencyKey) {
    request("POST", "/v1/rooms/" + encodeURIComponent(room.id) + "/send", {
      projectId: room.projectId, botId: botId || (room.participants && room.participants[0]) || "default", text: text,
      artifactIds: artifactIds || [],
      idempotencyKey: idempotencyKey || "qml-send-" + Date.now() + "-" + Math.random().toString(16).slice(2)
    }, callback)
  }

  function sendTerminal(room, text, artifactIds, callback, idempotencyKey, confirmedEmptyComposer) {
    request("POST", "/v1/tty/rooms/" + encodeURIComponent(room.id) + "/prompt", {
      text: text, artifactIds: artifactIds || [], idempotencyKey: idempotencyKey,
      confirmedEmptyComposer: confirmedEmptyComposer === true
    }, callback)
  }

  function uploadImage(room, path, callback) {
    if (uploadProc.running) { callback({ ok: false, error: "Another image is still uploading" }); return }
    uploadCallback = callback
    uploadProc.command = [launcherPath, "upload", "--project", room.projectId, "--room", room.id, "--path", path]
    uploadProc.running = true
  }

  function finishUpload(text) {
    var callback = uploadCallback
    uploadCallback = null
    if (!callback) return
    try { callback(JSON.parse(text || "{}")) }
    catch (_) { callback({ ok: false, error: "Invalid image upload response" }) }
  }

  function approve(event, choice, callback) {
    var payload = event.payload || ({})
    request("POST", "/v1/executions/" + encodeURIComponent(event.executionId || "") + "/approval", {
      requestId: payload.request_id || payload.requestId || "", choice: choice
    }, callback)
  }

  function lifecycle(room, action, callback) {
    request("POST", "/v1/rooms/" + encodeURIComponent(room.id) + "/" + action, {
      projectId: room.projectId,
      idempotencyKey: "qml-" + action + "-" + Date.now() + "-" + Math.random().toString(16).slice(2)
    }, callback)
  }

  Process {
    id: connectionProc
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: bridge.parseConnection(text) }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code) { if (code !== 0) { bridge.starting = false; bridge.available = false; reconnect.restart() } }
  }

  Process {
    id: uploadProc
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: bridge.finishUpload(text) }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code) {
      if (code !== 0 && bridge.uploadCallback) bridge.finishUpload('{"ok":false,"error":"Image upload failed"}')
    }
  }

  onEventRoomIdChanged: {
    olderEvents = []
    baseEvents = []
    historyRoomId = eventRoomId
    historyEarliestCursor = 0
    historyHasOlder = false
    if (available) {
      reload()
      reloadLive()
    }
  }

  Timer { id: livePoll; interval: 400; repeat: true; running: bridge.available && bridge.eventRoomId !== ""; onTriggered: bridge.reloadLive() }
  Timer { id: poll; interval: 1200; repeat: true; running: bridge.available; onTriggered: bridge.reload() }
  Timer { id: reconnect; interval: 4000; repeat: false; onTriggered: bridge.start() }
  Component.onCompleted: start()
}
