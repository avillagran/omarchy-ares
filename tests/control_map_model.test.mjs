import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const model = vm.createContext({});
vm.runInContext(readFileSync(new URL('../ControlMapModel.js', import.meta.url), 'utf8'), model);
const rooms = Array.from({ length: 86 }, (_, n) => ({
  id: `room-${n}`, name: `Room ${n}`, projectId: 'project', status: 'active',
  source: 'hermes', controlIncluded: true, participants: ['default', 'reviewer', 'default'],
}));
const bots = [{id: 'default', name: 'Default', faceShape: 'circle', colorRole: 'accent'},
  {id: 'reviewer', name: 'Reviewer', faceShape: 'triangle', colorRole: 'success'}];
const projects = [{id: 'project', name: 'Project One'}];
const build = (r = rooms, items = []) => model.build(r, items, bots, projects);

test('every included active room gets one stable nonoverlapping map node independent of order', () => {
  const nodes = build();
  assert.equal(nodes.length, 86);
  assert.equal(new Set(Array.from(nodes, n => n.roomId)).size, 86);
  const reordered = build([...rooms].reverse());
  const byId = new Map(Array.from(reordered, n => [n.roomId, `${n.x},${n.y}`]));
  for (const node of nodes) {
    assert.equal(`${node.x},${node.y}`, byId.get(node.roomId));
    assert.ok(node.x >= 0 && node.y >= 0 && node.x + model.NODE_WIDTH <= model.WORLD_WIDTH && node.y + model.NODE_HEIGHT <= model.WORLD_HEIGHT);
  }
  for (let i = 0; i < nodes.length; ++i)
    for (let j = i + 1; j < nodes.length; ++j)
      assert.ok(nodes[i].x + model.NODE_WIDTH <= nodes[j].x || nodes[j].x + model.NODE_WIDTH <= nodes[i].x ||
        nodes[i].y + model.NODE_HEIGHT <= nodes[j].y || nodes[j].y + model.NODE_HEIGHT <= nodes[i].y);
});

test('excludes opted-out, inactive and control rooms without inventing bot identities', () => {
  const nodes = build([rooms[0], {...rooms[1], controlIncluded: false},
    {...rooms[2], status: 'paused'}, {...rooms[3], source: 'control'},
    {...rooms[4], participants: []}]);
  assert.deepEqual(Array.from(nodes, n => n.roomId), ['room-0', 'room-4']);
  assert.equal(nodes[1].botId, '');
  assert.equal(nodes[1].additionalMembers, 0);
});

test('nodes carry room and project names and configured primary face with a member badge', () => {
  const node = build([rooms[0]])[0];
  assert.equal(node.roomName, 'Room 0');
  assert.equal(node.projectName, 'Project One');
  assert.equal(node.botId, 'default');
  assert.equal(node.botName, 'Default');
  assert.equal(node.shape, 'oval');
  assert.equal(node.colorRole, 'accent');
  assert.equal(node.additionalMembers, 1);
});

test('pointer-anchored zoom preserves the map coordinate and clamps both zoom and pan', () => {
  const before = {zoom: 1, x: 700, y: 350};
  const next = model.zoomAt(before, 2, 240, 120, 800, 500);
  assert.equal(next.zoom, 2);
  assert.equal((next.x + 240) / next.zoom, (before.x + 240) / before.zoom);
  assert.equal((next.y + 120) / next.zoom, (before.y + 120) / before.zoom);
  const bounded = model.zoomAt({zoom: 2.5, x: 90000, y: -30}, 100, 300, 200, 800, 500);
  assert.equal(bounded.zoom, model.MAX_ZOOM);
  assert.ok(bounded.x <= model.WORLD_WIDTH * bounded.zoom - 800 && bounded.x >= 0);
  assert.ok(bounded.y <= model.WORLD_HEIGHT * bounded.zoom - 500 && bounded.y >= 0);
  assert.equal(model.zoomAt({zoom: .1, x: 0, y: 0}, .001, 0, 0, 800, 500).zoom, model.MIN_ZOOM);
});

test('fit works for a compact viewport without exceeding the bounded minimum', () => {
  const view = model.fit(360, 220);
  assert.ok(model.WORLD_WIDTH * view.zoom <= 360);
  assert.ok(model.WORLD_HEIGHT * view.zoom <= 220);
  assert.ok(view.zoom >= model.MIN_ZOOM);
});

test('fit shows the entire world and reset fits available content', () => {
  const fit = model.fit(800, 500);
  assert.ok(fit.zoom >= model.MIN_ZOOM && fit.zoom <= model.MAX_ZOOM);
  assert.ok(model.WORLD_WIDTH * fit.zoom <= 800 && model.WORLD_HEIGHT * fit.zoom <= 500);
  const areas = [{x: 2100, y: 1100, width: 400, height: 300}];
  assert.deepEqual({...model.reset(800, 500, areas)}, {...model.overview(areas, 800, 500)});
  assert.deepEqual({...model.reset(800, 500, [])}, {...model.fit(800, 500)});
});

test('initial map view centers a waiting or working room at readable scale', () => {
  const nodes = [{roomId: 'idle', activity: 'idle', x: 100, y: 100},
    {roomId: 'working', activity: 'working', x: 3600, y: 2500},
    {roomId: 'waiting', activity: 'waiting', x: 4200, y: 3100}];
  const view = model.focus(nodes, 800, 500);
  assert.equal(view.zoom, 1);
  assert.ok(Math.abs((nodes[2].x + model.NODE_WIDTH / 2) - (view.x + 400)) <= 1);
  assert.ok(Math.abs((nodes[2].y + model.NODE_HEIGHT / 2) - (view.y + 250)) <= 1);
  assert.equal(model.focus([], 800, 500), null);
  assert.equal(model.focus(nodes.slice(0, 1), 800, 500), null);
  assert.equal(model.focusTarget(nodes), nodes[2]);
});

test('waiting overrides working; only working items with active delegation mark parent', () => {
  const nodes = build(rooms.slice(0, 3), [
    {roomId: 'room-0', status: 'working', kind: 'delegation', delegationUnits: 2},
    {roomId: 'room-1', status: 'working', kind: 'delegation', delegationUnits: 1},
    {roomId: 'room-1', status: 'waiting'},
    {roomId: 'room-2', status: 'waiting', kind: 'delegation', delegationUnits: 9},
  ]);
  const byId = new Map(Array.from(nodes, n => [n.roomId, n]));
  assert.equal(byId.get('room-0').activity, 'working');
  assert.equal(byId.get('room-0').parentWorking, true);
  assert.equal(byId.get('room-1').activity, 'waiting');
  assert.equal(byId.get('room-1').parentWorking, false);
  assert.equal(byId.get('room-2').parentWorking, false);
});

test('nodes carry their project and host so territories can be derived', () => {
  const multi = [
    {...rooms[0], id: 'room-a', projectId: 'p1'},
    {...rooms[1], id: 'room-b', projectId: 'p2'},
    {...rooms[2], id: 'room-c', projectId: 'p2'},
  ];
  const projects = [{id: 'p1', name: 'One', node: 'local'}, {id: 'p2', name: 'Two', node: 'node'}];
  const nodes = model.build(multi, [], bots, projects);
  const byId = new Map(Array.from(nodes, n => [n.roomId, n]));
  assert.equal(byId.get('room-a').projectId, 'p1');
  assert.equal(byId.get('room-a').host, 'local');
  assert.equal(byId.get('room-b').projectId, 'p2');
  assert.equal(byId.get('room-b').host, 'node');
  assert.equal(byId.get('room-c').host, 'node');
});

test('project territories contain their rooms without a parent host territory', () => {
  const multi = [
    {...rooms[0], id: 'room-a', projectId: 'p1'},
    {...rooms[1], id: 'room-b', projectId: 'p2'},
    {...rooms[2], id: 'room-c', projectId: 'p2'},
    {...rooms[3], id: 'room-d', projectId: 'missing'},
  ];
  const projects = [{id: 'p1', name: 'One', node: 'local'}, {id: 'p2', name: 'Two', node: 'node'}];
  const nodes = model.build(multi, [], bots, projects);
  const all = model.territories(nodes, projects);
  const hosts = all.filter(t => t.kind === 'host');
  const pros = all.filter(t => t.kind === 'project');
  assert.equal(hosts.length, 0);
  assert.equal(pros.length, 3);
  const byId = new Map(Array.from(all, t => [t.id, t]));
  const contains = (t, n) => n.x >= t.x && n.y >= t.y && n.x + model.NODE_WIDTH <= t.x + t.width && n.y + model.NODE_HEIGHT <= t.y + t.height;
  const nodeById = new Map(Array.from(nodes, n => [n.roomId, n]));
  for (const n of nodes) {
    const pro = pros.find(t => contains(t, n));
    assert.ok(pro, n.roomId);
    assert.equal(pro.node, n.host);
    assert.equal(pro.parentId, '');
  }
  const unknown = byId.get('missing');
  assert.equal(unknown.name, 'Unknown project');
  assert.equal(unknown.node, 'unknown');
  assert.ok(contains(byId.get('p2'), nodeById.get('room-b')));
  assert.ok(contains(byId.get('p2'), nodeById.get('room-c')));
});

test('project layout is host-independent and projects from different machines share a grid', () => {
  const projects = [
    {id: 'local-a', name: 'A', node: 'local'},
    {id: 'remote-b', name: 'B', node: 'host'},
    {id: 'local-c', name: 'C', node: 'local'},
  ];
  const multi = projects.map((p, i) => ({...rooms[i], id: `r${i}`, projectId: p.id}));
  const areas = model.territories(model.build(multi, [], bots, projects), projects);
  assert.deepEqual(Array.from(areas, a => a.name), ['A', 'B', 'C']);
  assert.ok(areas[0].x < areas[1].x && areas[1].x < areas[2].x);
  assert.deepEqual(Array.from(areas, a => a.node), ['local', 'host', 'local']);
});

test('dense remote project does not push local project territories outside the map', () => {
  const projects = Array.from({length: 15}, (_, i) => ({
    id: `local-${i}`, name: `Project ${String(i).padStart(2, '0')}`, node: 'local',
  }));
  projects.push({id: 'remote', name: 'NodeBot', node: 'host'});
  const dense = Array.from({length: 231}, (_, i) => ({
    ...rooms[0], id: `remote-${i}`, projectId: 'remote',
  }));
  const sparse = projects.slice(0, -1).map((p, i) => ({
    ...rooms[0], id: `local-${i}`, projectId: p.id,
  }));
  const areas = model.territories(model.build([...dense, ...sparse], [], bots, projects), projects);
  assert.equal(areas.length, projects.length);
  for (const area of areas) {
    assert.ok(area.x >= 0 && area.y >= 0, area.id);
    assert.ok(area.x + area.width <= model.WORLD_WIDTH, area.id);
    assert.ok(area.y + area.height <= model.WORLD_HEIGHT, area.id);
  }
});

test('project territories never overlap and layout is stable under reordering', () => {
  const multi = rooms.map((r, i) => ({...r, id: `room-${i}`, projectId: 'p' + (i % 5)}));
  const projects = Array.from({length: 5}, (_, i) => ({id: 'p' + i, name: 'P' + i, node: i % 2 ? 'node' : 'local'}));
  const nodes = model.build(multi, [], bots, projects);
  const all = model.territories(nodes, projects);
  const pros = all.filter(t => t.kind === 'project');
  for (let i = 0; i < pros.length; ++i)
    for (let j = i + 1; j < pros.length; ++j)
      assert.ok(pros[i].x + pros[i].width <= pros[j].x || pros[j].x + pros[j].width <= pros[i].x ||
        pros[i].y + pros[i].height <= pros[j].y || pros[j].y + pros[j].height <= pros[i].y,
        `${pros[i].id} overlaps ${pros[j].id}`);
  const shuffled = model.build([...multi].reverse(), [], [...bots].reverse(), [...projects].reverse());
  const a = new Map(Array.from(nodes, n => [n.roomId, `${n.x},${n.y}`]));
  for (const n of shuffled) assert.equal(`${n.x},${n.y}`, a.get(n.roomId));
});

test('focusBounds frames a territory with margin and keeps zoom inside bounds', () => {
  const t = {x: 1000, y: 800, width: 600, height: 400};
  const view = model.focusBounds(t, 800, 500);
  const worldZoom = Math.min(800 / model.WORLD_WIDTH, 500 / model.WORLD_HEIGHT);
  assert.ok(view.zoom >= worldZoom && view.zoom <= model.MAX_ZOOM);
  assert.ok(Math.abs((view.x + 400) / view.zoom - (t.x + t.width / 2)) <= 2);
  assert.ok(Math.abs((view.y + 250) / view.zoom - (t.y + t.height / 2)) <= 2);
  assert.ok(view.x >= 0 && view.y >= 0);
  const tiny = model.focusBounds({x: 0, y: 0, width: 200, height: 120}, 400, 300);
  assert.ok(tiny.zoom <= model.MAX_ZOOM);
  const huge = model.focusBounds({x: 0, y: 0, width: model.WORLD_WIDTH, height: model.WORLD_HEIGHT}, 800, 500);
  assert.ok(Math.abs(huge.zoom - worldZoom) < 1e-9);
});

test('project territories and their room nodes use distinct default colors and persisted overrides', () => {
  const multi = rooms.slice(0, 20).map((room, i) => ({...room, projectId: `p${i}`}));
  const projects = multi.map((room, i) => ({id: room.projectId, name: `Project ${i}`, node: 'local',
    mapColor: i === 3 ? '#1234AB' : ''}));
  const nodes = model.build(multi, [], bots, projects);
  const territories = model.territories(nodes, projects).filter(t => t.kind === 'project');
  assert.equal(territories.length, 20);
  assert.equal(new Set(territories.map(t => t.color)).size, 20);
  for (const node of nodes) assert.equal(node.projectColor, territories.find(t => t.id === node.projectId).color);
  assert.equal(territories.find(t => t.id === 'p3').color, '#1234AB');
  const reordered = model.territories(model.build([...multi].reverse(), [], bots, [...projects].reverse()), [...projects].reverse());
  for (const t of territories) assert.equal(reordered.find(r => r.id === t.id).color, t.color);
});

test('overview opens on every project territory instead of one working room', () => {
  const areas = [
    {kind: 'host', x: 180, y: 120, width: 490, height: 350},
    {kind: 'project', x: 230, y: 175, width: 360, height: 220},
    {kind: 'host', x: 3020, y: 2350, width: 610, height: 470},
    {kind: 'project', x: 3100, y: 2440, width: 440, height: 260},
  ];
  const view = model.overview(areas, 1000, 650);
  assert.ok(view.zoom < 1);
  for (const area of areas.filter(a => a.kind === 'project')) {
    assert.ok(area.x * view.zoom >= view.x - 1);
    assert.ok(area.y * view.zoom >= view.y - 1);
    assert.ok((area.x + area.width) * view.zoom <= view.x + 1001);
    assert.ok((area.y + area.height) * view.zoom <= view.y + 651);
  }
  assert.deepEqual({...model.overview([], 1000, 650)}, {...model.fit(1000, 650)});
});

test('territory focus returns pixel offsets at zoom rather than unscaled world offsets', () => {
  const t = {x: 1500, y: 1200, width: 400, height: 240};
  const v = model.focusBounds(t, 800, 500);
  assert.ok(Math.abs((v.x + 400) / v.zoom - (t.x + t.width / 2)) < 1);
  assert.ok(Math.abs((v.y + 250) / v.zoom - (t.y + t.height / 2)) < 1);
});

test('room-scoped external activity is working only when timestamp is fresh', () => {
  const now = 1_800_000_000_000;
  const active = {...rooms[0], externalActivity: 'tool running: terminal', externalActivityAt: now / 1000 - 30};
  const stale = {...rooms[1], externalActivity: 'old prompt', externalActivityAt: now / 1000 - 600};
  const missing = {...rooms[2], externalActivity: 'unknown time'};
  const nodes = model.build([active, stale, missing], [], bots, projects, now);
  assert.deepEqual(Array.from(nodes, n => n.activity), ['working', 'idle', 'idle']);
  assert.equal(model.focusTarget(nodes).roomId, active.id);
  assert.equal(model.build([active], [], bots, projects, now + 360_000)[0].activity, 'idle');
  assert.equal(model.build([{...active, source: 'other'}], [], bots, projects, now)[0].activity, 'idle');
  assert.equal(model.build([active, stale], [{roomId: stale.id, status: 'waiting'}], bots, projects, now)[1].activity, 'waiting');
});

test('dense fallback is a separate unassigned bucket with activity summary and zoom-gated cards', () => {
  const ps = [{id: 'real', name: 'Real', node: 'local', path: '/home/dev/real'},
    {id: 'fallback', name: 'Hermes', node: 'peer', path: 'peer://peer/default'}];
  const rs = [...Array.from({length: 229}, (_, i) => ({...rooms[0], id: 'f-' + i, projectId: 'fallback'})),
    {...rooms[0], id: 'real-room', projectId: 'real'}];
  const ns = model.build(rs, [{roomId: 'f-0', status: 'working'}, {roomId: 'f-1', status: 'waiting'}], bots, ps);
  const areas = model.territories(ns, ps);
  const bucket = areas.find(a => a.kind === 'unassigned');
  assert.equal(bucket.name, 'Unassigned / Home');
  assert.equal(bucket.roomCount, 229);
  assert.equal(bucket.workingCount, 1);
  assert.equal(bucket.waitingCount, 1);
  assert.ok(bucket.y > areas.find(a => a.id === 'real').y);
  assert.equal(model.showRoomCards(0.2), false);
  assert.equal(model.showRoomCards(0.8), true);
  assert.equal(model.unassignedProject({path: 'peer://peer/home/user/work'}), false);
});


test('archived rooms join their exact projects only when supplied and remain read-only nodes', () => {
  const archived = [{...rooms[1], id: 'old', status: 'deleted', projectId: 'project', controlIncluded: false},
    {...rooms[2], id: 'other', status: 'deleted', projectId: 'other', controlIncluded: false}];
  const active = build([rooms[0]]);
  assert.deepEqual(Array.from(active, n => n.roomId), ['room-0']);
  const all = model.build([rooms[0]], [], bots, [...projects, {id: 'other', name: 'Other'}], 1800000000000, archived, true);
  assert.deepEqual(Array.from(all, n => [n.roomId, n.archived]).sort((a, b) => a[0].localeCompare(b[0])),
    [['old', true], ['other', true], ['room-0', false]]);
  assert.equal(model.territories(all, [...projects, {id: 'other', name: 'Other'}]).find(t => t.id === 'project').roomCount, 2);
  assert.equal(all.find(n => n.roomId === 'old').activity, 'idle');
});

test('source-archived sessions disappear by default and return as archive cards only on demand', () => {
  const sourceArchived = {...rooms[0], externalArchived: true};
  const active = {...rooms[1], externalArchived: false};
  const now = 1800000000000;
  const regular = model.build([sourceArchived, active], [], bots, projects, now);
  assert.deepEqual(Array.from(regular, n => n.roomId), [active.id]);
  const revealed = model.build([sourceArchived, active], [], bots, projects, now, [], true);
  assert.equal(revealed.length, 2);
  assert.equal(revealed.find(n => n.roomId === sourceArchived.id).archived, true);
  assert.equal(revealed.find(n => n.roomId === sourceArchived.id).sourceArchived, true);
  assert.equal(revealed.find(n => n.roomId === sourceArchived.id).activity, 'idle');
  assert.equal(revealed.find(n => n.roomId === active.id).archived, false);
  assert.equal(regular.length, 1);
});

test('overview creates zero cards, while a zoomed viewport creates only intersecting cards', () => {
  const nodes = Array.from({length: 1100}, (_, i) => ({roomId: String(i), x: (i % 40) * 192, y: Math.floor(i / 40) * 106}));
  assert.equal(model.visibleNodes(nodes, .54, 0, 0, 800, 600, '').length, 0);
  const visible = model.visibleNodes(nodes, 1, 1920, 1060, 400, 220, '');
  assert.ok(visible.length > 0 && visible.length <= 80);
  for (const n of visible) {
    assert.ok(n.x < 2320 && n.x + model.NODE_WIDTH > 1920);
    assert.ok(n.y < 1280 && n.y + model.NODE_HEIGHT > 1060);
  }
  assert.notDeepEqual(Array.from(visible, n => n.roomId), Array.from(model.visibleNodes(nodes, 1, 0, 0, 400, 220, ''), n => n.roomId));
});

test('dense visible viewport remains capped and prioritizes its selected room', () => {
  const nodes = Array.from({length: 300}, (_, i) => ({roomId: String(i), x: 100, y: 100}));
  const visible = model.visibleNodes(nodes, 1, 0, 0, 800, 600, '299');
  assert.equal(visible.length, 80);
  assert.ok(visible.some(n => n.roomId === '299'));
  assert.equal(model.visibleNodes(nodes, 1, 2000, 0, 800, 600, '299').length, 0);
});

test('saved block position moves every room and its territory, while other projects avoid it', () => {
  const ps = [{id: 'p1', name: 'A', mapX: 1300, mapY: 900}, {id: 'p2', name: 'B'}];
  const rs = [{...rooms[0], projectId: 'p1'}, {...rooms[1], projectId: 'p1'}, {...rooms[2], projectId: 'p2'}];
  const nodes = model.build(rs, [], bots, ps);
  const areas = model.territories(nodes, ps);
  const a = areas.find(t => t.id === 'p1'), b = areas.find(t => t.id === 'p2');
  assert.equal(a.x, 1300); assert.equal(a.y, 900);
  assert.ok(nodes.filter(n => n.projectId === 'p1').every(n => n.x >= a.x && n.y >= a.y && n.x + model.NODE_WIDTH <= a.x + a.width && n.y + model.NODE_HEIGHT <= a.y + a.height));
  assert.ok(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y);
  const moved = model.build(rs, [], bots, [{...ps[0], mapX: 4300, mapY: 3400}, ps[1]]);
  const movedArea = model.territories(moved, ps).find(t => t.id === 'p1');
  assert.ok(movedArea.x + movedArea.width <= model.WORLD_WIDTH && movedArea.y + movedArea.height <= model.WORLD_HEIGHT);
  assert.ok(movedArea.x >= 0 && movedArea.y >= 0);
  const before = nodes.filter(n => n.projectId === 'p1');
  const after = moved.filter(n => n.projectId === 'p1');
  assert.equal(after[1].x - after[0].x, before[1].x - before[0].x);
  assert.equal(after[1].y - after[0].y, before[1].y - before[0].y);
});

test('saved placements take priority over automatic projects and resolve collisions deterministically', () => {
  const ps = [{id:'a', name:'A', mapX: 0, mapY: 0}, {id:'b', name:'B', mapX: 0, mapY: 0},
    {id:'c', name:'C'}, {id:'d', name:'D'}];
  const rs = ps.map((p, i) => ({...rooms[i], projectId:p.id}));
  const areas = model.territories(model.build(rs, [], bots, ps), ps);
  assert.equal(areas.find(t => t.id === 'a').x, 0);
  assert.equal(areas.find(t => t.id === 'a').y, 0);
  for (const t of areas) {
    assert.ok(t.x >= 0 && t.y >= 0 && t.x + t.width <= model.WORLD_WIDTH && t.y + t.height <= model.WORLD_HEIGHT, t.id);
    for (const other of areas.filter(o => o.id !== t.id))
      assert.ok(t.x + t.width <= other.x || other.x + other.width <= t.x ||
        t.y + t.height <= other.y || other.y + other.height <= t.y, `${t.id} overlaps ${other.id}`);
  }
  const reordered = model.territories(model.build([...rs].reverse(), [], bots, [...ps].reverse()), ps);
  for (const t of areas) assert.deepEqual([t.x, t.y], [reordered.find(o => o.id === t.id).x, reordered.find(o => o.id === t.id).y]);
});

test('realistic dense project blocks fit the world without overlaps', () => {
  const counts = [350, 345, 164, 51, 37, 33, 26, 23, 21, 19, 16, 15, 12, 9, 9, 8,
    7, 6, 5, 5, 4, 4, 3, 3, 2, 2, ...Array(15).fill(1)];
  const ps = counts.map((_, i) => ({id: `dense-${i}`, name: `Block ${i}`, path: `/synthetic/${i}`}));
  const rs = counts.flatMap((count, i) => Array.from({length: count}, (_, j) =>
    ({...rooms[0], id: `dense-${i}-${j}`, projectId: ps[i].id})));
  const areas = model.territories(model.build(rs, [], bots, ps), ps);
  assert.equal(areas.length, counts.length);
  for (let i = 0; i < areas.length; ++i) {
    const a = areas[i];
    assert.ok(a.x >= 0 && a.y >= 0 && a.x + a.width <= model.WORLD_WIDTH &&
      a.y + a.height <= model.WORLD_HEIGHT, `block ${i} outside world`);
    for (let j = i + 1; j < areas.length; ++j) {
      const b = areas[j];
      assert.ok(a.x + a.width <= b.x || b.x + b.width <= a.x ||
        a.y + a.height <= b.y || b.y + b.height <= a.y, `blocks ${i}/${j} overlap`);
    }
  }
});


test('local archives are absent by default and reappear only with explicit map toggle', () => {
  const old = {...rooms[0], id: 'recovery', status: 'deleted', controlIncluded: false};
  const regular = model.build([rooms[1]], [], bots, projects, 1800000000000, [old], false);
  assert.deepEqual(Array.from(regular, n => n.roomId), [rooms[1].id]);
  const revealed = model.build([rooms[1]], [], bots, projects, 1800000000000, [old], true);
  assert.deepEqual(Array.from(revealed, n => n.roomId).sort(), ['recovery', rooms[1].id].sort());
  assert.equal(revealed.find(n => n.roomId === old.id).archived, true);
});
