import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Unified chat transcript. Shared by the compact popup and the extended
// workspace views; never fork this rendering logic per surface.
Item {
    id: transcriptRoot

    required property var controller
    required property var bridge
    required property string roomId
    property var eventModel: []
    property color foreground: Color.popups.text
    property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.52)
    property color surface: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.045)
    property color surfaceStrong: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.09)
    property color accentWash: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.14)
    property string fontFamily: Style.font.family

    property var renderedEvents: []
    property var pendingEvents: []
    property bool autoFollow: true
    property bool loadingOlder: false
    property int bubbleIndent: 12

    // Palette resolved from caller tokens + theme; placeholders in the
    // service-side HTML are substituted with these live values.
    // Alpha-carrying surfaces are injected as CSS rgba() by styledHtml().
    readonly property color accentColor: Color.accent
    readonly property color dangerColor: Color.urgent

    ListModel { id: displayModel }

    onAutoFollowChanged: {
        if (!autoFollow) return
        if (pendingEvents.length > 0) applyPendingEvents()
        Qt.callLater(positionTail)
    }

    onRoomIdChanged: {
        displayModel.clear()
        renderedEvents = []
        pendingEvents = []
        autoFollow = true
        loadingOlder = false
    }

    onEventModelChanged: {
        if (autoFollow || loadingOlder) {
            renderedEvents = eventModel
            syncDisplay(eventModel)
            if (autoFollow) Qt.callLater(positionTail)
        } else {
            pendingEvents = eventModel
        }
    }

    Connections {
        target: transcriptRoot.bridge
        function onHistoryPageLoaded(roomId, count) {
            if (roomId === transcriptRoot.roomId) transcriptRoot.loadingOlder = false
        }
    }

    function applyPendingEvents() {
        var next = pendingEvents
        pendingEvents = []
        renderedEvents = next
        syncDisplay(next)
    }

    // Keep the tail pinned while streaming. ListModel inserts do not reset the
    // view, so this only matters for appends or last-row height growth.
    function positionTail() {
        if (displayModel.count === 0) return
        if (transcript.visibleArea.yPosition < 0.995) transcript.positionViewAtEnd()
    }

    // Qt serializes colors as "#rrggbb" when opaque and "#aarrggbb" otherwise;
    // HTML wants "#rrggbb" in both cases, so strip the alpha only when present.
    function _hex(colorValue) {
        var s = colorValue.toString()
        return s.length === 9 ? "#" + s.substring(3) : s
    }

    // Alpha-carrying theme surfaces must reach the rich text engine as CSS
    // rgba(): stripping the alpha (as _hex does) would composite them over
    // white, making code blocks white-on-white.
    function _cssRgba(colorValue, alpha) {
        return "rgba(" + Math.round(colorValue.r * 255) + "," + Math.round(colorValue.g * 255)
            + "," + Math.round(colorValue.b * 255) + "," + alpha + ")"
    }

    // Resolve the service-side placeholders against live theme tokens.
    function styledHtml(rawHtml) {
        if (!rawHtml) return ""
        return rawHtml
            .split("__ARES_CODE_BG__").join(_cssRgba(foreground, 0.16))
            .split("__ARES_SURFACE__").join(_cssRgba(foreground, 0.14))
            .split("__ARES_ACCENT__").join(_hex(accentColor))
            .split("__ARES_FG_DIM__").join(_hex(dim))
    }

    readonly property string cursorSpan: "<font color=\"" + _hex(accentColor) + "\">▍</font>"

    // Full-size image preview: click any image card to inspect it, click the
    // backdrop to dismiss. Lives here so it works on every surface that
    // embeds the transcript (compact popup and Grande alike).
    property string imagePreviewSource: ""
    Rectangle {
        anchors.fill: parent
        z: 100
        visible: transcriptRoot.imagePreviewSource !== ""
        color: Qt.rgba(0, 0, 0, 0.86)
        Image {
            anchors.fill: parent
            anchors.margins: Style.space(24)
            source: transcriptRoot.imagePreviewSource
            fillMode: Image.PreserveAspectFit
            asynchronous: true
        }
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            anchors.bottomMargin: Style.space(8)
            text: "Click anywhere to close"
            color: transcriptRoot.dim
            font.family: transcriptRoot.fontFamily
            font.pixelSize: Style.font.caption
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: transcriptRoot.imagePreviewSource = ""
        }
    }

    function _rowFor(event) {
        return {
            eventId: String(event.id || ""),
            eventKind: String(event.kind || ""),
            eventText: String(event.text || ""),
            eventHtml: String(event.html || ""),
            eventPayload: event.payload || ({}),
            event: event
        }
    }

    // Incremental diff: inserts, moves, updates and removals applied one row
    // at a time. Delegates are never rebuilt wholesale, which is what keeps
    // scrolling smooth while streaming and stops messages from flashing.
    function syncDisplay(next) {
        var current = []
        for (var m = 0; m < displayModel.count; ++m) current.push(displayModel.get(m))
        var index = 0
        for (var n = 0; n < next.length; ++n) {
            var ev = next[n]
            var found = -1
            for (var c = index; c < current.length; ++c) {
                if (current[c].eventId === ev.id) { found = c; break }
            }
            if (found < 0) {
                var row = _rowFor(ev)
                displayModel.insert(index, row)
                current.splice(index, 0, row)
            } else if (found !== index) {
                displayModel.move(found, index, 1)
                current.splice(index, 0, current.splice(found, 1)[0])
            }
            if (JSON.stringify(current[index].event) !== JSON.stringify(ev)) {
                var updated = _rowFor(ev)
                displayModel.set(index, updated)
                current[index] = updated
            }
            ++index
        }
        while (displayModel.count > next.length) displayModel.remove(displayModel.count - 1)
    }

    function longBody(text) {
        var limit = 12000
        if (text.length <= limit) return text
        return text.substring(0, limit) + "\n… (" + (text.length - limit) + " more chars)"
    }

    ListView {
        id: transcript
        anchors.fill: parent
        clip: true
        spacing: 10
        topMargin: 6
        bottomMargin: 12
        model: displayModel
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AlwaysOff }
        property string monospaceFamily: "monospace"
        // Mouse-wheel scrolling (touchpads included) must break tail-follow the
        // same way dragging does; otherwise every new message yanks the view
        // back to the bottom while the user is reading up.
        WheelHandler {
            onWheel: function(event) {
                if (event.angleDelta.y > 0 && transcriptRoot.autoFollow
                    && transcript.visibleArea.yPosition < 0.9)
                    transcriptRoot.autoFollow = false
                event.accepted = false
            }
        }
        onContentYChanged: {
            if (!transcriptRoot.autoFollow && transcript.visibleArea.yPosition >= 0.995)
                transcriptRoot.autoFollow = true
            else if (transcriptRoot.autoFollow && (transcript.dragging || transcript.flicking)
                     && transcript.visibleArea.yPosition < 0.9)
                transcriptRoot.autoFollow = false
            if (atYBeginning && !transcriptRoot.loadingOlder && transcriptRoot.bridge
                && transcriptRoot.bridge.historyHasOlder) {
                transcriptRoot.loadingOlder = true
                transcriptRoot.bridge.loadOlder()
            }
        }
        // Subtle entrance for genuinely new rows; in-place updates are not
        // animated, which keeps streaming text stable.
        add: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 180 } }

        delegate: Item {
            id: eventItem
            required property string eventId
            required property string eventKind
            required property string eventText
            required property string eventHtml
            required property var eventPayload
            required property var event

            width: transcript.width
            height: wrapper.height

            readonly property bool isApproval: eventKind === "approval.request"
            readonly property bool isToolStart: eventKind === "tool.start" || eventKind === "tool.generating" || eventKind === "tool.progress"
            readonly property bool isToolResult: eventKind === "tool.complete" || eventKind === "tool.output_risk"
            readonly property bool isActivity: isToolStart || isToolResult
                || eventKind === "tool.result" || eventKind === "artifact.available"
            readonly property bool isRisk: eventKind === "tool.output_risk"
            readonly property bool isThinking: eventKind.indexOf("reasoning") === 0 || eventKind.indexOf("thinking") === 0
            readonly property bool isImage: eventKind === "artifact.available" && /\.(png|jpe?g|webp|gif|bmp)$/i.test(eventPayload.name || "")
            readonly property bool isUser: eventKind === "message.user" || event.author === "user"
            readonly property bool isAssistant: eventKind === "message.assistant"
            readonly property bool isError: eventKind === "error"
            readonly property bool isSystem: !isUser && !isAssistant && !isActivity && !isRisk && !isThinking && !isImage && !isError
            readonly property bool streaming: eventPayload.streaming === true
            property bool expanded: false
            onEventIdChanged: expanded = false
            function lastDetailLine() {
                var lines = String(eventItem.eventText || "").split(/\r?\n/)
                for (var i = lines.length - 1; i >= 0; --i) {
                    var line = lines[i].trim()
                    // Trailing closing brackets of a JSON block carry no
                    // information; keep walking to the last content line.
                    if (line !== "" && !/^[\}\]\),;]+$/.test(line)) return line
                }
                return lines.length > 0 ? lines[lines.length - 1].trim() : ""
            }
            readonly property bool alignRight: isUser
            readonly property bool usesRichText: isAssistant && eventHtml !== ""
            readonly property string eventTime: {
                var stamp = event.createdAt || eventPayload.createdAt || 0
                if (!stamp) return ""
                return Qt.formatTime(new Date(Number(stamp) * 1000), "hh:mm")
            }

            RowLayout {
                id: wrapper
                width: eventItem.width
                spacing: Style.space(8)

                Rectangle {
                    Layout.preferredWidth: Style.space(3)
                    Layout.preferredHeight: 1
                    color: "transparent"
                    visible: eventItem.alignRight
                }

                Loader {
                    id: contentLoader
                    readonly property real maxBodyWidth: eventItem.width - (eventItem.alignRight ? 0 : transcriptRoot.bubbleIndent * 2) - Style.space(30)
                    Layout.maximumWidth: maxBodyWidth
                    // Card-style rows always claim the full body width. Depending
                    // on the loaded item's implicitWidth collapses rows whose
                    // children are all Layout.fillWidth (thinking/activity).
                    readonly property bool fullWidthCard: eventItem.usesRichText || eventItem.isActivity
                        || eventItem.isThinking || eventItem.isApproval || eventItem.isRisk
                        || eventItem.isError || eventItem.isSystem
                    Layout.preferredWidth: fullWidthCard ? maxBodyWidth : Math.min(implicitWidth, maxBodyWidth)
                    sourceComponent: {
                        if (eventItem.isImage) return imageCard
                        if (eventItem.isApproval) return approvalCard
                        if (eventItem.isActivity) return activityCard
                        if (eventItem.isRisk) return riskCard
                        if (eventItem.isThinking) return thinkingCard
                        if (eventItem.isUser) return userBubble
                        if (eventItem.isAssistant) return assistantCard
                        if (eventItem.isError) return errorCard
                        return systemCard
                    }
                }

                Rectangle {
                    Layout.preferredWidth: Style.space(3)
                    Layout.preferredHeight: 1
                    color: "transparent"
                    visible: !eventItem.alignRight
                }
            }

            Component {
                id: assistantCard
                ColumnLayout {
                    spacing: Style.space(2)
                    Text {
                        Layout.maximumWidth: contentLoader.maxBodyWidth
                        Layout.fillWidth: true
                        textFormat: Text.RichText
                        text: transcriptRoot.styledHtml(eventItem.eventHtml) + (eventItem.streaming ? transcriptRoot.cursorSpan : "")
                        wrapMode: Text.Wrap
                        color: transcriptRoot.foreground
                        font.family: transcriptRoot.fontFamily
                        font.pixelSize: Style.font.body
                        linkColor: transcriptRoot.accentColor
                        onLinkActivated: function(link) { Qt.openUrlExternally(link) }
                    }
                    Text {
                        visible: text !== "" && !eventItem.streaming
                        text: eventItem.eventTime
                        color: transcriptRoot.dim
                        font.family: transcriptRoot.fontFamily
                        font.pixelSize: Math.max(8, Style.font.body - 3)
                    }
                }
            }

            Component {
                id: userBubble
                ColumnLayout {
                    spacing: Style.space(2)
                    Rectangle {
                        Layout.maximumWidth: contentLoader.maxBodyWidth
                        Layout.alignment: Qt.AlignRight
                        implicitWidth: Math.min(bubbleText.implicitWidth + Style.space(16), contentLoader.maxBodyWidth)
                        implicitHeight: bubbleText.implicitHeight + Style.space(10)
                        radius: Style.cornerRadius
                        color: transcriptRoot.accentWash
                        Text {
                            id: bubbleText
                            anchors.centerIn: parent
                            width: parent.width - Style.space(16)
                            text: transcriptRoot.longBody(eventItem.eventText)
                            wrapMode: Text.Wrap
                            color: transcriptRoot.foreground
                            font.family: transcriptRoot.fontFamily
                            font.pixelSize: Style.font.body
                        }
                    }
                    Text {
                        Layout.alignment: Qt.AlignRight
                        visible: text !== ""
                        text: eventItem.eventTime
                        color: transcriptRoot.dim
                        font.family: transcriptRoot.fontFamily
                        font.pixelSize: Math.max(8, Style.font.body - 3)
                    }
                }
            }

            Component {
                id: thinkingCard
                RowLayout {
                    spacing: Style.space(6)
                    Rectangle {
                        Layout.preferredWidth: Style.space(2)
                        Layout.fillHeight: true
                        radius: Style.cornerRadius
                        color: Qt.alpha(transcriptRoot.accentColor, 0.24)
                    }
                    ColumnLayout {
                        Layout.maximumWidth: contentLoader.maxBodyWidth - Style.space(8)
                        spacing: Style.space(2)
                        Text {
                            text: eventItem.streaming ? "Thinking…" : "Thought"
                            color: transcriptRoot.dim
                            font.family: transcriptRoot.fontFamily
                            font.pixelSize: Style.font.body
                            font.italic: true
                            visible: eventItem.eventText === ""
                        }
                        Text {
                            Layout.fillWidth: true
                            visible: eventItem.eventText !== ""
                            text: transcriptRoot.longBody(eventItem.eventText) + (eventItem.streaming ? " ▍" : "")
                            wrapMode: Text.Wrap
                            color: transcriptRoot.dim
                            font.family: transcriptRoot.fontFamily
                            font.pixelSize: Style.font.body
                            opacity: eventItem.streaming ? 0.9 : 0.72
                            font.italic: true
                            maximumLineCount: 8
                            elide: Text.ElideRight
                        }
                    }
                }
            }

            Component {
                id: activityCard
                ColumnLayout {
                    Layout.maximumWidth: contentLoader.maxBodyWidth
                    spacing: Style.space(3)

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.maximumWidth: contentLoader.maxBodyWidth
                        implicitHeight: activityContent.implicitHeight + Style.space(12)
                        radius: Style.cornerRadius
                        color: transcriptRoot.surface
                        border.width: 1
                        border.color: transcriptRoot.surfaceStrong

                        RowLayout {
                            id: activityContent
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.margins: Style.space(6)
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: Style.space(8)

                            Rectangle {
                                Layout.preferredWidth: Style.space(3)
                                Layout.preferredHeight: Style.space(28)
                                radius: Style.cornerRadius
                                color: Qt.alpha(transcriptRoot.accentColor, eventItem.isToolStart ? 0.32 : 0.12)
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: Style.space(2)

                                RowLayout {
                                    spacing: Style.space(6)
                                    Text {
                                        text: eventItem.isToolStart ? "󰆍" : eventKind === "artifact.available" ? "󰋩" : "󰁔"
                                        color: transcriptRoot.dim
                                        font.family: transcriptRoot.fontFamily
                                        font.pixelSize: Style.font.body
                                    }
                                    Text {
                                        text: activityTitle()
                                        color: transcriptRoot.foreground
                                        font.family: transcriptRoot.fontFamily
                                        font.pixelSize: Style.font.body
                                        font.weight: Font.Medium
                                        elide: Text.ElideRight
                                        Layout.maximumWidth: Style.space(150)
                                    }
                                    // Collapsed tail: the CLI one-line summary when the
                                    // gateway provides one, else the last line of this
                                    // tool block, so each card previews its own outcome.
                                    Text {
                                        Layout.fillWidth: true
                                        visible: !eventItem.expanded && eventItem.eventText !== ""
                                        text: eventItem.eventPayload.summary || eventItem.lastDetailLine()
                                        elide: Text.ElideRight
                                        color: transcriptRoot.dim
                                        font.family: transcript.monospaceFamily
                                        font.pixelSize: Style.font.body
                                        opacity: 0.8
                                    }
                                }

                                // Expanded detail: full block, only on demand.
                                Text {
                                    Layout.fillWidth: true
                                    visible: eventItem.expanded && eventItem.eventText !== ""
                                    text: transcriptRoot.longBody(eventItem.eventText)
                                    wrapMode: Text.WrapAnywhere
                                    color: transcriptRoot.dim
                                    font.family: transcript.monospaceFamily
                                    font.pixelSize: Style.font.body
                                    maximumLineCount: 40
                                    elide: Text.ElideRight
                                }
                            }

                            Text {
                                visible: eventItem.eventText !== ""
                                text: eventItem.expanded ? "󰁍" : "󰁌"
                                color: transcriptRoot.dim
                                font.family: transcriptRoot.fontFamily
                                font.pixelSize: Style.font.body
                                opacity: expandArea.containsMouse ? 1.0 : 0.55
                                Behavior on opacity { NumberAnimation { duration: 120 } }
                                MouseArea {
                                    id: expandArea
                                    anchors.fill: parent
                                    anchors.margins: -Style.space(4)
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: eventItem.expanded = !eventItem.expanded
                                }
                            }
                        }
                    }

                    Text {
                        visible: eventItem.eventTime !== ""
                        text: eventItem.eventTime
                        color: transcriptRoot.dim
                        font.family: transcriptRoot.fontFamily
                        font.pixelSize: Math.max(8, Style.font.body - 3)
                    }
                }
            }

            function activityTitle() {
                if (eventItem.isToolStart) return eventItem.eventPayload.title || eventItem.eventPayload.toolName || eventItem.eventPayload.name || "Tool call"
                if (eventKind === "tool.complete") return eventItem.eventPayload.name || eventItem.eventPayload.title || "Tool result"
                if (eventKind === "tool.result") return eventItem.eventPayload.name || "Tool result"
                return eventItem.eventPayload.name || eventItem.eventPayload.title || "Artifact"
            }

            Component {
                id: approvalCard
                RowLayout {
                    spacing: Style.space(6)
                    Rectangle {
                        Layout.preferredWidth: Style.space(3)
                        Layout.fillHeight: true
                        radius: Style.cornerRadius
                        color: Qt.alpha(transcriptRoot.accentColor, 0.5)
                    }
                    ColumnLayout {
                        Layout.maximumWidth: contentLoader.maxBodyWidth - Style.space(8)
                        spacing: Style.space(4)
                        Text {
                            Layout.fillWidth: true
                            text: eventItem.eventPayload.tool || eventItem.eventPayload.title || "Approval required"
                            color: transcriptRoot.foreground
                            font.family: transcriptRoot.fontFamily
                            font.pixelSize: Style.font.body
                            font.weight: Font.Medium
                        }
                        Text {
                            Layout.fillWidth: true
                            visible: eventItem.eventText !== ""
                            text: eventItem.eventText
                            wrapMode: Text.Wrap
                            color: transcriptRoot.dim
                            font.family: transcriptRoot.fontFamily
                            font.pixelSize: Style.font.body
                        }
                        RowLayout {
                            spacing: Style.space(6)
                            Rectangle {
                                implicitWidth: approveLabel.implicitWidth + Style.space(14)
                                implicitHeight: approveLabel.implicitHeight + Style.space(8)
                                radius: Style.cornerRadius
                                color: transcriptRoot.accentWash
                                Text {
                                    id: approveLabel
                                    anchors.centerIn: parent
                                    text: "Approve"
                                    color: transcriptRoot.foreground
                                    font.family: transcriptRoot.fontFamily
                                    font.pixelSize: Style.font.body
                                }
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        if (transcriptRoot.bridge) transcriptRoot.bridge.approve(eventItem.event, "approve", function() {})
                                    }
                                }
                            }
                            Rectangle {
                                implicitWidth: denyLabel.implicitWidth + Style.space(14)
                                implicitHeight: denyLabel.implicitHeight + Style.space(8)
                                radius: Style.cornerRadius
                                color: transcriptRoot.surface
                                Text {
                                    id: denyLabel
                                    anchors.centerIn: parent
                                    text: "Deny"
                                    color: transcriptRoot.dim
                                    font.family: transcriptRoot.fontFamily
                                    font.pixelSize: Style.font.body
                                }
                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        if (transcriptRoot.bridge) transcriptRoot.bridge.approve(eventItem.event, "deny", function() {})
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Component {
                id: riskCard
                RowLayout {
                    spacing: Style.space(6)
                    Rectangle {
                        Layout.preferredWidth: Style.space(3)
                        Layout.fillHeight: true
                        radius: Style.cornerRadius
                        color: Qt.alpha(transcriptRoot.dangerColor, 0.6)
                    }
                    Text {
                        Layout.maximumWidth: contentLoader.maxBodyWidth - Style.space(8)
                        text: eventItem.eventText
                        wrapMode: Text.Wrap
                        color: transcriptRoot.dangerColor
                        font.family: transcriptRoot.fontFamily
                        font.pixelSize: Style.font.body
                    }
                }
            }

            Component {
                id: imageCard
                ColumnLayout {
                    spacing: Style.space(3)
                    property string imageSource: {
                        var name = eventItem.eventPayload.name || ""
                        var workspace = eventItem.eventPayload.workspace || ""
                        if (name === "" || workspace === "") return ""
                        var prefix = String(workspace).replace(/\/+$/, "")
                        if (prefix.startsWith("file://")) prefix = prefix.slice(7)
                        return "file://" + prefix + "/" + encodeURI(name)
                    }
                    Rectangle {
                        Layout.maximumWidth: contentLoader.maxBodyWidth
                        Layout.preferredWidth: Math.min(Style.space(280), thumbnail.implicitWidth)
                        Layout.preferredHeight: Math.min(Style.space(180), thumbnail.implicitHeight)
                        radius: Style.cornerRadius
                        color: transcriptRoot.surfaceStrong
                        clip: true
                        Image {
                            id: thumbnail
                            anchors.fill: parent
                            source: parent.parent.imageSource
                            fillMode: Image.PreserveAspectCrop
                            sourceSize.width: Style.space(560)
                            asynchronous: true
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: transcriptRoot.imagePreviewSource = thumbnail.source
                        }
                    }
                    Text {
                        text: eventItem.eventPayload.name || ""
                        color: transcriptRoot.dim
                        font.family: transcriptRoot.fontFamily
                        font.pixelSize: Style.font.body
                    }
                }
            }

            Component {
                id: errorCard
                Text {
                    Layout.maximumWidth: contentLoader.maxBodyWidth
                    text: eventItem.eventText
                    wrapMode: Text.Wrap
                    color: transcriptRoot.dangerColor
                    font.family: transcriptRoot.fontFamily
                    font.pixelSize: Style.font.body
                }
            }

            Component {
                id: systemCard
                Text {
                    Layout.maximumWidth: contentLoader.maxBodyWidth
                    text: eventItem.eventText || eventKind
                    wrapMode: Text.Wrap
                    color: transcriptRoot.dim
                    font.family: transcriptRoot.fontFamily
                    font.pixelSize: Style.font.body
                    font.italic: true
                }
            }
        }

        BusyIndicator {
            anchors.top: parent.top
            anchors.topMargin: Style.space(4)
            anchors.horizontalCenter: parent.horizontalCenter
            running: transcriptRoot.loadingOlder
            visible: running
            width: Style.space(18)
            height: Style.space(18)
        }
    }

    // Jump-to-latest pill: floats over the transcript whenever the tail is
    // not pinned, so catching up on a streaming room is one click.
    Rectangle {
        id: jumpToLatest
        readonly property bool atTail: transcript.visibleArea.yPosition >= 0.985
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        anchors.margins: Style.space(10)
        width: jumpRow.implicitWidth + Style.space(20)
        height: Style.space(30)
        radius: height / 2
        color: transcriptRoot.surfaceStrong
        border.color: transcriptRoot.accentColor
        border.width: 1
        opacity: (!atTail && displayModel.count > 0) ? 1 : 0
        visible: opacity > 0.01
        Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

        RowLayout {
            id: jumpRow
            anchors.centerIn: parent
            spacing: Style.space(4)

            Text {
                text: "󰁋"
                color: transcriptRoot.accentColor
                font.family: transcriptRoot.fontFamily
                font.pixelSize: Style.font.body
            }
            Text {
                text: "Ir al final"
                color: transcriptRoot.foreground
                font.family: transcriptRoot.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
            }
        }

        MouseArea {
            id: jumpMouse
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            enabled: jumpToLatest.opacity > 0.5
            onClicked: transcript.positionViewAtEnd()
        }

        ToolTip.visible: jumpMouse.containsMouse
        ToolTip.text: "Jump to the latest message"
        ToolTip.delay: 400
    }
}
