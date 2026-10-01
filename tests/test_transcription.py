import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

import MainCode_FasterWhisper as pipeline
from clean_with_Llama import load_local_env
from openrouter_transcription import CloudTranscriptionUnavailable, transcribe_with_openrouter


ARABIC_TEXT = "اليوم نشرح هياكل البيانات ونبدأ بالمصفوفات ثم القوائم المترابطة والأشجار"
# Parts with enough speech (over 100 letters) for their language to count.
ARABIC_PART = (
    "نبدأ اليوم بشرح هياكل البيانات الأساسية مثل المصفوفات والقوائم المترابطة، ثم ننتقل إلى "
    "الأشجار الثنائية وكيف نبحث فيها عن عنصر معين بسرعة كبيرة."
)
ARABIC_PART_2 = (
    "بعد ذلك نحل مجموعة من التمارين على الأشجار ونكتب الكود الخاص بكل عملية من عمليات "
    "الإضافة والحذف والبحث، ونحسب التعقيد الزمني لكل واحدة منها."
)
POLISH_PART = (
    "Dzisiaj omówimy struktury danych, zaczynając od tablic i list wiązanych, a potem przejdziemy "
    "do drzew binarnych i sposobów szybkiego wyszukiwania elementów."
)


class TranscriptionTests(unittest.TestCase):
    """Exercise cloud responses, local fallback, and the existing pipeline contract."""

    def setUp(self):
        """Isolate audio, output files, credentials and retry sleeps for each test."""
        temporary_dir = TemporaryDirectory()
        self.addCleanup(temporary_dir.cleanup)
        previous_dir = Path.cwd()
        self.addCleanup(os.chdir, previous_dir)
        os.chdir(temporary_dir.name)
        self.audio = Path("lecture.mp3")
        self.audio.write_bytes(b"test audio")
        environment = patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "test-key",
            "WHISPER_BACKEND": "openrouter",
            "WHISPER_LOCAL_FALLBACK": "true",
            "OPENROUTER_TRANSCRIPTION_MODEL": "openai/whisper-large-v3",
            "OPENROUTER_AUDIO_CHUNK_SECONDS": "300",
            "OPENROUTER_TRANSCRIPTION_TIMEOUT_SECONDS": "120",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for target in ("openrouter_transcription.time.sleep", "clean_with_Llama.time.sleep"):
            sleeper = patch(target)
            sleeper.start()
            self.addCleanup(sleeper.stop)

    def response(self, text="Original-language transcript", status=200, **extra):
        """Build a mock HTTP response consumed by the real response validation."""
        return Mock(ok=status == 200, status_code=status, headers={},
                    json=Mock(return_value={"text": text, "usage": {"seconds": 5}, **extra}))

    def test_cloud_default_saves_raw_text_without_loading_local_model(self):
        with patch("openrouter_transcription.requests.post", return_value=self.response()) as post:
            with patch.object(pipeline, "get_whisper_model") as model:
                path, info = pipeline.transcribe_audio(self.audio, video_duration_seconds=5)
        self.assertEqual(path, Path("OutputForWhisper/lecture_transcript.txt"))
        self.assertEqual(path.read_text(encoding="utf-8"), "Original-language transcript")
        self.assertEqual(info["provider"], "openrouter")
        model.assert_not_called()
        arguments = post.call_args.kwargs
        self.assertEqual(arguments["data"]["model"], "openai/whisper-large-v3")
        self.assertEqual(arguments["data"]["language"], "ar")
        self.assertEqual(arguments["data"]["response_format"], "verbose_json")
        self.assertNotIn("Content-Type", arguments["headers"])
        self.assertTrue(arguments["files"]["file"][1].closed)

    def test_http_failures_switch_to_local(self):
        for status in (401, 402, 429, 500, 503):
            with self.subTest(status=status):
                with patch("openrouter_transcription.requests.post", return_value=self.response(status=status)) as post:
                    with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("raw.txt"), {"provider": "local"})) as local:
                        _, info = pipeline.transcribe_audio(self.audio, video_duration_seconds=5)
                local.assert_called_once()
                self.assertIn(str(status), info["cloud_error"])
                # Only transient statuses are retried; auth and payment errors are final.
                self.assertEqual(post.call_count, 3 if status >= 429 else 1)

    def test_transient_failure_is_retried_before_succeeding(self):
        responses = [self.response(status=503), self.response("recovered text")]
        with patch("openrouter_transcription.requests.post", side_effect=responses) as post:
            text, info = transcribe_with_openrouter(self.audio, language="en", video_duration_seconds=5)
        self.assertEqual(text, "recovered text")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(info["provider"], "openrouter")

    def test_timeout_switches_to_local(self):
        with patch("openrouter_transcription.requests.post", side_effect=requests.Timeout):
            with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("raw.txt"), {"provider": "local"})) as local:
                _, info = pipeline.transcribe_audio(self.audio, video_duration_seconds=5)
        local.assert_called_once()
        self.assertIn("Timeout", info["cloud_error"])

    def test_missing_key_uses_local_without_network(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            with patch("openrouter_transcription.requests.post") as post:
                with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("raw.txt"), {"provider": "local"})) as local:
                    pipeline.transcribe_audio(self.audio, video_duration_seconds=5)
        post.assert_not_called()
        local.assert_called_once()

    def test_empty_or_invalid_responses_are_rejected(self):
        for value in ({"text": " "}, {"text": None}, [], {"error": "failed"}):
            with self.subTest(value=value):
                response = self.response()
                response.json.return_value = value
                with patch("openrouter_transcription.requests.post", return_value=response):
                    with self.assertRaises(CloudTranscriptionUnavailable):
                        transcribe_with_openrouter(self.audio, language="ar", video_duration_seconds=5)
        response.json.side_effect = ValueError("invalid JSON")
        with patch("openrouter_transcription.requests.post", return_value=response):
            with self.assertRaises(CloudTranscriptionUnavailable):
                transcribe_with_openrouter(self.audio, language="ar", video_duration_seconds=5)

    def test_fallback_can_be_disabled_on_cloud_servers(self):
        with patch.dict(os.environ, {"WHISPER_LOCAL_FALLBACK": "false"}):
            with patch("openrouter_transcription.requests.post", return_value=self.response(status=402)):
                with patch.object(pipeline, "transcribe_audio_local") as local:
                    with self.assertRaises(CloudTranscriptionUnavailable):
                        pipeline.transcribe_audio(self.audio, video_duration_seconds=5)
        local.assert_not_called()

    def test_ollama_fallback_can_be_disabled_on_cloud_servers(self):
        with patch.dict(os.environ, {"OLLAMA_LOCAL_FALLBACK": "false"}):
            with patch.object(pipeline, "clean_transcript_file_with_openrouter", side_effect=RuntimeError("cloud unavailable")):
                with patch.object(pipeline, "clean_transcript_file") as local:
                    cleaned, provider, error = pipeline.clean_transcript_with_preferred_model(
                        Path("raw.txt"), allow_ollama_fallback=True
                    )
        self.assertIsNone(cleaned)
        self.assertIsNone(provider)
        self.assertIn("cloud unavailable", error)
        local.assert_not_called()

    def test_ollama_failure_is_reported_instead_of_failing_the_job(self):
        with patch.dict(os.environ, {"OLLAMA_LOCAL_FALLBACK": "true"}):
            with patch.object(pipeline, "clean_transcript_file_with_openrouter", side_effect=RuntimeError("cloud unavailable")):
                with patch.object(pipeline, "clean_transcript_file", side_effect=RuntimeError("ollama down")):
                    cleaned, provider, error = pipeline.clean_transcript_with_preferred_model(
                        Path("raw.txt"), allow_ollama_fallback=True, language="ar"
                    )
        self.assertIsNone(cleaned)
        self.assertIsNone(provider)
        self.assertIn("cloud unavailable", error)
        self.assertIn("ollama down", error)

    def test_explicit_local_backend_skips_cloud(self):
        with patch.dict(os.environ, {"WHISPER_BACKEND": "local"}):
            with patch.object(pipeline, "transcribe_with_openrouter") as cloud:
                with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("raw.txt"), {"provider": "local"})) as local:
                    pipeline.transcribe_audio(self.audio)
        cloud.assert_not_called()
        local.assert_called_once()

    def make_chunks(self, arguments, **kwargs):
        """Emulate FFmpeg writing ordered segments for upload and cleanup checks."""
        self.chunk_dir = Path(arguments[-1]).parent
        for index in range(self.chunk_count):
            (self.chunk_dir / f"part-{index:06d}.mp3").write_bytes(b"audio")

    chunk_count = 2

    def test_chunks_join_in_order_and_temporary_files_are_removed(self):
        progress = Mock()
        with patch("openrouter_transcription.shutil.which", return_value="ffmpeg"):
            with patch("openrouter_transcription.subprocess.run", side_effect=self.make_chunks):
                with patch("openrouter_transcription.requests.post", side_effect=[self.response("first"), self.response("second")]):
                    text, info = transcribe_with_openrouter(self.audio, language="ar", video_duration_seconds=400, progress_callback=progress)
        # Parts join with a space: a 5-minute cut is not a paragraph boundary.
        self.assertEqual(text, "first second")
        self.assertEqual(info["audio_chunk_count"], 2)
        self.assertFalse(self.chunk_dir.exists())
        self.assertEqual(progress.call_args.args[1]["chunk_index"], 2)

    def test_failure_after_first_chunk_does_not_save_partial_transcript(self):
        responses = [self.response("partial")] + [self.response(status=503)] * 3
        with patch("openrouter_transcription.shutil.which", return_value="ffmpeg"):
            with patch("openrouter_transcription.subprocess.run", side_effect=self.make_chunks):
                with patch("openrouter_transcription.requests.post", side_effect=responses):
                    with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("local.txt"), {"provider": "local"})) as local:
                        pipeline.transcribe_audio(self.audio, video_duration_seconds=400)
        local.assert_called_once()
        self.assertFalse(Path("OutputForWhisper/lecture_transcript.txt").exists())
        self.assertFalse(self.chunk_dir.exists())

    def test_silent_part_is_skipped_instead_of_failing(self):
        self.chunk_count = 3
        responses = [self.response("first"), self.response(""), self.response("third")]
        with patch("openrouter_transcription.shutil.which", return_value="ffmpeg"):
            with patch("openrouter_transcription.subprocess.run", side_effect=self.make_chunks):
                with patch("openrouter_transcription.requests.post", side_effect=responses):
                    text, info = transcribe_with_openrouter(self.audio, language="en", video_duration_seconds=700)
        self.assertEqual(text, "first third")
        self.assertEqual(info["audio_chunk_count"], 3)

    def transcribe_parts(self, responses, language="auto", duration=900):
        """Run a split cloud transcription; returns `(text, info, sent form data per call)`."""
        self.chunk_count = 3 if duration > 600 else 2
        with patch("openrouter_transcription.shutil.which", return_value="ffmpeg"):
            with patch("openrouter_transcription.subprocess.run", side_effect=self.make_chunks):
                with patch("openrouter_transcription.requests.post", side_effect=responses) as post:
                    text, info = transcribe_with_openrouter(self.audio, language=language, video_duration_seconds=duration)
        sent = [dict(call.kwargs["data"], file=call.kwargs["files"]["file"][0]) for call in post.call_args_list]
        return text, info, sent

    def test_auto_language_parts_that_agree_need_no_extra_call(self):
        responses = [
            self.response(ARABIC_TEXT, language="arabic", duration=300.0,
                          segments=[{"start": 0.0, "end": 4.0, "text": ARABIC_TEXT}]),
            self.response("ثم ننتقل إلى الرسوم البيانية", language="arabic", duration=100.0,
                          segments=[{"start": 1.0, "end": 3.0, "text": "ثم ننتقل إلى الرسوم البيانية"}]),
        ]
        text, info, sent = self.transcribe_parts(responses, duration=400)
        self.assertEqual(len(sent), 2)
        for data in sent:
            self.assertNotIn("language", data)
        self.assertEqual(sent[0]["response_format"], "verbose_json")
        self.assertEqual(info["detected_language"], "ar")
        self.assertIsNone(info["requested_language"])
        # Segment times are made absolute across parts.
        self.assertEqual([segment["start"] for segment in info["segments"]], [0.0, 301.0])
        self.assertIn(ARABIC_TEXT, text)

    def test_auto_language_english_opening_is_transcribed_again_in_the_lecture_language(self):
        opening = ("Welcome to the computer networks course. Today we explain how routers forward "
                   "packets between networks and how the routing table is built.")
        opening_in_arabic = "أهلا بكم في مقرر شبكات الحاسوب، اليوم نشرح كيف يمرر الـ router الحزم بين الشبكات وكيف يبنى جدول التوجيه."
        responses = [
            self.response(opening, language="english", duration=300.0,
                          segments=[{"start": 0.0, "end": 9.0, "text": opening}]),
            self.response(ARABIC_PART, language="arabic", duration=300.0),
            self.response(ARABIC_PART_2, language="arabic", duration=120.0),
            self.response(opening_in_arabic, language="arabic", duration=300.0,
                          segments=[{"start": 0.5, "end": 9.5, "text": opening_in_arabic}]),
        ]
        text, info, sent = self.transcribe_parts(responses)
        self.assertEqual(len(sent), 4)
        for data in sent[:3]:
            self.assertNotIn("language", data)
        self.assertEqual(sent[3]["language"], "ar")
        self.assertEqual(sent[3]["file"], sent[0]["file"])
        self.assertTrue(text.startswith(opening_in_arabic))
        self.assertNotIn("Welcome", text)
        self.assertEqual(info["detected_language"], "ar")
        self.assertEqual(info["segments"][0], {"start": 0.5, "end": 9.5, "text": opening_in_arabic})

    def test_silence_fillers_never_force_an_arabic_lecture_into_english(self):
        for filler in (
            "Thank you. Thank you. Thank you. Thank you.",
            "Thank you for watching. Please subscribe to the channel.",
            "Subtitles by the Amara.org community",
        ):
            with self.subTest(filler=filler):
                responses = [
                    self.response(filler, language="english", duration=300.0),
                    self.response(ARABIC_PART, language="arabic", duration=300.0),
                    self.response(ARABIC_PART_2, language="arabic", duration=120.0),
                ]
                text, info, sent = self.transcribe_parts(responses)
                self.assertEqual([data.get("language") for data in sent], [None, None, None])
                self.assertEqual(info["detected_language"], "ar")
                self.assertIn(ARABIC_PART, text)

    def test_latin_lecture_of_unknown_language_is_never_forced_to_english(self):
        # json fallback: the provider reports no language at all.
        responses = [self.response(status=400)] + [self.response(POLISH_PART, duration=300.0)] * 3
        _text, _info, sent = self.transcribe_parts(responses)
        self.assertEqual(sent[-1]["response_format"], "json")
        self.assertTrue(all("language" not in data for data in sent))
        # Whisper's names are mapped ("polish" -> "pl"); a part it took for English is
        # transcribed again in the lecture's language, never the reverse.
        responses = [
            self.response(POLISH_PART, language="polish", duration=300.0),
            self.response(POLISH_PART, language="polish", duration=300.0),
            self.response("We continue with linked lists and their operations, then we compare the cost of "
                          "insertion and deletion with the cost for arrays.", language="english", duration=120.0),
            self.response(POLISH_PART, language="polish", duration=120.0),
        ]
        _text, info, sent = self.transcribe_parts(responses)
        self.assertEqual([data.get("language") for data in sent], [None, None, None, "pl"])
        self.assertEqual(info["detected_language"], "pl")

    def test_explicit_language_is_sent_with_every_part_and_nothing_is_repeated(self):
        responses = [self.response("Welcome to the course.", language="english"), self.response(ARABIC_PART, language="arabic")]
        _text, info, sent = self.transcribe_parts(responses, language="ar", duration=400)
        self.assertEqual([data["language"] for data in sent], ["ar", "ar"])
        self.assertEqual(info["requested_language"], "ar")

    def test_verbose_json_rejection_falls_back_to_json(self):
        rejected = self.response(status=400)
        with patch("openrouter_transcription.requests.post", side_effect=[rejected, self.response("plain")]) as post:
            text, info = transcribe_with_openrouter(self.audio, language=None, video_duration_seconds=5)
        formats = [call.kwargs["data"]["response_format"] for call in post.call_args_list]
        self.assertEqual(formats, ["verbose_json", "json"])
        self.assertEqual(text, "plain")
        self.assertEqual(info["response_format"], "json")
        self.assertEqual(info["segments"], [])

    def test_ffmpeg_failure_uses_local(self):
        with patch("openrouter_transcription.shutil.which", return_value="ffmpeg"):
            with patch("openrouter_transcription.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
                with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("raw.txt"), {"provider": "local"})) as local:
                    pipeline.transcribe_audio(self.audio, video_duration_seconds=400)
        local.assert_called_once()

    def local_model(self, *segments):
        model = Mock()
        model.transcribe.return_value = (
            list(segments) or [SimpleNamespace(text=" local speech ")],
            SimpleNamespace(language="ar", language_probability=0.98),
        )
        return model

    def test_local_cpu_fallback_uses_int8_and_language_prompt(self):
        model = self.local_model()
        # A stand-in module keeps this test independent of the heavy ctranslate2 wheel.
        fake_ctranslate2 = SimpleNamespace(get_cuda_device_count=lambda: 0)
        with patch.dict(sys.modules, {"ctranslate2": fake_ctranslate2}):
            with patch.object(pipeline, "get_whisper_model", return_value=model) as get_model:
                path, info = pipeline.transcribe_audio_local(self.audio, device="auto", compute_type="auto")
        self.assertEqual(get_model.call_args.kwargs["device"], "cpu")
        self.assertEqual(get_model.call_args.kwargs["compute_type"], "int8")
        kwargs = model.transcribe.call_args.kwargs
        self.assertEqual(kwargs["initial_prompt"], pipeline.build_initial_prompt("ar"))
        self.assertFalse(kwargs["condition_on_previous_text"])
        self.assertEqual(path.read_text(encoding="utf-8"), "local speech")
        self.assertEqual(info["provider"], "local")

    def test_initial_prompt_follows_the_lecture_language(self):
        arabic = pipeline.build_initial_prompt("ar")
        english = pipeline.build_initial_prompt("en")
        self.assertNotIn("Arabic", arabic)
        self.assertRegex(arabic, "[؀-ۿ]")
        self.assertNotRegex(english, "[؀-ۿ]")
        self.assertIsNone(pipeline.build_initial_prompt(None))
        self.assertIsNone(pipeline.build_initial_prompt("auto"))

    def test_local_auto_language_lets_whisper_detect_and_paragraphs_use_segments(self):
        segments = [
            SimpleNamespace(start=0.0, end=10.0, text=" ".join(["first"] * 25) + "."),
            SimpleNamespace(start=10.2, end=20.0, text=" ".join(["second"] * 25) + "."),
            SimpleNamespace(start=25.0, end=30.0, text="After a long pause the topic changes."),
        ]
        model = self.local_model(*segments)
        with patch.object(pipeline, "get_whisper_model", return_value=model):
            path, info = pipeline.transcribe_audio_local(
                self.audio, device="cpu", compute_type="int8", language="auto"
            )
        kwargs = model.transcribe.call_args.kwargs
        self.assertIsNone(kwargs["language"])
        self.assertNotIn("initial_prompt", kwargs)
        raw = path.read_text(encoding="utf-8")
        self.assertEqual(raw.split(), " ".join(segment.text for segment in segments).split())
        self.assertEqual(raw.count("\n\n"), 1)
        self.assertTrue(raw.split("\n\n")[1].startswith("After a long pause"))
        self.assertEqual(len(info["segments"]), 3)

    def test_fast_mode_formats_without_llm_and_formatted_mode_cleans_in_language(self):
        for clean in (False, True):
            with self.subTest(clean=clean):
                self.audio.write_bytes(b"test audio")
                with patch.object(pipeline.url_to_mp3, "download_youtube_mp3",
                                  return_value=(str(self.audio), {"duration": 5, "title": "محاضرة"})):
                    with patch("openrouter_transcription.requests.post", return_value=self.response(ARABIC_TEXT, language="arabic")):
                        with patch.object(pipeline, "clean_transcript_with_preferred_model", return_value=(None, None, "test")) as cleaner:
                            result = pipeline.process_youtube_url(
                                "https://youtu.be/nBDFtTDXLAs", clean=clean, use_cached_outputs=False, language="ar"
                            )
                self.assertEqual(result["transcription_provider"], "openrouter")
                self.assertTrue(Path(result["raw_transcript_path"]).exists())
                self.assertTrue(Path(result["cleaned_transcript_path"]).exists())
                self.assertTrue(result["audio_deleted"])
                self.assertEqual(result["title"], "محاضرة")
                self.assertEqual(result["detected_language"], "ar")
                if clean:
                    cleaner.assert_called_once()
                    self.assertEqual(cleaner.call_args.kwargs["language"], "ar")
                    self.assertTrue(cleaner.call_args.kwargs["allow_ollama_fallback"])
                else:
                    cleaner.assert_not_called()
                # Without any model output the result is the deterministic (fast) layout.
                self.assertEqual(result["mode"], "fast")
                self.assertEqual(result["requested_mode"], "formatted" if clean else "fast")
                self.assertEqual(result["cleaner_provider"], "formatter")

    def test_server_environment_takes_precedence_over_dotenv(self):
        path = Path(".env")
        path.write_text("OPENROUTER_API_KEY=old-file-key\nTEST_NEW_VALUE=from-file\n", encoding="utf-8")
        with patch.dict(os.environ):
            load_local_env(path)
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "test-key")
            self.assertEqual(os.environ["TEST_NEW_VALUE"], "from-file")

    def test_missing_audio_raises_without_calling_either_provider(self):
        with patch.object(pipeline, "transcribe_with_openrouter") as cloud:
            with patch.object(pipeline, "transcribe_audio_local") as local:
                with self.assertRaises(FileNotFoundError):
                    pipeline.transcribe_audio(Path("missing.mp3"))
        cloud.assert_not_called()
        local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
