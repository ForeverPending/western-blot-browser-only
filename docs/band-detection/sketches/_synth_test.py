import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detect_bands_sketch as db
from PIL import Image, ImageDraw

rng = np.random.default_rng(0)
H, W = 600, 800
img = np.full((H, W), 300.0)                      # dark floor
img += rng.normal(0, 20, (H, W))                  # noise
def band(cx, cy, sx, sy, amp):
    yy, xx = np.mgrid[0:H, 0:W]
    return amp*np.exp(-(((xx-cx)**2)/(2*sx**2) + ((yy-cy)**2)/(2*sy**2)))
# 6 lanes, 2-3 bands each, varying intensity
lane_x = np.linspace(90, 720, 6)
for i, cx in enumerate(lane_x):
    for cy, amp in [(150, 9000), (300, 4000 - i*400), (460, 1500)]:
        img += band(cx, cy, 26, 16, max(amp, 400))
img = np.clip(img, 0, 65535)

work, _ = db._working_image(img)
norm = db._orient_polarity(db._normalize(work))
skew = db._estimate_skew(norm)
norm = db._rotate(norm, skew)
print("shape", img.shape, "-> work", work.shape, "| est skew", skew)
for name, lvl in db.SENSITIVITY_LEVELS.items():
    boxes, lanes, trunc = db._detect_candidate(norm, lvl, 1.0, 1.0)
    print(f"{name:>12}: lanes={lanes} bands={len(boxes)} trunc={trunc}")
    c = (Image.fromarray((np.clip(norm,0,1)*255).astype('uint8'),'L').convert('RGB'))
    d = ImageDraw.Draw(c)
    for b in boxes:
        d.rectangle([b['x'],b['y'],b['x']+b['w'],b['y']+b['h']], outline=(0,255,120), width=2)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"_synth_{name}.png")
    c.save(out)
print("ok")
