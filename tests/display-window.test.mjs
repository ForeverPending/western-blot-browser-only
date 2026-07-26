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

// ── Channel enable/disable ───────────────────────────────────────────────────────
function channelApi(state) {
  const canvasState = { channelStats: null, enabledChannels: { 700: true, 800: true }, ...state };
  const api = new Function("canvasState", `
    ${grabConst("ALL_CHANNELS")}
    ${grab("availableChannels")}
    ${grab("isChannelEnabled")}
    ${grab("enabledChannels")}
    ${grab("syncChannelAvailability")}
    return { availableChannels, isChannelEnabled, enabledChannels, syncChannelAvailability };
  `)(canvasState);
  return { api, canvasState };
}

const BOTH_STATS = { 700: { min: 0, max: 100 }, 800: { min: 0, max: 100 } };

test("both channels are assumed available until stats say otherwise", () => {
  // Guessing "one" before /channel-stats lands would briefly hide a channel that exists.
  const { api } = channelApi({ channelStats: null });
  assert.deepEqual(api.availableChannels(), ["700", "800"]);
  assert.deepEqual(api.enabledChannels(), ["700", "800"]);
});

test("a channel this blot lacks is never available or enabled", () => {
  const { api } = channelApi({ channelStats: { 800: { min: 0, max: 100 } } });
  assert.deepEqual(api.availableChannels(), ["800"]);
  assert.equal(api.isChannelEnabled("700"), false);
  assert.deepEqual(api.enabledChannels(), ["800"]);
});

test("a missing channel stays off even if state claims it is enabled", () => {
  // Defence in depth: enabledChannels() must never name a channel with no pixels behind it,
  // because it feeds both the render request and the quantify options.
  const { api, canvasState } = channelApi({
    channelStats: { 700: { min: 0, max: 100 } },
    enabledChannels: { 700: true, 800: true },
  });
  assert.deepEqual(api.enabledChannels(), ["700"]);
  api.syncChannelAvailability();
  assert.equal(canvasState.enabledChannels[800], false);
});

test("disabling one channel leaves the other enabled", () => {
  const { api, canvasState } = channelApi({ channelStats: BOTH_STATS });
  canvasState.enabledChannels[800] = false;
  assert.deepEqual(api.enabledChannels(), ["700"]);
  assert.equal(api.isChannelEnabled("700"), true);
  assert.equal(api.isChannelEnabled("800"), false);
});

test("a disabled channel maps to an all-zero LUT", () => {
  // This is how the local preview drops a channel, mirroring build_composite zeroing the
  // array server-side. A non-zero entry here would leave a ghost of the hidden channel.
  const lutBlock = app.slice(app.indexOf("function buildDisplayLut"));
  const body = lutBlock.slice(0, lutBlock.indexOf("\n}\n"));
  assert.match(body, /isChannelEnabled\(channel\)/);
  assert.match(body, /return new Uint8Array\(QUANTIZE_MAX \+ 1\)/);
});

test("the render request carries the enabled set", () => {
  const start = app.indexOf("function buildCompositePayload");
  assert.notEqual(start, -1, "payload builder renamed; this test would be vacuous");
  const payload = app.slice(start, app.indexOf("async function renderCompositeBlob"));
  assert.match(payload, /enabledChannels: enabledChannels\(\)/);
});

test("switching off the quantify channel re-measures the boxes", () => {
  // The user chose auto-switch: signals measured against the channel that just went away
  // are stale, so they have to be re-extracted rather than left on screen.
  const start = app.indexOf("function applyChannelEnablement");
  assert.notEqual(start, -1, "enablement handler renamed; this test would be vacuous");
  const body = app.slice(start, app.indexOf("// ── Display window", start));
  assert.match(body, /syncQuantChannelOptions\(\)/);
  assert.match(body, /extractSignalsForBoxes/);
  assert.match(body, /cancelDetection/);
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

// ── Per-channel Off / colour / B&W ───────────────────────────────────────────────
// These selects replaced a per-channel checkbox plus a separate global colour-mode
// dropdown. The invariant the UI can no longer express, and so has to be enforced here, is
// that two channels shown at once are always in the SAME look: the composite is one image.
function stubSelect() {
  const options = { off: { disabled: false }, color: { disabled: false }, grayscale: { disabled: false } };
  return {
    value: "off",
    disabled: false,
    querySelector(selector) {
      const match = /^option\[value="(\w+)"\]$/.exec(selector);
      return match ? options[match[1]] ?? null : null;
    },
  };
}

function channelModeApi(state) {
  const canvasState = {
    channelStats: BOTH_STATS,
    enabledChannels: { 700: true, 800: true },
    colorMode: "color",
    ...state,
  };
  const selects = { 700: stubSelect(), 800: stubSelect() };
  const bodies = { 700: { hidden: false }, 800: { hidden: false } };
  const status = { text: "" };
  const document = {
    getElementById(id) {
      const select = /^channelMode(700|800)$/.exec(id);
      if (select) return selects[select[1]];
      const body = /^channelBody(700|800)$/.exec(id);
      if (body) return bodies[body[1]];
      return null;
    },
  };
  const api = new Function("canvasState", "document", "status", `
    ${grabConst("ALL_CHANNELS")}
    ${grab("availableChannels")}
    ${grab("isChannelEnabled")}
    ${grab("enabledChannels")}
    ${grab("syncChannelAvailability")}
    ${grab("currentColorMode")}
    ${grab("syncChannelControls")}
    ${grab("onChannelModeChange")}
    // The shipped applyChannelEnablement() also re-measures boxes and reloads the server
    // render (covered by its own test above); only its sync half is in scope here.
    function applyChannelEnablement() { syncChannelAvailability(); syncChannelControls(); }
    function setChannelStatus(message) { status.text = message; }
    return { syncChannelControls, onChannelModeChange, currentColorMode };
  `)(canvasState, document, status);
  return { api, canvasState, selects, bodies, status };
}

test("both channels on are locked to the same look", () => {
  const { api, canvasState, selects } = channelModeApi();
  api.syncChannelControls();
  assert.equal(selects[700].value, "color");
  assert.equal(selects[800].value, "color");

  selects[800].value = "grayscale";      // user picks B&W on 800nm
  api.onChannelModeChange("800");
  assert.equal(canvasState.colorMode, "grayscale");
  assert.equal(selects[700].value, "grayscale", "700nm must follow 800nm, not stay in colour");
});

test("a single visible channel can still switch between colour and black & white", () => {
  const { api, canvasState, selects, bodies } = channelModeApi();
  selects[800].value = "off";
  api.onChannelModeChange("800");
  assert.equal(canvasState.enabledChannels[800], false);
  assert.equal(bodies[800].hidden, true, "a hidden channel's window sliders adjust nothing");

  selects[700].value = "grayscale";
  api.onChannelModeChange("700");
  assert.equal(canvasState.colorMode, "grayscale");
  assert.equal(canvasState.enabledChannels[700], true);
  assert.equal(selects[700].disabled, false, "the sole channel's look must stay changeable");
});

test("the last visible channel cannot be switched off", () => {
  const { api, canvasState, selects, status } = channelModeApi({
    enabledChannels: { 700: true, 800: false },
  });
  api.syncChannelControls();
  assert.equal(selects[700].querySelector('option[value="off"]').disabled, true);

  selects[700].value = "off";            // as if the disabled option were forced through
  api.onChannelModeChange("700");
  assert.equal(canvasState.enabledChannels[700], true);
  assert.equal(selects[700].value, "color", "the select snaps back rather than lying");
  assert.match(status.text, /at least one channel/i);
});

test("turning a channel back on applies its chosen look to both", () => {
  const { api, canvasState, selects } = channelModeApi({
    enabledChannels: { 700: true, 800: false },
    colorMode: "grayscale",
  });
  api.syncChannelControls();
  assert.equal(selects[800].value, "off");
  assert.equal(selects[700].value, "grayscale");

  selects[800].value = "color";          // "Green" on 800nm
  api.onChannelModeChange("800");
  assert.equal(canvasState.colorMode, "color");
  assert.equal(selects[700].value, "color", "the composite cannot be half colour, half B&W");
});

test("a channel this blot lacks has its whole select disabled", () => {
  const { api, selects } = channelModeApi({ channelStats: { 700: { min: 0, max: 100 } } });
  api.syncChannelControls();
  assert.equal(selects[800].disabled, true);
  assert.equal(selects[800].value, "off");
  assert.equal(selects[700].disabled, false);
  assert.equal(selects[700].querySelector('option[value="off"]').disabled, true);
});

// ── Settings that change what is MEASURED must re-measure ───────────────────────
// Every one of these controls silently invalidated the numbers already on screen: the box
// list kept showing values the current settings never produced, and saveScan() then wrote
// those values under the new settings' labels. Source-level assertions because the failure
// is a missing call, not bad arithmetic.
function handlerBody(startMarker, endMarker) {
  const start = app.indexOf(startMarker);
  assert.notEqual(start, -1, `missing ${startMarker}; this test would be vacuous`);
  const end = app.indexOf(endMarker, start);
  assert.notEqual(end, -1, `missing ${endMarker}; this test would be vacuous`);
  return app.slice(start, end);
}

test("rotating invalidates detection results found at the old angle", () => {
  // Candidates are composite-space coords from before the rotation. At 5°, content ~900px
  // from centre moves ~80px — several band heights — so committing them quantifies
  // membrane instead of band, with no error anywhere.
  const body = grab("applyStraighten");
  assert.match(body, /detectionRequestId\+\+/, "an in-flight detect must be invalidated too");
  assert.match(body, /cancelDetection\(blotId\)/, "stage B candidates must be dropped");
  assert.match(body, /redetectLanesAtThreshold/, "stage A lanes must be re-detected");
  assert.match(body, /extractSignalsForBoxes/, "committed boxes must be re-measured");
  // The bump orphans an in-flight detect whose handler is gated on the request id, so with
  // neither piece of state populated yet nothing else would ever clear "Finding lanes…".
  assert.match(body, /setDetectionStatus\(""\)/, "an orphaned detect must not leave a live status");
});

test("straightening to the angle already set does nothing at all", () => {
  // Everything applyStraighten does is rotation-derived. Without the guard, Reset at 0° (or
  // a drag that settles back where it started) blanks every box to "Extracting…", drops the
  // pixel buffer and fires three no-op requests — and blocks Save while they land.
  const body = grab("applyStraighten");
  assert.match(body, /if \(canvasState\.rotationDeg === deg\) return;/);
  // The guard has to come before any of the work it is guarding.
  assert.ok(body.indexOf("=== deg) return;") < body.indexOf("pixelBuffer = null"));
  assert.ok(body.indexOf("=== deg) return;") < body.indexOf("extractSignalsForBoxes"));
});

test("a lane list capped by the backend says so", () => {
  // The cap drops the rightmost lanes, so unannounced it reads as the right of the blot
  // having none — triggered by lowering the threshold, which is meant to find more.
  assert.match(grab("laneCapNote"), /lanesTruncated/);
  assert.match(grab("enterLaneEditing"), /laneCapNote\(result\)/);
  assert.match(app.slice(app.indexOf("async function redetectLanesAtThreshold")), /laneCapNote\(result\)/);
});

test("a held arrow key does not fire one extract per repeat", () => {
  // /extract is limited to 30/min; OS key-repeat burns that in about a second, and the 429s
  // mark boxes "Extraction failed" and block Save until a manual refresh.
  const body = grab("nudgeSelectedBox");
  assert.match(body, /clearTimeout\(nudgeExtractTimer\)/);
  assert.match(body, /setTimeout/);
  // Must re-resolve the target after the delay: the selection can move on mid-burst.
  assert.match(body, /const target = selectedBox\(\)/);
});

test("an unsubtracted lane is visible in the exports, not just live", () => {
  // The live chip and the save confirm only reach someone sitting at the screen. Once saved,
  // backgroundSignal 0 is all that is left, and that reads as a measured zero.
  assert.match(grab("scanAuditRows"), /"Background subtracted": lane\.backgroundMissing \? "No" : "Yes"/);
  assert.match(grab("scanAuditLaneRowHtml"), /lane\.backgroundMissing/);
});

test("changing the quantify channel re-measures committed boxes", () => {
  // Otherwise saveScan() writes 700nm signals under channel:"800", and a box drawn after
  // the switch is genuinely 800nm — one scan measured on two channels.
  const body = handlerBody(
    'document.getElementById("quantChannel")?.addEventListener',
    "// ─── Blot canvas engine",
  );
  assert.match(body, /extractSignalsForBoxes\(blotId, canvasState\.boxes/);
});

test("changing the background method re-measures committed boxes", () => {
  // Sides / border px / stat decide what is SUBTRACTED. Redrawing the dotted overlay alone
  // leaves every displayed signal attributed to a method that did not produce it, and a
  // later single-box re-extract puts one lane on a different method than its neighbours.
  const body = handlerBody(
    '["backgroundAxis", "backgroundBorderWidth", "backgroundStat"]',
    "// Band detection: one delegated handler",
  );
  assert.match(body, /extractSignalsForBoxes\(blotId, canvasState\.boxes/);
});

test("an unsubtracted box is flagged, gated at save, and kept through a round trip", () => {
  // backendSignal 0 does not distinguish "measured a zero background" from "never measured
  // one", so the flag is the only thing carrying that difference to the user and the export.
  assert.match(grab("boxSignalHtml"), /backgroundMissing/);
  const save = handlerBody("const unsubtractedBoxes", "const scan = {");
  assert.match(save, /confirmUserAction/, "saving an unsubtracted lane must be a decision");
  assert.match(handlerBody("lanes: canvasState.boxes.map", "if (!blotState.scans[blotId])"),
    /backgroundMissing: signal\.backgroundMissing === true/);
  assert.match(grab("normalizeScan"), /backgroundMissing: lane\.backgroundMissing === true/);
});

test("colour mode has a single source of truth", () => {
  // The global "Color mode" dropdown is gone; a stray reader of it would silently fall back
  // to "color" and export a render that does not match the screen.
  assert.doesNotMatch(app, /id="colorMode"/);
  assert.doesNotMatch(app, /getElementById\("colorMode"\)/);
});
