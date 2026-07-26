import os
import io
import json
import re
import hashlib
import hmac
import random
import shutil
import struct
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
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
PNG_MIMETYPE = "image/png"
TIFF_MIMETYPE = "image/tiff"
# The composite is a measurement aid, so encode it losslessly: JPEG's 4:2:0 chroma
# subsampling bleeds the 700nm channel (red) into the 800nm channel (green) and its
# quantization noise is the same size as a faint band's whole display range. PNG of a
# very large composite gets expensive to send and decode, so above this many pixels
# fall back to JPEG at 4:4:4 (subsampling=0), which at least keeps the channels apart.
MAX_LOSSLESS_COMPOSITE_PIXELS = int(os.environ.get("MAX_LOSSLESS_COMPOSITE_PIXELS", 4_000_000))
# Ceiling on a composite handed to the browser as raw intensities for live display-window
# adjustment (/channel-pixels). Two uint16 channels, so the response is 4 bytes/pixel —
# 8MB at this cap. Above it the client falls back to server-side rendering per adjustment.
MAX_INTERACTIVE_PIXELS = int(os.environ.get("MAX_INTERACTIVE_PIXELS", 2_000_000))
# Exponent for the uint16 pixel quantization curve in /channel-pixels. The client must
# raise decoded values by this same power; see the endpoint's docstring for why it is not
# linear. Keep in sync with PIXEL_QUANTIZE_CURVE in frontend/app.js.
PIXEL_QUANTIZE_CURVE = 2.0
# Blots per /blot-fingerprints request.
MAX_FINGERPRINT_BLOTS = int(os.environ.get("MAX_FINGERPRINT_BLOTS", 50))
# ...and the bound that actually matters: total stored bytes re-read per request. A count
# cap alone does not limit I/O, because each blot is two TIFs of up to MAX_TIF_BYTES — so
# 50 blots meant one request could pull 50 x 2 x 100 MB = 10 GB out of blob storage and
# SHA-256 all of it, repeatable at 30 req/min against bytes the caller only had to upload
# once. The budget is checked before each blot rather than mid-read (a partial read would
# hash to a wrong digest), so a request can overshoot by at most one blot's two files.
MAX_FINGERPRINT_BYTES = int(os.environ.get("MAX_FINGERPRINT_BYTES", 200 * 1024 * 1024))
# Default display window percentiles. The white point sits above p99 because bands
# occupy well under 1% of a blot's pixels, so p99 lands on a band's shoulder and
# flattens every core to one value.
DISPLAY_AUTO_LOW_PCT = 1.0
DISPLAY_AUTO_HIGH_PCT = 99.8
MAX_DISPLAY_GAMMA = 5.0
MIN_DISPLAY_GAMMA = 0.1
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
# Separate, much shorter TTL for uploads/. A session's blot files live as long as the tab
# might come back to them; an uploaded ZIP is consumed inside one /process-upload call, so
# anything older than this is abandoned by definition. Clamped to the session TTL at use.
UPLOAD_FILE_TTL_SECONDS = int(os.environ.get("UPLOAD_FILE_TTL_SECONDS", 60 * 60))
# "log" (default) | "enforce" | "off" — see enforce_blob_host_pin. Ships as log-only so the
# pin can be shown silent against real traffic before it can reject anything.
BLOB_HOST_PIN = (os.environ.get("BLOB_HOST_PIN", "log") or "log").strip().lower()
if BLOB_HOST_PIN not in ("log", "enforce", "off"):
    # Fail loudly at import rather than silently degrading to "log". An operator who typed
    # "enforced" would otherwise believe the pin was on while it was only observing.
    raise RuntimeError(
        f"BLOB_HOST_PIN must be 'log', 'enforce', or 'off' (got {BLOB_HOST_PIN!r})."
    )
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


SENSITIVE_BLOB_HEADERS = ("authorization", "x-vercel-blob-store-id", "x-api-blob-request-id")
# Host of BLOB_API_BASE_URL. Object reads use *.blob.vercel-storage.com; the control-plane
# calls (put/list/delete) use this instead, and the two allowlists are not interchangeable.
VERCEL_BLOB_API_HOST = "vercel.com"


class BlobRedirectHandler(HTTPRedirectHandler):
    """Re-apply the host allowlist on every redirect hop, and never carry the blob token
    off-host.

    urlopen's default handler copies EVERY header except content-length/content-type onto
    the redirect target, across up to 10 hops, with no re-validation — so
    `authorization: Bearer <BLOB_READ_WRITE_TOKEN>` (read/write/delete over the whole
    store, every session) would follow a 3xx anywhere the first host chose to point it.
    Validating only the initial URL, as vercel_blob_request does, is not enough on its own:
    the allowlist decides who we are willing to *send the token to*, so it has to hold for
    the hop that actually receives it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is None:
            return None
        old_host = urlparse(req.full_url).hostname
        new_host = urlparse(newurl).hostname
        # Apply the allowlist the ORIGINAL url passed, not the object-host one unconditionally.
        # Half of all blob traffic starts at vercel.com/api/blob (put, list, delete), whose
        # host is deliberately NOT in the object allowlist — so checking only that suffix
        # turned an ordinary same-host canonicalising redirect there into a hard 502,
        # including on the sweep's own list call. Fail-closed, but a self-inflicted outage.
        if old_host == VERCEL_BLOB_API_HOST:
            allowed = new_host == VERCEL_BLOB_API_HOST
        else:
            allowed = is_allowed_vercel_blob_host(new_host)
        if not allowed:
            raise PublicError("Temporary storage redirected off-host.", 502)
        # Same-host hops keep their credentials; anything else is stripped. Compared on the
        # full host, so a redirect to a DIFFERENT store under the allowed suffix also loses
        # the token rather than handing it to a store we do not own.
        if new_host != old_host:
            for header in SENSITIVE_BLOB_HEADERS:
                new_request.headers.pop(header.capitalize(), None)
                new_request.headers.pop(header, None)
        return new_request


_blob_opener = build_opener(BlobRedirectHandler)


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


def blob_host_matches_own_store(hostname):
    """True if `hostname` belongs to THIS deployment's blob store.

    is_allowed_vercel_blob_host accepts any *.blob.vercel-storage.com and the path pin only
    fixes the pathname, so a descriptor can name a store we do not own. Since /channel-pixels
    returns the decoded intensity array, that turns the deployment into a read proxy for any
    public blob another tenant happens to keep at a session-shaped path.

    vercel_blob_store_id() is already called for every blob request (it is sent as
    x-vercel-blob-store-id), so a deployment where it cannot be determined has no working
    blob storage at all — pinning to it therefore adds no new failure mode. It raises rather
    than returning a sentinel, which is the correct fail-closed behaviour; do NOT add a
    wildcard fallback here, that would silently restore the hole."""
    host = (hostname or "").lower().rstrip(".")
    suffix = ".blob.vercel-storage.com"
    if not host.endswith(suffix):
        return False
    label = host[: -len(suffix)]
    store = vercel_blob_store_id().lower()
    return label in {store, f"{store}.public", f"{store}.private"}


def enforce_blob_host_pin(url, parsed):
    """Apply BLOB_HOST_PIN. Default "log": report a foreign-store descriptor and allow it,
    so the pin can be proven silent against real traffic before it is switched on. Every
    legitimate descriptor URL was minted by this deployment's own store, so the expected
    mismatch count is zero — once a full traffic cycle confirms that, set
    BLOB_HOST_PIN=enforce."""
    if BLOB_HOST_PIN == "off":
        return
    try:
        if blob_host_matches_own_store(parsed.hostname):
            return
    except Exception:
        # Store id undeterminable: in log mode say nothing (every blob call is already
        # failing for the same reason); in enforce mode fail closed below.
        if BLOB_HOST_PIN != "enforce":
            return
    app.logger.warning(
        json.dumps({
            "event": "blob_host_pin_mismatch",
            "mode": BLOB_HOST_PIN,
            "hostHash": log_token(parsed.hostname or ""),
            "path": log_temp_path(unquote(parsed.path.lstrip("/"))),
        })
    )
    if BLOB_HOST_PIN == "enforce":
        raise BlobHostPinError("Invalid temporary file reference.")


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
    enforce_blob_host_pin(url, parsed)
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
            # _blob_opener, not urlopen: the default opener would carry the blob token
            # across an off-host redirect. See BlobRedirectHandler.
            with _blob_opener.open(request, timeout=timeout) as response:
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
        except BlobHostPinError:
            # Deliberately NOT swallowed. Skipping is right for a malformed or
            # attacker-shaped descriptor; a pin mismatch is a configuration problem, and
            # silently skipping every URL would report a healthy "removed: 0" forever.
            raise
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


TEMP_ROOTS = ("sessions", "uploads")


def session_id_from_temp_path(path):
    """Return the session-id segment of a 'sessions/<id>/...' or 'uploads/<id>/...' path,
    else None. Both roots are keyed by session, and both need sweeping — see
    sweep_expired_vercel_blobs for why uploads/ used to be missed entirely."""
    if not isinstance(path, str):
        return None
    parts = path.split("/")
    if len(parts) < 2 or parts[0] not in TEMP_ROOTS or not parts[1]:
        return None
    return parts[1]


def expired_session_blobs(blobs, cutoff_epoch, exclude_session_id=None, root=None):
    """Pure selector: from a Vercel Blob list page, return the {pathname, url}
    entries under sessions/ or uploads/ that were uploaded at or before `cutoff_epoch`
    and do not belong to the excluded session. Side-effect free for unit testing.

    `root` restricts selection to one temp root, and it is what makes the caller's per-root
    cutoffs safe. The caller applies a DIFFERENT cutoff to each root (uploads expire in an
    hour, sessions in a day), so without this check the correctness of both rests entirely
    on the remote listing honouring its `prefix` argument. Any change that dropped or
    mistyped that prefix would apply the one-hour upload cutoff to sessions/ and delete
    every other session's live blot files. Cheap invariant; catastrophic to be missing."""
    excluded = safe_id(exclude_session_id) if exclude_session_id else None
    expired = []
    for blob in blobs or []:
        if not isinstance(blob, dict):
            continue
        pathname = blob.get("pathname") or blob.get("path") or ""
        url = blob.get("url")
        if root is not None and not pathname.startswith(f"{root}/"):
            continue
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


def sweep_expired_local_files(cutoff_epoch, upload_cutoff_epoch, exclude_session_id, max_deletes):
    """Local mirror of sweep_expired_vercel_blobs, including its uploads/ root — see there
    for why that root cannot be left to process_storage_upload's `finally`."""
    excluded = safe_id(exclude_session_id) if exclude_session_id else None
    removed = 0
    # uploads/ first, for the starvation reason documented in sweep_expired_vercel_blobs.
    for root, root_cutoff in (("uploads", upload_cutoff_epoch), ("sessions", cutoff_epoch)):
        base = os.path.join(os.path.abspath(LOCAL_TEMP_DIR), root)
        if not os.path.isdir(base):
            continue
        with os.scandir(base) as entries:
            for entry in entries:
                if removed >= max_deletes:
                    break
                if not entry.is_dir() or not SAFE_PATH_PART_PATTERN.fullmatch(entry.name):
                    continue
                if excluded and entry.name == excluded:
                    continue
                newest = newest_descendant_mtime(entry.path)
                if newest is None or newest > root_cutoff:
                    continue
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
        if removed >= max_deletes:
            break
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


def sweep_expired_vercel_blobs(cutoff_epoch, upload_cutoff_epoch, exclude_session_id, max_deletes):
    """Reclaim expired objects under BOTH temp roots.

    uploads/ used to be skipped here on the belief that process_storage_upload's `finally`
    always deletes the ZIP. It does not: the browser PUTs straight to Blob and only then
    calls /process-upload, so a closed tab, a lost connection, or a caller that simply
    stops leaves the object with nothing that ever deletes it — not this sweep (it never
    listed the prefix), not /cleanup (no caller sends `upload`). Those are the largest
    objects the app writes, so the leak was unbounded and permanent.

    uploads/ gets a much shorter cutoff because it is consumed inside a single request;
    anything older than UPLOAD_FILE_TTL_SECONDS is abandoned by definition."""
    removed = 0
    budget = max_deletes
    # uploads/ FIRST, and the order is load-bearing. Both roots share one delete budget, so
    # sweeping sessions/ first meant a store expiring more than TEMP_SWEEP_MAX_DELETES
    # session objects in a day — roughly a busy week of uploads, at 3 objects a blot — would
    # exhaust the budget before uploads/ was ever listed, and the abandoned-ZIP leak this
    # function exists to close would persist untouched, in steady state rather than as a
    # delay (list order is deterministic by pathname, so there is no eventual rotation).
    # uploads/ holds few objects but the largest ones, so going first costs sessions/ almost
    # nothing, and a session backlog catches up over successive daily runs.
    for root, prefix_cutoff in (("uploads", upload_cutoff_epoch), ("sessions", cutoff_epoch)):
        if budget <= 0:
            break
        to_delete = []  # (descriptor, validated_path) — per root, see the delete below
        cursor = None
        for _page in range(TEMP_SWEEP_MAX_PAGES):
            page = vercel_blob_list(prefix=f"{root}/", cursor=cursor) or {}
            for blob in expired_session_blobs(
                page.get("blobs", []), prefix_cutoff, exclude_session_id, root=root
            ):
                try:
                    # allow_uploads: this is the sweep's own listing, not a client-supplied
                    # descriptor, and uploads/<session>/<id>.zip is a shape it must accept.
                    path = validate_temp_path(blob["pathname"], allow_uploads=True)
                except PublicError:
                    continue
                to_delete.append(({"url": blob["url"], "pathname": path}, path))
                if len(to_delete) >= budget:
                    break
            if len(to_delete) >= budget or not page.get("hasMore"):
                break
            cursor = page.get("cursor")
            if not cursor:
                break
        # Delete per root rather than accumulating everything and issuing one call at the
        # end. vercel_blob_list raises after its own retries on a 429/5xx, and that used to
        # discard every deletion already collected — zero progress for the whole run, no
        # partial credit. This range doubled the number of list calls (two prefixes), so it
        # doubled the chance of hitting that. Now a later failure keeps the earlier root's work.
        if to_delete:
            deleted = vercel_blob_delete(to_delete)
            removed += deleted
            budget -= deleted
    return removed


_last_temp_sweep_monotonic = None


def sweep_expired_temp_files(
    exclude_session_id=None,
    max_age_seconds=None,
    max_deletes=None,
    force=False,
):
    """Best-effort reclamation of orphaned temp files under both roots. Never raises.

    Returns the number removed, or None if the sweep FAILED. The distinction matters:
    returning 0 for a failure made a permanently broken sweep (expired token, Blob API
    change, oversized list response) look exactly like a healthy quiet day, and the only
    monitor is Vercel Cron alerting on non-2xx."""
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
    # An abandoned upload is dead the moment its one /process-upload call does not arrive,
    # so it does not need the session TTL's grace. Never longer than the session cutoff.
    # UPLOAD_FILE_TTL_SECONDS <= 0 means "no separate uploads TTL", matching the
    # max_age_seconds <= 0 convention above. Without that guard it inverted into the most
    # destructive possible setting: min(..., 0) put the cutoff at now, expiring EVERY upload
    # including one being streamed by an in-flight /process-upload in another session.
    upload_cutoff_epoch = cutoff_epoch
    if UPLOAD_FILE_TTL_SECONDS > 0:
        upload_cutoff_epoch = max(
            cutoff_epoch, time.time() - min(max_age_seconds, UPLOAD_FILE_TTL_SECONDS)
        )
    try:
        if USE_VERCEL_BLOB:
            removed = sweep_expired_vercel_blobs(
                cutoff_epoch, upload_cutoff_epoch, exclude_session_id, max_deletes
            )
            backend = "vercel-blob"
        else:
            removed = sweep_expired_local_files(
                cutoff_epoch, upload_cutoff_epoch, exclude_session_id, max_deletes
            )
            backend = "local"
        if removed:
            app.logger.info(
                "Temp TTL sweep removed=%d backend=%s ttlSeconds=%d",
                removed, backend, max_age_seconds,
            )
        return removed
    except Exception:
        app.logger.exception("Temp file TTL sweep failed")
        return None      # not 0 — see the docstring; the caller must be able to tell


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
    if removed is None:
        # Non-2xx on purpose: Vercel Cron only alerts on failure status, so returning
        # {"status":"ok","removed":0} for a broken sweep meant orphaned objects could
        # accumulate forever with nothing to distinguish that from a quiet day.
        return jsonify({"status": "error", "error": "Temp file sweep failed."}), 500
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


def update_channel_fingerprint(digest, channel, member_bytes):
    """Feed one channel's bytes into a blot fingerprint, in the caller's channel order
    ("700" then "800"). Shared by the upload path and /blot-fingerprints so the two can
    never disagree about how a blot is identified — if they drifted, a re-upload would stop
    being recognised as a duplicate. Length-prefixed and channel-labelled so the same bytes
    appearing under a different channel cannot collide."""
    digest.update(f"{channel}:{len(member_bytes)}:".encode("ascii"))
    digest.update(member_bytes)


def store_blot_archive_members(zf, members, session_id, blot_id):
    """Validate and store one archive member at a time so a large preview and
    both TIF channels are never retained in memory together.

    Returns (files, fingerprint). The fingerprint is a SHA-256 over the MEASURED channel
    bytes only, so the client can recognise a blot it already holds and refuse to add it
    twice. The preview JPG is excluded deliberately: it is cosmetic, and a re-export could
    change it while the data is identical. Metadata is excluded for the same reason — if
    the pixels are byte-identical it is the same scan, whatever it has been renamed to.
    """
    files = {}
    digest = hashlib.sha256()
    hashed_any = False
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
                update_channel_fingerprint(digest, kind, member_bytes)
                hashed_any = True
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
        # A folder with a .txt (and maybe a .jpg) but no TIF hashes nothing, and
        # hexdigest() would then return SHA-256 of the empty input — e3b0c442…b855, a
        # NON-EMPTY string that sails straight past the client's `if (!fingerprint)`
        # sentinel. Every such blot after the first would be judged a duplicate of the
        # first, dropped, and have its temp files deleted, under the message "same image
        # data as X" — with nothing having been compared. /blot-fingerprints already
        # refuses to emit a digest in this case (its hashed_any guard); match it, so the
        # two producers of this field cannot disagree about what "no fingerprint" means.
        return files, (digest.hexdigest() if hashed_any else "")
    except Exception:
        delete_temp_files_safely(
            list(files.values()),
            session_id,
            context="Partial blot file rollback cleanup",
        )
        raise


def blot_record(blot_id, blot_name, created_at, members, files, fingerprint=""):
    return {
        "id": blot_id,
        "name": blot_name,
        # Content hash of the measured channels. The client uses it to skip a blot it
        # already holds; blot ids are freshly generated per upload and cannot serve.
        "fingerprint": fingerprint,
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
    stored_files, fingerprint = store_blot_archive_members(zf, members, session_id, blot_id)
    return blot_record(blot_id, blot_name, created_at, members, stored_files, fingerprint)


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


class BlobHostPinError(PublicError):
    """A descriptor URL named a store this deployment does not own, with BLOB_HOST_PIN
    enforcing. Distinct from PublicError so that callers which intentionally skip malformed
    descriptors do NOT also skip a configuration problem: vercel_blob_delete's
    `except PublicError: continue` would otherwise turn a host-shape mismatch into "every
    URL skipped, 0 deleted, status ok" on every sweep forever — exactly the
    indistinguishable-from-healthy state the None/500 return was added to eliminate.

    Defined here rather than beside the blob helpers above because it needs PublicError at
    class-creation time; those helpers only reference it inside function bodies."""


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
            # Count before materialising: len() walks the IFD offset chain, while list()
            # constructs a TiffPage per entry. Checking the cap first means a file that is
            # nothing but a long chain of IFDs is rejected without paying to build them all.
            if len(tif.pages) > MAX_TIF_PAGES:
                raise PublicError(f"{label} has an unsupported number of image pages.")
            pages = list(tif.pages)
            if not pages:
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
            # Count before materialising: len() walks the IFD offset chain, while list()
            # constructs a TiffPage per entry. Checking the cap first means a file that is
            # nothing but a long chain of IFDs is rejected without paying to build them all.
            if len(tif.pages) > MAX_TIF_PAGES:
                raise PublicError(f"{label} has an unsupported number of image pages.")
            pages = list(tif.pages)
            if not pages:
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
        # Only one channel is required: a single-channel blot renders the channel it has
        # (this used to 404, so such blots could not be viewed at all), and the user can
        # switch either channel off on a two-channel blot.
        blot = load_payload_blot(data.get("blot"), ("700", "800"), session_id)
        color_mode = data.get("colorMode", "color")
        # Display windows in RAW COUNTS, the unit the sliders and /channel-stats report.
        # None means "auto" and is resolved per channel inside build_composite.
        window_700 = parse_display_window(data, "700")
        window_800 = parse_display_window(data, "800")

        # Per-blot straighten happens per-channel in the native frame inside build_composite
        # (BEFORE the common-size resize) so the displayed rotation matches /detect-bands and
        # /extract exactly even when the two channels differ in aspect ratio.
        rotation = parse_rotation(data)
        img = build_composite(
            blot["tif_700_bytes"],
            blot["tif_800_bytes"],
            window_700,
            window_800,
            color_mode,
            rotation,
            parse_enabled_channels(data),
        )

        buf = io.BytesIO()
        if img.width * img.height <= MAX_LOSSLESS_COMPOSITE_PIXELS:
            img.save(buf, format="PNG", compress_level=6)
            mimetype = PNG_MIMETYPE
        else:
            # subsampling=0 keeps full chroma resolution, so the red (700nm) and green
            # (800nm) channels stay separate even on the lossy path.
            img.save(buf, format="JPEG", quality=95, subsampling=0)
            mimetype = JPEG_MIMETYPE
        buf.seek(0)
        return send_file(buf, mimetype=mimetype)
    except PublicError as error:
        return error_response(error, "Could not render blot image.")
    except Exception as error:
        app.logger.exception("Composite render failed")
        return error_response(error, "Could not render blot image.")


@app.route("/blot-fingerprints", methods=["POST"])
@app.route("/api/blot-fingerprints", methods=["POST"])
@limiter.limit("30 per minute")
def blot_fingerprints():
    """Content fingerprints for blots the client already holds, computed from their stored
    temp files. Needed because fingerprints only started being returned at upload time:
    without this, a workspace restored from before the change would have none, so
    re-uploading the same ZIP would still add a duplicate — the feature would look broken
    for exactly the blots a user already has on screen.

    Produces the same digest as the upload path (both go through
    update_channel_fingerprint) because the stored file is byte-identical to the ZIP member
    it came from."""
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    try:
        blots = data.get("blots")
        if not isinstance(blots, list):
            return jsonify({"error": "Blots must be a list."}), 400
        if len(blots) > MAX_FINGERPRINT_BLOTS:
            return jsonify({
                "error": f"Too many blots. Maximum is {MAX_FINGERPRINT_BLOTS} per request.",
            }), 400

        fingerprints = {}
        budget = MAX_FINGERPRINT_BYTES
        truncated = False
        for entry in blots:
            if budget <= 0:
                # Out of read budget. Report it so the client can ask for the rest in a
                # follow-up request instead of silently losing dedup on the tail.
                truncated = True
                break
            try:
                blot = load_payload_blot(entry, (), session_id)
            except PublicError:
                continue        # a blot whose files have already been swept: just skip it
            blot_id = blot.get("id")
            if not isinstance(blot_id, str) or not blot_id:
                continue
            digest = hashlib.sha256()
            complete = True
            hashed_any = False
            for channel in ("700", "800"):
                descriptor = blot["files"].get(channel)
                if not descriptor:
                    continue        # single-channel blot: the upload path skipped it too
                try:
                    channel_bytes = read_temp_file(
                        descriptor, session_id, max_bytes=BLOT_FILE_LIMITS[channel]
                    )
                except Exception:
                    # Charge the full per-file limit, not zero. A read that fails with 413
                    # has already pulled max_bytes+1 off the wire (see vercel_blob_request),
                    # so leaving the budget untouched would make oversize objects free and
                    # hand back the very amplification the budget exists to stop.
                    budget -= BLOT_FILE_LIMITS[channel]
                    complete = False
                    break
                budget -= len(channel_bytes)
                update_channel_fingerprint(digest, channel, channel_bytes)
                hashed_any = True
            # Only report a whole fingerprint. A partial one would hash fewer channels than
            # the upload path did, so it could never match — and worse, two different blots
            # that both failed on their second channel would hash the same.
            if complete and hashed_any:
                fingerprints[blot_id] = digest.hexdigest()
        return jsonify({"fingerprints": fingerprints, "truncated": truncated})
    except PublicError as error:
        return error_response(error, "Could not fingerprint blots.")
    except Exception as error:
        app.logger.exception("Blot fingerprinting failed")
        return error_response(error, "Could not fingerprint blots.")


@app.route("/channel-stats", methods=["POST"])
@app.route("/api/channel-stats", methods=["POST"])
@limiter.limit("60 per minute")
def channel_stats():
    """Raw-count statistics per channel, so the client can label its display-window
    sliders in real intensity units and bound them to this blot's actual range. Without
    this the window controls would have no idea what numbers mean for a given scan."""
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    try:
        blot = load_payload_blot(data.get("blot"), (), session_id)
        stats = {}
        for channel in ("700", "800"):
            descriptor = blot["files"].get(channel)
            if not descriptor:
                continue
            tif_bytes = read_temp_file(descriptor, session_id, max_bytes=BLOT_FILE_LIMITS[channel])
            arr = read_tif_channel(tif_bytes)
            auto_low, auto_high = auto_display_window(arr)
            percentiles = np.percentile(arr, [1.0, 50.0, 99.0, 99.8, 99.99])
            stats[channel] = {
                "min": float(arr.min()),
                "max": float(arr.max()),
                "p1": float(percentiles[0]),
                "median": float(percentiles[1]),
                "p99": float(percentiles[2]),
                "p998": float(percentiles[3]),
                "p9999": float(percentiles[4]),
                "autoMin": auto_low,
                "autoMax": auto_high,
                "width": int(arr.shape[1]),
                "height": int(arr.shape[0]),
            }
        if not stats:
            return jsonify({"error": "No TIF channels found for this blot"}), 404
        return jsonify({"channels": stats})
    except PublicError as error:
        return error_response(error, "Could not read channel statistics.")
    except Exception as error:
        app.logger.exception("Channel statistics failed")
        return error_response(error, "Could not read channel statistics.")


@app.route("/channel-pixels", methods=["POST"])
@app.route("/api/channel-pixels", methods=["POST"])
@limiter.limit("30 per minute")
def channel_pixels():
    """Both channels' intensities in composite space so the browser can apply the display
    window itself and repaint while a slider is being dragged, with no round trip.

    Body layout (little-endian), a self-describing binary prefix rather than metadata in
    headers so it survives any proxy or cross-origin setup without needing
    Access-Control-Expose-Headers:

        0  magic       4 bytes  b'WBP1'
        4  width       uint32
        8  height      uint32
        12 channels    uint32   (always 2: the composite needs both)
        16 scale       float32 x 4   700 min, 700 max, 800 min, 800 max
        32 pixels      uint16 x width*height, 700nm then 800nm

    Counts are quantized to uint16 against each channel's own [min, max], through a
    square-root curve (PIXEL_QUANTIZE_CURVE) rather than linearly. Linear quantization
    looks fine on average but fails exactly where it matters: a channel holding saturated
    pixels has a scale top of 65504, so a linear step is ~1 full count and a user who
    windows down to [0, 50] to hunt for a faint band would have only ~50 distinct source
    levels feeding 256 display levels — visible banding. The curve puts the resolution
    where the signal is, giving ~0.12-count steps at 250 counts in that same channel.
    """
    data = request.get_json(silent=True) or {}
    session_id = request_session_id(data)
    try:
        # Deliberately ignores which channels are enabled: the buffer always carries both,
        # so switching a channel off is an instant local recomposite with no refetch. An
        # absent channel arrives as zeros (prepare_composite_channels), and /channel-stats
        # omits it, which is how the client knows it cannot be switched back on.
        blot = load_payload_blot(data.get("blot"), ("700", "800"), session_id)
        rotation = parse_rotation(data)
        ch_700, ch_800, _auto_700, _auto_800 = prepare_composite_channels(
            blot["tif_700_bytes"], blot["tif_800_bytes"], rotation
        )
        height, width = ch_700.shape
        if width * height > MAX_INTERACTIVE_PIXELS:
            # Too big to hand over wholesale. The client falls back to asking the server
            # for a rendered composite per adjustment, which is the pre-Pass-2 behaviour.
            return jsonify({
                "error": "Blot is too large for interactive display adjustment.",
                "maxPixels": MAX_INTERACTIVE_PIXELS,
            }), 413

        payload = bytearray()
        payload += b"WBP1"
        payload += struct.pack("<III", width, height, 2)
        scales = []
        quantized = []
        for channel in (ch_700, ch_800):
            low = float(channel.min())
            high = float(channel.max())
            if not (high > low):
                high = low + 1.0
            scales.extend((low, high))
            normalized = np.clip((channel - low) / (high - low), 0.0, 1.0)
            curved = np.power(normalized, 1.0 / PIXEL_QUANTIZE_CURVE) * 65535.0
            quantized.append(np.rint(curved).astype("<u2"))
        payload += struct.pack("<ffff", *scales)
        for channel in quantized:
            payload += channel.tobytes(order="C")

        response = send_file(io.BytesIO(bytes(payload)), mimetype="application/octet-stream")
        # The buffer is derived entirely from session-scoped temp files and is not
        # revalidated on reuse, so keep it out of any shared cache.
        response.headers["Cache-Control"] = "no-store"
        return response
    except PublicError as error:
        return error_response(error, "Could not read channel pixels.")
    except Exception as error:
        app.logger.exception("Channel pixel read failed")
        return error_response(error, "Could not read channel pixels.")


def read_tif_channel(tif_bytes):
    """RAW validated counts for the display path, size-capped for the render budget.

    Deliberately does NOT normalize. The display window (black point / white point) is
    applied later from client-supplied raw counts, so the user can move the white point
    past a band's core the way Image Studio's min/max levels do. Normalizing here — the
    old behaviour, a fixed p1/p99 stretch — clipped band cores to a single value before
    any control could reach them, and nothing downstream could recover them.
    """
    arr = read_raw_channel(tif_bytes)
    if arr.size > MAX_RENDER_PIXELS:
        raise PublicError(
            "Blot image is too large to render a composite preview. "
            f"Maximum is {MAX_RENDER_PIXELS:,} pixels.",
            413,
        )
    return arr


def auto_display_window(arr):
    """Default (black, white) points in raw counts for a channel. Percentile-based so it
    ignores hot pixels, with the white point high enough that band cores keep their shape."""
    low = float(np.percentile(arr, DISPLAY_AUTO_LOW_PCT))
    high = float(np.percentile(arr, DISPLAY_AUTO_HIGH_PCT))
    if not np.isfinite(low):
        low = 0.0
    if not np.isfinite(high) or high <= low:
        finite = arr[np.isfinite(arr)]
        high = float(finite.max()) if finite.size else low
    if high <= low:
        high = low + 1.0
    return low, high


def finite_float(value):
    """A finite float from untrusted JSON, or None. Rejects NaN/inf so they cannot reach
    the window arithmetic and turn a whole channel into NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if np.isfinite(number) else None


def parse_enabled_channels(data):
    """Which channels the client wants shown, as a tuple. Anything unparseable means both.
    An explicitly empty selection also falls back to both rather than rendering a blank
    frame — the UI forbids turning the last channel off, so an empty list is a bug or a
    hand-rolled request, and a black image is a worse answer than showing everything."""
    requested = data.get("enabledChannels")
    if not isinstance(requested, list):
        return ("700", "800")
    enabled = tuple(channel for channel in ("700", "800") if channel in requested)
    return enabled or ("700", "800")


def parse_display_window(data, channel):
    """The client's display window for one channel as (low, high, gamma) in raw counts,
    or None to mean 'use auto'. Gamma alone is enough to build a window: a request that
    sends only gamma keeps the auto black/white points, which is what the Reset button
    leaves behind."""
    low = finite_float(data.get(f"displayMin{channel}"))
    high = finite_float(data.get(f"displayMax{channel}"))
    gamma = finite_float(data.get(f"gamma{channel}"))
    if gamma is None:
        gamma = 1.0
    gamma = max(MIN_DISPLAY_GAMMA, min(MAX_DISPLAY_GAMMA, gamma))
    if low is None or high is None or high <= low:
        # An unusable pair falls back to auto rather than erroring: the window is a view
        # preference, and a stale or half-populated slider should still render something.
        return (None, None, gamma)
    return (low, high, gamma)


def apply_display_window(channel, low, high, gamma=1.0):
    """Map raw counts onto [0,1] with `low` as black and `high` as white, then bend the
    curve with gamma. This is Image Studio's model: the WINDOW decides what is visible
    and gamma only shapes the tones between the two points. gamma < 1 lifts faint bands,
    gamma > 1 pushes background down."""
    span = high - low
    if span <= 0:
        return np.zeros(channel.shape, dtype=np.float32)
    scaled = np.clip((channel - low) / span, 0.0, 1.0).astype(np.float32, copy=False)
    if gamma != 1.0:
        scaled = np.power(scaled, gamma, dtype=np.float32)
    return scaled


def prepare_composite_channels(tif_700_bytes, tif_800_bytes, rotation=0.0):
    """Both channels as RAW counts in common composite space, plus each one's auto window.

    Shared by build_composite and /channel-pixels so the two cannot disagree about
    geometry: the client composites the buffers this returns, and a mismatch in size or
    rotation would put the live preview out of register with the drawn boxes.

    A channel whose file is absent (single-channel blot) becomes zeros shaped like the one
    that is present. Note the distinction from a channel the user has DISABLED: a disabled
    channel is still decoded here and still counts toward the composite size, because
    composite dimensions are max(700, 800) and box coordinates live in that space —
    resizing the frame when a channel is switched off would move every drawn box.
    """
    if not tif_700_bytes and not tif_800_bytes:
        raise PublicError("TIF files not found for this blot", 404)
    ch_700 = read_tif_channel(tif_700_bytes) if tif_700_bytes else None
    ch_800 = read_tif_channel(tif_800_bytes) if tif_800_bytes else None

    # Resolve auto windows on the NATIVE, unrotated counts. Rotation zero-fills the new
    # corners and the common-size resize interpolates, both of which would drag the
    # percentiles around — so a blot would change brightness as the user turned the
    # rotate slider, and /channel-stats (which reports native counts) would disagree
    # with what is on screen.
    auto_700 = auto_display_window(ch_700) if ch_700 is not None else (0.0, 1.0)
    auto_800 = auto_display_window(ch_800) if ch_800 is not None else (0.0, 1.0)

    # Per-blot straighten: rotate each channel in its OWN native frame, BEFORE the common-size
    # resize, so the displayed rotation matches /extract exactly (which rotates the native array,
    # then scales the box composite->native). Rotating the finished composite instead diverges
    # from /extract whenever the two channels differ in aspect ratio — a non-uniform
    # composite->native scale does not commute with rotation. See scale_box / the coord model.
    if rotation:
        if ch_700 is not None:
            ch_700 = _rotate_array(ch_700, rotation)
        if ch_800 is not None:
            ch_800 = _rotate_array(ch_800, rotation)

    if ch_700 is None:
        ch_700 = np.zeros_like(ch_800)
    elif ch_800 is None:
        ch_800 = np.zeros_like(ch_700)
    elif ch_700.shape != ch_800.shape:
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
        # Resize in float ("F" mode) so raw counts survive: the old path scaled by 65535
        # into uint16, which assumed the array was already normalized to [0,1] and would
        # now saturate every real count above 1.
        ch_700 = _resize_float(ch_700, w, h)
        ch_800 = _resize_float(ch_800, w, h)

    return ch_700, ch_800, auto_700, auto_800


def build_composite(tif_700_bytes, tif_800_bytes, window_700=None, window_800=None, color_mode="color", rotation=0.0, enabled_channels=("700", "800")):
    """Render the two channels into one RGB image. Each window is (low, high, gamma) in
    raw counts; None for low/high means auto for that channel. A channel left out of
    enabled_channels contributes nothing, which is how a user turns one off — the frame
    keeps its size either way, so drawn boxes do not move."""
    ch_700, ch_800, auto_700, auto_800 = prepare_composite_channels(
        tif_700_bytes, tif_800_bytes, rotation
    )
    low_700, high_700, gamma_700 = resolve_window(window_700, auto_700)
    low_800, high_800, gamma_800 = resolve_window(window_800, auto_800)

    ch_700 = apply_display_window(ch_700, low_700, high_700, gamma_700)
    ch_800 = apply_display_window(ch_800, low_800, high_800, gamma_800)
    if "700" not in enabled_channels:
        ch_700 = np.zeros_like(ch_700)
    if "800" not in enabled_channels:
        ch_800 = np.zeros_like(ch_800)

    if color_mode == "grayscale":
        blended = np.clip(ch_700 + ch_800, 0.0, 1.0)
        blended = 1.0 - blended
        gray = to_display_bytes(blended)
        rgb = np.stack([gray, gray, gray], axis=2)
    else:
        r = to_display_bytes(ch_700)
        g = to_display_bytes(ch_800)
        b = np.zeros_like(r)
        rgb = np.stack([r, g, b], axis=2)

    return Image.fromarray(rgb, mode="RGB")


def _resize_float(arr, w, h):
    """LANCZOS resize of a 2-D float array without leaving the float domain."""
    img = Image.fromarray(arr.astype(np.float32, copy=False), mode="F")
    return np.asarray(img.resize((w, h), Image.LANCZOS), dtype=np.float32)


def to_display_bytes(normalized):
    """[0,1] -> 0..255, rounding rather than truncating. Truncation threw away up to a
    whole level and biased everything dark, which matters most for faint bands: they live
    in the bottom handful of the 8-bit range."""
    return np.clip(np.rint(normalized * 255.0), 0, 255).astype(np.uint8)


def resolve_window(window, auto):
    """(low, high, gamma) for a channel, filling in the auto black/white points when the
    client did not supply a usable pair. `auto` is this channel's (low, high) default."""
    if window is None:
        return auto[0], auto[1], 1.0
    low, high, gamma = window
    if low is None or high is None:
        low, high = auto
    return low, high, gamma

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

        # Per-blot straighten: rotate the native pixels by the same angle applied to the
        # composite display and the detector, so axis-aligned boxes measure the intended
        # (straightened) content. Signal uses bilinear; the saturation array uses nearest and
        # is cast back to its native dtype so tif_saturation_mask's per-dtype max stays valid.
        rotation = parse_rotation(data)
        if rotation:
            # Rotating converts the array to float32 (+ a PIL copy), so cap it at the render
            # ceiling — straighten is display-driven and a blot too large to render a composite
            # can't be straightened in the UI anyway. Keeps the documented memory bound honest.
            if arr.size > MAX_RENDER_PIXELS:
                raise PublicError(
                    "Blot is too large to straighten. "
                    f"Maximum is {MAX_RENDER_PIXELS:,} pixels.",
                    413,
                )
            raw_dtype = raw_pixels.dtype
            arr = _rotate_array(arr, rotation)
            raw_pixels = _rotate_array(raw_pixels, rotation, resample=Image.NEAREST).astype(raw_dtype, copy=False)

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


def parse_rotation(data):
    """The client's per-blot straighten angle (degrees), clamped to +/-MAX_STRAIGHTEN_DEG.
    0 (missing/invalid) means no rotation, i.e. the original unchanged code path."""
    value = data.get("rotationDeg")
    if isinstance(value, (int, float)) and np.isfinite(value):
        return float(max(-MAX_STRAIGHTEN_DEG, min(MAX_STRAIGHTEN_DEG, value)))
    return 0.0


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
    # No strip landed inside the image — a box spanning the full width with "Left & Right"
    # sides (which is what a full-width detected lane produces), or full height with
    # "Top & Bottom". background_per_pixel then stays 0.0 and adjustedSignal == rawSignal,
    # which is indistinguishable from a real measurement against a genuinely zero
    # background. On a blot whose membrane floor is most of a full-width ROI's integrated
    # density that is a large over-report, so flag it rather than let it pass as an ordinary
    # number. background_uneven cannot cover this: it needs two strips to compare.
    background_missing = not strips
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
        "backgroundMissing": background_missing,
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

# Cap on client-supplied (or auto-detected) lanes, so a malformed payload can't fan out.
# Applied to whichever list wins at the call site, not just the client's — auto-detection is
# bounded only by min_width (~100 lanes) and is what an unauthenticated request gets by
# default, so capping only the client path left the documented limit unenforced.
MAX_DETECT_LANES = 64
# Floor for a client-supplied laneThreshold, matching the UI slider's own `min`. See the
# clamp in detect_bands() for why 0.0 is the pathological case.
MIN_LANE_THRESHOLD = 0.02

# Lane detection robustness. Background under the lane axis is low-frequency (uneven
# membrane / gradient); a rolling MINIMUM over a window wider than one lane traces that
# floor without eating lane signal, and subtracting it stops gradients from inventing or
# merging lanes. The MW ladder is a bright NARROW column — after flattening we scale by a
# high percentile (not max) so that one spike can't raise the threshold and hide faint
# real lanes. A wide run is then split wherever an internal valley drops at least
# LANE_SPLIT_DEPTH (in normalized 0..1 units) below the shoulders on both sides.
LANE_BASELINE_FRAC = 0.25   # rolling-min baseline window as a fraction of image width
LANE_NORM_PCT = 99.0        # robust high percentile used to scale the flattened profile
LANE_SPLIT_DEPTH = 0.30     # min valley depth to split two merged lanes apart

# Per-lane band detection. The migration-axis profile within a lane has its own uneven
# exposure/smear; a morphological opening (min then max) over a window a few bands wide
# estimates that background so it can be subtracted — faint bands surface and gradient
# shoulders stop reading as peaks. Prominence thresholds below are calibrated on the
# resulting background-subtracted profile (validated against the real test blots).
BAND_BASELINE_FRAC = 0.12   # opening window along the migration axis, as a fraction of height

# Deskew search: try angles in [-MAX_SKEW_DEG, +MAX_SKEW_DEG] at this step and keep the
# one that makes the lane (column) projection sharpest. 0 disables auto-deskew.
MAX_SKEW_DEG = 5.0
SKEW_STEP_DEG = 0.5

# Per-blot straighten. The box model is axis-aligned {x, y, w, h}, so a rotation cannot be a
# detection-only trick: the SAME angle must be applied to the composite DISPLAY
# (/render-composite), to detection here, and to /extract (which rotates the native array
# before slicing) so measured pixels match the straightened view. The client owns that angle
# (a manual slider + an "Auto" button); this endpoint applies the client angle and also
# reports a SUGGESTED angle from _estimate_skew for the Auto button to propose. Default 0 =
# no rotation = unchanged path. See DETECT_BANDS_DESIGN.md ("Rotation / deskew").
MAX_STRAIGHTEN_DEG = 15.0

# The "sets of bands to choose between" = one detection per sensitivity level. Lower
# `prominence` => more (fainter) bands captured; that is the knob that varies across
# levels. `lane_threshold` is deliberately HELD CONSTANT: lowering it with sensitivity
# merges adjacent lanes into one (verified visually during calibration), which is never
# wanted. Values are in normalized (0..1) profile units — tune against real blots.
# Prominence is measured on the per-lane BACKGROUND-SUBTRACTED migration profile
# (_lane_band_profiles), whose peaks are smaller than the raw 0..1 profile's — so these
# thresholds are lower than a raw-profile detector would use. Calibrated against the real
# test blots (scratchpad/calibrate.py): conservative keeps only clear bands, aggressive
# reaches faint ones. lane_threshold is held constant across levels (see note above).
SENSITIVITY_LEVELS = {
    "conservative": {"prominence": 0.10, "lane_threshold": 0.15, "min_distance_frac": 0.030},
    "balanced":     {"prominence": 0.05, "lane_threshold": 0.15, "min_distance_frac": 0.020},
    "aggressive":   {"prominence": 0.02, "lane_threshold": 0.15, "min_distance_frac": 0.012},
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

def _rotate_array(arr, angle_deg, resample=Image.BILINEAR):
    """Rotate a 2-D float array about its center by angle_deg (CCW), keeping the same shape
    (corners cropped). Returns arr unchanged at ~0 deg. resample=NEAREST relocates pixels
    without interpolating, so the exact values survive — used for the saturation array so its
    per-dtype max counts stay valid after a cast back to the native dtype."""
    if abs(angle_deg) < 1e-3:
        return arr
    img = Image.fromarray(arr.astype(np.float32, copy=False), mode="F")
    rotated = img.rotate(angle_deg, resample=resample, expand=False, fillcolor=0.0)
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


def _rolling_min(a, window):
    """Centered sliding minimum (edge-padded), same length as `a`. numpy-only."""
    window = int(window)
    if window <= 1 or a.size == 0:
        return a
    ap = np.pad(a, window, mode="edge")
    mins = np.lib.stride_tricks.sliding_window_view(ap, window).min(axis=1)
    start = (len(mins) - len(a)) // 2
    return mins[start:start + len(a)]


def _rolling_max(a, window):
    """Centered sliding maximum (edge-padded), same length as `a`. numpy-only."""
    window = int(window)
    if window <= 1 or a.size == 0:
        return a
    ap = np.pad(a, window, mode="edge")
    maxs = np.lib.stride_tricks.sliding_window_view(ap, window).max(axis=1)
    start = (len(maxs) - len(a)) // 2
    return maxs[start:start + len(a)]


def _morph_open(profile, window):
    """Grayscale morphological opening (erosion then dilation) — a background estimate that
    stays below the peaks without cutting into their bases the way a bare rolling-min does."""
    return _rolling_max(_rolling_min(profile, window), window)


def _flatten_column_profile(column_profile, width):
    """Baseline-subtract the (raw, smoothed) column projection and scale to 0..1 by a high
    percentile. Removes low-frequency background and keeps a bright MW-ladder column from
    dominating the scale, so the lane threshold below is a stable ABSOLUTE 0..1 cut."""
    window = max(5, int(width * LANE_BASELINE_FRAC))
    baseline = _smooth_profile(_rolling_min(column_profile, window), max(3, int(width * 0.02)))
    flat = np.clip(column_profile - baseline, 0.0, None)
    scale = float(np.percentile(flat, LANE_NORM_PCT))
    if scale <= 0:
        return np.zeros_like(flat)
    return np.clip(flat / scale, 0.0, 1.0)


def _split_run_at_valleys(flat, a, b, min_width):
    """Split a [a, b) run of the flattened profile at strong internal valleys, so two
    lanes the threshold merged into one wide run come apart. A boundary is the deepest
    point between two peaks that each clear LANE_SPLIT_DEPTH prominence. Returns >=1 spans."""
    sub = flat[a:b]
    peaks = [p for p, prom in _all_peaks(sub) if prom >= LANE_SPLIT_DEPTH]
    if len(peaks) <= 1:
        return [(a, b)]
    spans, start = [], a
    for left_pk, right_pk in zip(peaks[:-1], peaks[1:]):
        cut = a + left_pk + int(np.argmin(sub[left_pk:right_pk + 1]))
        if cut - start >= min_width:
            spans.append((start, cut))
            start = cut
    if b - start >= min_width:
        spans.append((start, b))
    return spans or [(a, b)]


def _lanes_from_flat(flat, lane_threshold, width):
    """Lanes from the flattened 0..1 column profile: contiguous runs above the (absolute)
    threshold, each wide run further split at internal valleys. Returns [] when nothing
    clears the threshold — the caller must NOT fabricate one full-width lane (that produced
    full-width boxes on hard blots); the lanes-first UX asks the user to place lanes instead."""
    min_width = max(2, int(width * 0.01))
    lanes = []
    for a, b in _bool_runs(flat >= lane_threshold):
        if (b - a) < min_width:
            continue
        lanes.extend(_split_run_at_valleys(flat, int(a), int(b), min_width))
    return lanes


def _detect_lanes(norm, lane_threshold):
    """Auto lane detection on the working image. Returns (flat_profile, lanes): the 0..1
    flattened column projection (also handed to the client for live re-thresholding) and a
    list of (x0, x1) working-grid spans (possibly empty)."""
    width = norm.shape[1]
    flat = _flatten_column_profile(_column_profile(norm), width)
    return flat, _lanes_from_flat(flat, lane_threshold, width)


def _client_lanes_to_working(payload, work_w, composite_w):
    """Convert client-confirmed lane spans (composite-space {x, w}) to working-grid
    (x0, x1) spans. Returns None if no list was sent, [] if the list held nothing valid.
    Malformed/empty entries are dropped; the rest are clamped and sorted."""
    if not isinstance(payload, list):
        return None
    scale = (work_w / composite_w) if composite_w else 1.0
    spans = []
    for item in payload[:MAX_DETECT_LANES]:
        if not isinstance(item, dict):
            continue
        try:
            x = float(item["x"]); w = float(item["w"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (np.isfinite(x) and np.isfinite(w)) or w <= 0:
            continue
        x0 = max(0, min(work_w, int(round(x * scale))))
        x1 = max(0, min(work_w, int(round((x + w) * scale))))
        if x1 - x0 >= 1:
            spans.append((x0, x1))
    spans.sort()
    return spans


def _lanes_to_composite(lanes, scale_x):
    """Working-grid (x0, x1) spans -> composite-space {x, w} for the client to draw/edit."""
    return [{"x": round(x0 * scale_x, 2), "w": round((x1 - x0) * scale_x, 2)}
            for (x0, x1) in lanes]


def _lane_band_profiles(norm, lanes):
    """Per-lane migration-axis profile + peak candidates for the given lane spans. Level-
    INDEPENDENT, so computed once and shared across the sensitivity sets (which differ only
    in the prominence/spacing filter applied later in _candidate_boxes). Returns
    prepared = [(x0, x1, profile, peaks), ...]."""
    window = max(3, int(norm.shape[0] * 0.01))
    bg_window = max(5, int(norm.shape[0] * BAND_BASELINE_FRAC))
    prepared = []
    for (x0, x1) in lanes:
        profile = _smooth_profile(norm[:, x0:x1].mean(axis=1), window)
        # Subtract the per-lane migration-axis background (uneven exposure / smear) so faint
        # bands surface and gradient shoulders don't read as peaks; opening keeps band peaks.
        baseline = _smooth_profile(_morph_open(profile, bg_window), window)
        profile = np.clip(profile - baseline, 0.0, None)
        prepared.append((x0, x1, profile, _all_peaks(profile)))
    return prepared


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
        # Floored at the slider's own minimum: 0.0 puts the entire profile above the cut, so
        # the whole width becomes one run and _split_run_at_valleys fans it out into the
        # maximum number of lanes — the most expensive input the detector accepts, and not
        # one the UI can produce.
        lane_threshold = data.get("laneThreshold")
        if isinstance(lane_threshold, (int, float)) and np.isfinite(lane_threshold):
            lane_threshold = float(min(1.0, max(MIN_LANE_THRESHOLD, lane_threshold)))
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

        # Per-blot straighten. Suggest an angle from the skew estimate (for the "Auto" button)
        # BEFORE applying any client angle, so the suggestion describes the original blot. Then
        # apply the client-owned angle — the same one /render-composite and /extract use — so
        # lanes/bands are found in the straightened frame the boxes will live in.
        #
        # Only on request: _estimate_skew rotates the whole working frame 21 times, and the
        # only caller that reads suggestedRotationDeg is the "Auto" button. Running it on
        # every detect made the common path pay for an answer nothing looked at.
        suggested_rotation = (
            round(_estimate_skew(norm), 2) if data.get("suggestRotation") is True else None
        )
        angle = parse_rotation(data)
        if angle:
            norm = _rotate_array(norm, angle)

        # Lane split. lane_threshold is now an ABSOLUTE 0..1 cut on the flattened profile.
        # Auto-detect always runs (so the client gets a suggestion + the flattened profile
        # for its slider), but client-confirmed lanes win — that is the lanes-first UX:
        # the user fixes the split, then band detection runs strictly within it.
        effective_lane_threshold = (
            lane_threshold if lane_threshold is not None
            else SENSITIVITY_LEVELS[levels[0]]["lane_threshold"]
        )
        flat_profile, auto_lanes = _detect_lanes(norm, effective_lane_threshold)
        client_lanes = _client_lanes_to_working(data.get("lanes"), work_w, composite_w)
        # Cap here, not only inside _client_lanes_to_working: auto_lanes bypasses that helper
        # entirely and is bounded only by min_width, so the default path could carry ~100
        # lanes into the per-lane peak walk.
        selected_lanes = client_lanes if client_lanes else auto_lanes
        lanes = selected_lanes[:MAX_DETECT_LANES]
        # Both lists are ascending in x, so the cap drops the RIGHTMOST lanes. Silently that
        # is actively misleading: lowering the lane threshold to catch one faint lane can
        # push the count past the cap and make the right-hand side of the blot lose its
        # lanes entirely — a knob labelled "more lanes" appearing to delete some. Report it
        # so the client can say so.
        lanes_truncated = len(selected_lanes) > len(lanes)

        lane_profile_out = _downsample_profile(flat_profile, PROFILE_SAMPLES).round(4).tolist()
        composite_lanes = _lanes_to_composite(lanes, scale_x)

        # Lanes-first stage: return the split (composite coords) + the flattened profile so
        # the user can confirm/edit lanes before paying for band detection. No candidates.
        if data.get("stage") == "lanes":
            return jsonify({
                "imageWidth": composite_w,
                "imageHeight": composite_h,
                "channel": channel,
                "rotationDeg": round(angle, 2),
                "suggestedRotationDeg": suggested_rotation,   # for the "Auto" straighten button
                "lanes": composite_lanes,
                "lanesTruncated": lanes_truncated,
                "laneProfile": lane_profile_out,
            })

        # Per-lane profiles + peak candidates are level-independent, so compute once and let
        # each sensitivity set differ only by its prominence/spacing filter.
        prepared = _lane_band_profiles(norm, lanes)
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

        return jsonify({
            "imageWidth": composite_w,       # coordinate space the boxes are in
            "imageHeight": composite_h,
            "channel": channel,
            "rotationDeg": round(angle, 2),  # apply to display + /extract to match
            "suggestedRotationDeg": suggested_rotation,   # for the "Auto" straighten button
            "candidates": candidates,
            "lanes": composite_lanes,        # so the client can show/edit the split used
            "lanesTruncated": lanes_truncated,
            "laneProfile": lane_profile_out,
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
