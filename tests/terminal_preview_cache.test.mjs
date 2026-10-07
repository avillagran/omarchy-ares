import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../TerminalPreviewCache.js', import.meta.url), 'utf8');
const script = vm.createContext({});
vm.runInContext(source, script);

test('read-only event previews stay bounded and retain the latest room synopsis', () => {
  let cache = { rows: {}, order: [] };
  for (let n = 0; n < 5; n++) cache = script.store(cache, `room-${n}`, [{ text: `line-${n}` }], 3);
  assert.deepEqual(Array.from(cache.order), ['room-2', 'room-3', 'room-4']);
  assert.equal(cache.rows['room-0'], undefined);
  assert.equal(cache.rows['room-4'][0].text, 'line-4');
  cache = script.store(cache, 'room-2', [{ text: 'updated' }], 3);
  assert.deepEqual(Array.from(cache.order), ['room-3', 'room-4', 'room-2']);
  assert.equal(cache.rows['room-2'][0].text, 'updated');
});

test('cold tiles request bounded latest events without touching the agent or rendering reasoning', () => {
  const source = readFileSync(new URL('../ExtendedWorkspace.qml', import.meta.url), 'utf8');
  assert.match(source, /function requestRoomPreview\(roomId\)/);
  assert.match(source, /before=9223372036854775807&limit=12/);
  assert.match(source, /kind === "message.user" \|\| kind === "message.assistant"/);
  assert.match(source, /if \(tile\.warmViewport && tile\.roomData\) extended\.requestRoomPreview\(tile\.roomData\.id\)/);
  assert.doesNotMatch(source, /opacity: surfacePainted \?/);
  assert.doesNotMatch(source, /model: tile\.cachedPreview/);
  assert.match(source, /previewFallback: !detailed && !liveTerminalCandidate/);
  assert.match(source, /model: tile\.previewFallback\s*\?[^\n]*tile\.cachedPreview\) : \[\]/);
});
