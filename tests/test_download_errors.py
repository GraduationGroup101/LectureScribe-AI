import errno
from unittest import TestCase
from unittest.mock import patch

import api


class DownloadFailureTests(TestCase):
    def test_read_only_cookie_error_is_not_reported_as_bot_verification(self):
        message = api.describe_pipeline_error(
            OSError(errno.EROFS, "Read-only file system", "/etc/secrets/youtube-cookies.txt")
        )
        self.assertIn("writable temporary directory", message)
        self.assertNotIn("not a bot", message)

    def test_bot_verification_error_keeps_its_recovery_hint(self):
        message = api.describe_pipeline_error(RuntimeError("Sign in to confirm you're not a bot"))
        self.assertIn("Pass browser cookies", message)

    def test_failed_download_preserves_the_last_progress(self):
        def fail(**request):
            api.update_job_progress("fixture-job", request, "downloading")
            raise RuntimeError("Download failed")

        with patch.object(api, "jobs", {"fixture-job": {}}), patch.object(
            api, "save_jobs_unlocked"
        ), patch.object(api, "process_youtube_url", side_effect=fail):
            api.run_transcription_job("fixture-job", {"clean": True})
            job = api.jobs["fixture-job"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["stage"], "failed")
        self.assertEqual(job["current_step"], 2)
        self.assertEqual(job["progress_percent"], 15)
        self.assertIn("Download failed", job["error"])
