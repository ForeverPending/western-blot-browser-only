import os
import io
import json
import re
import sqlite3
import ssl
import uuid
import zipfile
from collections import OrderedDict
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
from time import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import numpy as np
from flask import Flask, request, jsonify, send_file, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException
from PIL import Image
import tifffile
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
import base64

try:
    import certifi
except ImportError:
    certifi = None

app = Flask(__name__)


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return jsonify({"error": error.description}), error.code
    app.logger.exception("Unhandled backend error")
    return jsonify({"error": "Unexpected backend error."}), 500


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response

# ─── CORS ─────────────────────────────────────────────────────────────────────
LOCAL_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://[::1]:8080",
    "http://[::]:8080",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://[::1]:5500",
]
ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("ALLOWED_ORIGINS", ",".join(LOCAL_ORIGINS)).split(",")
    if origin.strip()
]
CORS(
    app,
    origins=ALLOWED_ORIGINS,
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    supports_credentials=False,
    max_age=600,
)

# ─── Rate limiting ─────────────────────────────────────────────────────────────
limiter = Limiter(
    lambda: getattr(g, "owner_id", None) or get_remote_address(),
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)

# ─── Constants ────────────────────────────────────────────────────────────────
MAX_ZIP_BYTES = int(os.environ.get("MAX_ZIP_BYTES", 250 * 1024 * 1024))
MAX_TIF_BYTES = int(os.environ.get("MAX_TIF_BYTES", 100 * 1024 * 1024))
MAX_BLOTS     = 100
MAX_ZIP_ENTRIES = 400
MAX_ZIP_UNCOMPRESSED = int(os.environ.get("MAX_ZIP_UNCOMPRESSED_BYTES", 400 * 1024 * 1024))
MAX_ZIP_COMPRESSION_RATIO = 200
MAX_IMAGE_PIXELS = 80_000_000
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_JPEG_BYTES = 50 * 1024 * 1024
MAX_NAME_LENGTH = 200
MAX_TIF_PAGES = 16
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", 16 * 1024 * 1024))
MAX_PPTX_SLIDES = 40
MAX_PPTX_GRAPHS = 120
MAX_PPTX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PPTX_TOTAL_IMAGE_BYTES = 40 * 1024 * 1024
BLOT_FILE_FIELDS = {
    "jpg": ("has_jpg", "jpg_bytes", "preview.jpg"),
    "700": ("has_700", "tif_700_bytes", "700.tif"),
    "800": ("has_800", "tif_800_bytes", "800.tif"),
}
ALL_BLOT_FILE_KINDS = tuple(BLOT_FILE_FIELDS)
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.environ.get("BLOT_DATA_DIR", os.path.join(BASE_DIR, "data"))
BLOT_FILE_DIR = os.path.join(DATA_DIR, "blots")
DB_PATH       = os.environ.get("BLOT_DB_PATH", os.path.join(DATA_DIR, "western_blot.sqlite3"))
STORAGE_BACKEND = os.environ.get("BLOT_STORAGE_BACKEND", "local").lower()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "western-blots")
USE_SUPABASE = STORAGE_BACKEND == "supabase"
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

# ─── In-memory blot store ─────────────────────────────────────────────────────
blot_store = {}
auth_token_cache = OrderedDict()
MAX_AUTH_CACHE_ENTRIES = 512

# ─── Persistent storage ───────────────────────────────────────────────────────

def init_storage():
    validate_app_config()
    if USE_SUPABASE:
        validate_supabase_config()
        return

    os.makedirs(BLOT_FILE_DIR, exist_ok=True)
    with db_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS blots (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL,
                folder TEXT NOT NULL,
                has_jpg INTEGER NOT NULL DEFAULT 0,
                has_700 INTEGER NOT NULL DEFAULT 0,
                has_800 INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blot_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                protein_name TEXT NOT NULL,
                channel TEXT NOT NULL,
                bg_axis TEXT NOT NULL,
                lanes_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (blot_id) REFERENCES blots(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_scans_blot_id ON scans(blot_id);
            CREATE INDEX IF NOT EXISTS idx_scans_owner_id ON scans(owner_id);
            CREATE INDEX IF NOT EXISTS idx_blots_owner_id ON blots(owner_id);
        """)
        ensure_local_column(conn, "blots", "owner_id", "TEXT NOT NULL DEFAULT ''")
        ensure_local_column(conn, "scans", "owner_id", "TEXT NOT NULL DEFAULT ''")


def ensure_local_column(conn, table, column, definition):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def db_connection():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def validate_app_config():
    missing = [
        name for name, value in (
            ("SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_PUBLISHABLE_KEY", SUPABASE_PUBLISHABLE_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Missing authentication configuration: " + ", ".join(missing))
    if SUPABASE_PUBLISHABLE_KEY.startswith("sb_secret_"):
        raise RuntimeError("SUPABASE_PUBLISHABLE_KEY must be a browser-safe publishable key.")


class AuthenticationError(Exception):
    pass


class SupabaseRequestError(RuntimeError):
    def __init__(self, status, details):
        super().__init__(f"Supabase request failed ({status})")
        self.status = status
        self.details = details


def require_user(route_fn):
    @wraps(route_fn)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return jsonify({"error": "Authentication required."}), 401
        token = authorization[7:].strip()
        try:
            user_id = verify_supabase_token(token)
        except AuthenticationError:
            return jsonify({"error": "Your session is invalid or expired."}), 401
        g.owner_id = user_id
        g.access_token = token
        return route_fn(*args, **kwargs)
    return limiter.limit("120 per minute", key_func=get_remote_address)(wrapped)


def current_owner_id():
    owner_id = getattr(g, "owner_id", None)
    if not owner_id:
        raise RuntimeError("Authenticated owner context is required.")
    return owner_id


def verify_supabase_token(token):
    token_hash = sha256(token.encode("utf-8")).hexdigest()
    cached = auth_token_cache.get(token_hash)
    now = int(time())
    if cached and cached["expires_at"] > now + 15:
        auth_token_cache.move_to_end(token_hash)
        return cached["user_id"]

    request_to_auth = Request(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={
            "apikey": SUPABASE_PUBLISHABLE_KEY,
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )
    try:
        with urlopen(request_to_auth, timeout=15, context=supabase_ssl_context()) as response:
            user = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        raise AuthenticationError() from error

    try:
        user_id = str(uuid.UUID(user["id"]))
        payload = decode_jwt_payload(token)
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as error:
        raise AuthenticationError() from error
    if expires_at <= now:
        raise AuthenticationError()

    auth_token_cache[token_hash] = {"user_id": user_id, "expires_at": expires_at}
    auth_token_cache.move_to_end(token_hash)
    while len(auth_token_cache) > MAX_AUTH_CACHE_ENTRIES:
        auth_token_cache.popitem(last=False)
    return user_id


def decode_jwt_payload(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))


def validate_supabase_config():
    missing = [
        name for name, value in (
            ("SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_BUCKET", SUPABASE_BUCKET),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Supabase storage is enabled, but these environment variables are missing: "
            + ", ".join(missing)
        )
def supabase_user_headers(extra=None):
    headers = {
        "apikey": SUPABASE_PUBLISHABLE_KEY,
        "Authorization": f"Bearer {g.access_token}",
    }
    if extra:
        headers.update(extra)
    return headers


def supabase_request(method, path, body=None, raw_body=None, headers=None, max_response_bytes=None):
    data = None
    request_headers = supabase_user_headers(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    elif raw_body is not None:
        data = raw_body

    request = Request(
        f"{SUPABASE_URL}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=60, context=supabase_ssl_context()) as response:
            response_body = response.read(max_response_bytes + 1 if max_response_bytes else -1)
            if max_response_bytes and len(response_body) > max_response_bytes:
                raise PublicError("Stored upload exceeds the configured size limit.", 413)
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        app.logger.warning("Supabase request failed with status %s: %s", error.code, details[:500])
        raise SupabaseRequestError(error.code, details) from error

    if "application/json" in content_type:
        return json.loads(response_body.decode("utf-8") or "null")
    return response_body


def supabase_ssl_context():
    if certifi:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def supabase_storage_path(blot_id, filename):
    return f"{current_owner_id()}/blots/{safe_id(blot_id)}/{filename}"


def supabase_upload_file(blot_id, filename, file_bytes, content_type):
    if not file_bytes:
        return
    object_path = quote(supabase_storage_path(blot_id, filename), safe="/")
    bucket = quote(SUPABASE_BUCKET, safe="")
    supabase_request(
        "POST",
        f"/storage/v1/object/{bucket}/{object_path}",
        raw_body=file_bytes,
        headers={
            "Content-Type": content_type,
            "x-upsert": "true",
        },
    )


def supabase_download_file(blot_id, filename):
    object_path = quote(supabase_storage_path(blot_id, filename), safe="/")
    bucket = quote(SUPABASE_BUCKET, safe="")
    try:
        return supabase_request("GET", f"/storage/v1/object/authenticated/{bucket}/{object_path}")
    except SupabaseRequestError as error:
        if not is_missing_storage_object(error):
            raise
        legacy_path = quote(f"blots/{safe_id(blot_id)}/{filename}", safe="/")
        return supabase_request("GET", f"/storage/v1/object/authenticated/{bucket}/{legacy_path}")


def is_missing_storage_object(error):
    if error.status == 404:
        return True
    if error.status != 400:
        return False
    try:
        details = json.loads(error.details)
    except (TypeError, ValueError):
        details = {}
    status_code = str(details.get("statusCode", ""))
    message = str(details.get("message", "")).lower()
    return status_code == "404" or "not found" in message


def supabase_download_upload(object_path):
    object_path = validate_owned_upload_path(object_path)
    bucket = quote(SUPABASE_BUCKET, safe="")
    encoded_path = quote(object_path, safe="/")
    return supabase_request(
        "GET",
        f"/storage/v1/object/authenticated/{bucket}/{encoded_path}",
        max_response_bytes=MAX_ZIP_BYTES,
    )


def validate_owned_upload_path(object_path):
    if not isinstance(object_path, str) or len(object_path) > 500:
        raise PublicError("Invalid upload path.")
    normalized = object_path.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if any(not part or part in (".", "..") for part in parts):
        raise PublicError("Invalid upload path.")
    expected_prefix = [current_owner_id(), "uploads"]
    if parts[:2] != expected_prefix or len(parts) != 3 or not parts[2].lower().endswith(".zip"):
        raise PublicError("Upload does not belong to the authenticated user.", 403)
    return normalized


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_id(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))
    return cleaned.strip("._") or uuid.uuid4().hex


def blot_dir(blot_id):
    return os.path.join(BLOT_FILE_DIR, safe_id(current_owner_id()), safe_id(blot_id))


def write_optional_file(directory, filename, file_bytes):
    if not file_bytes:
        return
    with open(os.path.join(directory, filename), "wb") as handle:
        handle.write(file_bytes)


def read_optional_file(directory, filename):
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def persist_blot(blot_id, blot):
    if USE_SUPABASE:
        supabase_upload_file(blot_id, "preview.jpg", blot.get("jpg_bytes"), "image/jpeg")
        supabase_upload_file(blot_id, "700.tif", blot.get("tif_700_bytes"), "image/tiff")
        supabase_upload_file(blot_id, "800.tif", blot.get("tif_800_bytes"), "image/tiff")
        supabase_request(
            "POST",
            "/rest/v1/blots?on_conflict=id",
            body={
                "id": blot_id,
                "owner_id": current_owner_id(),
                "name": blot["name"],
                "folder": blot["folder"],
                "has_jpg": bool(blot.get("jpg_bytes")),
                "has_700": bool(blot.get("tif_700_bytes")),
                "has_800": bool(blot.get("tif_800_bytes")),
                "created_at": now_iso(),
            },
            headers={
                "Prefer": "resolution=merge-duplicates",
            },
        )
        return

    directory = blot_dir(blot_id)
    os.makedirs(directory, exist_ok=True)
    write_optional_file(directory, "preview.jpg", blot.get("jpg_bytes"))
    write_optional_file(directory, "700.tif", blot.get("tif_700_bytes"))
    write_optional_file(directory, "800.tif", blot.get("tif_800_bytes"))

    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO blots
                (id, owner_id, name, folder, has_jpg, has_700, has_800, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                owner_id = excluded.owner_id,
                name = excluded.name,
                folder = excluded.folder,
                has_jpg = excluded.has_jpg,
                has_700 = excluded.has_700,
                has_800 = excluded.has_800
            """,
            (
                blot_id,
                current_owner_id(),
                blot["name"],
                blot["folder"],
                1 if blot.get("jpg_bytes") else 0,
                1 if blot.get("tif_700_bytes") else 0,
                1 if blot.get("tif_800_bytes") else 0,
                now_iso(),
            ),
        )


def blot_from_row(row):
    """Convert either a PostgREST object or SQLite row into cached metadata."""
    return {
        "name": row["name"],
        "folder": row["folder"],
        "has_jpg": bool(row["has_jpg"]),
        "has_700": bool(row["has_700"]),
        "has_800": bool(row["has_800"]),
    }


def load_blot_files(blot_id, blot, file_kinds):
    """Load only the image files required by the current endpoint."""
    directory = None if USE_SUPABASE else blot_dir(blot_id)
    for kind in file_kinds:
        has_field, bytes_field, filename = BLOT_FILE_FIELDS[kind]
        if bytes_field in blot:
            continue
        if not blot[has_field]:
            blot[bytes_field] = None
        elif USE_SUPABASE:
            blot[bytes_field] = supabase_download_file(blot_id, filename)
        else:
            blot[bytes_field] = read_optional_file(directory, filename)
    return blot


def get_blot(blot_id, file_kinds=ALL_BLOT_FILE_KINDS):
    """Authorize a blot lookup and lazily load the requested image channels."""
    owner_id = current_owner_id()
    cache_key = (owner_id, blot_id)
    blot = blot_store.get(cache_key)

    if not blot:
        if USE_SUPABASE:
            rows = supabase_request(
                "GET",
                f"/rest/v1/blots?id=eq.{quote(blot_id, safe='')}&owner_id=eq.{quote(owner_id, safe='')}&select=*",
            )
            row = rows[0] if rows else None
        else:
            with db_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM blots WHERE id = ? AND owner_id = ?",
                    (blot_id, owner_id),
                ).fetchone()
        if not row:
            return None
        blot = blot_from_row(row)
        blot_store[cache_key] = blot

    return load_blot_files(blot_id, blot, file_kinds)


def scan_row_to_dict(row):
    lanes = row["lanes_json"]
    if isinstance(lanes, str):
        lanes = json.loads(lanes)
    return {
        "id": row["id"],
        "proteinName": row["protein_name"],
        "channel": row["channel"],
        "backgroundAxis": row["bg_axis"],
        "lanes": lanes,
        "createdAt": row["created_at"],
    }


def get_scans_for_blot(blot_id):
    if USE_SUPABASE:
        rows = supabase_request(
            "GET",
            f"/rest/v1/scans?blot_id=eq.{quote(blot_id, safe='')}&owner_id=eq.{quote(current_owner_id(), safe='')}&select=*&order=id.asc",
        )
        return [scan_row_to_dict(row) for row in rows]

    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM scans
            WHERE blot_id = ? AND owner_id = ?
            ORDER BY id
            """,
            (blot_id, current_owner_id()),
        ).fetchall()
    return [scan_row_to_dict(row) for row in rows]


def blot_summary_from_row(row, scan_count=0):
    return {
        "id": row["id"],
        "name": row["name"],
        "hasJpg": bool(row["has_jpg"]),
        "has700": bool(row["has_700"]),
        "has800": bool(row["has_800"]),
        "scanCount": scan_count,
        "createdAt": row["created_at"],
    }


init_storage()

# ─── Health check ─────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ─── ZIP upload & parsing ─────────────────────────────────────────────────────

@app.route("/upload-zip", methods=["POST"])
@require_user
@limiter.limit("10 per minute")
def upload_zip():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".zip"):
        return jsonify({"error": "File must be a .zip"}), 400

    # Check content length before reading
    if request.content_length and request.content_length > MAX_ZIP_BYTES:
        return jsonify({"error": "File is too large for direct API upload. Use Storage upload instead."}), 413

    file_bytes = file.read()

    # Check actual file size after reading
    if len(file_bytes) > MAX_ZIP_BYTES:
        return jsonify({"error": "File is too large for direct API upload. Use Storage upload instead."}), 413

    # Validate ZIP magic bytes
    if not is_valid_zip(file_bytes):
        return jsonify({"error": "Invalid ZIP file."}), 400

    try:
        blots = parse_zip(file_bytes)
        return jsonify({"blots": blots})
    except PublicError as error:
        return error_response(error, "ZIP import failed.")
    except Exception as error:
        app.logger.exception("ZIP upload failed")
        return error_response(error, "ZIP import failed.")


@app.route("/process-upload", methods=["POST"])
@require_user
@limiter.limit("10 per minute")
def process_storage_upload():
    data = request.get_json(silent=True) or {}
    try:
        file_bytes = supabase_download_upload(data.get("objectPath"))
        if not is_valid_zip(file_bytes):
            raise PublicError("Stored object is not a valid ZIP file.")
        blots = parse_zip(file_bytes)
        return jsonify({"blots": blots})
    except PublicError as error:
        return error_response(error, "ZIP import failed.")
    except Exception as error:
        app.logger.exception("Stored ZIP processing failed")
        return error_response(error, "ZIP import failed.")


def is_valid_zip(file_bytes):
    return len(file_bytes) >= 4 and zipfile.is_zipfile(io.BytesIO(file_bytes))


def enforce_zip_member_size(zf, filename, limit, message):
    """Reject an optional archive member before reading oversized data into memory."""
    if filename and zf.getinfo(filename).file_size > limit:
        raise PublicError(message, 413)


def parse_zip(file_bytes):
    blots = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        validate_zip_archive(zf)
        # Group files by folder
        folders = {}
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            parts = name.split("/")
            folder = parts[0] if len(parts) > 1 else "__root__"
            folders.setdefault(folder, []).append(name)

        for folder, files in folders.items():
            txt_file = next((f for f in files if f.lower().endswith(".txt")), None)
            jpg_file = next((f for f in files if f.lower().endswith(".jpg") or f.lower().endswith(".jpeg")), None)
            tif_700  = next((f for f in files if "700" in f and f.lower().endswith(".tif")), None)
            tif_800  = next((f for f in files if "800" in f and f.lower().endswith(".tif")), None)

            if not txt_file:
                continue

            # Enforce per-file limits before loading archive members into memory.
            enforce_zip_member_size(zf, tif_700, MAX_TIF_BYTES, "A 700nm TIF exceeds the configured size limit.")
            enforce_zip_member_size(zf, tif_800, MAX_TIF_BYTES, "An 800nm TIF exceeds the configured size limit.")
            enforce_zip_member_size(zf, txt_file, MAX_TEXT_BYTES, "Blot metadata text is too large.")
            enforce_zip_member_size(zf, jpg_file, MAX_JPEG_BYTES, "Blot preview image is too large.")

            # Parse blot name from last line of txt file
            txt_content = zf.read(txt_file).decode("utf-8", errors="ignore")
            lines = txt_content.strip().splitlines()
            last_line = lines[-1] if lines else ""
            blot_name = last_line.split("=", 1)[1].strip() if last_line.startswith("Remarks=") else folder
            blot_name = blot_name[:MAX_NAME_LENGTH] or "Untitled blot"

            # Generate a unique unpredictable ID
            blot_id = f"{uuid.uuid4().hex}_{folder}".replace(" ", "_")

            # Evict oldest blots if store is full
            if len(blot_store) >= MAX_BLOTS:
                oldest_keys = list(blot_store.keys())[:10]
                for key in oldest_keys:
                    del blot_store[key]

            jpg_bytes = zf.read(jpg_file) if jpg_file else None
            tif_700_bytes = zf.read(tif_700) if tif_700 else None
            tif_800_bytes = zf.read(tif_800) if tif_800 else None
            validate_tif_pixels(tif_700_bytes, "700nm TIF")
            validate_tif_pixels(tif_800_bytes, "800nm TIF")

            # Cache the parsed blot once, then persist the same object.
            cache_key = (current_owner_id(), blot_id)
            blot = {
                "name": blot_name,
                "folder": folder,
                "has_jpg": jpg_bytes is not None,
                "has_700": tif_700_bytes is not None,
                "has_800": tif_800_bytes is not None,
                "jpg_bytes":      jpg_bytes,
                "tif_700_bytes":  tif_700_bytes,
                "tif_800_bytes":  tif_800_bytes,
            }
            blot_store[cache_key] = blot
            persist_blot(blot_id, blot)

            blots.append({
                "id": blot_id,
                "name": blot_name,
                "hasJpg": jpg_file is not None,
                "has700": tif_700 is not None,
                "has800": tif_800 is not None,
                "scanCount": 0,
            })

    return blots


class PublicError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def error_response(error, fallback):
    """Return approved client errors while hiding unexpected exception details."""
    if isinstance(error, PublicError):
        return jsonify({"error": str(error)}), error.status
    return jsonify({"error": fallback}), 500


def validate_zip_archive(zf):
    infos = [info for info in zf.infolist() if not info.is_dir()]
    if len(infos) > MAX_ZIP_ENTRIES:
        raise PublicError(f"ZIP has too many files. Maximum is {MAX_ZIP_ENTRIES}.")

    total_size = sum(info.file_size for info in infos)
    if total_size > MAX_ZIP_UNCOMPRESSED:
        raise PublicError("ZIP contents are too large after decompression.")

    seen_paths = set()
    for info in infos:
        filename = info.filename
        if not filename or len(filename) > 500 or "\x00" in filename or "\\" in filename:
            raise PublicError("ZIP contains an invalid file name.")
        path_parts = filename.split("/")
        if os.path.isabs(filename) or any(part in ("", ".", "..") for part in path_parts):
            raise PublicError("ZIP contains unsafe file paths.")
        normalized = filename.casefold()
        if normalized in seen_paths:
            raise PublicError("ZIP contains duplicate file paths.")
        seen_paths.add(normalized)
        if info.flag_bits & 0x1:
            raise PublicError("Encrypted ZIP entries are not supported.")
        if info.file_size > 0:
            ratio = info.file_size / max(1, info.compress_size)
            if ratio > MAX_ZIP_COMPRESSION_RATIO:
                raise PublicError("ZIP contains a suspiciously compressed file.")


def validate_tif_pixels(tif_bytes, label):
    if not tif_bytes:
        return
    decode_validated_tif(tif_bytes, label)


def decode_validated_tif(tif_bytes, label):
    try:
        with tifffile.TiffFile(io.BytesIO(tif_bytes)) as tif:
            pages = list(tif.pages)
            if not pages or len(pages) > MAX_TIF_PAGES:
                raise PublicError(f"{label} has an unsupported number of image pages.")

            full_resolution_pages = [page for page in pages if not page.is_reduced]
            if len(full_resolution_pages) != 1:
                raise PublicError(
                    f"{label} must contain one full-resolution image; "
                    "additional pages may only be reduced-resolution previews."
                )

            primary_page = full_resolution_pages[0]
            primary_shape = primary_page.shape
            if len(primary_shape) != 2:
                raise PublicError(f"{label} must be a two-dimensional grayscale image.")

            supported_dtypes = {np.dtype("uint16"), np.dtype("float16")}
            total_pixels = 0
            for page in pages:
                if len(page.shape) != 2:
                    raise PublicError(f"{label} contains a non-grayscale image page.")
                if page.dtype not in supported_dtypes:
                    raise PublicError(f"{label} must contain 16-bit grayscale image data.")
                page_pixels = int(np.prod(page.shape))
                if page_pixels <= 0:
                    raise PublicError(f"{label} has invalid dimensions.")
                total_pixels += page_pixels
                if page.is_reduced and (
                    page.shape[0] > primary_shape[0] or page.shape[1] > primary_shape[1]
                ):
                    raise PublicError(f"{label} contains an invalid reduced-resolution preview.")

            pixels = int(np.prod(primary_shape))
            if pixels > MAX_IMAGE_PIXELS:
                raise PublicError(f"{label} is too large. Maximum is {MAX_IMAGE_PIXELS:,} pixels.")
            if total_pixels > MAX_IMAGE_PIXELS * 2:
                raise PublicError(f"{label} contains too much decoded image data.")

            image = primary_page.asarray()
            if image.shape != primary_shape:
                raise PublicError(f"{label} contains invalid pixel data.")
            return sanitize_tif_pixels(image, label)
    except PublicError:
        raise
    except Exception as error:
        raise PublicError(f"{label} could not be read.") from error


def sanitize_tif_pixels(image, label):
    if image.dtype != np.dtype("float16"):
        return image

    finite = np.isfinite(image)
    if not finite.any():
        raise PublicError(f"{label} does not contain any finite pixel measurements.")
    if finite.all():
        return image

    nan_count = int(np.isnan(image).sum())
    posinf_count = int(np.isposinf(image).sum())
    neginf_count = int(np.isneginf(image).sum())
    app.logger.info(
        "%s contains LI-COR special pixels: %d masked, %d saturated, %d negative-overflow",
        label,
        nan_count,
        posinf_count,
        neginf_count,
    )
    limit = np.finfo(np.float16).max
    return np.nan_to_num(image, copy=True, nan=0.0, posinf=limit, neginf=-limit)


@app.route("/blots")
@require_user
@limiter.limit("60 per minute")
def list_blots():
    if USE_SUPABASE:
        rows = supabase_request(
            "GET",
            f"/rest/v1/blots?owner_id=eq.{quote(current_owner_id(), safe='')}&select=*&order=created_at.desc,name.asc",
        )
        scans_by_blot = {}
        scan_rows = supabase_request(
            "GET",
            f"/rest/v1/scans?owner_id=eq.{quote(current_owner_id(), safe='')}&select=*&order=id.asc",
        )
        for row in scan_rows:
            scans_by_blot.setdefault(row["blot_id"], []).append(scan_row_to_dict(row))

        blots = [
            blot_summary_from_row(row, len(scans_by_blot.get(row["id"], [])))
            for row in rows
        ]
        scans = {blot["id"]: scans_by_blot.get(blot["id"], []) for blot in blots}
        return jsonify({"blots": blots, "scans": scans})

    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT blots.*, COUNT(scans.id) AS scan_count
            FROM blots
            LEFT JOIN scans ON scans.blot_id = blots.id
            WHERE blots.owner_id = ?
            GROUP BY blots.id
            ORDER BY blots.created_at DESC, blots.name
            """,
            (current_owner_id(),),
        ).fetchall()

    blots = [blot_summary_from_row(row, row["scan_count"]) for row in rows]
    scans = {blot["id"]: get_scans_for_blot(blot["id"]) for blot in blots}
    return jsonify({"blots": blots, "scans": scans})


@app.route("/blots/<blot_id>/scans")
@require_user
@limiter.limit("60 per minute")
def list_scans(blot_id):
    if not get_blot(blot_id, ()):
        return jsonify({"error": "Blot not found"}), 404
    return jsonify({"scans": get_scans_for_blot(blot_id)})


@app.route("/blots/<blot_id>/scans", methods=["POST"])
@require_user
@limiter.limit("30 per minute")
def save_scan(blot_id):
    if not get_blot(blot_id, ()):
        return jsonify({"error": "Blot not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    protein_name = str(data.get("proteinName", "")).strip()
    channel = str(data.get("channel", "700"))
    background_axis = str(data.get("backgroundAxis", "leftright"))
    lanes = data.get("lanes", [])

    if not protein_name:
        return jsonify({"error": "Protein name is required."}), 400
    if len(protein_name) > MAX_NAME_LENGTH:
        return jsonify({"error": f"Protein name must be {MAX_NAME_LENGTH} characters or fewer."}), 400
    if channel not in ("700", "800"):
        return jsonify({"error": "Invalid channel."}), 400
    if background_axis not in ("leftright", "topbottom"):
        return jsonify({"error": "Invalid background mode."}), 400
    if not isinstance(lanes, list) or not lanes:
        return jsonify({"error": "At least one lane is required."}), 400
    if len(lanes) > 200:
        return jsonify({"error": "Too many lanes. Maximum is 200."}), 400

    cleaned_lanes = []
    for index, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            return jsonify({"error": f"Lane {index + 1} is invalid."}), 400
        name = str(lane.get("name", f"Lane {index + 1}")).strip() or f"Lane {index + 1}"
        if len(name) > MAX_NAME_LENGTH:
            return jsonify({"error": f"Lane {index + 1} name is too long."}), 400
        try:
            signal = float(lane.get("signal"))
        except (TypeError, ValueError):
            return jsonify({"error": f"Lane {index + 1} has an invalid signal."}), 400
        if not np.isfinite(signal):
            return jsonify({"error": f"Lane {index + 1} has an invalid signal."}), 400
        cleaned_lanes.append({"name": name, "signal": signal})

    if USE_SUPABASE:
        rows = supabase_request(
            "POST",
            "/rest/v1/scans",
            body={
                "blot_id": blot_id,
                "owner_id": current_owner_id(),
                "protein_name": protein_name,
                "channel": channel,
                "bg_axis": background_axis,
                "lanes_json": cleaned_lanes,
                "created_at": now_iso(),
            },
            headers={"Prefer": "return=representation"},
        )
        row = rows[0]
    else:
        with db_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO scans (blot_id, owner_id, protein_name, channel, bg_axis, lanes_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (blot_id, current_owner_id(), protein_name, channel, background_axis, json.dumps(cleaned_lanes), now_iso()),
            )
            scan_id = cursor.lastrowid
            row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()

    return jsonify({"scan": scan_row_to_dict(row)}), 201


@app.route("/blots/<blot_id>/scans/<int:scan_id>", methods=["DELETE"])
@require_user
@limiter.limit("30 per minute")
def delete_scan(blot_id, scan_id):
    if not get_blot(blot_id, ()):
        return jsonify({"error": "Scan not found"}), 404
    if USE_SUPABASE:
        rows = supabase_request(
            "DELETE",
            f"/rest/v1/scans?id=eq.{scan_id}&blot_id=eq.{quote(blot_id, safe='')}&owner_id=eq.{quote(current_owner_id(), safe='')}",
            headers={"Prefer": "return=representation"},
        )
        if not rows:
            return jsonify({"error": "Scan not found"}), 404
        return jsonify({"status": "deleted"})

    with db_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM scans WHERE id = ? AND blot_id = ? AND owner_id = ?",
            (scan_id, blot_id, current_owner_id()),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Scan not found"}), 404
    return jsonify({"status": "deleted"})

# ─── Serve JPG preview ────────────────────────────────────────────────────────

@app.route("/blots/<blot_id>/preview")
@require_user
@limiter.limit("60 per minute")
def blot_preview(blot_id):
    blot = get_blot(blot_id, ("jpg",))
    if not blot or not blot["jpg_bytes"]:
        return jsonify({"error": "Preview not found"}), 404
    return send_file(io.BytesIO(blot["jpg_bytes"]), mimetype="image/jpeg")

# ─── TIF composite rendering ──────────────────────────────────────────────────

@app.route("/blots/<blot_id>/composite")
@require_user
@limiter.limit("60 per minute")
def blot_composite(blot_id):
    blot = get_blot(blot_id, ("700", "800"))
    if not blot:
        return jsonify({"error": "Blot not found"}), 404
    if not blot["tif_700_bytes"] or not blot["tif_800_bytes"]:
        return jsonify({"error": "TIF files not found for this blot"}), 404

    try:
        brightness_700 = float(request.args.get("brightness700", 1.0))
        contrast_700 = float(request.args.get("contrast700", 1.0))
        brightness_800 = float(request.args.get("brightness800", 1.0))
        contrast_800 = float(request.args.get("contrast800", 1.0))
        color_mode = request.args.get("colorMode", "color")

        # Clamp adjustment values to safe ranges
        brightness_700 = max(0.1, min(5.0, brightness_700))
        contrast_700 = max(0.1, min(10.0, contrast_700))
        brightness_800 = max(0.1, min(5.0, brightness_800))
        contrast_800 = max(0.1, min(10.0, contrast_800))

        img = build_composite(
            blot["tif_700_bytes"],
            blot["tif_800_bytes"],
            brightness_700, contrast_700,
            brightness_800, contrast_800,
            color_mode,
        )

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")

    except Exception as error:
        app.logger.exception("Composite render failed")
        return error_response(error, "Could not render blot image.")


def read_tif_channel(tif_bytes):
    # Display normalization starts from the same validated pixels used for quantification.
    arr = read_raw_channel(tif_bytes)
    low  = np.percentile(arr, 1)
    high = np.percentile(arr, 99)
    if high > low:
        arr = (arr - low) / (high - low)
    else:
        arr = np.zeros_like(arr)
    return np.clip(arr, 0.0, 1.0)


def apply_adjustments(channel, brightness, contrast):
    adjusted = (channel - 0.5) * contrast + 0.5
    adjusted = adjusted + (brightness - 1.0)
    return np.clip(adjusted, 0.0, 1.0)


def build_composite(tif_700_bytes, tif_800_bytes, brightness_700, contrast_700, brightness_800, contrast_800, color_mode="color"):
    ch_700 = read_tif_channel(tif_700_bytes)
    ch_800 = read_tif_channel(tif_800_bytes)

    if ch_700.shape != ch_800.shape:
        h = max(ch_700.shape[0], ch_800.shape[0])
        w = max(ch_700.shape[1], ch_800.shape[1])
        ch_700 = np.array(Image.fromarray((ch_700 * 65535).astype(np.uint16)).resize((w, h), Image.LANCZOS)).astype(np.float32) / 65535
        ch_800 = np.array(Image.fromarray((ch_800 * 65535).astype(np.uint16)).resize((w, h), Image.LANCZOS)).astype(np.float32) / 65535

    ch_700 = apply_adjustments(ch_700, brightness_700, contrast_700)
    ch_800 = apply_adjustments(ch_800, brightness_800, contrast_800)

    if color_mode == "grayscale":
        blended = np.clip(ch_700 + ch_800, 0.0, 1.0)
        blended = 1.0 - blended
        gray = (blended * 255).astype(np.uint8)
        rgb = np.stack([gray, gray, gray], axis=2)
    else:
        r = (ch_700 * 255).astype(np.uint8)
        g = (ch_800 * 255).astype(np.uint8)
        b = np.zeros_like(r)
        rgb = np.stack([r, g, b], axis=2)

    return Image.fromarray(rgb, mode="RGB")

# ─── Signal extraction ────────────────────────────────────────────────────────

@app.route("/blots/<blot_id>/extract", methods=["POST"])
@require_user
@limiter.limit("30 per minute")
def extract_signals(blot_id):
    blot = get_blot(blot_id, ())
    if not blot:
        return jsonify({"error": "Blot not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    boxes           = data.get("boxes", [])
    channel         = data.get("channel", "700")
    background_axis = data.get("backgroundAxis", "leftright")

    # Validate channel
    if channel not in ("700", "800"):
        return jsonify({"error": "Invalid channel"}), 400
    if background_axis not in ("leftright", "topbottom"):
        return jsonify({"error": "Invalid background mode"}), 400
    if not isinstance(boxes, list):
        return jsonify({"error": "Boxes must be a list."}), 400

    # Limit number of boxes
    if len(boxes) > 200:
        return jsonify({"error": "Too many boxes. Maximum is 200."}), 400
    try:
        for box in boxes:
            validate_box(box)
        load_blot_files(blot_id, blot, (channel,))
        tif_bytes = blot["tif_700_bytes"] if channel == "700" else blot["tif_800_bytes"]
        if not tif_bytes:
            return jsonify({"error": f"No {channel}nm TIF found"}), 404
        arr = read_raw_channel(tif_bytes)
        results = [extract_box_signal(arr, box, background_axis) for box in boxes]
        return jsonify({"results": results})
    except PublicError as error:
        return error_response(error, "Signal extraction failed.")
    except Exception as error:
        app.logger.exception("Signal extraction failed")
        return error_response(error, "Signal extraction failed.")


def read_raw_channel(tif_bytes):
    return read_validated_tif(tif_bytes).astype(np.float32)


def read_validated_tif(tif_bytes):
    return decode_validated_tif(tif_bytes, "Stored TIF")


def extract_box_signal(arr, box, background_axis):
    x = max(0, int(round(box["x"])))
    y = max(0, int(round(box["y"])))
    w = max(1, int(round(box["w"])))
    h = max(1, int(round(box["h"])))

    img_h, img_w = arr.shape
    x2 = min(x + w, img_w)
    y2 = min(y + h, img_h)

    roi        = arr[y:y2, x:x2]
    raw_signal = float(np.sum(roi))

    border     = 3
    bg_pixels  = []

    if background_axis == "leftright":
        lx1 = max(0, x - border)
        lx2 = x
        if lx2 > lx1:
            bg_pixels.append(arr[y:y2, lx1:lx2].flatten())
        rx1 = x2
        rx2 = min(img_w, x2 + border)
        if rx2 > rx1:
            bg_pixels.append(arr[y:y2, rx1:rx2].flatten())
    else:
        ty1 = max(0, y - border)
        ty2 = y
        if ty2 > ty1:
            bg_pixels.append(arr[ty1:ty2, x:x2].flatten())
        by1 = y2
        by2 = min(img_h, y2 + border)
        if by2 > by1:
            bg_pixels.append(arr[by1:by2, x:x2].flatten())

    if bg_pixels:
        all_bg     = np.concatenate(bg_pixels)
        bg_median  = float(np.median(all_bg))
        n_pixels   = (y2 - y) * (x2 - x)
        background_signal = bg_median * n_pixels
    else:
        background_signal = 0.0

    adjusted_signal = max(0.0, raw_signal - background_signal)

    return {
        "rawSignal": round(raw_signal, 2),
        "backgroundSignal": round(background_signal, 2),
        "adjustedSignal": round(adjusted_signal, 2),
        "x": x, "y": y, "w": w, "h": h,
    }


def validate_box(box):
    if not isinstance(box, dict):
        raise PublicError("Each box must be an object.")
    for key in ("x", "y", "w", "h"):
        value = box.get(key)
        if not isinstance(value, (int, float)) or not np.isfinite(value):
            raise PublicError(f"Box {key} must be a finite number.")
    if box["w"] <= 0 or box["h"] <= 0:
        raise PublicError("Box width and height must be positive.")
    if box["w"] * box["h"] > MAX_IMAGE_PIXELS:
        raise PublicError("A selected box is too large.")

# ─── PowerPoint generation ────────────────────────────────────────────────────

@app.route("/generate-pptx", methods=["POST"])
@require_user
@limiter.limit("10 per minute")
def generate_pptx():
    try:
        data        = request.get_json(silent=True) or {}
        slides_data = data.get("slides", [])
        validate_pptx_payload(slides_data)

        prs              = Presentation()
        prs.slide_width  = Inches(10)
        prs.slide_height = Inches(7.5)
        blank_layout     = prs.slide_layouts[6]

        # Separate image slides from graph slides
        image_slide_data = next((s for s in slides_data if s.get("type") == "images"), None)
        graph_slides     = [s for s in slides_data if s.get("type") == "graphs"]

        # Build image slide first
        if image_slide_data:
            slide = prs.slides.add_slide(blank_layout)
            build_image_slide(slide, image_slide_data, prs)

        # Build graph slides, 4 per slide
        for slide_data in graph_slides:
            graphs   = slide_data.get("graphs", [])
            title    = slide_data.get("title", "")
            per_page = 4

            for page_start in range(0, max(1, len(graphs)), per_page):
                page_graphs = graphs[page_start:page_start + per_page]
                slide       = prs.slides.add_slide(blank_layout)
                build_graph_slide(slide, {
                    "title":  title if page_start == 0 else f"{title} (cont.)",
                    "graphs": page_graphs,
                }, prs)

        buf = io.BytesIO()
        prs.save(buf)
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            as_attachment=True,
            download_name="western-blot-analysis.pptx"
        )

    except Exception as error:
        app.logger.exception("PowerPoint generation failed")
        return error_response(error, "PowerPoint generation failed.")


def validate_pptx_payload(slides_data):
    if not isinstance(slides_data, list):
        raise PublicError("Slides must be a list.")
    if len(slides_data) > MAX_PPTX_SLIDES:
        raise PublicError(f"Too many slides. Maximum is {MAX_PPTX_SLIDES}.")

    graph_count = 0
    image_count = 0
    total_image_bytes = 0
    for slide in slides_data:
        if not isinstance(slide, dict):
            raise PublicError("Each slide must be an object.")
        images = slide.get("images", [])
        graphs = slide.get("graphs", [])
        if not isinstance(images, list) or not isinstance(graphs, list):
            raise PublicError("PowerPoint images and graphs must be lists.")
        image_count += len(images)
        if image_count > MAX_PPTX_SLIDES:
            raise PublicError("Too many blot images in PowerPoint export.")
        graph_count += len(graphs)
        for item in images:
            if not isinstance(item, dict):
                raise PublicError("Invalid blot image entry.")
            total_image_bytes += validate_data_url_size(item.get("image", ""))
            if len(str(item.get("label", ""))) > MAX_NAME_LENGTH:
                raise PublicError("A PowerPoint image label is too long.")
        for graph in graphs:
            total_image_bytes += validate_data_url_size(graph)
    if graph_count > MAX_PPTX_GRAPHS:
        raise PublicError(f"Too many graphs. Maximum is {MAX_PPTX_GRAPHS}.")
    if total_image_bytes > MAX_PPTX_TOTAL_IMAGE_BYTES:
        raise PublicError("PowerPoint image data exceeds the total size limit.", 413)


def validate_data_url_size(value):
    if not isinstance(value, str):
        raise PublicError("Invalid image data.")
    if not value.startswith("data:image/") or ";base64," not in value[:100]:
        raise PublicError("Export images must be base64 image data URLs.")
    encoded = value.split(",", 1)[-1]
    approx_bytes = (len(encoded) * 3) // 4
    if approx_bytes > MAX_PPTX_IMAGE_BYTES:
        raise PublicError("An exported image is too large for PowerPoint.")
    return approx_bytes


def add_image_from_base64(slide, b64_string, left, top, width, height):
    img_bytes  = base64.b64decode(b64_string.split(",")[-1], validate=True)
    img_stream = io.BytesIO(img_bytes)
    slide.shapes.add_picture(img_stream, left, top, width, height)


def add_label(slide, text, left, top, width, height, font_size=14, bold=False, align=PP_ALIGN.CENTER):
    text_box        = slide.shapes.add_textbox(left, top, width, height)
    tf              = text_box.text_frame
    tf.word_wrap   = True
    p              = tf.paragraphs[0]
    p.alignment    = align
    run            = p.add_run()
    run.text       = text
    run.font.size  = Pt(font_size)
    run.font.bold  = bold
    run.font.color.rgb = RGBColor(0x1f, 0x29, 0x33)


def build_image_slide(slide, slide_data, prs):
    images  = slide_data.get("images", [])
    if not images:
        return

    slide_w = prs.slide_width
    slide_h = prs.slide_height
    margin  = Inches(0.4)
    label_h = Inches(0.3)
    spacing = Inches(0.2)

    add_label(slide, "Western Blot Images",
              margin, Inches(0.1), slide_w - margin * 2, Inches(0.35),
              font_size=18, bold=True)

    n               = len(images)
    total_label     = label_h * n
    total_spacing   = spacing * (n - 1)
    available_h     = slide_h - Inches(0.6) - total_label - total_spacing - margin
    full_img_w      = slide_w - margin * 2
    img_w           = full_img_w * 2 / 3
    img_h           = available_h / n
    left_x          = margin + (full_img_w - img_w) / 2

    current_y = Inches(0.6)
    for item in images:
        b64   = item.get("image")
        label = item.get("label", "")
        if b64:
            try:
                add_image_from_base64(slide, b64, left_x, current_y, img_w, img_h)
            except Exception:
                pass
        current_y += img_h
        add_label(slide, label, left_x, current_y, img_w, label_h,
                  font_size=12, bold=True, align=PP_ALIGN.CENTER)
        current_y += label_h + spacing


def build_graph_slide(slide, slide_data, prs):
    graphs  = slide_data.get("graphs", [])
    title   = slide_data.get("title", "")
    if not graphs:
        return

    slide_w  = prs.slide_width
    slide_h  = prs.slide_height
    margin   = Inches(0.4)
    padding  = Inches(0.2)

    add_label(slide, title,
              margin, Inches(0.1), slide_w - margin * 2, Inches(0.4),
              font_size=16, bold=True)

    cols    = 2
    rows    = 2
    img_w   = (slide_w - margin * 2 - padding * (cols - 1)) / cols
    img_h   = (slide_h - Inches(0.55) - margin - padding * (rows - 1)) / rows

    for i, b64 in enumerate(graphs):
        if i >= cols * rows:
            break
        col = i % cols
        row = i // cols
        x   = margin + col * (img_w + padding)
        y   = Inches(0.55) + row * (img_h + padding)
        if b64:
            try:
                add_image_from_base64(slide, b64, x, y, img_w, img_h)
            except Exception:
                pass

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port  = int(os.environ.get("PORT", 5000))
    app.run(debug=debug, port=port)
