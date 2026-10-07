import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const model = vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../ExtendedStackModel.js', import.meta.url), 'utf8'), model);
const room = (id, projectId, extra = {}) => ({id, projectId, status:'active', ...extra});
const slots = [{projectId:'p',x:0,y:0,width:100,height:100},
  {projectId:'far',x:900,y:0,width:100,height:100},
  {projectId:'near',x:100,y:0,width:100,height:100}];
const run = (current, rooms, order, layout = slots) => Array.from(model.stack(current, rooms, order, layout));
test('siblings exclusively, exact project identity rather than host or title', () => {
  const rows = [room('a','p'),room('x','near'),room('b','p'),room('c','p')];
  assert.deepEqual(run(rows[0], rows, ['x','c','a','b']), ['c','b']);
});
test('singleton falls back to spatial project neighbors, keeping each project together', () => {
  const rows = [room('a','p'),room('f','far'),room('n1','near'),room('n2','near')];
  assert.deepEqual(run(rows[0],rows,['a','f','n2','n1']), ['n2','n1','f']);
});
test('selection changes recompute siblings; zoom does not change world proximity', () => {
  const rows = [room('a','p'),room('f','far'),room('n1','near'),room('n2','near')];
  assert.deepEqual(run(rows[2],rows,['a','f','n1','n2']), ['n2']);
  const moved = slots.map(s => s.projectId === 'far' ? {...s,x:50} : s);
  assert.deepEqual(run(rows[0],rows,['a','n1','f','n2'],moved), ['f','n1','n2']);
});
test('excludes archived, deleted, control, missing and duplicate identities', () => {
  const rows = [room('a','p'),room('old','p',{externalArchived:true}),room('gone','p',{status:'deleted'}),room('ctl','p',{source:'control'}),room('n','near')];
  assert.deepEqual(run(rows[0],rows,['a','old','gone','ctl','n','n','missing']), ['n']);
});
test('no map context retains global ordering for singleton; no current room is empty', () => {
  const rows=[room('a','p'),room('f','far'),room('n','near')];
  assert.deepEqual(run(rows[0],rows,['a','f','n'],[]), ['f','n']);
  assert.deepEqual(run(null, rows,['a','f','n']), []);
});
