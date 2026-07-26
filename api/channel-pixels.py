from index import app  # noqa: F401
from app import channel_pixels  # noqa: E402

app.add_url_rule("/", endpoint="channel_pixels_root", view_func=channel_pixels, methods=["POST"])
