from index import app  # noqa: F401
from app import detect_bands  # noqa: E402

app.add_url_rule("/", endpoint="detect_bands_root", view_func=detect_bands, methods=["POST"])
