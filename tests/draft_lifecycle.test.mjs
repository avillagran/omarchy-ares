import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const panel = readFileSync(new URL('../Panel.qml', import.meta.url), 'utf8');
const methods = ['draftForRoom', 'imagesForRoom', 'loadDrafts', 'scheduleDraftSave', 'queueDraftSave', 'persistDraftNow', 'clearDraft', 'appendLocalEvent', 'stageControlInstruction'];
const source = methods.map(name => {
  const match = panel.match(new RegExp(`^  function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?^  \\}`, 'm'));
  assert.ok(match, `${name} not found in Panel.qml`);
  return match[0];
}).join('\n');

function subject(room = 'room-a') {
  const calls = [];
  const ctx = {
    roomDrafts: {}, roomDraftImages: {}, draftPendingByRoom: {}, draftWritesInFlight: {}, roomSendKeys: {},
    draftRestoring: false, selectedRoomId: room, roomText: '',
    selectedProject: { id: 'project-a' }, selectedRoom: { id: room, projectId: 'project-a' },
    pendingImages: [], pendingDraftRoomId: '', pendingDraftText: '', sendingImage: false,
    section: 'control', controlDraftsLoaded: true, rooms: [{id: 'room-a', status: 'active'}, {id: 'room-b', status: 'active'}],
    selectRoom(target) { ctx.selectedRoomId = target.id; ctx.selectedRoom = target; ctx.roomText = ctx.draftForRoom(target.id); },
    sendBotForRoom() { return { id: 'default' }; },
    clearSelectedImage() { ctx.pendingImages = []; },
    draftSave: { restart() {}, stop() {} }, draftRetry: { restart() {} }, errorText: '',
    bridge: {
      request(method, path, body, callback) { calls.push({ method, path, body, callback }); },
      send(room, text, artifactIds, callback, botId, idempotencyKey) {
        calls.push({ method: 'send', room, text, artifactIds, callback, botId, idempotencyKey });
      },
      sendTerminal(room, text, artifactIds, callback, idempotencyKey, confirmedEmptyComposer) {
        calls.push({ method: 'terminal', room, text, artifactIds, callback, idempotencyKey, confirmedEmptyComposer });
      },
      uploadImage(room, path, callback) { calls.push({ method: 'upload', room, path, callback }); },
      reload() {},
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return { ctx, calls };
}

test('a late load never replaces a newer local edit or the selected room', () => {
  const { ctx, calls } = subject();
  ctx.loadDrafts();
  ctx.roomText = 'new local prompt';
  ctx.scheduleDraftSave();
  calls.shift().callback({ ok: true, drafts: [
    { roomId: 'room-a', text: 'stale server prompt' },
    { roomId: 'room-b', text: 'other room' },
  ] });
  assert.equal(ctx.roomText, 'new local prompt');
  assert.equal(ctx.draftForRoom('room-a'), 'new local prompt');
  assert.equal(ctx.draftForRoom('room-b'), 'other room');
});

test('Control Room stages a reviewed instruction without sending or replacing an existing draft', () => {
  const { ctx, calls } = subject();
  ctx.controlDraftsLoaded = false;
  ctx.stageControlInstruction('room-b', 'too early');
  assert.equal(ctx.selectedRoomId, 'room-a');
  ctx.controlDraftsLoaded = true;
  ctx.roomDrafts = {'room-b': 'existing draft'};
  ctx.stageControlInstruction('room-b', 'new instruction');
  assert.equal(ctx.roomDrafts['room-b'], 'existing draft');
  assert.equal(ctx.selectedRoomId, 'room-a');
  assert.equal(calls.length, 0);
  ctx.roomDrafts = {};
  ctx.stageControlInstruction('room-b', 'review this carefully');
  assert.equal(ctx.selectedRoomId, 'room-b');
  assert.equal(ctx.section, 'home');
  assert.equal(ctx.roomText, 'review this carefully');
  ctx.persistDraftNow();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/v1/drafts/room-b');
  assert.equal(calls[0].body.text, 'review this carefully');
  assert.equal(calls.some(call => call.method === 'send' || call.method === 'terminal'), false);
});

test('writes for the same room are serialized and preserve the newest text', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'first';
  ctx.scheduleDraftSave();
  ctx.persistDraftNow();
  ctx.roomText = 'second';
  ctx.scheduleDraftSave();
  ctx.persistDraftNow();
  assert.equal(calls.length, 1, 'must not race two writes to the same draft');
  calls[0].callback({ ok: true });
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.text, 'second');
  calls[1].callback({ ok: true });
  assert.equal(ctx.draftForRoom('room-a'), 'second');
});

test('failed persistence retains the edit for retry', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'important';
  ctx.scheduleDraftSave();
  ctx.persistDraftNow();
  calls[0].callback({ ok: false, error: 'offline' });
  assert.equal(ctx.draftForRoom('room-a'), 'important');
  assert.equal(ctx.pendingDraftRoomId, 'room-a');
  assert.equal(ctx.errorText, 'offline');
});

test('a failed room save retries its own snapshot while another room is in flight', () => {
  const { ctx, calls } = subject();
  ctx.queueDraftSave('room-a', 'first room', [{ artifactId: 'image-a' }]);
  ctx.queueDraftSave('room-b', 'second room', [{ artifactId: 'image-b' }]);
  ctx.persistDraftNow();
  assert.equal(calls.length, 2);
  calls[0].callback({ ok: false, error: 'offline' });
  assert.equal(ctx.draftPendingByRoom['room-a'].text, 'first room');
  assert.equal(ctx.draftPendingByRoom['room-a'].artifactIds[0], 'image-a');
  assert.equal(ctx.draftPendingByRoom['room-b'], undefined);
});

test('synchronous failure does not lose an earlier room while draining the queue', () => {
  const { ctx, calls } = subject();
  ctx.bridge.request = (method, path, body, callback) => {
    calls.push({ method, path, body, callback });
    if (path.endsWith('/room-a')) callback({ ok: false, error: 'offline' });
  };
  ctx.queueDraftSave('room-a', 'first room', [{ artifactId: 'image-a' }]);
  ctx.queueDraftSave('room-b', 'second room', [{ artifactId: 'image-b' }]);
  ctx.persistDraftNow();
  assert.equal(calls.length, 2);
  assert.equal(ctx.draftPendingByRoom['room-a']?.text, 'first room');
  assert.equal(ctx.draftPendingByRoom['room-a']?.artifactIds[0], 'image-a');
  assert.equal(ctx.pendingDraftRoomId, 'room-a');
});

test('successful send does not erase a draft in another selected room', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'room A message';
  ctx.scheduleDraftSave();
  ctx.appendLocalEvent();
  const send = calls.find(call => call.method === 'send');
  ctx.selectedRoomId = 'room-b';
  ctx.selectedRoom = { id: 'room-b', projectId: 'project-b' };
  ctx.roomText = 'room B draft';
  ctx.scheduleDraftSave();
  send.callback({ ok: true });
  assert.equal(ctx.roomText, 'room B draft');
  assert.equal(ctx.draftForRoom('room-b'), 'room B draft');
});

test('editing the same room while sending keeps the newer prompt', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'first version';
  ctx.scheduleDraftSave();
  ctx.appendLocalEvent();
  const send = calls.find(call => call.method === 'send');
  ctx.roomText = 'newer version';
  ctx.scheduleDraftSave();
  send.callback({ ok: true });
  assert.equal(ctx.roomText, 'newer version');
  assert.equal(ctx.draftForRoom('room-a'), 'newer version');
});

test('uploads keep the original room and image snapshot across selection changes', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'inspect';
  ctx.pendingImages = [{ path: '/image-a' }, { path: '/image-b' }];
  ctx.appendLocalEvent();
  const first = calls.find(call => call.method === 'upload');
  ctx.selectedRoom = { id: 'room-b', projectId: 'project-b' };
  ctx.selectedRoomId = 'room-b';
  ctx.pendingImages = [{ path: '/image-c' }];
  first.callback({ ok: true, artifact: { id: 'art-a' } });
  const second = calls.filter(call => call.method === 'upload')[1];
  assert.equal(second.room.id, 'room-a');
  assert.equal(second.path, '/image-b');
  second.callback({ ok: true, artifact: { id: 'art-b' } });
  const send = calls.find(call => call.method === 'send');
  assert.equal(send.room.id, 'room-a');
  assert.deepEqual(Array.from(send.artifactIds), ['art-a', 'art-b']);
});

test('retrying an unchanged failed send reuses its idempotency key', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'important';
  ctx.scheduleDraftSave();
  ctx.appendLocalEvent();
  const first = calls.find(call => call.method === 'send');
  first.callback({ ok: false, error: 'unknown delivery' });
  ctx.appendLocalEvent();
  const second = calls.filter(call => call.method === 'send')[1];
  assert.ok(first.idempotencyKey);
  assert.equal(first.idempotencyKey, second.idempotencyKey);
});

test('Edit submits text to the terminal and clears only after acknowledgement', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'multiline\nprompt';
  ctx.scheduleDraftSave();
  ctx.appendLocalEvent(true, true);
  const send = calls.find(call => call.method === 'terminal');
  assert.ok(send, 'Herdr-owned rooms must not use Desktop send');
  assert.equal(calls.some(call => call.method === 'send'), false);
  assert.equal(send.room.id, 'room-a');
  assert.equal(send.text, 'multiline\nprompt');
  assert.equal(send.confirmedEmptyComposer, true);
  assert.equal(ctx.roomText, 'multiline\nprompt');
  send.callback({ ok: true, accepted: true });
  assert.equal(ctx.roomText, '');
});

test('Edit retains images and text when terminal image transport is unavailable', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'look here';
  ctx.pendingImages = [{ path: '/private/image', artifactId: 'art-1' }];
  ctx.appendLocalEvent(true, true);
  assert.equal(calls.some(call => call.method === 'send' || call.method === 'terminal'), false);
  assert.equal(ctx.roomText, 'look here');
  assert.equal(ctx.pendingImages.length, 1);
  assert.match(ctx.errorText, /image/i);
  assert.match(ctx.errorText, /not supported/i);
  assert.doesNotMatch(ctx.errorText, /not ready|finish saving/i);
});

test('terminal rejection keeps the draft and does not retry automatically', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'precious';
  ctx.scheduleDraftSave();
  ctx.appendLocalEvent(true, true);
  const send = calls.find(call => call.method === 'terminal');
  assert.ok(send);
  send.callback({ ok: false, error: 'Agent busy' });
  assert.equal(ctx.roomText, 'precious');
  assert.equal(ctx.draftForRoom('room-a'), 'precious');
  assert.equal(calls.filter(call => call.method === 'terminal').length, 1);
});

test('terminal draft cannot submit without review of the visible input', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'important';
  ctx.scheduleDraftSave();
  ctx.appendLocalEvent(true);
  assert.equal(calls.some(call => call.method === 'terminal'), false);
  assert.equal(ctx.roomText, 'important');
  assert.match(ctx.errorText, /terminal input|composer/i);
});
