#!/usr/bin/env python3
"""
Calibration harness for tuning SENSITIVITY_LEVELS in detect_bands_sketch.py.

It does NOT touch the app. It decodes real blot TIFs (tifffile), runs the detector's
own functions, prints lane/band counts per sensitivity level and a prominence sweep,
and renders overlay PNGs so the detection can be inspected visually.

USAGE
    venv/bin/python scratchpad/calibrate_sensitivities.py <path-to-test-data> [channel]

    <path-to-test-data> may contain:
      • .zip files in the app's format (folder-per-blot, each with a *700*.tif / *800*.tif), or
      • already-extracted blot folders (each containing a *700*.tif and/or *800*.tif).
    channel defaults to "700".

OUTPUT
    Writes <scratchpad>/calibration-out/<blot>__<level>.png overlays + a summary table.
"""

import os
import re
import sys
import zipfile
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detect_bands_sketch as db  # the detector under calibration

try:
    import tifffile
except ImportError:
    sys.exit("tifffile not found — run with the repo venv: venv/bin/python …")

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration-out")
# Same channel-matching rule as backend/app.py:find_channel_tif.
CHANNEL_RE = lambda ch: re.compile(rf"(^|[^0-9]){re.escape(ch)}([^0-9]|$)")


# ─── Loading test blots into (name, native_array) pairs ─────────────────────────

def _decode_tif_bytes(raw):
    arr = tifffile.imread(__import__("io").BytesIO(raw))
    arr = np.asarray(arr)
    if arr.ndim > 2:                      # multipage / channels -> take first plane
        arr = arr[0] if arr.shape[0] <= 4 else arr[..., 0]
    return arr.astype(np.float32)


def _match(names, channel):
    rx = CHANNEL_RE(channel)
    hits = [n for n in names
            if os.path.basename(n).lower().endswith((".tif", ".tiff"))
            and rx.search(os.path.basename(n))]
    return hits[0] if hits else None


def load_blots(path, channel):
    """Yield (blot_name, native_float_array) from ZIPs and/or extracted folders."""
    blots = []
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        zips = [path]
        root = None
    else:
        root = path
        zips = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(".zip")]

    for zpath in zips:
        with zipfile.ZipFile(zpath) as zf:
            folders = {}
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                folder = n.split("/")[0] if "/" in n else "__root__"
                folders.setdefault(folder, []).append(n)
            for folder, names in sorted(folders.items()):
                member = _match(names, channel)
                if member:
                    label = f"{os.path.basename(zpath)}:{folder}"
                    blots.append((label, _decode_tif_bytes(zf.read(member))))

    if root and os.path.isdir(root):       # extracted folders
        for entry in sorted(os.listdir(root)):
            sub = os.path.join(root, entry)
            if not os.path.isdir(sub):
                continue
            member = _match(os.listdir(sub), channel)
            if member:
                with open(os.path.join(sub, member), "rb") as fh:
                    blots.append((entry, _decode_tif_bytes(fh.read())))
    return blots


# ─── Rendering overlays ─────────────────────────────────────────────────────────

COLORS = {"conservative": (0, 200, 255), "balanced": (0, 255, 120), "aggressive": (255, 120, 0)}


def render_overlay(norm, boxes, out_path, color):
    img8 = (np.clip(norm, 0, 1) * 255).astype(np.uint8)
    canvas = Image.fromarray(img8, mode="L").convert("RGB")
    draw = ImageDraw.Draw(canvas)
    for b in boxes:
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
    canvas.save(out_path)


# ─── Main calibration loop ──────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    channel = sys.argv[2] if len(sys.argv) > 2 else "700"
    os.makedirs(OUT_DIR, exist_ok=True)

    blots = load_blots(path, channel)
    if not blots:
        sys.exit(f"No {channel}nm TIFs found under {path}")
    print(f"Loaded {len(blots)} blot(s) for channel {channel}\n")

    sweep = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30]
    print(f"{'blot':<40} {'native':<13} {'skew°':>6}  lanes  " +
          "  ".join(f"p={p:<4}" for p in sweep))
    print("-" * 120)

    for name, arr in blots:
        work, _ = db._working_image(arr)
        norm = db._orient_polarity(db._normalize(work))
        skew = db._estimate_skew(norm)
        norm = db._rotate(norm, skew)
        lanes = db._detect_lanes(norm, db.SENSITIVITY_LEVELS["balanced"]["lane_threshold"])

        # Prominence sweep (band count at balanced lane threshold + spacing).
        counts = []
        for p in sweep:
            n = sum(len(db._detect_bands(norm, ln, p, 0.02)) for ln in lanes)
            counts.append(n)
        print(f"{name[:40]:<40} {str(arr.shape):<13} {skew:>6.1f}  {len(lanes):>5}  " +
              "  ".join(f"{c:<6}" for c in counts))

        # Overlay per named level (what the user would actually see).
        for level_name, level in db.SENSITIVITY_LEVELS.items():
            boxes, _lc, _tr = db._detect_candidate(norm, level, 1.0, 1.0)   # working-space boxes
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            render_overlay(norm, boxes, os.path.join(OUT_DIR, f"{safe}__{level_name}.png"),
                           COLORS.get(level_name, (255, 0, 0)))

    print(f"\nOverlays written to {OUT_DIR}")
    print("Inspect the PNGs, then adjust SENSITIVITY_LEVELS in detect_bands_sketch.py and re-run.")


if __name__ == "__main__":
    main()
