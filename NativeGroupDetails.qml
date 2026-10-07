pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// A catalog of independently resolved member sessions, not a chat renderer.
Item {
  id: details
  objectName: "nativeGroupDetails"
  property var group: ({})
  required property var bridge
  required property string roomId
  property var messages: []
  property real oldestSeq: 0
  property bool hasOlder: false
  property bool loading: false
  property string historyError: ""
  property int historyGeneration: 0

  function refreshHistory() {
    historyGeneration++
    loading = false
    messages = []
    oldestSeq = 0
    hasOlder = false
    historyError = ""
    loadPage(0)
  }
  function loadOlder() {
    if (!loading && hasOlder && oldestSeq > 1) loadPage(oldestSeq)
  }
  function loadPage(before) {
    if (loading || !roomId) return
    loading = true
    var generation = historyGeneration
    var requested = roomId
    var cursor = before
    var url = "/v1/groups/" + encodeURIComponent(requested) + "/history"
      + (cursor > 0 ? "?before=" + cursor : "")
    bridge.request("GET", url, null, function(value) {
      if (generation !== details.historyGeneration || requested !== details.roomId) return
      details.loading = false
      if (!value || value.ok !== true || !Array.isArray(value.messages)
          || value.messages.length > 200 || value.readOnly !== true || value.roomId !== requested
          || typeof value.oldestSeq !== "number" || typeof value.hasOlder !== "boolean"
          || (cursor > 0 && value.oldestSeq >= cursor)) {
        details.historyError = qsTr("Group history unavailable at source.")
        return
      }
      if (cursor > 0) {
        var seen = new Set(details.messages.map(function(message) { return message.seq }))
        var older = value.messages.filter(function(message) {
          if (message.seq >= cursor || seen.has(message.seq)) return false
          seen.add(message.seq)
          return true
        })
        details.messages = older.concat(details.messages).sort(function(a, b) { return a.seq - b.seq })
      } else {
        details.messages = value.messages
      }
      details.oldestSeq = value.oldestSeq
      details.hasOlder = value.hasOlder
      details.historyError = ""
    })
  }
  onRoomIdChanged: {
    loading = false
    refreshHistory()
  }
  Component.onCompleted: refreshHistory()
  property color foreground
  property color dim
  property color surface
  property string fontFamily: "monospace"
  property bool preview: false
  signal memberSelected(string memberKey)
  readonly property var members: Array.isArray(group.members) ? group.members : []

  function resolutionLabel(member) {
    switch (member.resolution) {
    case "resolved": return qsTr("Stored session found")
    case "missing": return qsTr("No matching stored session")
    case "ambiguous": return qsTr("Multiple sessions — identity unresolved")
    case "unsupported": return qsTr("Connection not supported for discovery")
    default: return qsTr("Session discovery unavailable")
    }
  }

  ColumnLayout {
    anchors.fill: parent
    anchors.margins: 12
    spacing: 8
    Text {
      Layout.fillWidth: true
      text: qsTr("Bot group · session catalog")
      color: details.foreground
      font.family: details.fontFamily
      font.bold: true
      textFormat: Text.PlainText
      wrapMode: Text.WordWrap
    }
    Text {
      Layout.fillWidth: true
      text: qsTr("Each member has its own session. Discovery does not start agents.")
      color: details.dim
      font.family: details.fontFamily
      textFormat: Text.PlainText
      wrapMode: Text.WordWrap
      visible: !details.preview
    }
    ListView {
      id: memberList
      objectName: "nativeGroupMembers"
      Layout.fillWidth: true
      Layout.preferredHeight: details.preview ? 110 : 175
      clip: true
      spacing: 6
      model: details.members
      interactive: !details.preview
      ScrollBar.vertical: ScrollBar { policy: details.preview ? ScrollBar.AlwaysOff : ScrollBar.AsNeeded }
      delegate: Rectangle {
        id: memberCard
        required property var modelData
        width: memberList.width
        height: memberColumn.implicitHeight + 16
        radius: 6
        color: details.surface
        ColumnLayout {
          id: memberColumn
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.margins: 8
          y: 8
          spacing: 4
          Text {
            Layout.fillWidth: true
            text: String(memberCard.modelData.profile || qsTr("Unresolved profile"))
              + " · " + String(memberCard.modelData.host || memberCard.modelData.connectionId || qsTr("Unresolved host"))
            color: details.foreground
            font.family: details.fontFamily
            font.bold: true
            textFormat: Text.PlainText
            elide: Text.ElideRight
          }
          Text {
            Layout.fillWidth: true
            text: details.resolutionLabel(memberCard.modelData)
            color: details.dim
            font.family: details.fontFamily
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
          }
          Text {
            Layout.fillWidth: true
            visible: !details.preview && memberCard.modelData.resolution === "resolved"
            text: String(memberCard.modelData.resolvedSessionId || memberCard.modelData.storedSessionId || "")
            color: details.dim
            font.family: details.fontFamily
            textFormat: Text.PlainText
            wrapMode: Text.WrapAnywhere
          }
          Button {
            objectName: "selectNativeGroupMember"
            visible: !details.preview
            enabled: memberCard.modelData.resolution === "resolved"
              && memberCard.modelData.connectionKind === "local"
              && memberCard.modelData.host === "local"
            text: qsTr("View existing terminal")
            onClicked: details.memberSelected(String(memberCard.modelData.memberKey))
          }
        }
      }
    }
    RowLayout {
      Layout.fillWidth: true
      Text {
        Layout.fillWidth: true
        text: qsTr("Group conversation · read-only")
        color: details.foreground
        font.family: details.fontFamily
        font.bold: true
      }
      Button {
        text: qsTr("Refresh")
        enabled: !details.loading
        visible: !details.preview
        onClicked: details.refreshHistory()
      }
      Button {
        text: qsTr("Older")
        enabled: !details.loading && details.hasOlder
        visible: !details.preview
        onClicked: details.loadOlder()
      }
    }
    Text {
      Layout.fillWidth: true
      visible: details.loading || details.historyError !== ""
      text: details.loading ? qsTr("Loading group history…") : details.historyError
      textFormat: Text.PlainText
      color: details.dim
      wrapMode: Text.WordWrap
    }
    ListView {
      id: conversation
      objectName: "nativeGroupConversation"
      Layout.fillWidth: true
      Layout.fillHeight: true
      clip: true
      spacing: 6
      model: details.messages
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      delegate: Rectangle {
        id: messageCard
        required property var modelData
        width: conversation.width
        height: messageColumn.implicitHeight + 12
        radius: 6
        color: details.surface
        ColumnLayout {
          id: messageColumn
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.margins: 6
          y: 6
          Text {
            Layout.fillWidth: true
            text: String(messageCard.modelData.author || "") + " · #" + String(messageCard.modelData.seq || "")
            color: details.dim
            font.family: details.fontFamily
            textFormat: Text.PlainText
          }
          Text {
            Layout.fillWidth: true
            text: String(messageCard.modelData.text || "")
            color: details.foreground
            font.family: details.fontFamily
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
          }
        }
      }
    }
    Text {
      Layout.fillWidth: true
      visible: details.members.length === 0
      text: qsTr("Member identities are not available yet.")
      color: details.dim
      font.family: details.fontFamily
      wrapMode: Text.WordWrap
    }
    Text {
      Layout.fillWidth: true
      visible: !details.preview
      text: qsTr("Read-only view of individually identified members. An existing observed terminal is required; no session will be started.")
      color: details.dim
      font.family: details.fontFamily
      wrapMode: Text.WordWrap
    }
  }
}
