import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../TerminalFrameCache.js', import.meta.url), 'utf8');
const script = vm.createContext({});
vm.runInContext(source, script);

function add(state, id, frame, at, limit = 3) {
  return script.store(state, id, frame, at, limit);
}

test('captured terminal frames retain their image handles with a bounded LRU', () => {
  let state = { frames: {}, order: [] };
  const images = Array.from({ length: 5 }, (_, n) => ({ url: `image://quick/${n}` }));
  for (let i = 0; i < images.length; i++) state = add(state, `room-${i}`, images[i], i + 1);
  assert.deepEqual(Array.from(state.order), ['room-2', 'room-3', 'room-4']);
  assert.equal(state.frames['room-2'].image, images[2]);
  assert.equal(state.frames['room-0'], undefined);
  state = add(state, 'room-2', images[2], 10);
  assert.deepEqual(Array.from(state.order), ['room-3', 'room-4', 'room-2']);
});

test('a late grab cannot replace a newer image for the same room', () => {
  const newer = { url: 'image://quick/new' };
  const old = { url: 'image://quick/old' };
  let state = add({ frames: {}, order: [] }, 'room', newer, 20);
  const unchanged = add(state, 'room', old, 19);
  assert.equal(unchanged, state);
  assert.equal(unchanged.frames.room.image, newer);
});

test('terminal surfaces capture in memory and show a frame until the next live paint', () => {
  const panel = readFileSync(new URL('../Panel.qml', import.meta.url), 'utf8');
  const terminal = readFileSync(new URL('../TerminalChat.qml', import.meta.url), 'utf8');
  const tiles = readFileSync(new URL('../ExtendedWorkspace.qml', import.meta.url), 'utf8');
  const compact = readFileSync(new URL('../AresWorkspace.qml', import.meta.url), 'utf8');
  assert.match(panel, /import "TerminalFrameCache\.js" as TerminalFrameCache/);
  assert.match(panel, /function cacheTerminalFrame\(/);
  assert.match(panel, /terminalFrameCount: Object\.keys\(root\.terminalFrameCache\.frames\)\.length/);
  assert.match(terminal, /view\.grabToImage\(/);
  assert.match(terminal, /onImagePainted:/);
  assert.match(terminal, /else if \(!contentRefresh\.running\) contentRefresh\.start\(\)/);
  assert.match(tiles, /source: tile\.cachedFrame \? tile\.cachedFrame\.image\.url : ""/);
  assert.doesNotMatch(tiles, /opacity: surfacePainted \?/);
  assert.match(terminal, /opacity: terminalChat.surfacePainted \? 1 : 0\.01/);
  assert.match(compact, /source: workspace\.selectedFrame \? workspace\.selectedFrame\.image\.url : ""/);
});
