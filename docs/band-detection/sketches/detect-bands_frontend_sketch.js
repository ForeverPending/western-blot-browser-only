/*
 * SKETCH ONLY — not wired into frontend/app.js.
 *
 * Frontend side of the /detect-bands feature. Three pieces:
 *   1. detectBands()            -> POST to the backend, get candidate sets back
 *   2. previewDetection...()    -> show a candidate non-destructively before commit
 *   3. acceptDetectedCandidate()-> commit chosen boxes (mirrors addCenteredBox exactly)
 *
 * All the `// reuse:` calls already exist in frontend/app.js. Boxes come back in
 * COMPOSITE coordinates (canvasState.imageWidth/Height space), so they drop straight
 * into createCanvasBox() with no mapping — same space the user draws in.
 *
 * CSP note: connect-src 'self' (vercel.json) already covers same-origin /api, so no
 * header change is needed for this request.
 */

// ─── 1. Fetch candidate band sets ───────────────────────────────────────────────

async function detectBands(blotId, { sensitivities, laneThreshold, rotationDeg } = {}) {
  const channel = document.getElementById("quantChannel")?.value ?? "700";  // reuse: quant channel dropdown
  const result = await apiJson(apiUrl("/detect-bands"), {                    // reuse: apiUrl, apiJson
    method: "POST",
    headers: { "Content-Type": "application/json" },
    timeoutMs: SIGNAL_EXTRACTION_TIMEOUT_MS,                                 // reuse: existing timeout const
    body: JSON.stringify({
      sessionId: activeSessionId,                                           // reuse: activeSessionId
      blot: blotById(blotId),                                              // reuse: blotById
      channel,
      // Same coordinate contract as /extract (app.js:5594-5599): tell the backend
      // the composite size so it emits boxes in the space we draw in.
      compositeWidth: canvasState.imageWidth || undefined,
      compositeHeight: canvasState.imageHeight || undefined,
      sensitivities,   // optional; omit to get all three (conservative/balanced/aggressive)
      laneThreshold,   // optional; the value the user tuned live via the laneProfile slider
      rotationDeg,     // optional; omit to auto-deskew
    }),
  }, "Band detection failed.");
  // -> { imageWidth, imageHeight, channel, rotationDeg,
  //      candidates: [{id,label,method,laneCount,bandCount,truncated,boxes:[...]}], laneProfile }

  // Cache the profile so lane re-thresholding can run client-side with no round-trip.
  canvasState.detection = {
    laneProfile: result.laneProfile || [],
    imageWidth: result.imageWidth || canvasState.imageWidth,
  };
  return result;
}


// ─── 2. Non-destructive preview ──────────────────────────────────────────────────
//
// Stash the focused candidate's boxes on canvasState and let renderCanvas() draw them
// in a distinct dashed color BEFORE the user commits. This needs a tiny addition to
// renderCanvas() (app.js:4768) — a block like the one below, after the real boxes loop:
//
//   if (canvasState.detectionPreview) {
//     ctx.save();
//     ctx.strokeStyle = themeToken("--annotation-preview") || themeToken("--primary");
//     ctx.setLineDash([6 / canvasState.zoom, 4 / canvasState.zoom]);
//     ctx.lineWidth = 1.5 / canvasState.zoom;
//     canvasState.detectionPreview.forEach(b => ctx.strokeRect(b.x, b.y, b.w, b.h));
//     ctx.restore();
//   }

function previewDetectionCandidate(candidate) {
  canvasState.detectionPreview = candidate ? candidate.boxes : null;
  renderCanvas();  // reuse: renderCanvas
}


// ─── 3. Commit the chosen candidate ──────────────────────────────────────────────
//
// This is the integration point. It is addCenteredBox() (app.js:4883) generalized to
// many boxes: build via createCanvasBox(), push, then extract signals through the
// existing backend path so detected boxes behave identically to hand-drawn ones.

function acceptDetectedCandidate(candidate, blotId = canvasState.currentBlotId, { replace = true } = {}) {
  if (!candidate || !candidate.boxes.length) return;

  if (replace) {
    canvasState.boxes = [];                          // matches "Clear all" then add
  }

  // Respect MAX_CANVAS_BOXES (app.js:17) via remainingBoxSlots() (app.js:5300).
  const slots = remainingBoxSlots();                 // reuse: remainingBoxSlots
  const chosen = candidate.boxes.slice(0, slots);
  if (chosen.length < candidate.boxes.length || candidate.truncated) {
    showUserMessage(`Showing ${chosen.length} strongest bands (max ${MAX_CANVAS_BOXES} per scan).`); // reuse: showUserMessage
  }

  const newBoxes = chosen.map(b => createCanvasBox({    // reuse: createCanvasBox (also clamps to image)
    x: b.x, y: b.y, w: b.w, h: b.h,
    laneName: `Lane ${b.lane + 1}`,
  }));

  canvasState.boxes.push(...newBoxes);
  canvasState.selectedBoxIndex = canvasState.boxes.length - 1;
  canvasState.detectionPreview = null;

  renderCanvas();                                       // reuse: renderCanvas
  renderBoxList(blotId);                                // reuse: renderBoxList
  void extractSignalsForBoxes(blotId, newBoxes, { alertOnError: false });  // reuse: extractSignalsForBoxes
}


// ─── Wiring sketch (UI) ──────────────────────────────────────────────────────────
//
// Add a "Detect bands" button next to Add centered box (boxToolControlsHtml, app.js:4401)
// and a small chooser. Rough flow:
//
//   async function onDetectBandsClick(blotId) {
//     const result = await detectBands(blotId);
//     // Render result.candidates as a segmented control (like modePan/modeDraw).
//     // On focus/hover of a candidate:      previewDetectionCandidate(candidate)
//     // On "Accept":                        acceptDetectedCandidate(candidate, blotId)
//     // On "Cancel":                        previewDetectionCandidate(null)
//   }


// ─── laneProfile: live client-side lane re-thresholding (no round-trip) ──────────
//
// The backend returns `laneProfile` (already-smoothed vertical projection, ≤512 samples).
// As the user drags a "lane threshold" slider we recompute lane spans IN JS and preview them
// instantly — this is a straight port of the backend `_detect_lanes` so client and server
// agree on where lanes fall. IMPORTANT: this re-thresholds LANES only. Bands within a lane
// need the 2-D pixels (backend); when the user settles on a threshold, call detectBands()
// again with { laneThreshold } to get fresh band boxes segmented by that value.

// Port of detect_bands_sketch.py:_detect_lanes, operating on the returned 1-D profile.
// `thresholdFrac` is the slider value (0..1). Returns composite-space lane rects [{x, w}].
function lanesFromProfile(thresholdFrac) {
  const det = canvasState.detection;
  if (!det || !det.laneProfile.length) return [];
  const prof = det.laneProfile;
  const n = prof.length;
  const imgW = det.imageWidth || canvasState.imageWidth || n;
  const pxPerSample = imgW / n;

  let lo = Infinity, hi = -Infinity;
  for (const v of prof) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (hi <= lo) return [{ x: 0, w: imgW }];
  const cut = lo + thresholdFrac * (hi - lo);

  // Contiguous runs above the cut, dropping lanes narrower than ~1% of width.
  const minWidthSamples = Math.max(1, Math.round(n * 0.01));
  const lanes = [];
  let start = null;
  for (let i = 0; i <= n; i++) {
    const above = i < n && prof[i] >= cut;
    if (above && start === null) start = i;
    else if (!above && start !== null) {
      if (i - start >= minWidthSamples) {
        lanes.push({ x: start * pxPerSample, w: (i - start) * pxPerSample });
      }
      start = null;
    }
  }
  return lanes.length ? lanes : [{ x: 0, w: imgW }];
}

// Live preview while dragging: draw full-height lane columns. Needs a small block in
// renderCanvas() (app.js:4768) that strokes canvasState.lanePreview (full image height):
//
//   if (canvasState.lanePreview) {
//     ctx.save();
//     ctx.strokeStyle = themeToken("--annotation-preview") || themeToken("--primary");
//     ctx.setLineDash([2 / canvasState.zoom, 4 / canvasState.zoom]);
//     ctx.lineWidth = 1 / canvasState.zoom;
//     const H = canvasState.imageHeight;
//     canvasState.lanePreview.forEach(l => ctx.strokeRect(l.x, 0, l.w, H));
//     ctx.restore();
//   }

function onLaneThresholdInput(event) {
  const thresholdFrac = Number(event.target.value);   // slider in [0,1]
  canvasState.laneThreshold = thresholdFrac;
  canvasState.lanePreview = lanesFromProfile(thresholdFrac);  // instant, no fetch
  renderCanvas();                                             // reuse: renderCanvas
}

// When the user is happy with the lane split, re-detect BANDS at that threshold (one round-trip).
async function redetectBandsAtLaneThreshold(blotId) {
  canvasState.lanePreview = null;
  const result = await detectBands(blotId, { laneThreshold: canvasState.laneThreshold });
  // hand result.candidates back to the chooser exactly as in onDetectBandsClick
  return result;
}
