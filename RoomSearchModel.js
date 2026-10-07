// Normalize once per inventory update, never once per room per keystroke.
function normalize(value) {
  return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

function activityAt(room) {
  var timestamp = Number(room && (room.externalActivityAt || room.externalUpdatedAt || room.updatedAt || room.createdAt || 0));
  return isFinite(timestamp) ? timestamp : 0;
}

function activityLabel(room, now) {
  var current = Number(now);
  if (!isFinite(current) || current <= 0) current = Date.now() / 1000;
  var seconds = Math.max(0, Math.floor(current - activityAt(room)));
  if (seconds < 60) return "now";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
  if (seconds < 604800) return Math.floor(seconds / 86400) + "d ago";
  return Math.floor(seconds / 604800) + "w ago";
}

function build(rooms, projects) {
  var byProject = Object.create(null);
  var seen = Object.create(null);
  projects.forEach(function(project) { byProject[project.id] = project; });
  var entries = [];
  rooms.slice().sort(function(a, b) {
    return activityAt(b) - activityAt(a);
  }).forEach(function(room) {
    var project = byProject[room.projectId];
    if (!project || !room.id || room.source === "control" || seen[room.id]) return;
    seen[room.id] = true;
    entries.push({
      text: normalize(room.alias) + "\n" + normalize(room.name),
      row: { kind: "room", project: project, room: room, direct: true, recent: true, depth: 0 }
    });
  });
  return entries;
}

function filter(entries, query) {
  var tokens = normalize(query).trim().split(/\s+/).filter(function(token) { return token !== ""; });
  var rows = [];
  for (var i = 0; i < entries.length; ++i) {
    var entry = entries[i];
    if (tokens.every(function(token) { return entry.text.indexOf(token) !== -1; })) rows.push(entry.row);
  }
  return rows;
}

function move(index, delta, count) {
  return count === 0 ? -1 : Math.max(0, Math.min(count - 1, index + delta));
}

function preserve(rows, roomId) {
  for (var i = 0; i < rows.length; ++i) {
    if (rows[i].room.id === roomId) return i;
  }
  return rows.length ? 0 : -1;
}
