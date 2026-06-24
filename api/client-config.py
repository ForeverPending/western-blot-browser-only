from index import app  # noqa: F401
from app import client_config  # noqa: E402

app.view_functions["frontend_index"] = client_config
app.add_url_rule("/", endpoint="client_config_root", view_func=client_config, methods=["GET"])
