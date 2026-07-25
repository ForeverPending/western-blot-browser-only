// Exercises the SHIPPED display-window slider math out of frontend/app.js rather than a
// re-implementation, by slicing the functions out and running them against a stub state.
// These controls replaced brightness/contrast, whose failure was arithmetic (a mid-gray
// pivot against data sitting near zero), so the mapping deserves real assertions.
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

function grabConst(name) {
  const match = app.match(new RegExp(`^const ${name} = [^;]*;`, "m"));
  assert.ok(match, `missing const ${name}`);
  return match[0];
}

const api = new Function(`
  ${grabConst("DISPLAY_SLIDER_STEPS")}
  ${grabConst("DISPLAY_SLIDER_CURVE")}
  ${grab("displaySliderToCount")}
  ${grab("displayCountToSlider")}
  ${grab("formatCount")}
  return { displaySliderToCount, displayCountToSlider, formatCount, DISPLAY_SLIDER_STEPS };
`)();

// Real numbers from test_blots/2026-04-16.zip's 700nm channel: the data of interest lives
// under ~400 counts while a few hot pixels push the maximum to 6684.
const STATS = { min: 0, max: 6684, autoMin: 0.1, autoMax: 375 };

test("slider ends map to the channel's full raw range", () => {
  assert.equal(api.displaySliderToCount(0, STATS), STATS.min);
  assert.equal(api.displaySliderToCount(api.DISPLAY_SLIDER_STEPS, STATS), STATS.max);
});

test("slider position and raw count round-trip", () => {
  for (const count of [0, 20, 216, 375, 1000, 6684]) {
    const position = api.displayCountToSlider(count, STATS);
    const back = api.displaySliderToCount(position, STATS);
    assert.ok(Math.abs(back - count) <= STATS.max / api.DISPLAY_SLIDER_STEPS * 3,
      `count ${count} -> position ${position} -> ${back}`);
  }
});

test("the curve keeps the usable window off the bottom of the track", () => {
  // The whole point of the non-linear track: a linear one would put the auto white point
  // (375 of 6684) at 5.6% of travel, so every useful setting would sit in a sliver. The
  // brightness/contrast controls this replaced failed in exactly that way.
  const linearFraction = STATS.autoMax / STATS.max;
  const curvedFraction = api.displayCountToSlider(STATS.autoMax, STATS) / api.DISPLAY_SLIDER_STEPS;
  assert.ok(linearFraction < 0.06, `sanity: linear would be ${linearFraction}`);
  assert.ok(curvedFraction > 0.2, `curved track puts it at ${curvedFraction}`);
});

test("resolution below the auto white point is fine enough to tune with", () => {
  // Steps available between black and the auto white point. Contrast previously went from
  // usable to a black frame in ONE step, so this bound is the regression guard.
  const steps = api.displayCountToSlider(STATS.autoMax, STATS);
  assert.ok(steps > 150, `only ${steps} steps below the auto white point`);
});

test("out-of-range positions clamp instead of extrapolating", () => {
  assert.equal(api.displaySliderToCount(-50, STATS), STATS.min);
  assert.equal(api.displaySliderToCount(99999, STATS), STATS.max);
  assert.equal(api.displayCountToSlider(-1, STATS), 0);
  assert.equal(api.displayCountToSlider(1e9, STATS), api.DISPLAY_SLIDER_STEPS);
});

test("a degenerate channel range cannot produce NaN slider positions", () => {
  const flat = { min: 7, max: 7 };
  assert.equal(api.displayCountToSlider(7, flat), 0);
  assert.equal(api.displaySliderToCount(500, flat), 7);
});

test("counts are formatted at a precision that matches their magnitude", () => {
  assert.equal(api.formatCount(6684), "6684");
  assert.equal(api.formatCount(216.4), "216");
  assert.equal(api.formatCount(12.34), "12.3");
  assert.equal(api.formatCount(0.128), "0.13");
  assert.equal(api.formatCount(Number.NaN), "—");
});

test("display sliders react while dragging, not only on release", () => {
  // "change" on a range input fires on mouse release. Binding these to "change" was the
  // reason the controls felt unresponsive next to Image Studio, so pin the listener type.
  const start = app.indexOf("function bindBlotViewerControls");
  const end = app.indexOf("function onStraightenInput");
  assert.ok(start !== -1 && end > start, "binding anchors moved; this test would be vacuous");
  const binding = app.slice(start, end);
  assert.ok(binding.includes('"displayMin700"'), "slider ids moved");
  const sliderBlock = binding.slice(binding.indexOf('"displayMin700"'));
  assert.match(sliderBlock, /addEventListener\("input"/);
  assert.doesNotMatch(sliderBlock.slice(0, sliderBlock.indexOf("autoWindow")),
    /addEventListener\("change"/);
});

// ── Pass 2: the live local preview ───────────────────────────────────────────────
const pixelApi = new Function(`
  ${grabConst("PIXEL_BUFFER_MAGIC")}
  ${grabConst("PIXEL_BUFFER_HEADER_BYTES")}
  ${grabConst("PIXEL_QUANTIZE_CURVE")}
  ${grabConst("QUANTIZE_MAX")}
  ${grab("parsePixelBuffer")}
  ${grab("decodeQuantized")}
  return { parsePixelBuffer, decodeQuantized, PIXEL_QUANTIZE_CURVE, QUANTIZE_MAX };
`)();

function makePixelBuffer({ magic = "WBP1", width = 2, height = 2, channels = 2, trailing = 0 } = {}) {
  const pixels = width * height;
  const bytes = new ArrayBuffer(32 + pixels * 2 * 2 + trailing);
  const view = new DataView(bytes);
  for (let i = 0; i < 4; i += 1) view.setUint8(i, magic.charCodeAt(i));
  view.setUint32(4, width, true);
  view.setUint32(8, height, true);
  view.setUint32(12, channels, true);
  view.setFloat32(16, 0, true);      // 700nm min
  view.setFloat32(20, 1000, true);   // 700nm max
  view.setFloat32(24, 5, true);      // 800nm min
  view.setFloat32(28, 500, true);    // 800nm max
  return bytes;
}

test("the client quantization curve matches the backend's", async () => {
  // The two constants are the contract for /channel-pixels. If they drift, the live preview
  // renders wrong intensities while the server render is right — a confusing flicker rather
  // than a clean failure, so assert them against each other directly.
  const py = await readFile(new URL("../backend/app.py", import.meta.url), "utf8");
  const match = py.match(/^PIXEL_QUANTIZE_CURVE = ([\d.]+)/m);
  assert.ok(match, "backend PIXEL_QUANTIZE_CURVE not found");
  assert.equal(Number(match[1]), pixelApi.PIXEL_QUANTIZE_CURVE);
});

test("quantized samples decode back to raw counts", () => {
  const scale = { min: 0, max: 6684 };
  assert.equal(pixelApi.decodeQuantized(0, scale), 0);
  assert.equal(pixelApi.decodeQuantized(pixelApi.QUANTIZE_MAX, scale), 6684);
  // The curve must put fine resolution at the low end, where band-hunting happens.
  const step = pixelApi.decodeQuantized(1, scale) - pixelApi.decodeQuantized(0, scale);
  assert.ok(step < 0.01, `low-end step is ${step} counts`);
});

test("a pixel buffer parses into two addressable channels", () => {
  const parsed = pixelApi.parsePixelBuffer(makePixelBuffer({ width: 3, height: 4 }));
  assert.equal(parsed.width, 3);
  assert.equal(parsed.height, 4);
  assert.equal(parsed.channels[700].data.length, 12);
  assert.equal(parsed.channels[800].data.length, 12);
  assert.deepEqual(parsed.channels[700].scale, { min: 0, max: 1000 });
  assert.deepEqual(parsed.channels[800].scale, { min: 5, max: 500 });
});

test("a malformed pixel buffer is rejected rather than misread", () => {
  // Misreading the header would paint garbage over the blot, so every field is checked.
  assert.throws(() => pixelApi.parsePixelBuffer(makePixelBuffer({ magic: "XXXX" })), /format/);
  assert.throws(() => pixelApi.parsePixelBuffer(makePixelBuffer({ channels: 1 })), /two channels/);
  assert.throws(() => pixelApi.parsePixelBuffer(makePixelBuffer({ trailing: 8 })), /size mismatch/);
  assert.throws(() => pixelApi.parsePixelBuffer(new ArrayBuffer(8)), /truncated/);
  assert.throws(() => pixelApi.parsePixelBuffer(makePixelBuffer({ width: 0 })), /size mismatch/);
});

test("the local preview is abandoned once the server render arrives", () => {
  // The server composite is what exports use, so the preview must be transient. If
  // previewDirty were never cleared, the canvas could keep showing the approximate local
  // recomposite indefinitely.
  const loader = app.slice(app.indexOf("async function loadCanvasImage"));
  const onload = loader.slice(0, loader.indexOf("const canPreserveView"));
  assert.match(onload, /previewDirty = false/);
});

test("a stale pixel buffer cannot be previewed after a rotation change", () => {
  // The buffer is rotation-baked, so reusing it at a new angle would draw the blot out of
  // register with the boxes drawn on top of it.
  const straighten = app.slice(app.indexOf("function applyStraighten"));
  const body = straighten.slice(0, straighten.indexOf("\n}\n"));
  assert.match(body, /pixelBuffer = null/);
  assert.match(body, /loadChannelPixels/);
});

test("quantification never reads the display window", () => {
  // The window is view-only. If an extract payload ever grew display keys, a user could
  // change their numbers by dragging a slider.
  const start = app.indexOf("async function extractSignalsForBoxes");
  assert.notEqual(start, -1, "extract function renamed; this test would be vacuous");
  const extractBlock = app.slice(start);
  const payload = extractBlock.slice(0, extractBlock.indexOf("apiFetch") + 2000);
  assert.doesNotMatch(payload, /displayMin|displayMax|gamma/);
});
