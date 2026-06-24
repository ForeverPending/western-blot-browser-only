from index import app  # noqa: F401
from app import health  # noqa: E402

app.view_functions["frontend_index"] = health
app.add_url_rule("/", endpoint="health_root", view_func=health, methods=["GET"])
