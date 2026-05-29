import os
import io
import uuid
import zipfile
import numpy as np
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from PIL import Image
import tifffile
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
import base64

app = Flask(__name__)

# ─── CORS ─────────────────────────────────────────────────────────────────────
CORS(app, origins=[
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    # Add your Vercel URL here when deployed:
    # "https://your-app.vercel.app",
])

# ─── Rate limiting ─────────────────────────────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# ─── Constants ────────────────────────────────────────────────────────────────
MAX_ZIP_SIZE  = 500 * 1024 * 1024  # 500MB
MAX_TIF_SIZE  = 200 * 1024 * 1024  # 200MB
MAX_BLOTS     = 100

# ─── In-memory blot store ─────────────────────────────────────────────────────
blot_store = {}

# ─── Health check ─────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ─── ZIP upload & parsing ─────────────────────────────────────────────────────

@app.route("/upload-zip", methods=["POST"])
@limiter.limit("10 per minute")
def upload_zip():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".zip"):
        return jsonify({"error": "File must be a .zip"}), 400

    # Check content length before reading
    if request.content_length and request.content_length > MAX_ZIP_SIZE:
        return jsonify({"error": "File too large. Maximum size is 500MB."}), 413

    file_bytes = file.read()

    # Check actual file size after reading
    if len(file_bytes) > MAX_ZIP_SIZE:
        return jsonify({"error": "File too large. Maximum size is 500MB."}), 413

    # Validate ZIP magic bytes
    if not is_valid_zip(file_bytes):
        return jsonify({"error": "Invalid ZIP file."}), 400

    try:
        blots = parse_zip(file_bytes)
        return jsonify({"blots": blots})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def is_valid_zip(file_bytes):
    return len(file_bytes) >= 4 and file_bytes[:4] == b'PK\x03\x04'


def parse_zip(file_bytes):
    blots = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        # Group files by folder
        folders = {}
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            parts = name.split("/")
            folder = parts[0] if len(parts) > 1 else "__root__"
            if folder not in folders:
                folders[folder] = []
            folders[folder].append(name)

        for folder, files in folders.items():
            txt_file = next((f for f in files if f.lower().endswith(".txt")), None)
            jpg_file = next((f for f in files if f.lower().endswith(".jpg") or f.lower().endswith(".jpeg")), None)
            tif_700  = next((f for f in files if "700" in f and f.lower().endswith(".tif")), None)
            tif_800  = next((f for f in files if "800" in f and f.lower().endswith(".tif")), None)

            if not txt_file:
                continue

            # Check TIF sizes before reading
            if tif_700 and zf.getinfo(tif_700).file_size > MAX_TIF_SIZE:
                continue
            if tif_800 and zf.getinfo(tif_800).file_size > MAX_TIF_SIZE:
                continue

            # Parse blot name from last line of txt file
            txt_content = zf.read(txt_file).decode("utf-8", errors="ignore")
            lines = txt_content.strip().splitlines()
            last_line = lines[-1] if lines else ""
            blot_name = last_line.split("=", 1)[1].strip() if last_line.startswith("Remarks=") else folder

            # Generate a unique unpredictable ID
            blot_id = f"{uuid.uuid4().hex}_{folder}".replace(" ", "_")

            # Evict oldest blots if store is full
            if len(blot_store) >= MAX_BLOTS:
                oldest_keys = list(blot_store.keys())[:10]
                for key in oldest_keys:
                    del blot_store[key]

            # Store raw file bytes in memory
            blot_store[blot_id] = {
                "name": blot_name,
                "folder": folder,
                "jpg_bytes":      zf.read(jpg_file)  if jpg_file  else None,
                "tif_700_bytes":  zf.read(tif_700)   if tif_700   else None,
                "tif_800_bytes":  zf.read(tif_800)   if tif_800   else None,
            }

            blots.append({
                "id":      blot_id,
                "name":    blot_name,
                "has_jpg": jpg_file is not None,
                "has_700": tif_700  is not None,
                "has_800": tif_800  is not None,
            })

    return blots

# ─── Serve JPG preview ────────────────────────────────────────────────────────

@app.route("/blot/<blot_id>/preview")
@limiter.limit("60 per minute")
def blot_preview(blot_id):
    blot = blot_store.get(blot_id)
    if not blot or not blot["jpg_bytes"]:
        return jsonify({"error": "Preview not found"}), 404
    return send_file(io.BytesIO(blot["jpg_bytes"]), mimetype="image/jpeg")

# ─── TIF composite rendering ──────────────────────────────────────────────────

@app.route("/blot/<blot_id>/composite")
@limiter.limit("60 per minute")
def blot_composite(blot_id):
    blot = blot_store.get(blot_id)
    if not blot:
        return jsonify({"error": "Blot not found"}), 404
    if not blot["tif_700_bytes"] or not blot["tif_800_bytes"]:
        return jsonify({"error": "TIF files not found for this blot"}), 404

    try:
        brightness_700 = float(request.args.get("brightness_700", 1.0))
        contrast_700   = float(request.args.get("contrast_700",   1.0))
        brightness_800 = float(request.args.get("brightness_800", 1.0))
        contrast_800   = float(request.args.get("contrast_800",   1.0))
        colormode      = request.args.get("colormode", "color")

        # Clamp adjustment values to safe ranges
        brightness_700 = max(0.1, min(5.0, brightness_700))
        contrast_700   = max(0.1, min(10.0, contrast_700))
        brightness_800 = max(0.1, min(5.0, brightness_800))
        contrast_800   = max(0.1, min(10.0, contrast_800))

        img = build_composite(
            blot["tif_700_bytes"],
            blot["tif_800_bytes"],
            brightness_700, contrast_700,
            brightness_800, contrast_800,
            colormode,
        )

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def read_tif_channel(tif_bytes):
    arr = tifffile.imread(io.BytesIO(tif_bytes))
    if arr.ndim == 3:
        arr = arr[0]
    arr = arr.astype(np.float32)
    low  = np.percentile(arr, 1)
    high = np.percentile(arr, 99)
    if high > low:
        arr = (arr - low) / (high - low)
    else:
        arr = np.zeros_like(arr)
    return np.clip(arr, 0.0, 1.0)


def apply_adjustments(channel, brightness, contrast):
    adjusted = (channel - 0.5) * contrast + 0.5
    adjusted = adjusted + (brightness - 1.0)
    return np.clip(adjusted, 0.0, 1.0)


def build_composite(tif_700_bytes, tif_800_bytes, brightness_700, contrast_700, brightness_800, contrast_800, colormode="color"):
    ch_700 = read_tif_channel(tif_700_bytes)
    ch_800 = read_tif_channel(tif_800_bytes)

    if ch_700.shape != ch_800.shape:
        h = max(ch_700.shape[0], ch_800.shape[0])
        w = max(ch_700.shape[1], ch_800.shape[1])
        ch_700 = np.array(Image.fromarray((ch_700 * 65535).astype(np.uint16)).resize((w, h), Image.LANCZOS)).astype(np.float32) / 65535
        ch_800 = np.array(Image.fromarray((ch_800 * 65535).astype(np.uint16)).resize((w, h), Image.LANCZOS)).astype(np.float32) / 65535

    ch_700 = apply_adjustments(ch_700, brightness_700, contrast_700)
    ch_800 = apply_adjustments(ch_800, brightness_800, contrast_800)

    if colormode == "grayscale":
        blended = np.clip(ch_700 + ch_800, 0.0, 1.0)
        blended = 1.0 - blended
        gray = (blended * 255).astype(np.uint8)
        rgb = np.stack([gray, gray, gray], axis=2)
    else:
        r = (ch_700 * 255).astype(np.uint8)
        g = (ch_800 * 255).astype(np.uint8)
        b = np.zeros_like(r)
        rgb = np.stack([r, g, b], axis=2)

    return Image.fromarray(rgb, mode="RGB")

# ─── Signal extraction ────────────────────────────────────────────────────────

@app.route("/blot/<blot_id>/extract", methods=["POST"])
@limiter.limit("30 per minute")
def extract_signals(blot_id):
    blot = blot_store.get(blot_id)
    if not blot:
        return jsonify({"error": "Blot not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    boxes           = data.get("boxes", [])
    channel         = data.get("channel", "700")
    background_axis = data.get("background_axis", "leftright")

    # Validate channel
    if channel not in ("700", "800"):
        return jsonify({"error": "Invalid channel"}), 400

    # Limit number of boxes
    if len(boxes) > 200:
        return jsonify({"error": "Too many boxes. Maximum is 200."}), 400

    tif_bytes = blot["tif_700_bytes"] if channel == "700" else blot["tif_800_bytes"]
    if not tif_bytes:
        return jsonify({"error": f"No {channel}nm TIF found"}), 404

    try:
        arr     = read_raw_channel(tif_bytes)
        results = []
        for box in boxes:
            signal = extract_box_signal(arr, box, background_axis)
            results.append(signal)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def read_raw_channel(tif_bytes):
    arr = tifffile.imread(io.BytesIO(tif_bytes))
    if arr.ndim == 3:
        arr = arr[0]
    return arr.astype(np.float32)


def extract_box_signal(arr, box, background_axis):
    x = max(0, int(round(box["x"])))
    y = max(0, int(round(box["y"])))
    w = max(1, int(round(box["w"])))
    h = max(1, int(round(box["h"])))

    img_h, img_w = arr.shape
    x2 = min(x + w, img_w)
    y2 = min(y + h, img_h)

    roi        = arr[y:y2, x:x2]
    raw_signal = float(np.sum(roi))

    border     = 3
    bg_pixels  = []

    if background_axis == "leftright":
        lx1 = max(0, x - border)
        lx2 = x
        if lx2 > lx1:
            bg_pixels.append(arr[y:y2, lx1:lx2].flatten())
        rx1 = x2
        rx2 = min(img_w, x2 + border)
        if rx2 > rx1:
            bg_pixels.append(arr[y:y2, rx1:rx2].flatten())
    else:
        ty1 = max(0, y - border)
        ty2 = y
        if ty2 > ty1:
            bg_pixels.append(arr[ty1:ty2, x:x2].flatten())
        by1 = y2
        by2 = min(img_h, y2 + border)
        if by2 > by1:
            bg_pixels.append(arr[by1:by2, x:x2].flatten())

    if bg_pixels:
        all_bg     = np.concatenate(bg_pixels)
        bg_median  = float(np.median(all_bg))
        n_pixels   = (y2 - y) * (x2 - x)
        bg_signal  = bg_median * n_pixels
    else:
        bg_signal  = 0.0

    adjusted_signal = max(0.0, raw_signal - bg_signal)

    return {
        "raw_signal":      round(raw_signal, 2),
        "bg_signal":       round(bg_signal, 2),
        "adjusted_signal": round(adjusted_signal, 2),
        "x": x, "y": y, "w": w, "h": h,
    }

# ─── PowerPoint generation ────────────────────────────────────────────────────

@app.route("/generate-pptx", methods=["POST"])
@limiter.limit("10 per minute")
def generate_pptx():
    try:
        data        = request.get_json()
        slides_data = data.get("slides", [])

        prs              = Presentation()
        prs.slide_width  = Inches(10)
        prs.slide_height = Inches(7.5)
        blank_layout     = prs.slide_layouts[6]

        # Separate image slides from graph slides
        image_slide_data = next((s for s in slides_data if s.get("type") == "images"), None)
        graph_slides     = [s for s in slides_data if s.get("type") == "graphs"]

        # Build image slide first
        if image_slide_data:
            slide = prs.slides.add_slide(blank_layout)
            build_image_slide(slide, image_slide_data, prs)

        # Build graph slides, 4 per slide
        for slide_data in graph_slides:
            graphs   = slide_data.get("graphs", [])
            title    = slide_data.get("title", "")
            per_page = 4

            for page_start in range(0, max(1, len(graphs)), per_page):
                page_graphs = graphs[page_start:page_start + per_page]
                slide       = prs.slides.add_slide(blank_layout)
                build_graph_slide(slide, {
                    "title":  title if page_start == 0 else f"{title} (cont.)",
                    "graphs": page_graphs,
                }, prs)

        buf = io.BytesIO()
        prs.save(buf)
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            as_attachment=True,
            download_name="western-blot-analysis.pptx"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def add_image_from_base64(slide, b64_string, left, top, width, height):
    img_bytes  = base64.b64decode(b64_string.split(",")[-1])
    img_stream = io.BytesIO(img_bytes)
    slide.shapes.add_picture(img_stream, left, top, width, height)


def add_label(slide, text, left, top, width, height, font_size=14, bold=False, align=PP_ALIGN.CENTER):
    txBox          = slide.shapes.add_textbox(left, top, width, height)
    tf             = txBox.text_frame
    tf.word_wrap   = True
    p              = tf.paragraphs[0]
    p.alignment    = align
    run            = p.add_run()
    run.text       = text
    run.font.size  = Pt(font_size)
    run.font.bold  = bold
    run.font.color.rgb = RGBColor(0x1f, 0x29, 0x33)


def build_image_slide(slide, slide_data, prs):
    images  = slide_data.get("images", [])
    if not images:
        return

    slide_w = prs.slide_width
    slide_h = prs.slide_height
    margin  = Inches(0.4)
    label_h = Inches(0.3)
    spacing = Inches(0.2)

    add_label(slide, "Western Blot Images",
              margin, Inches(0.1), slide_w - margin * 2, Inches(0.35),
              font_size=18, bold=True)

    n               = len(images)
    total_label     = label_h * n
    total_spacing   = spacing * (n - 1)
    available_h     = slide_h - Inches(0.6) - total_label - total_spacing - margin
    full_img_w      = slide_w - margin * 2
    img_w           = full_img_w * 2 / 3
    img_h           = available_h / n
    left_x          = margin + (full_img_w - img_w) / 2

    current_y = Inches(0.6)
    for item in images:
        b64   = item.get("image")
        label = item.get("label", "")
        if b64:
            try:
                add_image_from_base64(slide, b64, left_x, current_y, img_w, img_h)
            except Exception:
                pass
        current_y += img_h
        add_label(slide, label, left_x, current_y, img_w, label_h,
                  font_size=12, bold=True, align=PP_ALIGN.CENTER)
        current_y += label_h + spacing


def build_graph_slide(slide, slide_data, prs):
    graphs  = slide_data.get("graphs", [])
    title   = slide_data.get("title", "")
    if not graphs:
        return

    slide_w  = prs.slide_width
    slide_h  = prs.slide_height
    margin   = Inches(0.4)
    padding  = Inches(0.2)

    add_label(slide, title,
              margin, Inches(0.1), slide_w - margin * 2, Inches(0.4),
              font_size=16, bold=True)

    cols    = 2
    rows    = 2
    img_w   = (slide_w - margin * 2 - padding * (cols - 1)) / cols
    img_h   = (slide_h - Inches(0.55) - margin - padding * (rows - 1)) / rows

    for i, b64 in enumerate(graphs):
        if i >= cols * rows:
            break
        col = i % cols
        row = i // cols
        x   = margin + col * (img_w + padding)
        y   = Inches(0.55) + row * (img_h + padding)
        if b64:
            try:
                add_image_from_base64(slide, b64, x, y, img_w, img_h)
            except Exception:
                pass

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port  = int(os.environ.get("PORT", 5000))
    app.run(debug=debug, port=port)