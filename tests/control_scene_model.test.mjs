import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../ControlSceneModel.js', import.meta.url), 'utf8');
const scene = vm.createContext({});
vm.runInContext(source, scene);

const rooms = [
  { id: 'one', name: 'Research', source: 'hermes', status: 'active', controlIncluded: true, projectId: 'p1', participants: ['architect', 'reviewer'] },
  { id: 'two', name: 'Build', source: 'ares', status: 'active', controlIncluded: true, projectId: 'p2', participants: ['default'] },
  { id: 'three', name: 'Private', source: 'hermes', status: 'active', controlIncluded: false },
  { id: 'four', name: 'Archived', source: 'ares', status: 'paused', controlIncluded: true },
  { id: 'control', name: 'Control Room', source: 'control', status: 'active', controlIncluded: false },
];
const bots = [
  { id: 'architect', name: 'Architect', faceShape: 'triangle', colorRole: 'warning' },
  { id: 'reviewer', name: 'Reviewer', faceShape: 'diamond', colorRole: 'success' },
  { id: 'default', name: 'Default', faceShape: 'oval', colorRole: 'accent' },
];
const projects = [{ id: 'p1', name: 'Orion' }, { id: 'p2', name: 'Forge' }];
const build = (r, i, logs = {}, limit = 12) => scene.build(r, i, bots, projects, logs, limit);

test('included rooms without pending work remain visible as idle people', () => {
  const people = build(rooms, []).people;
  assert.deepEqual(Array.from(people, p => [p.roomId, p.activity]), [['one', 'idle'], ['one', 'idle'], ['two', 'idle']]);
  assert.equal(people.some(p => p.roomId === 'three' || p.roomId === 'control'), false);
  assert.deepEqual(Array.from(people, p => [p.projectName, p.shape, p.colorRole]),
    [['Orion', 'triangle', 'warning'], ['Orion', 'diamond', 'success'], ['Forge', 'oval', 'accent']]);
});

test('working and waiting rooms get distinct scene activities, one person per room', () => {
  const items = [
    { id: 'agent:one', roomId: 'one', kind: 'agent', status: 'working' },
    { id: 'exec:one', roomId: 'one', kind: 'execution', status: 'running' },
    { id: 'event:two', roomId: 'two', kind: 'clarify.request', status: 'waiting' },
  ];
  const result = build(rooms, items, { one: [{ text: 'Test suite passed', kind: 'tool.result' }] });
  assert.deepEqual(Array.from(result.people, p => [p.roomId, p.activity]),
    [['one', 'working'], ['one', 'working'], ['two', 'waiting']]);
  assert.equal(result.people[0].logLines[0].text, 'Test suite passed');
  assert.deepEqual({ ...result.counts }, { working: 2, waiting: 1, idle: 0 });
});

test('empty room list creates no fictitious agent and roster stays bounded', () => {
  assert.equal(build([], []).people.length, 0);
  const many = Array.from({ length: 50 }, (_, n) => ({ id: `room-${n}`, name: `Room ${n}`, status: 'active', controlIncluded: true }));
  const result = build(many, [], {}, 9);
  assert.equal(result.people.length, 3);
  assert.equal(result.counts.idle, 50);
});

test('busy rooms cannot hide the gathering or attention signal when the stage is crowded', () => {
  const crowd = Array.from({length: 15}, (_, n) => ({ id: `busy-${n}`, status: 'active', controlIncluded: true }));
  crowd.push({ id: 'waiting', status: 'active', controlIncluded: true });
  crowd.push({ id: 'idle', status: 'active', controlIncluded: true });
  const items = crowd.slice(0, 15).map(room => ({roomId: room.id, status: 'working'}));
  items.push({roomId: 'waiting', status: 'waiting'});
  const result = build(crowd, items, {}, 9);
  assert.ok(result.people.some(p => p.activity === 'waiting'));
  assert.ok(result.people.some(p => p.activity === 'idle'));
  assert.equal(result.people.filter(p => p.activity === 'working').length, 4);
  assert.equal(result.people.length, 6);
});

test('a pending human request overrides simultaneous background work for the same room', () => {
  const result = build([rooms[0]], [
    {roomId: 'one', kind: 'agent', status: 'working'},
    {roomId: 'one', kind: 'clarify.request', status: 'waiting'},
  ]);
  assert.equal(result.people[0].activity, 'waiting');
});

test('the gathering shows distinct configured bot faces before duplicate profiles', () => {
  const repeats = Array.from({length: 8}, (_, n) => ({id: `default-${n}`, status: 'active', controlIncluded: true, participants: ['default']}));
  repeats.push({id: 'architect-room', status: 'active', controlIncluded: true, participants: ['architect']});
  repeats.push({id: 'reviewer-room', status: 'active', controlIncluded: true, participants: ['reviewer']});
  const result = build(repeats, []);
  assert.deepEqual(Array.from(result.people, p => p.botId), ['default', 'architect', 'reviewer']);
});
