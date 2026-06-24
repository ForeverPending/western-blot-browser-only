from index import app  # noqa: F401
from app import generate_pptx  # noqa: E402

app.add_url_rule("/", endpoint="generate_pptx_root", view_func=generate_pptx, methods=["POST"])
