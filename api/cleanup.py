from index import app  # noqa: F401
from app import cleanup_temp_files  # noqa: E402

app.add_url_rule("/", endpoint="cleanup_root", view_func=cleanup_temp_files, methods=["POST"])
