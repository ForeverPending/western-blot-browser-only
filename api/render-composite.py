from index import app  # noqa: F401
from app import render_composite  # noqa: E402

app.add_url_rule("/", endpoint="render_composite_root", view_func=render_composite, methods=["POST"])
