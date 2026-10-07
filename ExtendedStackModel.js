// Preserve the overview's user order within a project, never mix it into siblings.
function stack(current, rooms, order, slots) {
  if (!current) return [];
  var byId = Object.create(null), seen = Object.create(null);
  (rooms || []).forEach(function(room) {
    if (room && room.id && room.status === 'active' && room.source !== 'control'
        && room.externalArchived !== true) byId[room.id] = room;
  });
  var candidates = (order || []).filter(function(id) {
    if (!byId[id] || id === current.id || seen[id]) return false;
    seen[id] = true;
    return true;
  });
  var siblings = candidates.filter(function(id) {
    return current.projectId && byId[id].projectId === current.projectId;
  });
  if (siblings.length) return siblings;
  var layout = slots || [];
  var origin = layout.find(function(slot) { return slot.projectId === current.projectId; });
  if (!origin) return candidates;
  var cx = origin.x + origin.width / 2, cy = origin.y + origin.height / 2;
  var nearest = layout.filter(function(slot) { return slot.projectId !== current.projectId; });
  function distance(slot) {
    var dx = slot.x + slot.width / 2 - cx, dy = slot.y + slot.height / 2 - cy;
    return dx * dx + dy * dy;
  }
  nearest.sort(function(a, b) { return distance(a) - distance(b)
    || String(a.projectId).localeCompare(String(b.projectId)); });
  var result = [];
  nearest.forEach(function(slot) {
    candidates.forEach(function(id) {
      if (byId[id].projectId === slot.projectId && result.indexOf(id) < 0) result.push(id);
    });
  });
  return result;
}
