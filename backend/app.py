import os
import io
import json
import re
import hashlib
import hmac
import random
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import numpy as np
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException
from PIL import Image
import tifffile

app = Flask(__name__)

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
FRONTEND_FILES = {
    "index.html",
    "styles.css",
    "config.js",
    "app.js",
    "privacy.html",
    "terms.html",
    "accessibility.html",
    "vendor/xlsx.full.min.js",
}
FRONTEND_CONNECT_SOURCES = (
    "'self'",
    "https://vercel.com",
    "https://blob.vercel-storage.com",
    "https://*.blob.vercel-storage.com",
    "https://*.public.blob.vercel-storage.com",
    "https://*.private.blob.vercel-storage.com",
)
LOCAL_FRONTEND_CONNECT_SOURCES = (
    "http://127.0.0.1:*",
    "http://localhost:*",
)


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return jsonify({"error": error.description}), error.code
    app.logger.exception("Unhandled backend error")
    return jsonify({"error": "Unexpected backend error."}), 500


@app.errorhandler(413)
def handle_request_too_large(error):
    # Werkzeug rejects bodies over MAX_CONTENT_LENGTH before a view runs; give a
    # clearer hint than the default "capacity limit" message.
    return jsonify({
        "error": "Upload is too large for direct API processing. Use the Storage (Blob) upload path for large ZIP files.",
    }), 413


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


def frontend_file_response(filename="index.html"):
    filename = filename.strip("/")
    if filename not in FRONTEND_FILES:
        return jsonify({"error": "Not found."}), 404
    response = send_from_directory(FRONTEND_DIR, filename)
    response.headers["Content-Security-Policy"] = frontend_csp()
    return response


def frontend_csp():
    connect_sources = list(FRONTEND_CONNECT_SOURCES)
    if is_local_request_host():
        connect_sources.extend(LOCAL_FRONTEND_CONNECT_SOURCES)
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "font-src 'self'; "
        "img-src 'self' blob: data:; "
        f"connect-src {' '.join(connect_sources)}; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )


def is_local_request_host():
    hostname = (urlparse(f"//{request.host}").hostname or "").lower()
    return hostname in ("localhost", "127.0.0.1", "::1")


@app.route("/", methods=["GET"])
def frontend_index():
    return frontend_file_response("index.html")


@app.route("/frontend/<path:filename>", methods=["GET"])
def frontend_prefixed_file(filename):
    return frontend_file_response(filename)


@app.route("/<path:filename>", methods=["GET"])
def frontend_asset_file(filename):
    return frontend_file_response(filename)

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
def configured_allowed_origins():
    origins = [
        origin.strip().rstrip("/")
        for origin in os.environ.get("ALLOWED_ORIGINS", ",".join(LOCAL_ORIGINS)).split(",")
        if origin.strip()
    ]
    for origin in origins:
        parsed = urlparse(origin)
        if origin == "*" or parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError("ALLOWED_ORIGINS must contain exact http(s) origins; wildcards are not allowed.")
        if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
            raise RuntimeError("ALLOWED_ORIGINS entries must not contain paths, query strings, or fragments.")
    return origins


ALLOWED_ORIGINS = configured_allowed_origins()
CORS(
    app,
    origins=ALLOWED_ORIGINS,
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Blot-Session"],
    supports_credentials=False,
    max_age=600,
)

# ─── Rate limiting ─────────────────────────────────────────────────────────────
def client_ip_key():
    # Vercel documents x-vercel-forwarded-for as the platform-owned client IP
    # header. Only trust proxy headers when the function is actually running on
    # Vercel; a directly exposed local Flask server must use its peer address.
    if os.environ.get("VERCEL") == "1":
        forwarded = (
            request.headers.get("X-Vercel-Forwarded-For")
            or request.headers.get("X-Forwarded-For")
            or request.headers.get("X-Real-IP")
            or ""
        )
        client = forwarded.split(",")[0].strip()
        if client:
            return client
    return get_remote_address()


# Wire RATELIMIT_ENABLED into Flask config; flask-limiter reads app.config, not
# os.environ, so setting the env var alone would otherwise have no effect.
app.config["RATELIMIT_ENABLED"] = (
    os.environ.get("RATELIMIT_ENABLED", "true").strip().lower() not in ("false", "0", "no")
)
RATE_LIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
ALLOW_IN_MEMORY_RATE_LIMITS = (
    os.environ.get("ALLOW_IN_MEMORY_RATE_LIMITS", "false").strip().lower()
    in ("true", "1", "yes")
)
if (
    os.environ.get("VERCEL_ENV") == "production"
    and app.config["RATELIMIT_ENABLED"]
    and RATE_LIMIT_STORAGE_URI.startswith("memory://")
    and not ALLOW_IN_MEMORY_RATE_LIMITS
):
    raise RuntimeError(
        "Production rate limits require a shared RATELIMIT_STORAGE_URI. "
        "Set ALLOW_IN_MEMORY_RATE_LIMITS=true only when equivalent edge limits are configured."
    )

# RATELIMIT_STORAGE_URI defaults to in-process memory, which does NOT persist or
# share across serverless invocations. Set it to a shared backend (e.g. Redis /
# Upstash) in production so limits are actually enforced.
limiter = Limiter(
    client_ip_key,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=RATE_LIMIT_STORAGE_URI,
)

# ─── Constants ────────────────────────────────────────────────────────────────
MAX_ZIP_BYTES = int(os.environ.get("MAX_ZIP_BYTES", 250 * 1024 * 1024))
MAX_TIF_BYTES = int(os.environ.get("MAX_TIF_BYTES", 100 * 1024 * 1024))
MAX_ZIP_ENTRIES = 400
MAX_ZIP_UNCOMPRESSED = int(os.environ.get("MAX_ZIP_UNCOMPRESSED_BYTES", 400 * 1024 * 1024))
MAX_ZIP_COMPRESSION_RATIO = 200
MAX_IMAGE_PIXELS = 80_000_000
# Rendering a composite decodes two channels to float32 and makes several resize
# copies, so cap the per-channel size lower than the extraction limit to bound
# peak memory on small serverless functions.
MAX_RENDER_PIXELS = int(os.environ.get("MAX_RENDER_PIXELS", 16_000_000))
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_JPEG_BYTES = 50 * 1024 * 1024
MAX_NAME_LENGTH = 200
MAX_TIF_PAGES = 16
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", 16 * 1024 * 1024))
JPEG_MIMETYPE = "image/jpeg"
TIFF_MIMETYPE = "image/tiff"
BLOT_TIMEZONE_OFFSETS = {
    "UTC": 0,
    "GMT": 0,
    "PST": -8 * 60,
    "PDT": -7 * 60,
    "MST": -7 * 60,
    "MDT": -6 * 60,
    "CST": -6 * 60,
    "CDT": -5 * 60,
    "EST": -5 * 60,
    "EDT": -4 * 60,
    "AKST": -9 * 60,
    "AKDT": -8 * 60,
    "HST": -10 * 60,
    "AST": -4 * 60,
    "ADT": -3 * 60,
    "NST": -(3 * 60 + 30),
    "NDT": -(2 * 60 + 30),
    "CET": 1 * 60,
    "CEST": 2 * 60,
}
BLOT_TIMESTAMP_PATTERN = re.compile(
    r"^#(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})\s+(?P<clock>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<zone>[A-Za-z]{2,5}|[+-]\d{4})\s+(?P<year>\d{4})$"
)
BLOT_FILE_FIELDS = {
    "jpg": ("has_jpg", "jpg_bytes", "preview.jpg"),
    "700": ("has_700", "tif_700_bytes", "700.tif"),
    "800": ("has_800", "tif_800_bytes", "800.tif"),
}
ALL_BLOT_FILE_KINDS = tuple(BLOT_FILE_FIELDS)
BLOT_FILE_LIMITS = {
    "jpg": MAX_JPEG_BYTES,
    "700": MAX_TIF_BYTES,
    "800": MAX_TIF_BYTES,
}
SAFE_PATH_PART_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
SESSION_FILE_NAMES = {field[2] for field in BLOT_FILE_FIELDS.values()}
TEMP_STORAGE_BACKEND = os.environ.get("BLOT_TEMP_STORAGE", "local").lower().replace("_", "-")
LOCAL_TEMP_DIR = os.environ.get("BLOT_TEMP_DIR") or os.path.join(
    tempfile.gettempdir(),
    "western-blot-browser-only",
)
USE_VERCEL_BLOB = TEMP_STORAGE_BACKEND == "vercel-blob"


def env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes")

# Client cleanup only runs while a tab is open, so temp files from sessions whose
# tab was closed are orphaned. Local development sweeps on upload; Vercel uses a
# daily authenticated cron by default so cold instances do not each LIST storage.
TEMP_FILE_TTL_SECONDS = int(os.environ.get("TEMP_FILE_TTL_SECONDS", 24 * 60 * 60))
TEMP_SWEEP_MAX_DELETES = int(os.environ.get("TEMP_SWEEP_MAX_DELETES", 500))
TEMP_SWEEP_MAX_PAGES = int(os.environ.get("TEMP_SWEEP_MAX_PAGES", 10))
TEMP_SWEEP_ON_UPLOAD = env_flag("TEMP_SWEEP_ON_UPLOAD", default=not USE_VERCEL_BLOB)
# The sweep fires on every upload; a full LIST+DELETE scan per request amplifies
# Blob API cost under load. Run it at most once per interval per process — ample
# for a multi-hour TTL. Set to 0 to sweep on every upload (previous behavior).
TEMP_SWEEP_MIN_INTERVAL_SECONDS = int(os.environ.get("TEMP_SWEEP_MIN_INTERVAL_SECONDS", 300))
MAX_BLOB_LIST_BYTES = 8 * 1024 * 1024


def resolve_blob_access():
    """Public access makes stored blot images world-readable by URL with no auth
    in front of them. Require an explicit acknowledgment so it cannot be flipped
    on by a single stray env var; otherwise fall back to private."""
    requested = (os.environ.get("BLOB_ACCESS") or "").strip().lower()
    if requested != "public":
        return "private"
    acknowledged = (os.environ.get("BLOB_PUBLIC_ACCESS_ACK") or "").strip().lower() in ("true", "1", "yes")
    if not acknowledged:
        print(
            json.dumps({
                "event": "blob_access_public_blocked",
                "message": (
                    "BLOB_ACCESS=public ignored because BLOB_PUBLIC_ACCESS_ACK is not set; "
                    "stored blot images would be world-readable. Falling back to private."
                ),
            }),
            flush=True,
        )
        return "private"
    print(
        json.dumps({
            "event": "blob_access_public_enabled",
            "message": "Vercel Blob temporary files will be PUBLICLY readable by URL.",
        }),
        flush=True,
    )
    return "public"


BLOB_ACCESS = resolve_blob_access()
RUNTIME_BLOB_ACCESS = BLOB_ACCESS
BLOB_API_BASE_URL = "https://vercel.com/api/blob"
BLOB_API_VERSION = "12"
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

def init_storage():
    if USE_VERCEL_BLOB:
        if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
            raise RuntimeError("BLOB_READ_WRITE_TOKEN is required when BLOT_TEMP_STORAGE=vercel-blob.")
        return
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_blot_created_at(value):
    match = BLOT_TIMESTAMP_PATTERN.fullmatch(str(value or "").strip())
    if not match:
        return None

    zone_name = match.group("zone").upper()
    if re.fullmatch(r"[+-]\d{4}", zone_name):
        sign = 1 if zone_name[0] == "+" else -1
        offset_minutes = sign * (int(zone_name[1:3]) * 60 + int(zone_name[3:5]))
    else:
        offset_minutes = BLOT_TIMEZONE_OFFSETS.get(zone_name)
    if offset_minutes is None or abs(offset_minutes) > 14 * 60:
        return None

    try:
        parsed = datetime.strptime(
            "{weekday} {month} {day} {clock} {year}".format(**match.groupdict()),
            "%a %b %d %H:%M:%S %Y",
        )
    except ValueError:
        return None

    captured_at = parsed.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
    return captured_at.astimezone(timezone.utc).isoformat(timespec="seconds")


def safe_id(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))
    return cleaned.strip("._") or uuid.uuid4().hex


def log_token(value):
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def log_temp_path(path):
    parts = str(path or "").split("/")
    if len(parts) >= 4 and parts[0] == "sessions":
        return f"sessions/{log_token(parts[1])}/{log_token(parts[2])}/{parts[3]}"
    if len(parts) >= 3 and parts[0] == "uploads":
        return f"uploads/{log_token(parts[1])}/{log_token(parts[2])}"
    return ""


def log_blob_url_path(parsed_url):
    redacted = log_temp_path(unquote((parsed_url.path or "").lstrip("/")))
    return f"/{redacted}" if redacted else parsed_url.path


def request_session_id(data=None):
    if data is None:
        data = {}
    candidate = (
        request.headers.get("X-Blot-Session")
        or request.form.get("sessionId")
        or data.get("sessionId")
        or data.get("session_id")
        or ""
    )
    return safe_id(candidate)[:80] if candidate else uuid.uuid4().hex


def temp_object_path(session_id, blot_id, filename):
    return f"sessions/{safe_id(session_id)}/{safe_id(blot_id)}/{safe_id(filename)}"


def validate_temp_path(path, session_id=None, allow_uploads=False):
    path = str(path or "")
    if len(path) > 500 or not path:
        raise PublicError("Invalid temporary file reference.")
    if "\\" in path or path.startswith("/") or "\x00" in path:
        raise PublicError("Invalid temporary file reference.")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PublicError("Invalid temporary file reference.")
    if any(not SAFE_PATH_PART_PATTERN.fullmatch(part) for part in parts):
        raise PublicError("Invalid temporary file reference.")
    allowed_roots = {"sessions"}
    if allow_uploads:
        allowed_roots.add("uploads")
    if parts[0] not in allowed_roots:
        raise PublicError("Invalid temporary file reference.")
    if parts[0] == "sessions":
        if len(parts) != 4 or parts[3] not in SESSION_FILE_NAMES:
            raise PublicError("Invalid temporary file reference.")
    if parts[0] == "uploads":
        if len(parts) != 3 or not parts[2].lower().endswith(".zip"):
            raise PublicError("Invalid temporary file reference.")
    if session_id and len(parts) > 1 and parts[1] != safe_id(session_id):
        raise PublicError("Temporary file does not belong to this browser session.", 403)
    return "/".join(parts)


def descriptor_path(descriptor, session_id=None, allow_uploads=False):
    if not isinstance(descriptor, dict):
        raise PublicError("Invalid temporary file reference.")
    path = descriptor.get("pathname") or descriptor.get("path")
    return validate_temp_path(path, session_id, allow_uploads=allow_uploads)


def local_temp_file_path(path):
    path = validate_temp_path(path, allow_uploads=True)
    full_path = os.path.abspath(os.path.join(LOCAL_TEMP_DIR, *path.split("/")))
    root = os.path.abspath(LOCAL_TEMP_DIR)
    if not full_path.startswith(root + os.sep):
        raise PublicError("Invalid temporary file reference.")
    return full_path


def descriptor_from_blob_result(result, fallback_path, content_type):
    result = result or {}
    return {
        "backend": "vercel-blob",
        "path": result.get("pathname") or result.get("path") or fallback_path,
        "pathname": result.get("pathname") or result.get("path") or fallback_path,
        "url": result.get("url"),
        "downloadUrl": result.get("downloadUrl") or result.get("download_url"),
        "contentType": result.get("contentType") or result.get("content_type") or content_type,
    }


def vercel_blob_token():
    token = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is required when BLOT_TEMP_STORAGE=vercel-blob.")
    return token


def normalize_vercel_blob_store_id(store_id):
    store_id = str(store_id or "").strip()
    return store_id[6:] if store_id.startswith("store_") else store_id


def vercel_blob_store_id():
    store_id = os.environ.get("BLOB_STORE_ID") or os.environ.get("blob_store_id")
    if store_id:
        return normalize_vercel_blob_store_id(store_id)

    token = vercel_blob_token()
    parts = token.split("_")
    if len(parts) >= 4 and parts[3]:
        return normalize_vercel_blob_store_id(parts[3])
    raise RuntimeError("BLOB_STORE_ID could not be inferred from BLOB_READ_WRITE_TOKEN.")


def is_allowed_vercel_blob_host(hostname):
    host = (hostname or "").lower().rstrip(".")
    return host == "blob.vercel-storage.com" or host.endswith(".blob.vercel-storage.com")


def validate_vercel_blob_api_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "vercel.com":
        raise PublicError("Invalid Blob API request.")
    if parsed.path != "/api/blob" and not parsed.path.startswith("/api/blob/"):
        raise PublicError("Invalid Blob API request.")
    return url


def validate_vercel_blob_url(url, expected_path=None):
    if not isinstance(url, str):
        raise PublicError("Invalid temporary file reference.")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not is_allowed_vercel_blob_host(parsed.hostname):
        raise PublicError("Invalid temporary file reference.")
    if expected_path is not None:
        decoded_path = unquote(parsed.path.lstrip("/"))
        if decoded_path != expected_path:
            raise PublicError("Temporary file URL does not match its storage path.", 403)
    return url


def vercel_blob_retry_delay(base_delay, attempt):
    return base_delay * (2 ** attempt) + random.uniform(0, base_delay)


def log_vercel_blob_retry(method, url, attempt, retries, reason):
    parsed = urlparse(url)
    print(
        json.dumps({
            "event": "vercel_blob_request_retry",
            "method": method,
            "attempt": attempt + 1,
            "maxAttempts": retries + 1,
            "urlHost": parsed.hostname,
            "urlPath": log_blob_url_path(parsed),
            "reason": reason,
        }),
        flush=True,
    )


def vercel_blob_request(
    method,
    url,
    body=None,
    headers=None,
    timeout=60,
    max_response_bytes=None,
    api_request=False,
    retries=0,
    retry_delay=0.5,
    output_file=None,
):
    if api_request:
        validate_vercel_blob_api_url(url)
    else:
        validate_vercel_blob_url(url)
    store_id = vercel_blob_store_id()
    request_headers = {
        "authorization": f"Bearer {vercel_blob_token()}",
        "x-api-blob-request-id": f"{store_id}:{int(time.time() * 1000)}:{uuid.uuid4().hex[:12]}",
        "x-api-blob-request-attempt": "0",
        "x-api-version": BLOB_API_VERSION,
        "x-vercel-blob-store-id": store_id,
    }
    if headers:
        request_headers.update(headers)
    retry_statuses = {429, 500, 502, 503, 504}

    for attempt in range(retries + 1):
        if output_file is not None:
            output_file.seek(0)
            output_file.truncate()
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                if output_file is not None:
                    response_size = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        response_size += len(chunk)
                        if max_response_bytes is not None and response_size > max_response_bytes:
                            raise PublicError("Temporary file exceeds the configured size limit.", 413)
                        output_file.write(chunk)
                    output_file.flush()
                    response_body = None
                elif max_response_bytes is None:
                    response_body = response.read()
                else:
                    response_body = response.read(max_response_bytes + 1)
                    if len(response_body) > max_response_bytes:
                        raise PublicError("Temporary file exceeds the configured size limit.", 413)
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as error:
            error_body = ""
            try:
                error_body = error.read(1024).decode("utf-8", "replace")
            finally:
                error.close()
            parsed = urlparse(url)
            print(
                json.dumps({
                    "event": "vercel_blob_request_error",
                    "method": method,
                    "status": error.code,
                    "reason": error.reason,
                    "urlHost": parsed.hostname,
                    "urlPath": log_blob_url_path(parsed),
                    "response": error_body[:500],
                }),
                flush=True,
            )
            public_error = PublicError("Temporary Blob storage request failed.", 502)
            public_error.blob_status = error.code
            public_error.blob_response = error_body
            if attempt < retries and error.code in retry_statuses:
                log_vercel_blob_retry(method, url, attempt, retries, str(error.code))
                time.sleep(vercel_blob_retry_delay(retry_delay, attempt))
                continue
            raise public_error from error
        except (URLError, TimeoutError) as error:
            parsed = urlparse(url)
            print(
                json.dumps({
                    "event": "vercel_blob_request_error",
                    "method": method,
                    "urlHost": parsed.hostname,
                    "urlPath": log_blob_url_path(parsed),
                    "error": str(error),
                }),
                flush=True,
            )
            public_error = PublicError("Temporary Blob storage request failed.", 502)
            public_error.blob_response = str(error)
            if attempt < retries:
                log_vercel_blob_retry(method, url, attempt, retries, str(error))
                time.sleep(vercel_blob_retry_delay(retry_delay, attempt))
                continue
            raise public_error from error
        if output_file is not None:
            return response_size
        if not response_body:
            return None
        if "application/json" in content_type:
            return json.loads(response_body.decode("utf-8"))
        return response_body
    raise PublicError("Temporary Blob storage request failed.", 502)


def is_private_store_access_error(error):
    response = getattr(error, "blob_response", "")
    return (
        getattr(error, "blob_status", None) == 400
        and "Cannot use public access on a private store" in response
    )


def vercel_blob_put_with_access(path, file_bytes, content_type, access):
    encoded_path = quote(path, safe="/")
    return vercel_blob_request(
        "PUT",
        f"{BLOB_API_BASE_URL}/?pathname={encoded_path}",
        body=file_bytes,
        headers={
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "0",
            "x-content-length": str(len(file_bytes)),
            "x-content-type": content_type,
            "x-cache-control-max-age": "60",
            "x-vercel-blob-access": access,
        },
        api_request=True,
    )


def vercel_blob_put(path, file_bytes, content_type):
    global RUNTIME_BLOB_ACCESS
    try:
        return vercel_blob_put_with_access(path, file_bytes, content_type, RUNTIME_BLOB_ACCESS)
    except PublicError as error:
        if RUNTIME_BLOB_ACCESS == "public" and is_private_store_access_error(error):
            RUNTIME_BLOB_ACCESS = "private"
            print(
                json.dumps({
                    "event": "vercel_blob_access_fallback",
                    "fromAccess": "public",
                    "toAccess": "private",
                    "path": log_temp_path(path),
                }),
                flush=True,
            )
            return vercel_blob_put_with_access(path, file_bytes, content_type, RUNTIME_BLOB_ACCESS)
        raise


def vercel_blob_read(descriptor, path, max_bytes=None):
    url = descriptor.get("downloadUrl") or descriptor.get("url")
    validate_vercel_blob_url(url, path)
    return vercel_blob_request("GET", url, max_response_bytes=max_bytes, retries=2)


def vercel_blob_read_to_file(descriptor, path, output_file, max_bytes=None):
    url = descriptor.get("downloadUrl") or descriptor.get("url")
    validate_vercel_blob_url(url, path)
    return vercel_blob_request(
        "GET",
        url,
        max_response_bytes=max_bytes,
        retries=2,
        output_file=output_file,
    )


def vercel_blob_delete(descriptor_paths):
    urls = []
    for descriptor, path in descriptor_paths:
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("url"), str):
            continue
        try:
            urls.append(validate_vercel_blob_url(descriptor["url"], path))
        except PublicError:
            continue
    if not urls:
        return 0
    body = json.dumps({"urls": urls}).encode("utf-8")
    vercel_blob_request(
        "POST",
        f"{BLOB_API_BASE_URL}/delete",
        body=body,
        headers={"Content-Type": "application/json"},
        api_request=True,
        retries=2,
    )
    return len(urls)


def store_temp_file(session_id, blot_id, filename, file_bytes, content_type):
    if not file_bytes:
        return None
    path = temp_object_path(session_id, blot_id, filename)
    if USE_VERCEL_BLOB:
        result = vercel_blob_put(path, file_bytes, content_type)
        return descriptor_from_blob_result(result, path, content_type)

    full_path = local_temp_file_path(path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as handle:
        handle.write(file_bytes)
    return {
        "backend": "local",
        "path": path,
        "pathname": path,
        "contentType": content_type,
    }


def read_temp_file(descriptor, session_id=None, allow_uploads=False, max_bytes=None):
    path = descriptor_path(descriptor, session_id, allow_uploads=allow_uploads)
    if USE_VERCEL_BLOB:
        return vercel_blob_read(descriptor, path, max_bytes=max_bytes)

    full_path = local_temp_file_path(path)
    if not os.path.exists(full_path):
        raise PublicError("Temporary file was not found.", 404)
    with open(full_path, "rb") as handle:
        if max_bytes is None:
            return handle.read()
        file_bytes = handle.read(max_bytes + 1)
    if len(file_bytes) > max_bytes:
        raise PublicError("Temporary file exceeds the configured size limit.", 413)
    return file_bytes


def copy_temp_file_to_handle(
    descriptor,
    output_file,
    session_id=None,
    allow_uploads=False,
    max_bytes=None,
):
    """Stream a temporary object into a seekable file without retaining the
    complete object in process memory. Returns the copied byte count."""
    path = descriptor_path(descriptor, session_id, allow_uploads=allow_uploads)
    if USE_VERCEL_BLOB:
        return vercel_blob_read_to_file(descriptor, path, output_file, max_bytes=max_bytes)

    full_path = local_temp_file_path(path)
    if not os.path.exists(full_path):
        raise PublicError("Temporary file was not found.", 404)
    size = os.path.getsize(full_path)
    if max_bytes is not None and size > max_bytes:
        raise PublicError("Temporary file exceeds the configured size limit.", 413)
    with open(full_path, "rb") as source:
        shutil.copyfileobj(source, output_file, length=1024 * 1024)
    output_file.flush()
    return size


def delete_temp_files(descriptors, session_id=None, allow_uploads=False):
    checked_descriptors = []
    invalid_count = 0
    for descriptor in descriptors:
        try:
            path = descriptor_path(descriptor, session_id, allow_uploads=allow_uploads)
            checked_descriptors.append((descriptor, path))
        except PublicError:
            invalid_count += 1
            continue
    if not checked_descriptors:
        if invalid_count:
            app.logger.info(
                "Temporary cleanup skipped %d invalid descriptor(s); no valid descriptors remained.",
                invalid_count,
            )
        return
    if USE_VERCEL_BLOB:
        deleted_count = vercel_blob_delete(checked_descriptors)
        app.logger.info(
            "Temporary cleanup requested=%d valid=%d deleted=%d invalid=%d backend=vercel-blob",
            len(descriptors),
            len(checked_descriptors),
            deleted_count,
            invalid_count,
        )
        return
    deleted_count = 0
    missing_count = 0
    for _descriptor, path in checked_descriptors:
        full_path = local_temp_file_path(path)
        if os.path.exists(full_path):
            os.remove(full_path)
            deleted_count += 1
        else:
            missing_count += 1
    app.logger.info(
        "Temporary cleanup requested=%d valid=%d deleted=%d missing=%d invalid=%d backend=local",
        len(descriptors),
        len(checked_descriptors),
        deleted_count,
        missing_count,
        invalid_count,
    )


def delete_temp_files_safely(descriptors, session_id=None, allow_uploads=False, context="Temporary cleanup"):
    try:
        delete_temp_files(descriptors, session_id, allow_uploads=allow_uploads)
    except Exception:
        app.logger.exception("%s failed", context)


# ─── Temporary file TTL sweep ─────────────────────────────────────────────────
# Reclaims blot images from sessions whose browser tab was closed or navigated
# away (client cleanup can no longer run for them). Scoped to the "sessions/"
# prefix — uploaded ZIPs are already deleted after processing — and never touches
# the caller's own in-flight session. All entry points are best-effort: a failure
# is logged and swallowed so it can never break an upload.

def parse_iso_epoch(value):
    """Parse an ISO-8601 timestamp (e.g. Vercel Blob's `uploadedAt`) to epoch
    seconds, tolerating a trailing 'Z'. Returns None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def session_id_from_temp_path(path):
    """Return the session-id segment of a 'sessions/<id>/...' path, else None."""
    if not isinstance(path, str):
        return None
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != "sessions" or not parts[1]:
        return None
    return parts[1]


def expired_session_blobs(blobs, cutoff_epoch, exclude_session_id=None):
    """Pure selector: from a Vercel Blob list page, return the {pathname, url}
    entries under sessions/ that were uploaded at or before `cutoff_epoch` and do
    not belong to the excluded session. Side-effect free for unit testing."""
    excluded = safe_id(exclude_session_id) if exclude_session_id else None
    expired = []
    for blob in blobs or []:
        if not isinstance(blob, dict):
            continue
        pathname = blob.get("pathname") or blob.get("path") or ""
        url = blob.get("url")
        session_id = session_id_from_temp_path(pathname)
        if not session_id or not isinstance(url, str):
            continue
        if excluded and session_id == excluded:
            continue
        uploaded = parse_iso_epoch(blob.get("uploadedAt"))
        if uploaded is None or uploaded > cutoff_epoch:
            continue
        expired.append({"pathname": pathname, "url": url})
    return expired


def newest_descendant_mtime(path):
    """Newest mtime across a directory tree, so a session still being written is
    not swept mid-upload. Returns None if the path is unreadable."""
    try:
        newest = os.path.getmtime(path)
    except OSError:
        return None
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                continue
    return newest


def sweep_expired_local_files(cutoff_epoch, exclude_session_id, max_deletes):
    base = os.path.join(os.path.abspath(LOCAL_TEMP_DIR), "sessions")
    if not os.path.isdir(base):
        return 0
    excluded = safe_id(exclude_session_id) if exclude_session_id else None
    removed = 0
    with os.scandir(base) as entries:
        for entry in entries:
            if removed >= max_deletes:
                break
            if not entry.is_dir() or not SAFE_PATH_PART_PATTERN.fullmatch(entry.name):
                continue
            if excluded and entry.name == excluded:
                continue
            newest = newest_descendant_mtime(entry.path)
            if newest is None or newest > cutoff_epoch:
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
    return removed


def vercel_blob_list(prefix=None, cursor=None, limit=1000):
    params = {"limit": str(limit)}
    if prefix:
        params["prefix"] = prefix
    if cursor:
        params["cursor"] = cursor
    url = f"{BLOB_API_BASE_URL}?{urlencode(params)}"
    result = vercel_blob_request(
        "GET", url, api_request=True, retries=2, max_response_bytes=MAX_BLOB_LIST_BYTES
    )
    return result if isinstance(result, dict) else {}


def sweep_expired_vercel_blobs(cutoff_epoch, exclude_session_id, max_deletes):
    to_delete = []  # (descriptor, validated_path)
    cursor = None
    for _page in range(TEMP_SWEEP_MAX_PAGES):
        page = vercel_blob_list(prefix="sessions/", cursor=cursor) or {}
        for blob in expired_session_blobs(page.get("blobs", []), cutoff_epoch, exclude_session_id):
            try:
                path = validate_temp_path(blob["pathname"])
            except PublicError:
                continue
            to_delete.append(({"url": blob["url"], "pathname": path}, path))
            if len(to_delete) >= max_deletes:
                break
        if len(to_delete) >= max_deletes or not page.get("hasMore"):
            break
        cursor = page.get("cursor")
        if not cursor:
            break
    if not to_delete:
        return 0
    return vercel_blob_delete(to_delete)


_last_temp_sweep_monotonic = None


def sweep_expired_temp_files(
    exclude_session_id=None,
    max_age_seconds=None,
    max_deletes=None,
    force=False,
):
    """Best-effort reclamation of orphaned session temp files. Never raises."""
    if max_age_seconds is None:
        max_age_seconds = TEMP_FILE_TTL_SECONDS
    if max_age_seconds <= 0:
        return 0
    # Local upload-triggered sweeps are throttled per process. The authenticated
    # scheduled sweep passes force=True so it runs exactly when invoked.
    global _last_temp_sweep_monotonic
    now = time.monotonic()
    if (
        not force
        and TEMP_SWEEP_MIN_INTERVAL_SECONDS > 0
        and _last_temp_sweep_monotonic is not None
        and now - _last_temp_sweep_monotonic < TEMP_SWEEP_MIN_INTERVAL_SECONDS
    ):
        return 0
    _last_temp_sweep_monotonic = now
    if max_deletes is None:
        max_deletes = TEMP_SWEEP_MAX_DELETES
    cutoff_epoch = time.time() - max_age_seconds
    try:
        if USE_VERCEL_BLOB:
            removed = sweep_expired_vercel_blobs(cutoff_epoch, exclude_session_id, max_deletes)
            backend = "vercel-blob"
        else:
            removed = sweep_expired_local_files(cutoff_epoch, exclude_session_id, max_deletes)
            backend = "local"
        if removed:
            app.logger.info(
                "Temp TTL sweep removed=%d backend=%s ttlSeconds=%d",
                removed, backend, max_age_seconds,
            )
        return removed
    except Exception:
        app.logger.exception("Temp file TTL sweep failed")
        return 0


def collect_blot_file_descriptors(blots):
    descriptors = []
    for blot in blots or []:
        files = blot.get("files", {}) if isinstance(blot, dict) else {}
        for descriptor in files.values():
            if descriptor:
                descriptors.append(descriptor)
    return descriptors


def load_payload_blot(blot, file_kinds=ALL_BLOT_FILE_KINDS, session_id=None):
    if not isinstance(blot, dict):
        raise PublicError("Blot data is missing.")
    files = blot.get("files")
    if not isinstance(files, dict):
        raise PublicError("Blot temporary file references are missing.")
    loaded = {"id": blot.get("id"), "name": blot.get("name", "Untitled blot"), "files": files}
    for kind in file_kinds:
        descriptor = files.get(kind)
        bytes_field = BLOT_FILE_FIELDS[kind][1]
        loaded[bytes_field] = (
            read_temp_file(descriptor, session_id, max_bytes=BLOT_FILE_LIMITS[kind])
            if descriptor
            else None
        )
    return loaded


init_storage()

# ─── Health check ─────────────────────────────────────────────────────────────

@app.route("/health")
@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "sharedRateLimits": not RATE_LIMIT_STORAGE_URI.startswith("memory://"),
        "tempStorage": TEMP_STORAGE_BACKEND,
    })


@app.route("/client-config")
@app.route("/api/client-config")
def client_config():
    return jsonify({
        "blobAccess": BLOB_ACCESS,
        "maxDirectUploadBytes": min(MAX_REQUEST_BYTES, MAX_ZIP_BYTES),
        "maxZipUploadBytes": MAX_ZIP_BYTES,
    })


@app.route("/cron-cleanup", methods=["GET"])
@app.route("/api/cron-cleanup", methods=["GET"])
@limiter.exempt
def cron_cleanup():
    secret = os.environ.get("CRON_SECRET", "")
    supplied = request.headers.get("Authorization", "")
    if len(secret) < 16 or not hmac.compare_digest(supplied, f"Bearer {secret}"):
        return jsonify({"error": "Unauthorized"}), 401
    removed = sweep_expired_temp_files(max_deletes=TEMP_SWEEP_MAX_DELETES, force=True)
    return jsonify({"status": "ok", "removed": removed})

# ─── ZIP upload & parsing ─────────────────────────────────────────────────────

@app.route("/upload-zip", methods=["POST"])
@app.route("/api/upload-zip", methods=["POST"])
@limiter.limit("10 per minute")
def upload_zip():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".zip"):
        return jsonify({"error": "File must be a .zip"}), 400

    # Reject oversized multipart requests before inspecting the seekable upload.
    if request.content_length and request.content_length > MAX_ZIP_BYTES:
        return jsonify({"error": "File is too large for direct API upload. Use Storage upload instead."}), 413

    file.stream.seek(0, os.SEEK_END)
    file_size = file.stream.tell()
    file.stream.seek(0)
    if file_size > MAX_ZIP_BYTES:
        return jsonify({"error": "File is too large for direct API upload. Use Storage upload instead."}), 413

    if not is_valid_zip(file.stream):
        return jsonify({"error": "Invalid ZIP file."}), 400

    try:
        session_id = request_session_id()
        blots = parse_zip(file.stream, session_id)
        if TEMP_SWEEP_ON_UPLOAD:
            sweep_expired_temp_files(exclude_session_id=session_id)
        return jsonify({"blots": blots})
    except PublicError as error:
        return error_response(error, "ZIP import failed.")
    except Exception as error:
        app.logger.exception("ZIP upload failed")
        return error_response(error, "ZIP import failed.")


@app.route("/process-upload", methods=["POST"])
@app.route("/api/process-upload", methods=["POST"])
@limiter.limit("10 per minute")
def process_storage_upload():
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    upload_descriptor = data.get("upload") or data.get("blob") or {}
    started = time.monotonic()
    upload_path = upload_descriptor.get("pathname") or upload_descriptor.get("path")
    print(
        json.dumps({
            "event": "process_upload_start",
            "sessionHash": log_token(session_id),
            "path": log_temp_path(upload_path),
        }),
        flush=True,
    )
    try:
        with tempfile.NamedTemporaryFile(prefix="western-blot-upload-", suffix=".zip") as upload_file:
            copied_bytes = copy_temp_file_to_handle(
                upload_descriptor,
                upload_file,
                session_id,
                allow_uploads=True,
                max_bytes=MAX_ZIP_BYTES,
            )
            upload_file.seek(0)
            print(
                json.dumps({
                    "event": "process_upload_blob_read",
                    "sessionHash": log_token(session_id),
                    "bytes": copied_bytes,
                    "elapsedSeconds": round(time.monotonic() - started, 3),
                }),
                flush=True,
            )
            if not is_valid_zip(upload_file):
                raise PublicError("Stored object is not a valid ZIP file.")
            blots = parse_zip(upload_file, session_id)
        if TEMP_SWEEP_ON_UPLOAD:
            sweep_expired_temp_files(exclude_session_id=session_id)
        print(
            json.dumps({
                "event": "process_upload_zip_parsed",
                "sessionHash": log_token(session_id),
                "blotCount": len(blots),
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }),
            flush=True,
        )
        print(
            json.dumps({
                "event": "process_upload_done",
                "sessionHash": log_token(session_id),
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }),
            flush=True,
        )
        return jsonify({"blots": blots})
    except PublicError as error:
        print(
            json.dumps({
                "event": "process_upload_public_error",
                "sessionHash": log_token(session_id),
                "error": str(error),
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }),
            flush=True,
        )
        return error_response(error, "ZIP import failed.")
    except Exception as error:
        print(
            json.dumps({
                "event": "process_upload_unexpected_error",
                "sessionHash": log_token(session_id),
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }),
            flush=True,
        )
        app.logger.exception("Stored ZIP processing failed")
        return error_response(error, "ZIP import failed.")
    finally:
        if upload_descriptor:
            delete_temp_files_safely(
                [upload_descriptor],
                session_id,
                allow_uploads=True,
                context="Stored ZIP upload cleanup",
            )


def is_valid_zip(zip_source):
    if isinstance(zip_source, (bytes, bytearray)):
        return len(zip_source) >= 4 and zipfile.is_zipfile(io.BytesIO(zip_source))
    try:
        original_position = zip_source.tell()
        zip_source.seek(0)
        valid = zipfile.is_zipfile(zip_source)
        zip_source.seek(original_position)
        return valid
    except (AttributeError, OSError):
        return False


def enforce_zip_member_size(zf, filename, limit, message):
    """Reject an optional archive member before reading oversized data into memory."""
    if filename and zf.getinfo(filename).file_size > limit:
        raise PublicError(message, 413)


def find_channel_tif(files, channel):
    pattern = re.compile(rf"(^|[^0-9]){re.escape(channel)}([^0-9]|$)")
    matches = [
        filename
        for filename in files
        if os.path.basename(filename).lower().endswith((".tif", ".tiff"))
        and pattern.search(os.path.basename(filename))
    ]
    if len(matches) > 1:
        raise PublicError(f"Multiple {channel}nm TIF files were found in one blot folder.")
    return matches[0] if matches else None


def parse_blot_metadata(txt_content, folder):
    lines = txt_content.splitlines()
    remarks = next(
        (
            line.split("=", 1)[1].strip()
            for line in lines
            if line.strip().startswith("Remarks=")
        ),
        "",
    )
    blot_name = (remarks or folder)[:MAX_NAME_LENGTH] or "Untitled blot"
    created_at = next(
        (parsed for parsed in (parse_blot_created_at(line) for line in lines) if parsed),
        None,
    ) or now_iso()
    return blot_name, created_at


def zip_folders(zf):
    folders = {}
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        parts = name.split("/")
        folder = parts[0] if len(parts) > 1 else "__root__"
        folders.setdefault(folder, []).append(name)
    return {folder: sorted(files) for folder, files in folders.items()}


def blot_folder_members(files):
    txt_file = next((f for f in files if f.lower().endswith(".txt")), None)
    if not txt_file:
        return None

    members = {
        "txt": txt_file,
        "jpg": next((f for f in files if f.lower().endswith(".jpg") or f.lower().endswith(".jpeg")), None),
        "700": find_channel_tif(files, "700"),
        "800": find_channel_tif(files, "800"),
    }
    if members["700"] and members["800"] and members["700"] == members["800"]:
        raise PublicError("A single TIF file cannot be used for both 700nm and 800nm channels.")
    return members


def log_blot_folder_parse(session_id, folder_index, members):
    print(
        json.dumps({
            "event": "process_upload_parse_folder",
            "sessionHash": log_token(session_id),
            "folderIndex": folder_index,
            "has700": members["700"] is not None,
            "has800": members["800"] is not None,
            "hasJpg": members["jpg"] is not None,
        }),
        flush=True,
    )


def enforce_blot_member_sizes(zf, members):
    enforce_zip_member_size(zf, members["700"], MAX_TIF_BYTES, "A 700nm TIF exceeds the configured size limit.")
    enforce_zip_member_size(zf, members["800"], MAX_TIF_BYTES, "An 800nm TIF exceeds the configured size limit.")
    enforce_zip_member_size(zf, members["txt"], MAX_TEXT_BYTES, "Blot metadata text is too large.")
    enforce_zip_member_size(zf, members["jpg"], MAX_JPEG_BYTES, "Blot preview image is too large.")


def store_blot_archive_members(zf, members, session_id, blot_id):
    """Validate and store one archive member at a time so a large preview and
    both TIF channels are never retained in memory together."""
    files = {}
    try:
        member_settings = (
            ("jpg", "preview.jpg", JPEG_MIMETYPE, None),
            ("700", "700.tif", TIFF_MIMETYPE, "700nm TIF"),
            ("800", "800.tif", TIFF_MIMETYPE, "800nm TIF"),
        )
        for kind, filename, content_type, tif_label in member_settings:
            archive_name = members[kind]
            if not archive_name:
                continue
            member_bytes = zf.read(archive_name)
            if tif_label:
                validate_tif_pixels(member_bytes, tif_label)
            descriptor = store_temp_file(
                session_id,
                blot_id,
                filename,
                member_bytes,
                content_type,
            )
            if descriptor:
                files[kind] = descriptor
            del member_bytes
        return files
    except Exception:
        delete_temp_files_safely(
            list(files.values()),
            session_id,
            context="Partial blot file rollback cleanup",
        )
        raise


def blot_record(blot_id, blot_name, created_at, members, files):
    return {
        "id": blot_id,
        "name": blot_name,
        "hasJpg": members["jpg"] is not None,
        "has700": members["700"] is not None,
        "has800": members["800"] is not None,
        "scanCount": 0,
        "createdAt": created_at,
        "files": files,
    }


def parse_zip_folder(zf, folder, folder_index, files, session_id):
    members = blot_folder_members(files)
    if not members:
        return None

    log_blot_folder_parse(session_id, folder_index, members)
    enforce_blot_member_sizes(zf, members)

    txt_content = zf.read(members["txt"]).decode("utf-8", errors="ignore")
    blot_name, created_at = parse_blot_metadata(txt_content, folder)
    blot_id = f"{uuid.uuid4().hex}_{folder}".replace(" ", "_")
    stored_files = store_blot_archive_members(zf, members, session_id, blot_id)
    return blot_record(blot_id, blot_name, created_at, members, stored_files)


def parse_zip(zip_source, session_id):
    blots = []
    try:
        source = io.BytesIO(zip_source) if isinstance(zip_source, (bytes, bytearray)) else zip_source
        source.seek(0)
        with zipfile.ZipFile(source) as zf:
            validate_zip_archive(zf)
            folders = zip_folders(zf)
            for folder_index, folder in enumerate(sorted(folders), start=1):
                blot = parse_zip_folder(zf, folder, folder_index, folders[folder], session_id)
                if blot:
                    blots.append(blot)

        return blots
    except Exception:
        delete_temp_files_safely(
            collect_blot_file_descriptors(blots),
            session_id,
            context="Parsed blot rollback cleanup",
        )
        raise


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


def decode_validated_tif(tif_bytes, label, return_saturation=False):
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
            sanitized = sanitize_tif_pixels(image, label)
            if return_saturation:
                # Hand back the pre-sanitize pixels rather than a whole-image
                # saturation mask: the extract path only measures small ROIs, so
                # saturation is counted per-ROI in extract_box_signal instead of
                # allocating a boolean mask the size of the full image.
                return sanitized, image
            return sanitized
    except PublicError:
        raise
    except Exception as error:
        raise PublicError(f"{label} could not be read.") from error


def read_tif_dimensions(tif_bytes, label):
    """(height, width) of the primary full-resolution page, WITHOUT decoding the
    pixels. Mirrors decode_validated_tif's page selection so the shape matches what
    read_raw_channel would decode. Used to size the composite grid for coordinate
    scaling without paying to fully decode the second channel.

    This is intentionally lighter than decode_validated_tif (no dtype / pixel-cap /
    reduced-page checks) because it only reads header geometry. Full validation still
    runs when the channel is actually decoded, and boxes only exist after a composite
    render decoded both channels — so a file that passes here but would fail a full
    decode never produces a wrong measurement (composite_dimensions also falls back
    to the native grid on any read failure)."""
    try:
        with tifffile.TiffFile(io.BytesIO(tif_bytes)) as tif:
            pages = list(tif.pages)
            if not pages or len(pages) > MAX_TIF_PAGES:
                raise PublicError(f"{label} has an unsupported number of image pages.")
            full_resolution_pages = [page for page in pages if not page.is_reduced]
            if len(full_resolution_pages) != 1:
                raise PublicError(f"{label} must contain one full-resolution image.")
            shape = full_resolution_pages[0].shape
            if len(shape) != 2:
                raise PublicError(f"{label} must be a two-dimensional grayscale image.")
            if int(np.prod(shape)) > MAX_IMAGE_PIXELS:
                raise PublicError(f"{label} is too large. Maximum is {MAX_IMAGE_PIXELS:,} pixels.")
            return int(shape[0]), int(shape[1])
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


def tif_saturation_mask(image):
    """Boolean mask of detector-saturated pixels, computed from the ORIGINAL
    (pre-sanitize) pixels. LI-COR float scans encode saturation as +Inf; integer
    scans saturate at the dtype ceiling (65535 for uint16). Saturated bands cannot
    be accurately quantified — the summed signal underestimates the true amount."""
    arr = np.asarray(image)
    if arr.dtype == np.dtype("float16"):
        return np.isposinf(arr.astype(np.float32))
    if np.issubdtype(arr.dtype, np.integer):
        return arr >= np.iinfo(arr.dtype).max
    return np.isposinf(arr.astype(np.float32))


# ─── TIF composite rendering ──────────────────────────────────────────────────

@app.route("/render-composite", methods=["POST"])
@app.route("/api/render-composite", methods=["POST"])
@limiter.limit("60 per minute")
def render_composite():
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    try:
        blot = load_payload_blot(data.get("blot"), ("700", "800"), session_id)
        if not blot["tif_700_bytes"] or not blot["tif_800_bytes"]:
            return jsonify({"error": "TIF files not found for this blot"}), 404

        brightness_700 = float(data.get("brightness700", 1.0))
        contrast_700 = float(data.get("contrast700", 1.0))
        gamma_700 = float(data.get("gamma700", 1.0))
        brightness_800 = float(data.get("brightness800", 1.0))
        contrast_800 = float(data.get("contrast800", 1.0))
        gamma_800 = float(data.get("gamma800", 1.0))
        color_mode = data.get("colorMode", "color")

        brightness_700 = max(0.1, min(5.0, brightness_700))
        contrast_700 = max(0.1, min(10.0, contrast_700))
        gamma_700 = max(0.1, min(5.0, gamma_700))
        brightness_800 = max(0.1, min(5.0, brightness_800))
        contrast_800 = max(0.1, min(10.0, contrast_800))
        gamma_800 = max(0.1, min(5.0, gamma_800))

        img = build_composite(
            blot["tif_700_bytes"],
            blot["tif_800_bytes"],
            brightness_700, contrast_700,
            brightness_800, contrast_800,
            color_mode,
            gamma_700, gamma_800,
        )

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        return send_file(buf, mimetype=JPEG_MIMETYPE)
    except PublicError as error:
        return error_response(error, "Could not render blot image.")
    except Exception as error:
        app.logger.exception("Composite render failed")
        return error_response(error, "Could not render blot image.")


def read_tif_channel(tif_bytes):
    # Display normalization starts from the same validated pixels used for quantification.
    arr = read_raw_channel(tif_bytes)
    if arr.size > MAX_RENDER_PIXELS:
        raise PublicError(
            "Blot image is too large to render a composite preview. "
            f"Maximum is {MAX_RENDER_PIXELS:,} pixels.",
            413,
        )
    low  = np.percentile(arr, 1)
    high = np.percentile(arr, 99)
    if high > low:
        arr = (arr - low) / (high - low)
    else:
        arr = np.zeros_like(arr)
    return np.clip(arr, 0.0, 1.0)


def apply_adjustments(channel, brightness, contrast, gamma=1.0):
    # Gamma acts on the normalized [0,1] intensities first: gamma < 1 lifts faint
    # bands without blowing out strong ones (matching Image Studio's display curve),
    # gamma > 1 darkens. Contrast (pivot at mid-gray) and additive brightness follow.
    adjusted = np.power(np.clip(channel, 0.0, 1.0), gamma)
    adjusted = (adjusted - 0.5) * contrast + 0.5
    adjusted = adjusted + (brightness - 1.0)
    return np.clip(adjusted, 0.0, 1.0)


def build_composite(tif_700_bytes, tif_800_bytes, brightness_700, contrast_700, brightness_800, contrast_800, color_mode="color", gamma_700=1.0, gamma_800=1.0):
    ch_700 = read_tif_channel(tif_700_bytes)
    ch_800 = read_tif_channel(tif_800_bytes)

    if ch_700.shape != ch_800.shape:
        h = max(ch_700.shape[0], ch_800.shape[0])
        w = max(ch_700.shape[1], ch_800.shape[1])
        # Each channel individually passes read_tif_channel's MAX_RENDER_PIXELS
        # cap, but that only bounds each array's total pixels. Two channels with
        # orthogonal aspect ratios (e.g. 30000x1334 and 1334x30000) both fit under
        # the cap while their union (max height x max width) explodes into a
        # multi-gigapixel resize target — a sub-1MB upload can force multi-GB
        # allocation. Bound the union before allocating so the documented memory
        # ceiling actually holds.
        if w * h > MAX_RENDER_PIXELS:
            raise PublicError(
                "Blot channels differ in size and are too large to composite "
                f"at a common resolution. Maximum is {MAX_RENDER_PIXELS:,} pixels.",
                413,
            )
        ch_700 = np.array(Image.fromarray((ch_700 * 65535).astype(np.uint16)).resize((w, h), Image.LANCZOS)).astype(np.float32) / 65535
        ch_800 = np.array(Image.fromarray((ch_800 * 65535).astype(np.uint16)).resize((w, h), Image.LANCZOS)).astype(np.float32) / 65535

    ch_700 = apply_adjustments(ch_700, brightness_700, contrast_700, gamma_700)
    ch_800 = apply_adjustments(ch_800, brightness_800, contrast_800, gamma_800)

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

@app.route("/extract", methods=["POST"])
@app.route("/api/extract", methods=["POST"])
@limiter.limit("30 per minute")
def extract_payload_signals():
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    try:
        blot = load_payload_blot(data.get("blot"), (), session_id)
        boxes = data.get("boxes", [])
        channel = data.get("channel", "700")
        # backgroundSides supersedes the legacy backgroundAxis key; keep the old
        # one working for clients that have not been updated yet.
        background_sides = data.get("backgroundSides") or data.get("backgroundAxis", "leftright")
        background_stat = data.get("backgroundStat", "median")
        border_width = data.get("borderWidth", 3)

        if channel not in ("700", "800"):
            return jsonify({"error": "Invalid channel"}), 400
        if background_sides not in ("leftright", "topbottom", "allsides"):
            return jsonify({"error": "Invalid background mode"}), 400
        if background_stat not in ("median", "mean"):
            return jsonify({"error": "Invalid background statistic"}), 400
        try:
            border_width = max(1, min(5, int(border_width)))
        except (TypeError, ValueError):
            border_width = 3
        if not isinstance(boxes, list):
            return jsonify({"error": "Boxes must be a list."}), 400
        if len(boxes) > 200:
            return jsonify({"error": "Too many boxes. Maximum is 200."}), 400

        for box in boxes:
            validate_box(box)
        descriptor = blot["files"].get(channel)
        if not descriptor:
            return jsonify({"error": f"No {channel}nm TIF found"}), 404
        tif_bytes = read_temp_file(descriptor, session_id, max_bytes=BLOT_FILE_LIMITS[channel])
        arr, raw_pixels = read_raw_channel(tif_bytes, return_saturation=True)

        # Boxes arrive in COMPOSITE image space (the frontend draws on the
        # max(700,800) composite from build_composite). When the two channels have
        # different native dimensions this channel is smaller than the composite, so
        # rescale each box into this channel's native pixel grid before measuring —
        # otherwise boxes toward the far edge would read the wrong, edge-clamped
        # pixels. A no-op (scale 1.0) whenever the composite equals this channel.
        native_h, native_w = arr.shape
        composite_h, composite_w = resolve_composite_dimensions(
            data, blot, channel, session_id, native_h, native_w
        )
        scale_x = native_w / composite_w if composite_w else 1.0
        scale_y = native_h / composite_h if composite_h else 1.0
        results = [
            extract_box_signal(
                arr, scale_box(box, scale_x, scale_y),
                background_sides, border_width, background_stat, raw_arr=raw_pixels,
            )
            for box in boxes
        ]
        return jsonify({"results": results})
    except PublicError as error:
        return error_response(error, "Signal extraction failed.")
    except Exception as error:
        app.logger.exception("Signal extraction failed")
        return error_response(error, "Signal extraction failed.")


def composite_dimensions(blot, channel, session_id, native_h, native_w):
    """Dimensions of the composite the frontend drew its boxes on = the element-wise
    max of both channels' native shapes (exactly what build_composite resizes to).
    Falls back to this channel's own native dimensions when the other channel is
    absent or unreadable — which reproduces the pre-scaling behaviour and means a
    single-channel blot never scales."""
    other = "800" if channel == "700" else "700"
    descriptor = blot["files"].get(other)
    if not descriptor:
        return native_h, native_w
    try:
        other_bytes = read_temp_file(descriptor, session_id, max_bytes=BLOT_FILE_LIMITS[other])
        other_h, other_w = read_tif_dimensions(other_bytes, f"{other}nm TIF")
    except Exception:
        app.logger.info(
            "Could not read %snm dimensions for coordinate scaling; using native grid.", other
        )
        return native_h, native_w
    return max(native_h, other_h), max(native_w, other_w)


def valid_composite_dimension(value, minimum):
    """Coerce a client-supplied composite dimension to an int, rejecting anything
    below this channel's native size. The composite the frontend drew its boxes on is
    the element-wise max of both channels, so a legitimate value is never smaller than
    the native grid; returns None for a missing or implausible value."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < minimum:
        return None
    return number


def resolve_composite_dimensions(data, blot, channel, session_id, native_h, native_w):
    """Dimensions the frontend's boxes were drawn on. Prefer the natural size the
    client reports for the rendered composite (compositeWidth / compositeHeight): the
    composite is max(700, 800) per axis, so a valid report is at least this channel's
    native size and within the decode cap. Using it avoids re-downloading and header-
    parsing the OTHER channel on every /extract — which, when both channels share
    dimensions (the common case), only ever yields a no-op 1.0 scale anyway. Falls back
    to composite_dimensions (which reads the other channel) when the client sends
    nothing usable, e.g. an older client or before the composite image has loaded."""
    width = valid_composite_dimension(data.get("compositeWidth"), native_w)
    height = valid_composite_dimension(data.get("compositeHeight"), native_h)
    if width and height and width * height <= MAX_IMAGE_PIXELS:
        return height, width
    return composite_dimensions(blot, channel, session_id, native_h, native_w)


def scale_box(box, scale_x, scale_y):
    """Map a box from composite space into a channel's native pixel grid. Returns the
    box unchanged when there is no scaling, so equal-dimension blots are untouched."""
    if scale_x == 1.0 and scale_y == 1.0:
        return box
    return {
        "x": box["x"] * scale_x,
        "y": box["y"] * scale_y,
        "w": box["w"] * scale_x,
        "h": box["h"] * scale_y,
    }


def read_raw_channel(tif_bytes, return_saturation=False):
    result = decode_validated_tif(tif_bytes, "Stored TIF", return_saturation=return_saturation)
    if return_saturation:
        # Extract path: keep the channel in its native dtype (uint16/float16) instead
        # of allocating a float32 copy of the whole image — only small ROIs are read,
        # and their sums upcast to float64 locally in extract_box_signal. The second
        # array is the pre-sanitize pixels used for per-ROI saturation counts.
        sanitized, raw_pixels = result
        return sanitized, raw_pixels
    # Render path normalizes the full array, so float32 keeps the intermediates small.
    return result.astype(np.float32)


def read_validated_tif(tif_bytes):
    return decode_validated_tif(tif_bytes, "Stored TIF")


def extract_box_signal(arr, box, background_sides="leftright", border_width=3, background_stat="median", raw_arr=None):
    raw_x = int(round(box["x"]))
    raw_y = int(round(box["y"]))
    w = max(1, int(round(box["w"])))
    h = max(1, int(round(box["h"])))

    img_h, img_w = arr.shape
    x = max(0, raw_x)
    y = max(0, raw_y)
    x2 = min(raw_x + w, img_w)
    y2 = min(raw_y + h, img_h)
    if x2 <= x or y2 <= y:
        raise PublicError("A selected box is outside the image bounds.")

    roi        = arr[y:y2, x:x2]
    # Accumulate in float64 on the slice: the channel is kept in its native
    # dtype (uint16/float16), so a float64 accumulator avoids the precision loss
    # a float32 sum would incur over a large ROI.
    raw_signal = float(np.sum(roi, dtype=np.float64))
    n_pixels   = (y2 - y) * (x2 - x)

    # Local-background strips flanking the box. Mirrors LI-COR Image Studio's
    # Median method: Signal = Total - (Bkgnd x Area), where Bkgnd is the median
    # (or mean) per-pixel intensity of a 1-5 px border. "allsides" averages a
    # gradient better; left-right / top-bottom sample only one axis.
    border    = max(1, min(5, int(border_width)))
    strips    = []

    if background_sides in ("leftright", "allsides"):
        lx1, lx2 = max(0, x - border), x
        if lx2 > lx1:
            strips.append(arr[y:y2, lx1:lx2].flatten())
        rx1, rx2 = x2, min(img_w, x2 + border)
        if rx2 > rx1:
            strips.append(arr[y:y2, rx1:rx2].flatten())
    if background_sides in ("topbottom", "allsides"):
        ty1, ty2 = max(0, y - border), y
        if ty2 > ty1:
            strips.append(arr[ty1:ty2, x:x2].flatten())
        by1, by2 = y2, min(img_h, y2 + border)
        if by2 > by1:
            strips.append(arr[by1:by2, x:x2].flatten())

    background_per_pixel = 0.0
    background_uneven = False
    if strips:
        # Compute background stats in float64. The channel is kept in its native
        # dtype now, and np.median / np.mean of a float16 array would otherwise round
        # the result back to the float16 grid; widening first keeps the estimate at
        # least as accurate as the previous float32 path.
        all_bg = np.concatenate(strips).astype(np.float64, copy=False)
        background_per_pixel = (
            float(np.mean(all_bg)) if background_stat == "mean" else float(np.median(all_bg))
        )
        # If opposite strips differ by >2x, one likely overlaps a neighbouring
        # band — the background estimate (and the subtraction) is unreliable.
        side_medians = [float(np.median(s.astype(np.float64, copy=False))) for s in strips if s.size]
        if len(side_medians) >= 2 and min(side_medians) > 0 and max(side_medians) / min(side_medians) > 2.0:
            background_uneven = True

    background_signal = background_per_pixel * n_pixels
    adjusted_signal = max(0.0, raw_signal - background_signal)

    # Count detector-saturated pixels on the ROI slice of the pre-sanitize pixels,
    # reusing tif_saturation_mask's per-dtype logic — no whole-image mask needed.
    saturated_pixels = (
        int(np.count_nonzero(tif_saturation_mask(raw_arr[y:y2, x:x2])))
        if raw_arr is not None else 0
    )

    return {
        "rawSignal": round(raw_signal, 2),
        "backgroundSignal": round(background_signal, 2),
        "adjustedSignal": round(adjusted_signal, 2),
        "backgroundPerPixel": round(background_per_pixel, 4),
        "backgroundUneven": background_uneven,
        "saturatedPixels": saturated_pixels,
        "saturatedFraction": round(saturated_pixels / n_pixels, 6) if n_pixels else 0.0,
        "maxPixel": round(float(roi.max()), 2),
        "x": x, "y": y, "w": x2 - x, "h": y2 - y,
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


# ─── Automatic band detection (/detect-bands) ───────────────────────────────────
#
# Detection runs HERE, on the backend, on the native validated pixels from
# read_raw_channel() — the same array extract_box_signal() quantifies from — not the
# lossy 8-bit tone-mapped JPEG composite the browser holds. It emits boxes in COMPOSITE
# coordinate space, so a detected box is indistinguishable from a hand-drawn one and
# flows through the untouched extraction / normalization / fold-change pipeline
# (/extract still rescales composite -> native at measure time via scale_box).
#
# Projection-profile method, numpy only (no scipy — peak finding is hand-rolled).

# Cap detected boxes per candidate to match the frontend's MAX_CANVAS_BOXES (app.js:17).
MAX_DETECT_BOXES = 200

# Detect on a copy downsampled to this max long-axis size: projection profiles don't
# need full resolution, and this bounds cost/memory independent of image size (a native
# TIF may be up to MAX_IMAGE_PIXELS). Boxes scale back to composite space at the end, so
# downsampling never affects the emitted coordinates.
DETECT_MAX_DIM = 1600

# Longer axis of the returned laneProfile (for optional live client-side re-thresholding).
PROFILE_SAMPLES = 512

# Deskew search: try angles in [-MAX_SKEW_DEG, +MAX_SKEW_DEG] at this step and keep the
# one that makes the lane (column) projection sharpest. 0 disables auto-deskew.
MAX_SKEW_DEG = 5.0
SKEW_STEP_DEG = 0.5

# Rotation is GATED OFF in v1. The box model is axis-aligned {x, y, w, h}, so a box found
# in a rotated frame is a *tilted* rectangle in the original composite — unrepresentable.
# Rotation therefore only works as ONE angle applied identically to detection + canvas
# DISPLAY + /extract (which must rotate the native array before slicing). /extract does
# not do that yet, so applying any angle here would emit boxes that /extract measures
# against the UNROTATED pixels — silent wrong signals. Detect in the unrotated composite
# frame (boxes flow through /extract unchanged) until rotation is plumbed end to end; then
# flip this flag. See DETECT_BANDS_DESIGN.md ("Rotation / deskew — the one real catch").
DETECT_ENABLE_DESKEW = False

# The "sets of bands to choose between" = one detection per sensitivity level. Lower
# `prominence` => more (fainter) bands captured; that is the knob that varies across
# levels. `lane_threshold` is deliberately HELD CONSTANT: lowering it with sensitivity
# merges adjacent lanes into one (verified visually during calibration), which is never
# wanted. Values are in normalized (0..1) profile units — tune against real blots.
SENSITIVITY_LEVELS = {
    "conservative": {"prominence": 0.20, "lane_threshold": 0.15, "min_distance_frac": 0.030},
    "balanced":     {"prominence": 0.10, "lane_threshold": 0.15, "min_distance_frac": 0.020},
    "aggressive":   {"prominence": 0.05, "lane_threshold": 0.15, "min_distance_frac": 0.012},
}
DEFAULT_LEVELS = ["conservative", "balanced", "aggressive"]


def _working_image(arr):
    """Stride-downsample so the longer axis is <= DETECT_MAX_DIM. Striding is cheap and
    fine for projection profiles. Returns (work, step)."""
    h, w = arr.shape
    step = max(1, int(np.ceil(max(h, w) / DETECT_MAX_DIM)))
    return arr[::step, ::step], step


def _normalize_for_detect(arr):
    """Robust 0..1 normalization using percentiles, so a few saturated pixels or a dark
    floor don't dominate. Computed locally for detection only."""
    a = arr.astype(np.float64, copy=False)
    # One percentile call partitions once for both cut points (vs two full partitions).
    lo, hi = (float(v) for v in np.percentile(a, [5.0, 99.5]))
    if hi <= lo:
        return np.zeros_like(a)
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def _orient_polarity(norm):
    """LI-COR Odyssey scans are signal-bright-on-dark, so high intensity = band. If the
    frame is mostly bright (median > 0.5) it's a dark-band-on-light capture — invert so
    bands are always the high values downstream."""
    if float(np.median(norm)) > 0.5:
        return 1.0 - norm
    return norm


def _smooth_profile(profile, window):
    """Moving-average smoothing via convolution (mode='same' keeps length)."""
    if window <= 1:
        return profile
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(profile, kernel, mode="same")


def _bool_runs(mask):
    """Contiguous True runs in a 1-D boolean array -> list of (start, end) half-open."""
    idx = np.flatnonzero(np.diff(np.concatenate(([0], mask.view(np.int8), [0]))))
    return list(zip(idx[0::2], idx[1::2]))


def _downsample_profile(profile, target):
    n = len(profile)
    if n <= target:
        return profile
    return profile[np.linspace(0, n - 1, target).astype(int)]


# Rotation / deskew. The box model is AXIS-ALIGNED {x, y, w, h}, so a box found in a
# rotated frame is a *tilted* rectangle back in the original composite, which the model
# can't represent. Rotation therefore can't be a detection-only trick: the returned
# rotationDeg must be applied identically to the canvas DISPLAY and to /extract (which
# would rotate the native array before slicing) so the measured pixels match the view.
# The functions below cover only the detection side and the angle estimate.

def _rotate_array(arr, angle_deg):
    """Rotate a 2-D float array about its center by angle_deg (CCW), keeping the same
    shape (corners cropped). Bilinear resample. Returns arr unchanged at ~0 deg."""
    if abs(angle_deg) < 1e-3:
        return arr
    img = Image.fromarray(arr.astype(np.float32, copy=False), mode="F")
    rotated = img.rotate(angle_deg, resample=Image.BILINEAR, expand=False, fillcolor=0.0)
    return np.asarray(rotated, dtype=np.float32)


def _lane_sharpness(norm):
    """How 'peaky' the lane (column) projection is. Sharp, well-separated lanes give a
    high-variance profile; a smeared/tilted blot flattens it. Deskew objective."""
    prof = _smooth_profile(norm.mean(axis=0), max(3, int(norm.shape[1] * 0.01)))
    return float(np.var(prof))


def _estimate_skew(norm, max_deg=MAX_SKEW_DEG, step=SKEW_STEP_DEG):
    """Pick the small rotation that maximizes lane sharpness (a coarse projection deskew).
    Prefers no rotation: angle 0 is the baseline and only a strictly sharper angle beats
    it, so a flat/degenerate frame returns 0.0 rather than the first angle tried. Returns
    degrees; 0.0 when deskew is disabled."""
    if max_deg <= 0:
        return 0.0
    best_angle, best_score = 0.0, _lane_sharpness(norm)
    for angle in np.arange(-max_deg, max_deg + step / 2, step):
        if abs(float(angle)) < 1e-9:   # 0.0 already scored as the baseline
            continue
        score = _lane_sharpness(_rotate_array(norm, float(angle)))
        if score > best_score:
            best_score, best_angle = score, float(angle)
    return best_angle


def _all_peaks(y):
    """All interior local maxima with their topographic prominence, unfiltered, sorted by
    index. Prominence proxy: walk outward from each peak until a higher sample, tracking
    the lowest valley on each side; prominence = peak - max(left_valley, right_valley).
    This walk depends only on `y`, so splitting it from the per-level prominence/spacing
    filter (_select_peaks) lets the three sensitivity sets share one pass over each lane."""
    n = len(y)
    if n < 3:
        return []
    # Candidate local maxima (>= on the right handles flat-topped plateaus).
    cand = np.flatnonzero((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:])) + 1
    scored = []
    for i in cand:
        peak = y[i]
        left_valley = peak
        j = i - 1
        while j >= 0 and y[j] < peak:
            left_valley = min(left_valley, y[j]); j -= 1
        right_valley = peak
        j = i + 1
        while j < n and y[j] < peak:
            right_valley = min(right_valley, y[j]); j += 1
        scored.append((int(i), float(peak - max(left_valley, right_valley))))
    return scored


def _select_peaks(peaks, min_prominence, min_distance):
    """From precomputed (index, prominence) peaks, keep those with prominence >=
    min_prominence, spaced at least min_distance apart (greedy by prominence when they
    crowd). Returns kept (index, prominence) pairs sorted by index. The per-peak
    prominence is carried through for _peak_extent's half-prominence descent."""
    scored = [pair for pair in peaks if pair[1] >= min_prominence]
    scored.sort(key=lambda t: t[1], reverse=True)
    kept = []
    for i, prom in scored:
        if all(abs(i - k) >= min_distance for k, _ in kept):
            kept.append((i, prom))
    kept.sort(key=lambda t: t[0])
    return kept


def _peak_extent(y, i, prominence):
    """Band y-extent: descend from the peak on both sides until the profile drops to
    half-prominence below the peak (a stable proxy for valley-to-valley). Returns a
    half-open [y0, y1)."""
    n = len(y)
    floor = y[i] - 0.5 * prominence
    a = i
    while a > 0 and y[a - 1] > floor:
        a -= 1
    b = i
    while b < n - 1 and y[b + 1] > floor:
        b += 1
    return a, b + 1


def _column_profile(norm):
    """Smoothed vertical projection (mean per column) = the lane axis. Shared by lane
    detection and the returned laneProfile, so it is computed once."""
    return _smooth_profile(norm.mean(axis=0), max(3, int(norm.shape[1] * 0.01)))


def _lanes_from_profile(column_profile, lane_threshold):
    """Lanes are contiguous spans of the (already smoothed) column projection above a
    fraction of its dynamic range. Returns list of (x0, x1) half-open."""
    w = len(column_profile)
    lo, hi = float(column_profile.min()), float(column_profile.max())
    if hi <= lo:
        return [(0, w)]
    above = column_profile >= (lo + lane_threshold * (hi - lo))
    min_width = max(2, int(w * 0.01))
    lanes = [(int(a), int(b)) for a, b in _bool_runs(above) if (b - a) >= min_width]
    return lanes or [(0, w)]


def _prepare_lanes(norm, lane_threshold):
    """Level-INDEPENDENT detection work, computed once and shared across the sensitivity
    sets: the column profile, the lane spans, and each lane's smoothed migration-axis
    profile with all its peak candidates. The sets differ only in the prominence/spacing
    filter applied later (_candidate_boxes), so doing this once instead of once per level
    avoids ~3x redundant column/lane means, convolutions, and prominence walks. Returns
    (column_profile, lanes, prepared) with prepared = [(x0, x1, profile, peaks), ...]."""
    column_profile = _column_profile(norm)
    lanes = _lanes_from_profile(column_profile, lane_threshold)
    window = max(3, int(norm.shape[0] * 0.01))
    prepared = []
    for (x0, x1) in lanes:
        profile = _smooth_profile(norm[:, x0:x1].mean(axis=1), window)
        prepared.append((x0, x1, profile, _all_peaks(profile)))
    return column_profile, lanes, prepared


def _candidate_boxes(prepared, level, scale_x, scale_y):
    """Assemble one sensitivity set's boxes (COMPOSITE coords) from the shared prepared
    lanes: apply this level's prominence/spacing filter, then a per-peak half-prominence
    extent. Returns (boxes, truncated)."""
    boxes = []
    for li, (x0, x1, profile, peaks) in enumerate(prepared):
        min_distance = max(2, int(len(profile) * level["min_distance_frac"]))
        for bi, (p, prom) in enumerate(_select_peaks(peaks, level["prominence"], min_distance)):
            # This peak's ACTUAL prominence sizes the extent, not the level threshold —
            # otherwise a tall band descends only a threshold-sized sliver and its box
            # (hence integrated ROI) is under-sized relative to a faint band.
            y0, y1 = _peak_extent(profile, p, prom)
            boxes.append({
                "x": round(x0 * scale_x, 2),
                "y": round(y0 * scale_y, 2),
                "w": round((x1 - x0) * scale_x, 2),
                "h": round((y1 - y0) * scale_y, 2),
                "lane": li,
                "band": bi,
                "score": round(float(profile[p]), 4),
            })
    truncated = False
    if len(boxes) > MAX_DETECT_BOXES:
        boxes.sort(key=lambda b: b["score"], reverse=True)   # keep strongest bands
        boxes = boxes[:MAX_DETECT_BOXES]
        truncated = True
    return boxes, truncated


@app.route("/detect-bands", methods=["POST"])
@app.route("/api/detect-bands", methods=["POST"])
@limiter.limit("15 per minute")
def detect_bands():
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    try:
        blot = load_payload_blot(data.get("blot"), (), session_id)
        channel = data.get("channel", "700")
        if channel not in ("700", "800"):
            return jsonify({"error": "Invalid channel"}), 400

        levels = data.get("sensitivities") or DEFAULT_LEVELS
        if not isinstance(levels, list):
            return jsonify({"error": "sensitivities must be a list."}), 400
        # Keep known levels, de-duplicated (dict preserves order), capped at 3.
        levels = list(dict.fromkeys(n for n in levels if n in SENSITIVITY_LEVELS))[:3] or ["balanced"]

        # Optional lane-threshold override from the client's live laneProfile slider (0..1).
        lane_threshold = data.get("laneThreshold")
        if isinstance(lane_threshold, (int, float)) and np.isfinite(lane_threshold):
            lane_threshold = float(min(1.0, max(0.0, lane_threshold)))
        else:
            lane_threshold = None

        descriptor = blot["files"].get(channel)
        if not descriptor:
            return jsonify({"error": f"No {channel}nm TIF found"}), 404
        tif_bytes = read_temp_file(descriptor, session_id, max_bytes=BLOT_FILE_LIMITS[channel])

        # Same validated-decode path as /extract, but keep the native dtype (uint16/
        # float16) instead of read_raw_channel's whole-image float32 copy: _working_image
        # strides this down to DETECT_MAX_DIM (a view) and only _normalize_for_detect
        # converts the small working copy to float — so peak memory tracks the downsampled
        # size, not the up-to-MAX_IMAGE_PIXELS native array.
        arr = read_validated_tif(tif_bytes)
        native_h, native_w = arr.shape

        # Detect on a downsampled copy; scale boxes from the WORKING grid straight to
        # composite space (composite = element-wise max of both channels' native sizes,
        # exactly what the frontend drew its boxes on).
        work, _step = _working_image(arr)
        work_h, work_w = work.shape
        composite_h, composite_w = resolve_composite_dimensions(
            data, blot, channel, session_id, native_h, native_w
        )
        scale_x = composite_w / work_w if work_w else 1.0
        scale_y = composite_h / work_h if work_h else 1.0

        norm = _orient_polarity(_normalize_for_detect(work))

        # Deskew is gated off in v1 (DETECT_ENABLE_DESKEW): detecting in a rotated frame
        # would emit boxes that /extract measures against the unrotated native array. Detect
        # in the unrotated composite frame so boxes flow through /extract unchanged, and
        # report rotationDeg = 0.0. The rotation code below stays ready for when the angle
        # is plumbed through display + /extract together (see the flag's comment).
        if DETECT_ENABLE_DESKEW:
            # Honor a client-supplied angle if given, else auto-estimate. The returned
            # angle must then be applied to the canvas display AND /extract to match.
            requested_angle = data.get("rotationDeg")
            if isinstance(requested_angle, (int, float)) and np.isfinite(requested_angle):
                angle = max(-45.0, min(45.0, float(requested_angle)))
            else:
                angle = _estimate_skew(norm)
            norm = _rotate_array(norm, angle)
        else:
            angle = 0.0

        # Lane spans, per-lane profiles, and peak candidates are level-independent
        # (lane_threshold is held constant across levels), so compute them ONCE and let
        # each sensitivity set differ only by its prominence/spacing filter.
        effective_lane_threshold = (
            lane_threshold if lane_threshold is not None
            else SENSITIVITY_LEVELS[levels[0]]["lane_threshold"]
        )
        column_profile, lanes, prepared = _prepare_lanes(norm, effective_lane_threshold)

        candidates = []
        for name in levels:
            boxes, truncated = _candidate_boxes(prepared, SENSITIVITY_LEVELS[name], scale_x, scale_y)
            candidates.append({
                "id": name,
                "label": name.capitalize(),
                "laneCount": len(lanes),
                "bandCount": len(boxes),
                "truncated": truncated,   # frontend surfaces this ("showing strongest 200")
                "boxes": boxes,
            })

        # Reuse the column projection already computed for lane detection so the client can
        # re-threshold lanes live without a round-trip (no extra full-image mean/convolve).
        return jsonify({
            "imageWidth": composite_w,       # coordinate space the boxes are in
            "imageHeight": composite_h,
            "channel": channel,
            "rotationDeg": round(angle, 2),  # apply to display + /extract to match
            "candidates": candidates,
            "laneProfile": _downsample_profile(column_profile, PROFILE_SAMPLES).round(4).tolist(),
        })
    except PublicError as error:
        return error_response(error, "Band detection failed.")
    except Exception as error:
        app.logger.exception("Band detection failed")
        return error_response(error, "Band detection failed.")


@app.route("/cleanup", methods=["POST"])
@app.route("/api/cleanup", methods=["POST"])
@limiter.limit("30 per minute")
def cleanup_temp_files():
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    descriptors = []
    descriptors.extend(collect_blot_file_descriptors(data.get("blots", [])))
    if isinstance(data.get("files"), list):
        descriptors.extend(data["files"])
    if isinstance(data.get("upload"), dict):
        descriptors.append(data["upload"])
    delete_temp_files(descriptors, session_id, allow_uploads=True)
    return jsonify({"status": "deleted"})


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port  = int(os.environ.get("PORT", 5000))
    app.run(debug=debug, port=port)
