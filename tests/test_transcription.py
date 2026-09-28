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


class TranscriptionTests(unittest.TestCase):
    """Exercise cloud responses, local fallback, and the existing pipeline contract."""

    def setUp(self):
        """Isolate audio, output files, and credentials for each test."""
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

    def response(self, text="Original-language transcript", status=200):
        """Build a mock HTTP response consumed by the real response validation."""
        return Mock(ok=status == 200, status_code=status,
                    json=Mock(return_value={"text": text, "usage": {"seconds": 5}}))

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
        self.assertNotIn("Content-Type", arguments["headers"])
        self.assertTrue(arguments["files"]["file"][1].closed)

    def test_http_failures_switch_to_local(self):
        for status in (401, 402, 429, 500, 503):
            with self.subTest(status=status):
                with patch("openrouter_transcription.requests.post", return_value=self.response(status=status)):
                    with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("raw.txt"), {"provider": "local"})) as local:
                        _, info = pipeline.transcribe_audio(self.audio, video_duration_seconds=5)
                local.assert_called_once()
                self.assertIn(str(status), info["cloud_error"])

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
        for index in range(2):
            (self.chunk_dir / f"part-{index:06d}.mp3").write_bytes(b"audio")

    def test_chunks_join_in_order_and_temporary_files_are_removed(self):
        progress = Mock()
        with patch("openrouter_transcription.shutil.which", return_value="ffmpeg"):
            with patch("openrouter_transcription.subprocess.run", side_effect=self.make_chunks):
                with patch("openrouter_transcription.requests.post", side_effect=[self.response("first"), self.response("second")]):
                    text, info = transcribe_with_openrouter(self.audio, language="ar", video_duration_seconds=400, progress_callback=progress)
        self.assertEqual(text, "first\n\nsecond")
        self.assertEqual(info["audio_chunk_count"], 2)
        self.assertFalse(self.chunk_dir.exists())
        self.assertEqual(progress.call_args.args[1]["chunk_index"], 2)

    def test_failure_after_first_chunk_does_not_save_partial_transcript(self):
        with patch("openrouter_transcription.shutil.which", return_value="ffmpeg"):
            with patch("openrouter_transcription.subprocess.run", side_effect=self.make_chunks):
                with patch("openrouter_transcription.requests.post", side_effect=[self.response("partial"), self.response(status=503)]):
                    with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("local.txt"), {"provider": "local"})) as local:
                        pipeline.transcribe_audio(self.audio, video_duration_seconds=400)
        local.assert_called_once()
        self.assertFalse(Path("OutputForWhisper/lecture_transcript.txt").exists())
        self.assertFalse(self.chunk_dir.exists())

    def test_ffmpeg_failure_uses_local(self):
        with patch("openrouter_transcription.shutil.which", return_value="ffmpeg"):
            with patch("openrouter_transcription.subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
                with patch.object(pipeline, "transcribe_audio_local", return_value=(Path("raw.txt"), {"provider": "local"})) as local:
                    pipeline.transcribe_audio(self.audio, video_duration_seconds=400)
        local.assert_called_once()

    def test_local_cpu_fallback_uses_int8_and_original_prompt(self):
        model = Mock()
        model.transcribe.return_value = (
            [SimpleNamespace(text=" local speech ")],
            SimpleNamespace(language="ar", language_probability=0.98),
        )
        with patch("ctranslate2.get_cuda_device_count", return_value=0):
            with patch.object(pipeline, "get_whisper_model", return_value=model) as get_model:
                path, info = pipeline.transcribe_audio_local(self.audio, device="auto", compute_type="auto")
        self.assertEqual(get_model.call_args.kwargs["device"], "cpu")
        self.assertEqual(get_model.call_args.kwargs["compute_type"], "int8")
        self.assertEqual(model.transcribe.call_args.kwargs["initial_prompt"], pipeline.build_initial_prompt())
        self.assertEqual(path.read_text(encoding="utf-8"), "local speech")
        self.assertEqual(info["provider"], "local")

    def test_both_modes_use_cloud_and_still_clean_and_delete_audio(self):
        for clean in (False, True):
            with self.subTest(clean=clean):
                self.audio.write_bytes(b"test audio")
                with patch.object(pipeline.url_to_mp3, "download_youtube_mp3", return_value=(str(self.audio), {"duration": 5})):
                    with patch("openrouter_transcription.requests.post", return_value=self.response()):
                        with patch.object(pipeline, "clean_transcript_with_preferred_model", return_value=(None, None, "test")) as cleaner:
                            result = pipeline.process_youtube_url("https://youtu.be/nBDFtTDXLAs", clean=clean, use_cached_outputs=False)
                self.assertEqual(result["transcription_provider"], "openrouter")
                self.assertTrue(Path(result["raw_transcript_path"]).exists())
                self.assertTrue(result["audio_deleted"])
                cleaner.assert_called_once()
                self.assertEqual(cleaner.call_args.kwargs["allow_ollama_fallback"], clean)

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
