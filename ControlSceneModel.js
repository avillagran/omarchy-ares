function build(rooms, items, bots, projects, logs, limit) {
  var activity = Object.create(null);
  for (var i = 0; i < items.length; ++i) {
    var item = items[i];
    var id = String(item.roomId || "");
    if (!id) continue;
    var next = (item.status === "working" || item.status === "running" || item.status === "queued")
      ? "working" : "waiting";
    if (next === "waiting" || !activity[id]) activity[id] = next;
  }
  var groups = {working: [], waiting: [], idle: []};
  var projectNames = Object.create(null);
  var botConfig = Object.create(null);
  for (var p = 0; p < projects.length; ++p) projectNames[projects[p].id] = projects[p].name;
  for (var b = 0; b < bots.length; ++b) botConfig[bots[b].id] = bots[b];
  for (var j = 0; j < rooms.length; ++j) {
    var room = rooms[j];
    if (room.status !== "active" || room.source === "control" || room.controlIncluded !== true) continue;
    var participants = room.participants && room.participants.length ? room.participants : ["default"];
    var seen = Object.create(null);
    for (var n = 0; n < participants.length; ++n) {
      var botId = String(participants[n]);
      if (seen[botId]) continue;
      seen[botId] = true;
      var bot = botConfig[botId] || {};
      var state = activity[room.id] || "idle";
      var shape = String(bot.faceShape || "diamond");
      if (shape === "circle") shape = "oval";
      if (shape === "hexagon") shape = "square";
      groups[state].push({roomId: room.id, botId: botId, name: String(bot.name || botId),
        projectName: String(projectNames[room.projectId] || room.name || "Project"),
        shape: shape, colorRole: String(bot.colorRole || "accent"), activity: state,
        logLines: state === "working" && Array.isArray(logs[room.id]) ? logs[room.id].slice(0, 2) : []});
    }
  }
  var distinct = [], duplicates = [], shownBots = Object.create(null);
  for (var d = 0; d < groups.idle.length; ++d) {
    var candidate = groups.idle[d];
    if (shownBots[candidate.botId]) duplicates.push(candidate);
    else { distinct.push(candidate); shownBots[candidate.botId] = true; }
  }
  groups.idle = distinct.concat(duplicates);
  var counts = {working: groups.working.length, waiting: groups.waiting.length, idle: groups.idle.length};
  var order = ["working", "waiting", "idle"];
  var cap = Math.max(0, Math.min(12, limit));
  var zoneCaps = {working: 4, waiting: 3, idle: 3};
  var picked = {working: [], waiting: [], idle: []};
  // A crowded studio must not erase the commons or the user's attention signal.
  for (var k = 0; k < order.length; ++k) {
    var group = order[k];
    if (groups[group].length && cap > 0) { picked[group].push(groups[group].shift()); --cap; }
  }
  for (var m = 0; m < order.length && cap > 0; ++m) {
    var remaining = groups[order[m]].splice(0, Math.min(cap, zoneCaps[order[m]] - picked[order[m]].length));
    picked[order[m]] = picked[order[m]].concat(remaining);
    cap -= remaining.length;
  }
  return {people: picked.working.concat(picked.waiting, picked.idle), counts: counts};
}
