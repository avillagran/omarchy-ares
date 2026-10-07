import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const qml = readFileSync(new URL('../TerminalChat.qml', import.meta.url), 'utf8');
const model = vm.createContext({});
vm.runInContext(readFileSync(new URL('../TerminalConnectionModel.js', import.meta.url), 'utf8'), model);
function harness() {
  const calls = [];
  const ctx = vm.createContext({ Connection: model, active: true, visible: true, terminalRoom: true,
    nativeGroupRoom: false, remotePeerRoom: false, selectedGroupMemberKey: '', roomId: 'room/a',
    connection: model.initial(), previewReadOnly: false, chooserOpen: false, chooserAttempt: 0,
    chooserRoomId: '', chooserBusy: false, chooserRows: [], chooserSelection: '', chooserError: '',
    explicitBindingRoomId: '', detailResolutionRequested: false, targetResolved: false,
    startupTimeout: { restart() {}, stop() {} },
    bridge: { available: true, request(...args) { calls.push(args); } } });
  ctx.terminalChat = ctx;
  for (const name of ['focusRoom', 'promoteVisibleDetail', 'resetConnection', 'showChooser', 'refreshChooser', 'cancelChooser', 'confirmTerminal', 'captureFrame']) {
    const match = qml.match(new RegExp('  function ' + name + '\\([^]*?\\n  }'));
    if (match) vm.runInContext(match[0], ctx);
  }
  return { ctx, calls };
}

test('focus requests capture generation and room; previews ensure without focus', () => {
  const { ctx, calls } = harness();
  ctx.focusRoom();
  assert.equal(calls[0][1], '/v1/tty/rooms/room%2Fa/focus');
  assert.equal(ctx.connection.phase, 'resolving');
  ctx.roomId = 'room-b'; ctx.focusRoom(true);
  calls[0][3]({ ok: false, error: 'old failure' });
  assert.equal(ctx.connection.roomId, 'room-b');
  assert.equal(ctx.connection.error, '');
  ctx.previewReadOnly = true; ctx.focusRoom(true);
  assert.equal(calls.at(-1)[1], '/v1/tty/rooms/room-b/ensure');
});

test('all room navigation only attaches existing terminals with a bounded output deadline', () => {
  const { ctx, calls } = harness();
  for (const [visible, previewReadOnly] of [[true, false], [true, true], [false, false]]) {
    ctx.visible = visible;
    ctx.previewReadOnly = previewReadOnly;
    ctx.focusRoom(true);
    assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[2])), {openIfMissing: false});
    assert.equal(ctx.startupTimeout.interval, 12000);
  }
});

test('promoting a warm slot or preview rechecks existing ownership once and ignores stale reply', () => {
  for (const preview of [false, true]) {
    const {ctx,calls} = harness();
    ctx.visible = preview;
    ctx.previewReadOnly = preview;
    ctx.focusRoom();
    assert.equal(calls[0][2].openIfMissing,false);
    assert.equal(typeof ctx.promoteVisibleDetail,'function');
    ctx.visible = true; ctx.previewReadOnly = false;
    ctx.promoteVisibleDetail();
    assert.equal(calls.length,2);
    assert.equal(calls[1][2].openIfMissing,false);
    calls[0][3]({ok:false,error:'no existing owner'});
    assert.equal(ctx.connection.phase,'resolving');
    calls[1][3]({ok:false,error:'no existing owner'});
    ctx.visible = false; ctx.promoteVisibleDetail();
    ctx.visible = true; ctx.promoteVisibleDetail();
    assert.equal(calls.length,2,'visibility must not retry an uncertain launch');
  }
});

test('chooser only lists until explicit confirmation posts the opaque candidate id', () => {
  const { ctx, calls } = harness();
  assert.equal(typeof ctx.showChooser, 'function');
  ctx.showChooser();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].slice(0, 2), ['GET', '/v1/tty/rooms/room%2Fa/terminals']);
  calls[0][3]({ ok: true, terminals: [{ id: 'digest', terminalId: 'term_raw', workspace: 'w', label: 'tab', paneId: 'p', agentKind: 'claude' }], errors: [] });
  ctx.chooserSelection = 'digest';
  ctx.confirmTerminal(false);
  assert.equal(calls.length, 1);
  ctx.confirmTerminal(true);
  assert.equal(calls.length, 2);
  assert.equal(calls[1][1], '/v1/tty/rooms/room%2Fa/binding');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1][2])), { terminalId: 'digest', confirmed: true });
  ctx.confirmTerminal(true);
  assert.equal(calls.length, 2, 'no duplicate confirmation while pending');
  ctx.roomId = 'other';
  calls[1][3]({ ok: true });
  assert.equal(calls.length, 2, 'old confirmation cannot attach a new room');
});

test('refresh and cancel invalidate stale inventory; failed list still permits refresh', () => {
  const { ctx, calls } = harness();
  assert.equal(typeof ctx.showChooser, 'function');
  ctx.showChooser(); ctx.refreshChooser();
  calls[0][3]({ ok: true, terminals: [{ id: 'stale' }] });
  assert.equal(ctx.chooserRows.length, 0);
  calls[1][3]({ ok: true, terminals: [], errors: ['host offline'] });
  assert.match(ctx.chooserError, /host offline/);
  ctx.refreshChooser(); ctx.cancelChooser();
  calls[2][3]({ ok: true, terminals: [{ id: 'late' }] });
  assert.equal(ctx.chooserOpen, false);
  assert.equal(ctx.chooserRows.length, 0);
  assert.equal(calls.every(c => c[0] === 'GET'), true);
});

test('workspace send routes fail closed while terminal owns input but is not interactive', () => {
  for (const [file, id, own, interactive] of [
    ['AresWorkspace.qml', 'workspace', 'selectedTerminalOwnsInput', 'selectedTerminalInteractive'],
    ['ExtendedWorkspace.qml', 'extended', 'currentTerminalOwnsInput', 'currentTerminalInteractive']]) {
    const source = readFileSync(new URL('../' + file, import.meta.url), 'utf8');
    const calls = [];
    const ctx = vm.createContext({ currentRoom: { id: 'r' }, [own]: true, [interactive]: false,
      controller: { selectedRoomId: 'r', pendingImages: [], roomText: 'unsent', appendLocalEvent(...args) { calls.push(args); } } });
    ctx[id] = ctx;
    vm.runInContext(source.match(/  function submitEditPrompt\([^]*?\n  }/)[0], ctx);
    ctx.submitEditPrompt();
    assert.equal(calls.length, 0, file + ' must not use Desktop while terminal is unresolved');
    assert.match(ctx.controller.errorText, /terminal/i);
    ctx[own] = false;
    ctx.submitEditPrompt();
    assert.equal(calls.length, 1, 'unbound rooms retain legacy send');
  }
});

test('late same-room image capture cannot publish across a retry', () => {
  const { ctx } = harness();
  const cached = [];
  let callback;
  ctx.terminalAttached = true;
  ctx.controller = { cacheTerminalFrame(...args) { cached.push(args); } };
  ctx.connection = model.begin(ctx.connection, ctx.roomId);
  const view = { width: 700, height: 400, grabToImage(fn) { callback = fn; } };
  ctx.captureFrame(view);
  ctx.connection = model.begin(ctx.connection, ctx.roomId);
  callback({ url: 'image://old' });
  assert.equal(cached.length, 0);
});

test('loader readiness, output signal, timeout, and status visibility are wired independently', () => {
  assert.match(qml, /startupTimeout.attempt = connection/);
  assert.match(qml, /onTriggered: terminalChat.sessionEvent\(attempt, "timeout"\)/);
  assert.match(qml, /onVisibleChanged: \{\s*if \(!visible\) \{ cancelChooser\(\); historyOpen = false \}\s*else promoteVisibleDetail\(\)/);
  assert.match(qml, /onPreviewReadOnlyChanged: promoteVisibleDetail\(\)/);
  assert.match(qml, /active: terminalChat\.targetResolved/);
  assert.match(qml, /ignoreUnknownSignals: true/);
  assert.match(qml, /function onOutputReceived\(/);
  assert.match(qml, /interval: 12000/);
  assert.match(qml, /property var attachedAttempt: null/);
  assert.doesNotMatch(qml, /readonly property string attachedRoomId: terminalChat.roomId/);
  assert.match(qml, /opacity: terminalChat.surfacePainted \? 1 : 0/);
  assert.match(qml, /text: "Choose terminal"/);
  assert.match(qml, /text: "Confirm selection"/);
  assert.match(qml, /modelData\.workspace/);
  assert.match(qml, /modelData\.paneId/);
  assert.match(qml, /modelData\.agentKind/);
  for (const file of ['AresWorkspace.qml', 'ExtendedWorkspace.qml']) {
    const caller = readFileSync(new URL('../' + file, import.meta.url), 'utf8');
    assert.doesNotMatch(caller, /opacity: (?:workspace.showSelectedFrame && !surfacePainted|surfacePainted) \?/);
    assert.match(caller, /terminalOwnsInput/);
  }
});

test('remote preview observes by GET; visible remote detail requests guarded exact opening', () => {
  const {ctx,calls}=harness();
  ctx.remotePeerRoom=true;
  ctx.previewReadOnly=true;
  ctx.focusRoom();
  assert.equal(calls[0][0],'GET');
  assert.match(calls[0][1],/^\/v1\/tty\/rooms\/room%2Fa\/observe\?cols=/);
  assert.equal(calls[0][2],null);
  ctx.previewReadOnly=false;
  ctx.focusRoom(true);
  assert.equal(calls.at(-1)[0], 'POST');
  assert.equal(calls.at(-1)[1], '/v1/tty/rooms/room%2Fa/focus');
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[2])), {openIfMissing:true});
  assert.match(qml,/readOnlyInput: [^\n]*readOnlyPeer/);
  assert.match(qml,/enabled: terminalChat\.terminalInteractive/);
  assert.match(qml,/previewHideInput: [^\n]*readOnlyPeer/);
  assert.match(qml,/shellProgram: [^]*?ares-peer-observe-client/);
  assert.match(qml,/function openSourcePreview\(/);
  assert.match(qml,/MapChatPreview \{/);
  // The status bar (with "Choose terminal") only shows while unresolved or on error;
  // a healthy attached terminal must not be covered by chooser/warning noise.
  assert.match(qml,/id: statusBar[\s\S]*?visible: [\s\S]*?&& \(!terminalChat\.terminalAttached \|\| terminalChat\.error !== ""\)/);
});

test('visible tiles attach without a click while remote tiles do not observe private output', () => {
  const expanded = readFileSync(new URL('../ExtendedWorkspace.qml', import.meta.url), 'utf8');
  assert.match(expanded,/previewFallback: !detailed && !liveTerminalCandidate/);
  assert.match(expanded,/tile\.detailed\s*\? extended\.controller\.extendedViewMode === "grown"/);
  assert.match(expanded,/tile\.warmViewport && \(tile\.detailed/);
  assert.doesNotMatch(expanded,/tile\.inViewport && \(tile\.detailed/);
});

test('peer frames are never retained as cross-room thumbnail caches', () => {
  const {ctx}=harness();
  const captured=[];
  ctx.remotePeerRoom=true; ctx.terminalAttached=true;
  ctx.controller={cacheTerminalFrame(...args){captured.push(args)}};
  ctx.captureFrame({width:700,height:400,grabToImage(callback){callback({url:'image://private'})}});
  assert.equal(captured.length,0);
});
