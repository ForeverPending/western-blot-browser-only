import base64
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

    def test_pptx_export_rejects_svg_data_urls(self):
        svg = base64.b64encode(b"<svg></svg>").decode("ascii")
        with self.assertRaises(backend.PublicError):
            backend.validate_data_url_size(f"data:image/svg+xml;base64,{svg}")

    def test_pptx_export_rejects_invalid_png_data_urls(self):
        with self.assertRaises(backend.PublicError):
            backend.validate_data_url_size("data:image/png;base64,AAAA")

    def test_pptx_export_rejects_unknown_slide_types(self):
        with self.assertRaises(backend.PublicError):
            backend.validate_pptx_payload([{"type": "image", "images": [], "graphs": []}])

    def test_pptx_export_rejects_mixed_slide_contracts(self):
        with self.assertRaises(backend.PublicError):
            backend.validate_pptx_payload([{"type": "graphs", "images": [{"image": ""}], "graphs": []}])


if __name__ == "__main__":
    unittest.main()
