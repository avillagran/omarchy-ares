import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const panel = fs.readFileSync(new URL('../Panel.qml', import.meta.url), 'utf8');
const modelSource = fs.readFileSync(new URL('../ControlMapModel.js', import.meta.url), 'utf8');

function functionText(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++;
    if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unclosed ${name}`);
}

function presence({room, events = [], executions = [], now = 1_800_000_000_000}) {
  const model = {};
  vm.runInNewContext(`${functionText(modelSource, 'freshExternalActivity')}; this.freshExternalActivity = freshExternalActivity`, model);
  const context = {rooms: [room], events, bridge: {state: {executions}},
    ControlMapModel: model, Date: class extends Date { static now() { return now; } }, presenceNow: now,
    roomNeedsReply: () => false};
  vm.runInNewContext(['latestRoomMessage', 'roomWorking', 'roomStatusLabel']
    .map(name => functionText(panel, name)).join('\n'), context);
  return {working: context.roomWorking(room.id), label: context.roomStatusLabel(room)};
}

const base = {id: 'r', source: 'hermes', status: 'active'};
const now = 1_800_000_000_000;
const user = {roomId: 'r', kind: 'message.user', cursor: 5, executionId: 'e'};

test('external presence is room-scoped and uses map freshness, not durable description alone', () => {
  assert.match(panel, /import "ControlMapModel\.js" as ControlMapModel/);
  assert.match(panel, /Timer \{ interval: 30000; running: root\.opened; repeat: true; onTriggered: root\.presenceNow = Date\.now\(\) \}/);
  assert.deepEqual(presence({room: {...base, externalActivity: 'tool running', externalActivityAt: now / 1000 - 30}}), {working: true, label: 'WORKING'});
  assert.deepEqual(presence({room: {...base, externalActivity: 'tool running', externalActivityAt: now / 1000 - 300}}), {working: true, label: 'WORKING'});
  assert.deepEqual(presence({room: {...base, externalActivity: '   ', externalActivityAt: now / 1000 - 30}}), {working: false, label: 'IDLE'});
  for (const externalActivityAt of [now / 1000 - 301, undefined, 0, now / 1000 + 30, Infinity]) {
    assert.deepEqual(presence({room: {...base, externalActivity: 'old prompt', externalActivityAt}, events: [user]}), {working: false, label: 'IDLE'});
  }
  assert.deepEqual(presence({room: {...base, source: 'ares', externalActivity: 'tool running', externalActivityAt: now / 1000 - 30}}), {working: false, label: 'IDLE'});
  assert.deepEqual(presence({room: {...base, externalActivity: 'tool running', externalActivityAt: now / 1000 - 30}, now: now + 360_000}), {working: false, label: 'IDLE'});
});

test('queued and running executions are authoritative; terminal outcomes are not working', () => {
  for (const status of ['queued', 'running'])
    assert.deepEqual(presence({room: base, executions: [{id: 'e', roomId: 'r', status}]}), {working: true, label: 'WORKING'});
  assert.deepEqual(presence({room: base, executions: [{id: 'e', roomId: 'another', status: 'running'}]}), {working: false, label: 'IDLE'});
  for (const status of ['completed', 'failed', 'cancelled', 'interrupted'])
    assert.deepEqual(presence({room: {...base, source: 'ares'}, events: [user], executions: [{id: 'e', roomId: 'r', status}]}), {working: false, label: 'IDLE'});
});

test('a local pending user turn without a terminal outcome remains pending', () => {
  const local = {...base, source: 'ares'};
  assert.deepEqual(presence({room: local, events: [user]}), {working: true, label: 'WORKING'});
  assert.deepEqual(presence({room: local, events: [user, {roomId: 'r', kind: 'message.assistant', cursor: 6}]}), {working: false, label: 'IDLE'});
  assert.deepEqual(presence({room: local, events: [user, {roomId: 'r', kind: 'execution.failed', executionId: 'e', cursor: 6}]}), {working: false, label: 'IDLE'});
});
