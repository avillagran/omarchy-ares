import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import "PeerWorkspaceModel.js" as PeerModel

Item {
  id: workspace
  required property var controller
  required property var bridge
  property var peers: []
  property var sessions: []
  property string selectedId: ""
  property string fingerprintAtSelection: ""
  property string pairAlias: ""
  property string oneTimeCode: ""
  property bool fingerprintConfirmed: false
  property bool revokeConfirmed: false
  property bool busy: false
  property bool loading: false
  property string status: ""
  property string activityStatus: ""
  readonly property var selected: PeerModel.selectedPeer(peers, selectedId)
  readonly property var pairedPeers: peers.filter(function(peer) { return peer.paired === true })
  readonly property var discoveredPeers: peers.filter(function(peer) { return peer.paired !== true })
  readonly property bool pairReady: PeerModel.canPair(selected, fingerprintAtSelection, fingerprintConfirmed, oneTimeCode, busy)
  readonly property bool revokeReady: PeerModel.canRevoke(selected, revokeConfirmed, busy)
  readonly property color ink: Color.popups.text
  readonly property color dim: Qt.rgba(ink.r, ink.g, ink.b, 0.60)
  readonly property color line: Qt.rgba(ink.r, ink.g, ink.b, 0.18)
  readonly property color surface: Qt.rgba(ink.r, ink.g, ink.b, 0.06)

  function clearConsent() {
    oneTimeCode = ""
    fingerprintConfirmed = false
    revokeConfirmed = false
  }
  function choose(peer) {
    clearConsent()
    selectedId = String(peer.id || "")
    fingerprintAtSelection = String(peer.fingerprint || "")
    pairAlias = String(peer.alias || peer.name || "")
    sessions = []
    activityStatus = ""
    if (peer.paired === true) loadSessions()
  }
  function refresh() {
    loading = true
    bridge.request("GET", "/v1/peers", null, function(value) {
      loading = false
      if (!value.ok || !Array.isArray(value.peers)) {
        peers = []
        sessions = []
        clearConsent()
        status = "Peer service unavailable. Existing projects and Control Room are unaffected."
        return
      }
      peers = value.peers
      status = ""
      var current = PeerModel.selectedPeer(peers, selectedId)
      if (!current || String(current.fingerprint || "") !== fingerprintAtSelection) {
        selectedId = ""
        fingerprintAtSelection = ""
        sessions = []
        clearConsent()
        if (current) status = "Peer identity changed. Select it again and verify its certificate."
      } else if (current.paired === true) {
        clearConsent()
        loadSessions()
      }
    })
  }
  function loadSessions() {
    var peer = selected
    if (!peer || peer.paired !== true || !peer.alias) return
    var id = selectedId
    bridge.request("GET", "/v1/peers/" + encodeURIComponent(peer.alias) + "/sessions", null, function(value) {
      if (selectedId !== id) return
      sessions = value.ok && Array.isArray(value.sessions) ? value.sessions : []
      activityStatus = value.ok ? (sessions.length ? "Session catalog (read-only)" : "No sessions reported")
        : "Session catalog unavailable (peer may be offline)."
    })
  }
  function pair() {
    if (!pairReady) return
    var peer = selected
    var code = oneTimeCode.trim()
    var alias = pairAlias.trim()
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(alias)) { status = "Use 1–64 letters, digits, _ or - for the peer name."; return }
    busy = true
    clearConsent()
    bridge.request("POST", "/v1/peers/pair", {
      peerId: peer.id, alias: alias, fingerprint: fingerprintAtSelection, code: code
    }, function(value) {
      busy = false
      status = value.ok ? "Pairing completed. Refreshing peers…" : "Pairing failed. Check the certificate and one-time code on the peer, then retry."
      if (value.ok) refresh()
    })
    code = ""
  }
  function revoke() {
    if (!revokeReady) return
    var peer = selected
    busy = true
    clearConsent()
    bridge.request("POST", "/v1/peers/" + encodeURIComponent(peer.alias) + "/revoke", { confirmed: true }, function(value) {
      busy = false
      status = value.ok ? "Peer access revoked. Refreshing peers…" : "Could not revoke peer access. Retry when the service is available."
      if (value.ok) refresh()
    })
  }
  onVisibleChanged: {
    if (visible) refresh()
    else clearConsent()
  }
  Timer { interval: 15000; repeat: true; running: workspace.visible; onTriggered: workspace.refresh() }

  Rectangle {
    anchors.fill: parent
    color: Color.popups.background
    radius: Style.cornerRadius
    ColumnLayout {
      anchors.fill: parent
      anchors.margins: Style.space(12)
      spacing: Style.space(8)
      RowLayout {
        Layout.fillWidth: true
        Text { Layout.fillWidth: true; text: "PEER SERVICES"; color: Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.subtitle; font.bold: true }
        PeerButton { label: "Refresh"; onActivated: workspace.refresh() }
        PeerButton { label: "Back"; onActivated: workspace.controller.section = "home" }
      }
      Text {
        Layout.fillWidth: true
        text: "Discovered devices are not trusted. Pair only after checking the certificate on the other device."
        color: workspace.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
      Text {
        Layout.fillWidth: true; visible: workspace.status !== "" || workspace.loading
        text: workspace.loading ? "Checking peer service…" : workspace.status
        color: workspace.status ? Color.urgent : workspace.dim
        font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
      }
      Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: workspace.line }
      ScrollView {
        Layout.fillWidth: true; Layout.fillHeight: true
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
          width: Math.max(0, parent.width - Style.space(12))
          spacing: Style.space(8)
          Text { text: "PAIRED  ·  " + workspace.pairedPeers.length; color: Color.accent; font.bold: true; font.family: Style.font.family; font.pixelSize: Style.font.caption }
          Text { visible: workspace.pairedPeers.length === 0; text: "No paired peers"; color: workspace.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption }
          Repeater {
            model: workspace.pairedPeers
            delegate: PeerRow { required property var modelData; peer: modelData }
          }
          Text { text: "DISCOVERED  ·  " + workspace.discoveredPeers.length; color: Color.accent; font.bold: true; font.family: Style.font.family; font.pixelSize: Style.font.caption }
          Text { visible: workspace.discoveredPeers.length === 0; text: "No devices discovered"; color: workspace.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption }
          Repeater {
            model: workspace.discoveredPeers
            delegate: PeerRow { required property var modelData; peer: modelData }
          }
          Rectangle {
            Layout.fillWidth: true
            visible: workspace.selected !== null
            implicitHeight: details.implicitHeight + Style.space(24)
            radius: Style.cornerRadius
            color: workspace.surface
            border.color: workspace.line
            ColumnLayout {
              id: details
              anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
              anchors.margins: Style.space(12)
              spacing: Style.space(8)
              Text { Layout.fillWidth: true; text: workspace.selected ? String(workspace.selected.alias || workspace.selected.name || "Peer") : ""; color: workspace.ink; font.bold: true; font.family: Style.font.family; font.pixelSize: Style.font.body; elide: Text.ElideRight }
              Text { Layout.fillWidth: true; text: workspace.selected && workspace.selected.online === false ? "Offline · last known status" : "Online status: " + (workspace.selected && workspace.selected.online === true ? "reachable" : "unknown"); color: workspace.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption }
              Text { Layout.fillWidth: true; text: "Certificate fingerprint (verify on the peer itself):"; color: workspace.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap }
              Text { Layout.fillWidth: true; text: workspace.selected ? String(workspace.selected.fingerprint || "Not provided by discovery") : ""; textFormat: Text.PlainText; color: workspace.ink; font.family: "monospace"; font.pixelSize: Style.font.caption; wrapMode: Text.WrapAnywhere }
              CheckBox {
                visible: workspace.selected && workspace.selected.paired !== true
                text: "I compared this fingerprint on the peer and it matches"
                checked: workspace.fingerprintConfirmed
                onToggled: workspace.fingerprintConfirmed = checked
              }
              TextField {
                Layout.fillWidth: true
                visible: workspace.selected && workspace.selected.paired !== true
                placeholderText: "Peer name"
                text: workspace.pairAlias
                onTextEdited: workspace.pairAlias = text
              }
              TextField {
                Layout.fillWidth: true
                visible: workspace.selected && workspace.selected.paired !== true
                placeholderText: "One-time code from the peer"
                echoMode: TextInput.Password
                text: workspace.oneTimeCode
                onTextEdited: workspace.oneTimeCode = text
              }
              PeerButton {
                visible: workspace.selected && workspace.selected.paired !== true
                label: workspace.busy ? "Pairing…" : "Pair"
                enabled: workspace.pairReady && /^[A-Za-z0-9_-]{1,64}$/.test(workspace.pairAlias.trim())
                onActivated: workspace.pair()
              }
              Text { visible: workspace.selected && workspace.selected.paired === true; text: workspace.activityStatus; color: workspace.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption }
              Repeater {
                model: workspace.selected && workspace.selected.paired === true ? workspace.sessions.slice(0, 8) : []
                delegate: Text {
                  required property var modelData
                  Layout.fillWidth: true
                  text: PeerModel.sessionSummary(modelData)
                  textFormat: Text.PlainText; color: workspace.dim; font.family: Style.font.family; font.pixelSize: Style.font.caption; elide: Text.ElideRight
                }
              }
              CheckBox {
                visible: workspace.selected && workspace.selected.paired === true
                text: "I understand this will revoke access"
                checked: workspace.revokeConfirmed
                onToggled: workspace.revokeConfirmed = checked
              }
              PeerButton {
                visible: workspace.selected && workspace.selected.paired === true
                label: "Revoke access"
                enabled: workspace.revokeReady
                onActivated: workspace.revoke()
              }
            }
          }
        }
      }
    }
  }

  component PeerRow: Rectangle {
    required property var peer
    Layout.fillWidth: true
    implicitHeight: Style.space(42)
    radius: Style.cornerRadius
    color: workspace.selectedId === String(peer.id || "") ? workspace.surface : "transparent"
    border.color: workspace.line
    Text {
      anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
      anchors.margins: Style.space(10)
      text: String(peer.alias || peer.name || peer.id || "Unknown device")
        + "  ·  " + (peer.online === false ? "OFFLINE" : peer.online === true ? "ONLINE" : "STATUS UNKNOWN")
      textFormat: Text.PlainText; color: workspace.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption; elide: Text.ElideRight
    }
    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: workspace.choose(peer) }
  }
  component PeerButton: Rectangle {
    id: button
    property string label: ""
    signal activated()
    implicitWidth: caption.implicitWidth + Style.space(20)
    implicitHeight: Style.space(28)
    radius: Style.cornerRadius
    color: enabled ? workspace.surface : "transparent"
    border.color: enabled ? Color.accent : workspace.line
    opacity: enabled ? 1 : 0.45
    Text { id: caption; anchors.centerIn: parent; text: button.label; color: workspace.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption }
    MouseArea { anchors.fill: parent; enabled: button.enabled; cursorShape: Qt.PointingHandCursor; onClicked: button.activated() }
  }
}
