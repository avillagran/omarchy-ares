import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const panel = readFileSync(new URL('../Panel.qml', import.meta.url), 'utf8');
const names = [
  'draftForRoom', 'imagesForRoom', 'restoreSelectedDraft', 'loadDrafts',
  'scheduleDraftSave', 'queueDraftSave', 'persistDraftNow', 'setRoomImages', 'addPendingImage', 'removePendingImage',
  'stageNextDraftImage', 'appendLocalEvent',
];
const source = names.map(name => {
  const found = panel.match(new RegExp(`^  function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?^  \\}`, 'm'));
  return found ? found[0] : '';
}).join('\n');
function subject() {
  const calls = [];
  const ctx = {
    selectedRoomId: 'a', selectedRoom: { id: 'a', projectId: 'project-a' },
    roomText: '', roomDrafts: {}, roomDraftImages: {}, pendingImages: [], draftRestoring: false,
    draftPendingByRoom: {}, draftWritesInFlight: {}, pendingDraftRoomId: '', pendingDraftText: '',
    draftSave: { restart() {} }, draftRetry: { restart() {} }, errorText: '',
    imageStagingQueue: [], imageStagingBusy: false,
    sendingImage: false, roomSendKeys: {},
    selectedProject: { id: 'project-a' },
    sendBotForRoom() { return { id: 'default' }; },
    clearSelectedImage() {}, clearDraft() {},
    bridge: {
      artifactRoot: '/private/artifacts',
      uploadImage(room, path, callback) { calls.push({ kind: 'upload', room, path, callback }); },
      send(room, text, artifactIds, callback) { calls.push({ kind: 'send', room, text, artifactIds, callback }); },
      request(method, path, body, callback) { calls.push({ kind: 'request', method, path, body, callback }); },
      reload() {},
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return { ctx, calls };
}

test('uploaded images stay with their original room after selection changes', () => {
  const { ctx, calls } = subject();
  assert.equal(typeof ctx.addPendingImage, 'function');
  ctx.addPendingImage('/source/a.png');
  const first = calls.find(x => x.kind === 'upload');
  assert.equal(first.room.id, 'a');
  ctx.selectedRoomId = 'b';
  ctx.selectedRoom = { id: 'b', projectId: 'project-b' };
  ctx.restoreSelectedDraft();
  assert.equal(ctx.pendingImages.length, 0);
  first.callback({ ok: true, artifact: { id: 'art-a', name: 'a.png' } });
  assert.equal(ctx.pendingImages.length, 0);
  assert.equal(ctx.imagesForRoom('a')[0].artifactId, 'art-a');
  assert.equal(ctx.imagesForRoom('a')[0].path, '/private/artifacts/art-a');
});

test('a delayed file-picker result attaches to the room that opened it', () => {
  const { ctx, calls } = subject();
  ctx.rooms = [{ id: 'a', projectId: 'project-a' }, { id: 'b', projectId: 'project-b' }];
  ctx.selectedRoomId = 'b';
  ctx.selectedRoom = ctx.rooms[1];
  ctx.addPendingImage('/source/a.png', 'a');
  assert.equal(calls.find(x => x.kind === 'upload').room.id, 'a');
  assert.equal(ctx.imagesForRoom('b').length, 0);
});

test('two fast images stage sequentially in attachment order', () => {
  const { ctx, calls } = subject();
  assert.equal(typeof ctx.addPendingImage, 'function');
  ctx.addPendingImage('/source/a.png');
  ctx.addPendingImage('/source/b.png');
  assert.equal(calls.filter(x => x.kind === 'upload').length, 1);
  calls.find(x => x.kind === 'upload').callback({ ok: true, artifact: { id: 'art-a' } });
  const second = calls.filter(x => x.kind === 'upload')[1];
  assert.equal(second.path, '/source/b.png');
  second.callback({ ok: true, artifact: { id: 'art-b' } });
  assert.deepEqual(Array.from(ctx.imagesForRoom('a'), x => x.artifactId), ['art-a', 'art-b']);
});

test('an image removed during upload does not reappear on completion', () => {
  const { ctx, calls } = subject();
  assert.equal(typeof ctx.addPendingImage, 'function');
  ctx.addPendingImage('/source/a.png');
  ctx.removePendingImage(0);
  calls.find(x => x.kind === 'upload').callback({ ok: true, artifact: { id: 'art-a' } });
  assert.equal(ctx.imagesForRoom('a').length, 0);
});

test('reloaded draft restores a private stored image without relying on source file', () => {
  const { ctx, calls } = subject();
  assert.equal(typeof ctx.restoreSelectedDraft, 'function');
  ctx.loadDrafts();
  calls[0].callback({ ok: true, drafts: [{ roomId: 'a', text: 'remember',
    attachmentIds: ['art-a'], attachments: [{ id: 'art-a', name: 'a.png', mime: 'image/png' }] }] });
  assert.equal(ctx.roomText, 'remember');
  assert.equal(ctx.pendingImages[0].path, '/private/artifacts/art-a');
  assert.equal(ctx.pendingImages[0].name, 'a.png');
});

test('send waits for staging then uses stored artifact without a second upload', () => {
  const { ctx, calls } = subject();
  ctx.roomText = 'inspect';
  ctx.addPendingImage('/source/a.png');
  ctx.appendLocalEvent();
  assert.equal(calls.filter(x => x.kind === 'send').length, 0);
  assert.equal(calls.filter(x => x.kind === 'upload').length, 1);
  calls.find(x => x.kind === 'upload').callback({ ok: true, artifact: { id: 'art-a' } });
  ctx.appendLocalEvent();
  assert.equal(calls.filter(x => x.kind === 'upload').length, 1);
  const send = calls.find(x => x.kind === 'send');
  assert.deepEqual(Array.from(send.artifactIds), ['art-a']);
});
