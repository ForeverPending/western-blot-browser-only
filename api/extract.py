from index import app  # noqa: F401
from app import extract_payload_signals  # noqa: E402

app.add_url_rule("/", endpoint="extract_root", view_func=extract_payload_signals, methods=["POST"])
