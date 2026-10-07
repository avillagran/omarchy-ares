import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const model = vm.createContext({});
vm.runInContext(readFileSync(new URL('../ControlTilesModel.js', import.meta.url), 'utf8'), model);
const projects = Array.from({length: 9}, (_, i) =>
  ({id: `p${i}`, name: `Project ${i}`, path: `/work/${i}`, nodeId: i % 2 ? 'remote' : 'local'}));

test('project identity is preserved while saved order and new projects are merged', () => {
  const sorted = model.order(projects, ['p3', 'p0', 'p3', 'missing']);
  assert.deepEqual(Array.from(sorted, p => p.id), ['p3', 'p0', 'p1', 'p2', 'p4', 'p5', 'p6', 'p7', 'p8']);
  assert.equal(new Set(Array.from(sorted, p => p.id)).size, projects.length);
});

test('workspaces show no more than four project tiles and do not depend on host', () => {
  const ordered = model.order(projects, []);
  assert.equal(model.pageCount(ordered), 3);
  assert.deepEqual(Array.from(model.page(ordered, 0), p => p.id), ['p0', 'p1', 'p2', 'p3']);
  assert.deepEqual(Array.from(model.page(ordered, 2), p => p.id), ['p8']);
});

test('narrow panels keep two readable tiles per workspace', () => {
  assert.equal(model.pageCount(projects, 2), 5);
  assert.deepEqual(Array.from(model.page(projects, 1, 2), p => p.id), ['p2','p3']);
});

test('semantic zoom reveals eight compact territories then one detailed project', () => {
  assert.equal(model.zoomLevel(0.6), 'overview');
  assert.equal(model.zoomLevel(1), 'normal');
  assert.equal(model.zoomLevel(1.5), 'detail');
  assert.equal(model.capacityForZoom(0.6, false), 8);
  assert.equal(model.capacityForZoom(0.6, true), 4);
  assert.equal(model.capacityForZoom(1, false), 4);
  assert.equal(model.capacityForZoom(1, true), 2);
  assert.equal(model.capacityForZoom(1.5, false), 1);
  assert.equal(model.pageCount(projects, 8), 2);
  assert.deepEqual(Array.from(model.page(projects, 1, 8), p=>p.id), ['p8']);
  assert.equal(model.pageForProject(projects, 'p7', 8), 0);
  assert.equal(model.pageForProject(projects, 'p7', 1), 7);
});

test('compact territories fill the map with no overlap or clipping', () => {
  const rects = model.rectangles(8, 'overview');
  assert.equal(rects.length, 8);
  let area = 0;
  for (let i=0;i<rects.length;i++) {
    const a=rects[i]; area+=a.width*a.height;
    assert.ok(a.x>=0 && a.y>=0 && a.x+a.width<=1 && a.y+a.height<=1);
    for (let j=i+1;j<rects.length;j++) {
      const b=rects[j];
      assert.ok(a.x+a.width<=b.x || b.x+b.width<=a.x || a.y+a.height<=b.y || b.y+b.height<=a.y);
    }
  }
  assert.equal(area,1);
});

test('dwindle split fills a workspace exactly with no overlaps', () => {
  for (let n=1; n<=4; n++) {
    const rects = model.rectangles(n);
    assert.equal(rects.length, n);
    let area = 0;
    for (let i=0; i<n; i++) {
      const a=rects[i]; area += a.width*a.height;
      assert.ok(a.x>=0 && a.y>=0 && a.x+a.width<=1 && a.y+a.height<=1);
      for (let j=i+1; j<n; j++) {
        const b=rects[j];
        assert.ok(a.x+a.width<=b.x || b.x+b.width<=a.x || a.y+a.height<=b.y || b.y+b.height<=a.y);
      }
    }
    assert.equal(area, 1);
  }
});

test('dropping a header onto another tile swaps exact project IDs', () => {
  const ids=Array.from(projects, p=>p.id);
  assert.deepEqual(Array.from(model.swap(ids,'p0','p2')).slice(0,4), ['p2','p1','p0','p3']);
  assert.deepEqual(Array.from(model.swap(ids,'p0','p0')), ids);
  assert.deepEqual(Array.from(model.swap(ids,'p0','unknown')), ids);
});

test('Fit is compact even on large panels; physical zoom progressively reveals content', () => {
  assert.equal(model.zoomLevel(0.86,0.86),'overview');
  assert.equal(model.zoomLevel(1.16,0.86),'normal');
  assert.equal(model.zoomLevel(2.1,0.86),'detail');
  assert.equal(model.zoomLevel(0.33,0.33),'overview');
  assert.equal(model.zoomLevel(0.6,0.33),'normal');
  assert.equal(model.zoomLevel(1.6,0.33),'detail');
});

test('far zoom fits every project on one spatial map with stable non-overlapping slots', () => {
  const fortyFour = Array.from({length: 44}, (_, i) => ({id:`project-${i}`}));
  const layout = model.mapLayout(fortyFour, 850, 550);
  assert.equal(layout.slots.length, 44);
  assert.equal(new Set(Array.from(layout.slots, slot => slot.project.id)).size, 44);
  const fit = model.fitZoom(layout, 850, 550);
  assert.ok(fit > 0 && fit < 1);
  const visible = model.visibleTiles(layout, fit, 0, 0, 850, 550);
  assert.equal(visible.length, 44);
  for (const slot of layout.slots) {
    assert.ok(slot.x >= 0 && slot.y >= 0);
    assert.ok(slot.x + slot.width <= layout.width);
    assert.ok(slot.y + slot.height <= layout.height);
  }
  for (let i=0;i<layout.slots.length;i++) {
    const a=layout.slots[i];
    for(let j=i+1;j<layout.slots.length;j++) {
      const b=layout.slots[j];
      assert.ok(a.x+a.width<=b.x || b.x+b.width<=a.x || a.y+a.height<=b.y || b.y+b.height<=a.y);
    }
  }
});

test('camera zoom preserves the world point under the cursor and clamps panning', () => {
  const layout = model.mapLayout(projects, 850, 550);
  const before = {zoom:1, x:300, y:180};
  const after = model.zoomAt(before, 1.5, 210, 140, layout, 850, 550);
  assert.ok(Math.abs((before.x+210)/before.zoom - (after.x+210)/after.zoom) < 0.00001);
  assert.ok(Math.abs((before.y+140)/before.zoom - (after.y+140)/after.zoom) < 0.00001);
  assert.ok(after.x>=0 && after.y>=0);
  assert.ok(after.x<=Math.max(0,layout.width*after.zoom-850));
  assert.ok(after.y<=Math.max(0,layout.height*after.zoom-550));
  const fitted=model.zoomAt(after,0.001,210,140,layout,850,550);
  assert.ok(fitted.zoom>=model.fitZoom(layout,850,550));
  assert.equal(fitted.x,0);
  assert.equal(fitted.y,0);
});

test('continuous wheel zoom keeps the cursor world point fixed across density transitions', () => {
  const layout=model.mapLayout(Array.from({length:44},(_,i)=>({id:`p${i}`})),850,550);
  const fit=model.fitZoom(layout,850,550);
  let view={zoom:fit,x:0,y:0};
  const cursor={x:425,y:275};
  const origin={x:(view.x+cursor.x-Math.max(0,(850-layout.width*fit)/2))/fit,
    y:(view.y+cursor.y-Math.max(0,(550-layout.height*fit)/2))/fit};
  const seen=new Set();
  for(let step=0;step<28;step++) {
    view=model.zoomAt(view,1.07,cursor.x,cursor.y,layout,850,550);
    seen.add(model.zoomLevel(view.zoom,fit));
    const insetX=Math.max(0,(850-layout.width*view.zoom)/2);
    const insetY=Math.max(0,(550-layout.height*view.zoom)/2);
    assert.ok(Math.abs((view.x+cursor.x-insetX)/view.zoom-origin.x)<0.001);
    assert.ok(Math.abs((view.y+cursor.y-insetY)/view.zoom-origin.y)<0.001);
  }
  assert.deepEqual(Array.from(seen).sort(),['detail','normal','overview']);
  for(let step=0;step<28;step++) view=model.zoomAt(view,1/1.07,cursor.x,cursor.y,layout,850,550);
  assert.ok(Math.abs(view.zoom-fit)<0.001);
});

test('close zoom keeps the same slots and reveals only visible projects', () => {
  const layout=model.mapLayout(projects,850,550);
  const shown=model.visibleTiles(layout,2.5,0,0,850,550);
  assert.ok(shown.length>0 && shown.length<projects.length);
  assert.equal(model.slotAt(layout,layout.slots[0].x+20,layout.slots[0].y+20).project.id,projects[0].id);
  assert.equal(model.slotAt(layout,-5,10),null);
  assert.equal(model.zoomLevel(0.3),'overview');
  assert.equal(model.zoomLevel(0.9),'normal');
  assert.equal(model.zoomLevel(1.6),'detail');
});

test('user groups create non-overlapping districts and keep all projects in exactly one district', () => {
  const items=Array.from({length:44},(_,i)=>({id:`project-${i}`}));
  const categories={groups:[{id:'work',name:'Work',color:'#2299dd'},
    {id:'life',name:'Life',color:'#aa7744'}],assignments:{'project-0':'work','project-1':'life'}};
  const layout=model.mapLayout(items,850,550,categories);
  assert.deepEqual(Array.from(layout.groups,g=>g.id),['work','life','']);
  assert.equal(layout.slots.length,44);
  assert.equal(new Set(Array.from(layout.slots,s=>s.project.id)).size,44);
  assert.deepEqual(Array.from(layout.slots.filter(s=>s.groupId==='work'),s=>s.project.id),['project-0']);
  assert.equal(layout.slots.filter(s=>s.groupId==='').length,42);
  assert.equal(model.visibleTiles(layout,model.fitZoom(layout,850,550),0,0,850,550).length,44);
  for(const a of layout.groups) {
    for(const b of layout.groups) if(a!==b) assert.ok(a.y+a.height<=b.y || b.y+b.height<=a.y);
    for(const slot of layout.slots.filter(s=>s.groupId===a.id)) {
      assert.ok(slot.x>=a.x && slot.y>=a.y && slot.x+slot.width<=a.x+a.width && slot.y+slot.height<=a.y+a.height);
      assert.equal(model.slotAt(layout,slot.x+8,slot.y+8).project.id,slot.project.id);
    }
    assert.equal(model.slotAt(layout,a.x+5,a.y+5),null);
  }
});

test('room lists remain scoped to one project and hide archives until requested', () => {
  const rooms=[{id:'a',projectId:'p0',status:'active',controlIncluded:true},
    {id:'b',projectId:'p0',status:'active',controlIncluded:true,externalArchived:true},
    {id:'other',projectId:'p1',status:'active',controlIncluded:true}];
  const archived=[{id:'c',projectId:'p0',status:'deleted'}];
  assert.deepEqual(Array.from(model.projectRooms(rooms,archived,'p0',false), r=>r.id), ['a']);
  assert.deepEqual(Array.from(model.projectRooms(rooms,archived,'p0',true), r=>r.id), ['a','b','c']);
});
