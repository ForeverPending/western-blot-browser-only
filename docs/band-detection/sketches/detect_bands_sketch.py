"""
SKETCH ONLY — not wired into the app, nothing here is imported by backend/app.py.

A backend band detector for the western-blot browser-only app.

WHERE THIS WOULD LIVE
  - The functions + the `detect_bands` route go into backend/app.py, right next to
    extract_payload_signals() / render_composite() (they reuse the same helpers).
  - A thin Vercel wrapper goes in api/detect-bands.py (see api_detect-bands_wrapper.py).
  - Frontend caller + UI: see detect-bands_frontend_sketch.js.

WHY BACKEND
  Detection runs on the native 16-bit TIF via read_raw_channel() — the SAME validated
  pixels extract_box_signal() quantifies from — instead of the lossy, tone-mapped 8-bit
  JPEG composite the browser holds. It reuses decode_validated_tif() hardening, so the
  attack surface matches /extract.

CONTRACT (in vs out)
  Boxes are EMITTED in COMPOSITE coordinate space (x, y, w, h), exactly what
  canvasState.boxes / createCanvasBox() expect. The existing /extract route then scales
  them composite -> native again at measure time. So detection output drops straight into
  the same pipeline as a hand-drawn box. See DETECT_BANDS_DESIGN.md for the JSON contract.

DEPENDENCIES
  numpy only (already a dep). No scipy — peak finding is hand-rolled below.

The `# reuse:` comments mark existing backend/app.py symbols this leans on.
"""

import numpy as np
from PIL import Image  # already a dep (pillow); used only for rotation/deskew
# reuse: np, Image, PublicError, error_response, request, jsonify, app, limiter,
#        request_session_id, load_payload_blot, read_temp_file, read_raw_channel,
#        resolve_composite_dimensions, BLOT_FILE_LIMITS   (all already in backend/app.py)


# ─── Tunable parameters ────────────────────────────────────────────────────────

# Cap detected boxes to match the frontend's MAX_CANVAS_BOXES (app.js:17).
MAX_DETECT_BOXES = 200

# Detection is downsampled to this max dimension first: projection profiles don't
# need full resolution, and this bounds cost/memory independent of image size
# (a native TIF may be up to MAX_IMAGE_PIXELS = 80M px). Boxes scale back to
# composite space at the end, so downsampling never affects the emitted coordinates.
DETECT_MAX_DIM = 1600

# Longer axis of the returned laneProfile (for optional live client-side re-thresholding).
PROFILE_SAMPLES = 512

# Deskew search: try angles in [-MAX_SKEW_DEG, +MAX_SKEW_DEG] at this step and keep the
# one that makes the lane (column) projection sharpest. 0 disables auto-deskew.
MAX_SKEW_DEG = 5.0
SKEW_STEP_DEG = 0.5

# The "sets of bands to choose between" = one detection per sensitivity level. Lower
# `prominence` => more (fainter) bands captured; that is the knob that varies across levels.
# `lane_threshold` is deliberately HELD CONSTANT: lowering it with sensitivity merges
# adjacent lanes into one (verified visually — see calibration notes), which is never what
# we want. Values are in normalized (0..1) profile units. Tune against real blots.
SENSITIVITY_LEVELS = {
    "conservative": {"prominence": 0.20, "lane_threshold": 0.15, "min_distance_frac": 0.030},
    "balanced":     {"prominence": 0.10, "lane_threshold": 0.15, "min_distance_frac": 0.020},
    "aggressive":   {"prominence": 0.05, "lane_threshold": 0.15, "min_distance_frac": 0.012},
}
DEFAULT_LEVELS = ["conservative", "balanced", "aggressive"]


# ─── Image prep ────────────────────────────────────────────────────────────────

def _working_image(arr):
    """Stride-downsample so the longer axis is <= DETECT_MAX_DIM. Striding is cheap and
    fine for projection profiles. Returns (work, step)."""
    h, w = arr.shape
    step = max(1, int(np.ceil(max(h, w) / DETECT_MAX_DIM)))
    return arr[::step, ::step], step


def _normalize(arr):
    """Robust 0..1 normalization using percentiles, so a few saturated pixels or a
    dark floor don't dominate. Mirrors the spirit of read_tif_channel's percentile
    stretch, but computed locally for detection only."""
    a = arr.astype(np.float64, copy=False)
    lo = float(np.percentile(a, 5.0))
    hi = float(np.percentile(a, 99.5))
    if hi <= lo:
        return np.zeros_like(a)
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def _orient_polarity(norm):
    """LI-COR Odyssey scans are signal-bright-on-dark, so high intensity = band.
    If the frame is mostly bright (median > 0.5) it's a dark-band-on-light capture —
    invert so bands are always the high values downstream."""
    if float(np.median(norm)) > 0.5:
        return 1.0 - norm
    return norm


def _smooth(profile, window):
    """Moving-average smoothing via convolution (mode='same' keeps length)."""
    if window <= 1:
        return profile
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(profile, kernel, mode="same")


def _runs(mask):
    """Contiguous True runs in a 1-D boolean array -> list of (start, end) half-open."""
    idx = np.flatnonzero(np.diff(np.concatenate(([0], mask.view(np.int8), [0]))))
    return list(zip(idx[0::2], idx[1::2]))


def _downsample_profile(profile, target):
    n = len(profile)
    if n <= target:
        return profile
    return profile[np.linspace(0, n - 1, target).astype(int)]


# ─── Rotation / deskew ──────────────────────────────────────────────────────────
#
# WHY THIS IS MORE THAN A ONE-LINER: the app's box model is AXIS-ALIGNED {x, y, w, h}
# (createCanvasBox, app.js:5229). A box found in a rotated frame is a *tilted* rectangle
# back in the original composite — which the model can't represent. So rotation can't be a
# detection-only trick; it has to be a property of the whole blot view. Two coherent options:
#
#   (A) Detect-only, angle returned to caller (what this sketch does): the frontend rotates
#       the CANVAS display by the same angle (canvas transform, on top of pan/zoom at
#       app.js:4780) so the user sees a deskewed blot and boxes stay axis-aligned in that
#       frame. Then /extract must rotate the NATIVE array by the same angle before slicing
#       (one np/PIL rotate in extract_box_signal's caller), so measurement matches the view.
#       => needs a small, consistent change in three places: detect, display, extract.
#
#   (B) Do nothing server-side; let the user rotate in the UI first. Simpler, but then the
#       rendered composite itself must carry the rotation (add an angle to /render-composite).
#
# Either way the rule is: ONE angle, applied identically to display + detection + extraction.
# The functions below cover the detection side and the angle estimate.

def _rotate(arr, angle_deg):
    """Rotate a 2-D float array about its center by angle_deg (degrees, CCW), keeping the
    same shape (corners are cropped). Bilinear resample. Returns arr unchanged at 0°."""
    if abs(angle_deg) < 1e-3:
        return arr
    img = Image.fromarray(arr.astype(np.float32, copy=False), mode="F")
    rotated = img.rotate(angle_deg, resample=Image.BILINEAR, expand=False, fillcolor=0.0)
    return np.asarray(rotated, dtype=np.float32)


def _lane_sharpness(norm):
    """How 'peaky' the lane (column) projection is. Sharp, well-separated lanes give a
    high-variance profile; a smeared/tilted blot flattens it. Used as the deskew objective."""
    prof = _smooth(norm.mean(axis=0), max(3, int(norm.shape[1] * 0.01)))
    return float(np.var(prof))


def _estimate_skew(norm, max_deg=MAX_SKEW_DEG, step=SKEW_STEP_DEG):
    """Pick the small rotation that maximizes lane sharpness (a coarse Radon/projection
    deskew). Returns degrees; 0.0 when deskew is disabled or flat."""
    if max_deg <= 0:
        return 0.0
    angles = np.arange(-max_deg, max_deg + step / 2, step)
    best_angle, best_score = 0.0, -np.inf
    for angle in angles:
        score = _lane_sharpness(_rotate(norm, float(angle)))
        if score > best_score:
            best_score, best_angle = score, float(angle)
    return best_angle


# ─── Peak finding (numpy, no scipy) ─────────────────────────────────────────────

def _find_peaks(y, min_prominence, min_distance):
    """Local maxima whose topographic prominence >= min_prominence, spaced at least
    min_distance apart (greedy by prominence when they crowd). Prominence proxy: walk
    outward from each peak until a higher sample, tracking the lowest valley on each
    side; prominence = peak - max(left_valley, right_valley)."""
    n = len(y)
    if n < 3:
        return []
    # Candidate local maxima (>= on the right handles flat-topped plateaus).
    cand = np.flatnonzero((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:])) + 1

    scored = []
    for i in cand:
        peak = y[i]
        left_valley = peak
        j = i - 1
        while j >= 0 and y[j] < peak:
            left_valley = min(left_valley, y[j]); j -= 1
        right_valley = peak
        j = i + 1
        while j < n and y[j] < peak:
            right_valley = min(right_valley, y[j]); j += 1
        prominence = peak - max(left_valley, right_valley)
        if prominence >= min_prominence:
            scored.append((i, prominence))

    # Enforce spacing: keep the most prominent, drop peaks too close to a kept one.
    scored.sort(key=lambda t: t[1], reverse=True)
    kept = []
    for i, _prom in scored:
        if all(abs(i - k) >= min_distance for k in kept):
            kept.append(i)
    kept.sort()
    return kept


def _peak_extent(y, i, prominence):
    """Band y-extent: descend from the peak on both sides until the profile drops to
    half-prominence below the peak (a stable proxy for valley-to-valley). Returns a
    half-open [y0, y1)."""
    n = len(y)
    floor = y[i] - 0.5 * prominence
    a = i
    while a > 0 and y[a - 1] > floor:
        a -= 1
    b = i
    while b < n - 1 and y[b + 1] > floor:
        b += 1
    return a, b + 1


# ─── Lane + band detection ──────────────────────────────────────────────────────

def _detect_lanes(norm, lane_threshold):
    """Vertical projection (mean per column) -> lanes are contiguous spans above a
    fraction of the profile's dynamic range. Returns list of (x0, x1) half-open."""
    h, w = norm.shape
    prof = _smooth(norm.mean(axis=0), max(3, int(w * 0.01)))
    lo, hi = float(prof.min()), float(prof.max())
    if hi <= lo:
        return [(0, w)]
    above = prof >= (lo + lane_threshold * (hi - lo))
    min_width = max(2, int(w * 0.01))
    lanes = [(int(a), int(b)) for a, b in _runs(above) if (b - a) >= min_width]
    return lanes or [(0, w)]


def _detect_bands(norm, lane, prominence, min_distance_frac):
    """Horizontal projection within a lane's columns -> bands are peaks along the
    migration axis. Returns list of (y0, y1, score)."""
    h, _w = norm.shape
    x0, x1 = lane
    prof = _smooth(norm[:, x0:x1].mean(axis=1), max(3, int(h * 0.01)))
    peaks = _find_peaks(prof, prominence, min_distance=max(2, int(h * min_distance_frac)))
    out = []
    for p in peaks:
        y0, y1 = _peak_extent(prof, p, prominence)
        out.append((y0, y1, float(prof[p])))
    return out


def _detect_candidate(norm, level, scale_x, scale_y, lane_threshold=None):
    """Run one sensitivity level over all lanes; emit boxes in COMPOSITE coordinates.
    lane_threshold overrides the level default — used when the client tuned lanes live via
    the laneProfile slider and re-detects at that value."""
    boxes = []
    lanes = _detect_lanes(norm, level["lane_threshold"] if lane_threshold is None else lane_threshold)
    for li, (x0, x1) in enumerate(lanes):
        for bi, (y0, y1, score) in enumerate(
            _detect_bands(norm, (x0, x1), level["prominence"], level["min_distance_frac"])
        ):
            boxes.append({
                "x": round(x0 * scale_x, 2),
                "y": round(y0 * scale_y, 2),
                "w": round((x1 - x0) * scale_x, 2),
                "h": round((y1 - y0) * scale_y, 2),
                "lane": li,
                "band": bi,
                "score": round(score, 4),
            })
    truncated = False
    if len(boxes) > MAX_DETECT_BOXES:
        boxes.sort(key=lambda b: b["score"], reverse=True)   # keep strongest bands
        boxes = boxes[:MAX_DETECT_BOXES]
        truncated = True
    return boxes, len(lanes), truncated


# ─── Flask route (mirrors extract_payload_signals) ──────────────────────────────

# @app.route("/detect-bands", methods=["POST"])
# @app.route("/api/detect-bands", methods=["POST"])
# @limiter.limit("15 per minute")
def detect_bands():
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    try:
        blot = load_payload_blot(data.get("blot"), (), session_id)
        channel = data.get("channel", "700")
        if channel not in ("700", "800"):
            return jsonify({"error": "Invalid channel"}), 400

        levels = data.get("sensitivities") or DEFAULT_LEVELS
        if not isinstance(levels, list):
            return jsonify({"error": "sensitivities must be a list."}), 400
        levels = [name for name in levels if name in SENSITIVITY_LEVELS][:3] or ["balanced"]

        # Optional lane-threshold override from the client's live laneProfile slider (0..1).
        lane_threshold = data.get("laneThreshold")
        if isinstance(lane_threshold, (int, float)) and np.isfinite(lane_threshold):
            lane_threshold = float(min(1.0, max(0.0, lane_threshold)))
        else:
            lane_threshold = None

        descriptor = blot["files"].get(channel)
        if not descriptor:
            return jsonify({"error": f"No {channel}nm TIF found"}), 404
        tif_bytes = read_temp_file(descriptor, session_id, max_bytes=BLOT_FILE_LIMITS[channel])

        # Same validated-decode path as /extract; float32 native array (no saturation copy).
        arr = read_raw_channel(tif_bytes)
        native_h, native_w = arr.shape

        # Detect on a downsampled copy; scale boxes from the WORKING grid straight to
        # composite space (composite = element-wise max of both channels' native sizes).
        work, _step = _working_image(arr)
        work_h, work_w = work.shape
        composite_h, composite_w = resolve_composite_dimensions(
            data, blot, channel, session_id, native_h, native_w
        )
        scale_x = composite_w / work_w if work_w else 1.0
        scale_y = composite_h / work_h if work_h else 1.0

        norm = _orient_polarity(_normalize(work))

        # Deskew: honor a client-supplied angle if given, else auto-estimate. The angle is
        # returned so the frontend can rotate the canvas display + /extract can rotate the
        # native array to match (see the _rotate docstring above). Detection then runs in
        # the deskewed frame where lanes are vertical and boxes stay axis-aligned.
        requested_angle = data.get("rotationDeg")
        if isinstance(requested_angle, (int, float)) and np.isfinite(requested_angle):
            angle = max(-45.0, min(45.0, float(requested_angle)))
        else:
            angle = _estimate_skew(norm)
        norm = _rotate(norm, angle)

        candidates = []
        for name in levels:
            boxes, lane_count, truncated = _detect_candidate(
                norm, SENSITIVITY_LEVELS[name], scale_x, scale_y, lane_threshold=lane_threshold
            )
            candidates.append({
                "id": name,
                "label": name.capitalize(),
                "laneCount": lane_count,
                "bandCount": len(boxes),
                "truncated": truncated,   # frontend should surface this ("showing strongest 200")
                "boxes": boxes,
            })

        # Optional: coarse lane profile so the client can re-threshold live without a
        # round-trip. Omit if you only ever toggle between the returned candidate sets.
        lane_profile = _smooth(norm.mean(axis=0), max(3, int(work_w * 0.01)))

        return jsonify({
            "imageWidth": composite_w,     # coordinate space the boxes are in
            "imageHeight": composite_h,
            "channel": channel,
            "rotationDeg": round(angle, 2),  # apply this to display + /extract to match
            "candidates": candidates,
            "laneProfile": _downsample_profile(lane_profile, PROFILE_SAMPLES).round(4).tolist(),
        })
    except PublicError as error:                       # reuse: PublicError
        return error_response(error, "Band detection failed.")   # reuse: error_response
    except Exception as error:
        app.logger.exception("Band detection failed")
        return error_response(error, "Band detection failed.")
