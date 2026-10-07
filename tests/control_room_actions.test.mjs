import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../ControlRoomActions.js', import.meta.url), 'utf8');
const actions = vm.createContext({});
vm.runInContext(source, actions);

const room = {id: 'room-a', status: 'active', source: 'hermes', controlIncluded: true};

test('archiving requires an included active room with no known running or waiting work', () => {
  assert.equal(actions.canArchive(room, []), true);
  for (const status of ['working', 'queued', 'running', 'blocked', 'waiting', 'needs_input'])
    assert.equal(actions.canArchive(room, [{roomId: room.id, status}]), false, status);
  assert.equal(actions.canArchive({...room, status: 'paused'}, []), false);
  assert.equal(actions.canArchive({...room, controlIncluded: false}, []), false);
  assert.equal(actions.canArchive({...room, source: 'control'}, []), false);
  assert.equal(actions.canArchive(null, []), false);
});


test('source-archived rooms cannot be archived again from the map', () => {
  assert.equal(actions.canArchive({...room, externalArchived: true}, []), false);
  assert.equal(actions.canArchive({...room, externalArchived: false}, []), true);
});
