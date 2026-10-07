import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
const panel = readFileSync(new URL('../Panel.qml', import.meta.url), 'utf8');
const source = ['toggleRoomPause', 'archiveRoom'].map(name => {
  const match = panel.match(new RegExp(`^  function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?^  \\}`, 'm'));
  assert.ok(match);
  return match[0];
}).join('\n');
for (const [method, action] of [['toggleRoomPause','pause'], ['archiveRoom','archive']]) {
  test(`${action} uses the declared bridge id, not an absent root property`, () => {
    const room = {id:'fixture',status:'active'};
    const calls = [];
    const ctx = {
      root:{selectedRoom:room,selectedRoomId:'fixture',extendedRoomId:'',errorText:''},
      bridge:{
        lifecycle(target, requested, callback) { calls.push({target,requested}); callback({ok:true}); },
        reload() { calls.push({reload:true}); }
      }
    };
    vm.createContext(ctx); vm.runInContext(source,ctx);
    ctx[method](room);
    assert.equal(calls[0].requested,action);
    assert.equal(calls[0].target,room);
    assert.equal(calls[1].reload,true);
  });
}
