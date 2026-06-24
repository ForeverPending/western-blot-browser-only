from index import app  # noqa: F401
from app import process_storage_upload  # noqa: E402

app.add_url_rule("/", endpoint="process_upload_root", view_func=process_storage_upload, methods=["POST"])
