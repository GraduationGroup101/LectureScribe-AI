from collections import deque
import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

import api


def lecture(index):
    """A distinct lecture per submission: identical active jobs are deduplicated, not counted."""
    return {"youtube_url": f"https://youtu.be/lecture{index:04d}"}


class RateLimitTests(TestCase):
    def setUp(self):
        for name, value in (
            ("jobs", {}),
            ("job_submissions", deque()),
            ("job_submissions_by_client", {}),
        ):
            replacement = patch.object(api, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        for name in ("save_jobs_unlocked", "executor"):
            replacement = patch.object(api, name)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = TestClient(api.app)
        self.lectures = 0

    def body(self):
        self.lectures += 1
        return lecture(self.lectures)

    def submit(self, ip=None):
        headers = {"CF-Connecting-IP": ip} if ip else None
        return self.client.post("/jobs", json=self.body(), headers=headers)

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
        # A fixed clock: on a busy machine more than a second can pass between the
        # submissions, which would make Retry-After 3599.
        with patch.object(api, "JOB_MAX_ACTIVE", 100), patch.object(api, "monotonic", return_value=1000.0):
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
                    json=self.body(),
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

    def test_gateway_students_get_their_own_hourly_limit(self):
        with patch.dict(os.environ, {"RENDER": "true", "GATEWAY_KEYS": "edufusion-key, other-key"}), patch.object(
            api, "JOB_MAX_ACTIVE", 100
        ):
            gateway = {"CF-Connecting-IP": "192.0.2.50", "X-Gateway-Key": "edufusion-key"}
            student_a = {**gateway, "X-Gateway-User": "student:123"}
            student_b = {**gateway, "X-Gateway-User": "student:456"}
            for _ in range(api.JOB_RATE_PER_USER):
                self.assertEqual(self.client.post("/jobs", json=self.body(), headers=student_a).status_code, 202)
            blocked = self.client.post("/jobs", json=self.body(), headers=student_a)
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("hourly lecture limit", blocked.json()["detail"])
            # Another student behind the same gateway address is unaffected.
            self.assertEqual(self.client.post("/jobs", json=self.body(), headers=student_b).status_code, 202)
            # The account header means nothing without a valid key: it is a public caller by IP.
            for _ in range(api.JOB_RATE_PER_IP):
                self.assertEqual(self.client.post("/jobs", json=self.body(), headers={"CF-Connecting-IP": "192.0.2.60", "X-Gateway-User": "student:999"}).status_code, 202)
            self.assertEqual(self.client.post("/jobs", json=self.body(), headers={"CF-Connecting-IP": "192.0.2.60", "X-Gateway-User": "student:000"}).status_code, 429)
            # A malformed account name falls back to the gateway bucket.
            self.assertEqual(api.client_key(SimpleNamespace(headers={"X-Gateway-Key": "edufusion-key", "X-Gateway-User": "bad name!"}, client=None))[0], "gateway:unknown")

    def test_global_limit_still_caps_a_trusted_gateway(self):
        with patch.dict(os.environ, {"RENDER": "true", "GATEWAY_KEYS": "edufusion-key"}), patch.object(api, "JOB_MAX_ACTIVE", 1000), patch.object(api, "JOB_RATE_GLOBAL", 4):
            for index in range(4):
                headers = {"X-Gateway-Key": "edufusion-key", "X-Gateway-User": f"student:{index}"}
                self.assertEqual(self.client.post("/jobs", json=self.body(), headers=headers).status_code, 202)
            response = self.client.post("/jobs", json=self.body(), headers={"X-Gateway-Key": "edufusion-key", "X-Gateway-User": "student:new"})
            self.assertEqual(response.status_code, 429)
            self.assertIn("Server job limit", response.json()["detail"])

    def test_gateway_key_is_ignored_when_none_is_configured(self):
        with patch.dict(os.environ, {"GATEWAY_KEYS": ""}), patch.object(api, "JOB_MAX_ACTIVE", 100):
            for _ in range(api.JOB_RATE_PER_IP):
                self.assertEqual(self.client.post("/jobs", json=self.body(), headers={"X-Gateway-Key": "anything"}).status_code, 202)
            self.assertEqual(self.client.post("/jobs", json=self.body(), headers={"X-Gateway-Key": "anything"}).status_code, 429)

    def test_limits_are_configurable_from_the_environment(self):
        with patch.dict(os.environ, {"JOB_RATE_GLOBAL": "40", "JOB_MAX_ACTIVE": "x", "JOB_RATE_PER_IP": "0"}):
            self.assertEqual(api._limit_from_env("JOB_RATE_GLOBAL", 60), 40)
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
