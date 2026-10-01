"""LectureScribe <-> EduFusion contract v2: validation, dedupe, gateway scoping, callbacks,
keep-alive and job-state robustness. No network, no pipeline, no real sleeps."""

from collections import deque
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import api


GATEWAY_KEY = "edufusion-key"
GATEWAY = {"X-Gateway-Key": GATEWAY_KEY, "X-Gateway-User": "student-hash-1"}
CALLBACK = "https://edufusion.example/api/lecture-scribe/callback"


def lecture(video_id="nBDFtTDXLAs", **extra):
    return {"youtube_url": f"https://youtu.be/{video_id}", **extra}


class ApiTestCase(TestCase):
    """Isolated registry, no disk writes, no worker thread, no keep-alive."""

    def setUp(self):
        for name, value in (
            ("jobs", {}),
            ("job_submissions", deque()),
            ("job_submissions_by_client", {}),
            ("job_callbacks", {}),
            ("job_reuses", deque()),
            ("job_reuses_by_client", {}),
            ("callback_deliveries", {}),
            ("last_jobs_save", 0.0),
            ("last_finished_at", 0.0),
            ("keepalive_running", False),
            ("JOB_RATE_PER_IP", 100),
            ("JOB_MAX_ACTIVE", 100),
        ):
            replacement = patch.object(api, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.save = self.patch("save_jobs_unlocked")
        self.executor = self.patch("executor")
        environment = patch.dict(os.environ, {"GATEWAY_KEYS": GATEWAY_KEY, "RENDER": ""})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("RENDER_EXTERNAL_URL", "KEEPALIVE_DISABLED", "CALLBACK_ALLOW_HTTP", "KEEPALIVE_INTERVAL_SECONDS",
                     "KEEPALIVE_GRACE_SECONDS"):
            os.environ.pop(name, None)
        self.client = TestClient(api.app)

    def patch(self, name, **kwargs):
        replacement = patch.object(api, name, **kwargs)
        mock = replacement.start()
        self.addCleanup(replacement.stop)
        return mock

    def submit(self, body=None, headers=None):
        return self.client.post("/jobs", json=body or lecture(), headers=headers)

    def completed_job(self, directory, job_id="done-1", *, video_id="nBDFtTDXLAs", language="ar", mode="formatted",
                      format_version=api.FORMAT_VERSION, finished_at=100.0, via_gateway=False):
        raw = Path(directory) / f"{job_id}.raw.txt"
        cleaned = Path(directory) / f"{job_id}.cleaned.md"
        raw.write_text("نص المحاضرة", encoding="utf-8")
        cleaned.write_text("## مقدمة\n\nنص المحاضرة", encoding="utf-8")
        job = {
            "job_id": job_id, "status": "completed", "stage": "completed", "progress_percent": 100,
            "submitted_at": finished_at - 50, "started_at": finished_at - 40, "finished_at": finished_at,
            "error": None, "request": {"youtube_url": f"https://youtu.be/{video_id}", "clean": mode == "formatted",
                                       "language": language},
            "result": {"raw_transcript_path": str(raw), "cleaned_transcript_path": str(cleaned),
                       "title": "محاضرة", "format_version": format_version},
            "title": "محاضرة", "video_id": video_id, "language": language, "mode": mode,
            "detected_language": "ar", "format_version": format_version, "video_duration_seconds": 600,
            "via_gateway": via_gateway,
        }
        api.jobs[job_id] = job
        return job


class LanguageValidationTests(ApiTestCase):
    def test_language_defaults_to_arabic_and_is_normalised(self):
        first = self.submit(lecture("aaaaaaaaaaa"))
        self.assertEqual(first.status_code, 202)
        self.assertEqual(api.jobs[first.json()["job_id"]]["language"], "ar")
        for sent, stored, video in ((" AUTO ", "auto", "bbbbbbbbbbb"), ("En", "en", "ccccccccccc"), ("fr", "fr", "ddddddddddd")):
            response = self.submit(lecture(video, language=sent))
            self.assertEqual(response.status_code, 202, sent)
            job = api.jobs[response.json()["job_id"]]
            self.assertEqual(job["language"], stored)
            self.assertEqual(job["request"]["language"], stored)

    def test_invalid_language_is_rejected_before_anything_is_queued(self):
        for value in ("english", "a", "ar-EG", "", None, 5, "x1"):
            response = self.submit(lecture(language=value))
            self.assertEqual(response.status_code, 422, value)
        self.assertEqual(api.jobs, {})
        self.assertEqual(len(api.job_submissions), 0)

    def test_submit_response_and_job_record_follow_the_contract(self):
        with patch.object(api, "time", return_value=1234.5):
            response = self.submit(lecture(clean=False, language="auto"))
        body = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["status_url"], f"/jobs/{body['job_id']}")
        self.assertEqual(body["transcript_url"], f"/jobs/{body['job_id']}/transcript")
        self.assertEqual(body["submitted_at"], 1234.5)
        self.assertNotIn("deduplicated", body)
        job = self.client.get(body["status_url"]).json()
        for field, value in (("video_id", "nBDFtTDXLAs"), ("language", "auto"), ("mode", "fast"),
                             ("title", None), ("detected_language", None), ("format_version", None),
                             ("video_duration_seconds", None), ("via_gateway", False)):
            self.assertEqual(job[field], value, field)
        self.assertEqual(job["jobs_ahead"], 0)
        self.executor.submit.assert_called_once()


class DedupeAndReuseTests(ApiTestCase):
    def test_identical_active_job_is_returned_without_using_a_slot(self):
        first = self.submit(lecture(language="ar"), headers=GATEWAY).json()
        again = self.submit(lecture(language="ar"), headers={**GATEWAY, "X-Gateway-User": "student-hash-2"})
        self.assertEqual(again.status_code, 202)
        self.assertEqual(again.json()["job_id"], first["job_id"])
        self.assertTrue(again.json()["deduplicated"])
        self.assertEqual(len(api.jobs), 1)
        self.assertEqual(len(api.job_submissions), 1)
        self.assertNotIn("user:student-hash-2", api.job_submissions_by_client)
        self.executor.submit.assert_called_once()

    def test_another_language_or_mode_is_a_different_job(self):
        first = self.submit(lecture(language="ar")).json()["job_id"]
        other_language = self.submit(lecture(language="en")).json()["job_id"]
        other_mode = self.submit(lecture(language="ar", clean=False)).json()["job_id"]
        self.assertEqual(len({first, other_language, other_mode}), 3)

    def test_a_finished_job_is_not_a_dedupe_target(self):
        first = self.submit().json()["job_id"]
        api.jobs[first].update(status="failed")
        second = self.submit().json()
        self.assertNotEqual(second["job_id"], first)
        self.assertNotIn("deduplicated", second)

    def test_saved_output_of_the_current_pipeline_completes_at_once(self):
        with TemporaryDirectory() as directory:
            source = self.completed_job(directory)
            response = self.submit(lecture(language="ar"), headers=GATEWAY)
            body = response.json()
            self.assertEqual(response.status_code, 202)
            self.assertEqual(body["status"], "completed")
            self.assertTrue(body["cached"])
            self.executor.submit.assert_not_called()
            self.assertEqual(len(api.job_submissions), 0)
            job = self.client.get(f"/jobs/{body['job_id']}", headers=GATEWAY).json()
            self.assertEqual(job["source_job_id"], source["job_id"])
            self.assertEqual(job["title"], "محاضرة")
            self.assertEqual(job["format_version"], api.FORMAT_VERSION)
            self.assertTrue(job["via_gateway"])
            self.assertTrue(job["result"]["used_cached_cleaned_transcript"])
            transcript = self.client.get(f"/jobs/{body['job_id']}/transcript")
            self.assertIn("مقدمة", transcript.text)

    def test_legacy_other_language_or_missing_outputs_are_not_reused(self):
        with TemporaryDirectory() as directory:
            self.completed_job(directory, "legacy", format_version=None)
            self.completed_job(directory, "english", language="en")
            gone = self.completed_job(directory, "gone")
            Path(gone["result"]["cleaned_transcript_path"]).unlink()
            response = self.submit(lecture(language="ar"))
            self.assertEqual(response.json()["status"], "queued")
            self.executor.submit.assert_called_once()

    def test_fallback_layout_of_a_formatted_request_is_not_reused_as_formatted(self):
        with TemporaryDirectory() as directory:
            job = self.completed_job(directory, "degraded")
            # Recorded before the top-level mode followed the produced mode.
            job["result"].update(mode="fast", requested_mode="formatted", cleaner_provider="formatter")
            response = self.submit(lecture(language="ar"))
        self.assertEqual(response.json()["status"], "queued")

    def test_reuse_can_be_declined(self):
        with TemporaryDirectory() as directory:
            self.completed_job(directory)
            response = self.submit(lecture(language="ar", use_cached_outputs=False))
        self.assertEqual(response.json()["status"], "queued")

    def test_reuse_has_its_own_hourly_budget_per_caller(self):
        with TemporaryDirectory() as directory, patch.object(api, "JOB_REUSE_PER_CLIENT", 3):
            self.completed_job(directory)
            answers = [self.submit(lecture(language="ar")).json() for _ in range(5)]
        self.assertEqual([answer["status"] for answer in answers], ["completed"] * 3 + ["queued"] * 2)
        self.assertTrue(all(answer.get("cached") for answer in answers[:3]))
        # Past the budget the caller is queued like any other request, under the normal limits.
        self.assertTrue(answers[4]["deduplicated"])
        self.assertEqual(len(api.job_reuses), 3)
        self.assertEqual(len(api.job_submissions), 1)
        self.assertEqual(len(api.job_submissions_by_client["ip:testclient"]), 1)

    def test_free_reuses_cannot_push_an_unfetched_result_out_of_the_history(self):
        with TemporaryDirectory() as directory, patch.object(api, "JOB_HISTORY_LIMIT", 5), \
                patch.object(api, "JOB_RATE_PER_IP", 1):
            self.completed_job(directory, "public-source", finished_at=200.0)
            self.completed_job(directory, "student-job", video_id="aaaaaaaaaaa", finished_at=50.0, via_gateway=True)
            statuses = [self.submit(lecture(language="ar")).status_code for _ in range(10)]
            self.assertEqual(self.client.get("/jobs/student-job").status_code, 200)
        self.assertEqual(statuses.count(202), 10)
        # At most half the history can be reuse records within an hour.
        self.assertEqual(len(api.job_reuses), 2)
        self.assertLessEqual(len(api.jobs), 5)
        self.assertEqual(self.save.call_count, 3)


class GatewayScopeTests(ApiTestCase):
    def test_gateway_jobs_are_hidden_from_the_public_list(self):
        student = self.submit(lecture("aaaaaaaaaaa"), headers=GATEWAY).json()["job_id"]
        public = self.submit(lecture("bbbbbbbbbbb")).json()["job_id"]
        self.assertTrue(api.jobs[student]["via_gateway"])
        listed = {job["job_id"] for job in self.client.get("/jobs").json()["jobs"]}
        self.assertEqual(listed, {public})
        wrong_key = {job["job_id"] for job in self.client.get("/jobs", headers={"X-Gateway-Key": "nope"}).json()["jobs"]}
        self.assertEqual(wrong_key, {public})
        with_key = {job["job_id"] for job in self.client.get("/jobs", headers={"X-Gateway-Key": GATEWAY_KEY}).json()["jobs"]}
        self.assertEqual(with_key, {student, public})
        # Reading one job by its id stays open for older gateway deployments.
        self.assertEqual(self.client.get(f"/jobs/{student}").status_code, 200)

    def test_non_ascii_gateway_key_is_rejected_not_a_server_error(self):
        response = self.client.get("/jobs", headers={"X-Gateway-Key": "clé".encode("latin-1")})
        self.assertEqual(response.status_code, 200)

    def test_health_reports_queue_counts(self):
        api.jobs.update({
            "a": {"status": "running"}, "b": {"status": "queued"}, "c": {"status": "queued"}, "d": {"status": "completed"},
        })
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "active_jobs": 1, "queued_jobs": 2})


class CallbackTests(ApiTestCase):
    def test_callback_needs_a_gateway_key_and_https(self):
        self.submit(lecture("aaaaaaaaaaa", callback_url=CALLBACK))
        self.submit(lecture("bbbbbbbbbbb", callback_url="http://edufusion.example/cb"), headers=GATEWAY)
        self.submit(lecture("ccccccccccc", callback_url="ftp://edufusion.example/cb"), headers=GATEWAY)
        self.assertEqual(api.job_callbacks, {})
        with patch.dict(os.environ, {"CALLBACK_ALLOW_HTTP": "true"}):
            local = self.submit(lecture("ddddddddddd", callback_url="http://127.0.0.1:5000/cb"), headers=GATEWAY).json()
        self.assertEqual(api.job_callbacks[local["job_id"]][0][0], "http://127.0.0.1:5000/cb")

    def test_callback_keeps_only_a_key_fingerprint_and_never_reaches_the_job(self):
        job_id = self.submit(lecture(callback_url=CALLBACK), headers=GATEWAY).json()["job_id"]
        self.assertEqual(api.job_callbacks[job_id], [(CALLBACK, sha256(GATEWAY_KEY.encode()).hexdigest())])
        stored = json.dumps(api.jobs, default=str)
        self.assertNotIn(GATEWAY_KEY, stored)
        self.assertNotIn("callback", stored)
        # The pipeline only ever sees its own keyword arguments.
        self.assertNotIn("callback_url", self.executor.submit.call_args.args[2])

    def test_duplicate_submission_adds_its_callback_to_the_running_job(self):
        job_id = self.submit(lecture(callback_url=CALLBACK), headers=GATEWAY).json()["job_id"]
        other = "https://other.example/callback"
        self.submit(lecture(callback_url=other), headers=GATEWAY)
        self.submit(lecture(callback_url=other), headers=GATEWAY)
        self.assertEqual([url for url, _ in api.job_callbacks[job_id]], [CALLBACK, other])

    def test_signed_notice_retries_transient_failures(self):
        post = self.patch("requests")
        post.post.side_effect = [MagicMock(status_code=502), MagicMock(status_code=200)]
        sleeps = []
        with patch.object(api, "sleep", side_effect=sleeps.append), patch.object(api, "time", return_value=1700000000.9):
            delivered = api.deliver_callback(CALLBACK, api.key_fingerprint(GATEWAY_KEY), "job-1", "completed")
        self.assertTrue(delivered)
        self.assertEqual(sleeps, list(api.CALLBACK_RETRY_DELAYS[:2]))
        args, kwargs = post.post.call_args
        self.assertEqual(args, (CALLBACK,))
        self.assertEqual(kwargs["json"], {"event": "job.finished", "job_id": "job-1", "status": "completed"})
        self.assertEqual(kwargs["timeout"], 30)
        headers = kwargs["headers"]
        self.assertEqual(headers["X-LectureScribe-Timestamp"], "1700000000")
        expected = hmac.new(GATEWAY_KEY.encode(), b"1700000000.job-1.completed", sha256).hexdigest()
        self.assertEqual(headers["X-LectureScribe-Signature"], f"sha256={expected}")

    def test_notice_gives_up_on_rejection_or_unknown_key(self):
        post = self.patch("requests")
        self.patch("sleep")
        post.post.return_value = MagicMock(status_code=401)
        self.assertFalse(api.deliver_callback(CALLBACK, api.key_fingerprint(GATEWAY_KEY), "job-1", "failed"))
        self.assertEqual(post.post.call_count, 1)
        post.post.reset_mock()
        post.post.side_effect = OSError("connection refused")
        self.assertFalse(api.deliver_callback(CALLBACK, api.key_fingerprint(GATEWAY_KEY), "job-1", "failed"))
        self.assertEqual(post.post.call_count, len(api.CALLBACK_RETRY_DELAYS))
        post.post.reset_mock()
        self.assertFalse(api.deliver_callback(CALLBACK, api.key_fingerprint("rotated-away"), "job-1", "failed"))
        post.post.assert_not_called()

    def test_finished_job_notifies_every_callback_once(self):
        sent = []
        self.patch("deliver_callback", side_effect=lambda *args: sent.append(args))
        self.patch("start_background", side_effect=lambda target, *args: target(*args))
        job_id = self.submit(lecture(callback_url=CALLBACK), headers=GATEWAY).json()["job_id"]
        self.submit(lecture(callback_url="https://other.example/cb"), headers=GATEWAY)
        request_data = self.executor.submit.call_args.args[2]
        with patch.object(api, "process_youtube_url", return_value={"title": "Lecture", "format_version": api.FORMAT_VERSION}):
            api.run_transcription_job(job_id, request_data)
        fingerprint = api.key_fingerprint(GATEWAY_KEY)
        self.assertEqual(sent, [(CALLBACK, fingerprint, job_id, "completed"),
                                ("https://other.example/cb", fingerprint, job_id, "completed")])
        self.assertNotIn(job_id, api.job_callbacks)

    def test_retries_cover_a_gateway_outage_of_a_quarter_hour(self):
        self.assertGreaterEqual(sum(api.CALLBACK_RETRY_DELAYS), 900)

    def test_notice_in_flight_keeps_the_instance_awake_and_the_job_listed(self):
        started = []
        self.patch("start_background", side_effect=lambda target, *args: started.append((target, args)))
        delivered = self.patch("deliver_callback")
        api.jobs.update({f"done-{index}": {"status": "completed", "finished_at": index} for index in range(4)})
        with patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://ls.onrender.com"}):
            api.dispatch_callbacks("done-0", "completed", [(CALLBACK, api.key_fingerprint(GATEWAY_KEY))])
        self.assertEqual([target for target, _ in started], [api.deliver_tracked_callback, api.keepalive_loop])
        self.assertEqual(api.callback_deliveries, {"done-0": 1})
        with patch.object(api, "time", return_value=10_000.0):
            self.assertTrue(api.keepalive_needed())
        with patch.object(api, "JOB_HISTORY_LIMIT", 2):
            api.trim_job_history_unlocked()
        # The oldest job is kept while its notice is out; the next oldest go instead.
        self.assertEqual(set(api.jobs), {"done-0", "done-3"})
        target, args = started[0]
        target(*args)
        delivered.assert_called_once_with(CALLBACK, api.key_fingerprint(GATEWAY_KEY), "done-0", "completed")
        self.assertEqual(api.callback_deliveries, {})
        with patch.object(api, "time", return_value=10_000.0):
            self.assertFalse(api.keepalive_needed())

    def test_failed_job_notifies_with_failed_status(self):
        sent = []
        self.patch("deliver_callback", side_effect=lambda *args: sent.append(args))
        self.patch("start_background", side_effect=lambda target, *args: target(*args))
        job_id = self.submit(lecture(callback_url=CALLBACK), headers=GATEWAY).json()["job_id"]
        with patch.object(api, "process_youtube_url", side_effect=RuntimeError("Video unavailable")):
            api.run_transcription_job(job_id, self.executor.submit.call_args.args[2])
        self.assertEqual([args[3] for args in sent], ["failed"])
        self.assertEqual(api.jobs[job_id]["status"], "failed")


class KeepAliveTests(ApiTestCase):
    def test_disabled_without_render_url_or_when_switched_off(self):
        self.assertIsNone(api.keepalive_url())
        self.assertFalse(api.ensure_keepalive())
        with patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://lecturescribe.onrender.com/", "KEEPALIVE_DISABLED": "true"}):
            self.assertIsNone(api.keepalive_url())
        with patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://lecturescribe.onrender.com/"}):
            self.assertEqual(api.keepalive_url(), "https://lecturescribe.onrender.com/health")

    def test_pings_while_jobs_are_active_then_stops(self):
        api.jobs["job"] = {"status": "running"}
        http = self.patch("requests")
        waits = []

        def fake_sleep(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                api.jobs["job"]["status"] = "completed"

        with patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://ls.onrender.com", "KEEPALIVE_INTERVAL_SECONDS": "90"}), \
                patch.object(api, "sleep", side_effect=fake_sleep):
            api.keepalive_running = True
            api.keepalive_loop()
        self.assertEqual(waits, [90, 90, 90])
        self.assertEqual(http.get.call_count, 2)
        http.get.assert_called_with("https://ls.onrender.com/health", timeout=10)
        self.assertFalse(api.keepalive_running)

    def test_stays_awake_for_the_grace_period_after_the_last_job(self):
        now = [5000.0]
        api.jobs["job"] = {"status": "running"}
        with patch.object(api, "time", side_effect=lambda: now[0]):
            api.finish_job("job", status="completed")
        self.assertEqual(api.last_finished_at, 5000.0)
        http = self.patch("requests")
        waits = []

        def fake_sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        with patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://ls.onrender.com", "KEEPALIVE_INTERVAL_SECONDS": "240",
                                     "KEEPALIVE_GRACE_SECONDS": "900"}), \
                patch.object(api, "sleep", side_effect=fake_sleep), patch.object(api, "time", side_effect=lambda: now[0]):
            api.keepalive_running = True
            api.keepalive_loop()
        # Pings at +240, +480 and +720 s; at +960 s the 900 s grace is over.
        self.assertEqual(waits, [240] * 4)
        self.assertEqual(http.get.call_count, 3)
        self.assertFalse(api.keepalive_running)

    def test_keepalive_decision(self):
        api.jobs["job"] = {"status": "completed"}
        with patch.dict(os.environ, {"KEEPALIVE_GRACE_SECONDS": "900"}):
            api.last_finished_at = 1000.0
            for when, pending, needed in ((1899.0, False, True), (1900.0, False, False), (5000.0, True, True)):
                with self.subTest(when=when, pending=pending):
                    api.callback_deliveries.clear()
                    if pending:
                        api.callback_deliveries["job"] = 1
                    with patch.object(api, "time", return_value=when):
                        self.assertEqual(api.keepalive_needed(), needed)
            api.callback_deliveries.clear()
            api.jobs["job"]["status"] = "queued"
            with patch.object(api, "time", return_value=99_999.0):
                self.assertTrue(api.keepalive_needed())
        with patch.dict(os.environ, {"KEEPALIVE_GRACE_SECONDS": "0"}):
            api.jobs["job"]["status"] = "failed"
            with patch.object(api, "time", return_value=1000.0):
                self.assertFalse(api.keepalive_needed())

    def test_one_loop_at_a_time_and_ping_errors_are_ignored(self):
        started = []
        self.patch("start_background", side_effect=lambda target, *args: started.append(target))
        with patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://ls.onrender.com"}):
            self.submit(lecture("aaaaaaaaaaa"))
            self.submit(lecture("bbbbbbbbbbb"))
            self.assertEqual(started, [api.keepalive_loop])
            api.jobs.clear()
            api.jobs["job"] = {"status": "queued"}
            http = self.patch("requests")
            http.get.side_effect = OSError("offline")
            calls = []

            def fake_sleep(_seconds):
                calls.append(1)
                if len(calls) == 2:
                    api.jobs["job"]["status"] = "failed"

            with patch.object(api, "sleep", side_effect=fake_sleep):
                api.keepalive_loop()
        self.assertEqual(http.get.call_count, 1)
        self.assertFalse(api.keepalive_running)


class JobStateTests(ApiTestCase):
    def queue(self, job_id="job", **request):
        request_data = {"youtube_url": "https://youtu.be/nBDFtTDXLAs", "clean": True, "language": "ar", **request}
        api.jobs[job_id] = {"job_id": job_id, "status": "queued", "progress_percent": 0, "request": request_data,
                            "video_id": "nBDFtTDXLAs", "language": request_data["language"],
                            "mode": api.mode_of(request_data), "title": None, "submitted_at": 1.0}
        return request_data

    def test_pipeline_receives_only_its_own_arguments(self):
        request_data = self.queue(callback_url=CALLBACK, unexpected=True)
        seen = {}

        def pipeline(**kwargs):
            seen.update(kwargs)
            return {}

        with patch.object(api, "process_youtube_url", side_effect=pipeline):
            api.run_transcription_job("job", request_data)
        self.assertEqual(set(seen) - {"progress_callback"}, {"youtube_url", "clean", "language"})
        self.assertEqual(seen["language"], "ar")

    def test_title_shows_while_running_and_result_fills_the_contract(self):
        request_data = self.queue(language="auto")
        observed = {}

        def pipeline(progress_callback, **_kwargs):
            progress_callback("downloading", {"detail": "Audio download finished", "title": "محاضرة الشبكات",
                                              "video_duration_seconds": 3600})
            observed.update(api.jobs["job"])
            return {"title": "محاضرة الشبكات", "video_id": "nBDFtTDXLAs", "language": "auto",
                    "detected_language": "ar", "mode": "formatted", "format_version": api.FORMAT_VERSION,
                    "video_duration_seconds": 3600, "raw_transcript_path": "raw.txt"}

        with patch.object(api, "process_youtube_url", side_effect=pipeline):
            api.run_transcription_job("job", request_data)
        self.assertEqual(observed["title"], "محاضرة الشبكات")
        self.assertEqual(observed["video_duration_seconds"], 3600)
        job = api.jobs["job"]
        self.assertEqual(job["status"], "completed")
        for field, value in (("title", "محاضرة الشبكات"), ("language", "auto"), ("detected_language", "ar"),
                             ("mode", "formatted"), ("format_version", api.FORMAT_VERSION)):
            self.assertEqual(job[field], value, field)

    def test_result_from_an_older_pipeline_is_never_stamped_with_a_version(self):
        request_data = self.queue()
        with patch.object(api, "process_youtube_url", return_value={"raw_transcript_path": "raw.txt"}):
            api.run_transcription_job("job", request_data)
        job = api.jobs["job"]
        self.assertIsNone(job["format_version"])
        self.assertIsNone(job["result"]["format_version"])
        self.assertEqual(job["result"]["video_id"], "nBDFtTDXLAs")
        self.assertEqual(job["result"]["mode"], "formatted")

    def test_job_reports_the_mode_actually_produced(self):
        request_data = self.queue()
        with TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.txt"
            cleaned = Path(directory) / "fast.md"
            raw.write_text("نص المحاضرة", encoding="utf-8")
            cleaned.write_text("نص المحاضرة", encoding="utf-8")
            result = {"mode": "fast", "requested_mode": "formatted", "cleaner_provider": "formatter",
                      "format_version": api.FORMAT_VERSION, "detected_language": "ar",
                      "raw_transcript_path": str(raw), "cleaned_transcript_path": str(cleaned)}
            with patch.object(api, "process_youtube_url", return_value=result):
                api.run_transcription_job("job", request_data)
            job = api.jobs["job"]
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["mode"], "fast")
            self.assertTrue(job["request"]["clean"])
            # A formatted request runs the models again instead of reusing the fallback layout ...
            self.assertEqual(self.submit(lecture(language="ar")).json()["status"], "queued")
            # ... while a fast request may use it at once.
            fast = self.submit(lecture(language="ar", clean=False)).json()
        self.assertEqual(fast["status"], "completed")
        self.assertTrue(fast["cached"])

    def test_bookkeeping_errors_still_fail_the_job(self):
        request_data = self.queue()
        self.save.side_effect = OSError("disk full")
        with patch.object(api, "process_youtube_url") as pipeline:
            api.run_transcription_job("job", request_data)
        pipeline.assert_not_called()
        self.assertEqual(api.jobs["job"]["status"], "failed")
        self.assertIn("disk full", api.jobs["job"]["error"])

    def test_completion_errors_do_not_leave_the_job_running(self):
        request_data = self.queue()
        with patch.object(api, "process_youtube_url", return_value={}), \
                patch.object(api, "result_contract_fields", side_effect=RuntimeError("boom")):
            api.run_transcription_job("job", request_data)
        self.assertEqual(api.jobs["job"]["status"], "failed")
        self.assertIn("boom", api.jobs["job"]["error"])

    def test_progress_never_moves_backwards(self):
        request_data = self.queue()
        api.jobs["job"]["status"] = "running"
        seen = []
        for stage, details in (
            ("downloading", {}),
            ("transcribing", {"detail": "Preparing audio"}),
            ("transcribing", {"chunk_index": 1, "chunk_total": 4}),
            ("transcribing", {"chunk_index": 3, "chunk_total": 4}),
            ("formatting", {}),
            ("formatting", {"chunk_index": 0, "chunk_total": 5}),
            ("downloading", {}),
        ):
            api.update_job_progress("job", request_data, stage, details)
            seen.append(api.jobs["job"]["progress_percent"])
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[1], seen[2])

    def test_stage_start_is_kept_across_chunk_events(self):
        request_data = self.queue()
        with patch.object(api, "time", return_value=100.0):
            api.update_job_progress("job", request_data, "transcribing", {"chunk_index": 1, "chunk_total": 3})
        with patch.object(api, "time", return_value=160.0):
            api.update_job_progress("job", request_data, "transcribing", {"chunk_index": 2, "chunk_total": 3})
        self.assertEqual(api.jobs["job"]["stage_started_at"], 100.0)
        with patch.object(api, "time", return_value=200.0):
            api.update_job_progress("job", request_data, "formatting", {})
        self.assertEqual(api.jobs["job"]["stage_started_at"], 200.0)

    def test_remaining_time_from_the_pipeline_restarts_the_countdown(self):
        # OpenRouter reports "seconds left from now" with every part: 60 s per part left.
        request_data = self.queue()
        for when, part in ((100.0, 1), (460.0, 7)):
            details = {"chunk_index": part, "chunk_total": 12, "estimated_stage_seconds": 60 * (12 - part + 1)}
            with patch.object(api, "time", return_value=when):
                api.update_job_progress("job", request_data, "transcribing", details)
        job = api.jobs["job"]
        self.assertEqual(job["stage_started_at"], 460.0)
        # What both web clients show: start + estimate - now.
        self.assertEqual(job["stage_started_at"] + job["estimated_stage_seconds"] - 460.0, 360)

    def test_progress_writes_are_throttled_but_stage_changes_are_saved(self):
        request_data = self.queue()
        now = [10.0]
        with patch.object(api, "time", side_effect=lambda: now[0]):
            api.update_job_progress("job", request_data, "transcribing", {"chunk_index": 1, "chunk_total": 9})
            self.assertEqual(self.save.call_count, 1)
            for when, chunk in ((10.5, 2), (11.9, 3)):
                now[0] = when
                api.update_job_progress("job", request_data, "transcribing", {"chunk_index": chunk, "chunk_total": 9})
            self.assertEqual(self.save.call_count, 1)
            now[0] = 12.5
            api.update_job_progress("job", request_data, "transcribing", {"chunk_index": 4, "chunk_total": 9})
            self.assertEqual(self.save.call_count, 2)
            now[0] = 12.6
            api.update_job_progress("job", request_data, "formatting", {})
        self.assertEqual(self.save.call_count, 3)

    def test_a_failed_progress_write_does_not_abort_the_lecture(self):
        request_data = self.queue()
        self.save.side_effect = OSError("disk full")
        api.update_job_progress("job", request_data, "downloading", {})
        self.assertEqual(api.jobs["job"]["stage"], "downloading")

    def test_estimates_survive_jobs_being_added_concurrently(self):
        for index in range(3000):
            api.jobs[f"old-{index}"] = {"status": "completed", "request": {"clean": True}, "started_at": 1, "finished_at": 2}
        errors = []

        def writer():
            for index in range(3000):
                with api.jobs_lock:
                    api.jobs[f"new-{index}"] = {"status": "queued"}

        thread = Thread(target=writer)
        thread.start()
        try:
            for _ in range(200):
                try:
                    api.build_progress_update("formatting", {"clean": True}, {})
                except RuntimeError as exc:
                    errors.append(exc)
        finally:
            thread.join()
        self.assertEqual(errors, [])

    def test_history_keeps_the_newest_finished_jobs_and_every_active_one(self):
        for index in range(6):
            api.jobs[f"done-{index}"] = {"status": "completed", "finished_at": index}
        api.jobs["running"] = {"status": "running"}
        with patch.object(api, "JOB_HISTORY_LIMIT", 3):
            api.trim_job_history_unlocked()
        self.assertEqual(set(api.jobs), {"done-3", "done-4", "done-5", "running"})

    def test_restart_fails_running_jobs_and_resumes_queued_ones(self):
        with TemporaryDirectory() as directory:
            jobs_file = Path(directory) / "jobs.json"
            jobs_file.write_text(json.dumps({"jobs": {
                "running": {"job_id": "running", "status": "running", "request": {}},
                "queued": {"job_id": "queued", "status": "queued", "submitted_at": 5,
                           "request": {"youtube_url": "https://youtu.be/nBDFtTDXLAs", "clean": True, "language": "ar"}},
            }}), encoding="utf-8")
            with patch.object(api, "JOBS_FILE", jobs_file):
                loaded = api.load_jobs()
        self.assertEqual(loaded["running"]["status"], "failed")
        self.assertEqual(loaded["queued"]["status"], "queued")
        api.jobs.update(loaded)
        self.assertEqual(api.resume_queued_jobs(), 1)
        self.executor.submit.assert_called_once_with(api.run_transcription_job, "queued", loaded["queued"]["request"])


class TranscriptEndpointTests(ApiTestCase):
    def test_transcript_names_its_language_and_keeps_undecodable_bytes_visible(self):
        with TemporaryDirectory() as directory:
            job = self.completed_job(directory)
            Path(job["result"]["raw_transcript_path"]).write_bytes("نص".encode() + b"\xff")
            raw = self.client.get(f"/jobs/{job['job_id']}/transcript?kind=raw")
            cleaned = self.client.get(f"/jobs/{job['job_id']}/transcript")
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.headers["content-language"], "ar")
        self.assertEqual(raw.headers["x-transcript-format"], "plain")
        self.assertEqual(raw.text, "نص�")
        self.assertEqual(cleaned.headers["x-transcript-format"], "markdown")
        self.assertTrue(cleaned.headers["content-type"].startswith("text/plain"))

    def test_missing_file_does_not_reveal_server_paths(self):
        with TemporaryDirectory() as directory:
            job = self.completed_job(directory)
            Path(job["result"]["cleaned_transcript_path"]).unlink()
            response = self.client.get(f"/jobs/{job['job_id']}/transcript")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(directory, response.json()["detail"])
