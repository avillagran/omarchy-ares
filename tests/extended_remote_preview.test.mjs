import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../ExtendedWorkspace.qml', import.meta.url), 'utf8');
const remote = { id: 'remote-room', source: 'hermes', externalProfile: 'remote::peer::default' };
const local = { id: 'local-room', source: 'hermes', externalProfile: 'default' };
const malformed = { id: 'bad-room', source: 'hermes', externalProfile: 'not-remote::peer' };
const wrongSource = { id: 'other-room', source: 'control', externalProfile: 'remote::peer::default' };

function tileProperty(name, roomData, extra = {}) {
  const match = source.match(new RegExp('readonly property (?:bool|var) ' + name + ': ([^\\n]*(?:\\n      [^\\n]*)*)'));
  assert.ok(match, `missing ${name}`);
  return vm.runInNewContext(match[1], { roomData, ...extra });
}

function previewCalls(room) {
  const calls = [];
  const controller = { rooms: [room], opened: true };
  const ctx = vm.createContext({ controller, bridge: { available: true, request(...args) { calls.push(args); } },
    roomPreviewCache: { rows: {} }, previewRequests: {} });
  ctx.extended = ctx;
  vm.runInContext(source.match(/  function requestRoomPreview\(roomId\) \{[^]*?\n  \}/)[0], ctx);
  ctx.requestRoomPreview(room.id);
  return calls;
}

test('canonical paired-room DTO selects fallback and never reads cached events or frames', () => {
  for (const room of [remote, local, malformed, wrongSource]) {
    const isRemote = room === remote;
    const peer = tileProperty('remotePeerRoom', room);
    assert.equal(peer, isRemote, room.id);
    assert.equal(tileProperty('previewFallback', room, { detailed: false, liveTerminalCandidate: room.source !== 'control', remotePeerRoom: peer }), room.source === 'control', room.id);
    const gatedByProfile = room.externalProfile.startsWith('remote::');
    assert.equal(previewCalls(room).length, gatedByProfile ? 0 : 1, room.id);
    const requested = [];
    const tile = { roomData: room, warmViewport: true, liveTerminalCandidate: room.source !== 'control' };
    const context = vm.createContext({ tile, extended: { requestRoomPreview(id) { requested.push(id); } } });
    vm.runInContext(source.match(/    function requestPreview\(\) \{[^]*?\n    \}/)[0], context);
    context.requestPreview();
    assert.deepEqual(requested, [], room.id);
  }
  const extra = { extended: { controller: { extendedMode: true, terminalFrameCache: { frames: { [remote.id]: { image: 'private' } } }, eventsForRoom() { throw Error('cached remote events accessed'); }, extendedViewMode: 'overview' }, visible: true, roomPreviewCache: { rows: {} } }, tile: { detailed: false, previewFallback: true, inViewport: true }, tileTerminalChat: { legacyTranscriptNeeded: false } };
  assert.equal(tileProperty('cachedFrame', remote, extra), null);
  assert.deepEqual(Array.from(tileProperty('roomEvents', remote, extra)), []);
  const stalePreview = { summaryText: 'stale cached private message' };
  const cachedExtra = { extended: { roomPreviewCache: { rows: { [remote.id]: [stalePreview], [local.id]: [stalePreview] } } } };
  assert.deepEqual(Array.from(tileProperty('cachedPreview', remote, cachedExtra)), []);
  assert.equal(tileProperty('cachedPreview', local, cachedExtra)[0], stalePreview);
  assert.doesNotMatch(source, /\.external_profile\b/);
  const localFrame = { image: 'local' };
  const localExtra = { ...extra, tile: { ...extra.tile, previewFallback: false }, extended: { ...extra.extended, controller: {
    ...extra.extended.controller, extendedMode: true, terminalFrameCache: { frames: { [local.id]: localFrame } },
    eventsForRoom() { return [{kind: 'message.user'}]; }
  } } };
  assert.equal(tileProperty('cachedFrame', local, localExtra), localFrame);
  assert.equal(tileProperty('roomEvents', local, localExtra).length, 0);
});

test('terminal room classification accepts canonical DTO only', () => {
  const terminal = readFileSync(new URL('../TerminalChat.qml', import.meta.url), 'utf8');
  const expr = terminal.match(/readonly property bool remotePeerRoom: ([^\n]*\n    && [^\n]*)/)[1];
  for (const [roomRecord, expected] of [[remote, true], [local, false], [malformed, false],
    [wrongSource, false], [{source: 'hermes', external_profile: 'remote::peer::default'}, false]]) {
    assert.equal(vm.runInNewContext(expr, { roomRecord }), expected);
  }
});
