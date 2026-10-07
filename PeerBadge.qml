import QtQuick
import QtQuick.Layouts
import qs.Commons

Rectangle {
  id: badge

  required property var controller
  required property string nodeId
  property bool compact: false

  readonly property var peer: controller.peerForNode(nodeId)
  readonly property color badgeColor: controller.peerColor(peer ? peer.colorRole : "muted")

  implicitWidth: badgeLabel.implicitWidth + Style.space(compact ? 7 : 10)
  implicitHeight: Style.space(compact ? 16 : 19)
  radius: height / 2
  color: Qt.rgba(badgeColor.r, badgeColor.g, badgeColor.b, 0.18)
  border.color: badgeColor
  border.width: 1

  Text {
    id: badgeLabel
    anchors.centerIn: parent
    text: badge.controller.peerBadgeForNode(badge.nodeId)
    textFormat: Text.PlainText
    color: badge.badgeColor
    font.family: Style.font.family
    font.pixelSize: Math.max(8, Style.font.caption - (badge.compact ? 1 : 0))
    font.bold: true
    elide: Text.ElideRight
  }
}
