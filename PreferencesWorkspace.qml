import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Rectangle {
  id: preferences
  property int activeTab: 0
  required property var controller
  property string keybindState: ""

  Process {
    id: keybindApplyProc
    command: ["bash", "-c",
      'FILE="$HOME/.config/hypr/bindings.lua"; '
      + 'LINE=\'o.bind("SUPER + SHIFT + A", "Ares", "qs -c /usr/share/omarchy/shell ipc call io.github.avillagran.omarchy-ares toggle")\'; '
      + 'grep -qF "omarchy-ares toggle" "$FILE" 2>/dev/null || echo "$LINE" >> "$FILE"; '
      + 'hyprctl reload >/dev/null 2>&1']
    onExited: function(code) {
      preferences.keybindState = code === 0 ? "SUPER+SHIFT+A is bound to Ares" : "Could not apply the keybind"
    }
  }

  color: Color.popups.background
  radius: Style.cornerRadius

  ColumnLayout {
    anchors.fill: parent
    spacing: Style.space(10)

    RowLayout {
      Layout.fillWidth: true
      Layout.preferredHeight: Style.space(50)
      spacing: Style.space(10)

      Text {
        text: "󰒓"
        color: Color.accent
        font.family: Style.font.family
        font.pixelSize: Style.font.title
      }
      ColumnLayout {
        Layout.fillWidth: true
        spacing: 0
        Text {
          text: "PREFERENCES"
          color: Color.popups.text
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Text {
          text: controller.peers.length + " connected peers · names and colors are local to Ares"
          color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
      PrefButton { glyph: "󰁍"; hint: "Back to chats"; onActivated: controller.section = "home" }
    }

    Rectangle { Layout.fillWidth: true; height: 1; color: Color.popups.border }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.space(4)

      Repeater {
        model: ["GENERAL", "BOTS", "PEERS"]
        delegate: Rectangle {
          required property var modelData
          required property int index
          readonly property bool activeTab: preferences.activeTab === index
          Layout.preferredWidth: tabLabel.implicitWidth + Style.space(16)
          Layout.preferredHeight: Style.space(26)
          radius: Style.space(4)
          color: activeTab ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18)
            : Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.05)
          border.color: activeTab ? Color.accent : Color.popups.border
          border.width: 1

          Text {
            id: tabLabel
            anchors.centerIn: parent
            text: modelData
            color: activeTab ? Color.accent : Color.popups.text
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: Style.space(1)
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: preferences.activeTab = index
          }
        }
      }

      Item { Layout.fillWidth: true }
    }

    Item {
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: preferences.activeTab === 0

      Flickable {
        id: generalFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: generalColumn.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        clip: true

        ColumnLayout {
          id: generalColumn
          width: generalFlick.width
          spacing: Style.space(8)

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(66)
            radius: Style.cornerRadius
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.045)
            border.color: Color.popups.border
            border.width: 1

            RowLayout {
              anchors.fill: parent
              anchors.margins: Style.space(11)
              spacing: Style.space(12)
              ColumnLayout {
                Layout.fillWidth: true
                spacing: Style.space(2)
                Text {
                  text: "Show provider usage on Home"
                  color: Color.popups.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  font.bold: true
                }
                Text {
                  text: "Display one horizontal usage bar for every connected provider."
                  color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }
              Switch {
                checked: controller.homeQuotaVisible
                onToggled: controller.setHomeQuotaVisible(checked)
              }
            }
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(66)
            radius: Style.cornerRadius
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.045)
            border.color: Color.popups.border
            border.width: 1

            RowLayout {
              anchors.fill: parent
              anchors.margins: Style.space(11)
              spacing: Style.space(12)
              ColumnLayout {
                Layout.fillWidth: true
                spacing: Style.space(2)
                Text {
                  text: "Open Ares with SUPER+SHIFT+A"
                  color: Color.popups.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  font.bold: true
                }
                Text {
                  text: preferences.keybindState !== "" ? preferences.keybindState
                    : "Adds a Hyprland binding to toggle the Ares panel from anywhere."
                  color: preferences.keybindState !== "" ? Color.accent
                    : Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }
              PrefButton {
                glyph: "󰌌"
                hint: "Add SUPER+SHIFT+A binding (safe to rerun)"
                onActivated: keybindApplyProc.running = true
              }
            }
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: searchShortcutRow.implicitHeight + Style.space(22)
            radius: Style.cornerRadius
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.045)
            border.color: Color.popups.border
            border.width: 1

            RowLayout {
              id: searchShortcutRow
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(11)
              spacing: Style.space(12)
              ColumnLayout {
                Layout.fillWidth: true
                spacing: Style.space(2)
                Text {
                  text: qsTr("Search shortcut")
                  color: Color.popups.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  font.bold: true
                }
                Text {
                  Layout.fillWidth: true
                  text: qsTr("Opens room search only while Ares is focused; does not change Hyprland bindings.")
                  wrapMode: Text.WordWrap
                  color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }
              ComboBox {
                objectName: "searchShortcutSelector"
                Layout.preferredWidth: Style.space(145)
                model: ["Ctrl+Space", "Alt+Space"]
                currentIndex: controller.searchShortcut === "Alt+Space" ? 1 : 0
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                palette.text: Color.popups.text
                palette.buttonText: Color.popups.text
                palette.button: Color.popups.background
                palette.base: Color.popups.background
                palette.highlight: Color.accent
                Accessible.name: qsTr("Search shortcut")
                onActivated: controller.setSearchShortcut(currentText)
              }
            }
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: terminalAppearanceColumn.implicitHeight + Style.space(22)
            radius: Style.cornerRadius
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.045)
            border.color: Color.popups.border
            border.width: 1

            ColumnLayout {
              id: terminalAppearanceColumn
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(11)
              spacing: Style.space(8)

              Text {
                text: "TERMINAL APPEARANCE"
                color: Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                font.bold: true
              }

              RowLayout {
                Layout.fillWidth: true
                Text {
                  Layout.fillWidth: true
                  text: "Terminal font size  ·  Ctrl++ / Ctrl+-"
                  color: Color.popups.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }
                PrefButton {
                  glyph: "−"
                  hint: "Decrease terminal font size"
                  onActivated: controller.setTerminalFontSize(controller.terminalFontSize - 1)
                }
                Text {
                  text: controller.terminalFontSize + " pt"
                  color: Color.popups.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }
                PrefButton {
                  glyph: "+"
                  hint: "Increase terminal font size"
                  onActivated: controller.setTerminalFontSize(controller.terminalFontSize + 1)
                }
              }

              Text {
                text: "Terminal background  ·  follows the active theme"
                color: Color.popups.text
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(8)
                Repeater {
                  model: [{ key: "popups.background", label: "Popup" }, { key: "background", label: "Desktop" },
                    { key: "muted", label: "Muted" }, { key: "accent", label: "Accent" }]
                  delegate: Rectangle {
                    required property var modelData
                    readonly property bool selected: controller.terminalBackgroundRole === modelData.key
                    Layout.preferredWidth: Style.space(90)
                    Layout.preferredHeight: Style.space(29)
                    radius: Style.cornerRadius
                    color: selected ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18) : Color.popups.background
                    border.color: selected ? Color.accent : Color.popups.border
                    border.width: 1
                    Rectangle {
                      anchors.left: parent.left
                      anchors.leftMargin: Style.space(7)
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(12)
                      height: width
                      radius: width / 2
                      color: modelData.key === "accent" ? Color.accent : modelData.key === "muted" ? Color.muted
                        : modelData.key === "background" ? Color.background : Color.popups.background
                      border.color: Color.popups.text
                    }
                    Text {
                      anchors.right: parent.right
                      anchors.rightMargin: Style.space(8)
                      anchors.verticalCenter: parent.verticalCenter
                      text: modelData.label
                      color: Color.popups.text
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: controller.setTerminalBackgroundRole(modelData.key)
                    }
                  }
                }
              }

              RowLayout {
                Layout.fillWidth: true
                Text {
                  text: "Terminal transparency"
                  color: Color.popups.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                }
                Slider {
                  Layout.fillWidth: true
                  from: 0
                  to: 1
                  stepSize: 0.05
                  value: 1 - controller.terminalBackgroundOpacity
                  onMoved: controller.setTerminalBackgroundOpacity(1 - value)
                }
                Text {
                  text: Math.round((1 - controller.terminalBackgroundOpacity) * 100) + "%"
                  color: Color.popups.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(10)
                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: Style.space(2)
                  Text {
                    text: "Read-only terminal previews"
                    color: Color.popups.text
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                  }
                  Text {
                    text: "Click a tile to grow it and work in the full terminal."
                    color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                }
                Switch {
                  checked: controller.terminalPreviewReadOnly
                  onToggled: controller.setTerminalPreviewReadOnly(checked)
                }
              }
            }
          }

          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: tokenColumn.implicitHeight + Style.space(22)
            radius: Style.cornerRadius
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.045)
            border.color: Color.popups.border
            border.width: 1

            ColumnLayout {
              id: tokenColumn
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(11)
              spacing: Style.space(8)

              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(12)
                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: Style.space(2)
                  Text {
                    text: "Mobile access link & token"
                    color: Color.popups.text
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    font.bold: true
                  }
                  Text {
                    Layout.fillWidth: true
                    text: controller.accessTokenMasked !== ""
                      ? "Current token " + controller.accessTokenMasked + " · the copied link opens signed in. Regenerating disconnects every browser."
                      : "Service offline — the token is unavailable."
                    wrapMode: Text.Wrap
                    color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                }
                PrefButton {
                  glyph: "󰆏"
                  hint: "Copy access link (token included)"
                  onActivated: controller.copyAccess()
                }
                PrefButton {
                  glyph: "󰑐"
                  hint: "Regenerate token (invalidates old links)"
                  onActivated: controller.regenerateAccessToken()
                }
              }

              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(6)
                TextField {
                  id: tokenInput
                  Layout.fillWidth: true
                  placeholderText: "Custom token · 32–256 letters, digits, _ or -"
                  color: Color.popups.text
                  font.family: Style.font.family
                  selectByMouse: true
                  background: Rectangle {
                    radius: Style.space(4)
                    color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.055)
                    border.color: tokenInput.activeFocus ? Color.accent : Color.popups.border
                  }
                }
                PrefButton {
                  glyph: "󰄬"
                  hint: "Save custom token"
                  onActivated: {
                    var value = tokenInput.text.trim()
                    if (value.length === 0) return
                    tokenInput.text = ""
                    controller.setAccessToken(value)
                  }
                }
              }

              Text {
                Layout.fillWidth: true
                visible: controller.accessTokenStatus !== ""
                text: controller.accessTokenStatus
                wrapMode: Text.Wrap
                color: Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }
          }

          Text {
            visible: controller.quotaProviders.length > 0
            text: "PROVIDERS ON HOME"
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: Style.space(1)
          }

          GridLayout {
            Layout.fillWidth: true
            visible: controller.quotaProviders.length > 0
            columns: 2
            columnSpacing: Style.space(8)
            rowSpacing: Style.space(6)

            Repeater {
              model: controller.quotaProviders
              delegate: Rectangle {
                required property var modelData
                Layout.fillWidth: true
                Layout.preferredHeight: Style.space(42)
                radius: Style.cornerRadius
                color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.035)
                border.color: Color.popups.border
                border.width: 1

                RowLayout {
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(10)
                  anchors.rightMargin: Style.space(6)
                  spacing: Style.space(8)
                  Text {
                    Layout.fillWidth: true
                    text: String(modelData.provider || "provider").replace(/-/g, " ")
                    color: Color.popups.text
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    font.bold: true
                    elide: Text.ElideRight
                  }
                  Switch {
                    checked: controller.homeQuotaProviderVisible(modelData.provider)
                    enabled: controller.homeQuotaVisible
                    onToggled: controller.setHomeQuotaProviderVisible(modelData.provider, checked)
                  }
                }
              }
            }
          }
        }
      }
    }

    Item {
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: preferences.activeTab === 1

      Flickable {
        id: botsFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: botsColumn.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        clip: true

        ColumnLayout {
          id: botsColumn
          width: botsFlick.width
          spacing: Style.space(8)

          Text {
            Layout.fillWidth: true
            visible: controller.bots.length === 0
            text: "No bots registered yet. They appear here once Hermes Desktop syncs profiles."
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
          }

          Text {
            visible: controller.bots.length > 0
            text: "BOTS"
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: Style.space(1)
          }

          Repeater {
            model: controller.bots
            delegate: Rectangle {
              id: botCard
              required property var modelData
              property string draftName: String(modelData.name || modelData.id)
              property string draftExpertise: String(modelData.description || "")
              property string draftShape: String(modelData.faceShape || "diamond")
              property string draftColor: String(modelData.colorRole || "accent")
              property string draftModelKey: botCard.modelChoiceFor(String(modelData.provider || ""), String(modelData.model || ""))
              readonly property var choices: {
                var list = []
                var seen = {}
                var items = controller.models || []
                for (var i = 0; i < items.length; ++i) {
                  var key = botCard.modelChoiceFor(String(items[i].provider || ""), String(items[i].model || ""))
                  if (key !== "" && !seen[key]) { seen[key] = true; list.push(key) }
                }
                if (botCard.draftModelKey !== "" && list.indexOf(botCard.draftModelKey) < 0)
                  list.unshift(botCard.draftModelKey)
                return list
              }
              Layout.fillWidth: true
              Layout.preferredHeight: Style.space(146)
              radius: Style.cornerRadius
              color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.045)
              border.color: controller.botColorForRole(draftColor)
              border.width: 1

              function modelChoiceFor(provider, model) {
                if (!model) return ""
                return provider ? provider + " / " + model : model
              }

              RowLayout {
                anchors.fill: parent
                anchors.margins: Style.space(11)
                spacing: Style.space(12)

                BotFace {
                  Layout.preferredWidth: Style.space(34)
                  Layout.preferredHeight: Style.space(34)
                  size: Style.space(34)
                  shapeName: botCard.draftShape === "circle" ? "oval"
                    : botCard.draftShape === "hexagon" ? "square" : botCard.draftShape
                  colorHex: String(controller.botColorForRole(botCard.draftColor))
                }

                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: Style.space(4)
                  Text {
                    text: String(botCard.modelData.profile || botCard.modelData.id)
                    color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.58)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                  ComboBox {
                    Layout.fillWidth: true
                    model: botCard.choices
                    currentIndex: Math.max(0, botCard.choices.indexOf(botCard.draftModelKey))
                    onActivated: function(index) { botCard.draftModelKey = botCard.choices[index] }
                    background: Rectangle {
                      radius: Style.space(4)
                      color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.055)
                      border.color: parent.activeFocus ? Color.accent : Color.popups.border
                    }
                    contentItem: Text {
                      leftPadding: Style.space(8)
                      rightPadding: Style.space(8)
                      verticalAlignment: Text.AlignVCenter
                      text: parent.displayText
                      color: Color.popups.text
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideRight
                    }
                    popup: Popup {
                      width: parent.width
                      implicitHeight: Math.min(contentItem.implicitHeight, Style.space(260))
                      contentItem: ListView {
                        clip: true
                        implicitHeight: contentHeight
                        model: botCard.choices
                        delegate: ItemDelegate {
                          width: ListView.view.width
                          text: modelData
                          palette.text: Color.popups.text
                          palette.highlightedText: Color.popups.text
                          palette.highlight: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.25)
                          font.pixelSize: Style.font.caption
                          onClicked: {
                            botCard.draftModelKey = modelData
                            parent.parent.close()
                          }
                        }
                      }
                      background: Rectangle {
                        color: Color.popups.background
                        border.color: Color.popups.border
                        radius: Style.space(4)
                      }
                    }
                  }
                  TextField {
                    Layout.fillWidth: true
                    text: botCard.draftName
                    placeholderText: "Bot display name"
                    color: Color.popups.text
                    font.family: Style.font.family
                    selectByMouse: true
                    onTextEdited: botCard.draftName = text
                    background: Rectangle {
                      radius: Style.space(4)
                      color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.055)
                      border.color: parent.activeFocus ? Color.accent : Color.popups.border
                    }
                  }
                  TextField {
                    Layout.fillWidth: true
                    text: botCard.draftExpertise
                    placeholderText: "Expertise — what is this bot good at?"
                    color: Color.popups.text
                    font.family: Style.font.family
                    selectByMouse: true
                    onTextEdited: botCard.draftExpertise = text
                    background: Rectangle {
                      radius: Style.space(4)
                      color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.055)
                      border.color: parent.activeFocus ? Color.accent : Color.popups.border
                    }
                  }
                }

                ColumnLayout {
                  spacing: Style.space(6)
                  RowLayout {
                    spacing: Style.space(3)
                    Repeater {
                      model: ["oval", "square", "triangle", "diamond"]
                      delegate: Rectangle {
                        required property var modelData
                        Layout.preferredWidth: Style.space(20)
                        Layout.preferredHeight: Style.space(20)
                        radius: Style.space(4)
                        color: botCard.draftShape === modelData
                          ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.25) : "transparent"
                        border.color: botCard.draftShape === modelData ? Color.accent : Color.popups.border
                        border.width: 1
                        BotFace {
                          anchors.centerIn: parent
                          size: Style.space(13)
                          shapeName: String(parent.modelData)
                          working: false
                          waiting: false
                        }
                        MouseArea {
                          anchors.fill: parent
                          cursorShape: Qt.PointingHandCursor
                          onClicked: botCard.draftShape = String(parent.modelData)
                        }
                      }
                    }
                  }
                  RowLayout {
                    spacing: Style.space(3)
                    Repeater {
                      model: ["accent", "success", "warning", "urgent", "info", "muted"]
                      delegate: Rectangle {
                        required property var modelData
                        Layout.preferredWidth: Style.space(18)
                        Layout.preferredHeight: Style.space(18)
                        radius: width / 2
                        color: controller.botColorForRole(String(modelData))
                        border.color: botCard.draftColor === modelData ? Color.popups.text : "transparent"
                        border.width: 2
                        MouseArea {
                          anchors.fill: parent
                          cursorShape: Qt.PointingHandCursor
                          onClicked: botCard.draftColor = String(parent.modelData)
                        }
                      }
                    }
                  }
                }

                PrefButton {
                  glyph: "󰄬"
                  hint: "Save bot identity"
                  onActivated: {
                    var key = botCard.draftModelKey
                    var sep = key.indexOf(" / ")
                    var choice = sep >= 0
                      ? { provider: key.substring(0, sep), model: key.substring(sep + 3) }
                      : { provider: "", model: key }
                    controller.updateBotPreferences(botCard.modelData, botCard.draftName,
                      botCard.draftExpertise, botCard.draftShape, botCard.draftColor, choice)
                  }
                }
              }
            }
          }
        }
      }
    }

    Item {
      Layout.fillWidth: true
      Layout.fillHeight: true
      visible: preferences.activeTab === 2

      ColumnLayout {
        anchors.fill: parent
        spacing: Style.space(8)

        Text {
          text: "PEER IDENTITY"
          color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: Style.space(1)
        }

        ListView {
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true
          spacing: Style.space(8)
          model: controller.peers
          boundsBehavior: Flickable.StopAtBounds
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

          delegate: Rectangle {
            id: peerCard
            required property var modelData
            property string draftName: String(modelData.name || modelData.id)
            property string draftColor: String(modelData.colorRole || "accent")
            width: ListView.view.width
            height: Style.space(104)
            radius: Style.cornerRadius
            color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.045)
            border.color: controller.peerColor(draftColor)
            border.width: 1

            RowLayout {
              anchors.fill: parent
              anchors.margins: Style.space(11)
              spacing: Style.space(12)

              Rectangle {
                Layout.preferredWidth: Style.space(12)
                Layout.preferredHeight: Style.space(62)
                radius: width / 2
                color: controller.peerColor(peerCard.draftColor)
              }

              ColumnLayout {
                Layout.fillWidth: true
                spacing: Style.space(5)
                Text {
                  text: peerCard.modelData.id + " · " + String(peerCard.modelData.status || "unknown").toUpperCase()
                  color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.58)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
                TextField {
                  Layout.fillWidth: true
                  text: peerCard.draftName
                  placeholderText: "Peer display name"
                  color: Color.popups.text
                  font.family: Style.font.family
                  selectByMouse: true
                  onTextEdited: peerCard.draftName = text
                  background: Rectangle {
                    radius: Style.space(4)
                    color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.055)
                    border.color: parent.activeFocus ? Color.accent : Color.popups.border
                  }
                }
                Text {
                  Layout.fillWidth: true
                  text: peerCard.modelData.endpoint || "No endpoint"
                  elide: Text.ElideMiddle
                  color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.46)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }

              RowLayout {
                spacing: Style.space(4)
                Repeater {
                  model: ["accent", "success", "warning", "urgent", "info", "muted"]
                  delegate: Rectangle {
                    required property var modelData
                    Layout.preferredWidth: Style.space(22)
                    Layout.preferredHeight: Style.space(22)
                    radius: width / 2
                    color: controller.peerColor(String(modelData))
                    border.color: peerCard.draftColor === modelData ? Color.popups.text : "transparent"
                    border.width: 2
                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: peerCard.draftColor = String(parent.modelData)
                    }
                  }
                }
              }

              PrefButton {
                glyph: "󰄬"
                hint: "Save peer identity"
                onActivated: controller.updatePeerPreferences(peerCard.modelData, peerCard.draftName, peerCard.draftColor)
              }
            }
          }
        }

        Text {
          Layout.fillWidth: true
          visible: controller.peers.length === 0
          text: "No peers discovered yet. Refresh after connecting another Hermes host."
          color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.56)
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          horizontalAlignment: Text.AlignHCenter
        }
      }
    }
  }

  component PrefButton: Rectangle {
    id: prefButton
    property string glyph: ""
    property string hint: ""
    signal activated()
    implicitWidth: Style.space(32)
    implicitHeight: Style.space(32)
    radius: Style.space(4)
    color: prefMouse.containsMouse ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18) : "transparent"
    border.color: Color.popups.border
    border.width: 1
    Text { anchors.centerIn: parent; text: parent.glyph; color: Color.popups.text; font.family: Style.font.family; font.pixelSize: Style.font.body }
    ToolTip.visible: prefMouse.containsMouse
    ToolTip.text: prefButton.hint
    MouseArea { id: prefMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.activated() }
  }
}
