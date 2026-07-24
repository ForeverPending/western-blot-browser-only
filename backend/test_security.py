import io
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import numpy as np
import tifffile


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
                "brightness700": 1,
                "contrast700": 1,
                "brightness800": 1,
                "contrast800": 1,
                "colorMode": "color",
            },
        )
        self.assertEqual(composite.status_code, 200, composite.get_json())
        self.assertEqual(composite.mimetype, "image/jpeg")

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
                "brightness700": 1, "contrast700": 1, "brightness800": 1, "contrast800": 1,
                "colorMode": "color", "rotationDeg": 999,
            },
        )
        self.assertEqual(composite.status_code, 200, composite.get_json())
        self.assertEqual(composite.mimetype, "image/jpeg")

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
                "brightness700": 1, "contrast700": 1, "brightness800": 1, "contrast800": 1,
                "colorMode": "color", "rotationDeg": 10,
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.mimetype, "image/jpeg")

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
        ), mock.patch("app.urlopen", side_effect=fake_urlopen):
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
        ), mock.patch("app.urlopen", side_effect=fake_urlopen), mock.patch.object(
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
        ), mock.patch("app.urlopen", side_effect=fake_urlopen), mock.patch("app.time.sleep"):
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
