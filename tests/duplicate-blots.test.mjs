// Exercises the SHIPPED duplicate-blot merge out of frontend/app.js. Blot ids are
// regenerated on every upload, so content fingerprints are the only thing stopping a
// re-uploaded ZIP from stacking identical copies.
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

function makeApi() {
  const blotState = { blots: [], scans: {}, activeBlotIndex: null };
  const api = new Function("blotState", `
    ${grab("normalizeBlot")}
    ${grab("blotFingerprint")}
    ${grab("findBlotByFingerprint")}
    ${grab("mergeBlots")}
    ${grab("sortBlotsByCreatedAt")}
    ${grab("duplicateSkipMessage")}
    return { mergeBlots, duplicateSkipMessage, blotFingerprint };
  `)(blotState);
  return { api, blotState };
}

let counter = 0;
function blot({ name = "Blot A", fingerprint = "abc123", id, createdAt = "2026-05-15T00:00:00Z" } = {}) {
  counter += 1;
  return {
    id: id || `upload-${counter}`,
    name,
    fingerprint,
    createdAt,
    files: { 700: { path: "p700" }, 800: { path: "p800" } },
  };
}

test("re-uploading the same content does not add a second blot", () => {
  const { api, blotState } = makeApi();
  const first = api.mergeBlots([blot({ name: "Day 1" })]);
  assert.equal(first.added.length, 1);
  assert.equal(first.duplicates.length, 0);

  // Same fingerprint, fresh id — exactly what a repeat upload of the same ZIP produces.
  const second = api.mergeBlots([blot({ name: "Day 1" })]);
  assert.equal(second.added.length, 0);
  assert.equal(second.duplicates.length, 1);
  assert.equal(blotState.blots.length, 1, "state must still hold one blot");
});

test("a duplicate is reported with the blot it already had", () => {
  const { api } = makeApi();
  api.mergeBlots([blot({ name: "Original", fingerprint: "dup" })]);
  const { duplicates } = api.mergeBlots([blot({ name: "Copy", fingerprint: "dup" })]);
  assert.equal(duplicates[0].blot.name, "Copy");
  assert.equal(duplicates[0].existing.name, "Original");
});

test("duplicates within one ZIP collapse to a single blot", () => {
  const { api, blotState } = makeApi();
  const { added, duplicates } = api.mergeBlots([
    blot({ name: "Folder 1", fingerprint: "same" }),
    blot({ name: "Folder 2", fingerprint: "same" }),
    blot({ name: "Folder 3", fingerprint: "other" }),
  ]);
  assert.equal(added.length, 2);
  assert.equal(duplicates.length, 1);
  assert.equal(blotState.blots.length, 2);
});

test("genuinely different blots are all kept", () => {
  const { api, blotState } = makeApi();
  const { added, duplicates } = api.mergeBlots([
    blot({ name: "A", fingerprint: "f1" }),
    blot({ name: "B", fingerprint: "f2" }),
    blot({ name: "C", fingerprint: "f3" }),
  ]);
  assert.equal(added.length, 3);
  assert.equal(duplicates.length, 0);
  assert.equal(blotState.blots.length, 3);
});

test("a missing fingerprint never counts as a match", () => {
  // Blots saved by an older version have no fingerprint. If "" matched "", every legacy
  // blot would look like a duplicate of every other and uploads would silently vanish.
  const { api, blotState } = makeApi();
  api.mergeBlots([blot({ name: "Legacy A", fingerprint: "" })]);
  const { added, duplicates } = api.mergeBlots([
    blot({ name: "Legacy B", fingerprint: "" }),
    blot({ name: "Legacy C", fingerprint: "   " }),
  ]);
  assert.equal(duplicates.length, 0, "unknown content is not evidence of duplication");
  assert.equal(added.length, 2);
  assert.equal(blotState.blots.length, 3);
});

test("re-merging the same blot id updates rather than duplicating", () => {
  // Distinct from a content duplicate: same id means the same record being refreshed, and
  // the merge behaviour that fills in fields has to survive.
  const { api, blotState } = makeApi();
  api.mergeBlots([blot({ id: "fixed", name: "Before", fingerprint: "f" })]);
  const { added, duplicates } = api.mergeBlots([
    blot({ id: "fixed", name: "After", fingerprint: "f" }),
  ]);
  assert.equal(duplicates.length, 0);
  assert.equal(added.length, 0);
  assert.equal(blotState.blots.length, 1);
  assert.equal(blotState.blots[0].name, "After");
});

test("scan buckets are only created for blots that were kept", () => {
  const { api, blotState } = makeApi();
  api.mergeBlots([blot({ fingerprint: "keep" })]);
  const skipped = blot({ fingerprint: "keep" });
  api.mergeBlots([skipped]);
  assert.ok(!(skipped.id in blotState.scans), "a skipped blot must not get a scan bucket");
});

test("the skip message distinguishes a renamed copy from an exact one", () => {
  const { api } = makeApi();
  const same = api.duplicateSkipMessage([
    { blot: { name: "Blot A" }, existing: { name: "Blot A" } },
  ]);
  assert.match(same, /already loaded/);
  assert.doesNotMatch(same, /same image data/);

  const renamed = api.duplicateSkipMessage([
    { blot: { name: "Copy of A" }, existing: { name: "Blot A" } },
  ]);
  assert.match(renamed, /same image data as "Blot A"/);

  assert.equal(api.duplicateSkipMessage([]), "");
  assert.match(api.duplicateSkipMessage([1, 2, 3].map(() => ({
    blot: { name: "x" }, existing: { name: "y" },
  }))), /Skipped 3 blots/);
});

test("the fingerprint backfill batches to the backend's cap", async () => {
  // A single scanner ZIP can hold ten blots, so a few uploads exceed the per-request cap.
  // An unbatched request would 400 and the whole backfill would silently do nothing.
  const py = await readFile(new URL("../backend/app.py", import.meta.url), "utf8");
  const cap = py.match(/^MAX_FINGERPRINT_BLOTS = int\(os\.environ\.get\("MAX_FINGERPRINT_BLOTS", (\d+)\)\)/m);
  assert.ok(cap, "backend MAX_FINGERPRINT_BLOTS not found");
  const client = app.match(/^const FINGERPRINT_BATCH_SIZE = (\d+);/m);
  assert.ok(client, "client FINGERPRINT_BATCH_SIZE not found");
  assert.ok(Number(client[1]) <= Number(cap[1]),
    `batch size ${client[1]} exceeds backend cap ${cap[1]}`);

  const start = app.indexOf("async function backfillBlotFingerprints");
  assert.notEqual(start, -1, "backfill renamed; this test would be vacuous");
  const body = app.slice(start, app.indexOf("\n}\n", start));
  assert.match(body, /FINGERPRINT_BATCH_SIZE/);
  assert.match(body, /slice\(0, FINGERPRINT_BATCH_SIZE\)/);
  // The backend bounds each request by total BYTES read as well as blot count, and says so
  // with `truncated`. The client has to re-queue the tail it never read, or a workspace of
  // large TIFs loses dedup on everything past the budget — and the round cap is what stops
  // a budget too small for even one blot from spinning.
  assert.match(body, /payload\.truncated === true/);
  assert.match(body, /FINGERPRINT_MAX_ROUNDS/);
});

test("skipped duplicates have their temp files released", () => {
  // The backend has already stored them by the time we know they are duplicates; not
  // cleaning up would orphan the files until the session TTL sweep.
  const start = app.indexOf("const { added, duplicates } = mergeBlots(");
  assert.notEqual(start, -1, "upload handler changed; this test would be vacuous");
  const handler = app.slice(start, start + 1200);
  assert.match(handler, /cleanupBlots\(duplicates\.map/);
});
