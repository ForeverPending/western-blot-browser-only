# Band detection

Design record and prototypes for the automatic band-detection feature
(`/detect-bands`). The projection-profile detector, its route, the Vercel wrapper,
and the frontend UI are **implemented**; the Otsu method and parameter calibration
are **not yet done**. The `sketches/` files are the original prototypes kept for
reference — some are now superseded by the shipped code (see the map below), so their
"SKETCH ONLY — not wired in" headers are historical.

## Status

Implemented:

- **Projection-profile detector + `/detect-bands` route** — `backend/app.py` (the
  `# Automatic band detection` block; `detect_bands()` view, double-decorated
  `/detect-bands` + `/api/detect-bands`, rate-limited 15/min).
- **Vercel wrapper** — `api/detect-bands.py`.
- **Frontend UI** — `frontend/app.js` (the "Detect bands" section in the blot viewer:
  candidate chooser, non-destructive canvas preview, accept/cancel, live lane-split
  slider) and `frontend/styles.css` (`.detection-*`, `--annotation-preview`).
- **Tests** — `backend/test_security.py::test_detect_bands_*`.

Not yet done:

- **Otsu + connected-components detector** (2nd candidate method) — prototype only, in
  `sketches/otsu_cc_sketch.py`. Decision pending: fold into `/detect-bands` via a
  `method` param, or keep separate.
- **Calibrating `SENSITIVITY_LEVELS`** against real blots — the shipped values in
  `backend/app.py` are the prototype defaults. Use `sketches/calibrate_sensitivities.py`
  (see "Calibration" below).
- **Rotation / deskew** — implemented but gated off (`DETECT_ENABLE_DESKEW = False`).
  See "The one real catch" below.

## Sketch → shipped code

| `sketches/` file | Shipped as |
|---|---|
| `detect_bands_sketch.py` | the detector helpers + `detect_bands()` route in `backend/app.py` |
| `api_detect-bands_wrapper.py` | `api/detect-bands.py` |
| `detect-bands_frontend_sketch.js` | the Detect-bands UI in `frontend/app.js` |
| `otsu_cc_sketch.py` | **not implemented** (future 2nd method) |
| `calibrate_sensitivities.py` | dev tool, not shipped (see below) |
| `_synth_test.py`, `_synth_otsu_test.py` | synthetic-blot smoke tests, not shipped |

Some helper names were renamed when porting into `backend/app.py` to avoid collisions:
`_normalize` → `_normalize_for_detect`, `_rotate` → `_rotate_array`, `_smooth` →
`_smooth_profile`, `_runs` → `_bool_runs`. `_find_peaks` now returns `(index, prominence)`
pairs (the sketch returned bare indices).

## The one real catch: rotation / deskew

The box model is axis-aligned `{x, y, w, h}`, so a box found in a rotated frame is a
tilted rectangle in the original composite — unrepresentable. Rotation only works as
**one angle applied identically to detection + canvas display + `/extract`** (which must
rotate the native array before slicing). `/extract` does not do that yet, so deskew is
gated off in the shipped route. To enable it, plumb the angle end to end and flip
`DETECT_ENABLE_DESKEW`. Full analysis in `DETECT_BANDS_DESIGN.md` and `CHAT_MEMORY.md`.

## Calibration

`sketches/calibrate_sensitivities.py` decodes real blot TIFs, sweeps detector params,
and renders overlay PNGs so `SENSITIVITY_LEVELS` can be tuned by eye. It was written
against the standalone `detect_bands_sketch.py` module; to calibrate the shipped
detector, point it at the (renamed) helpers in `backend/app.py`. Key finding already
baked into the defaults: `lane_threshold` must stay **constant** across sensitivity
levels — only `prominence` (and `min_distance_frac`) vary — or all lanes merge into one.
Local test ZIPs live under `test_blots/` (gitignored).

## Files

- `DETECT_BANDS_DESIGN.md` — master design doc: data flow, JSON contract, algorithm,
  security notes, tradeoffs, and the rotation analysis.
- `CHAT_MEMORY.md` — original design-session handoff notes (historical; predates the
  implementation).
- `sketches/` — the original prototypes (see the map above).
