from index import app  # noqa: F401
from app import blot_fingerprints  # noqa: E402

app.add_url_rule("/", endpoint="blot_fingerprints_root", view_func=blot_fingerprints, methods=["POST"])
