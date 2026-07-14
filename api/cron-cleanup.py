from index import app  # noqa: F401
from app import cron_cleanup  # noqa: E402

app.add_url_rule("/", endpoint="cron_cleanup_root", view_func=cron_cleanup, methods=["GET"])
