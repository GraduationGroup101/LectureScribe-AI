import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
from typing import Any, Callable

import requests

from transcript_format import (
    ARABIC_SCRIPT_LANGUAGES,
    collapse_loops,
    detect_text_language,
    language_name,
    letter_count,
    normalize_language,
    resolve_detected_language,
    strip_hallucinations,
)


TRANSCRIPTION_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
TRANSCRIPTION_MODEL = "openai/whisper-large-v3"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# verbose_json adds the detected language and timed segments; providers that do not
# support it are asked again with plain json.
RESPONSE_FORMATS = ("verbose_json", "json")
FORMAT_REJECTED_STATUSES = frozenset({400, 415, 422})
# Seconds slept between attempts for one audio part; transient failures only.
RETRY_DELAYS = (2, 6)
MAX_RETRY_AFTER_SECONDS = 30
TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# Letters of real speech (after Whisper's silence fillers are removed) a part needs
# before Whisper's language verdict for it counts. "Thank you." x4 over silence has
# 32 letters; five minutes of speech have thousands.
SPEECH_LETTERS = 100


class CloudTranscriptionUnavailable(RuntimeError):
    """Signal that the caller should try local transcription instead."""


def _retry_after_seconds(response: Any) -> float | None:
    """Read a numeric Retry-After header, capped so one part cannot stall the job."""
    try:
        value = response.headers.get("Retry-After")
    except AttributeError:
        return None
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _post_audio_part(
    chunk: Path,
    *,
    model: str,
    headers: dict[str, str],
    timeout_seconds: int,
    language: str | None,
    response_format: str,
) -> tuple[dict[str, Any], str]:
    """Upload one audio part, retrying transient failures.

    Returns:
        `(json_body, response_format)`; the format falls back from verbose_json to json
        when the provider rejects it, and the caller keeps using the returned one.
    Raises:
        CloudTranscriptionUnavailable: Non-transient HTTP errors, exhausted retries,
            or a response that is not a JSON object.
    """
    attempts = len(RETRY_DELAYS) + 1
    attempt = 0
    last_error = "OpenRouter audio request was not sent."
    while attempt < attempts:
        attempt += 1
        data = {"model": model, "response_format": response_format, "temperature": "0"}
        if language:
            data["language"] = language
        delay = None
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
            last_error = f"OpenRouter audio request failed ({type(exc).__name__})."
        else:
            status = response.status_code
            if not response.ok and status in FORMAT_REJECTED_STATUSES and response_format != RESPONSE_FORMATS[-1]:
                print(f"OpenRouter rejected response_format={response_format}; retrying with json.")
                response_format = RESPONSE_FORMATS[-1]
                attempt -= 1
                continue
            if not response.ok:
                last_error = f"OpenRouter audio request returned HTTP {status}."
                if status not in TRANSIENT_HTTP_STATUSES:
                    raise CloudTranscriptionUnavailable(last_error)
                delay = _retry_after_seconds(response)
            else:
                try:
                    result = response.json()
                except ValueError as exc:
                    raise CloudTranscriptionUnavailable("OpenRouter audio response was not JSON.") from exc
                if not isinstance(result, dict):
                    raise CloudTranscriptionUnavailable("OpenRouter audio response has an invalid shape.")
                return result, response_format
        if attempt < attempts:
            print(f"{last_error} Retrying ...")
            time.sleep(delay if delay is not None else RETRY_DELAYS[attempt - 1])
    raise CloudTranscriptionUnavailable(last_error)


def _parse_segments(result: dict[str, Any], offset: float) -> list[dict[str, Any]]:
    """Read verbose_json segments as `{start, end, text}` with lecture-absolute times."""
    segments = []
    for segment in result.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        start = _float_or_none(segment.get("start"))
        end = _float_or_none(segment.get("end"))
        segments.append({
            "start": round(offset + start, 2) if start is not None else None,
            "end": round(offset + end, 2) if end is not None else None,
            "text": text.strip(),
        })
    return segments


def _script_group(arabic_script: bool) -> str:
    return "arabic" if arabic_script else "latin"


def _language_vote(text: str, reported: str | None) -> tuple[str, str | None, int] | None:
    """Say which language one auto-detected part is in, as `(script group, code, weight)`.

    Purpose:
        Let every part of an auto-detected lecture vote on the lecture language
        without trusting Whisper's silence fillers or an unknown language name.
    Returns:
        None when the part has too little real speech to judge its script. Otherwise
        the script group ('arabic' or 'latin'), the language code when it is certain
        (Arabic script decides by itself; a Latin script needs Whisper's own verdict,
        because the script cannot tell French from English) or None, and the number
        of speech letters used as the vote's weight.
    Workflow:
        Judges the text with known hallucinations and repetition loops removed, so
        "Thank you. Thank you. ..." or "Subtitles by the Amara.org community" over a
        silent opening cannot decide anything.
    """
    probe = strip_hallucinations(collapse_loops(text))
    script = detect_text_language(probe)
    if script is None:
        return None
    letters = letter_count(probe)
    code = None
    if letters >= SPEECH_LETTERS:
        candidate = resolve_detected_language(probe, reported)
        if candidate in ARABIC_SCRIPT_LANGUAGES or (candidate and candidate == reported):
            code = candidate
    arabic_script = code in ARABIC_SCRIPT_LANGUAGES if code else script == "ar"
    return _script_group(arabic_script), code, letters


def _majority_language(votes: list[tuple[str, str | None, int] | None]) -> str | None:
    """The language most of the lecture is in, or None when it cannot be named safely.

    The script group with the most speech letters wins; within it, the known code
    with the most letters. An Arabic-script lecture defaults to 'ar'. A Latin-script
    lecture with no reported language stays None: forcing 'en' on it would make
    Whisper translate a French or Polish lecture into English.
    """
    weights = {"arabic": 0, "latin": 0}
    for vote in votes:
        if vote:
            weights[vote[0]] += vote[2]
    if not any(weights.values()):
        return None
    group = max(weights, key=weights.get)
    codes: dict[str, int] = {}
    for vote in votes:
        if vote and vote[0] == group and vote[1]:
            codes[vote[1]] = codes.get(vote[1], 0) + vote[2]
    if codes:
        return max(codes, key=codes.get)
    return "ar" if group == "arabic" else None


def _disagrees(vote: tuple[str, str | None, int] | None, target: str) -> bool:
    """Whether a part came back in another script or another known language than `target`."""
    if vote is None:
        return False
    if vote[0] != _script_group(target in ARABIC_SCRIPT_LANGUAGES):
        return True
    return vote[1] is not None and vote[1] != target


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
        language: Input language code, or None/'auto' for automatic detection.
        video_duration_seconds: Known lecture length, used to decide whether to split.
        progress_callback: Optional API job-progress callback.
    Returns:
        Complete transcript text (parts joined by a space; paragraphs are laid out
        later) and metadata: provider, model, requested and detected language, timed
        `segments` (empty when the provider returns none), and usage.
    Workflow:
        Splits long or oversized audio into temporary five-minute MP3s with FFmpeg and
        uploads each in order, asking for verbose_json (falling back to json). An
        explicit language is sent with every part. For auto-detection every part is
        first sent without a language; each part then votes with its speech (silence
        fillers removed) and Whisper's verdict, and only the parts that came back in
        another language than the lecture's majority are sent again with that
        language. A silent or English-titled opening therefore cannot turn an Arabic
        lecture into English, and a lecture whose parts agree costs no extra call.
        Transient HTTP errors are retried; a part with no speech is skipped. Temporary
        files are removed on both success and failure.
    Connects to:
        Called by MainCode_FasterWhisper.transcribe_audio before local fallback.
    Raises:
        CloudTranscriptionUnavailable: Missing credentials, invalid settings, media
            conversion failure, exhausted retries, HTTP error, invalid response, or no
            speech in any part.
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

    requested_language = normalize_language(language)
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
    target_language = None
    response_format = RESPONSE_FORMATS[0]

    def transcribe_part(chunk: Path, part_language: str | None, offset: float) -> dict[str, Any]:
        """Upload one part and return its text, timed segments, reported language and usage."""
        nonlocal response_format
        result, response_format = _post_audio_part(
            chunk,
            model=model,
            headers=headers,
            timeout_seconds=timeout_seconds,
            language=part_language,
            response_format=response_format,
        )
        text = result.get("text")
        if not isinstance(text, str):
            raise CloudTranscriptionUnavailable("OpenRouter audio response has no transcript text.")
        text = text.strip()
        return {
            "text": text,
            "segments": _parse_segments(result, offset) if text else [],
            "reported": normalize_language(result.get("language")),
            "usage": result.get("usage"),
            "duration": _float_or_none(result.get("duration")),
        }

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

        parts: list[dict[str, Any]] = []
        offset = 0.0
        for index, chunk in enumerate(chunks, start=1):
            if chunk.stat().st_size > MAX_UPLOAD_BYTES:
                raise CloudTranscriptionUnavailable("Audio chunk exceeds the 25 MB upload limit.")
            print(f"OpenRouter transcription: part {index}/{len(chunks)} with {model}")
            if progress_callback:
                progress_callback("transcribing", {
                    "detail": f"Whisper is transcribing your lecture (part {index} of {len(chunks)})",
                    "transcription_provider": "openrouter",
                    "chunk_index": index,
                    "chunk_total": len(chunks),
                    "video_duration_seconds": video_duration_seconds,
                    "estimated_stage_seconds": 60 * (len(chunks) - index + 1),
                })
            part = transcribe_part(chunk, requested_language, offset)
            part.update(chunk=chunk, index=index, offset=offset)
            if not part["text"]:
                print(f"OpenRouter returned no speech for part {index}; continuing.")
            parts.append(part)
            offset += part["duration"] if part["duration"] is not None else float(chunk_seconds)

        if requested_language is None:
            votes = [_language_vote(part["text"], part["reported"]) for part in parts]
            target_language = _majority_language(votes)
            retry = [part for part, vote in zip(parts, votes) if target_language and _disagrees(vote, target_language)]
            if target_language:
                print(f"Detected lecture language: {target_language}; transcribing {len(retry)} part(s) again in it.")
            for position, part in enumerate(retry, start=1):
                print(f"OpenRouter transcription: part {part['index']}/{len(chunks)} again in {target_language}")
                if progress_callback:
                    progress_callback("transcribing", {
                        "detail": (
                            f"Whisper is transcribing part {part['index']} of {len(chunks)} again in "
                            f"{language_name(target_language) or target_language}"
                        ),
                        "transcription_provider": "openrouter",
                        "chunk_index": part["index"],
                        "chunk_total": len(chunks),
                        "video_duration_seconds": video_duration_seconds,
                        "estimated_stage_seconds": 60 * (len(retry) - position + 1),
                    })
                again = transcribe_part(part["chunk"], target_language, part["offset"])
                # An empty answer keeps the first attempt rather than dropping the part.
                if again["text"]:
                    part.update(text=again["text"], segments=again["segments"], reported=again["reported"])
                part["usage"] = [part["usage"], again["usage"]]

    texts = [part["text"] for part in parts if part["text"]]
    if not texts:
        raise CloudTranscriptionUnavailable("OpenRouter returned an empty audio transcript.")

    full_text = " ".join(texts)
    reported_languages = [part["reported"] for part in parts if part["text"] and part["reported"]]
    reported_language = max(set(reported_languages), key=reported_languages.count) if reported_languages else None
    detected_language = resolve_detected_language(
        full_text, reported_language, requested_language or target_language
    )
    return full_text, {
        "provider": "openrouter",
        "model": model,
        "requested_language": requested_language,
        "detected_language": detected_language,
        "language_probability": None,
        "response_format": response_format,
        "video_duration_seconds": video_duration_seconds,
        "audio_chunk_count": len(chunks),
        "usage": [part["usage"] for part in parts],
        "segments": [segment for part in parts for segment in part["segments"]],
    }
