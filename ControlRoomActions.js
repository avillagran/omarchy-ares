function canArchive(room, items) {
  if (!room || room.status !== "active" || room.source === "control" || room.externalArchived === true || room.controlIncluded !== true)
    return false;
  return !items.some(function(item) {
    return item.roomId === room.id && ["working", "queued", "running", "blocked", "waiting", "needs_input"].indexOf(item.status) >= 0;
  });
}
