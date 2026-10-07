import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons

// Bounded, explicit read-only peek. Never changes the selected room or transcript.
Item {
  id: peek
  required property var bridge
  property string roomId: ""
  property string roomName: ""
  property bool sourcePreviewLabel: false
  property var messages: []
  property bool loading: false
  property string errorText: ""
  property int generation: 0
  visible: roomId !== ""
  focus: visible
  Keys.onEscapePressed: dismiss()

  function dismiss() {
    generation++
    roomId = ""
    roomName = ""
    messages = []
    loading = false
    errorText = ""
  }

  function openRoom(id, name) {
    if (!id) return
    generation++
    var requested = generation
    roomId = String(id)
    roomName = String(name || "Chat")
    messages = []
    errorText = ""
    loading = false
    forceActiveFocus()
    if (!bridge || !bridge.available) {
      errorText = "Preview unavailable"
      return
    }
    loading = true
    bridge.request("GET", "/v1/rooms/" + encodeURIComponent(roomId) + "/preview", null, function(value) {
      if (requested !== peek.generation || roomId !== String(id)) return
      loading = false
      if (!value || value.ok !== true || !Array.isArray(value.messages)) {
        errorText = "Preview unavailable"
        return
      }
      messages = value.messages.filter(function(message) {
        var role = message && message.role
        return (role === "user" || role === "assistant") && typeof message.text === "string"
      }).slice(-8).map(function(message) {
        return { role: message.role, text: message.text.slice(0, 500) }
      })
    })
  }

  Rectangle {
    anchors.fill: parent
    color: Qt.rgba(0, 0, 0, 0.72)
    MouseArea { anchors.fill: parent; onClicked: peek.dismiss() }
  }
  Rectangle {
    anchors.centerIn: parent
    width: Math.min(parent.width - Style.space(20), Style.space(560))
    height: Math.min(parent.height - Style.space(20), Style.space(480))
    radius: Style.cornerRadius
    color: Color.popups.background
    border.color: Color.popups.border
    ColumnLayout {
      anchors.fill: parent
      anchors.margins: Style.space(12)
      spacing: Style.space(8)
      RowLayout {
        Layout.fillWidth: true
        Text {
          Layout.fillWidth: true
          text: peek.roomName
          textFormat: Text.PlainText
          elide: Text.ElideRight
          color: Color.popups.text
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Button { text: "Close"; onClicked: peek.dismiss() }
      }
      Text {
        Layout.fillWidth: true
        text: peek.sourcePreviewLabel ? "Read-only paired-source preview · latest 8 messages, 500 characters each · not live"
          : "Read-only preview · latest messages"
        color: Color.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
      ListView {
        id: messageList
        Layout.fillWidth: true
        Layout.fillHeight: true
        clip: true
        spacing: Style.space(8)
        model: peek.messages
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: Rectangle {
          required property var modelData
          width: messageList.width
          height: row.implicitHeight + Style.space(12)
          color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.06)
          radius: Style.space(5)
          Column {
            id: row
            x: Style.space(8)
            y: Style.space(6)
            width: parent.width - Style.space(16)
            spacing: Style.space(4)
            Text {
              text: modelData.role === "user" ? "YOU" : "ASSISTANT"
              color: Color.accent
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Text {
              width: parent.width
              text: modelData.text
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }
          }
        }
      }
      Text {
        Layout.fillWidth: true
        visible: peek.loading || peek.errorText !== "" || peek.messages.length === 0
        text: peek.loading ? "Loading preview…" : peek.errorText !== "" ? peek.errorText : "No messages available for this chat."
        textFormat: Text.PlainText
        color: Color.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }
    }
  }
}
