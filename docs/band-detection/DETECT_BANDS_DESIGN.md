# `/detect-bands` — backend band detection (design sketch)

Sketch only. Nothing in the repo is modified. Companion files in this folder:

| File | Becomes | Purpose |
|---|---|---|
| `detect_bands_sketch.py` | new functions + route in `backend/app.py` | projection detector + Flask endpoint |
| `otsu_cc_sketch.py` | more functions in `backend/app.py` | 2nd detector: Otsu + connected components |
| `api_detect-bands_wrapper.py` | `api/detect-bands.py` | Vercel serverless wrapper |
| `detect-bands_frontend_sketch.js` | additions to `frontend/app.js` | fetch, preview, commit, live lane re-threshold |
| `calibrate_sensitivities.py` | dev tool (not shipped) | tune `SENSITIVITY_LEVELS` against real blots |

Both detectors emit the identical candidate/box shape and share the same prepped image
(`_normalize` → `_orient_polarity` → deskew), so they populate the same chooser. Projection
gives uniform lane-width boxes; Otsu gives tight per-blob boxes and makes no grid assumption.
Verified on a synthetic 6-lane blot: projection 6/10/18 bands, Otsu 6/9/10/11 by sensitivity.

---

## Data flow

```
user clicks "Detect bands"
        │
        ▼
frontend detectBands()  ──POST /detect-bands──►  backend detect_bands()
  {blot, channel,                                   read_temp_file → read_raw_channel   (native 16-bit TIF)
   compositeW/H,                                    downsample → normalize → polarity
   sensitivities}                                   for each sensitivity level:
        ▲                                              detect lanes (vertical projection)
        │                                              detect bands per lane (horizontal projection + peaks)
        │                                              scale boxes WORKING-grid → COMPOSITE space
        └──────────── candidates[] ◄──────────────  jsonify
        │
        ▼
preview a candidate (dashed overlay, non-destructive)
        │
        ▼
accept → createCanvasBox() → canvasState.boxes → extractSignalsForBoxes()   (existing /extract path)
```

The key invariant: **detection emits boxes in composite coordinates**, so the output is
indistinguishable from hand-drawn boxes and flows through the untouched extraction /
normalization / fold-change pipeline. `/extract` still rescales composite → native at
measure time (`scale_box`, app.py:1816).

---

## JSON contract

### Request — `POST /detect-bands` (prod: `/api/detect-bands`)
```jsonc
{
  "sessionId": "…",                 // reuse request_session_id()
  "blot": { "id": "…", "files": { "700": {…}, "800": {…} } },  // same shape /extract gets
  "channel": "700",                 // "700" | "800" — detect on the quant channel
  "compositeWidth": 1200,           // optional; natural size of the composite the UI drew on
  "compositeHeight": 900,           // optional; lets backend emit boxes in composite space
  "sensitivities": ["conservative", "balanced", "aggressive"],  // optional; subset, order preserved
  "rotationDeg": 0,                 // optional; omit to auto-deskew, or pin an angle
  "laneThreshold": 0.15             // optional; overrides lane split (from the live laneProfile slider)
}
```

### Response
```jsonc
{
  "imageWidth": 1200,               // coordinate space the boxes are in (composite)
  "imageHeight": 900,
  "channel": "700",
  "rotationDeg": 1.5,               // deskew applied; frontend + /extract must match it
  "candidates": [
    {
      "id": "balanced",
      "label": "Balanced",
      "laneCount": 8,
      "bandCount": 24,
      "truncated": false,           // true if >200 bands were found and trimmed to strongest
      "boxes": [
        { "x": 140.0, "y": 88.0, "w": 46.0, "h": 22.0, "lane": 0, "band": 0, "score": 0.83 }
        // … composite-space rectangles, ready for createCanvasBox()
      ]
    }
    // … one entry per requested sensitivity ("sets of bands to choose between")
  ],
  "laneProfile": [0.02, 0.05, …]    // coarse vertical projection (≤512 samples) for optional
                                    // client-side live re-thresholding without a round-trip
}
```

---

## Algorithm (projection-profile, numpy only)

1. **Decode** the native channel via `read_raw_channel()` — same validated pixels as `/extract`.
2. **Downsample** to ≤1600 px on the long axis (`DETECT_MAX_DIM`). Profiles don't need full
   res; this bounds cost regardless of image size. Boxes scale back up to composite at the end.
3. **Normalize** robustly (5th/99.5th percentile stretch → 0..1).
4. **Polarity**: if median > 0.5 it's a dark-band-on-light capture → invert, so bands are
   always the high values. (LI-COR Odyssey is already bright-on-dark.)
4b. **Deskew** (optional): try small rotations and keep the one that maximizes lane-projection
   sharpness (`_estimate_skew`), or honor a client `rotationDeg`. See the Rotation note below —
   the angle must be applied to display + `/extract` too, because the box model is axis-aligned.
5. **Lanes** = vertical projection (mean per column), smoothed; contiguous spans above a
   fraction of the profile's dynamic range.
6. **Bands per lane** = horizontal projection over that lane's columns; peaks via a hand-rolled
   **prominence** finder (no scipy) with a **minimum spacing**. Box y-extent = descend to
   half-prominence (valley-to-valley proxy); box x-extent = lane width.
7. **Sensitivity → candidate sets**: the same pass at 3 prominence/threshold levels →
   conservative / balanced / aggressive. That's the "choose between them" UX.
8. **Cap** at 200 boxes/candidate (matches `MAX_CANVAS_BOXES`); keep the strongest, set `truncated`.

**Second method (sketched in `otsu_cc_sketch.py`)**: Otsu global threshold → binary mask →
connected components (vectorized label propagation, no scipy) → per-blob bounding boxes,
grouped into lanes by x-overlap. Offered as extra candidates (`id: "otsu-balanced"`, …) in the
same chooser. Better for non-gridded / rotated / smeared layouts; for uneven background, swap
the global Otsu for a per-tile adaptive threshold (noted in that file). Same output shape.

---

## Integration points (what changes, minimally)

- **`backend/app.py`**: add the detection helpers + `detect_bands()` route (double-decorated
  `/detect-bands` and `/api/detect-bands`, `@limiter.limit("15 per minute")`).
- **`api/detect-bands.py`**: 3-line wrapper (copy of `api/extract.py`).
- **`frontend/app.js`**:
  - `detectBands()`, `previewDetectionCandidate()`, `acceptDetectedCandidate()`.
  - one small block in `renderCanvas()` (app.js:4768) to draw `canvasState.detectionPreview`.
  - a "Detect bands" button + chooser in `boxToolControlsHtml()` (app.js:4401).
- **No CSP change** — `connect-src 'self'` already covers same-origin `/api` (vercel.json).
- **No changes** to extraction, normalization, fold-change, coordinate scaling, or the box model.

---

## Security notes (matches the app's trust model — see SECURITY.md / `webtrustyrusty`)

- Reuses `decode_validated_tif` / `sanitize_tif_pixels` hardening via `read_raw_channel`; no new
  TIFF-parsing path and no in-browser pixel path.
- Session-scoped temp files only (`read_temp_file(descriptor, session_id, …)`), same as `/extract`.
- Rate-limited; validate `channel ∈ {700,800}` and `sensitivities` against an allowlist.
- Read-only: produces coordinates, never writes or persists anything.
- Net new attack surface vs `/extract` is essentially nil — worth a `webtrustyrusty` pass anyway.

---

## Tradeoffs / limitations to decide on

- **Fidelity vs interactivity**: backend detects on true pixels (the win) but a *re-detect* at a
  new prominence is a round-trip. Mitigated two ways, both implemented in the frontend sketch:
  (1) the 3 ready-made candidate sets mean most tuning is just picking one, and (2) `laneProfile`
  drives live client-side LANE re-thresholding (`lanesFromProfile`, a port of `_detect_lanes`) —
  the lane slider previews instantly with no fetch; only band-level re-detection at the chosen
  `laneThreshold` costs a round-trip.
- **Projection profiles assume roughly vertical lanes / horizontal bands.** Handled by the
  deskew step (`_estimate_skew` / `_rotate`) for small tilts; heavy smears still want the
  connected-components method. NOTE the axis-aligned-box constraint in the Rotation section.
- **Parameters (`SENSITIVITY_LEVELS`) are guesses** — they must be tuned against real scans;
  expose the knobs while calibrating.
- **Touching bands** in a lane merge into one peak; splitting them reliably is a harder problem
  (deconvolution / watershed) and out of scope for v1.

---

## Rotation / deskew — the one real catch

The box model is **axis-aligned** `{x, y, w, h}` (`createCanvasBox`, app.js:5229). A box found in
a rotated frame is a *tilted* rectangle back in the original composite, which the model can't
represent. So rotation cannot be a detection-only trick — it must be **one angle applied
identically to display + detection + extraction**:

1. `/detect-bands` deskews internally and returns `rotationDeg` (`_estimate_skew` + `_rotate`).
2. The frontend rotates the **canvas display** by that angle (a rotate on top of the pan/zoom
   transform at app.js:4780), so the user sees a straight blot and boxes stay axis-aligned.
3. `/extract` rotates the **native array** by the same angle before slicing (one rotate in the
   `extract_box_signal` caller), so the measured pixels match what the user sees.

If you skip step 3, detected/edited boxes would measure slightly-off pixels on tilted blots.
The alternative is to let the user rotate in the UI first and add the angle to
`/render-composite` — simpler UX plumbing, same "one consistent angle" rule.

---

## Calibrating `SENSITIVITY_LEVELS` (with your test blots)

`scratchpad/calibrate_sensitivities.py` decodes real blot TIFs, runs the detector, prints
lane/band counts + a prominence sweep, and renders overlay PNGs to `calibration-out/`.

Validated already on a synthetic 6-lane blot (all functions run; boxes land on bands). That
run surfaced a real fix now baked into the defaults: **`lane_threshold` must stay constant
across levels** — lowering it for "aggressive" merged all lanes into one. Only `prominence`
(and `min_distance_frac`) should vary by sensitivity.

Loop: drop test data → run the harness → read the PNGs → adjust `SENSITIVITY_LEVELS` → re-run.
