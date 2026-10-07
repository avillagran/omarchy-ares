// Project identity is the only tile key; hosts are metadata, not containers.
var TILES_PER_WORKSPACE = 4;
var CELL_WIDTH = 320;
var CELL_HEIGHT = 230;

function order(projects, savedIds) {
  var byId = Object.create(null);
  (projects || []).forEach(function(project) { if (project && project.id) byId[project.id] = project; });
  var remaining = Object.keys(byId).sort(function(a, b) {
    var ap = byId[a], bp = byId[b];
    var af = /^peer:\/\/[^/]+\/[^/]+$/.test(String(ap.path || ''));
    var bf = /^peer:\/\/[^/]+\/[^/]+$/.test(String(bp.path || ''));
    if (af !== bf) return af ? 1 : -1;
    var an = String(ap.name || ''), bn = String(bp.name || '');
    return an.localeCompare(bn) || a.localeCompare(b);
  });
  var ids = [], seen = Object.create(null);
  (Array.isArray(savedIds) ? savedIds : []).concat(remaining).forEach(function(id) {
    if (Object.prototype.hasOwnProperty.call(byId, id) && !seen[id]) {
      seen[id] = true;
      ids.push(id);
    }
  });
  return ids.map(function(id) { return byId[id]; });
}
function zoomLevel(zoom, fitScale) {
  if (fitScale > 0) {
    if (zoom <= fitScale * 1.12 || CELL_WIDTH * zoom < 180) return 'overview';
    return CELL_WIDTH * zoom < 500 ? 'normal' : 'detail';
  }
  return zoom < 0.85 ? 'overview' : zoom > 1.3 ? 'detail' : 'normal';
}
function capacityForZoom(zoom, narrow) {
  var level = zoomLevel(zoom);
  return level === 'detail' ? 1 : level === 'overview' ? (narrow ? 4 : 8) : (narrow ? 2 : 4);
}
function capacity(limit) { return limit === 1 || limit === 2 || limit === 8 ? limit : TILES_PER_WORKSPACE; }
function pageCount(projects, limit) { return Math.max(1, Math.ceil((projects || []).length / capacity(limit))); }
function page(projects, index, limit) {
  var perPage = capacity(limit);
  var n = Math.max(0, Math.min(pageCount(projects, perPage) - 1, Math.floor(Number(index) || 0)));
  return (projects || []).slice(n * perPage, (n + 1) * perPage);
}
function pageForProject(projects, projectId, limit) {
  var index = (projects || []).findIndex(function(p) { return p.id === projectId; });
  return index < 0 ? 0 : Math.floor(index / capacity(limit));
}
// The same alternating binary split as a small dwindle workspace.
function rectangles(count, mode) {
  var n = Math.max(0, Math.min(mode === 'overview' ? 8 : TILES_PER_WORKSPACE, Math.floor(Number(count) || 0)));
  if (mode === 'overview') {
    var columns = n > 4 ? 4 : 2;
    var rows = Math.max(1, Math.ceil(n / columns));
    var overview = [];
    for (var row = 0; row < rows; row++) {
      var rowCount = Math.min(columns, n - row * columns);
      for (var col = 0; col < rowCount; col++) {
        overview.push({x:col / rowCount, y:row / rows, width:1 / rowCount, height:1 / rows});
      }
    }
    return overview;
  }
  var rects = [], remaining = {x:0, y:0, width:1, height:1};
  for (var i=0; i<n; i++) {
    if (i===n-1) { rects.push(remaining); break; }
    if (i % 2 === 0) {
      rects.push({x:remaining.x,y:remaining.y,width:remaining.width/2,height:remaining.height});
      remaining = {x:remaining.x+remaining.width/2,y:remaining.y,width:remaining.width/2,height:remaining.height};
    } else {
      rects.push({x:remaining.x,y:remaining.y,width:remaining.width,height:remaining.height/2});
      remaining = {x:remaining.x,y:remaining.y+remaining.height/2,width:remaining.width,height:remaining.height/2};
    }
  }
  return rects;
}
function swap(ids, first, second) {
  var next = (ids || []).slice();
  var a = next.indexOf(first), b = next.indexOf(second);
  if (a >= 0 && b >= 0 && a !== b) { next[a] = second; next[b] = first; }
  return next;
}
// Every project has a stable spatial slot. Zoom only changes the camera and detail.
function mapLayout(projects, viewportWidth, viewportHeight, categories, collapsedGroupIds) {
  var items = projects || [];
  var count = Math.max(1, items.length);
  var ratio = Math.max(1, Number(viewportWidth) || 1) / Math.max(1, Number(viewportHeight) || 1);
  var columns = Math.max(1, Math.ceil(Math.sqrt(count * ratio * CELL_HEIGHT / CELL_WIDTH)));
  var configured = categories && Array.isArray(categories.groups) ? categories.groups : [];
  if (configured.length) {
    var buckets = Object.create(null), assignments = categories.assignments || {};
    var groups = [], slots = [], seen = Object.create(null);
    var collapsed = collapsedGroupIds || {};
    configured.forEach(function(group) {
      if (!group || !group.id || seen[group.id]) return;
      seen[group.id] = true;
      buckets[group.id] = [];
      groups.push({id:group.id, name:group.name, color:group.color, collapsed:collapsed[group.id] === true});
    });
    buckets[''] = [];
    items.forEach(function(project) {
      var assigned = assignments[project.id];
      var id = Object.prototype.hasOwnProperty.call(buckets, assigned) ? assigned : '';
      buckets[id].push(project);
    });
    if (buckets[''].length) groups.push({id:'', name:'Unassigned', color:'#808080', collapsed:collapsed[''] === true});
    var top = 0, header = 62, gap = 28;
    groups.forEach(function(group) {
      var members = buckets[group.id], rows = Math.ceil(members.length / columns), collapsed = group.collapsed === true;
      var bounds = {id:group.id, name:group.name, color:group.color,
        x:0, y:top, width:columns * CELL_WIDTH, height:header + (collapsed ? 0 : rows * CELL_HEIGHT),
        count:members.length, collapsed: collapsed};
      if (!collapsed) members.forEach(function(project, index) {
          slots.push({project:project, index:slots.length, groupId:group.id,
            x:(index % columns)*CELL_WIDTH, y:top+header+Math.floor(index/columns)*CELL_HEIGHT,
            width:CELL_WIDTH, height:CELL_HEIGHT});
      });
      groups[groups.indexOf(group)] = bounds;
      top += bounds.height + gap;
    });
    return {columns:columns, rows:Math.ceil(top / CELL_HEIGHT),
      width:columns * CELL_WIDTH, height:Math.max(1,top-gap), slots:slots, groups:groups};
  }
  var rows = Math.ceil(count / columns);
  var slots = items.map(function(project, index) {
    return {project:project, index:index, x:(index % columns) * CELL_WIDTH,
      y:Math.floor(index / columns) * CELL_HEIGHT, width:CELL_WIDTH, height:CELL_HEIGHT};
  });
  return {columns:columns, rows:rows, width:columns * CELL_WIDTH,
    height:rows * CELL_HEIGHT, slots:slots, groups:[]};
}
function fitZoom(layout, viewportWidth, viewportHeight) {
  return Math.min(Math.max(1, viewportWidth - 16) / layout.width,
    Math.max(1, viewportHeight - 16) / layout.height);
}
function zoomAt(view, factor, cursorX, cursorY, layout, viewportWidth, viewportHeight) {
  var oldZoom = Math.max(0.0001, Number(view.zoom) || 1);
  var minZoom = fitZoom(layout, viewportWidth, viewportHeight);
  var nextZoom = Math.max(minZoom, Math.min(4, oldZoom * factor));
  var oldInsetX = Math.max(0, (viewportWidth - layout.width * oldZoom) / 2);
  var oldInsetY = Math.max(0, (viewportHeight - layout.height * oldZoom) / 2);
  var worldX = ((Number(view.x) || 0) + cursorX - oldInsetX) / oldZoom;
  var worldY = ((Number(view.y) || 0) + cursorY - oldInsetY) / oldZoom;
  var nextInsetX = Math.max(0, (viewportWidth - layout.width * nextZoom) / 2);
  var nextInsetY = Math.max(0, (viewportHeight - layout.height * nextZoom) / 2);
  var maxX = Math.max(0, layout.width * nextZoom - viewportWidth);
  var maxY = Math.max(0, layout.height * nextZoom - viewportHeight);
  return {zoom:nextZoom,
    x:Math.max(0, Math.min(maxX, worldX * nextZoom + nextInsetX - cursorX)),
    y:Math.max(0, Math.min(maxY, worldY * nextZoom + nextInsetY - cursorY))};
}
function visibleTiles(layout, zoom, contentX, contentY, viewportWidth, viewportHeight) {
  if (!(zoom > 0)) return [];
  var insetX = Math.max(0, (viewportWidth - layout.width * zoom) / 2);
  var insetY = Math.max(0, (viewportHeight - layout.height * zoom) / 2);
  return layout.slots.filter(function(slot) {
    var x = slot.x * zoom + insetX - contentX;
    var y = slot.y * zoom + insetY - contentY;
    return x < viewportWidth && y < viewportHeight && x + slot.width * zoom > 0 && y + slot.height * zoom > 0;
  });
}
function slotAt(layout, worldX, worldY) {
  if (worldX < 0 || worldY < 0 || worldX >= layout.width || worldY >= layout.height) return null;
  if (layout.groups && layout.groups.length) {
    for (var i=0;i<layout.slots.length;i++) {
      var slot=layout.slots[i];
      if (worldX>=slot.x && worldX<slot.x+slot.width && worldY>=slot.y && worldY<slot.y+slot.height) return slot;
    }
    return null;
  }
  var column = Math.floor(worldX / CELL_WIDTH), row = Math.floor(worldY / CELL_HEIGHT);
  return layout.slots[row * layout.columns + column] || null;
}
function projectRooms(rooms, archivedRooms, projectId, showArchived) {
  var active = (rooms || []).filter(function(r) {
    return r.projectId === projectId && r.status === 'active' && r.controlIncluded === true &&
      r.source !== 'control' && (showArchived || r.externalArchived !== true);
  });
  var old = showArchived ? (archivedRooms || []).filter(function(r) { return r.projectId === projectId; }) : [];
  return active.concat(old);
}
