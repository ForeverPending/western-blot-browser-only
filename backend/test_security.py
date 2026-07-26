import io
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

import numpy as np
import tifffile
from PIL import Image


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["BLOT_TEMP_STORAGE"] = "local"
os.environ["BLOT_TEMP_DIR"] = TEST_DATA.name
os.environ["RATELIMIT_ENABLED"] = "false"
sys.path.insert(0, os.path.dirname(__file__))

import app as backend  # noqa: E402


SESSION_ID = "test-browser-session"


def make_test_zip(blot_name="Session test blot", created_line="#Fri May 15 16:38:05 PDT 2026", metadata=None):
    tif_buffer = io.BytesIO()
    tifffile.imwrite(tif_buffer, np.arange(64, dtype=np.uint16).reshape(8, 8))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Blot A/metadata.txt", metadata or f"Image metadata\n{created_line}\nRemarks={blot_name}")
        zf.writestr("Blot A/image_700.tif", tif_buffer.getvalue())
        zf.writestr("Blot A/image_800.tif", tif_buffer.getvalue())
    archive.seek(0)
    return archive


def make_single_channel_zip(folder="Blot 700", tif_name="image_800.tif"):
    tif_buffer = io.BytesIO()
    tifffile.imwrite(tif_buffer, np.arange(64, dtype=np.uint16).reshape(8, 8))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder}/metadata.txt", "Image metadata\n#Fri May 15 16:38:05 PDT 2026\nRemarks=Single channel")
        zf.writestr(f"{folder}/{tif_name}", tif_buffer.getvalue())
    archive.seek(0)
    return archive


def make_licor_pyramid_tif():
    full_resolution = np.arange(128, dtype=np.float16).reshape(8, 16)
    image = io.BytesIO()
    with tifffile.TiffWriter(image) as tif:
        tif.write(full_resolution, subfiletype=0, description="LI-COR Image Description")
        tif.write(full_resolution[::2, ::2], subfiletype=1, description="LI-COR Image Description")
        tif.write(full_resolution[::4, ::4], subfiletype=1, description="LI-COR Image Description")
    return image.getvalue(), full_resolution


def make_multilane_tif(h=600, w=800, n_lanes=6):
    """Deterministic synthetic bright-bands-on-dark blot: n_lanes columns with a few
    Gaussian bands each. Used to exercise /detect-bands end to end. No random noise, so
    detection results are reproducible across runs."""
    img = np.full((h, w), 300.0)
    yy, xx = np.mgrid[0:h, 0:w]
    for i, cx in enumerate(np.linspace(w * 0.11, w * 0.9, n_lanes)):
        for cy, amp in [(150, 9000), (300, max(400, 4000 - i * 400)), (460, 1500)]:
            img += amp * np.exp(-(((xx - cx) ** 2) / (2 * 26 ** 2) + ((yy - cy) ** 2) / (2 * 16 ** 2)))
    img = np.clip(img, 0, 65535).astype(np.uint16)
    buf = io.BytesIO()
    tifffile.imwrite(buf, img)
    return buf.getvalue(), img


class StatelessSessionBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.app.config.update(TESTING=True)
        cls.client = backend.app.test_client()

    def upload_blot(self):
        response = self.client.post(
            "/upload-zip",
            data={"sessionId": SESSION_ID, "file": (make_test_zip(), "scanner.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["blots"][0]

    def test_zip_upload_returns_session_file_descriptors(self):
        blot = self.upload_blot()
        self.assertEqual(blot["createdAt"], "2026-05-15T23:38:05+00:00")
        self.assertIn("hasJpg", blot)
        self.assertNotIn("has_jpg", blot)
        self.assertIn("files", blot)
        self.assertTrue(blot["files"]["700"]["path"].startswith(f"sessions/{SESSION_ID}/"))
        self.assertTrue(blot["files"]["800"]["path"].startswith(f"sessions/{SESSION_ID}/"))

    def test_zip_metadata_is_parsed_by_field_not_line_position(self):
        response = self.client.post(
            "/upload-zip",
            data={
                "sessionId": SESSION_ID,
                "file": (
                    make_test_zip(metadata="Remarks=Reordered metadata\nImage metadata\n#Fri May 15 16:38:05 PDT 2026"),
                    "scanner.zip",
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        blot = response.get_json()["blots"][0]
        self.assertEqual(blot["name"], "Reordered metadata")
        self.assertEqual(blot["createdAt"], "2026-05-15T23:38:05+00:00")

    def test_frontend_csp_only_allows_localhost_connects_for_local_hosts(self):
        production = self.client.get("/", headers={"Host": "analysis.example"})
        production_csp = production.headers["Content-Security-Policy"]
        production.close()
        self.assertNotIn("http://localhost:*", production_csp)
        self.assertNotIn("http://127.0.0.1:*", production_csp)

        local = self.client.get("/", headers={"Host": "localhost:5001"})
        local_csp = local.headers["Content-Security-Policy"]
        local.close()
        self.assertIn("http://localhost:*", local_csp)
        self.assertIn("http://127.0.0.1:*", local_csp)

    def test_frontend_csp_self_hosts_scripts_and_blocks_inline_styles(self):
        response = self.client.get("/", headers={"Host": "analysis.example"})
        csp = response.headers["Content-Security-Policy"]
        response.close()
        self.assertIn("script-src 'self'", csp)
        self.assertIn("style-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("cdn.sheetjs.com", csp)
        self.assertNotIn("fonts.googleapis.com", csp)

    def test_client_config_reports_effective_direct_upload_limit(self):
        response = self.client.get("/client-config")
        payload = response.get_json()
        self.assertEqual(payload["maxDirectUploadBytes"], min(backend.MAX_REQUEST_BYTES, backend.MAX_ZIP_BYTES))
        self.assertEqual(payload["maxZipUploadBytes"], backend.MAX_ZIP_BYTES)

    def test_cron_cleanup_requires_secret_and_forces_sweep(self):
        unauthorized = self.client.get("/cron-cleanup")
        self.assertEqual(unauthorized.status_code, 401)

        with mock.patch.dict(os.environ, {"CRON_SECRET": "test-cron-secret-value"}), mock.patch.object(
            backend, "sweep_expired_temp_files", return_value=3
        ) as sweep:
            response = self.client.get(
                "/cron-cleanup",
                headers={"Authorization": "Bearer test-cron-secret-value"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["removed"], 3)
        sweep.assert_called_once_with(max_deletes=backend.TEMP_SWEEP_MAX_DELETES, force=True)

    def test_sweep_reclaims_abandoned_uploads_not_just_sessions(self):
        # The browser PUTs a ZIP straight to storage and only THEN calls /process-upload, so
        # a closed tab leaves the object with nothing to delete it: the sweep used to list
        # only sessions/, and no frontend caller sends /cleanup an `upload`. Those are the
        # largest objects the app writes, so the leak was unbounded and permanent.
        with tempfile.TemporaryDirectory() as isolated:
            upload_dir = os.path.join(isolated, "uploads", "abandoned-session")
            session_dir = os.path.join(isolated, "sessions", "abandoned-session")
            os.makedirs(upload_dir)
            os.makedirs(session_dir)
            for path in (os.path.join(upload_dir, "x.zip"), os.path.join(session_dir, "700.tif")):
                with open(path, "wb") as handle:
                    handle.write(b"stale")
            with mock.patch.object(backend, "LOCAL_TEMP_DIR", isolated):
                removed = backend.sweep_expired_local_files(
                    cutoff_epoch=time.time() + 60,          # everything is expired
                    upload_cutoff_epoch=time.time() + 60,
                    exclude_session_id=None,
                    max_deletes=50,
                )
            self.assertEqual(removed, 2, "both roots must be swept")
            self.assertFalse(os.path.isdir(upload_dir), "abandoned upload was left behind")
            self.assertFalse(os.path.isdir(session_dir))

    def test_uploads_expire_sooner_than_sessions(self):
        # A session's files live as long as the tab might return to them; an uploaded ZIP is
        # consumed inside one request, so a fresh-ish upload is already abandoned while a
        # session of the same age is not.
        with tempfile.TemporaryDirectory() as isolated:
            upload_dir = os.path.join(isolated, "uploads", "s")
            session_dir = os.path.join(isolated, "sessions", "s")
            os.makedirs(upload_dir)
            os.makedirs(session_dir)
            for path in (os.path.join(upload_dir, "x.zip"), os.path.join(session_dir, "700.tif")):
                with open(path, "wb") as handle:
                    handle.write(b"stale")
            with mock.patch.object(backend, "LOCAL_TEMP_DIR", isolated):
                removed = backend.sweep_expired_local_files(
                    cutoff_epoch=time.time() - 3600,        # session not yet expired
                    upload_cutoff_epoch=time.time() + 60,   # upload is
                    exclude_session_id=None,
                    max_deletes=50,
                )
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.isdir(upload_dir))
            self.assertTrue(os.path.isdir(session_dir), "session TTL must not be shortened")

    def test_uploads_are_swept_before_sessions_so_they_cannot_be_starved(self):
        # Both roots share one max_deletes budget. Sweeping sessions/ first meant a store
        # expiring more than the cap in session objects — about a busy week of uploads — used
        # up the budget before uploads/ was ever listed, so the abandoned-ZIP leak this sweep
        # was added to close would never actually be closed.
        with tempfile.TemporaryDirectory() as isolated:
            for index in range(5):
                os.makedirs(os.path.join(isolated, "sessions", f"s{index}"))
                with open(os.path.join(isolated, "sessions", f"s{index}", "700.tif"), "wb") as handle:
                    handle.write(b"x")
            os.makedirs(os.path.join(isolated, "uploads", "abandoned"))
            with open(os.path.join(isolated, "uploads", "abandoned", "x.zip"), "wb") as handle:
                handle.write(b"x")
            with mock.patch.object(backend, "LOCAL_TEMP_DIR", isolated):
                backend.sweep_expired_local_files(
                    cutoff_epoch=time.time() + 60,
                    upload_cutoff_epoch=time.time() + 60,
                    exclude_session_id=None,
                    max_deletes=2,      # smaller than the sessions/ backlog
                )
            self.assertFalse(
                os.path.isdir(os.path.join(isolated, "uploads", "abandoned")),
                "uploads/ must not be starved by a sessions/ backlog",
            )

    def test_a_zero_upload_ttl_disables_it_rather_than_expiring_everything(self):
        # max_age_seconds <= 0 already means "TTL off", so 0 here reads as "no separate
        # uploads TTL". Unguarded it inverted into the most destructive setting available:
        # min(..., 0) put the cutoff at now, expiring every upload including one being
        # streamed right then by another session's /process-upload.
        with tempfile.TemporaryDirectory() as isolated:
            in_flight = os.path.join(isolated, "uploads", "streaming-now")
            os.makedirs(in_flight)
            with open(os.path.join(in_flight, "x.zip"), "wb") as handle:
                handle.write(b"x")
            with mock.patch.object(backend, "LOCAL_TEMP_DIR", isolated), \
                 mock.patch.object(backend, "UPLOAD_FILE_TTL_SECONDS", 0), \
                 mock.patch.object(backend, "USE_VERCEL_BLOB", False):
                backend.sweep_expired_temp_files(max_age_seconds=86400, max_deletes=50, force=True)
            self.assertTrue(os.path.isdir(in_flight),
                            "a 0 upload TTL must disable it, not expire everything")

    def test_expired_session_blobs_selects_both_roots(self):
        page = [
            {"pathname": f"uploads/{SESSION_ID}/abandoned.zip", "url": "https://x/u",
             "uploadedAt": "2020-01-01T00:00:00Z"},
            {"pathname": f"sessions/{SESSION_ID}/blot/700.tif", "url": "https://x/s",
             "uploadedAt": "2020-01-01T00:00:00Z"},
            {"pathname": "nonsense/whatever", "url": "https://x/n",
             "uploadedAt": "2020-01-01T00:00:00Z"},
        ]
        selected = backend.expired_session_blobs(page, cutoff_epoch=time.time())
        self.assertEqual({entry["pathname"] for entry in selected},
                         {f"uploads/{SESSION_ID}/abandoned.zip", f"sessions/{SESSION_ID}/blot/700.tif"})

    def test_cron_cleanup_reports_a_failed_sweep_as_an_error(self):
        # Vercel Cron only alerts on non-2xx, so {"status":"ok","removed":0} for a broken
        # sweep was indistinguishable from a healthy quiet day — objects could pile up
        # forever with no signal at all.
        with mock.patch.dict(os.environ, {"CRON_SECRET": "test-cron-secret-value"}), \
             mock.patch.object(backend, "sweep_expired_temp_files", return_value=None):
            response = self.client.get(
                "/cron-cleanup", headers={"Authorization": "Bearer test-cron-secret-value"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["status"], "error")

    def test_blob_redirect_never_carries_the_token_off_host(self):
        # urlopen's default handler copies every header except content-length/content-type
        # onto a redirect target, up to 10 hops, unvalidated — so the blob token (read/write/
        # delete over the whole store) would follow a 3xx wherever the first host pointed it.
        handler = backend.BlobRedirectHandler()
        original = backend.Request(
            "https://store.public.blob.vercel-storage.com/sessions/s/b/700.tif",
            headers={"Authorization": "Bearer super-secret", "X-Api-Version": "7"},
        )

        # Off-allowlist target: refused outright rather than followed.
        with self.assertRaises(backend.PublicError):
            handler.redirect_request(
                original, None, 302, "Found", {}, "https://attacker.example/steal",
            )

        # Allowed suffix but a DIFFERENT store: followed, but stripped of credentials.
        moved = handler.redirect_request(
            original, None, 302, "Found", {},
            "https://someone-else.public.blob.vercel-storage.com/sessions/s/b/700.tif",
        )
        header_names = {name.lower() for name in moved.headers}
        self.assertNotIn("authorization", header_names)
        self.assertIn("x-api-version", header_names, "only credentials should be dropped")

    def test_redirects_on_the_api_host_are_not_treated_as_off_host(self):
        # Half of all blob traffic starts at vercel.com/api/blob (put, list, delete), whose
        # host is deliberately NOT in the object allowlist. Checking only that suffix turned
        # an ordinary same-host canonicalising redirect there into a hard 502 — including on
        # the sweep's own list call, which would then fail every cron run.
        handler = backend.BlobRedirectHandler()
        api = backend.Request(f"{backend.BLOB_API_BASE_URL}?limit=1000",
                              headers={"Authorization": "Bearer secret"})
        moved = handler.redirect_request(
            api, None, 302, "Found", {}, f"{backend.BLOB_API_BASE_URL}/?limit=1000",
        )
        self.assertIsNotNone(moved)
        self.assertTrue(any(v == "Bearer secret" for v in moved.headers.values()),
                        "a same-host API hop should keep its credentials")
        # ...but the API host must not become a bridge to an object host, or vice versa.
        with self.assertRaises(backend.PublicError):
            handler.redirect_request(api, None, 302, "Found", {},
                                     "https://store.public.blob.vercel-storage.com/sessions/s/b/700.tif")
        obj = backend.Request("https://store.public.blob.vercel-storage.com/sessions/s/b/700.tif")
        with self.assertRaises(backend.PublicError):
            handler.redirect_request(obj, None, 302, "Found", {}, backend.BLOB_API_BASE_URL)

    def test_a_pin_rejection_is_not_swallowed_as_a_skipped_descriptor(self):
        # vercel_blob_delete skips malformed descriptors on purpose. A pin mismatch is a
        # CONFIG problem, and swallowing it the same way meant every URL got skipped, 0 was
        # returned (not None), and /cron-cleanup reported a healthy "removed: 0" forever
        # while nothing was ever reclaimed — the exact blindness the None/500 return killed.
        foreign = [({"url": "https://not-ours.public.blob.vercel-storage.com/sessions/s/b/700.tif"},
                    "sessions/s/b/700.tif")]
        with mock.patch.object(backend, "vercel_blob_store_id", return_value="ourstore"), \
             mock.patch.object(backend, "BLOB_HOST_PIN", "enforce"):
            with self.assertRaises(backend.BlobHostPinError):
                backend.vercel_blob_delete(foreign)
        # A genuinely malformed descriptor is still skipped rather than raised.
        with mock.patch.object(backend, "BLOB_HOST_PIN", "off"):
            self.assertEqual(backend.vercel_blob_delete([({"url": "not-a-url"}, "sessions/s/b/700.tif")]), 0)

    def test_expired_session_blobs_can_be_confined_to_one_root(self):
        # The caller applies a DIFFERENT cutoff per root (uploads 1h, sessions 24h), so
        # without this filter both rest entirely on the remote listing honouring `prefix`.
        # Drop or mistype that prefix and the 1h upload cutoff hits sessions/, deleting every
        # other session's live blot files.
        page = [
            {"pathname": f"uploads/{SESSION_ID}/a.zip", "url": "https://x/u", "uploadedAt": "2020-01-01T00:00:00Z"},
            {"pathname": f"sessions/{SESSION_ID}/b/700.tif", "url": "https://x/s", "uploadedAt": "2020-01-01T00:00:00Z"},
        ]
        uploads_only = backend.expired_session_blobs(page, time.time(), root="uploads")
        self.assertEqual([entry["pathname"] for entry in uploads_only], [f"uploads/{SESSION_ID}/a.zip"])
        sessions_only = backend.expired_session_blobs(page, time.time(), root="sessions")
        self.assertEqual([entry["pathname"] for entry in sessions_only], [f"sessions/{SESSION_ID}/b/700.tif"])

    def test_a_failing_list_page_keeps_the_earlier_root_deletions(self):
        # to_delete used to accumulate across every page of BOTH roots and only delete at the
        # end, so one 429 on a later list call discarded everything already collected: zero
        # objects reclaimed for the whole run. Two prefixes doubled the list calls, so it
        # doubled the chance of hitting that.
        deleted_batches = []

        def fake_list(prefix=None, cursor=None, limit=1000):
            if prefix == "uploads/":
                return {"blobs": [{"pathname": f"uploads/{SESSION_ID}/a.zip",
                                   "url": "https://x/u", "uploadedAt": "2020-01-01T00:00:00Z"}],
                        "hasMore": False}
            raise backend.PublicError("Temporary Blob storage request failed.", 502)

        def fake_delete(batch):
            deleted_batches.append([path for _descriptor, path in batch])
            return len(batch)

        with mock.patch.object(backend, "vercel_blob_list", side_effect=fake_list), \
             mock.patch.object(backend, "vercel_blob_delete", side_effect=fake_delete):
            with self.assertRaises(backend.PublicError):
                backend.sweep_expired_vercel_blobs(time.time(), time.time(), None, 500)
        self.assertEqual(deleted_batches, [[f"uploads/{SESSION_ID}/a.zip"]],
                         "the uploads batch must already be deleted when sessions/ fails")

    def test_an_unknown_blob_host_pin_value_is_rejected_at_import(self):
        # "enforced" instead of "enforce" used to degrade silently to log-only, so an operator
        # would believe the pin was on while it was merely observing. Checked in a subprocess:
        # importlib.reload would rebuild the Flask app and temp-dir config this suite's client
        # is bound to, breaking every later test.
        def probe(value):
            env = {**os.environ, "BLOB_HOST_PIN": value,
                   "BLOT_TEMP_STORAGE": "local", "RATELIMIT_ENABLED": "false"}
            return subprocess.run(
                [sys.executable, "-c", "import app"],
                cwd=os.path.dirname(__file__), env=env, capture_output=True, text=True,
            )
        bad = probe("enforced")
        self.assertNotEqual(bad.returncode, 0, "a typo'd pin mode must not import cleanly")
        self.assertIn("BLOB_HOST_PIN must be", bad.stderr)
        for good in ("log", "enforce", "off"):
            self.assertEqual(probe(good).returncode, 0, f"{good} should be accepted")

    def test_blob_host_pin_logs_by_default_and_can_enforce(self):
        # Descriptor URLs are host-checked only against *.blob.vercel-storage.com, so one can
        # name a store we do not own — and /channel-pixels returns the decoded intensity
        # array, which makes that a read proxy rather than just wasted I/O. Ships log-only so
        # it can be proven silent on real traffic before it rejects anything.
        foreign = "https://not-our-store.public.blob.vercel-storage.com/sessions/s/b/700.tif"
        with mock.patch.object(backend, "vercel_blob_store_id", return_value="ourstore"):
            with mock.patch.object(backend, "BLOB_HOST_PIN", "log"):
                self.assertEqual(backend.validate_vercel_blob_url(foreign), foreign)
            with mock.patch.object(backend, "BLOB_HOST_PIN", "enforce"):
                with self.assertRaises(backend.PublicError):
                    backend.validate_vercel_blob_url(foreign)
                own = "https://ourstore.public.blob.vercel-storage.com/sessions/s/b/700.tif"
                self.assertEqual(backend.validate_vercel_blob_url(own), own)

    def test_zip_validation_preserves_seekable_stream_position(self):
        archive = make_test_zip()
        archive.seek(7)
        self.assertTrue(backend.is_valid_zip(archive))
        self.assertEqual(archive.tell(), 7)

    def test_local_temp_object_can_stream_to_seekable_file(self):
        path = f"uploads/{SESSION_ID}/stream-test.zip"
        source_path = backend.local_temp_file_path(path)
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        archive_bytes = make_test_zip().getvalue()
        with open(source_path, "wb") as source:
            source.write(archive_bytes)
        try:
            output = io.BytesIO()
            copied = backend.copy_temp_file_to_handle(
                {"path": path},
                output,
                SESSION_ID,
                allow_uploads=True,
                max_bytes=len(archive_bytes),
            )
            self.assertEqual(copied, len(archive_bytes))
            self.assertEqual(output.getvalue(), archive_bytes)
        finally:
            os.remove(source_path)

    def test_stored_zip_processing_streams_and_deletes_upload(self):
        path = f"uploads/{SESSION_ID}/stored-stream-test.zip"
        source_path = backend.local_temp_file_path(path)
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        with open(source_path, "wb") as source:
            source.write(make_test_zip().getvalue())

        response = self.client.post(
            "/process-upload",
            json={"sessionId": SESSION_ID, "upload": {"path": path}},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(response.get_json()["blots"]), 1)
        self.assertFalse(os.path.exists(source_path))
        cleanup = self.client.post(
            "/cleanup",
            json={"sessionId": SESSION_ID, "blots": response.get_json()["blots"]},
        )
        self.assertEqual(cleanup.status_code, 200)

    def test_renders_composite_and_extracts_signals_from_descriptor(self):
        blot = self.upload_blot()

        composite = self.client.post(
            "/render-composite",
            json={
                "sessionId": SESSION_ID,
                "blot": blot,
                "displayMin700": 0,
                "displayMax700": 500,
                "displayMin800": 0,
                "displayMax800": 500,
                "colorMode": "color",
            },
        )
        self.assertEqual(composite.status_code, 200, composite.get_json())
        self.assertEqual(composite.mimetype, "image/png")

        invalid_box = self.client.post(
            "/extract",
            json={
                "sessionId": SESSION_ID,
                "blot": blot,
                "channel": "700",
                "backgroundAxis": "leftright",
                "boxes": [{"x": "invalid", "y": 0, "w": 2, "h": 2}],
            },
        )
        self.assertEqual(invalid_box.status_code, 400)

        extraction = self.client.post(
            "/extract",
            json={
                "sessionId": SESSION_ID,
                "blot": blot,
                "channel": "700",
                "backgroundAxis": "leftright",
                "boxes": [{"x": 0, "y": 0, "w": 2, "h": 2}],
            },
        )
        self.assertEqual(extraction.status_code, 200, extraction.get_json())
        signal = extraction.get_json()["results"][0]
        self.assertIn("rawSignal", signal)
        self.assertIn("backgroundSignal", signal)
        self.assertIn("adjustedSignal", signal)
        self.assertNotIn("raw_signal", signal)

    def store_multilane_blot(self, blot_id="detect-blot"):
        tif_bytes, _img = make_multilane_tif()
        descriptor = backend.store_temp_file(SESSION_ID, blot_id, "700.tif", tif_bytes, "image/tiff")
        return {"id": blot_id, "files": {"700": descriptor}}

    def test_detect_bands_returns_composite_space_candidates(self):
        blot = self.store_multilane_blot()
        response = self.client.post(
            "/detect-bands",
            json={
                "sessionId": SESSION_ID,
                "blot": blot,
                "channel": "700",
                "compositeWidth": 800,
                "compositeHeight": 600,
                "sensitivities": ["conservative", "balanced", "aggressive"],
            },
            headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual(body["imageWidth"], 800)
        self.assertEqual(body["imageHeight"], 600)
        self.assertEqual(body["channel"], "700")
        # No straighten angle was sent, so boxes stay in the unrotated composite frame that
        # /extract consumes unchanged (rotationDeg defaults to 0).
        self.assertEqual(body["rotationDeg"], 0.0)
        self.assertEqual([c["id"] for c in body["candidates"]], ["conservative", "balanced", "aggressive"])
        self.assertIsInstance(body["laneProfile"], list)

        balanced = next(c for c in body["candidates"] if c["id"] == "balanced")
        self.assertGreaterEqual(balanced["laneCount"], 2)   # the six lanes must not merge
        self.assertGreaterEqual(balanced["bandCount"], 2)
        self.assertEqual(balanced["bandCount"], len(balanced["boxes"]))
        for box in balanced["boxes"]:
            self.assertGreater(box["w"], 0)
            self.assertGreater(box["h"], 0)
            self.assertGreaterEqual(box["x"], 0)
            self.assertGreaterEqual(box["y"], 0)
            self.assertLessEqual(box["x"] + box["w"], body["imageWidth"] + 1)
            self.assertLessEqual(box["y"] + box["h"], body["imageHeight"] + 1)

    def test_detect_bands_rejects_invalid_channel(self):
        blot = self.store_multilane_blot("detect-blot-badchan")
        response = self.client.post(
            "/detect-bands",
            json={"sessionId": SESSION_ID, "blot": blot, "channel": "999"},
            headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertEqual(response.status_code, 400)

    def test_detect_bands_enforces_session_scoping(self):
        blot = self.store_multilane_blot("detect-blot-session")
        response = self.client.post(
            "/detect-bands",
            json={"sessionId": "someone-else", "blot": blot, "channel": "700"},
            headers={"X-Blot-Session": "someone-else"},
        )
        self.assertEqual(response.status_code, 403)

    def test_detect_bands_lanes_stage_returns_lanes_only(self):
        blot = self.store_multilane_blot("detect-blot-lanes")
        response = self.client.post(
            "/detect-bands",
            json={
                "sessionId": SESSION_ID, "blot": blot, "channel": "700",
                "compositeWidth": 800, "compositeHeight": 600, "stage": "lanes",
            },
            headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        # The lanes-first stage returns the split + a suggested straighten angle, no bands.
        self.assertNotIn("candidates", body)
        self.assertIn("suggestedRotationDeg", body)
        self.assertIsInstance(body["lanes"], list)
        self.assertGreaterEqual(len(body["lanes"]), 2)   # the six lanes must not all merge
        for lane in body["lanes"]:
            self.assertGreater(lane["w"], 0)
            self.assertGreaterEqual(lane["x"], 0)
            self.assertLessEqual(lane["x"] + lane["w"], body["imageWidth"] + 1)

    def test_detect_bands_honors_client_lanes(self):
        blot = self.store_multilane_blot("detect-blot-client-lanes")
        response = self.client.post(
            "/detect-bands",
            json={
                "sessionId": SESSION_ID, "blot": blot, "channel": "700",
                "compositeWidth": 800, "compositeHeight": 600,
                "sensitivities": ["balanced"],
                # Two user-confirmed lanes must be used verbatim (not auto-detected).
                "lanes": [{"x": 120, "w": 90}, {"x": 430, "w": 90}],
            },
            headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual(body["candidates"][0]["laneCount"], 2)
        self.assertEqual(len(body["lanes"]), 2)

    def test_detect_bands_no_full_width_fallback_on_blank(self):
        # A blank blot must yield NO lanes, never one full-width lane (the old catastrophe
        # that produced full-image-width boxes).
        blank = np.zeros((300, 400), dtype=np.uint16)
        buf = io.BytesIO()
        tifffile.imwrite(buf, blank)
        descriptor = backend.store_temp_file(SESSION_ID, "detect-blank", "700.tif", buf.getvalue(), "image/tiff")
        blot = {"id": "detect-blank", "files": {"700": descriptor}}
        response = self.client.post(
            "/detect-bands",
            json={"sessionId": SESSION_ID, "blot": blot, "channel": "700", "stage": "lanes"},
            headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["lanes"], [])

    def test_detect_bands_caps_auto_detected_lanes(self):
        # MAX_DETECT_LANES was only applied to client-supplied lanes, so the DEFAULT path —
        # what an unauthenticated request gets — could carry ~100 lanes into the per-lane
        # peak walk. Squeeze the cap and confirm auto-detection now respects it too.
        blot = self.store_multilane_blot("detect-blot-lanecap")
        with mock.patch.object(backend, "MAX_DETECT_LANES", 2):
            response = self.client.post(
                "/detect-bands",
                json={"sessionId": SESSION_ID, "blot": blot, "channel": "700", "stage": "lanes"},
                headers={"X-Blot-Session": SESSION_ID},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertLessEqual(len(payload["lanes"]), 2)
        # Both lane lists run left to right, so the cap drops the RIGHTMOST lanes. Silently
        # that looks like the right of the blot having no lanes — and lowering the threshold
        # to find MORE lanes is exactly what triggers it. The client has to be able to say so.
        self.assertTrue(payload["lanesTruncated"])

    def test_detect_bands_does_not_claim_truncation_when_under_the_cap(self):
        blot = self.store_multilane_blot("detect-blot-nocap")
        response = self.client.post(
            "/detect-bands",
            json={"sessionId": SESSION_ID, "blot": blot, "channel": "700", "stage": "lanes"},
            headers={"X-Blot-Session": SESSION_ID},
        )
        payload = response.get_json()
        self.assertLess(len(payload["lanes"]), backend.MAX_DETECT_LANES)
        self.assertFalse(payload["lanesTruncated"])

    def test_detect_bands_floors_the_lane_threshold(self):
        # laneThreshold 0 puts the whole profile above the cut, making the entire width one
        # run and fanning it out into the maximum number of lanes — the most expensive input
        # the detector takes, and one the UI slider (min 0.02) cannot produce.
        blot = self.store_multilane_blot("detect-blot-thresh")
        floored = self.client.post(
            "/detect-bands",
            json={
                "sessionId": SESSION_ID, "blot": blot, "channel": "700",
                "stage": "lanes", "laneThreshold": 0,
            },
            headers={"X-Blot-Session": SESSION_ID},
        )
        at_minimum = self.client.post(
            "/detect-bands",
            json={
                "sessionId": SESSION_ID, "blot": blot, "channel": "700",
                "stage": "lanes", "laneThreshold": backend.MIN_LANE_THRESHOLD,
            },
            headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertEqual(floored.status_code, 200, floored.get_json())
        # 0 is treated as the floor, so it cannot buy a bigger fan-out than the slider can.
        self.assertEqual(floored.get_json()["lanes"], at_minimum.get_json()["lanes"])

    def test_detect_bands_skips_the_skew_estimate_unless_asked(self):
        # _estimate_skew rotates the whole working frame 21 times and only the "Auto"
        # straighten button reads the result, so the common path must not pay for it.
        blot = self.store_multilane_blot("detect-blot-skew")
        body = {"sessionId": SESSION_ID, "blot": blot, "channel": "700", "stage": "lanes"}
        default = self.client.post(
            "/detect-bands", json=body, headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertIsNone(default.get_json()["suggestedRotationDeg"])

        requested = self.client.post(
            "/detect-bands",
            json={**body, "suggestRotation": True},
            headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertIsInstance(requested.get_json()["suggestedRotationDeg"], (int, float))

    def test_detect_bands_clamps_rotation(self):
        blot = self.store_multilane_blot("detect-blot-rot")
        response = self.client.post(
            "/detect-bands",
            json={"sessionId": SESSION_ID, "blot": blot, "channel": "700", "rotationDeg": 999},
            headers={"X-Blot-Session": SESSION_ID},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["rotationDeg"], backend.MAX_STRAIGHTEN_DEG)

    def test_extract_and_composite_accept_rotation(self):
        # Straighten must not break the measurement/display endpoints; a clamped angle rotates
        # the native pixels (extract) and the composite (render) without erroring.
        blot = self.upload_blot()
        extraction = self.client.post(
            "/extract",
            json={
                "sessionId": SESSION_ID, "blot": blot, "channel": "700",
                "backgroundAxis": "leftright", "rotationDeg": 5,
                "boxes": [{"x": 1, "y": 1, "w": 3, "h": 3}],
            },
        )
        self.assertEqual(extraction.status_code, 200, extraction.get_json())
        self.assertIn("rawSignal", extraction.get_json()["results"][0])

        composite = self.client.post(
            "/render-composite",
            json={
                "sessionId": SESSION_ID, "blot": blot,
                "gamma700": 0.5, "gamma800": 1.0,
                "colorMode": "color", "rotationDeg": 999,
            },
        )
        self.assertEqual(composite.status_code, 200, composite.get_json())
        self.assertEqual(composite.mimetype, "image/png")

    def test_channel_stats_reports_raw_counts_and_auto_window(self):
        blot = self.upload_blot()
        response = self.client.post(
            "/channel-stats", json={"sessionId": SESSION_ID, "blot": blot},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        channels = response.get_json()["channels"]
        for channel in ("700", "800"):
            stats = channels[channel]
            # The sliders bound themselves to [min, max] and start at [autoMin, autoMax],
            # so an inverted or degenerate window here would make them unusable.
            self.assertLess(stats["min"], stats["max"])
            self.assertLess(stats["autoMin"], stats["autoMax"])
            self.assertGreaterEqual(stats["autoMin"], stats["min"])
            self.assertLessEqual(stats["autoMax"], stats["max"])
            self.assertGreater(stats["width"], 0)
            self.assertGreater(stats["height"], 0)

    def test_display_window_white_point_preserves_band_core_detail(self):
        # The regression this whole control exists for: the old fixed p1/p99 stretch clipped
        # every band core to one value before any slider could reach it. A tiny bright core
        # on a dim field reproduces that — bands are far under 1% of a blot's pixels.
        field = np.full((60, 60), 20, dtype=np.uint16)
        field[28:32, 28:32] = np.array(
            [[300, 900, 2600, 400]] * 4, dtype=np.uint16
        )  # a graded core spanning ~9x
        buf = io.BytesIO(); tifffile.imwrite(buf, field)
        descriptor = backend.store_temp_file(
            SESSION_ID, "window-blot", "700.tif", buf.getvalue(), "image/tiff"
        )
        blot = {"id": "window-blot", "files": {"700": descriptor, "800": descriptor}}

        def core_levels(payload):
            response = self.client.post(
                "/render-composite", json={"sessionId": SESSION_ID, "blot": blot, **payload},
            )
            self.assertEqual(response.status_code, 200, response.data[:200])
            rendered = np.asarray(Image.open(io.BytesIO(response.data)).convert("RGB"))
            return len(np.unique(rendered[28:32, 28:32, 0]))

        # A white point below the core flattens it; lifting the white point above the core
        # keeps its gradation. The old pipeline could only ever produce the first case.
        self.assertEqual(core_levels({"displayMin700": 0, "displayMax700": 250}), 1)
        self.assertGreater(core_levels({"displayMin700": 0, "displayMax700": 2600}), 2)

    def test_display_window_rejects_unusable_values_without_erroring(self):
        # The window is a view preference, so a stale or malformed pair must fall back to the
        # automatic window rather than 500 or render a NaN-filled frame.
        blot = self.upload_blot()
        for payload in (
            {"displayMin700": 500, "displayMax700": 10},           # inverted
            {"displayMin700": float("nan"), "displayMax700": 10},  # not finite
            {"displayMin700": "0", "displayMax700": "500"},        # wrong type
            {"gamma700": 10_000},                                  # out of range
            {"displayMin700": 5, "displayMax700": 5},              # zero span
        ):
            response = self.client.post(
                "/render-composite",
                json={"sessionId": SESSION_ID, "blot": blot, "colorMode": "color", **payload},
            )
            self.assertEqual(response.status_code, 200, f"{payload}: {response.data[:200]}")
            rendered = np.asarray(Image.open(io.BytesIO(response.data)).convert("RGB"))
            self.assertGreater(len(np.unique(rendered)), 1, f"{payload} rendered a blank frame")

    def test_channel_pixels_round_trips_to_the_server_arrays(self):
        blot = self.upload_blot()
        response = self.client.post(
            "/channel-pixels", json={"sessionId": SESSION_ID, "blot": blot},
        )
        self.assertEqual(response.status_code, 200, response.data[:200])
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        body = response.data
        self.assertEqual(body[:4], b"WBP1")
        width, height, channels = struct.unpack_from("<III", body, 4)
        self.assertEqual(channels, 2)
        scales = struct.unpack_from("<ffff", body, 16)
        self.assertEqual(len(body), 32 + width * height * 2 * 2)

        # The client decodes with pow(step/65535, PIXEL_QUANTIZE_CURVE); if the two curves
        # ever disagree the live preview would be visibly wrong while the server render is
        # right, so pin the round trip against the arrays the renderer actually uses.
        raw = np.frombuffer(body, dtype="<u2", offset=32).reshape(2, height, width)
        with backend.app.test_request_context():
            tif_700 = backend.read_temp_file(
                blot["files"]["700"], SESSION_ID, max_bytes=backend.BLOT_FILE_LIMITS["700"]
            )
            tif_800 = backend.read_temp_file(
                blot["files"]["800"], SESSION_ID, max_bytes=backend.BLOT_FILE_LIMITS["800"]
            )
        expected = backend.prepare_composite_channels(tif_700, tif_800, 0.0)[:2]
        for index, truth in enumerate(expected):
            low, high = scales[2 * index], scales[2 * index + 1]
            decoded = low + (high - low) * np.power(
                raw[index] / 65535.0, backend.PIXEL_QUANTIZE_CURVE
            )
            span = float(truth.max() - truth.min()) or 1.0
            self.assertLess(float(np.abs(decoded - truth).max()) / span, 1e-4)

    def test_blot_fingerprint_identifies_identical_content(self):
        # Blot ids are regenerated per upload, so the client needs a content hash to notice
        # it is being handed the same scan twice.
        first = self.upload_blot()
        second = self.upload_blot()
        self.assertTrue(first["fingerprint"])
        self.assertNotEqual(first["id"], second["id"], "ids are per-upload")
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_backfilled_fingerprint_matches_the_upload_fingerprint(self):
        # The whole point of /blot-fingerprints is to recognise a re-upload of a blot that
        # was stored before fingerprints existed. If the two digests differ by so much as a
        # byte ordering, the backfill is worse than useless — it would report a fingerprint
        # that never matches anything.
        blot = self.upload_blot()
        response = self.client.post(
            "/blot-fingerprints", json={"sessionId": SESSION_ID, "blots": [blot]},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            response.get_json()["fingerprints"][blot["id"]], blot["fingerprint"]
        )

    def test_backfilled_fingerprint_matches_for_a_single_channel_blot(self):
        upload = self.client.post(
            "/upload-zip",
            data={"sessionId": SESSION_ID, "file": (make_single_channel_zip(), "single.zip")},
            content_type="multipart/form-data",
        )
        blot = upload.get_json()["blots"][0]
        response = self.client.post(
            "/blot-fingerprints", json={"sessionId": SESSION_ID, "blots": [blot]},
        )
        self.assertEqual(
            response.get_json()["fingerprints"][blot["id"]], blot["fingerprint"]
        )

    def test_blot_fingerprints_is_session_scoped_and_bounded(self):
        blot = self.upload_blot()
        other_session = self.client.post(
            "/blot-fingerprints", json={"sessionId": "someone-elses-session", "blots": [blot]},
        )
        # Unreadable in another session: reported as absent rather than leaking a hash of
        # content that session cannot see.
        self.assertEqual(other_session.status_code, 200)
        self.assertEqual(other_session.get_json()["fingerprints"], {})

        too_many = self.client.post(
            "/blot-fingerprints",
            json={"sessionId": SESSION_ID, "blots": [blot] * (backend.MAX_FINGERPRINT_BLOTS + 1)},
        )
        self.assertEqual(too_many.status_code, 400)

        not_a_list = self.client.post(
            "/blot-fingerprints", json={"sessionId": SESSION_ID, "blots": "nope"},
        )
        self.assertEqual(not_a_list.status_code, 400)

    def test_blot_fingerprints_stops_at_the_byte_budget(self):
        # The blot cap bounds COUNT, not bytes: at two 100 MB TIFs each, 50 blots meant one
        # request could re-read 10 GB out of storage. Squeeze the budget so a second blot
        # cannot fit and check the request stops rather than reading everything asked for.
        blot = self.upload_blot()
        with mock.patch.object(backend, "MAX_FINGERPRINT_BYTES", 1):
            response = self.client.post(
                "/blot-fingerprints",
                json={"sessionId": SESSION_ID, "blots": [blot, blot, blot]},
            )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        # One blot's worth of reads is allowed to overshoot — the budget is checked before a
        # blot, not mid-file, because hashing a partially read file would give a wrong digest.
        self.assertEqual(payload["fingerprints"], {blot["id"]: blot["fingerprint"]})
        self.assertTrue(payload["truncated"], "the client needs to know the tail was skipped")

    def test_a_blot_with_no_tif_channel_gets_no_fingerprint(self):
        # SHA-256 of nothing is e3b0c442...b855 — a NON-EMPTY string, so it passed the
        # client's `if (!fingerprint)` sentinel. Every channel-less blot after the first was
        # then judged a duplicate of the first, dropped, and had its temp files DELETED,
        # reported as "same image data as X" with nothing having been compared.
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for folder in ("Blot A", "Blot B"):
                zf.writestr(f"{folder}/metadata.txt",
                            f"Image metadata\n#Fri May 15 16:38:05 PDT 2026\nRemarks={folder}")
        archive.seek(0)
        response = self.client.post(
            "/upload-zip",
            data={"sessionId": SESSION_ID, "file": (archive, "no-channels.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        blots = response.get_json()["blots"]
        self.assertEqual(len(blots), 2, "both channel-less blots must survive")
        empty_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        for blot in blots:
            self.assertEqual(blot["fingerprint"], "")
            self.assertNotEqual(blot["fingerprint"], empty_sha)

    def test_a_real_blot_still_gets_a_fingerprint(self):
        self.assertTrue(self.upload_blot()["fingerprint"])

    def test_blot_fingerprints_charges_the_budget_for_failed_reads(self):
        # A read that fails with 413 has already pulled max_bytes+1 off the wire. If the
        # budget is only decremented on SUCCESS, oversize objects are free and the whole
        # amplification is back: 100 reads x 100 MB in one request, budget untouched.
        # 150 MB of budget must survive one 100 MB failure and not a second.
        blot = self.upload_blot()
        with mock.patch.object(backend, "MAX_FINGERPRINT_BYTES", 150 * 1024 * 1024), \
             mock.patch.object(backend, "read_temp_file", side_effect=backend.PublicError("too big", 413)):
            response = self.client.post(
                "/blot-fingerprints",
                json={"sessionId": SESSION_ID, "blots": [blot, blot, blot]},
            )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertEqual(payload["fingerprints"], {})      # every read failed
        self.assertTrue(payload["truncated"], "failed reads must still cost budget")

    def test_blot_fingerprints_reports_untruncated_for_a_normal_request(self):
        blot = self.upload_blot()
        response = self.client.post(
            "/blot-fingerprints", json={"sessionId": SESSION_ID, "blots": [blot]},
        )
        self.assertFalse(response.get_json()["truncated"])

    def test_blot_fingerprint_changes_with_pixel_data(self):
        def upload(pixels):
            tif = io.BytesIO(); tifffile.imwrite(tif, pixels)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("Blot A/metadata.txt", "Image metadata\n#Fri May 15 16:38:05 PDT 2026\nRemarks=x")
                zf.writestr("Blot A/image_700.tif", tif.getvalue())
                zf.writestr("Blot A/image_800.tif", tif.getvalue())
            archive.seek(0)
            response = self.client.post(
                "/upload-zip",
                data={"sessionId": SESSION_ID, "file": (archive, "scanner.zip")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()["blots"][0]["fingerprint"]

        base = np.arange(64, dtype=np.uint16).reshape(8, 8)
        changed = base.copy()
        changed[0, 0] += 1
        self.assertNotEqual(upload(base), upload(changed), "one pixel must change the hash")

    def test_blot_fingerprint_ignores_the_preview_image(self):
        # The JPG is cosmetic; a re-export that only changes it is still the same scan.
        def upload(preview):
            tif = io.BytesIO(); tifffile.imwrite(tif, np.arange(64, dtype=np.uint16).reshape(8, 8))
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("Blot A/metadata.txt", "Image metadata\n#Fri May 15 16:38:05 PDT 2026\nRemarks=x")
                zf.writestr("Blot A/image_700.tif", tif.getvalue())
                zf.writestr("Blot A/image_800.tif", tif.getvalue())
                zf.writestr("Blot A/preview.jpg", preview)
            archive.seek(0)
            response = self.client.post(
                "/upload-zip",
                data={"sessionId": SESSION_ID, "file": (archive, "scanner.zip")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()["blots"][0]["fingerprint"]

        self.assertEqual(upload(b"first-preview-bytes"), upload(b"different-preview-bytes"))

    def test_blot_fingerprint_distinguishes_channel_assignment(self):
        # Same bytes, different channel. The digest is channel-labelled so a single-channel
        # 800nm blot cannot collide with a single-channel 700nm one.
        def upload(channel):
            tif = io.BytesIO(); tifffile.imwrite(tif, np.arange(64, dtype=np.uint16).reshape(8, 8))
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("Blot A/metadata.txt", "Image metadata\n#Fri May 15 16:38:05 PDT 2026\nRemarks=x")
                zf.writestr(f"Blot A/image_{channel}.tif", tif.getvalue())
            archive.seek(0)
            response = self.client.post(
                "/upload-zip",
                data={"sessionId": SESSION_ID, "file": (archive, "scanner.zip")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()["blots"][0]["fingerprint"]

        self.assertNotEqual(upload("700"), upload("800"))

    def test_disabled_channel_is_dropped_from_the_composite(self):
        blot = self.upload_blot()

        def render(enabled):
            payload = {"sessionId": SESSION_ID, "blot": blot, "colorMode": "color"}
            if enabled is not None:
                payload["enabledChannels"] = enabled
            response = self.client.post("/render-composite", json=payload)
            self.assertEqual(response.status_code, 200, response.data[:200])
            return np.asarray(Image.open(io.BytesIO(response.data)).convert("RGB"))

        both = render(None)
        self.assertGreater(both[:, :, 0].max(), 0)
        self.assertGreater(both[:, :, 1].max(), 0)

        only_700 = render(["700"])
        self.assertGreater(only_700[:, :, 0].max(), 0)
        self.assertEqual(only_700[:, :, 1].max(), 0, "800nm should contribute nothing")
        # The kept channel must be untouched, not merely present — disabling one channel is
        # a view change and must not alter how the other is rendered.
        np.testing.assert_array_equal(only_700[:, :, 0], both[:, :, 0])

        only_800 = render(["800"])
        self.assertEqual(only_800[:, :, 0].max(), 0)
        np.testing.assert_array_equal(only_800[:, :, 1], both[:, :, 1])

        # Never render a blank frame: an empty or malformed selection falls back to both.
        for bogus in ([], "700", {"700": True}, ["999"]):
            np.testing.assert_array_equal(render(bogus), both)

    def test_disabling_a_channel_does_not_resize_the_composite(self):
        # Box coordinates live in composite space, and composite size is max(700, 800) per
        # axis. If switching a channel off shrank the frame to the other channel's size,
        # every drawn box would silently move relative to the pixels it measures.
        b700 = io.BytesIO(); tifffile.imwrite(b700, np.arange(40 * 60, dtype=np.uint16).reshape(40, 60))
        b800 = io.BytesIO(); tifffile.imwrite(b800, np.arange(70 * 30, dtype=np.uint16).reshape(70, 30))
        d700 = backend.store_temp_file(SESSION_ID, "size-blot", "700.tif", b700.getvalue(), "image/tiff")
        d800 = backend.store_temp_file(SESSION_ID, "size-blot", "800.tif", b800.getvalue(), "image/tiff")
        blot = {"id": "size-blot", "files": {"700": d700, "800": d800}}

        sizes = set()
        for enabled in (["700", "800"], ["700"], ["800"]):
            response = self.client.post(
                "/render-composite",
                json={"sessionId": SESSION_ID, "blot": blot, "colorMode": "color",
                      "enabledChannels": enabled},
            )
            self.assertEqual(response.status_code, 200, response.data[:200])
            sizes.add(Image.open(io.BytesIO(response.data)).size)
        self.assertEqual(len(sizes), 1, f"composite size changed with the enabled set: {sizes}")
        self.assertEqual(sizes.pop(), (60, 70))   # max width, max height

    def test_single_channel_blot_renders_and_reports_one_channel(self):
        # Previously a 404: a blot with only one TIF could not be viewed at all.
        archive = make_single_channel_zip()
        upload = self.client.post(
            "/upload-zip",
            data={"sessionId": SESSION_ID, "file": (archive, "single.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 200, upload.get_json())
        blot = upload.get_json()["blots"][0]

        composite = self.client.post(
            "/render-composite",
            json={"sessionId": SESSION_ID, "blot": blot, "colorMode": "color"},
        )
        self.assertEqual(composite.status_code, 200, composite.data[:200])
        rendered = np.asarray(Image.open(io.BytesIO(composite.data)).convert("RGB"))
        self.assertEqual(rendered[:, :, 0].max(), 0, "absent 700nm must be black, not noise")
        self.assertGreater(rendered[:, :, 1].max(), 0)

        # The client decides availability from /channel-stats, so it must list only the
        # channel that exists — that is what stops the missing one being switched on.
        stats = self.client.post("/channel-stats", json={"sessionId": SESSION_ID, "blot": blot})
        self.assertEqual(set(stats.get_json()["channels"]), {"800"})

        pixels = self.client.post("/channel-pixels", json={"sessionId": SESSION_ID, "blot": blot})
        self.assertEqual(pixels.status_code, 200)
        width, height, channels = struct.unpack_from("<III", pixels.data, 4)
        self.assertEqual(channels, 2, "buffer always carries two channels, absent one zeroed")
        raw = np.frombuffer(pixels.data, dtype="<u2", offset=32).reshape(2, height, width)
        self.assertEqual(raw[0].max(), 0)

    def test_blot_with_no_channels_is_rejected(self):
        response = self.client.post(
            "/render-composite",
            json={"sessionId": SESSION_ID, "blot": {"id": "empty", "files": {}}},
        )
        self.assertEqual(response.status_code, 404)

    def test_channel_pixels_refuses_oversized_blots(self):
        # Above the cap the client must be told to fall back to server rendering rather than
        # being handed a multi-megabyte buffer.
        blot = self.upload_blot()
        with mock.patch.object(backend, "MAX_INTERACTIVE_PIXELS", 4):
            response = self.client.post(
                "/channel-pixels", json={"sessionId": SESSION_ID, "blot": blot},
            )
        self.assertEqual(response.status_code, 413)
        self.assertIn("maxPixels", response.get_json())

    def test_channel_pixels_is_session_scoped(self):
        # Same boundary as every other blot route: another session's descriptor must not
        # become readable just because this endpoint returns raw pixels.
        blot = self.upload_blot()
        response = self.client.post(
            "/channel-pixels", json={"sessionId": "other-session-entirely", "blot": blot},
        )
        self.assertGreaterEqual(response.status_code, 400)

    def test_composite_straighten_handles_mismatched_channel_shapes(self):
        # Different-aspect channels make the composite->native scale non-uniform. Straighten
        # rotates each channel in its native frame (matching /extract), so the display must
        # still render without error even in this case (regression for the coord-frame fix).
        b700 = io.BytesIO(); tifffile.imwrite(b700, np.arange(40 * 60, dtype=np.uint16).reshape(40, 60))
        b800 = io.BytesIO(); tifffile.imwrite(b800, np.arange(60 * 40, dtype=np.uint16).reshape(60, 40))
        d700 = backend.store_temp_file(SESSION_ID, "mismatch-blot", "700.tif", b700.getvalue(), "image/tiff")
        d800 = backend.store_temp_file(SESSION_ID, "mismatch-blot", "800.tif", b800.getvalue(), "image/tiff")
        blot = {"id": "mismatch-blot", "files": {"700": d700, "800": d800}}
        response = self.client.post(
            "/render-composite",
            json={
                "sessionId": SESSION_ID, "blot": blot,
                "colorMode": "color", "rotationDeg": 10,
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.mimetype, "image/png")

    def test_rotation_preserves_nonfinite_saturation_markers(self):
        # LI-COR float scans mark saturation with +inf; NEAREST rotation must not interpolate
        # it away, or a straightened extract would undercount saturated pixels.
        a = np.zeros((40, 50), dtype=np.float16)
        a[10, 10] = np.inf
        before = int(np.count_nonzero(backend.tif_saturation_mask(a)))
        rotated = backend._rotate_array(a, 4.0, resample=backend.Image.NEAREST).astype(np.float16)
        after = int(np.count_nonzero(backend.tif_saturation_mask(rotated)))
        self.assertEqual(before, 1)
        self.assertEqual(after, before)

    def test_session_id_must_match_temp_descriptors(self):
        blot = self.upload_blot()
        response = self.client.post(
            "/extract",
            json={
                "sessionId": "other-session",
                "blot": blot,
                "channel": "700",
                "backgroundAxis": "leftright",
                "boxes": [{"x": 0, "y": 0, "w": 2, "h": 2}],
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_temp_descriptors_must_use_expected_path_shape(self):
        with self.assertRaises(backend.PublicError):
            backend.validate_temp_path(f"sessions/{SESSION_ID}/blot-1/not-allowed.tif", SESSION_ID)
        with self.assertRaises(backend.PublicError):
            backend.validate_temp_path(f"uploads/{SESSION_ID}/nested/file.zip", SESSION_ID, allow_uploads=True)

    def test_blob_urls_must_be_vercel_blob_urls_matching_descriptor_path(self):
        path = f"sessions/{SESSION_ID}/blot-1/700.tif"
        with self.assertRaises(backend.PublicError):
            backend.vercel_blob_read({"url": f"https://example.test/{path}"}, path)
        with self.assertRaises(backend.PublicError):
            backend.vercel_blob_read(
                {"url": "https://store.public.blob.vercel-storage.com/sessions/other/blot-1/700.tif"},
                path,
            )

    def test_vercel_blob_put_uses_current_api_headers(self):
        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return b'{"pathname":"sessions/test-browser-session/blot-1/700.tif","url":"https://abc.public.blob.vercel-storage.com/sessions/test-browser-session/blot-1/700.tif"}'

        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        path = f"sessions/{SESSION_ID}/blot-1/700.tif"
        with mock.patch.dict(
            os.environ,
            {
                "BLOB_READ_WRITE_TOKEN": "vercel_blob_rw_teststore_secret",
                "BLOB_STORE_ID": "",
            },
            clear=False,
        ), mock.patch.object(backend._blob_opener, "open", side_effect=fake_urlopen):
            result = backend.vercel_blob_put(path, b"abc", "image/tiff")

        request = captured["request"]
        self.assertEqual(request.full_url, f"https://vercel.com/api/blob/?pathname={path}")
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.headers["X-api-version"], "12")
        self.assertEqual(request.headers["X-vercel-blob-access"], backend.BLOB_ACCESS)
        self.assertEqual(request.headers["X-vercel-blob-store-id"], "teststore")
        self.assertEqual(request.headers["X-content-length"], "3")
        self.assertEqual(result["pathname"], path)

    def test_vercel_blob_put_retries_private_when_store_rejects_public_access(self):
        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return b'{"pathname":"sessions/test-browser-session/blot-1/800.tif","url":"https://abc.private.blob.vercel-storage.com/sessions/test-browser-session/blot-1/800.tif"}'

        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                body = b'{"error":{"code":"bad_request","message":"Cannot use public access on a private store. The store is configured with private access."}}'
                raise backend.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(body))
            return FakeResponse()

        path = f"sessions/{SESSION_ID}/blot-1/800.tif"
        with mock.patch.dict(
            os.environ,
            {
                "BLOB_READ_WRITE_TOKEN": "vercel_blob_rw_teststore_secret",
                "BLOB_STORE_ID": "",
            },
            clear=False,
        ), mock.patch.object(backend._blob_opener, "open", side_effect=fake_urlopen), mock.patch.object(
            backend, "BLOB_ACCESS", "public"
        ), mock.patch.object(
            backend, "RUNTIME_BLOB_ACCESS", "public"
        ):
            result = backend.vercel_blob_put(path, b"abc", "image/tiff")

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].headers["X-vercel-blob-access"], "public")
        self.assertEqual(requests[1].headers["X-vercel-blob-access"], "private")
        self.assertEqual(result["pathname"], path)

    def test_vercel_blob_read_retries_transient_errors(self):
        class FakeResponse:
            headers = {"Content-Type": "application/octet-stream"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return b"ok"

        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise backend.HTTPError(request.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"retry"))
            return FakeResponse()

        path = f"sessions/{SESSION_ID}/blot-1/700.tif"
        descriptor = {
            "url": f"https://abc.private.blob.vercel-storage.com/{path}",
        }
        with mock.patch.dict(
            os.environ,
            {
                "BLOB_READ_WRITE_TOKEN": "vercel_blob_rw_teststore_secret",
                "BLOB_STORE_ID": "",
            },
            clear=False,
        ), mock.patch.object(backend._blob_opener, "open", side_effect=fake_urlopen), mock.patch("app.time.sleep"):
            result = backend.vercel_blob_read(descriptor, path)

        self.assertEqual(result, b"ok")
        self.assertEqual(len(requests), 2)

    def test_cleanup_removes_temp_files(self):
        blot = self.upload_blot()
        response = self.client.post(
            "/cleanup",
            json={"sessionId": SESSION_ID, "blots": [blot]},
        )
        self.assertEqual(response.status_code, 200, response.get_json())

        missing = self.client.post(
            "/render-composite",
            json={"sessionId": SESSION_ID, "blot": blot},
        )
        self.assertEqual(missing.status_code, 404)

    def test_parses_blot_creation_time_with_timezone(self):
        self.assertEqual(
            backend.parse_blot_created_at("#Fri May 15 16:38:05 PDT 2026"),
            "2026-05-15T23:38:05+00:00",
        )

    def test_zip_channel_detection_uses_tif_filename_not_folder_name(self):
        response = self.client.post(
            "/upload-zip",
            data={"sessionId": SESSION_ID, "file": (make_single_channel_zip(), "scanner.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        blot = response.get_json()["blots"][0]
        self.assertFalse(blot["has700"])
        self.assertTrue(blot["has800"])
        self.assertNotIn("700", blot["files"])
        self.assertIn("800", blot["files"])

    def test_signal_extraction_clips_partial_boxes_without_sliding_roi(self):
        arr = np.arange(30, dtype=np.float32).reshape(1, 30)
        signal = backend.extract_box_signal(
            arr,
            {"x": -10, "y": 0, "w": 20, "h": 1},
            "topbottom",
        )
        self.assertEqual(signal["rawSignal"], 45.0)
        self.assertEqual(signal["x"], 0)
        self.assertEqual(signal["w"], 10)

    def test_signal_extraction_rejects_fully_outside_boxes(self):
        arr = np.arange(9, dtype=np.float32).reshape(3, 3)
        with self.assertRaises(backend.PublicError):
            backend.extract_box_signal(arr, {"x": 5, "y": 0, "w": 2, "h": 2}, "leftright")

    def test_full_width_box_reports_that_no_background_was_subtracted(self):
        # A full-width lane (what detection emits when the column profile never drops below
        # the cut) leaves no room for either left or right strip. adjustedSignal then equals
        # rawSignal, which on its own is indistinguishable from a real zero background — so
        # the flag is the only thing separating "measured 0" from "did not measure".
        arr = np.full((4, 6), 10.0, dtype=np.float32)
        signal = backend.extract_box_signal(arr, {"x": 0, "y": 0, "w": 6, "h": 4}, "leftright")
        self.assertTrue(signal["backgroundMissing"])
        self.assertEqual(signal["backgroundSignal"], 0.0)
        self.assertEqual(signal["adjustedSignal"], signal["rawSignal"])

    def test_a_box_with_room_for_a_strip_is_not_flagged(self):
        arr = np.full((4, 6), 10.0, dtype=np.float32)
        signal = backend.extract_box_signal(arr, {"x": 2, "y": 0, "w": 2, "h": 4}, "leftright")
        self.assertFalse(signal["backgroundMissing"])
        self.assertEqual(signal["adjustedSignal"], 0.0)   # uniform field subtracts to zero

    def test_one_sided_background_still_counts_as_measured(self):
        # A box flush against the left edge keeps its right strip. That is a degraded
        # estimate, not a missing one, and must not raise the flag.
        arr = np.full((4, 6), 10.0, dtype=np.float32)
        signal = backend.extract_box_signal(arr, {"x": 0, "y": 0, "w": 3, "h": 4}, "leftright")
        self.assertFalse(signal["backgroundMissing"])
        self.assertEqual(signal["backgroundPerPixel"], 10.0)

    def test_rejects_zip_traversal(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../metadata.txt", "Remarks=Unsafe")
        archive.seek(0)
        response = self.client.post(
            "/upload-zip",
            data={"sessionId": SESSION_ID, "file": (archive, "unsafe.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

    def test_licor_reduced_resolution_tif_is_accepted(self):
        tif_bytes, expected = make_licor_pyramid_tif()
        result = backend.decode_validated_tif(tif_bytes, "LI-COR TIF")
        np.testing.assert_array_equal(result, expected)


if __name__ == "__main__":
    unittest.main()
