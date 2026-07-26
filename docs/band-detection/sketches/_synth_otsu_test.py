import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detect_bands_sketch as db
import otsu_cc_sketch as ot
from PIL import Image, ImageDraw

rng = np.random.default_rng(0)
H, W = 600, 800
img = np.full((H, W), 300.0) + rng.normal(0, 20, (H, W))
def band(cx, cy, sx, sy, amp):
    yy, xx = np.mgrid[0:H, 0:W]
    return amp*np.exp(-(((xx-cx)**2)/(2*sx**2) + ((yy-cy)**2)/(2*sy**2)))
for i, cx in enumerate(np.linspace(90, 720, 6)):
    for cy, amp in [(150, 9000), (300, 4000 - i*400), (460, 1500)]:
        img += band(cx, cy, 26, 16, max(amp, 400))
img = np.clip(img, 0, 65535)

work, _ = db._working_image(img)
norm = db._orient_polarity(db._normalize(work))
norm = db._rotate(norm, db._estimate_skew(norm))
cands = ot.detect_candidates_otsu(norm, 1.0, 1.0)
for c in cands:
    print(f"{c['id']:>18}: lanes={c['laneCount']} bands={c['bandCount']} trunc={c['truncated']}")
    canvas = Image.fromarray((np.clip(norm,0,1)*255).astype('uint8'),'L').convert('RGB')
    d = ImageDraw.Draw(canvas)
    for b in c['boxes']:
        d.rectangle([b['x'],b['y'],b['x']+b['w'],b['y']+b['h']], outline=(255,120,0), width=2)
    canvas.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), f"_synth_{c['id']}.png"))
print("ok")
