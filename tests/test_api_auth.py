import os
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

import api


class AccessTests(TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "REQUIRE_ACCESS_TOKEN": "true",
            "APP_ACCESS_TOKEN": "a-long-test-access-code-1234567890",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.client = TestClient(api.app)
        self.headers = {"Authorization": "Bearer a-long-test-access-code-1234567890"}

    def test_job_routes_require_a_valid_token(self):
        for path in ("/jobs", "/jobs/unknown", "/jobs/unknown/transcript"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(
                    self.client.get(path, headers={"Authorization": "Bearer wrong"}).status_code,
                    401,
                )
                self.assertIn(self.client.get(path, headers=self.headers).status_code, (200, 404))

        with patch.object(api, "submit_job", return_value={"job_id": "test"}) as submit:
            body = {"youtube_url": "https://youtu.be/nBDFtTDXLAs"}
            self.assertEqual(self.client.post("/jobs", json=body).status_code, 401)
            submit.assert_not_called()
            self.assertEqual(self.client.post("/jobs", json=body, headers=self.headers).status_code, 202)
            submit.assert_called_once()

    def test_access_check_and_config(self):
        self.assertEqual(self.client.get("/auth/config").json(), {"required": True})
        self.assertEqual(self.client.get("/auth/check").status_code, 401)
        self.assertEqual(self.client.get("/auth/check", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_missing_server_secret_fails_closed(self):
        with patch.dict(os.environ, {"APP_ACCESS_TOKEN": ""}):
            self.assertEqual(self.client.get("/health").status_code, 503)
            self.assertEqual(self.client.get("/jobs").status_code, 503)
            self.assertEqual(self.client.post("/jobs", json={}).status_code, 503)

        with patch.dict(os.environ, {"APP_ACCESS_TOKEN": "Admin123!"}):
            self.assertEqual(self.client.get("/health").status_code, 503)
            self.assertEqual(self.client.get("/jobs", headers=self.headers).status_code, 503)

    def test_local_mode_remains_available_without_a_token(self):
        with patch.dict(os.environ, {"REQUIRE_ACCESS_TOKEN": "false", "APP_ACCESS_TOKEN": ""}):
            self.assertEqual(self.client.get("/auth/config").json(), {"required": False})
            self.assertEqual(self.client.get("/jobs").status_code, 200)
            self.assertEqual(self.client.get("/health").status_code, 200)
