import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
const terminal = readFileSync('TerminalChat.qml', 'utf8');
const extended = readFileSync('ExtendedWorkspace.qml', 'utf8');
const history = readFileSync('RemoteRoomHistory.qml', 'utf8');
const tiles = readFileSync('ControlTiles.qml', 'utf8');
const source = (name) => readFileSync(name, 'utf8');
test('selection shares one terminal-first view in compact, extended and tile', () => {
  assert.match(source('Panel.qml'), /function activateSearchRoom\(candidate\)[\s\S]*?selectRecentRoom\(room\)/);
  assert.match(tiles, /roomActivated\(chatEntry\.modelData\.id\)/);
  assert.match(source('AresWorkspace.qml'), /TerminalChat \{/);
  assert.match(extended, /TerminalChat \{/);
  assert.match(extended, /readonly property bool previewFallback: !detailed && !liveTerminalCandidate/);
  assert.match(terminal, /readonly property bool legacyTranscriptNeeded: active && !nativeGroupRoom && allowLegacyTranscript && !terminalAttached[\s\S]*?!terminalRoom/);
  assert.doesNotMatch(terminal, /ChatTranscript[\s\S]*?active: terminalChat\.active && terminalChat\.terminalRoom/);
});
test('plain Herdr attach output never grants local Ares-room input; exact SSH sharing is separate', () => {
  const client = source('bin/ares-herdr-client');
  assert.match(client, /exec "\$4" terminal attach "\$2"/);
  assert.match(terminal, /readonly property bool verifiedWriter: false/);
  assert.match(terminal, /readonly property bool sharedSshTerminal: remotePeerRoom && connection\.target[\s\S]*?connection\.target\.transport === "ssh" && connection\.target\.sessionName === "terminal"/);
  assert.match(terminal, /terminalInteractive: terminalAttached && \(verifiedWriter \|\| sharedSshTerminal\)/);
  assert.match(terminal, /readOnlyInput: [^\n]*!terminalChat\.terminalInteractive/);
  assert.match(terminal, /previewHideInput: [^\n]*!terminalChat\.terminalInteractive/);
  assert.match(terminal, /enabled: terminalChat\.terminalInteractive/);
});
test('history is optional and modal, never the selected room surface', () => {
  assert.match(terminal, /Popup \{\s*id: historyPanel[\s\S]*?modal: true/);
  assert.match(source('AresWorkspace.qml'), /selectedTerminal\.historyOpen = true/);
  assert.match(history, /function loadHistory\(\)/);
  assert.match(history, /messages\.length <= 8/);
  assert.doesNotMatch(terminal, /historyOpen = true[\s\S]*?focusRoom\(/);
});
test('unverified peer has no SSH writer or fallback composer', () => {
  assert.match(terminal, /readonly property bool readOnlyPeer: !!connection\.target && connection\.target\.transport === "peer"/);
  assert.match(terminal, /sharedSshTerminal:[\s\S]*?transport === "ssh"/);
  assert.match(terminal, /readonly property bool terminalOwnsInput: targetResolved/);
  assert.match(terminal, /readOnlyInput: [^\n]*readOnlyPeer/);
  // The "no verified writer (source fence not installed)" banner was always-on noise
  // (verifiedWriter is hardcoded false); the user wants it gone.
  assert.doesNotMatch(terminal, /no verified writer \(source fence not installed\)/);
  assert.match(extended, /readonly property bool currentTerminalOwnsInput: controller.extendedViewMode === "grown"[\s\S]*?masterTile.terminalOwnsInput/);
});
test('Retry and history are compact Nerd Font header actions around Edit', () => {
  const workspace = source('AresWorkspace.qml');
  assert.match(workspace, /id: compactTranscriptA/);
  assert.match(workspace, /glyph: "󰑐"[\s\S]*?hint: "Retry terminal attachment"[\s\S]*?selectedTerminal\.focusRoom\(true\)/);
  assert.match(workspace, /glyph: "󰏫"[\s\S]*?hint: workspace\.editMode/);
  assert.match(workspace, /glyph: "󰋚"[\s\S]*?hint: "Open bounded history"[\s\S]*?selectedTerminal\.historyOpen = true/);
  assert.ok(workspace.indexOf('glyph: "󰑐"') < workspace.indexOf('glyph: "󰏫"'));
  assert.ok(workspace.indexOf('glyph: "󰏫"') < workspace.indexOf('glyph: "󰋚"'));
  assert.doesNotMatch(terminal, /text: "Retry"/);
  assert.doesNotMatch(terminal, /text: "History"/);
});
