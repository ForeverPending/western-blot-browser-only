// Exercises the SHIPPED custom-group selection helpers out of frontend/app.js. Ticking a
// lane checkbox runs updateGroupSelection, and the grouped graphs are drawn from
// buildSelectedGroups, so a toggle must show up in the built groups.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../frontend/app.js", import.meta.url), "utf8");

function grab(name) {
  const start = app.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const end = app.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated function ${name}`);
  return app.slice(start, end + 3);
}

const api = new Function(`
  ${grab("analysisRowKey")}
  ${grab("rowKeyMap")}
  ${grab("groupSelectionKeys")}
  ${grab("setGroupSelectionKeys")}
  ${grab("updateGroupSelection")}
  ${grab("buildSelectedGroups")}
  ${grab("createDefaultCustomGroups")}
  return { analysisRowKey, updateGroupSelection, buildSelectedGroups, createDefaultCustomGroups };
`)();

const rows = ["L1", "L2", "L3", "L4", "L5", "L6"].map((lane) => ({ lane }));
const lanesOf = (groups) => groups.map((group) => group.rows.map((row) => row.lane));

test("ticking a lane adds it to the group in lane order", () => {
  const groups = api.createDefaultCustomGroups(rows, api.analysisRowKey);
  api.updateGroupSelection(groups[0], rows, api.analysisRowKey, "L2", true);
  assert.deepEqual(lanesOf(api.buildSelectedGroups(groups, rows)), [
    ["L1", "L2", "L3", "L5"],
    ["L2", "L4", "L6"],
  ]);
});

test("unticking a lane removes it, and an emptied group drops out", () => {
  const groups = api.createDefaultCustomGroups(rows, api.analysisRowKey);
  api.updateGroupSelection(groups[0], rows, api.analysisRowKey, "L3", false);
  assert.deepEqual(lanesOf(api.buildSelectedGroups(groups, rows)), [
    ["L1", "L5"],
    ["L2", "L4", "L6"],
  ]);
  for (const lane of ["L2", "L4", "L6"]) api.updateGroupSelection(groups[1], rows, api.analysisRowKey, lane, false);
  assert.deepEqual(lanesOf(api.buildSelectedGroups(groups, rows)), [["L1", "L5"]]);
});
