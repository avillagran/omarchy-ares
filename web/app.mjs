import { approvalAction, groupProjects, makeIdempotencyKey, parseRoomRoute, reconcileEvents, roomRoute, tokenFromLocation } from "./core.mjs";

const $ = (selector) => document.querySelector(selector);
const state = { token: sessionStorage.getItem("ares.token") || "", data: null, projectId: "", roomId: "", cursor: 0, polling: false, selectedFile: null, wasNearEnd: true };

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.token}`);
  headers.set("Accept", "application/json");
  if (options.body && !(options.body instanceof Blob) && typeof options.body !== "string") {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { message = (await response.json()).error || message; } catch {}
    throw new Error(message);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : response;
}

function login(show) {
  $("#login").hidden = !show;
  $("#app").hidden = show;
}

async function connect(token) {
  state.token = token;
  const data = await api("/v1/state?compact=1");
  sessionStorage.setItem("ares.token", token);
  state.data = data;
  login(false);
  applyRoute();
  if (state.roomId) await refresh();
  else render();
  poll();
}

function applyRoute() {
  const route = parseRoomRoute(location.hash);
  if (route && state.data?.projects.some((item) => item.id === route.projectId) && state.data?.rooms.some((item) => item.id === route.roomId && item.projectId === route.projectId)) {
    state.projectId = route.projectId;
    state.roomId = route.roomId;
  } else if (!state.projectId && state.data?.projects.length) {
    state.projectId = state.data.projects[0].id;
    const room = state.data.rooms.find((item) => item.projectId === state.projectId);
    state.roomId = room?.id || "";
  }
}

async function refresh() {
  const query = state.roomId ? `?compact=1&eventRoomId=${encodeURIComponent(state.roomId)}` : "?compact=1";
  const selected = await api(`/v1/state${query}`);
  state.data = selected;
  state.cursor = Number(state.data.latestCursor || 0);
  render();
}

async function poll() {
  if (state.polling || !state.token) return;
  state.polling = true;
  try {
    const room = state.roomId ? `&roomId=${encodeURIComponent(state.roomId)}` : "";
    const update = await api(`/v1/events?after=${state.cursor}&limit=300${room}`);
    if (update.gap) await refresh();
    else if (update.events.length) {
      const known = new Set((state.data.events || []).map((event) => event.id));
      state.data.events.push(...update.events.filter((event) => !known.has(event.id)));
      state.cursor = update.nextCursor;
      renderTimeline();
      await refresh();
    }
    $("#connection").textContent = "live";
  } catch (error) {
    $("#connection").textContent = `reconnecting · ${error.message}`;
  } finally {
    state.polling = false;
    setTimeout(poll, document.hidden ? 2500 : 450);
  }
}

function selectProject(projectId) {
  state.projectId = projectId;
  state.roomId = state.data.rooms.find((room) => room.projectId === projectId)?.id || "";
  if (state.roomId) location.hash = roomRoute(projectId, state.roomId).slice(1);
  render();
}

async function selectRoom(projectId, roomId) {
  state.projectId = projectId;
  state.roomId = roomId;
  location.hash = roomRoute(projectId, roomId).slice(1);
  $("#rail").classList.remove("open");
  const draft = localStorage.getItem(`ares.draft.${roomId}`) || "";
  $("#prompt").value = draft;
  await refresh();
}

function textElement(tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function render() {
  if (!state.data) return;
  const projects = $("#projects");
  projects.replaceChildren();
  for (const group of groupProjects(state.data.projects, state.data.groups || [])) {
    projects.append(textElement("div", group.name, "group-label"));
    for (const project of group.projects) {
      const button = textElement("button", project.name, `project ${project.id === state.projectId ? "active" : ""}`);
      button.append(textElement("small", `${project.node} · ${project.path}`));
      button.onclick = () => selectProject(project.id);
      projects.append(button);
    }
  }
  const groupSelect = $("#project-group");
  const selectedGroup = groupSelect.value;
  groupSelect.replaceChildren(new Option("Ungrouped", ""), ...(state.data.groups || []).map((group) => new Option(group.name, group.id)));
  groupSelect.value = selectedGroup;
  const rooms = $("#rooms");
  rooms.replaceChildren();
  for (const room of state.data.rooms.filter((item) => item.projectId === state.projectId)) {
    const button = textElement("button", room.name, `room-link ${room.id === state.roomId ? "active" : ""}`);
    button.append(textElement("small", `${room.status} · ${room.eventCount} events`));
    button.onclick = () => selectRoom(room.projectId, room.id);
    rooms.append(button);
  }
  const room = state.data.rooms.find((item) => item.id === state.roomId);
  $("#empty").hidden = Boolean(room);
  $("#room-content").hidden = !room;
  $("#new-room").disabled = !state.projectId;
  const management = $("#management-state");
  management.replaceChildren(textElement("div", `${state.data.bots.length} bots · ${state.data.peers.length} peers · cursor ${state.data.latestCursor}`));
  for (const quota of state.data.quotas || []) {
    const card = document.createElement("div"); card.className = "quota";
    card.append(textElement("span", `${quota.provider} · ${quota.label}`), textElement("strong", `${Math.round(quota.availablePercent)}%`));
    const track = document.createElement("div"); track.className = "quota-track";
    const fill = document.createElement("i"); fill.style.width = `${Math.max(0, Math.min(100, quota.availablePercent))}%`; track.append(fill);
    for (let index = 1; index <= quota.dailySeparators; index++) { const mark = document.createElement("b"); mark.style.left = `${index / (quota.dailySeparators + 1) * 100}%`; track.append(mark); }
    card.append(track); management.append(card);
  }
  if (!room) return;
  const project = state.data.projects.find((item) => item.id === room.projectId);
  $("#project-name").textContent = project?.name || "";
  $("#room-name").textContent = room.name;
  $("#participants").textContent = room.participants.join(" · ");
  $("#pause").textContent = room.status === "paused" ? "Resume" : "Pause";
  $("#prompt").disabled = room.status !== "active";
  $("#send").disabled = room.status !== "active";
  renderTimeline();
}

function renderTimeline() {
  if (!state.data || !state.roomId) return;
  const timeline = $("#timeline");
  const nearEnd = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 100;
  const events = reconcileEvents(state.data.events.filter((event) => event.roomId === state.roomId));
  timeline.replaceChildren();
  for (const event of events) timeline.append(renderEvent(event));
  if (nearEnd || state.wasNearEnd) {
    requestAnimationFrame(() => { timeline.scrollTop = timeline.scrollHeight; });
    $("#new-activity").hidden = true;
  } else $("#new-activity").hidden = false;
  state.wasNearEnd = false;
}

function renderEvent(event) {
  const kind = event.kind || "status";
  const category = kind === "message.user" ? "user" : kind === "message.assistant" ? "assistant" : kind.startsWith("tool.") ? "tool" : "lifecycle";
  const card = document.createElement("article");
  const failed = kind.includes("failed") || kind === "error" || kind.endsWith(".error");
  card.className = `event ${category} ${failed ? "error" : ""} ${event.live ? "live" : ""}`;
  card.dataset.eventId = event.sourceEventId || event.id;
  const meta = textElement("div", `#${event.sequence} · ${event.author} · ${kind}`, "meta");
  card.append(meta);
  const payload = event.payload || {};
  if (kind.startsWith("tool.")) {
    const details = document.createElement("details");
    details.open = kind !== "tool.complete";
    details.append(textElement("summary", `${payload.name || "tool"} · ${kind.split(".")[1]}`));
    details.append(textElement("pre", JSON.stringify(payload, null, 2)));
    card.append(details);
  } else if (event.text) card.append(textElement("div", event.text));
  const approval = approvalAction(event);
  if (approval) {
    const actions = document.createElement("div");
    actions.className = "actions";
    for (const [label, choice] of [["Allow once", "once"], ["Always allow", "always"], ["Deny", "deny"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.onclick = async () => {
        actions.querySelectorAll("button").forEach((node) => { node.disabled = true; });
        try {
          await api(`/v1/executions/${encodeURIComponent(approval.executionId)}/approval`, {
            method: "POST", body: { requestId: approval.requestId, choice },
          });
          await refresh();
        } catch (error) {
          actions.append(textElement("span", error.message, "error"));
        }
      };
      actions.append(button);
    }
    card.append(actions);
  }
  if (kind === "artifact.available" && payload.artifactId) {
    const url = `/v1/artifacts/${encodeURIComponent(payload.artifactId)}`;
    if (String(payload.mime).startsWith("image/")) {
      const image = document.createElement("img");
      image.loading = "lazy";
      loadAuthorizedMedia(image, url);
      image.alt = payload.name || "Image attachment";
      image.onclick = () => openMedia("image", url);
      card.append(image);
    } else if (String(payload.mime).startsWith("video/")) {
      const video = document.createElement("video");
      video.controls = true;
      video.preload = "metadata";
      loadAuthorizedMedia(video, url);
      card.append(video);
    }
  }
  return card;
}

async function loadAuthorizedMedia(element, url) {
  try {
    const response = await fetch(url, { headers: { Authorization: `Bearer ${state.token}` } });
    const blob = await response.blob();
    element.src = URL.createObjectURL(blob);
  } catch { element.replaceWith(textElement("p", "Media unavailable")); }
}

async function openMedia(type, url) {
  const holder = $("#media-large");
  holder.replaceChildren();
  const node = document.createElement(type === "video" ? "video" : "img");
  if (type === "video") node.controls = true;
  await loadAuthorizedMedia(node, url);
  holder.append(node);
  $("#media-dialog").showModal();
}

async function uploadSelected() {
  if (!state.selectedFile) return null;
  const file = state.selectedFile;
  const query = new URLSearchParams({ projectId: state.projectId, roomId: state.roomId, name: file.name, author: "user" });
  const result = await api(`/v1/artifacts?${query}`, { method: "PUT", headers: { "Content-Type": file.type }, body: file });
  return result.artifact;
}

$("#login-form").onsubmit = async (event) => {
  event.preventDefault();
  try { await connect($("#token").value); }
  catch (error) { $("#login-error").textContent = error.message; }
};
$("#logout").onclick = () => { state.token = ""; sessionStorage.removeItem("ares.token"); login(true); };
$("#refresh").onclick = refresh;
$("#nav-toggle").onclick = () => $("#rail").classList.toggle("open");
$("#new-project").onclick = () => $("#project-dialog").showModal();
$("#new-group").onclick = () => $("#group-dialog").showModal();
$("#new-room").onclick = () => $("#room-dialog").showModal();
$("#group-form").onsubmit = async (event) => {
  event.preventDefault();
  await api("/v1/groups", { method: "POST", body: Object.fromEntries(new FormData(event.currentTarget)) });
  $("#group-dialog").close();
  event.currentTarget.reset();
  await refresh();
};
$("#project-form").onsubmit = async (event) => {
  event.preventDefault();
  const body = Object.fromEntries(new FormData(event.currentTarget));
  await api("/v1/projects", { method: "POST", body });
  $("#project-dialog").close();
  event.currentTarget.reset();
  await refresh();
};
$("#room-form").onsubmit = async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  const result = await api("/v1/rooms", { method: "POST", body: { projectId: state.projectId, name: values.name, participants: [values.bot] } });
  $("#room-dialog").close();
  event.currentTarget.reset();
  await selectRoom(state.projectId, result.room.id);
};
$("#pause").onclick = async () => {
  const room = state.data.rooms.find((item) => item.id === state.roomId);
  const action = room.status === "paused" ? "resume" : "pause";
  await api(`/v1/rooms/${room.id}/${action}`, { method: "POST", body: { projectId: room.projectId, idempotencyKey: makeIdempotencyKey(action) } });
  await refresh();
};
$("#delete-room").onclick = async () => {
  const room = state.data.rooms.find((item) => item.id === state.roomId);
  if (!confirm(`Move “${room.name}” to recovery?`)) return;
  await api(`/v1/rooms/${room.id}/delete`, { method: "POST", body: { projectId: room.projectId, idempotencyKey: makeIdempotencyKey("delete") } });
  state.roomId = "";
  await refresh();
};
$("#prompt").oninput = (event) => localStorage.setItem(`ares.draft.${state.roomId}`, event.target.value);
$("#attachment").onchange = (event) => {
  state.selectedFile = event.target.files[0] || null;
  const preview = $("#attachment-preview");
  preview.replaceChildren();
  if (!state.selectedFile) return;
  const node = document.createElement(state.selectedFile.type.startsWith("video/") ? "video" : "img");
  if (node instanceof HTMLVideoElement) node.controls = true;
  node.src = URL.createObjectURL(state.selectedFile);
  preview.append(node, textElement("span", `${state.selectedFile.name} · stored in the room; model delivery depends on route capability`));
};
$("#composer").onsubmit = async (event) => {
  event.preventDefault();
  const text = $("#prompt").value.trim();
  if (!text) return;
  $("#composer-status").textContent = "Uploading and accepting…";
  try {
    const artifact = await uploadSelected();
    const room = state.data.rooms.find((item) => item.id === state.roomId);
    const result = await api(`/v1/rooms/${state.roomId}/send`, { method: "POST", body: { projectId: state.projectId, botId: room.participants[0], text, artifactIds: artifact ? [artifact.id] : [], idempotencyKey: makeIdempotencyKey("send") } });
    $("#composer-status").textContent = `Accepted · ${result.execution.id}`;
    $("#prompt").value = "";
    localStorage.removeItem(`ares.draft.${state.roomId}`);
    state.selectedFile = null;
    $("#attachment").value = "";
    $("#attachment-preview").replaceChildren();
    await refresh();
  } catch (error) { $("#composer-status").textContent = error.message; }
};
$("#new-activity").onclick = () => { const node = $("#timeline"); node.scrollTop = node.scrollHeight; $("#new-activity").hidden = true; };
$("#timeline").onscroll = (event) => { const node = event.currentTarget; state.wasNearEnd = node.scrollHeight - node.scrollTop - node.clientHeight < 100; };
$("#media-close").onclick = () => $("#media-dialog").close();
window.addEventListener("hashchange", async () => { applyRoute(); await refresh(); });

document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
// Access links copied from the desktop panel carry the token in the URL
// fragment (#token=…); a fresh link always wins over the stored session token
// so regenerated credentials take effect without a manual logout.
const urlToken = tokenFromLocation(location.search, location.hash);
if (urlToken) {
  state.token = urlToken;
  sessionStorage.setItem("ares.token", urlToken);
  history.replaceState(null, "", location.pathname);
}
if (state.token) connect(state.token).catch(() => { sessionStorage.removeItem("ares.token"); state.token = ""; login(true); });
else login(true);
