// Keep small, read-only room synopses for cold virtualized tiles.
function store(cache, roomId, lines, limit) {
  if (!roomId || !Array.isArray(lines)) return cache;
  var rows = Object.assign({}, cache.rows);
  var order = cache.order.filter(function(id) { return id !== roomId; });
  rows[roomId] = lines;
  order.push(roomId);
  while (order.length > limit) delete rows[order.shift()];
  return { rows: rows, order: order };
}
