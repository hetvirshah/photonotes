"""
Integration test suite for PhotoNotes.
Tests all routes in LOCAL_MODE:
1. GET /health (shallow ALB check)
2. GET /health/deep (database health check)
3. GET / (home page with IMDSv2 metadata banner)
4. POST /notes (valid note + photo upload)
5. Validation: missing text, exceeding 500 chars, invalid file extensions
6. GET /notes (JSON list with presigned photo URLs)
7. GET /uploads/<key> (local image file serving)
"""

import io
import os
import unittest

# Ensure LOCAL_MODE is enabled before importing app
os.environ["LOCAL_MODE"] = "1"
from app import app, init_db, SQLITE_DB_PATH, UPLOAD_FOLDER


class PhotoNotesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.client = app.test_client()

    def test_01_shallow_health_check(self):
        """GET /health must return 200 {'status': 'ok'} without database operations."""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data, {"status": "ok"})

    def test_02_deep_health_check(self):
        """GET /health/deep must test database connectivity and return 200."""
        resp = self.client.get("/health/deep")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("database"), "connected")
        self.assertEqual(data.get("mode"), "local")

    def test_03_index_page(self):
        """GET / must render the UI with EC2 metadata banner."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("PhotoNotes", html)
        self.assertIn("Instance ID:", html)
        self.assertIn("Availability Zone:", html)
        self.assertIn("Post a New Note", html)

    def test_04_create_note_success(self):
        """POST /notes with valid text and photo must upload and redirect."""
        dummy_file = (io.BytesIO(b"fake-image-bytes-content"), "sunset.png")
        resp = self.client.post(
            "/notes",
            data={
                "note_text": "Beautiful test sunset from EC2 target node!",
                "photo": dummy_file,
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/?message=", resp.headers.get("Location", ""))

    def test_05_list_notes_api(self):
        """GET /notes returns JSON array with presigned/local photo URLs."""
        resp = self.client.get("/notes")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        first = data[0]
        self.assertIn("id", first)
        self.assertIn("note_text", first)
        self.assertIn("s3_key", first)
        self.assertIn("created_at", first)
        self.assertIn("photo_url", first)
        self.assertTrue(first["photo_url"].startswith("/uploads/"))

    def test_06_uploaded_file_route(self):
        """GET /uploads/<filename> serves uploaded image in local mode."""
        resp = self.client.get("/notes")
        data = resp.get_json()
        photo_url = data[0]["photo_url"]
        file_resp = self.client.get(photo_url)
        self.assertEqual(file_resp.status_code, 200)
        self.assertEqual(file_resp.data, b"fake-image-bytes-content")

    def test_07_validation_empty_text(self):
        """POST /notes without note_text must fail validation."""
        dummy_file = (io.BytesIO(b"bytes"), "test.jpg")
        resp = self.client.post(
            "/notes",
            data={"note_text": "", "photo": dummy_file},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=Note+text+is+required.", resp.headers.get("Location", ""))

    def test_08_validation_text_too_long(self):
        """POST /notes with >500 chars must fail validation."""
        dummy_file = (io.BytesIO(b"bytes"), "test.jpg")
        long_text = "A" * 501
        resp = self.client.post(
            "/notes",
            data={"note_text": long_text, "photo": dummy_file},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("exceeds+maximum+length", resp.headers.get("Location", ""))

    def test_09_validation_invalid_file_extension(self):
        """POST /notes with disallowed file extension must fail validation."""
        dummy_file = (io.BytesIO(b"malicious script"), "script.exe")
        resp = self.client.post(
            "/notes",
            data={"note_text": "Try uploading exe", "photo": dummy_file},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("Invalid+file+format", resp.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
