import Quickshell
import QtQuick

// Robot face avatar: colored body (4 shapes), two white eyes with black
// outline (no pupils — they are robots). Eyes wander + blink when `working`.
Item {
  id: face
  property real size: 28
  property real hue: 220            // 0-360 body color
  property string shapeName: "oval" // oval | square | triangle | diamond
  property bool working: false
  property bool waiting: false
  property string colorHex: ""      // overrides hue when set
  property color notificationColor: "#f2b84b"

  readonly property color bodyColor: colorHex !== "" ? colorHex
                                      : Qt.hsla(hue / 360, 0.55, 0.45, 1)
  width: size
  height: size

  Rectangle {
    anchors.centerIn: parent
    width: face.size * 1.16
    height: width
    radius: width / 2
    color: "transparent"
    border.color: face.notificationColor
    border.width: Math.max(1, face.size * 0.07)
    visible: face.waiting
    SequentialAnimation on opacity {
      running: face.waiting
      loops: Animation.Infinite
      NumberAnimation { to: 0.28; duration: 560; easing.type: Easing.InOutSine }
      NumberAnimation { to: 1; duration: 560; easing.type: Easing.InOutSine }
    }
  }

  // ── body ──
  Item {
    anchors.fill: parent
    // diamond rotates a square body; eyes counter-rotate to stay level
    rotation: face.shapeName === "diamond" ? 45 : 0

    Rectangle {
      visible: face.shapeName !== "triangle"
      anchors.centerIn: parent
      width: face.shapeName === "diamond" ? parent.width * 0.78 : parent.width
      height: face.shapeName === "diamond" ? parent.height * 0.78 : parent.height
      radius: face.shapeName === "square" ? size * 0.18 : width / 2
      color: face.bodyColor
    }
    // inverted triangle = the whole body (drawn, not overdrawn)
    Canvas {
      visible: face.shapeName === "triangle"
      anchors.fill: parent
      onPaint: {
        var ctx = getContext("2d")
        ctx.clearRect(0, 0, width, height)
        ctx.fillStyle = Qt.rgba(face.bodyColor.r, face.bodyColor.g, face.bodyColor.b, 1)
        ctx.beginPath()
        ctx.moveTo(0, 0)
        ctx.lineTo(width, 0)
        ctx.lineTo(width / 2, height)
        ctx.closePath()
        ctx.fill()
      }
    }
  }

  // ── eyes (stay level for every shape) ──
  Row {
    anchors.centerIn: parent
    anchors.verticalCenterOffset: face.shapeName === "triangle" ? -size * 0.10 : 0
    spacing: size * 0.10
    rotation: face.shapeName === "diamond" ? -45 : 0

    Repeater {
      model: 2
      Item {
        id: eyeWrap
        required property int index
        width: size * 0.32
        height: size * 0.38

        Rectangle {
          id: eye
          anchors.fill: parent
          radius: width / 2
          color: "#ffffff"
          border.color: "#1a1a1a"
          border.width: Math.max(1, size * 0.055)

          // gaze drift while working
          transform: Translate {
            id: gaze
            x: 0
            y: 0
            SequentialAnimation on x {
              loops: Animation.Infinite
              running: face.working
              NumberAnimation { to: -size * 0.06; duration: 430; easing.type: Easing.InOutSine }
              NumberAnimation { to: size * 0.08; duration: 660; easing.type: Easing.InOutSine }
              NumberAnimation { to: 0; duration: 380; easing.type: Easing.InOutSine }
            }
            SequentialAnimation on y {
              loops: Animation.Infinite
              running: face.working
              NumberAnimation { to: size * 0.05; duration: 520; easing.type: Easing.InOutSine }
              NumberAnimation { to: -size * 0.04; duration: 580; easing.type: Easing.InOutSine }
            }
          }

          // blink while awake (both eyes share cadence via eye index offset)
          SequentialAnimation {
            loops: Animation.Infinite
            running: !face.sleeping
            PauseAnimation { duration: (face.working ? 1600 : 3800) + eyeWrap.index * 60 }
            NumberAnimation { target: eye; property: "scale"; to: 0.12; duration: 70 }
            NumberAnimation { target: eye; property: "scale"; to: 1.0; duration: 110 }
          }
        }

        // sleeping eyelid — body-colored cover with a thin sleepy line,
        // fades in/out smoothly when the bot falls asleep / wakes up.
        Rectangle {
          anchors.fill: parent
          radius: width / 2
          color: face.bodyColor
          opacity: face.sleeping ? 1 : 0
          Behavior on opacity { NumberAnimation { duration: 450 } }
          Rectangle {
            anchors.centerIn: parent
            width: parent.width * 0.72
            height: Math.max(1.2, size * 0.055)
            radius: height / 2
            color: "#1a1a1a"
            opacity: 0.85
          }
        }
      }
    }
  }

  Rectangle {
    anchors.right: parent.right
    anchors.top: parent.top
    width: Math.max(4, face.size * 0.22)
    height: width
    radius: width / 2
    color: face.notificationColor
    border.color: "#1a1a1a"
    border.width: 1
    visible: face.waiting
  }

  // A bot that isn't working sleeps (closed eyes).
  readonly property bool sleeping: !face.working && !face.waiting
}
