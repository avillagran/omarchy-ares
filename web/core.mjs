export function reconcileEvents(events) {
  const output = [];
  const liveIndex = new Map();
  for (const event of [...events].sort((a, b) => Number(a.sequence || 0) - Number(b.sequence || 0))) {
    const execution = String(event.executionId || "");
    const payload = event.payload || {};
    if (event.kind === "message.delta" && execution) {
      const key = `assistant:${execution}`;
      const row = {
        ...event,
        id: key,
        sourceEventId: event.id,
        kind: "message.assistant",
        live: true,
        text: String(payload.text || payload.delta || ""),
      };
      if (liveIndex.has(key)) output[liveIndex.get(key)] = row;
      else {
        liveIndex.set(key, output.length);
        output.push(row);
      }
      continue;
    }
    if (event.kind === "message.assistant" && execution) {
      const key = `assistant:${execution}`;
      const row = { ...event, id: key, sourceEventId: event.id, live: false, text: String(payload.text || event.text || "") };
      if (liveIndex.has(key)) output[liveIndex.get(key)] = row;
      else {
        liveIndex.set(key, output.length);
        output.push(row);
      }
      continue;
    }
    if (String(event.kind || "").endsWith(".delta") && execution) {
      const key = `${event.kind}:${execution}`;
      const text = String(payload.text || payload.delta || "");
      if (!text.trim()) continue;
      let merged = text;
      if (liveIndex.has(key)) {
        const previous = String(output[liveIndex.get(key)]?.text || "");
        merged = text.startsWith(previous) ? text : previous + text;
      }
      const row = { ...event, id: key, sourceEventId: event.id, live: true, text: merged };
      if (liveIndex.has(key)) output[liveIndex.get(key)] = row;
      else {
        liveIndex.set(key, output.length);
        output.push(row);
      }
      continue;
    }
    if (event.kind === "execution.completed" && execution) {
      const assistantIndex = liveIndex.get(`assistant:${execution}`);
      const assistantText = assistantIndex === undefined ? "" : String(output[assistantIndex]?.text || "");
      const completedText = String(payload.text || event.text || "");
      output.push({ ...event, text: completedText === assistantText ? "Completed" : completedText || "Completed" });
      continue;
    }
    output.push({ ...event, text: String(payload.text || payload.delta || event.text || "") });
  }
  return output.sort((a, b) => Number(a.sequence || 0) - Number(b.sequence || 0));
}

export function quotaSeparatorCount(windowSeconds) {
  const seconds = Number(windowSeconds);
  if (!Number.isFinite(seconds) || seconds < 86400) return 0;
  const days = Math.round(seconds / 86400);
  return Math.max(0, days - 1);
}

export function approvalAction(event) {
  if (!event || event.kind !== "approval.request") return null;
  const executionId = String(event.executionId || "");
  const requestId = String(event.payload?.request_id || event.payload?.requestId || "");
  return executionId && requestId ? { executionId, requestId } : null;
}

export function groupProjects(projects, groups) {
  const buckets = groups.map((group) => ({ id: group.id, name: group.name, projects: [] }));
  const byId = new Map(buckets.map((bucket) => [bucket.id, bucket]));
  const ungrouped = { id: null, name: "Ungrouped", projects: [] };
  for (const project of projects) {
    (byId.get(project.groupId) || ungrouped).projects.push(project);
  }
  if (ungrouped.projects.length) buckets.push(ungrouped);
  return buckets.filter((bucket) => bucket.projects.length);
}

export function roomRoute(projectId, roomId) {
  return `#/projects/${encodeURIComponent(projectId)}/rooms/${encodeURIComponent(roomId)}`;
}

export function parseRoomRoute(hash) {
  const match = /^#\/projects\/([^/]+)\/rooms\/([^/]+)$/.exec(hash || "");
  return match ? { projectId: decodeURIComponent(match[1]), roomId: decodeURIComponent(match[2]) } : null;
}

export function makeIdempotencyKey(prefix = "web") {
  return `${prefix}-${Date.now().toString(36)}-${crypto.randomUUID()}`;
}

export function tokenFromLocation(search, hash) {
  const fromQuery = new URLSearchParams(search || "").get("token");
  if (fromQuery) return fromQuery;
  const match = /^#token=([A-Za-z0-9_-]+)$/.exec(hash || "");
  return match ? match[1] : "";
}
