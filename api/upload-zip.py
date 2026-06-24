from index import app  # noqa: F401
from app import upload_zip  # noqa: E402

app.add_url_rule("/", endpoint="upload_zip_root", view_func=upload_zip, methods=["POST"])
