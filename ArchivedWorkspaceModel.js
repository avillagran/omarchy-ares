function unassignedProject(project) {
  return /^peer:\/\/[^/]+\/[^/]+$/.test(String((project || {}).path || ""));
}

function groups(rooms, projects) {
  var projectById = Object.create(null);
  for (var p = 0; p < projects.length; ++p) projectById[String(projects[p].id)] = projects[p];
  var grouped = Object.create(null);
  var result = [];
  for (var i = 0; i < rooms.length; ++i) {
    var room = rooms[i];
    if (!room || typeof room !== 'object') continue;
    var id = String(room.projectId || '');
    if (!grouped[id]) {
      var project = projectById[id] || null;
      var group = {
        projectId: id,
        name: project && unassignedProject(project) ? 'Unassigned / Home'
          : (project && project.name ? String(project.name) : 'Unknown project'),
        count: 0,
        rooms: []
      };
      grouped[id] = group;
      result.push(group);
    }
    grouped[id].rooms.push(room);
    grouped[id].count += 1;
  }
  return result;
}
