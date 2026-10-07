import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const model = vm.createContext({});
vm.runInContext(readFileSync(new URL('../ArchivedWorkspaceModel.js', import.meta.url), 'utf8'), model);
const projects = [
  { id: 'p1', name: 'One', path: '/work/one' },
  { id: 'p2', name: 'Two', path: '/work/two' },
  { id: 'fallback', name: 'Legacy host bucket', path: 'peer://host/default' },
];
const archives = [
  { id: 'a', projectId: 'p2', name: 'A', eventCount: 1 },
  { id: 'b', projectId: 'p1', name: 'B', eventCount: 2 },
  { id: 'c', projectId: 'p2', name: 'C', eventCount: 3 },
  { id: 'd', projectId: 'fallback', name: 'D', eventCount: 4 },
  { id: 'e', projectId: 'missing', name: 'E', eventCount: 5 },
];

test('archives keep exact project identity, order and restore payload', () => {
  const groups = model.groups(archives, projects);
  assert.deepEqual(Array.from(groups, g => g.projectId), ['p2', 'p1', 'fallback', 'missing']);
  assert.deepEqual(Array.from(groups, g => Array.from(g.rooms, r => r.id)), [['a', 'c'], ['b'], ['d'], ['e']]);
  assert.deepEqual(Array.from(groups, g => g.count), [2, 1, 1, 1]);
  assert.equal(groups[0].rooms[0], archives[0]);
  assert.equal(groups[2].name, 'Unassigned / Home');
  assert.equal(groups[3].name, 'Unknown project');
});

test('empty archives do not invent projects or rooms', () => {
  assert.deepEqual(Array.from(model.groups([], projects)), []);
});

test('same project id remains one group even across peers', () => {
  const result = model.groups([
    { id: 'x', projectId: 'p1' }, { id: 'y', projectId: 'p2' }, { id: 'z', projectId: 'p1' },
  ], projects);
  assert.equal(result.length, 2);
  assert.deepEqual(Array.from(result[0].rooms, r => r.id), ['x', 'z']);
});
