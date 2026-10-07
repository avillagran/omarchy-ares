import test from "node:test";
import assert from "node:assert/strict";
import { approvalAction, groupProjects, reconcileEvents, quotaSeparatorCount, roomRoute, tokenFromLocation } from "../core.mjs";

test("reconciles message deltas into one live transcript row", () => {
  const rows = reconcileEvents([
    { id: "1", sequence: 1, executionId: "e1", kind: "message.delta", payload: { text: "Hel" } },
    { id: "2", sequence: 2, executionId: "e1", kind: "tool.start", payload: { name: "terminal" } },
    { id: "3", sequence: 3, executionId: "e1", kind: "message.delta", payload: { text: "Hello" } },
    { id: "4", sequence: 4, executionId: "e1", kind: "message.assistant", payload: { text: "Hello" } },
    { id: "5", sequence: 5, executionId: "e1", kind: "reasoning.delta", payload: { text: "rea" } },
    { id: "6", sequence: 6, executionId: "e1", kind: "reasoning.delta", payload: { text: "son" } },
    { id: "7", sequence: 7, executionId: "e1", kind: "thinking.delta", payload: { text: "" } },
    { id: "8", sequence: 8, executionId: "e1", kind: "execution.completed", payload: { text: "Hello" } },
  ]);
  assert.equal(rows.filter((row) => row.kind === "message.assistant").length, 1);
  assert.equal(rows.find((row) => row.kind === "message.assistant").text, "Hello");
  assert.equal(rows.filter((row) => row.kind === "tool.start").length, 1);
  assert.equal(rows.filter((row) => row.kind === "reasoning.delta").length, 1);
  assert.equal(rows.find((row) => row.kind === "reasoning.delta").text, "reason");
  assert.equal(rows.some((row) => row.kind === "thinking.delta"), false);
  assert.equal(rows.find((row) => row.kind === "execution.completed").text, "Completed");
});

test("daily quota separators use duration and omit unknown or sub-day windows", () => {
  assert.equal(quotaSeparatorCount(7 * 86400), 6);
  assert.equal(quotaSeparatorCount(30 * 86400), 29);
  assert.equal(quotaSeparatorCount(3600), 0);
  assert.equal(quotaSeparatorCount(null), 0);
});

test("room routes preserve project ownership", () => {
  assert.equal(roomRoute("project a", "room/b"), "#/projects/project%20a/rooms/room%2Fb");
});

test("approval actions require exact execution and request identifiers", () => {
  assert.deepEqual(
    approvalAction({ kind: "approval.request", executionId: "exec-1", payload: { request_id: "request-1" } }),
    { executionId: "exec-1", requestId: "request-1" },
  );
  assert.equal(approvalAction({ kind: "approval.request", executionId: "", payload: {} }), null);
});

test("groups projects while preserving an ungrouped section", () => {
  const grouped = groupProjects([{ id: "a", groupId: "g" }, { id: "b", groupId: null }], [{ id: "g", name: "Work" }]);
  assert.deepEqual(grouped.map((entry) => [entry.name, entry.projects.map((project) => project.id)]), [["Work", ["a"]], ["Ungrouped", ["b"]]]);
});

test("access links carry the token in the fragment, never mistaken for a room route", () => {
  assert.equal(tokenFromLocation("", "#token=abc-DEF_123"), "abc-DEF_123");
  assert.equal(tokenFromLocation("?token=query-token", ""), "query-token");
  assert.equal(tokenFromLocation("?token=query-token", "#token=fragment-token"), "query-token");
  assert.equal(tokenFromLocation("", ""), "");
  assert.equal(tokenFromLocation("", "#/projects/p/rooms/r"), "");
  assert.equal(tokenFromLocation("", "#token=has space"), "");
});
