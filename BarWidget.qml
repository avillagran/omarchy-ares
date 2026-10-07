import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.avillagran.omarchy-ares"

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened : false
  readonly property bool working: panelLoader.item ? panelLoader.item.working : false

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
  }

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }

  onBarChanged: injectPanel()
  implicitWidth: Style.bar.statusSlot
  implicitHeight: Style.bar.statusSlot

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
  }

  BarIconButton {
    id: button
    anchors.centerIn: parent
    bar: root.bar
    active: root.opened
    slotSize: Style.bar.statusSlot
    tooltipText: "Ares · projects and rooms"

    iconComponent: Component {
      Item {
        anchors.fill: parent
        Rectangle {
          anchors.centerIn: parent
          width: 22
          height: 22
          radius: width / 2
          color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.16)
          SequentialAnimation on scale {
            loops: Animation.Infinite
            running: root.working
            NumberAnimation { to: 1.22; duration: 1300; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.0; duration: 1300; easing.type: Easing.InOutSine }
          }
        }
        BotFace {
          anchors.centerIn: parent
          size: 17
          colorHex: String(Color.accent)
          shapeName: "diamond"
          working: root.working
        }
      }
    }
    onPressed: function(button) {
      if (button === Qt.LeftButton) root.togglePanel()
      else if (button === Qt.MiddleButton && panelLoader.item) panelLoader.item.reload()
    }
  }
}
