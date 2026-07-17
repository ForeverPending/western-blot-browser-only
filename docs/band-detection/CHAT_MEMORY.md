# Session memory — band-detection feature design

Handoff notes for the "auto-detect band sets → choose → draw/edit boxes" feature for the
western-blot browser-only app. All artifacts referenced live in this same scratchpad folder.
Nothing in the git repo was modified — this was a design + sketch session ("don't change code").

> ⚠️ This file is in the session scratchpad (`/private/tmp/...`), which is temporary. Move it
> into the repo (e.g. next to the sketches, or `docs/`) once the TCC lockout below is cleared.

---

## The goal

Add computer-vision band detection: given a blot image, identify candidate **sets of bands**,
let the user **choose between them**, draw boxes, and edit them — feeding into the existing
quantification pipeline.

## Key decisions (and why)

1. **Detection runs on the BACKEND, not in-browser.** The browser only holds a lossy 8-bit JPEG
   composite (percentile-normalized, brightness/contrast/gamma-adjusted, JPEG-compressed). The
   backend has the native 16-bit TIF via `read_raw_channel()` — the *same* validated pixels
   `extract_box_signal()` quantifies from. Detecting on those is higher fidelity and reuses the
   `decode_validated_tif` hardening (attack surface ≈ existing `/extract`).
2. **Coordinate contract = composite space.** Detection emits boxes in composite coordinates, so
   output is indistinguishable from a hand-drawn box and flows through the untouched
   extraction/normalization/fold-change pipeline. `/extract` already rescales composite→native.
3. **"Sets to choose between" = one detection per sensitivity level** (conservative/balanced/
   aggressive), returned together; the user picks one.
4. **Two detector methods offered as candidates**, same output shape, same chooser:
   - Projection-profile (primary)
   - Otsu + connected components (alternative; grid-free)
5. **Interactivity without losing fidelity**: return the 3 ready-made sets + a `laneProfile` for
   live client-side LANE re-thresholding (no round-trip); only band-level re-detect costs a fetch.

---

## Artifacts in this folder

| File | Role |
|---|---|
| `DETECT_BANDS_DESIGN.md` | Master design doc: data flow, JSON contract, integration, security, tradeoffs |
| `detect_bands_sketch.py` | Projection-profile detector + `/detect-bands` Flask route (→ `backend/app.py`) |
| `otsu_cc_sketch.py` | Otsu + connected-components detector (2nd candidate method) |
| `api_detect-bands_wrapper.py` | Vercel wrapper (→ `api/detect-bands.py`) |
| `detect-bands_frontend_sketch.js` | Frontend: fetch, preview, commit, live lane re-threshold |
| `calibrate_sensitivities.py` | Dev tool: decode real blot TIFs, sweep params, render overlay PNGs |
| `_synth_test.py`, `_synth_otsu_test.py` | Synthetic-blot smoke tests (validation) |
| `.venv/` | Throwaway venv (numpy/pillow/tifffile) — substitutes for the locked repo venv |

---

## How the two detectors work

**Projection-profile** (`detect_bands_sketch.py`):
normalize (5/99.5 pct) → polarity (bright=band) → optional deskew → vertical projection ⇒ lanes
(contiguous columns above `lane_threshold`) → per-lane horizontal projection ⇒ bands (hand-rolled
prominence peak finder, no scipy) → box y-extent by half-prominence descent, x-extent = lane width.
Boxes are uniform lane-width.

**Otsu + connected components** (`otsu_cc_sketch.py`):
Otsu global threshold → binary mask → connected components (vectorized label propagation, no scipy)
→ per-blob bounding boxes filtered by area/fill → grouped into lanes by x-overlap. Boxes are tight
per-blob. No grid assumption (better for irregular/rotated/smeared). Note in-file: swap global Otsu
for per-tile adaptive threshold if background is uneven.

## The three sensitivity sets

Same algorithm, varying **band `prominence`** only (lower = more/fainter bands):
`conservative` 0.20 · `balanced` 0.10 · `aggressive` 0.05 (normalized profile units).

**Calibration finding (baked into defaults):** `lane_threshold` must be held CONSTANT across
levels. Originally it was lowered for "aggressive", which merged all lanes into one giant lane —
caught visually on the synthetic test. Only prominence (+ min band spacing) varies by level.

## laneProfile live re-thresholding

Backend returns `laneProfile` (smoothed vertical projection, ≤512 samples). Frontend
`lanesFromProfile(thresholdFrac)` is a JS port of `_detect_lanes` — a lane slider previews lane
columns instantly with NO fetch. Re-detecting BANDS at the chosen value calls `detectBands({
laneThreshold })` (one round-trip; bands need 2-D pixels). Backend route reads `laneThreshold`
and overrides the per-level lane split.

## Rotation / deskew — the one real catch

Added `_rotate()` + `_estimate_skew()` (auto-deskew: pick the small angle maximizing lane
sharpness) and a `rotationDeg` field. **Catch:** the box model is axis-aligned `{x,y,w,h}`, so a
box found in a rotated frame is a tilted rectangle in the original — unrepresentable. Rotation must
be ONE angle applied identically to **display + detection + `/extract`** (extract rotates the native
array before slicing). Skipping the extract side would measure slightly-off pixels on tilted blots.

---

## Validation (synthetic 6-lane blot; overlays viewed as PNGs)

- Projection: 6 lanes; bands **10 / 18 / 18** (conservative/balanced/aggressive). Boxes centered on
  bands; conservative correctly skips the faintest.
- Otsu: 6 lanes; bands **9 / 10 / 11**. Tight per-blob boxes; fades out faint bands by threshold.
- Deskew estimated 0° on the (untilted) synthetic. Both detectors run clean after all edits.

Could NOT yet calibrate on real data — the user's test ZIPs are under the repo, which is
TCC-locked (see below).

---

## Integration points in the real codebase (verified this session)

- Boxes array: `canvasState.boxes` (`frontend/app.js:3473`); factory `createCanvasBox` (`:5229`).
- Commit pattern to mirror: `addCenteredBox` (`app.js:4883`) → push → `renderCanvas` (`:4768`) →
  `renderBoxList` → `extractSignalsForBoxes` (`:5560`). Cap: `MAX_CANVAS_BOXES = 200` (`app.js:17`).
- Extraction route: `extract_payload_signals` (`backend/app.py:1699`); core `extract_box_signal`
  (`:1846`); composite→native scaling `resolve_composite_dimensions` (`:1800`) / `scale_box` (`:1816`).
- Reuse for the new route: `request_session_id`, `load_payload_blot` (`:1038`), `read_temp_file`,
  `read_raw_channel` (`:1829`), `PublicError`/`error_response`, `limiter`, `BLOT_FILE_LIMITS`.
- Vercel wrapper pattern: copy `api/extract.py` (3 lines).
- CSP: `connect-src 'self'` already covers same-origin `/api` — no header change needed.
- ZIP format the app ingests (from `parse_zip`/`find_channel_tif`): one top-level folder per blot,
  each with a `.txt` + a `*700*.tif` + a `*800*.tif` (channel matched by digits bounded by non-digits).

---

## Outstanding / next steps

1. **Restore repo access** (TCC lockout — see below), then:
   - Add `test_blots/` + `test-blots/` to `.gitignore`.
   - Point `calibrate_sensitivities.py` at the real ZIPs; tune `SENSITIVITY_LEVELS` / `OTSU_LEVELS`
     from the overlay PNGs.
2. Decide: fold Otsu into `/detect-bands` via a `method` param, or keep separate.
3. Implement the frontend UI (Detect button + candidate chooser like the Pan/Draw toggle; lane
   slider) and the small `renderCanvas()` preview blocks noted in the JS sketch.
4. On-canvas **resize handles** don't exist today (boxes are draw/nudge/match/align/duplicate only,
   `app.js:4401`). "Edit box sizes" via corner-drag is a separate small addition: hit-test handles
   in `onMouseDown` (`:4949`), add a `"resize"` interaction, re-extract on mouse-up.
5. Run the `webtrustyrusty` reviewer on the new route before merging.

---

## Environment status: `~/Documents` TCC lockout

Mid-session the macOS TCC protection on `~/Documents` blocked all repo reads/writes ("Operation
not permitted") for the Claude Code process — affecting my tools AND user-typed `!` commands (same
session shell). Fixes, fastest first: (1) fully quit & relaunch the terminal app hosting Claude
Code; (2) grant it Full Disk Access (or Files & Folders → Documents) in System Settings, relaunch;
(3) durable: move the repo out of `~/Documents`, e.g. `mv ~/Documents/western-blot-browser-only
~/dev/`. The scratchpad (`/private/tmp/...`) stayed accessible throughout.
