import json
import unittest
from pathlib import Path


class ManifestContractTests(unittest.TestCase):
    def test_remote_terminal_uses_ssh_target_without_local_herdr_fallback(self):
        root = Path(__file__).resolve().parents[1]
        terminal = (root / "TerminalChat.qml").read_text()
        client = (root / "bin" / "ares-herdr-client").read_text()
        http = (root / "server" / "src" / "http.rs").read_text()
        model = (root / "TerminalConnectionModel.js").read_text()
        self.assertIn('Connection.argumentsFor(termView.attachedAttempt.target)', terminal)
        self.assertIn('target.sessionName === "terminal" || target.sessionName === "remote"', model)
        self.assertIn('target.sshTarget, target.remoteBin, target.remoteSocket', model)
        self.assertIn('ssh -tt', client)
        self.assertIn('if target.peer.is_some()', http)

    def test_server_has_stable_lan_address(self):
        root = Path(__file__).resolve().parents[1]
        server = (root / "server" / "src" / "cli.rs").read_text()

        self.assertIn('.unwrap_or("0.0.0.0")', server)
        self.assertIn('.unwrap_or(9420)', server)

    def test_mobile_web_never_bootstraps_the_unbounded_snapshot(self):
        root = Path(__file__).resolve().parents[1]
        app = (root / "web" / "app.mjs").read_text()

        self.assertIn('api("/v1/state?compact=1")', app)
        self.assertIn('`?compact=1&eventRoomId=${encodeURIComponent(state.roomId)}`', app)
        self.assertNotIn('`?roomId=${encodeURIComponent(state.roomId)}`', app)

    def test_background_service_starts_ares_before_panel_opens(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertIn("service", manifest["kinds"])
        self.assertEqual(manifest["entryPoints"]["service"], "Service.qml")
        source = (root / "Service.qml").read_text()
        self.assertIn("/bin/ares-server", source)
        self.assertIn('"connection"', source)

    def test_qml_workspace_has_real_image_attachment_and_inline_media(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        bridge = (root / "AresBridge.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        transcript = (root / "ChatTranscript.qml").read_text()

        self.assertIn("FileDialog", panel)
        self.assertIn("pendingImages", panel)
        self.assertIn("addPendingImage", panel)
        self.assertIn("removePendingImage", panel)
        self.assertIn("uploadImage", bridge)
        self.assertIn("artifactIds", bridge)
        self.assertIn('eventKind === "artifact.available"', transcript)
        self.assertIn("Image {", transcript)
        self.assertIn("ATTACH IMAGE", workspace)

    def test_qml_navigation_is_a_compact_expandable_project_room_tree(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()

        self.assertIn("expandedProjects", panel)
        self.assertIn("navigationRevision", panel)
        self.assertIn("buildNavigationRows", panel)
        self.assertIn("toggleProject", panel)
        self.assertIn("controller.navigationRows", workspace)
        self.assertIn('modelData.kind === "project"', workspace)
        self.assertIn('treeRow.projectData.name + " (" + treeRow.modelData.roomCount + ")"', workspace)
        self.assertNotIn("id: projectList", workspace)
        self.assertNotIn("id: roomList", workspace)

    def test_sidebar_archives_recent_rows_and_project_groups_without_opening_a_chat(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        self.assertIn("function archiveProjectRooms(project)", panel)
        self.assertIn("controller.archiveProjectRooms(treeRow.projectData)", workspace)
        self.assertIn("workspace.requestArchiveSidebarSelection(treeRow.roomData)", workspace)
        self.assertIn("treeRow.roomItem && treeRow.modelData.recent", workspace)

    def test_sidebar_supports_range_selection_and_confirms_multi_archive(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        self.assertIn("property var sidebarSelectedRoomIds: []", panel)
        self.assertIn("function extendSidebarSelection(delta)", panel)
        self.assertIn("function archiveSidebarSelection()", panel)
        self.assertIn("Shift+Down", panel)
        self.assertIn("Shift+Up", panel)
        self.assertIn("controller.selectSidebarRoom(treeRow.roomData, (mouse.modifiers & Qt.ShiftModifier) !== 0", workspace)
        self.assertIn("workspace.requestArchiveSidebarSelection(treeRow.roomData)", workspace)
        self.assertIn("id: archiveSidebarSelectionDialog", workspace)

    def test_sidebar_tooltips_never_take_keyboard_focus(self):
        workspace = (Path(__file__).resolve().parents[1] / "AresWorkspace.qml").read_text()
        action_button = workspace.split("component ActionButton: Rectangle {", 1)[1].split("component QuietButton: Rectangle {", 1)[0]
        quiet_button = workspace.split("component QuietButton: Rectangle {", 1)[1].split("component SoftField: Rectangle {", 1)[0]
        self.assertIn("focus: false", action_button)
        self.assertIn("focus: false", quiet_button)

    def test_sidebar_tooltips_wait_three_seconds_before_opening(self):
        workspace = (Path(__file__).resolve().parents[1] / "AresWorkspace.qml").read_text()
        action_button = workspace.split("component ActionButton: Rectangle {", 1)[1].split("component QuietButton: Rectangle {", 1)[0]
        quiet_button = workspace.split("component QuietButton: Rectangle {", 1)[1].split("component SoftField: Rectangle {", 1)[0]
        self.assertIn("delay: 3000", action_button)
        self.assertIn("delay: 3000", quiet_button)
        self.assertIn("ToolTip.delay: 3000", workspace)

    def test_sidebar_archive_preserves_scroll_and_grip_cycles_widths(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        self.assertIn("function cycleSidebarWidth()", panel)
        self.assertIn("sidebarWidthCycleBase", panel)
        self.assertIn("controller.cycleSidebarWidth()", workspace)
        self.assertIn("function retainSidebarScroll()", workspace)
        self.assertIn("function restoreSidebarScroll()", workspace)
        self.assertIn("treeList.contentY = Math.max(0, Math.min(workspace.sidebarRestoreContentY", workspace)

    def test_recent_rows_and_search_show_newest_activity_with_an_age_label(self):
        root = Path(__file__).resolve().parents[1]
        search_model = (root / "RoomSearchModel.js").read_text()
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        palette = (root / "RoomSearchPalette.qml").read_text()
        self.assertIn("function activityAt(room)", search_model)
        self.assertIn("function activityLabel(room, now)", search_model)
        self.assertIn("rooms.slice().sort", search_model)
        self.assertIn("function roomActivityAge(room)", panel)
        self.assertIn("controller.roomActivityAge(treeRow.roomData)", workspace)
        self.assertIn("root.activity(row.modelData)", palette)

    def test_sidebar_resize_keeps_its_local_drag_width_until_the_write_is_acknowledged(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        self.assertIn("property bool sidebarWidthSaving: false", panel)
        self.assertIn("if (!sidebarWidthSaving)", panel)
        self.assertIn("sidebarWidthSaving = true", panel)
        self.assertIn("sidebarWidthSaving = false", panel)

    def test_qml_has_full_screen_tiled_extended_mode(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        extended = (root / "ExtendedWorkspace.qml").read_text()

        self.assertIn("property bool extendedMode", panel)
        self.assertIn("function openExtended", panel)
        self.assertIn("ExtendedWorkspace", panel)
        self.assertIn("controller.openExtended()", workspace)
        self.assertIn("controller.activeRooms", extended)
        self.assertIn("GridView", extended)
        self.assertIn('extendedViewMode === "grown"', extended)
        self.assertIn('extendedViewMode === "focus"', extended)
        self.assertIn('hint: "Grow room"', extended)
        self.assertIn('hint: "Focus fullscreen"', extended)
        self.assertIn('hint: "Show all rooms"', extended)
        self.assertIn("font.family: Style.font.family", extended)

    def test_every_chat_view_identifies_its_host_with_the_peer_color(self):
        root = Path(__file__).resolve().parents[1]
        compact = (root / "AresWorkspace.qml").read_text()
        extended = (root / "ExtendedWorkspace.qml").read_text()
        badge = (root / "PeerBadge.qml").read_text()

        self.assertIn("required property var controller", badge)
        self.assertIn("required property string nodeId", badge)
        self.assertIn("controller.peerBadgeForNode", badge)
        self.assertIn("controller.peerColor", badge)
        self.assertIn("PeerBadge {", compact)
        self.assertIn("id: selectedRoomPeerBadge", compact)
        self.assertIn("id: focusPeerBadge", extended)
        self.assertIn("id: tilePeerBadge", extended)

    def test_assistant_messages_use_markdown_without_formatting_tool_output(self):
        root = Path(__file__).resolve().parents[1]
        transcript = (root / "ChatTranscript.qml").read_text()

        # Assistant bodies render as sanitized RichText converted service-side;
        # tool output stays plain monospace — never markdown-formatted.
        self.assertIn("textFormat: Text.RichText", transcript)
        self.assertIn("styledHtml(eventItem.eventHtml)", transcript)
        self.assertIn("font.family: transcript.monospaceFamily", transcript)

    def test_chat_titles_reveal_full_text_and_active_title_marquees(self):
        root = Path(__file__).resolve().parents[1]
        compact = (root / "AresWorkspace.qml").read_text()
        extended = (root / "ExtendedWorkspace.qml").read_text()
        marquee = (root / "MarqueeTitle.qml").read_text()

        self.assertIn("id: treeTitle", compact)
        self.assertIn("treeMouse.containsMouse && treeTitle.truncated", compact)
        self.assertIn("ToolTip.text: treeTitle.text", compact)
        self.assertIn("MarqueeTitle {", compact)
        self.assertIn("MarqueeTitle {", extended)
        self.assertIn("readonly property bool overflow", marquee)
        self.assertIn("ToolTip.visible: titleHover.hovered && root.overflow", marquee)

    def test_local_chat_views_share_terminal_and_remote_fallback(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        compact = (root / "AresWorkspace.qml").read_text()
        extended = (root / "ExtendedWorkspace.qml").read_text()
        transcript = (root / "ChatTranscript.qml").read_text()

        terminal = (root / "TerminalChat.qml").read_text()
        self.assertEqual(compact.count("TerminalChat {"), 2)
        self.assertGreaterEqual(extended.count("TerminalChat {"), 2)
        self.assertIn("Terminal.QMLTermWidget {", terminal)
        self.assertIn("termView.forceActiveFocus()", terminal)
        self.assertIn("ChatTranscript {", terminal)
        self.assertIn("legacyTranscriptNeeded: active && !nativeGroupRoom && allowLegacyTranscript && !terminalAttached", terminal)
        self.assertIn("&& !terminalRoom", terminal)
        self.assertIn("Connection.argumentsFor(termView.attachedAttempt.target)", terminal)
        self.assertIn("targetResolved && connection.started && connection.outputObserved", terminal)
        self.assertIn("!workspace.selectedTerminalInteractive", compact)
        self.assertIn("controller.editMode || !extended.currentTerminalOwnsInput", extended)
        self.assertIn("hideComposer: extended.controller.editMode && !extended.terminalPromptConfirmOpen", extended)
        self.assertIn("response.ok === false || tty.agentError", (root / "TerminalConnectionModel.js").read_text())
        self.assertIn('var action = terminalChat.previewReadOnly ? "/ensure" : "/focus"', terminal)
        self.assertIn('"/v1/tty/rooms/" + encodeURIComponent(requested) + (peerObserve ? "/observe?cols=80&rows=24" : action)', terminal)
        self.assertNotIn("id: eventList", panel)
        self.assertNotIn("active: false", panel)
        self.assertNotIn("id: focusEvent", extended)
        self.assertIn('readonly property bool isActivity: isToolStart || isToolResult', transcript)
        self.assertIn('readonly property bool isThinking: eventKind.indexOf("reasoning") === 0', transcript)
        self.assertIn("function syncDisplay(next)", transcript)
        self.assertIn('eventKind === "tool.start" || eventKind === "tool.generating" || eventKind === "tool.progress"', transcript)
        self.assertIn('eventKind === "tool.complete" || eventKind === "tool.output_risk"', transcript)
        self.assertIn('eventKind.indexOf("reasoning") === 0', transcript)
        self.assertIn("transcript.monospaceFamily", transcript)
        self.assertIn("bridge.approve", transcript)

    def test_view_cycle_preserves_the_selected_chat_in_both_directions(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()

        self.assertIn("function cycleView(direction)", panel)
        self.assertIn("extendedRoomId = room.id", panel)
        self.assertIn('extendedViewMode = "grown"', panel)
        self.assertIn("onTabRequested: function(direction) { root.cycleView(direction) }", panel)
        self.assertIn("Compact → Extended → Tiles", panel)

    def test_hidden_extended_modes_keep_models_without_loading_legacy_previews(self):
        extended = (Path(__file__).resolve().parents[1] / "ExtendedWorkspace.qml").read_text()
        self.assertIn('model: controller.opened ? extended.overviewRoomIds : []', extended)
        self.assertIn('model: controller.opened ? extended.stackRoomIds : []', extended)
        self.assertIn('roomData && extended.visible && extended.controller.extendedMode', extended)
        self.assertIn('tile.roomEvents.length > 0 ? extended.tilePreviewEvents(tile.roomEvents) : tile.cachedPreview', extended)

    def test_terminal_views_skip_legacy_event_model_work(self):
        root = Path(__file__).resolve().parents[1]
        terminal = (root / "TerminalChat.qml").read_text()
        compact = (root / "AresWorkspace.qml").read_text()
        extended = (root / "ExtendedWorkspace.qml").read_text()
        self.assertIn("readonly property bool legacyTranscriptNeeded:", terminal)
        self.assertIn("active: terminalChat.legacyTranscriptNeeded", terminal)
        self.assertEqual(compact.count("eventModel: legacyTranscriptNeeded && visible ? controller.displayEvents : []"), 2)
        self.assertEqual(extended.count("eventModel: []"), 2)
        self.assertIn("&& tile.previewFallback && tile.inViewport", extended)

    def test_terminal_appearance_is_bound_to_persisted_preferences(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        prefs = (root / "PreferencesWorkspace.qml").read_text()
        terminal = (root / "TerminalChat.qml").read_text()
        self.assertIn("property int terminalFontSize: 10", panel)
        self.assertIn("function setTerminalFontSize(size)", panel)
        self.assertIn("function setTerminalBackgroundRole(role)", panel)
        self.assertIn("function setTerminalBackgroundOpacity(opacity)", panel)
        self.assertIn("terminalFontSize: root.terminalFontSize", panel)
        self.assertIn("terminalBackgroundRole: root.terminalBackgroundRole", panel)
        self.assertIn("terminalBackgroundOpacity: root.terminalBackgroundOpacity", panel)
        self.assertIn("font.pointSize: terminalChat.controller.terminalFontSize", terminal)
        self.assertIn("terminalBackground: terminalChat.controller.terminalBackgroundColor", terminal)
        self.assertIn("backgroundOpacity: terminalChat.controller.terminalBackgroundOpacity", terminal)
        self.assertIn("Qt.Key_Plus", terminal)
        self.assertIn("Qt.Key_Minus", terminal)
        self.assertIn("Terminal font size", prefs)
        self.assertIn("Terminal background", prefs)
        self.assertIn("Terminal transparency", prefs)

    def test_extended_tiles_keep_visible_room_terminals_live_without_poll_resets(self):
        extended = (Path(__file__).resolve().parents[1] / "ExtendedWorkspace.qml").read_text()
        self.assertIn("property var overviewRoomIds: []", extended)
        self.assertIn("function syncVisibleRoomIds()", extended)
        self.assertIn("? extended.overviewRoomIds : []", extended)
        self.assertIn("? extended.stackRoomIds : []", extended)
        self.assertIn("roomData: controller.rooms.find(function(room) { return room.id === modelData })", extended)
        self.assertIn("visible: tile.detailed || (tile.liveTerminalCandidate && !tile.previewFallback)", extended)
        self.assertIn("allowLegacyTranscript: tile.detailed", extended)
        self.assertIn("focusOnAttach: tile.detailed", extended)
        self.assertIn("active: extended.controller.opened && extended.controller.extendedMode", extended)
        self.assertIn("extended.controller.extendedViewMode === tile.previewMode", extended)
        self.assertIn("inViewport: y + height > GridView.view.contentY", extended)
        self.assertIn("visible: tile.previewFallback", extended)

    def test_terminal_focus_callback_ignores_destroyed_virtualized_tiles(self):
        terminal = (Path(__file__).resolve().parents[1] / "TerminalChat.qml").read_text()
        self.assertIn("if (!terminalChat) return", terminal)

    def test_small_live_terminals_are_read_only_previews_that_open_the_room(self):
        root = Path(__file__).resolve().parents[1]
        terminal = (root / "TerminalChat.qml").read_text()
        extended = (root / "ExtendedWorkspace.qml").read_text()
        self.assertIn("property bool previewReadOnly: false", terminal)
        self.assertIn("terminalInteractive: terminalAttached && (verifiedWriter || sharedSshTerminal)", terminal)
        self.assertIn("active: terminalChat.targetResolved", terminal)
        self.assertIn("previewHideInput: terminalChat.previewReadOnly || terminalChat.nativeGroupRoom || terminalChat.readOnlyPeer || terminalChat.localHermesRoom || terminalChat.hideComposer || !terminalChat.terminalInteractive", terminal)
        self.assertNotIn("previewHiddenHeight", terminal)
        self.assertIn("localSelection: true", terminal)
        self.assertIn("readOnlyInput: terminalChat.previewReadOnly || terminalChat.nativeGroupRoom || terminalChat.readOnlyPeer || terminalChat.localHermesRoom || terminalChat.hideComposer || !terminalChat.terminalInteractive", terminal)
        self.assertIn("previewReadOnly: !tile.detailed", extended)
        self.assertIn("onPreviewActivated: tile.growRequested(tile.roomData)", extended)
        self.assertIn("onClicked: terminalChat.previewActivated()", terminal)
        self.assertIn("enabled: terminalChat.terminalInteractive", terminal)
        self.assertIn("acceptedButtons: Qt.LeftButton", terminal)
        self.assertNotIn("onTapped: terminalChat.previewActivated()", terminal)
        self.assertIn("passivePreviewWheel: terminalChat.previewReadOnly", terminal)
        self.assertIn("wheel.accepted = false", terminal)

    def test_compact_switch_keeps_two_terminal_attachments_warm(self):
        workspace = (Path(__file__).resolve().parents[1] / "AresWorkspace.qml").read_text()
        terminal = (Path(__file__).resolve().parents[1] / "TerminalChat.qml").read_text()
        self.assertIn('property string cachedRoomA: ""', workspace)
        self.assertIn('property string cachedRoomB: ""', workspace)
        self.assertIn('function cacheSelectedRoom()', workspace)
        self.assertIn('roomId: workspace.cachedRoomA', workspace)
        self.assertIn('roomId: workspace.cachedRoomB', workspace)
        self.assertIn('active: workspace.terminalCacheActive && roomId !== ""', workspace)
        self.assertNotIn('roomId: controller.selectedRoomId\n            eventModel:', workspace)
        self.assertIn('interval: 50', terminal)
        self.assertIn('!termView.startRequested && termView.width >= 160 && termView.height >= 100', terminal)

    def test_terminal_footer_explains_how_to_copy(self):
        workspace = (Path(__file__).resolve().parents[1] / "AresWorkspace.qml").read_text()
        self.assertIn("Drag select  ·  SUPER+C copy  ·  SUPER+V paste", workspace)

    def test_dashboard_tiles_leave_enough_room_for_live_terminal_output(self):
        extended = (Path(__file__).resolve().parents[1] / "ExtendedWorkspace.qml").read_text()
        # The old stack offered 248x98 px to the terminal; startup requires 160x100.
        self.assertIn("Math.floor(width / Style.space(680))", extended)
        self.assertIn("cellHeight: Math.max(Style.space(360)", extended)
        self.assertIn("readonly property int columns: 1", extended)
        self.assertIn("cellHeight: Math.max(Style.space(300), height / 3)", extended)
        self.assertIn("width: parent.width * 0.62 - Style.space(5)", extended)

    def test_historical_tile_fallback_is_only_for_nonterminal_rooms(self):
        extended = (Path(__file__).resolve().parents[1] / "ExtendedWorkspace.qml").read_text()
        self.assertIn('readonly property bool previewFallback: !detailed && !liveTerminalCandidate', extended)
        self.assertIn("visible: tile.previewFallback", extended)
        self.assertIn("tile.roomEvents.length > 0 ? extended.tilePreviewEvents(tile.roomEvents) : tile.cachedPreview", extended)
        self.assertNotIn("model: tile.cachedPreview", extended)
        self.assertIn("onClicked: tile.growRequested(tile.roomData)", extended)

    def test_compact_dashboard_titles_do_not_animate_over_live_terminals(self):
        extended = (Path(__file__).resolve().parents[1] / "ExtendedWorkspace.qml").read_text()
        self.assertIn("visible: tile.detailed\n            Layout.preferredHeight: visible ? implicitHeight : 0", extended)
        self.assertIn("visible: !tile.detailed\n            Layout.preferredHeight: visible ? implicitHeight : 0", extended)
        self.assertIn("ToolTip.visible: compactTitleHover.hovered && truncated", extended)

    def test_read_only_previews_are_an_opt_out_preference(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        prefs = (root / "PreferencesWorkspace.qml").read_text()
        self.assertIn("property bool terminalPreviewReadOnly: true", panel)
        self.assertIn("terminalPreviewReadOnly = preferences.terminalPreviewReadOnly !== false", panel)
        self.assertIn("function setTerminalPreviewReadOnly(readOnly)", panel)
        self.assertIn("Read-only terminal previews", prefs)
        self.assertIn("onToggled: controller.setTerminalPreviewReadOnly(checked)", prefs)


    def test_streaming_transcripts_follow_tail_until_the_user_scrolls(self):
        root = Path(__file__).resolve().parents[1]
        bridge = (root / "AresBridge.qml").read_text()
        compact = (root / "AresWorkspace.qml").read_text()
        extended = (root / "ExtendedWorkspace.qml").read_text()
        transcript = (root / "ChatTranscript.qml").read_text()

        self.assertIn("function loadOlder()", bridge)
        self.assertIn('"&before=" + before + "&limit=40"', bridge)
        self.assertIn("historyHasOlder", bridge)
        self.assertIn("historyPageLoaded", bridge)
        self.assertIn("readonly property int historyWindowLimit: 240", bridge)
        self.assertIn("olderEvents.slice(0, historyWindowLimit)", bridge)

        self.assertIn("property bool autoFollow: true", transcript)
        self.assertIn("autoFollow = false", transcript)
        self.assertIn("if (autoFollow) Qt.callLater", transcript)
        self.assertIn("bridge.loadOlder()", transcript)
        self.assertNotIn("onCountChanged: {\n    autoFollow = true", transcript)
        self.assertIn("TerminalChat {", compact)
        self.assertIn("TerminalChat {", extended)

    def test_navigation_preserves_single_room_projects_and_prioritizes_attention(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        face = (root / "BotFace.qml").read_text()

        self.assertIn("function roomActivity(room)", panel)
        self.assertIn("function roomNeedsReply(roomId)", panel)
        self.assertIn("return roomActivity(b) - roomActivity(a)", panel)
        self.assertNotIn("if (childRooms.length === 1)", panel)
        self.assertNotIn('kind: "room", project: project, room: childRooms[0], direct: true', panel)
        self.assertIn('kind: "project", project: project, roomCount: childRooms.length, expanded: expanded, depth: 1', panel)
        self.assertIn("readonly property bool directRoom", workspace)
        self.assertIn("BotFace {", workspace)
        self.assertIn("waiting: treeRow.roomData ? controller.roomNeedsReply(treeRow.roomData.id) : false", workspace)
        self.assertIn("property bool waiting: false", face)
        self.assertIn("readonly property bool sleeping: !face.working && !face.waiting", face)

    def test_polling_preserves_models_and_user_scroll_when_data_is_unchanged(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        bridge = (root / "AresBridge.qml").read_text()

        self.assertIn("function sameData(current, incoming)", panel)
        self.assertIn("if (!sameData(rooms, nextRooms)) rooms = nextRooms", panel)
        self.assertIn("if (!sameData(events, nextEvents)) events = nextEvents", panel)
        self.assertIn("var changed = false", bridge)
        self.assertIn("if (!changed) return", bridge)
        transcript = (root / "ChatTranscript.qml").read_text()
        # Incremental ListModel diff: no wholesale delegate rebuilds, which is
        # what kept messages flashing and the scroll jumping during streaming.
        self.assertIn("model: displayModel", transcript)
        self.assertIn("function syncDisplay(next)", transcript)
        self.assertIn("displayModel.insert(index, row)", transcript)
        self.assertIn("pendingEvents = eventModel", transcript)
        self.assertIn("baseEvents = incoming", bridge)
        self.assertIn("sameData(prior, item) ? prior : item", bridge)

    def test_imported_remote_history_is_not_treated_as_new_attention(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()

        self.assertIn('room.source === "hermes"', panel)
        self.assertNotIn("events[index].roomId !== room.id", panel)
        self.assertIn("property var seenRoomCursors", panel)
        self.assertIn("function markRoomSeen(roomId)", panel)
        self.assertIn("latestAssistantCursor(roomId) > Number(seenRoomCursors[roomId] || 0)", panel)
        self.assertNotIn('return latestRoomMessageKind(roomId) === "message.assistant"', panel)

    def test_navigation_starts_with_globally_fresh_rooms_across_hosts(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()

        self.assertIn("readonly property int recentRoomLimit: 6", panel)
        self.assertIn("function sortedRooms()", panel)
        self.assertIn('rows.push({ kind: "section", label: "RECENT", expanded: recentExpanded', panel)
        self.assertIn('kind: "room", project: recentProject, room: recentRoom, direct: true, recent: true', panel)
        self.assertLess(panel.index('label: "RECENT"'), panel.index('label: "PROJECTS"'))
        self.assertIn("readonly property bool sectionItem", workspace)
        self.assertIn("treeRow.modelData.label", workspace)
        self.assertIn('label: "CHATS  " + (roomSearch.expanded ? roomSearch.results.length : controller.rooms.length)', workspace)
        self.assertIn("treeRow.modelData.recent ? treeRow.roomData.name", workspace)
        self.assertIn('treeRow.modelData.recent ? "!"', workspace)

    def test_sidebar_search_uses_all_rooms_without_opening_them_on_input(self):
        root = Path(__file__).resolve().parents[1]
        workspace = (root / "AresWorkspace.qml").read_text()
        search = (root / "RoomSearch.qml").read_text()
        self.assertIn("model: roomSearch.expanded ? roomSearch.results : controller.navigationRows", workspace)
        self.assertIn("rooms: controller.sortedRooms()", workspace)
        self.assertIn("onRoomActivated: function(room) { workspace.activateSearchRoom(room) }", workspace)
        self.assertIn("treeList.positionViewAtIndex(roomSearch.currentIndex, ListView.Contain)", workspace)
        self.assertEqual(workspace.count("focusOnAttach: visible && !roomSearch.expanded"), 2)
        self.assertIn("onTextChanged: root.refresh(true)", search)
        self.assertNotIn("bridge.", search)
        self.assertNotIn("controller.", search)

    def test_recent_expands_and_projects_can_collapse_independently(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        self.assertIn("property bool recentExpanded: false", panel)
        self.assertIn("property bool projectsCollapsed: false", panel)
        self.assertIn("recentExpanded ? allRecentRooms.length : recentRoomLimit", panel)
        self.assertIn("if (projectsCollapsed) return rows", panel)
        self.assertIn("function toggleNavigationSection(label)", panel)
        self.assertIn("controller.toggleNavigationSection(treeRow.modelData.label)", workspace)
        self.assertNotIn("enabled: !treeRow.sectionItem", workspace)

    def test_rooms_needing_attention_have_a_subtle_pulsing_highlight(self):
        workspace = (Path(__file__).resolve().parents[1] / "AresWorkspace.qml").read_text()
        self.assertIn("readonly property bool needsAttention: roomItem && roomData", workspace)
        self.assertIn("id: attentionGlow", workspace)
        self.assertIn("visible: treeRow.needsAttention", workspace)
        self.assertIn("SequentialAnimation on opacity", workspace)

    def test_project_navigation_groups_projects_under_expandable_hosts(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()

        self.assertIn("property var collapsedHosts: ({})", panel)
        self.assertIn("function projectsForNode(nodeId)", panel)
        self.assertIn('kind: "host", nodeId: nodeId, projectCount: hostProjects.length', panel)
        self.assertIn("function toggleHost(nodeId)", panel)
        self.assertIn("function selectRecentRoom(room)", panel)
        self.assertIn("readonly property bool hostItem", workspace)
        self.assertIn("controller.toggleHost(treeRow.modelData.nodeId)", workspace)
        self.assertIn("controller.selectSidebarRoom(treeRow.roomData", workspace)
        self.assertIn("readonly property int depth: Number(modelData.depth || 0)", workspace)

    def test_archived_button_is_visible_and_archived_rooms_are_grouped_by_project(self):
        root = Path(__file__).resolve().parents[1]
        workspace = (root / "AresWorkspace.qml").read_text()
        archived = (root / "ArchivedWorkspace.qml").read_text()
        self.assertIn('id: archivedSidebarButton', workspace)
        self.assertIn('label: "Archived (" + controller.archivedRooms.length + ")"', workspace)
        self.assertIn('model: archived.groups', archived)
        self.assertIn('model: groupItem.expanded ? groupItem.modelData.rooms : []', archived)
        self.assertIn('controller.restoreRoom(modelData)', archived)

    def test_workspace_exposes_preferences_peers_and_archived_rooms(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        preferences = (root / "PreferencesWorkspace.qml").read_text()
        archived = (root / "ArchivedWorkspace.qml").read_text()

        self.assertIn('property string section: "home"', panel)
        self.assertIn("quotaProviders", panel)
        self.assertIn("peerBadgeForNode", panel)
        self.assertIn('controller.section = "preferences"', workspace)
        self.assertIn('controller.section = "archived"', workspace)
        self.assertIn("updatePeerPreferences", preferences)
        self.assertIn("controller.peerColor", preferences)
        self.assertIn("controller.archivedRooms", archived)
        self.assertIn('controller.restoreRoom(modelData)', archived)

    def test_home_quota_is_horizontal_configurable_and_collapses_to_one_marquee(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        preferences = (root / "PreferencesWorkspace.qml").read_text()

        self.assertIn("property bool homeQuotaVisible", panel)
        self.assertIn("property bool homeQuotaCollapsed", panel)
        self.assertIn("property var hiddenHomeQuotaProviders", panel)
        self.assertIn("readonly property var visibleQuotaProviders", panel)
        self.assertIn("function updatePreferences", panel)
        self.assertIn("function setHomeQuotaProviderVisible", panel)
        self.assertIn('"/v1/preferences"', panel)
        self.assertIn("id: providerQuotaStrip", workspace)
        self.assertIn("Layout.preferredHeight: Style.space(30)", workspace)
        self.assertIn("id: quotaMarquee", workspace)
        self.assertIn("loops: Animation.Infinite", workspace)
        self.assertIn("controller.toggleHomeQuotaCollapsed()", workspace)
        self.assertIn('text: "PROVIDER USAGE"', workspace)
        self.assertIn("visible: !controller.homeQuotaCollapsed", workspace)
        self.assertIn("? Style.space(30)", workspace)
        self.assertIn('hint: "Expand provider usage"', workspace)
        self.assertIn('text: "Show provider usage on Home"', preferences)
        self.assertIn("checked: controller.homeQuotaVisible", preferences)
        self.assertIn("controller.setHomeQuotaVisible", preferences)
        self.assertIn("model: controller.quotaProviders", preferences)
        self.assertIn("controller.homeQuotaProviderVisible", preferences)
        self.assertIn("controller.setHomeQuotaProviderVisible", preferences)
        self.assertIn("function weeklyWindow", workspace)
        self.assertIn("function fiveHourWindow", workspace)
        self.assertIn("function expectedAvailablePercent", workspace)
        self.assertIn("id: weeklyQuotaTrack", workspace)
        self.assertIn("id: weeklyPaceMarker", workspace)
        self.assertIn("weeklyAvailable >= weeklyExpected", workspace)
        self.assertIn('text: "WEEK "', workspace)
        self.assertIn('? "5H " +', workspace)
        self.assertIn("function providerPaceColor", workspace)
        self.assertIn("model: controller.visibleQuotaProviders", workspace)
        self.assertIn("color: workspace.providerPaceColor(modelData)", workspace)
        self.assertIn('"5H N/A"', workspace)

    def test_compact_panel_scales_and_sidebar_collapses_with_persisted_preferences(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        service = (root / "bin" / "ares_service.py").read_text()
        self.assertIn("readonly property real panelSizeFraction", panel)
        self.assertIn("availableCardHeight * root.panelSizeFraction", panel)
        self.assertIn('updatePreferences({ panelSize: panelSize === "large" ? "medium" : "large" })', panel)
        self.assertIn("Layout.preferredWidth: controller.sidebarCollapsed ? 0 : Style.space(controller.sidebarWidth)", workspace)
        self.assertIn("Layout.maximumWidth: controller.sidebarCollapsed ? 0 : Style.space(480)", workspace)
        self.assertIn("function setSidebarWidth(width, fromDrag)", panel)
        self.assertIn("controller.setSidebarWidth(Math.round(newRealWidth / scale), true)", workspace)
        self.assertIn("id: sidebarGrip", workspace)
        self.assertIn("property int sidebarWidth: 200", panel)
        self.assertIn('hint: "Collapse sidebar"', workspace)
        self.assertIn('hint: "Expand sidebar"', workspace)
        self.assertIn('"panelSize", "sidebarCollapsed", "sidebarWidth"', service)

    def test_composer_drafts_multiline_shift_enter_and_extended_editor(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        workspace = (root / "AresWorkspace.qml").read_text()
        service = (root / "bin" / "ares_service.py").read_text()
        # Service-backed drafts survive panel restarts/crashes.
        self.assertIn('parsed.path == "/v1/drafts"', service)
        self.assertIn("store.save_draft(device, unquote(draft_match.group(1))", service)
        self.assertIn("store.device_drafts(", service)
        self.assertIn("def save_draft(self, device_id", service)
        self.assertIn("def device_drafts(self, device_id", service)
        self.assertIn('"sidebarWidth": 200', service)
        # Composer restores drafts per room and auto-saves edits.
        self.assertIn("property var roomDrafts", panel)
        self.assertIn("function draftForRoom(roomId)", panel)
        self.assertIn("function scheduleDraftSave()", panel)
        self.assertIn("roomText = draftForRoom(selectedRoomId)", panel)
        self.assertIn("onRoomTextChanged: if (!draftRestoring) scheduleDraftSave()", panel)
        self.assertIn("clearDraft(targetRoomId)", panel)
        self.assertIn('loadDrafts()', panel)
        self.assertIn('"/v1/drafts/" + encodeURIComponent(id)', panel)
        self.assertIn('var pending = Object.assign({}, draftPendingByRoom)', panel)
        self.assertIn('pendingDraftRoomId = Object.keys(draftPendingByRoom)[0] || ""', panel)
        # Multi-line editor: Shift+Enter newline, auto-grow, extend modal.
        self.assertIn("TextArea {", workspace)
        self.assertIn("wrapMode: TextEdit.Wrap", workspace)
        self.assertIn("if (event.modifiers & Qt.ShiftModifier) event.accepted = false", workspace)
        self.assertIn("Layout.preferredHeight: Math.min(Style.space(112), Math.max(Style.space(26), composerInput.contentHeight + Style.space(10)))", workspace)
        self.assertIn("property bool extendedEditorOpen: false", workspace)
        self.assertIn("id: extendedArea", workspace)
        self.assertIn('hint: "Extend editor — write with room"', workspace)
        # Model chip is model-name only and opens a modal picker.
        self.assertIn("if (model !== \"\") return model", workspace)
        self.assertIn("workspace.botPickerOpen = !workspace.botPickerOpen", workspace)
        self.assertIn("botModelList", workspace)
        self.assertIn("workspace.botPickerOpen = false", workspace)

    def test_terminal_edit_mode_reuses_native_composer_below_pty(self):
        root = Path(__file__).resolve().parents[1]
        workspace = (root / "AresWorkspace.qml").read_text()
        self.assertIn("property bool editMode: false", (root / "Panel.qml").read_text())
        self.assertIn("readonly property bool editMode: controller.editMode", workspace)
        self.assertIn("!workspace.selectedTerminalOwnsInput || workspace.editMode", workspace)
        self.assertIn('label: workspace.editMode ? "Terminal" : "Editar"', workspace)
        self.assertIn('hint: "Extend editor — write with room"', workspace)
        self.assertIn("workspace.extendedEditorOpen = false", workspace)

    def test_edit_mode_hides_terminal_composer_without_detaching_the_pty(self):
        root = Path(__file__).resolve().parents[1]
        workspace = (root / "AresWorkspace.qml").read_text()
        terminal = (root / "TerminalChat.qml").read_text()
        self.assertIn("hideComposer: visible && workspace.editMode && !workspace.terminalPromptConfirmOpen", workspace)
        self.assertIn("previewHideInput: terminalChat.previewReadOnly || terminalChat.nativeGroupRoom || terminalChat.readOnlyPeer || terminalChat.localHermesRoom || terminalChat.hideComposer || !terminalChat.terminalInteractive", terminal)
        self.assertIn("readOnlyInput: terminalChat.previewReadOnly || terminalChat.nativeGroupRoom || terminalChat.readOnlyPeer || terminalChat.localHermesRoom || terminalChat.hideComposer || !terminalChat.terminalInteractive", terminal)
        self.assertIn("enabled: terminalChat.terminalInteractive", terminal)
        self.assertIn("function sendTerminal(room, text, artifactIds, callback, idempotencyKey, confirmedEmptyComposer)",
                      (root / "AresBridge.qml").read_text())
        self.assertIn("workspace.terminalPromptConfirmOpen = true", workspace)
        self.assertIn("controller.appendLocalEvent(true, true)", workspace)

    def test_extended_draft_modal_can_attach_and_preview_images(self):
        workspace = (Path(__file__).resolve().parents[1] / "AresWorkspace.qml").read_text()
        modal = workspace[workspace.index("// Extended composer:"):]
        self.assertIn("controller.chooseImage()", modal)
        self.assertIn("controller.pendingImages.length", modal)
        self.assertIn("controller.removePendingImage(", modal)

    def test_delayed_clipboard_image_keeps_the_requesting_room(self):
        root = Path(__file__).resolve().parents[1]
        workspace = (root / "AresWorkspace.qml").read_text()
        panel = (root / "Panel.qml").read_text()
        self.assertIn("property string pasteRoomId", workspace)
        self.assertIn("controller.addPendingImage(line.substring(\"ARES_PASTE:\".length), workspace.pasteRoomId)", workspace)
        self.assertIn("property string imageDialogRoomId", panel)
        self.assertIn("root.addPendingImage(path, root.imageDialogRoomId)", panel)

    def test_action_buttons_are_icon_only_with_tooltips_and_chat_switches_send_bot(self):
        root = Path(__file__).resolve().parents[1]
        workspace = (root / "AresWorkspace.qml").read_text()
        panel = (root / "Panel.qml").read_text()
        bridge = (root / "AresBridge.qml").read_text()
        self.assertIn("property string hint: \"\"", workspace)
        self.assertIn("quiet.label === \"\" ? Style.space(28) : quietText.implicitWidth + Style.space(16)", workspace)
        self.assertIn("visible: quietMouse.containsMouse && quiet.hint !== \"\" && quiet.label === \"\"", workspace)
        self.assertIn('hint: "Archive room"', workspace)
        self.assertIn('hint: controller.sendingImage ? "Sending…" : "Send (Enter)"', workspace)
        self.assertNotIn('label: "ARCHIVE"', workspace)
        self.assertNotIn('label: "SEND', workspace)
        self.assertIn("function sendBotForRoom(room)", panel)
        self.assertIn("roomSendBotId = modelData.id", workspace)
        self.assertIn("function send(room, text, artifactIds, callback, botId, idempotencyKey)", bridge)

    def test_preferences_expose_bot_configurator_and_keybind_suggestion(self):
        root = Path(__file__).resolve().parents[1]
        panel = (root / "Panel.qml").read_text()
        prefs = (root / "PreferencesWorkspace.qml").read_text()

        self.assertIn("function updateBotPreferences(bot, name, description, faceShape, colorRole, modelChoice)", panel)
        self.assertIn("function loadModels()", panel)
        self.assertIn('bridge.request("GET", "/v1/models"', panel)
        self.assertIn("function botColorForRole(role)", panel)
        self.assertIn('text: "BOTS"', prefs)
        self.assertIn('placeholderText: "Expertise — what is this bot good at?"', prefs)
        self.assertIn('model: ["oval", "square", "triangle", "diamond"]', prefs)
        self.assertIn("ComboBox {", prefs)
        self.assertIn("draftModelKey", prefs)
        self.assertIn("controller.updateBotPreferences(botCard.modelData", prefs)
        self.assertIn("SUPER + SHIFT + A", prefs)
        self.assertIn("bindings.lua", prefs)

    def test_composer_has_dictation_button_and_extended_editor_grows(self):
        root = Path(__file__).resolve().parents[1]
        workspace = (root / "AresWorkspace.qml").read_text()

        self.assertIn('command: ["omarchy-voxtype-status"]', workspace)
        self.assertIn('command: ["voxtype", "record", "toggle"]', workspace)
        self.assertIn('glyph: "󰍬"', workspace)
        self.assertIn("extendedColumn.implicitHeight", workspace)
        self.assertIn("extendedArea.contentHeight", workspace)


if __name__ == "__main__":
    unittest.main()
