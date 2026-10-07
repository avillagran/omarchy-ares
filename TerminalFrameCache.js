// Keep only a bounded set of rendered terminal surfaces in memory. The image
// handle owns its image:// URL; dropping the handle releases the frame.
function store(cache, roomId, image, capturedAt, limit) {
  if (!roomId || !image || !image.url) return cache;
  var previous = cache.frames[roomId];
  if (previous && previous.at > capturedAt) return cache;
  var frames = Object.assign({}, cache.frames);
  var order = cache.order.filter(function(id) { return id !== roomId; });
  frames[roomId] = { image: image, at: capturedAt };
  order.push(roomId);
  while (order.length > limit) delete frames[order.shift()];
  return { frames: frames, order: order };
}
