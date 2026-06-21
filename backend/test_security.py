import io
import os
import tempfile
import unittest
import uuid
import zipfile
from unittest.mock import patch

import numpy as np
import tifffile


TEST_DATA = tempfile.TemporaryDirectory()
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
os.environ.setdefault("BLOT_STORAGE_BACKEND", "local")
os.environ["BLOT_DATA_DIR"] = TEST_DATA.name
os.environ["RATELIMIT_ENABLED"] = "false"

import app as backend  # noqa: E402


USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())


def fake_verify(token):
    users = {"token-a": USER_A, "token-b": USER_B}
    if token not in users:
        raise backend.AuthenticationError()
    return users[token]


def make_test_zip():
    tif_buffer = io.BytesIO()
    tifffile.imwrite(tif_buffer, np.arange(64, dtype=np.uint16).reshape(8, 8))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Blot A/metadata.txt", "Remarks=Security test blot")
        zf.writestr("Blot A/image_700.tif", tif_buffer.getvalue())
        zf.writestr("Blot A/image_800.tif", tif_buffer.getvalue())
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


class MultiUserSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.verify_supabase_token = fake_verify
        backend.app.config.update(TESTING=True)
        cls.client = backend.app.test_client()

    def auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_authentication_and_owner_isolation(self):
        self.assertEqual(self.client.get("/blots").status_code, 401)

        upload = self.client.post(
            "/upload-zip",
            headers=self.auth("token-a"),
            data={"file": (make_test_zip(), "scanner.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 200, upload.get_json())
        uploaded_blot = upload.get_json()["blots"][0]
        blot_id = uploaded_blot["id"]
        self.assertIn("hasJpg", uploaded_blot)
        self.assertNotIn("has_jpg", uploaded_blot)

        owner_list = self.client.get("/blots", headers=self.auth("token-a"))
        other_list = self.client.get("/blots", headers=self.auth("token-b"))
        self.assertEqual(len(owner_list.get_json()["blots"]), 1)
        self.assertEqual(other_list.get_json()["blots"], [])
        listed_blot = owner_list.get_json()["blots"][0]
        self.assertIn("scanCount", listed_blot)
        self.assertIn("createdAt", listed_blot)
        self.assertNotIn("scan_count", listed_blot)

        hidden = self.client.get(
            f"/blots/{blot_id}/composite",
            headers=self.auth("token-b"),
        )
        self.assertEqual(hidden.status_code, 404)

        invalid_box = self.client.post(
            f"/blots/{blot_id}/extract",
            headers=self.auth("token-a"),
            json={
                "channel": "700",
                "backgroundAxis": "leftright",
                "boxes": [{"x": "invalid", "y": 0, "w": 2, "h": 2}],
            },
        )
        self.assertEqual(invalid_box.status_code, 400)

        extraction = self.client.post(
            f"/blots/{blot_id}/extract",
            headers=self.auth("token-a"),
            json={
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

        scan = self.client.post(
            f"/blots/{blot_id}/scans",
            headers=self.auth("token-a"),
            json={
                "proteinName": "IRE1",
                "channel": "700",
                "backgroundAxis": "leftright",
                "lanes": [{"name": "Lane 1", "signal": 100.0}],
            },
        )
        self.assertEqual(scan.status_code, 201, scan.get_json())
        self.assertEqual(scan.get_json()["scan"]["backgroundAxis"], "leftright")
        hidden_scans = self.client.get(
            f"/blots/{blot_id}/scans",
            headers=self.auth("token-b"),
        )
        self.assertEqual(hidden_scans.status_code, 404)

    def test_rejects_zip_traversal(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../metadata.txt", "Remarks=Unsafe")
        archive.seek(0)
        response = self.client.post(
            "/upload-zip",
            headers=self.auth("token-a"),
            data={"file": (archive, "unsafe.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

    def test_blot_files_are_loaded_only_when_requested(self):
        upload = self.client.post(
            "/upload-zip",
            headers=self.auth("token-a"),
            data={"file": (make_test_zip(), "scanner.zip")},
            content_type="multipart/form-data",
        )
        blot_id = upload.get_json()["blots"][0]["id"]
        backend.blot_store.pop((USER_A, blot_id), None)

        # Metadata checks should not read large image files from storage.
        with backend.app.test_request_context():
            backend.g.owner_id = USER_A
            backend.g.access_token = "token-a"
            metadata = backend.get_blot(blot_id, ())
            self.assertNotIn("tif_700_bytes", metadata)
            self.assertNotIn("tif_800_bytes", metadata)

            loaded = backend.get_blot(blot_id, ("700",))
            self.assertIn("tif_700_bytes", loaded)
            self.assertNotIn("tif_800_bytes", loaded)

    def test_legacy_storage_fallback_handles_supabase_missing_object_response(self):
        calls = []

        def fake_supabase_request(method, path, **_kwargs):
            calls.append((method, path))
            if f"/{USER_A}/blots/legacy-blot/preview.jpg" in path:
                raise backend.SupabaseRequestError(
                    400,
                    '{"statusCode":"404","error":"not_found","message":"Object not found"}',
                )
            return b"legacy-preview"

        with backend.app.test_request_context():
            backend.g.owner_id = USER_A
            backend.g.access_token = "token-a"
            with patch.object(backend, "supabase_request", side_effect=fake_supabase_request):
                result = backend.supabase_download_file("legacy-blot", "preview.jpg")

        self.assertEqual(result, b"legacy-preview")
        self.assertEqual(len(calls), 2)
        self.assertIn("/blots/legacy-blot/preview.jpg", calls[1][1])

    def test_accepts_licor_float16_pyramid_tif(self):
        tif_bytes, expected = make_licor_pyramid_tif()

        backend.validate_tif_pixels(tif_bytes, "700nm TIF")
        decoded = backend.read_validated_tif(tif_bytes)

        np.testing.assert_array_equal(decoded, expected)

    def test_rejects_multiple_full_resolution_tif_pages(self):
        image = io.BytesIO()
        with tifffile.TiffWriter(image) as tif:
            tif.write(np.ones((8, 8), dtype=np.uint16), subfiletype=0)
            tif.write(np.ones((8, 8), dtype=np.uint16), subfiletype=0)

        with self.assertRaises(backend.PublicError):
            backend.validate_tif_pixels(image.getvalue(), "700nm TIF")

    def test_sanitizes_licor_masked_and_saturated_pixels(self):
        pixels = np.arange(64, dtype=np.float16).reshape(8, 8)
        pixels[0, 0] = np.nan
        pixels[0, 1] = np.inf
        image = io.BytesIO()
        tifffile.imwrite(image, pixels)

        decoded = backend.read_validated_tif(image.getvalue())

        self.assertEqual(decoded[0, 0], np.float16(0))
        self.assertEqual(decoded[0, 1], np.finfo(np.float16).max)
        self.assertTrue(np.isfinite(decoded).all())

    def test_rejects_tif_without_finite_measurements(self):
        image = io.BytesIO()
        tifffile.imwrite(image, np.full((8, 8), np.nan, dtype=np.float16))

        with self.assertRaises(backend.PublicError):
            backend.validate_tif_pixels(image.getvalue(), "700nm TIF")


if __name__ == "__main__":
    unittest.main()
