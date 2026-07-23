"""
SKETCH ONLY — would become api/detect-bands.py.

Mirrors the existing serverless wrappers exactly (compare api/extract.py and
api/render-composite.py). Each file under api/ is one Vercel Python function; it
imports the shared Flask `app` from api/index.py and mounts the view at "/", because
Vercel routes the request path (/api/detect-bands) to this file and strips it.

Locally, the double @app.route("/detect-bands") / @app.route("/api/detect-bands")
decorators in backend/app.py already serve it off the single Flask process on :5001,
so this wrapper only matters for the Vercel deployment.
"""

from index import app  # noqa: F401
from app import detect_bands  # noqa: E402

app.add_url_rule("/", endpoint="detect_bands_root", view_func=detect_bands, methods=["POST"])
