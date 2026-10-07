pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// This view owns only an in-memory, bounded window; the source rechecks every page.
Item {
  id: historyView
  objectName: "boundedHistory"
  required property var bridge
  required property string roomId
  property bool eligible: false
  property var rows: []
  property var nextBefore: null
  property bool hasMore: false
  property bool loading: false
  property string errorText: ""
  property int generation: 0
  property color foreground: "white"
  property color dim: "gray"
  property color surface: "#222222"
  property string fontFamily: "monospace"
  signal closed()

  function clearHistory() {
    generation++
    rows = []
    nextBefore = null
    hasMore = false
    loading = false
    errorText = ""
  }
  function validCursor(value) {
    return typeof value === "string" && /^[1-9][0-9]*$/.test(value)
      && (value.length < 19 || value.length === 19 && value <= "9223372036854775807")
  }
  function cursorLess(left, right) {
    return left.length < right.length || left.length === right.length && left < right
  }
  function loadHistory() {
    if (!eligible || loading) return
    clearHistory()
    requestPage(null)
  }
  function loadOlder() {
    if (!eligible || loading || !hasMore || !validCursor(nextBefore)) return
    requestPage(nextBefore)
  }
  function requestPage(before) {
    if (!eligible || loading || !roomId || before !== null && !validCursor(before)) return
    if (!bridge || !bridge.available) {
      clearHistory()
      errorText = "History unavailable at source (sign in, hidden session, or source offline)."
      return
    }
    loading = true
    var requested = roomId
    var attempt = generation
    var url = "/v1/rooms/" + encodeURIComponent(requested) + "/history"
      + (before !== null ? "?before=" + encodeURIComponent(before) : "")
    bridge.request("GET", url, null, function(value) {
      if (attempt !== historyView.generation || requested !== historyView.roomId || !historyView.eligible) return
      historyView.loading = false
      var valid = bridge.available && value && value.ok === true && Array.isArray(value.messages)
        && value.messages.length <= 8 && typeof value.has_more === "boolean"
        && (value.next_before === null || validCursor(value.next_before))
      var prior = null
      if (valid) for (var i = 0; i < value.messages.length; i++) {
        var row = value.messages[i]
        if (!row || (row.role !== "user" && row.role !== "assistant")
            || typeof row.text !== "string" || row.text.length === 0 || Array.from(row.text).length > 500
            || !validCursor(row.cursor) || prior !== null && !cursorLess(prior, row.cursor)
            || before !== null && !cursorLess(row.cursor, before)) { valid = false; break }
        prior = row.cursor
      }
      if (valid) valid = value.has_more
        ? value.messages.length > 0 && value.next_before === value.messages[0].cursor
          && (before === null || cursorLess(value.next_before, before))
        : value.next_before === null
      if (!valid) {
        historyView.clearHistory()
        historyView.errorText = "History unavailable at source (sign in, hidden session, or source offline)."
        return
      }
      historyView.rows = (before !== null ? value.messages.concat(historyView.rows) : value.messages).slice(0, 64)
      historyView.nextBefore = value.next_before
      historyView.hasMore = value.has_more
      historyView.errorText = ""
    })
  }
  onRoomIdChanged: clearHistory()
  onEligibleChanged: if (!eligible) clearHistory()
  onVisibleChanged: if (!visible) clearHistory()
  Connections {
    target: historyView.bridge
    function onConnectionChanged(online) { if (!online) historyView.clearHistory() }
  }

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: 10
    spacing: 8
    RowLayout {
      Layout.fillWidth: true
      Text {
        Layout.fillWidth: true
        text: "Hermes history · read-only · 8 × 500 characters per page"
        textFormat: Text.PlainText
        color: historyView.foreground
        font.family: historyView.fontFamily
        elide: Text.ElideRight
      }
      Button { text: "Close"; onClicked: historyView.closed() }
    }
    RowLayout {
      Layout.fillWidth: true
      Button {
        text: "Load history"
        enabled: historyView.eligible && !historyView.loading
        onClicked: historyView.loadHistory()
      }
      Button {
        text: "Load older"
        enabled: historyView.eligible && !historyView.loading && historyView.hasMore
        onClicked: historyView.loadOlder()
      }
    }
    Text {
      Layout.fillWidth: true
      visible: historyView.loading || historyView.errorText !== ""
      text: historyView.loading ? "Loading history…" : historyView.errorText
      textFormat: Text.PlainText
      color: historyView.dim
    }
    ListView {
      id: historyList
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      spacing: 6
      model: historyView.rows
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      delegate: Rectangle {
        id: messageCard
        required property var modelData
        width: historyList.width
        height: entry.implicitHeight + 12
        color: historyView.surface
        Column {
          id: entry
          x: 6
          y: 6
          width: parent.width - 12
          spacing: 4
          Text {
            text: messageCard.modelData.role === "user" ? "YOU" : "ASSISTANT"
            textFormat: Text.PlainText
            color: historyView.dim
            font.family: historyView.fontFamily
          }
          Text {
            width: parent.width
            text: messageCard.modelData.text
            textFormat: Text.PlainText
            color: historyView.foreground
            font.family: historyView.fontFamily
            wrapMode: Text.Wrap
          }
        }
      }
    }
  }
}
