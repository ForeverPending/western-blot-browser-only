"""
SKETCH ONLY — a SECOND detection hypothesis to offer alongside the projection-profile
candidates (detect_bands_sketch.py). Same output box shape, same coordinate contract, so it
plugs into the same chooser and the same acceptDetectedCandidate() path on the frontend.

METHOD: Otsu threshold -> binary mask -> connected components -> per-blob bounding boxes,
blobs grouped into lanes by x-overlap. Unlike projection profiles this makes NO grid
assumption, so it handles irregular / rotated / non-aligned layouts, spots, and doublets that
projections would merge. Downsides: a single global Otsu struggles with uneven background
(use the local variant noted below), faint bands fragment or vanish, and blobs need
area / fill filtering to reject noise.

DEPENDENCIES: numpy only. Connected components is done with vectorized label propagation
(no scipy). In production you'd reach for scipy.ndimage.label or cv2.connectedComponents;
this keeps to the app's existing deps.

Reuses the image-prep helpers from detect_bands_sketch.py (_normalize, _orient_polarity,
_working_image, _rotate/_estimate_skew) so both detectors see identical pixels.
"""

import numpy as np
import detect_bands_sketch as db  # _normalize, _orient_polarity, _working_image, _rotate, MAX_DETECT_BOXES


# ─── Tunables ───────────────────────────────────────────────────────────────────

# Threshold multiplier per sensitivity level: >1 raises the Otsu cut (fewer/smaller blobs =
# conservative), <1 lowers it (more/larger blobs = aggressive). Mirrors the three projection
# candidate sets so the chooser UX is identical.
OTSU_LEVELS = {
    "conservative": {"thresh_mult": 1.15, "min_area_frac": 3.0e-4, "min_fill": 0.35},
    "balanced":     {"thresh_mult": 1.00, "min_area_frac": 1.5e-4, "min_fill": 0.25},
    "aggressive":   {"thresh_mult": 0.85, "min_area_frac": 8.0e-5, "min_fill": 0.18},
}
CC_MAX_ITER = 400  # label-propagation cap; ~longest blob geodesic in the downsampled image


# ─── Otsu threshold ─────────────────────────────────────────────────────────────

def _otsu_threshold(gray, nbins=256):
    """Classic Otsu on a 0..1 image: the cut maximizing between-class variance."""
    hist, edges = np.histogram(gray, bins=nbins, range=(0.0, 1.0))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 0.5
    mids = (edges[:-1] + edges[1:]) / 2.0
    omega = np.cumsum(hist) / total              # class-0 weight at each cut
    mu = np.cumsum(hist * mids) / total          # class-0 cumulative mean
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    denom[denom == 0] = 1e-12
    sigma_b2 = (mu_t * omega - mu) ** 2 / denom   # between-class variance
    return float(mids[int(np.argmax(sigma_b2))])


# ─── Connected components (vectorized label propagation, no scipy) ──────────────

def _connected_components(mask, connectivity=8, max_iter=CC_MAX_ITER):
    """Label 4- or 8-connected True regions. Each True pixel starts with a unique id; every
    iteration each pixel takes the min id among itself and its neighbors, until stable. Returns
    an int label image (0 = background). Iterations ≈ the longest path inside a component, which
    is small for compact bands."""
    h, w = mask.shape
    BIG = h * w + 1
    ids = np.where(mask, np.arange(1, h * w + 1).reshape(h, w), BIG).astype(np.int64)
    for _ in range(max_iter):
        m = ids.copy()
        m[1:, :]  = np.minimum(m[1:, :],  ids[:-1, :])   # neighbor above
        m[:-1, :] = np.minimum(m[:-1, :], ids[1:, :])    # below
        m[:, 1:]  = np.minimum(m[:, 1:],  ids[:, :-1])   # left
        m[:, :-1] = np.minimum(m[:, :-1], ids[:, 1:])    # right
        if connectivity == 8:
            m[1:, 1:]   = np.minimum(m[1:, 1:],   ids[:-1, :-1])
            m[1:, :-1]  = np.minimum(m[1:, :-1],  ids[:-1, 1:])
            m[:-1, 1:]  = np.minimum(m[:-1, 1:],  ids[1:, :-1])
            m[:-1, :-1] = np.minimum(m[:-1, :-1], ids[1:, 1:])
        m[~mask] = BIG
        if np.array_equal(m, ids):
            break
        ids = m
    ids[~mask] = 0
    return ids


def _component_boxes(labels, min_area, min_fill):
    """Per-label bounding box, filtered by pixel area and fill ratio (area / bbox area) to
    drop sparse noise. Returns [(x0, y0, x1, y1, area, score), ...] in label-image pixels."""
    out = []
    for lab in np.unique(labels):
        if lab == 0:
            continue
        ys, xs = np.where(labels == lab)
        area = int(xs.size)
        if area < min_area:
            continue
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        bbox_area = (x1 - x0) * (y1 - y0)
        if bbox_area == 0 or (area / bbox_area) < min_fill:
            continue
        out.append((x0, y0, x1, y1, area, area / bbox_area))
    return out


# ─── Lane grouping (blobs -> lanes by x-overlap) ────────────────────────────────

def _assign_lanes(boxes):
    """Cluster blob bounding boxes into lanes by horizontal overlap. Returns a lane index per
    box (input order preserved). Lanes are ordered left→right."""
    if not boxes:
        return []
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])  # by x0
    lane_of = [0] * len(boxes)
    lanes = []  # each lane: [x0, x1]
    for i in order:
        x0, _y0, x1, _y1, _a, _f = boxes[i]
        placed = False
        for li, (lx0, lx1) in enumerate(lanes):
            if x0 < lx1 and x1 > lx0:  # x-ranges overlap
                lanes[li] = [min(lx0, x0), max(lx1, x1)]
                lane_of[i] = li
                placed = True
                break
        if not placed:
            lanes.append([x0, x1])
            lane_of[i] = len(lanes) - 1
    # Re-order lane indices left→right by final lane x0.
    remap = {li: rank for rank, li in enumerate(sorted(range(len(lanes)), key=lambda li: lanes[li][0]))}
    return [remap[l] for l in lane_of]


# ─── One candidate at one sensitivity ───────────────────────────────────────────

def _detect_candidate_otsu(norm, level, scale_x, scale_y):
    """Otsu+CC detection at one sensitivity; boxes emitted in COMPOSITE coordinates."""
    h, w = norm.shape
    base = _otsu_threshold(norm)
    thresh = float(np.clip(base * level["thresh_mult"], 0.0, 1.0))
    mask = norm >= thresh

    labels = _connected_components(mask, connectivity=8)
    min_area = max(4, int(level["min_area_frac"] * h * w))
    raw = _component_boxes(labels, min_area, level["min_fill"])
    lane_idx = _assign_lanes(raw)

    boxes = []
    for (x0, y0, x1, y1, _area, fill), lane in zip(raw, lane_idx):
        boxes.append({
            "x": round(x0 * scale_x, 2),
            "y": round(y0 * scale_y, 2),
            "w": round((x1 - x0) * scale_x, 2),
            "h": round((y1 - y0) * scale_y, 2),
            "lane": lane,
            "score": round(float(fill), 4),
        })
    boxes.sort(key=lambda b: (b["lane"], b["y"]))
    truncated = False
    if len(boxes) > db.MAX_DETECT_BOXES:
        strongest = sorted(boxes, key=lambda b: b["score"], reverse=True)[:db.MAX_DETECT_BOXES]
        keep = set(id(b) for b in strongest)
        boxes = [b for b in boxes if id(b) in keep]
        truncated = True
    lane_count = (max((b["lane"] for b in boxes), default=-1) + 1)
    return boxes, lane_count, truncated


# ─── Public entry (would be a branch inside detect_bands, or its own route) ─────
#
# Fold this into detect_bands() as method="otsu", or expose /detect-bands?method=otsu.
# It receives the SAME already-prepped `norm` (downsampled, polarity-fixed, deskewed) and the
# SAME scale_x/scale_y as the projection detector, so both write identical-shaped candidates.

def detect_candidates_otsu(norm, scale_x, scale_y, levels=("conservative", "balanced", "aggressive")):
    candidates = []
    for name in levels:
        if name not in OTSU_LEVELS:
            continue
        boxes, lane_count, truncated = _detect_candidate_otsu(norm, OTSU_LEVELS[name], scale_x, scale_y)
        candidates.append({
            "id": f"otsu-{name}",
            "label": f"Otsu · {name.capitalize()}",
            "method": "otsu",
            "laneCount": lane_count,
            "bandCount": len(boxes),
            "truncated": truncated,
            "boxes": boxes,
        })
    return candidates


# LOCAL / ADAPTIVE OTSU (note, not implemented here): for blots with uneven background, replace
# the single global _otsu_threshold with a per-tile threshold (compute Otsu on each block of a
# coarse grid, bilinearly interpolate to a per-pixel threshold surface, then mask). Same CC
# stage downstream. This is the main robustness upgrade if global Otsu under/over-segments.
