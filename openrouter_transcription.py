import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable

import requests


TRANSCRIPTION_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
TRANSCRIPTION_MODEL = "openai/whisper-large-v3"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class CloudTranscriptionUnavailable(RuntimeError):
    """Signal that the caller should try local transcription instead."""


def transcribe_with_openrouter(
    audio_path: Path,
    *,
    language: str | None,
    video_duration_seconds: int | float | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Transcribe original-language audio with OpenRouter, without saving partial text.

    Args:
        audio_path: Existing audio file from the downloader.
        language: Input language code, or None for automatic detection.
        video_duration_seconds: Known lecture length, used to decide whether to split.
        progress_callback: Optional API job-progress callback.
    Returns:
        Complete transcript text and provider/model/usage metadata.
    Workflow:
        Splits long or oversized audio into temporary five-minute MP3s with FFmpeg,
        uploads each sequentially, validates responses, and joins their text in order.
        Temporary files are removed on both success and failure.
    Connects to:
        Called by MainCode_FasterWhisper.transcribe_audio before local fallback.
    Raises:
        CloudTranscriptionUnavailable: Missing credentials, invalid settings, media
            conversion failure, timeout, HTTP error, or invalid/empty response.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise CloudTranscriptionUnavailable("OPENROUTER_API_KEY is not set.")

    model = os.environ.get("OPENROUTER_TRANSCRIPTION_MODEL", TRANSCRIPTION_MODEL).strip()
    try:
        chunk_seconds = int(os.environ.get("OPENROUTER_AUDIO_CHUNK_SECONDS", "300"))
        timeout_seconds = int(os.environ.get("OPENROUTER_TRANSCRIPTION_TIMEOUT_SECONDS", "120"))
        if chunk_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError
    except ValueError as exc:
        raise CloudTranscriptionUnavailable("Audio chunk size and timeout must be positive integers.") from exc

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://lecturescribe.app"),
        "X-Title": os.environ.get("OPENROUTER_APP_NAME", "LectureScribe AI"),
    }
    needs_split = (
        video_duration_seconds is None
        or float(video_duration_seconds) > chunk_seconds
        or audio_path.stat().st_size > MAX_UPLOAD_BYTES
    )
    with TemporaryDirectory(prefix="lecturescribe-audio-") as temporary_dir:
        chunks = [audio_path]
        if needs_split:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise CloudTranscriptionUnavailable("FFmpeg is required to split cloud transcription audio.")
            if progress_callback:
                progress_callback("transcribing", {
                    "detail": "Preparing audio for cloud transcription",
                    "transcription_provider": "openrouter",
                    "estimated_stage_seconds": 60,
                })
            try:
                subprocess.run(
                    [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000",
                     "-c:a", "libmp3lame", "-b:a", "32k", "-f", "segment",
                     "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
                     str(Path(temporary_dir) / "part-%06d.mp3")],
                    check=True, capture_output=True, timeout=600,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise CloudTranscriptionUnavailable("Could not prepare audio chunks with FFmpeg.") from exc
            chunks = sorted(Path(temporary_dir).glob("part-*.mp3"))
            if not chunks:
                raise CloudTranscriptionUnavailable("FFmpeg produced no audio chunks.")

        texts = []
        usages = []
        for index, chunk in enumerate(chunks, start=1):
            if chunk.stat().st_size > MAX_UPLOAD_BYTES:
                raise CloudTranscriptionUnavailable("Audio chunk exceeds the 25 MB upload limit.")
            detail = f"Whisper is transcribing your lecture (part {index} of {len(chunks)})"
            print(f"OpenRouter transcription: part {index}/{len(chunks)} with {model}")
            if progress_callback:
                progress_callback("transcribing", {
                    "detail": detail,
                    "transcription_provider": "openrouter",
                    "chunk_index": index,
                    "chunk_total": len(chunks),
                    "video_duration_seconds": video_duration_seconds,
                    "estimated_stage_seconds": 60 * (len(chunks) - index + 1),
                })
            data = {"model": model, "response_format": "json", "temperature": "0"}
            if language:
                data["language"] = language
            try:
                with chunk.open("rb") as audio_file:
                    response = requests.post(
                        TRANSCRIPTION_URL,
                        headers=headers,
                        data=data,
                        files={"file": (chunk.name, audio_file, "audio/mpeg")},
                        timeout=(15, timeout_seconds),
                    )
            except requests.RequestException as exc:
                raise CloudTranscriptionUnavailable(
                    f"OpenRouter audio request failed ({type(exc).__name__})."
                ) from exc
            if not response.ok:
                raise CloudTranscriptionUnavailable(
                    f"OpenRouter audio request returned HTTP {response.status_code}."
                )
            try:
                result = response.json()
            except ValueError as exc:
                raise CloudTranscriptionUnavailable("OpenRouter audio response was not JSON.") from exc
            if not isinstance(result, dict):
                raise CloudTranscriptionUnavailable("OpenRouter audio response has an invalid shape.")
            text = result.get("text")
            if not isinstance(text, str) or not text.strip():
                raise CloudTranscriptionUnavailable("OpenRouter returned an empty audio transcript.")
            texts.append(text.strip())
            usages.append(result.get("usage"))

    return "\n\n".join(texts), {
        "provider": "openrouter",
        "model": model,
        "requested_language": language,
        "detected_language": None,
        "language_probability": None,
        "video_duration_seconds": video_duration_seconds,
        "audio_chunk_count": len(texts),
        "usage": usages,
    }
