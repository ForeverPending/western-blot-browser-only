from index import app  # noqa: F401
from app import channel_stats  # noqa: E402

app.add_url_rule("/", endpoint="channel_stats_root", view_func=channel_stats, methods=["POST"])
