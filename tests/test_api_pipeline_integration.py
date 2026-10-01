"""The real API driving the real pipeline, with only the network edges stubbed.

yt-dlp, OpenRouter Whisper and the OpenRouter chat model are replaced; everything
between them (request validation, the call into `process_youtube_url`, progress
events, result keys, caches, transcript files, callbacks and jobs.json) runs for
real in a temporary directory. No network, no Whisper model, no real sleeps.
"""

from collections import deque
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import clean_with_Llama as cleaner
import MainCode_FasterWhisper as pipeline
import transcript_format as tf


VIDEO_ID = "nBDFtTDXLAs"
URL = f"https://youtu.be/{VIDEO_ID}"
TITLE = "محاضرة هياكل البيانات"
GATEWAY_KEY = "edufusion-key"
GATEWAY = {"X-Gateway-Key": GATEWAY_KEY, "X-Gateway-User": "student-hash-1"}
CALLBACK = "https://edufusion.example/api/lecture-scribe/callback"
ARABIC = " ".join([
    "اليوم سنتحدث عن هياكل البيانات وأهميتها في البرمجة.",
    "نبدأ بالمصفوفات لأنها أبسط بنية لتخزين العناصر في الذاكرة.",
    "ثم ننتقل إلى القوائم المترابطة linked lists ونقارن بينها وبين المصفوفات.",
    "بعد ذلك نشرح الأشجار الثنائية وكيف نبحث فيها عن عنصر معين.",
] * 3)
ENGLISH = " ".join([
    "Today we talk about data structures and why they matter.",
    "We start with arrays because they store elements next to each other in memory.",
    "Then we move to linked lists and compare their complexity with arrays.",
] * 3)


class SynchronousExecutor:
    """Runs a submitted job at once, so the job has finished when POST /jobs returns."""

    def __init__(self):
        self.calls = 0

    def submit(self, fn, *args, **kwargs):
        self.calls += 1
        fn(*args, **kwargs)


def chunk_of(prompt: str) -> str:
    return prompt.split("Transcript chunk:\n", 1)[1].strip()


class ApiPipelineIntegrationTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(temporary.name)
        self.jobs_file = Path(temporary.name) / "jobs.json"

        self.executor = SynchronousExecutor()
        for name, value in (
            ("jobs", {}),
            ("job_submissions", deque()),
            ("job_submissions_by_client", {}),
            ("job_callbacks", {}),
            ("last_jobs_save", 0.0),
            ("keepalive_running", False),
            ("executor", self.executor),
            ("JOBS_FILE", self.jobs_file),
            ("JOB_RATE_PER_IP", 100),
        ):
            self.start(patch.object(api, name, value))
        self.callbacks = []
        self.start(patch.object(api, "deliver_callback", side_effect=lambda *args: self.callbacks.append(args)))
        self.start(patch.object(api, "start_background", side_effect=lambda target, *args: target(*args)))

        self.start(patch.dict(os.environ, {
            "GATEWAY_KEYS": GATEWAY_KEY,
            "OPENROUTER_API_KEY": "test-key",
            "WHISPER_BACKEND": "openrouter",
            "WHISPER_LOCAL_FALLBACK": "false",
            "OLLAMA_LOCAL_FALLBACK": "false",
            "RENDER": "",
        }))
        for name in ("RENDER_EXTERNAL_URL", "CALLBACK_ALLOW_HTTP"):
            os.environ.pop(name, None)

        self.spoken = ARABIC
        self.downloads = 0
        self.whisper_languages = []
        self.title_while_running = []
        self.prompts = []
        self.start(patch.object(pipeline.url_to_mp3, "download_youtube_mp3", side_effect=self.fake_download))
        self.start(patch.object(pipeline, "transcribe_with_openrouter", side_effect=self.fake_whisper))
        self.llm = self.start(patch.object(cleaner, "openrouter_generate", side_effect=self.editing_model))
        self.client = TestClient(api.app)

    def start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    # Network edges ---------------------------------------------------------
    def fake_download(self, url, skip_cache=False, return_metadata=False):
        self.downloads += 1
        audio = Path("downloads") / f"{VIDEO_ID}.mp3"
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b"audio")
        return str(audio), {"video_id": VIDEO_ID, "title": TITLE, "duration": 900}

    def fake_whisper(self, audio_path, *, language, video_duration_seconds=None, progress_callback=None):
        self.whisper_languages.append(language)
        running = [job for job in api.jobs.values() if job.get("status") == "running"]
        self.title_while_running.append(running[0].get("title") if running else None)
        progress_callback("transcribing", {"detail": "part 1 of 1", "chunk_index": 1, "chunk_total": 1,
                                           "transcription_provider": "openrouter"})
        name = "arabic" if self.spoken is ARABIC else "english"
        return self.spoken, {"provider": "openrouter", "detected_language": name, "segments": []}

    def editing_model(self, prompt, system=None):
        """A cooperative model: adds a heading and the usual chatty preamble."""
        self.prompts.append((system or "", prompt))
        return "إليك النص بعد التحرير:\n\n## مقدمة\n\n" + chunk_of(prompt)

    # Helpers -----------------------------------------------------------------
    def submit(self, headers=None, **body):
        response = self.client.post("/jobs", json={"youtube_url": URL, **body}, headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def job(self, job_id):
        return self.client.get(f"/jobs/{job_id}", headers=GATEWAY).json()

    def transcript(self, job_id, kind="cleaned"):
        return self.client.get(f"/jobs/{job_id}/transcript", params={"kind": kind})

    # Tests -------------------------------------------------------------------
    def test_formatted_arabic_lecture_end_to_end(self):
        submitted = self.submit(GATEWAY, clean=True, language="ar", callback_url=CALLBACK)
        job = self.job(submitted["job_id"])

        self.assertEqual(job["status"], "completed", job.get("error"))
        expected = {"title": TITLE, "video_id": VIDEO_ID, "language": "ar", "mode": "formatted",
                    "detected_language": "ar", "format_version": tf.FORMAT_VERSION,
                    "video_duration_seconds": 900, "progress_percent": 100, "via_gateway": True}
        for field, value in expected.items():
            self.assertEqual(job[field], value, field)
        for field in ("title", "video_id", "language", "detected_language", "mode", "format_version"):
            self.assertEqual(job["result"][field], job[field], field)
        self.assertEqual(job["result"]["cleaner_provider"], "openrouter")
        self.assertNotIn("callback_url", job["request"])
        self.assertEqual(self.whisper_languages, ["ar"])
        # The title reached the job record as soon as the download finished.
        self.assertEqual(self.title_while_running, [TITLE])

        # The cleaner was told to keep Arabic, never to translate it.
        self.assertTrue(self.prompts)
        for system, prompt in self.prompts:
            self.assertNotIn("English only", system)
            self.assertNotIn("Translate Arabic", system)
            self.assertIn("Arabic", prompt)

        cleaned = self.transcript(job["job_id"])
        self.assertEqual(cleaned.status_code, 200)
        self.assertEqual(cleaned.headers["content-language"], "ar")
        self.assertEqual(cleaned.headers["x-transcript-format"], "markdown")
        self.assertTrue(cleaned.text.startswith("## مقدمة"), cleaned.text[:80])
        self.assertNotIn("إليك", cleaned.text)
        self.assertIn("linked lists", cleaned.text)
        self.assertGreaterEqual(tf.arabic_ratio(cleaned.text), 0.8)

        raw = self.transcript(job["job_id"], "raw")
        self.assertEqual(raw.headers["x-transcript-format"], "plain")
        # The raw view only changes whitespace.
        self.assertEqual("".join(raw.text.split()), "".join(ARABIC.split()))
        for key in ("raw_transcript_path", "cleaned_transcript_path"):
            self.assertNotIn("محاضرة", Path(job["result"][key]).name)

        self.assertEqual(self.callbacks, [(CALLBACK, api.key_fingerprint(GATEWAY_KEY), job["job_id"], "completed")])
        saved = self.jobs_file.read_text(encoding="utf-8")
        self.assertNotIn(GATEWAY_KEY, saved)
        self.assertNotIn("callback", saved)
        self.assertEqual(json.loads(saved)["jobs"][job["job_id"]]["title"], TITLE)

    def test_repeat_requests_never_transcribe_again(self):
        first = self.submit(GATEWAY, clean=True, language="ar")
        text = self.transcript(first["job_id"]).text

        # The API answers from its own registry without queueing anything.
        again = self.submit(GATEWAY, clean=True, language="ar")
        self.assertEqual(again["status"], "completed")
        self.assertTrue(again["cached"])
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.transcript(again["job_id"]).text, text)

        # After a restart wiped the registry, the pipeline's cache answers instead.
        api.jobs.clear()
        restarted = self.job(self.submit(GATEWAY, clean=True, language="ar")["job_id"])
        self.assertEqual(self.executor.calls, 2)
        self.assertEqual(self.downloads, 1)
        self.assertTrue(restarted["result"]["used_cached_cleaned_transcript"])
        for field in ("title", "mode", "detected_language", "format_version"):
            self.assertEqual(restarted[field], {"title": TITLE, "mode": "formatted", "detected_language": "ar",
                                                "format_version": tf.FORMAT_VERSION}[field], field)
        self.assertEqual(self.transcript(restarted["job_id"]).text, text)

    def test_auto_detection_in_fast_mode_uses_no_model(self):
        self.spoken = ENGLISH
        job = self.job(self.submit(clean=False, language="auto")["job_id"])

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(self.whisper_languages, [None])
        self.llm.assert_not_called()
        for field, value in (("language", "auto"), ("detected_language", "en"), ("mode", "fast"),
                             ("format_version", tf.FORMAT_VERSION)):
            self.assertEqual(job[field], value, field)
        self.assertEqual(job["result"]["cleaner_provider"], "formatter")
        cleaned = self.transcript(job["job_id"])
        self.assertEqual(cleaned.headers["content-language"], "en")
        self.assertIn("linked lists", cleaned.text)
        # Public, non-gateway jobs stay in the public history.
        self.assertIn(job["job_id"], {item["job_id"] for item in self.client.get("/jobs").json()["jobs"]})

    def test_a_translating_model_never_turns_an_arabic_lecture_into_english(self):
        self.llm.side_effect = lambda prompt, system=None: (
            "Today we will talk about data structures and their importance in programming. " * 8
        )
        job = self.job(self.submit(GATEWAY, clean=True, language="ar")["job_id"])

        self.assertEqual(job["status"], "completed", job.get("error"))
        # Every chunk was rejected, so the lecture keeps its own words in the plain
        # layout and is reported (and cached) as fast, not as AI formatting.
        self.assertEqual(job["mode"], "fast")
        self.assertTrue(job["request"]["clean"])
        self.assertEqual(job["result"]["requested_mode"], "formatted")
        self.assertEqual(job["result"]["cleaner_provider"], "formatter")
        cleaned = self.transcript(job["job_id"]).text
        self.assertNotIn("Today we will talk", cleaned)
        self.assertGreaterEqual(tf.arabic_ratio(cleaned), 0.8)

        # Once the model behaves, a new formatted request formats again from the saved
        # Whisper text instead of reusing the fallback layout.
        self.llm.side_effect = self.editing_model
        retried = self.job(self.submit(GATEWAY, clean=True, language="ar")["job_id"])
        self.assertEqual(self.executor.calls, 2)
        self.assertEqual(self.downloads, 1)
        self.assertEqual(retried["mode"], "formatted")
        self.assertTrue(retried["result"]["used_cached_raw_transcript"])
        self.assertTrue(self.transcript(retried["job_id"]).text.startswith("## مقدمة"))

    def test_pipeline_failure_fails_the_job_and_notifies(self):
        def unavailable(*_args, **_kwargs):
            raise RuntimeError("ERROR: [youtube] nBDFtTDXLAs: Video unavailable")

        with patch.object(pipeline.url_to_mp3, "download_youtube_mp3", side_effect=unavailable):
            job = self.job(self.submit(GATEWAY, clean=True, language="ar", callback_url=CALLBACK)["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("cannot be reached", job["error"])
        self.assertEqual([args[3] for args in self.callbacks], ["failed"])
        self.assertEqual(self.client.get(f"/jobs/{job['job_id']}/transcript").status_code, 409)
