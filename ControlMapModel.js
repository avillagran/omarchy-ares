var NODE_WIDTH = 192;
var NODE_HEIGHT = 106;
var WORLD_WIDTH = 9600;
var WORLD_HEIGHT = 7200;
var MIN_ZOOM = 0.03;
var MAX_ZOOM = 2.5;

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function zoomAt(previous, factor, pointerX, pointerY, viewportWidth, viewportHeight) {
  var zoom = clamp(previous.zoom * factor, MIN_ZOOM, MAX_ZOOM);
  return {zoom: zoom,
    x: clamp((previous.x + pointerX) * zoom / previous.zoom - pointerX,
      0, Math.max(0, WORLD_WIDTH * zoom - viewportWidth)),
    y: clamp((previous.y + pointerY) * zoom / previous.zoom - pointerY,
      0, Math.max(0, WORLD_HEIGHT * zoom - viewportHeight))};
}

function fit(viewportWidth, viewportHeight) {
  return {zoom: clamp(Math.min(viewportWidth / WORLD_WIDTH, viewportHeight / WORLD_HEIGHT), MIN_ZOOM, MAX_ZOOM), x: 0, y: 0};
}

function reset(viewportWidth, viewportHeight, areas) {
  return overview(areas || [], viewportWidth, viewportHeight);
}

function focusTarget(nodes) {
  return nodes.find(function(node) { return node.activity === "waiting"; })
    || nodes.find(function(node) { return node.activity === "working"; }) || null;
}

function focus(nodes, viewportWidth, viewportHeight) {
  var target = focusTarget(nodes);
  if (!target) return null;
  return {zoom: 1,
    x: clamp(target.x + NODE_WIDTH / 2 - viewportWidth / 2, 0, Math.max(0, WORLD_WIDTH - viewportWidth)),
    y: clamp(target.y + NODE_HEIGHT / 2 - viewportHeight / 2, 0, Math.max(0, WORLD_HEIGHT - viewportHeight))};
}

function focusBounds(t, viewportWidth, viewportHeight) {
  var worldZoom = Math.min(viewportWidth / WORLD_WIDTH, viewportHeight / WORLD_HEIGHT);
  var zoom = clamp(Math.min(viewportWidth / t.width, viewportHeight / t.height) * 0.82,
    Math.max(MIN_ZOOM, worldZoom), MAX_ZOOM);
  return {zoom: zoom,
    x: clamp((t.x + t.width / 2) * zoom - viewportWidth / 2, 0, Math.max(0, WORLD_WIDTH * zoom - viewportWidth)),
    y: clamp((t.y + t.height / 2) * zoom - viewportHeight / 2, 0, Math.max(0, WORLD_HEIGHT * zoom - viewportHeight))};
}

function overview(areas, viewportWidth, viewportHeight) {
  if (!areas.length) return fit(viewportWidth, viewportHeight);
  var left = WORLD_WIDTH, top = WORLD_HEIGHT, right = 0, bottom = 0;
  for (var i = 0; i < areas.length; ++i) {
    left = Math.min(left, areas[i].x);
    top = Math.min(top, areas[i].y);
    right = Math.max(right, areas[i].x + areas[i].width);
    bottom = Math.max(bottom, areas[i].y + areas[i].height);
  }
  return focusBounds({x: left, y: top, width: right - left, height: bottom - top},
    viewportWidth, viewportHeight);
}

function hash(value) {
  var result = 2166136261;
  for (var i = 0; i < value.length; ++i) {
    result ^= value.charCodeAt(i);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function generatedColor(index) {
  var hue = (index * 137.508 + 12) % 360 / 360;
  var saturation = 0.72, lightness = 0.60;
  function channel(offset) {
    var k = (offset + hue * 12) % 12;
    var chroma = saturation * Math.min(lightness, 1 - lightness);
    return Math.round(255 * (lightness - chroma * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
  }
  return '#' + [channel(0), channel(8), channel(4)].map(function(c) { return c.toString(16).padStart(2, '0'); }).join('').toUpperCase();
}

function projectColors(ids, projectById) {
  var colors = Object.create(null), used = Object.create(null);
  var ordered = ids.slice().sort();
  for (var i = 0; i < ordered.length; ++i) {
    var override = String((projectById[ordered[i]] || {}).mapColor || '').toUpperCase();
    if (/^#[0-9A-F]{6}$/.test(override) && !used[override]) {
      colors[ordered[i]] = override;
      used[override] = true;
    }
  }
  var candidate = 0;
  for (var j = 0; j < ordered.length; ++j) {
    if (colors[ordered[j]]) continue;
    var color;
    do { color = generatedColor(candidate++); } while (used[color]);
    colors[ordered[j]] = color;
    used[color] = true;
  }
  return colors;
}

function freshExternalActivity(room, nowMs) {
  var at = Number(room.externalActivityAt);
  var age = nowMs - at * 1000; // Ares timestamps are epoch seconds.
  return room.source === "hermes" && String(room.externalActivity || "").trim() !== ""
    && Number.isFinite(at) && at > 0 && age >= 0 && age <= 5 * 60 * 1000;
}

function unassignedProject(project) {
  return /^peer:\/\/[^/]+\/[^/]+$/.test(String((project || {}).path || ""));
}

function showRoomCards(zoom) { return zoom >= 0.55; }

// The map keeps every node for territories and counts, but only creates card delegates
// for the intersecting viewport. Never create cards for an overview.
function visibleNodes(nodes, zoom, contentX, contentY, viewportWidth, viewportHeight, selectedId) {
  if (!showRoomCards(zoom) || !Number.isFinite(zoom) || zoom <= 0 ||
      !Number.isFinite(viewportWidth) || !Number.isFinite(viewportHeight) ||
      viewportWidth <= 0 || viewportHeight <= 0) return [];
  var left = contentX / zoom, top = contentY / zoom;
  var right = (contentX + viewportWidth) / zoom, bottom = (contentY + viewportHeight) / zoom;
  var limit = 80, result = [], selected = null;
  for (var i = 0; i < nodes.length; ++i) {
    var node = nodes[i];
    if (node.x >= right || node.y >= bottom || node.x + NODE_WIDTH <= left || node.y + NODE_HEIGHT <= top) continue;
    if (node.roomId === selectedId) selected = node;
    if (result.length < limit) result.push(node);
  }
  if (selected && result.indexOf(selected) < 0) result[result.length - 1] = selected;
  return result;
}

function build(rooms, items, bots, projects, nowMs, archivedRooms, includeSourceArchived) {
  if (nowMs === undefined) nowMs = Date.now();
  var nodes = [];
  var botById = Object.create(null);
  var projectById = Object.create(null);
  var activityByRoom = Object.create(null);
  for (var b = 0; b < bots.length; ++b) botById[bots[b].id] = bots[b];
  for (var p = 0; p < projects.length; ++p) projectById[projects[p].id] = projects[p];
  for (var a = 0; a < items.length; ++a) {
    var item = items[a];
    var id = String(item.roomId || "");
    if (!id) continue;
    var previous = activityByRoom[id] || {activity: "idle", parentWorking: false};
    var working = item.status === "working" || item.status === "running" || item.status === "queued";
    var waiting = item.status === "waiting" || item.status === "blocked" || item.status === "needs_input";
    if (working && previous.activity !== "waiting") previous.activity = "working";
    if (waiting) previous.activity = "waiting";
    if (item.status === "working" && item.kind === "delegation" && Number(item.delegationUnits) > 0)
      previous.parentWorking = true;
    activityByRoom[id] = previous;
  }
  var eligible = rooms.filter(function(room) {
    return room && room.id && room.status === "active" && room.source !== "control" &&
      room.controlIncluded === true && (includeSourceArchived === true || room.externalArchived !== true);
  });
  var archivedIds = Object.create(null), includedIds = Object.create(null);
  for (var ai = 0; ai < eligible.length; ++ai) {
    includedIds[String(eligible[ai].id)] = true;
    if (eligible[ai].externalArchived === true) archivedIds[String(eligible[ai].id)] = true;
  }
  // Source-archived rooms are active Ares records with a verified archive flag;
  // locally archived rooms come only from the explicit archive catalog. Neither
  // changes stored status or membership.
  var archive = includeSourceArchived === true ? (archivedRooms || []) : [];
  for (var ar = 0; ar < archive.length; ++ar) {
    var archived = archive[ar];
    if (!archived || !archived.id || archived.status === "active" || archived.source === "control" || includedIds[String(archived.id)]) continue;
    archivedIds[String(archived.id)] = true;
    includedIds[String(archived.id)] = true;
    eligible.push(archived);
  }
  eligible.sort(function(a, b) { return String(a.id).localeCompare(String(b.id)); });
  // Projects are peers on the map; host is metadata, never a parent territory.
  var byProject = Object.create(null);
  var projectIds = [];
  for (var e = 0; e < eligible.length; ++e) {
    var pid = String(eligible[e].projectId || "");
    if (!byProject[pid]) { byProject[pid] = []; projectIds.push(pid); }
    byProject[pid].push(eligible[e]);
  }
  function metaOf(pid) {
    var project = projectById[pid] || {};
    return {host: String(project.node || "unknown"), name: String(project.name || "")};
  }
  projectIds.sort(function(a, b) {
    var ua = unassignedProject(projectById[a]), ub = unassignedProject(projectById[b]);
    if (ua !== ub) return ua ? 1 : -1;
    var ma = metaOf(a), mb = metaOf(b);
    if (ma.name !== mb.name) return ma.name < mb.name ? -1 : 1;
    if (ma.host !== mb.host) return ma.host < mb.host ? -1 : 1;
    return a < b ? -1 : a > b ? 1 : 0;
  });
  var colors = projectColors(projectIds, projectById);
  var PAD = 48, HEADER = 64, GUTTER = 40, INNER = 28;
  var blocks = Object.create(null), occupied = [];
  // Reserve persisted blocks before fitting automatic projects around them.
  var placementOrder = projectIds.slice().sort(function(a, b) {
    var ap = projectById[a] || {}, bp = projectById[b] || {};
    var as = Number.isInteger(ap.mapX) && Number.isInteger(ap.mapY);
    var bs = Number.isInteger(bp.mapX) && Number.isInteger(bp.mapY);
    return as === bs ? projectIds.indexOf(a) - projectIds.indexOf(b) : as ? -1 : 1;
  });
  function intersects(a, b) {
    return a.x < b.x + b.w + GUTTER && b.x < a.x + a.w + GUTTER &&
      a.y < b.y + b.h + GUTTER && b.y < a.y + a.h + GUTTER;
  }
  function candidates(desired, size, axis, bound) {
    var values = [0, PAD, desired, Math.max(0, bound - size)];
    for (var i = 0; i < occupied.length; ++i) {
      var other = occupied[i], start = other[axis], extent = axis === 'x' ? other.w : other.h;
      values.push(start + extent + GUTTER, start - size - GUTTER);
    }
    return Array.from(new Set(values.map(function(v) {
      return clamp(Math.ceil(v), 0, Math.max(0, bound - size));
    })));
  }
  for (var pi = 0; pi < placementOrder.length; ++pi) {
    var projectId = placementOrder[pi], project = projectById[projectId] || {};
    var count = byProject[projectId].length;
    var cols = clamp(Math.max(Math.ceil(Math.sqrt(count)), Math.ceil(count / 31)), 1, 8);
    var rows = Math.ceil(count / cols);
    var blockW = Math.max(NODE_WIDTH * 2.2, INNER * 2 + cols * NODE_WIDTH);
    var blockH = Math.max(NODE_HEIGHT * 1.9, HEADER + INNER * 2 + rows * NODE_HEIGHT);
    var saved = Number.isInteger(project.mapX) && Number.isInteger(project.mapY) &&
      project.mapX >= 0 && project.mapY >= 0 && project.mapX <= WORLD_WIDTH && project.mapY <= WORLD_HEIGHT;
    var desiredX = saved ? clamp(project.mapX, 0, Math.max(0, WORLD_WIDTH - blockW)) : PAD;
    var desiredY = saved ? clamp(project.mapY, 0, Math.max(0, WORLD_HEIGHT - blockH))
      : unassignedProject(project) && occupied.length
        ? Math.min(Math.max(0, WORLD_HEIGHT - blockH),
          Math.max.apply(null, occupied.map(function(b) { return b.y + b.h + GUTTER; }))) : PAD;
    var xs = candidates(desiredX, blockW, 'x', WORLD_WIDTH);
    var ys = candidates(desiredY, blockH, 'y', WORLD_HEIGHT);
    var possible = [];
    for (var yi = 0; yi < ys.length; ++yi)
      for (var xi = 0; xi < xs.length; ++xi) {
        var candidate = {x: xs[xi], y: ys[yi], w: blockW, h: blockH};
        if (!occupied.some(function(other) { return intersects(candidate, other); }))
          possible.push(candidate);
      }
    if (!possible.length) {
      // Never draw two project territories on top of each other: the canvas
      // must be enlarged if the room density exceeds its capacity.
      throw new Error('Control Map world is too small for project blocks');
    }
    possible.sort(function(a, b) {
      var costA = saved ? Math.abs(a.x - desiredX) + Math.abs(a.y - desiredY)
        : (unassignedProject(project) && a.y < desiredY ? WORLD_HEIGHT : 0) + a.y;
      var costB = saved ? Math.abs(b.x - desiredX) + Math.abs(b.y - desiredY)
        : (unassignedProject(project) && b.y < desiredY ? WORLD_HEIGHT : 0) + b.y;
      return costA - costB || a.y - b.y || a.x - b.x;
    });
    var pos = possible[0];
    blocks[projectId] = {x: pos.x, y: pos.y, cols: cols};
    occupied.push(pos);
  }
  for (var ni = 0; ni < projectIds.length; ++ni) {
    var pid = projectIds[ni], meta = metaOf(pid), block = blocks[pid], members = byProject[pid];
    for (var mi = 0; mi < members.length; ++mi) {
      nodes.push(makeNode(members[mi], pid, meta.host,
        block.x + INNER + (mi % block.cols) * NODE_WIDTH,
        block.y + HEADER + INNER + Math.floor(mi / block.cols) * NODE_HEIGHT));
    }
  }
  return nodes;

  function makeNode(room, projectId, host, x, y) {
    var participants = Array.isArray(room.participants) ? room.participants : [];
    var distinct = [];
    for (var n = 0; n < participants.length; ++n)
      if (participants[n] && distinct.indexOf(String(participants[n])) < 0) distinct.push(String(participants[n]));
    var botId = distinct.length ? distinct[0] : "";
    var bot = botById[botId] || {};
    var project = projectById[room.projectId] || {};
    var state = archivedIds[String(room.id)] ? {activity: "idle", parentWorking: false}
      : (activityByRoom[room.id] || {activity: "idle", parentWorking: false});
    var activity = !archivedIds[String(room.id)] && state.activity === "idle" && freshExternalActivity(room, nowMs) ? "working" : state.activity;
    var shape = String(bot.faceShape || "diamond");
    if (shape === "circle") shape = "oval";
    if (shape === "hexagon") shape = "square";
    return {roomId: String(room.id), archived: !!archivedIds[String(room.id)],
      sourceArchived: room.externalArchived === true && room.status === "active",
      roomName: String(room.name || ""),
      projectName: String(project.name || ""), botId: botId,
      botName: String(bot.name || botId), shape: shape,
      colorRole: String(bot.colorRole || "accent"), additionalMembers: Math.max(0, distinct.length - 1),
      activity: activity, parentWorking: activity === "working" && state.parentWorking,
      projectId: projectId, projectColor: colors[projectId], host: host, x: x, y: y};
  }
}

function territories(nodes, projects) {
  var projectById = Object.create(null);
  for (var p = 0; p < projects.length; ++p) projectById[projects[p].id] = projects[p];
  function metaOf(pid) {
    var project = projectById[pid] || {};
    return {host: String(project.node || "unknown"), name: String(project.name || "")};
  }
  var boxes = Object.create(null);
  for (var i = 0; i < nodes.length; ++i) {
    var node = nodes[i];
    var pid = String(node.projectId || "");
    var box = boxes[pid];
    if (!box) box = boxes[pid] = {x1: node.x, y1: node.y, x2: node.x + NODE_WIDTH, y2: node.y + NODE_HEIGHT, roomCount: 0, workingCount: 0, waitingCount: 0};
    else {
      box.x1 = Math.min(box.x1, node.x); box.y1 = Math.min(box.y1, node.y);
      box.x2 = Math.max(box.x2, node.x + NODE_WIDTH); box.y2 = Math.max(box.y2, node.y + NODE_HEIGHT);
    }
    box.roomCount++;
    if (node.activity === "working") box.workingCount++;
    if (node.activity === "waiting") box.waitingCount++;
  }
  var INNER = 28, HEADER = 64, MIN_W = NODE_WIDTH * 2.2, MIN_H = NODE_HEIGHT * 1.9;
  var pros = [];
  var ids = Object.keys(boxes).sort(function(a, b) {
    var ma = metaOf(a), mb = metaOf(b);
    if (ma.name !== mb.name) return ma.name < mb.name ? -1 : 1;
    if (ma.host !== mb.host) return ma.host < mb.host ? -1 : 1;
    return a < b ? -1 : a > b ? 1 : 0;
  });
  var colors = projectColors(ids, projectById);
  for (var k = 0; k < ids.length; ++k) {
    var id = ids[k];
    var meta = metaOf(id);
    var b = boxes[id];
    var w = Math.max(b.x2 - b.x1 + INNER * 2, MIN_W);
    var h = Math.max(b.y2 - b.y1 + HEADER + INNER * 2, MIN_H);
    var x = clamp(b.x1 - INNER, 0, Math.max(0, WORLD_WIDTH - w));
    var y = clamp(b.y1 - HEADER - INNER, 0, Math.max(0, WORLD_HEIGHT - h));
    var unassigned = unassignedProject(projectById[id]);
    var t = {id: id || "unknown", kind: unassigned ? "unassigned" : "project",
      name: unassigned ? "Unassigned / Home" : (meta.name || "Unknown project"), node: meta.host,
      parentId: "", color: colors[id], x: x, y: y, width: w, height: h,
      roomCount: b.roomCount, workingCount: b.workingCount, waitingCount: b.waitingCount};
    pros.push(t);
  }
  return pros;
}
