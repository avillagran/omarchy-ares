import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { performance } from 'node:perf_hooks';
const search = vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../RoomSearchModel.js', import.meta.url), 'utf8'), search);
const projects = [{id: 'p', node: 'local'}, {id: 'remote', node: 'node'}];
const rooms = [
  {id:'a',projectId:'p',alias:'K-view',name:'Verificar suma de views',updatedAt:100},
  {id:'b',projectId:'remote',alias:'Póster',name:'Diseño de afiche',updatedAt:300},
  {id:'c',projectId:'remote',name:'Another title',updatedAt:200},
];
const ids = rows => Array.from(rows, row => row.room.id);
test('alias and original title are both searchable, case and accent insensitive', () => {
  const index = search.build(rooms, projects);
  assert.deepEqual(ids(search.filter(index,'K-VIEW')), ['a']);
  assert.deepEqual(ids(search.filter(index,'views')), ['a']);
  assert.deepEqual(ids(search.filter(index,'poster diseno')), ['b']);
  assert.deepEqual(ids(search.filter(index,'  ')), ['b','c','a']);
  assert.deepEqual(ids(search.filter(index,'missing')), []);
});
test('search is flat across hosts, orders newest activity first and excludes invalid or duplicate rooms', () => {
  const index = search.build([...rooms,rooms[0],{id:'ctl',projectId:'p',source:'control'}, {id:'orphan',projectId:'missing'}], projects);
  assert.deepEqual(ids(search.filter(index,'')), ['b','c','a']);
  assert.equal(index[1].row.project.node,'node');
  assert.equal(index[1].row.kind,'room');
  assert.equal(index[1].row.depth,0);
});
test('relative activity labels use compact stable units', () => {
  assert.equal(search.activityLabel({updatedAt:1000}, 1020), 'now');
  assert.equal(search.activityLabel({updatedAt:1000}, 1120), '2m ago');
  assert.equal(search.activityLabel({updatedAt:1000}, 8200), '2h ago');
  assert.equal(search.activityLabel({updatedAt:1000}, 173800), '2d ago');
});
test('navigation clamps, empty results cannot activate, refresh preserves the highlighted room', () => {
  assert.equal(search.move(0,1,3),1);
  assert.equal(search.move(2,1,3),2);
  assert.equal(search.move(0,-1,3),0);
  assert.equal(search.move(0,1,0),-1);
  const rows = search.filter(search.build(rooms,projects),'');
  assert.equal(search.preserve(rows,'b'),0);
  assert.equal(search.preserve(rows,'gone'),0);
  assert.equal(search.preserve([],'b'),-1);
});
test('large in-memory index supports incremental filtering without rebuilding', () => {
  const index = search.build(Array.from({length:10000},(_,i)=>({id:String(i),projectId:'p',alias:'Chat '+i,name:'Project search fixture'})),projects);
  const before = performance.now();
  for(let i=0;i<100;i++) search.filter(index,'chat '+i);
  console.log('Search benchmark: 100 filters / 10000 rooms = '+(performance.now()-before).toFixed(1)+' ms');
  assert.equal(index.length,10000);
});
