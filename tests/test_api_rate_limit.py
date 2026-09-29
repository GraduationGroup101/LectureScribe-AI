from collections import deque
import os
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

import api


BODY = {"youtube_url": "https://youtu.be/nBDFtTDXLAs"}


class RateLimitTests(TestCase):
    def setUp(self):
        for name, value in (
            ("jobs", {}),
            ("job_submissions", deque()),
            ("job_submissions_by_ip", {}),
        ):
            replacement = patch.object(api, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        for name in ("save_jobs_unlocked", "executor"):
            replacement = patch.object(api, name)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = TestClient(api.app)

    def submit(self, ip=None):
        headers = {"CF-Connecting-IP": ip} if ip else None
        return self.client.post("/jobs", json=BODY, headers=headers)

    def test_jobs_and_health_are_public_without_access_routes(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/jobs").status_code, 200)
        for path in ("/", "/app", "/app/jobs"):
            body = self.client.get(path).text
            self.assertNotIn("access-panel", body)
            self.assertNotIn("access.js", body)
        self.assertEqual(self.client.get("/auth/config").status_code, 404)
        self.assertEqual(self.client.get("/auth/check").status_code, 404)
        self.assertEqual(self.submit().status_code, 202)

    def test_per_ip_limit_and_retry_after(self):
        with patch.object(api, "JOB_MAX_ACTIVE", 100):
            for _ in range(api.JOB_RATE_PER_IP):
                self.assertEqual(self.submit().status_code, 202)
            response = self.submit()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "3600")
        self.assertEqual(len(api.jobs), api.JOB_RATE_PER_IP)

    def test_global_limit_cannot_be_bypassed_by_changing_ip(self):
        with patch.dict(os.environ, {"RENDER": "true"}), patch.object(api, "JOB_MAX_ACTIVE", 100):
            for index in range(api.JOB_RATE_GLOBAL):
                self.assertEqual(self.submit(f"192.0.2.{index + 1}").status_code, 202)
            response = self.submit("192.0.2.200")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Server job limit", response.json()["detail"])

    def test_local_requests_ignore_spoofed_proxy_headers(self):
        with patch.dict(os.environ, {"RENDER": ""}), patch.object(api, "JOB_MAX_ACTIVE", 100):
            for index in range(api.JOB_RATE_PER_IP):
                response = self.client.post(
                    "/jobs",
                    json=BODY,
                    headers={"CF-Connecting-IP": f"192.0.2.{index + 1}", "X-Forwarded-For": f"198.51.100.{index + 1}"},
                )
                self.assertEqual(response.status_code, 202)
            self.assertEqual(self.submit("192.0.2.200").status_code, 429)

    def test_queue_limit_releases_when_job_finishes(self):
        with patch.dict(os.environ, {"RENDER": "true"}):
            for index in range(api.JOB_MAX_ACTIVE):
                self.assertEqual(self.submit(f"192.0.2.{index + 1}").status_code, 202)
            response = self.submit("192.0.2.100")
            self.assertEqual(response.status_code, 429)
            self.assertIn("queue is full", response.json()["detail"])
            next(iter(api.jobs.values()))["status"] = "completed"
            self.assertEqual(self.submit("192.0.2.100").status_code, 202)

    def test_trusted_gateway_skips_only_the_per_ip_limit(self):
        with patch.dict(os.environ, {"RENDER": "true", "GATEWAY_KEYS": "edufusion-key, other-key"}), patch.object(
            api, "JOB_MAX_ACTIVE", 100
        ):
            headers = {"CF-Connecting-IP": "192.0.2.50", "X-Gateway-Key": "edufusion-key"}
            for _ in range(api.JOB_RATE_PER_IP + 2):
                self.assertEqual(self.client.post("/jobs", json=BODY, headers=headers).status_code, 202)
            # A wrong or missing key is an ordinary client from the same address.
            for bad in ({"CF-Connecting-IP": "192.0.2.50", "X-Gateway-Key": "guess"}, {"CF-Connecting-IP": "192.0.2.50"}):
                response = self.client.post("/jobs", json=BODY, headers=bad)
                self.assertEqual(response.status_code, 429)
                self.assertIn("Your job limit", response.json()["detail"])
            remaining = api.JOB_RATE_GLOBAL - len(api.jobs)
            for _ in range(remaining):
                self.assertEqual(self.client.post("/jobs", json=BODY, headers=headers).status_code, 202)
            response = self.client.post("/jobs", json=BODY, headers=headers)
            self.assertEqual(response.status_code, 429)
            self.assertIn("Server job limit", response.json()["detail"])

    def test_gateway_key_is_ignored_when_none_is_configured(self):
        with patch.dict(os.environ, {"GATEWAY_KEYS": ""}), patch.object(api, "JOB_MAX_ACTIVE", 100):
            for _ in range(api.JOB_RATE_PER_IP):
                self.assertEqual(self.client.post("/jobs", json=BODY, headers={"X-Gateway-Key": "anything"}).status_code, 202)
            self.assertEqual(self.client.post("/jobs", json=BODY, headers={"X-Gateway-Key": "anything"}).status_code, 429)

    def test_limits_are_configurable_from_the_environment(self):
        with patch.dict(os.environ, {"JOB_RATE_GLOBAL": "40", "JOB_MAX_ACTIVE": "x", "JOB_RATE_PER_IP": "0"}):
            self.assertEqual(api._limit_from_env("JOB_RATE_GLOBAL", 12), 40)
            self.assertEqual(api._limit_from_env("JOB_MAX_ACTIVE", 3), 3)
            self.assertEqual(api._limit_from_env("JOB_RATE_PER_IP", 3), 3)

    def test_window_expires_and_invalid_url_does_not_count(self):
        with patch.object(api, "JOB_MAX_ACTIVE", 100), patch.object(
            api, "monotonic", side_effect=[1000, 1000, 1000, 1001, 4600]
        ):
            invalid = self.client.post("/jobs", json={"youtube_url": "not a URL"})
            self.assertEqual(invalid.status_code, 400)
            for _ in range(api.JOB_RATE_PER_IP):
                self.assertEqual(self.submit().status_code, 202)
            self.assertEqual(self.submit().status_code, 429)
            self.assertEqual(self.submit().status_code, 202)
