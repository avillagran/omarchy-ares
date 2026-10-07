import QtQuick
import QtQuick.Controls

Item {
  id: root

  property string text: ""
  property color color: "white"
  property string fontFamily: "sans-serif"
  property real pixelSize: 14
  property bool bold: true
  readonly property bool overflow: titleText.implicitWidth > width + 1
  readonly property real overflowPixels: Math.max(0, titleText.implicitWidth - width)

  implicitHeight: titleText.implicitHeight
  clip: true

  Text {
    id: titleText
    anchors.verticalCenter: parent.verticalCenter
    text: root.text
    textFormat: Text.PlainText
    color: root.color
    font.family: root.fontFamily
    font.pixelSize: root.pixelSize
    font.bold: root.bold
    maximumLineCount: 1
  }

  SequentialAnimation {
    id: marqueeAnimation
    running: root.visible && root.overflow
    loops: Animation.Infinite
    PauseAnimation { duration: 900 }
    NumberAnimation {
      target: titleText
      property: "x"
      from: 0
      to: -root.overflowPixels
      duration: Math.max(1300, root.overflowPixels * 34)
      easing.type: Easing.InOutCubic
    }
    PauseAnimation { duration: 1050 }
    NumberAnimation {
      target: titleText
      property: "x"
      to: 0
      duration: Math.max(700, root.overflowPixels * 18)
      easing.type: Easing.OutCubic
    }
  }

  HoverHandler {
    id: titleHover
    cursorShape: Qt.ArrowCursor
  }

  ToolTip.visible: titleHover.hovered && root.overflow
  ToolTip.text: root.text
  ToolTip.delay: 450
}
