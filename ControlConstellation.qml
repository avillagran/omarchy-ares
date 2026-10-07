import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import qs.Commons
import qs.Ui

// A shared living map. Agents keep their identity while crossing between zones.
Item {
  id: scene
  required property var controller
  required property var snapshot
  signal roomActivated(string roomId)
  signal roomAction(string roomId, string action)
  property string hoverRoomId: ""
  readonly property var people: snapshot && snapshot.people ? snapshot.people : []
  readonly property var actionTarget: people.find(function(person) { return person.roomId === hoverRoomId }) || null
  readonly property var counts: snapshot && snapshot.counts ? snapshot.counts : ({working: 0, waiting: 0, idle: 0})
  readonly property color ink: Color.popups.text
  readonly property color quiet: Qt.rgba(ink.r, ink.g, ink.b, 0.58)
  readonly property color haze: Qt.rgba(ink.r, ink.g, ink.b, 0.045)
  readonly property var currentLogs: {
    for (var i = 0; i < people.length; ++i)
      if (people[i].activity === "working" && people[i].logLines.length > 0)
        return people[i].logLines.slice(0, 2)
    return []
  }

  ListModel { id: roster; dynamicRoles: true }
  function syncRoster() {
    var desired = scene.people
    var ids = ({})
    for (var i = 0; i < desired.length; ++i) ids[desired[i].roomId + ":" + desired[i].botId] = true
    for (var old = roster.count - 1; old >= 0; --old)
      if (!ids[roster.get(old).key]) roster.remove(old)
    var positions = {working: 0, waiting: 0, idle: 0}
    for (var j = 0; j < desired.length; ++j) {
      var person = desired[j]
      var key = person.roomId + ":" + person.botId
      var data = { key: key, roomId: person.roomId, botId: person.botId,
        name: person.name, projectName: person.projectName, shape: person.shape,
        colorRole: person.colorRole, activity: person.activity,
        zoneIndex: positions[person.activity]++, logLines: JSON.stringify(person.logLines || []) }
      var at = -1
      for (var n = 0; n < roster.count; ++n) if (roster.get(n).key === key) { at = n; break }
      if (at < 0) roster.append(data)
      else for (var field in data)
        if (roster.get(at)[field] !== data[field]) roster.setProperty(at, field, data[field])
    }
  }
  onSnapshotChanged: syncRoster()
  Component.onCompleted: syncRoster()

  Rectangle {
    anchors.fill: parent
    radius: Style.cornerRadius
    color: Color.popups.background
    border.color: Qt.rgba(scene.ink.r, scene.ink.g, scene.ink.b, 0.16)
  }
  Item {
    id: stage
    anchors { fill: parent; margins: Style.space(10); topMargin: Style.space(31) }
    clip: true

    Canvas {
      id: paths
      anchors.fill: parent
      property color lineColor: Color.accent
      onLineColorChanged: requestPaint()
      onWidthChanged: requestPaint()
      onHeightChanged: requestPaint()
      onPaint: {
        var ctx = getContext("2d")
        ctx.clearRect(0, 0, width, height)
        ctx.globalAlpha = 0.30
        ctx.strokeStyle = String(lineColor)
        ctx.lineWidth = Style.space(1)
        ctx.beginPath()
        ctx.moveTo(width * 0.33, height * 0.34)
        ctx.bezierCurveTo(width * 0.05, height * 0.52, width * 0.56, height * 0.61, width * 0.30, height * 0.80)
        ctx.bezierCurveTo(width * 0.62, height * 1.03, width * 0.66, height * 0.51, width * 0.83, height * 0.49)
        ctx.bezierCurveTo(width * 0.98, height * 0.28, width * 0.58, height * 0.03, width * 0.33, height * 0.34)
        ctx.stroke()
        ctx.globalAlpha = 1
      }
    }

    Item {
      id: studio
      x: 0; y: 0; width: stage.width * 0.60; height: stage.height * 0.56
      Text {
        anchors { left: parent.left; top: parent.top; margins: Style.space(10) }
        text: "01  /  STUDIO     " + scene.counts.working + " in motion"
        color: Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.caption
      }
      Rectangle {
        x: parent.width * 0.62; y: parent.height * 0.25
        width: parent.width * 0.35; height: parent.height * 0.60
        radius: Style.space(7)
        color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.11)
        border.color: Color.accent
        visible: scene.counts.working > 0
        Column {
          anchors { fill: parent; margins: Style.space(8) }
          spacing: Style.space(6)
          Text {
            text: "LIVE  /  RECENT OUTPUT"
            color: Color.accent; font.family: Style.font.family; font.pixelSize: Style.font.caption
          }
          Repeater {
            model: scene.currentLogs
            Text {
              id: logText
              width: parent.width
              text: String(modelData.text)
              textFormat: Text.PlainText
              wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
              color: scene.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption
              HoverHandler { id: logHover }
              ToolTip.visible: logHover.hovered && logText.truncated
              ToolTip.text: logText.text
              ToolTip.delay: 450
            }
          }
          Text {
            visible: scene.currentLogs.length === 0
            text: "No recent output"
            color: scene.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
          }
        }
      }
      Text {
        anchors.centerIn: parent
        visible: scene.counts.working === 0
        text: "The workbench is quiet"
        color: scene.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
      }
    }

    Item {
      id: commons
      x: 0; y: stage.height * 0.59; width: stage.width * 0.60; height: stage.height * 0.41
      Text {
        anchors { left: parent.left; top: parent.top; margins: Style.space(10) }
        text: "02  /  COMMONS     " + scene.counts.idle + " at rest"
        color: scene.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
      }
      Item {
        x: (parent.width - width) / 2
        y: Math.min(parent.height - height, parent.height * 0.76 - height / 2)
        width: Style.space(62); height: width
        Rectangle {
          anchors.centerIn: parent
          width: parent.width; height: width; radius: width / 2
          color: "transparent"; border.color: Color.accent; border.width: Style.space(1)
          opacity: 0.36
          SequentialAnimation on scale {
            running: scene.visible && scene.counts.idle > 0
            loops: Animation.Infinite
            NumberAnimation { to: 1.30; duration: 2200; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1; duration: 2200; easing.type: Easing.InOutSine }
          }
        }
        Rectangle {
          anchors.centerIn: parent
          width: Style.space(19); height: width; radius: width / 2
          color: Color.accent; opacity: 0.28
        }
      }
      Text {
        anchors { right: parent.right; bottom: parent.bottom; margins: Style.space(9) }
        text: scene.counts.idle > 3 ? "+" + (scene.counts.idle - 3) + " nearby" : ""
        color: scene.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
      }
    }

    Item {
      id: signalZone
      x: stage.width * 0.625; y: 0; width: stage.width * 0.375; height: stage.height
      Text {
        anchors { left: parent.left; top: parent.top; margins: Style.space(10) }
        text: "03  /  SIGNAL     " + scene.counts.waiting + " need you"
        color: scene.counts.waiting > 0 ? Color.urgent : scene.quiet
        font.family: Style.font.family; font.pixelSize: Style.font.caption
      }
      Rectangle {
        anchors { right: parent.right; top: parent.top; rightMargin: Style.space(15); topMargin: Style.space(11) }
        width: Style.space(7); height: width; radius: width / 2
        color: Color.urgent
        opacity: scene.counts.waiting > 0 ? 1 : 0.25
        SequentialAnimation on opacity {
          running: scene.visible && scene.counts.waiting > 0
          loops: Animation.Infinite
          NumberAnimation { to: 0.24; duration: 650 }
          NumberAnimation { to: 1; duration: 650 }
        }
      }
      Rectangle {
        anchors.centerIn: parent
        width: parent.width * 0.55; height: width; radius: width / 2
        color: "transparent"; border.color: Color.urgent; border.width: Style.space(1)
        opacity: scene.counts.waiting > 0 ? 0.35 : 0.22
        SequentialAnimation on scale {
          running: scene.visible && scene.counts.waiting > 0
          loops: Animation.Infinite
          NumberAnimation { to: 1.18; duration: 1300; easing.type: Easing.InOutSine }
          NumberAnimation { to: 1; duration: 1300; easing.type: Easing.InOutSine }
        }
      }
      Text {
        anchors.centerIn: parent
        visible: scene.counts.waiting === 0
        text: "No decisions needed"
        color: scene.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
      }
    }

    Repeater {
      model: roster
      Item {
        id: actor
        required property int index
        required property string key
        required property string roomId
        required property string botId
        required property string name
        required property string projectName
        required property string shape
        required property string colorRole
        required property string activity
        required property int zoneIndex
        required property string logLines
        width: activity === "idle" ? Style.space(92) : activity === "working" ? Style.space(166) : Style.space(146)
        height: activity === "working" ? Style.space(105) : Style.space(88)
        function recentLine() {
          try { var lines = JSON.parse(logLines); return lines.length ? String(lines[0].text || "") : "" }
          catch (err) { return "" }
        }
        x: activity === "working" ? studio.x + Style.space(11) + (zoneIndex % 2) * (studio.width * 0.28)
          : activity === "waiting" ? signalZone.x + Style.space(12) + (zoneIndex % 2) * (signalZone.width / 2)
          : commons.x + commons.width * (0.16 + zoneIndex * 0.31) - width / 2
        y: activity === "working" ? studio.y + Style.space(43) + Math.floor(zoneIndex / 2) * (studio.height * 0.40)
          : activity === "waiting" ? signalZone.y + Style.space(50) + Math.floor(zoneIndex / 2) * (signalZone.height * 0.29)
          : commons.y + Style.space(40) + (zoneIndex % 2) * Style.space(18)
        Behavior on x { NumberAnimation { duration: 950; easing.type: Easing.InOutCubic } }
        Behavior on y { NumberAnimation { duration: 950; easing.type: Easing.InOutCubic } }

        Text {
          width: parent.width
          text: actor.projectName
          textFormat: Text.PlainText
          horizontalAlignment: Text.AlignHCenter
          elide: Text.ElideRight
          color: scene.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true
        }
        BotFace {
          id: face
          anchors { horizontalCenter: parent.horizontalCenter; top: parent.top; topMargin: Style.space(19) }
          size: Style.space(33)
          shapeName: actor.shape
          colorHex: String(scene.controller.botColorForRole(actor.colorRole))
          working: actor.activity !== "waiting"
          waiting: actor.activity === "waiting"
          notificationColor: Color.urgent
          transform: Translate {
            y: 0
            SequentialAnimation on y {
              running: scene.visible && actor.activity === "idle"
              loops: Animation.Infinite
              NumberAnimation { to: -Style.space(3); duration: 1400; easing.type: Easing.InOutSine }
              NumberAnimation { to: 0; duration: 1400; easing.type: Easing.InOutSine }
            }
          }
        }
        Text {
          anchors { top: face.bottom; horizontalCenter: parent.horizontalCenter; topMargin: Style.space(2) }
          text: actor.name
          color: scene.quiet; font.family: Style.font.family; font.pixelSize: Style.font.caption
          width: actor.width; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight
        }
        Rectangle {
          visible: actor.activity === "waiting"
          anchors { left: face.right; verticalCenter: face.verticalCenter; leftMargin: Style.space(3) }
          width: Style.space(15); height: width; radius: width / 2
          color: Color.urgent
          Text { anchors.centerIn: parent; text: "!"; color: Color.popups.background; font.bold: true; font.pixelSize: Style.font.caption }
        }
        Rectangle {
          visible: actor.activity === "working" && actor.recentLine() !== ""
          anchors { left: parent.left; right: parent.right; bottom: parent.bottom; margins: Style.space(2) }
          height: Style.space(33); radius: Style.space(5)
          color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.13)
          border.color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.33)
          Text {
            anchors { fill: parent; margins: Style.space(4) }
            text: actor.recentLine(); textFormat: Text.PlainText
            wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
            color: scene.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption
          }
        }
        HoverHandler { id: hover; onHoveredChanged: if (hovered) scene.hoverRoomId = actor.roomId }
        ToolTip.visible: hover.hovered
        ToolTip.text: actor.projectName + " · " + actor.name + " · " + actor.activity
        ToolTip.delay: 450
        TapHandler {
          acceptedButtons: Qt.LeftButton
          onTapped: { scene.hoverRoomId = actor.roomId; scene.roomActivated(actor.roomId) }
        }
      }
    }
  }
  Row {
    anchors { top: parent.top; right: parent.right; margins: Style.space(4) }
    spacing: Style.space(3)
    visible: scene.actionTarget !== null
    Text {
      width: Style.space(150); height: Style.space(23)
      text: scene.actionTarget ? scene.actionTarget.projectName + " · " + scene.actionTarget.name : ""
      textFormat: Text.PlainText; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter
      color: scene.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption
    }
    SceneAction { label: "Go to chat"; onActivated: scene.roomAction(scene.actionTarget.roomId, "chat") }
    SceneAction { label: "Draft"; onActivated: scene.roomAction(scene.actionTarget.roomId, "draft") }
    SceneAction { label: "Archive"; onActivated: scene.roomAction(scene.actionTarget.roomId, "archive") }
  }
  Text {
    anchors.centerIn: parent
    visible: scene.counts.working + scene.counts.waiting + scene.counts.idle === 0
    text: "No included rooms · choose which conversations join this space"
    color: scene.quiet; font.family: Style.font.family; font.pixelSize: Style.font.body
  }
  component SceneAction: Rectangle {
    property string label: ""
    signal activated()
    implicitWidth: Math.max(Style.space(37), actionText.implicitWidth + Style.space(10))
    implicitHeight: Style.space(23)
    radius: Style.space(4); color: Color.popups.background
    border.color: Qt.rgba(scene.ink.r, scene.ink.g, scene.ink.b, 0.25)
    Text { id: actionText; anchors.centerIn: parent; text: parent.label; color: scene.ink; font.family: Style.font.family; font.pixelSize: Style.font.caption }
    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: parent.activated() }
  }
}
