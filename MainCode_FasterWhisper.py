import json
import os
from pathlib import Path
import sys
from threading import Lock, RLock
from time import time
from typing import Any, Callable, TYPE_CHECKING

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from clean_with_Llama import (
    CloudCleanerUnavailable,
    clean_transcript_file,
    clean_transcript_file_with_openrouter,
    load_local_env,
)
from openrouter_transcription import CloudTranscriptionUnavailable, transcribe_with_openrouter
from transcript_format import (
    FORMAT_VERSION,
    detect_text_language,
    format_transcript,
    language_label,
    normalize_language,
    paragraphize,
    resolve_detected_language,
)

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

load_local_env()
import url_to_mp3


MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "large-v3")
DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "auto")
_LOCAL_MODEL_PATH = Path(r"C:\Users\Mahmoud\models\faster-whisper-large-v3")
MODEL_PATH = os.environ.get("WHISPER_MODEL_PATH") or (
    str(_LOCAL_MODEL_PATH) if MODEL_SIZE == "large-v3" and _LOCAL_MODEL_PATH.is_dir() else MODEL_SIZE
)
# Used when a request omits `language` (older clients). 'auto' or None detects.
DEFAULT_LANGUAGE = "ar"
TRANSCRIPT_CACHE_FILE = Path("transcript_cache.json")
# Older name of FORMAT_VERSION, kept for callers that still import it.
TRANSCRIPT_PROMPT_VERSION = FORMAT_VERSION
RAW_OUTPUT_DIR = Path("OutputForWhisper")
CLEANED_OUTPUT_DIR = Path("OutputForOllama")
# 'formatted' = LLM cleaner (clean=True); 'fast' = deterministic formatter only.
MODES = ("formatted", "fast")
whisper_model_lock = Lock()
whisper_models: dict[tuple[str, str, str], "WhisperModel"] = {}
# The API may read the cache from request threads while the worker writes it.
transcript_cache_lock = RLock()
LEGACY_FILENAME_CACHE_FIELDS = (
    "audio_path",
    "audio_filename",
    "download_filename",
    "raw_transcript_filename",
    "cleaned_transcript_filename",
    "ollama_output_filename",
    "llama_folder_filenames",
)
ProgressCallback = Callable[[str, dict[str, Any]], None]


def estimate_whisper_seconds(video_duration_seconds: int | float | None) -> int | None:
    """Estimate Whisper time from the lecture length.

    Purpose:
        Give the API a practical ETA based on the observed rule that Whisper takes
        about one third of the video duration on this machine.
    Args:
        video_duration_seconds: YouTube duration in seconds, when available.
    Returns:
        Estimated Whisper seconds, or None when duration is unknown.
    Workflow:
        Converts the supplied duration to seconds and divides it by three.
    Connects to:
        Called by `transcribe_audio` and `process_youtube_url`; its output is passed
        through progress events to `api.build_progress_update`.
    """
    if video_duration_seconds is None:
        return None
    try:
        duration = float(video_duration_seconds)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return max(30, int(round(duration / 3)))


def emit_progress(
    progress_callback: ProgressCallback | None,
    stage: str,
    **details: Any,
) -> None:
    """Send a pipeline progress event when a callback is available.

    Purpose:
        Decouple pipeline work from API job-status persistence and frontend updates.
    Args:
        progress_callback: Optional callable receiving a stage and details dictionary.
        stage: Stable pipeline stage identifier.
        **details: Stage-specific progress metadata.
    Returns:
        None.
    Workflow:
        Invokes the callback only when the caller supplied one.
    Connects to:
        Called throughout transcription and cleaning; the API supplies
        `update_job_progress` through a lambda.
    """
    if progress_callback:
        progress_callback(stage, details)


def mode_for(clean: bool) -> str:
    """Map the request's `clean` flag to the mode name used in caches and results."""
    return "formatted" if clean else "fast"


def transcript_cache_key(video_id: str, language: str | None, mode: str) -> str:
    """Cache key of one cleaned result: video, requested language ('auto' is its own key), mode, format version."""
    return f"{video_id}:{language_label(language)}:{mode}:{FORMAT_VERSION}"


def raw_output_path(video_id: str, language: str | None) -> Path:
    """Raw Whisper transcript path, keyed by video and requested language only.

    The name never contains the title (Arabic titles can exceed the 255-byte file-name
    limit) and never matches legacy `{id}_{title}_transcript.txt` files.
    """
    return RAW_OUTPUT_DIR / f"{video_id}_{language_label(language)}_raw.txt"


def raw_metadata_path(raw_path: Path) -> Path:
    """Sidecar JSON next to a raw transcript (segments, detected language, title)."""
    return Path(raw_path).with_suffix(".json")


def cleaned_output_path(video_id: str, language: str | None, mode: str) -> Path:
    """Cleaned transcript path, keyed by video, requested language, mode and format version."""
    return CLEANED_OUTPUT_DIR / f"{video_id}_{language_label(language)}_{mode}_{FORMAT_VERSION}.md"


def list_ollama_output_filenames(output_dir: Path = CLEANED_OUTPUT_DIR) -> list[str]:
    """List files currently stored in the cleaned-transcript directory.

    Purpose:
        Maintain a lightweight filename inventory in persistent JSON state.
    Args:
        output_dir: Directory containing cleaned transcript files.
    Returns:
        Sorted filenames, or an empty list when the directory does not exist.
    Workflow:
        Iterates over direct child files and returns only their names.
    Connects to:
        Called by transcript-cache persistence and API job persistence.
    """
    if not output_dir.exists():
        return []
    return sorted(path.name for path in output_dir.iterdir() if path.is_file())


def load_transcript_cache() -> dict[str, Any]:
    """Load and normalize the persistent transcript cache.

    Purpose:
        Restore cleaned transcript locations across API and machine restarts.
    Args:
        None.
    Returns:
        A dictionary containing normalized `videos` and `llama_folder_filenames` fields.
        `videos` maps `transcript_cache_key(...)` strings to entries.
    Workflow:
        Reads JSON when available, recovers from invalid files, validates field types,
        and refreshes missing filename inventory data.
    Connects to:
        Calls `list_ollama_output_filenames`; used by cache lookup and cache writes.
    """
    with transcript_cache_lock:
        if not TRANSCRIPT_CACHE_FILE.exists():
            return {"videos": {}, "llama_folder_filenames": list_ollama_output_filenames()}

        try:
            data = json.loads(TRANSCRIPT_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"videos": {}, "llama_folder_filenames": list_ollama_output_filenames()}

    if not isinstance(data, dict):
        data = {}
    videos = data.get("videos")
    if not isinstance(videos, dict):
        videos = {}

    filenames = data.get("llama_folder_filenames")
    if not isinstance(filenames, list):
        filenames = list_ollama_output_filenames()

    return {"videos": videos, "llama_folder_filenames": filenames}


def save_transcript_cache(cache: dict[str, Any]) -> None:
    """Persist transcript cache data with an atomic temporary-file replacement.

    Purpose:
        Save cache updates while avoiding partially written JSON files.
    Args:
        cache: Complete transcript cache dictionary to store.
    Returns:
        None.
    Workflow:
        Drops entries from older format versions (legacy video-id-only entries pointed
        at English-translated output), removes obsolete per-entry filename fields,
        refreshes the cleaned-folder inventory, writes a temporary JSON file, and
        replaces the live cache file.
    Connects to:
        Calls `list_ollama_output_filenames`; used by all cache mutation functions.
    """
    videos = cache.get("videos", {})
    if isinstance(videos, dict):
        for key in [key for key, entry in videos.items()
                    if not isinstance(entry, dict) or entry.get("format_version") != FORMAT_VERSION]:
            videos.pop(key, None)
        for entry in videos.values():
            for field in LEGACY_FILENAME_CACHE_FIELDS:
                entry.pop(field, None)

    with transcript_cache_lock:
        cache["llama_folder_filenames"] = list_ollama_output_filenames()
        tmp = TRANSCRIPT_CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(TRANSCRIPT_CACHE_FILE)


def get_cached_cleaned_entry(
    video_id: str,
    language: str | None = DEFAULT_LANGUAGE,
    mode: str = "formatted",
) -> dict[str, Any] | None:
    """Return a valid cleaned-transcript cache entry for a video, language and mode.

    Purpose:
        Skip download, Whisper, and cleaning when a current result exists for exactly
        this request: an English result is never served for an Arabic request, and a
        fast result is never served for a formatted one.
    Args:
        video_id: YouTube video ID.
        language: Requested language ('auto'/None is its own key).
        mode: 'formatted' or 'fast'.
    Returns:
        A copy of the cache entry when it is current and both transcript files exist,
        otherwise None (a stale entry is removed).
    Connects to:
        Calls cache load/save helpers; called by `find_cached_result`.
    """
    key = transcript_cache_key(video_id, language, mode)
    with transcript_cache_lock:
        cache = load_transcript_cache()
        entry = cache["videos"].get(key)
        if isinstance(entry, dict) and entry.get("format_version") == FORMAT_VERSION:
            cleaned_path = entry.get("cleaned_transcript_path")
            raw_path = entry.get("raw_transcript_path")
            if cleaned_path and Path(cleaned_path).is_file() and raw_path and Path(raw_path).is_file():
                return dict(entry)
        if key in cache["videos"]:
            cache["videos"].pop(key, None)
            save_transcript_cache(cache)
    return None


def save_cleaned_cache_entry(
    video_id: str,
    *,
    original_url: str,
    canonical_url: str,
    raw_path: Path,
    cleaned_path: Path,
    cleaner_provider: str | None = None,
    language: str | None = DEFAULT_LANGUAGE,
    mode: str = "formatted",
    title: str | None = None,
    video_duration_seconds: int | float | None = None,
    detected_language: str | None = None,
    cleaner_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update the cache entry of one (video, language, mode) result.

    Purpose:
        Record enough metadata to reuse a completed result, title and language
        included, on future requests.
    Args:
        video_id: YouTube video ID.
        original_url: URL submitted by the user.
        canonical_url: Normalized single-video URL.
        raw_path: Path to the raw Whisper transcript.
        cleaned_path: Path to the cleaned (formatted or fast) transcript.
        cleaner_provider: 'openrouter', 'ollama' or 'formatter'.
        language: Requested language ('auto'/None is its own key).
        mode: Mode of the stored cleaned transcript.
        title: Video title from yt-dlp.
        video_duration_seconds: Video length.
        detected_language: Language the transcript is actually written in.
        cleaner_stats: Per-chunk cleaner counters, when an LLM ran.
    Returns:
        The cache entry that was saved.
    Workflow:
        Preserves the original creation time, updates metadata and timestamps, then
        writes the complete cache atomically under the cache lock.
    Connects to:
        Calls cache load/save helpers; used by output discovery and the main pipeline.
    """
    key = transcript_cache_key(video_id, language, mode)
    with transcript_cache_lock:
        cache = load_transcript_cache()
        existing = cache["videos"].get(key)
        now = time()
        entry = {
            "video_id": video_id,
            "language": language_label(language),
            "mode": mode,
            "format_version": FORMAT_VERSION,
            "detected_language": normalize_language(detected_language),
            "title": title,
            "video_duration_seconds": video_duration_seconds,
            "canonical_url": canonical_url,
            "original_url": original_url,
            "raw_transcript_path": str(raw_path),
            "cleaned_transcript_path": str(cleaned_path),
            "cleaner_provider": cleaner_provider,
            "cleaner_stats": cleaner_stats or None,
            "created_at": existing.get("created_at", now) if isinstance(existing, dict) else now,
            "updated_at": now,
        }
        cache["videos"][key] = entry
        save_transcript_cache(cache)
    return dict(entry)


def load_raw_metadata(raw_path: Path) -> dict[str, Any]:
    """Read the sidecar of a raw transcript; an empty dict when missing or invalid."""
    path = raw_metadata_path(raw_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if not isinstance(data.get("segments"), list):
        data["segments"] = None
    return data


def save_raw_metadata(raw_path: Path, metadata: dict[str, Any]) -> None:
    """Write the sidecar of a raw transcript atomically (segments, language, title)."""
    path = raw_metadata_path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def find_existing_cleaned_output(
    video_id: str,
    *,
    original_url: str,
    canonical_url: str,
    language: str | None = DEFAULT_LANGUAGE,
    mode: str = "formatted",
) -> dict[str, Any] | None:
    """Re-register a current-format result found on disk but missing from the cache.

    Purpose:
        Recover outputs after transcript_cache.json was lost. Only files whose names
        encode this video, language, mode and the current format version qualify, so
        legacy outputs (English translations named after the title) are never revived.
    Args:
        video_id: YouTube video ID.
        original_url: URL submitted for the current request.
        canonical_url: Normalized URL saved with the recovered entry.
        language: Requested language.
        mode: 'formatted' or 'fast'.
    Returns:
        A newly saved cache entry, or None when the exact files do not both exist.
    Connects to:
        Calls `save_cleaned_cache_entry`; called by `find_cached_result`.
    """
    cleaned_path = cleaned_output_path(video_id, language, mode)
    raw_path = raw_output_path(video_id, language)
    if not cleaned_path.is_file() or not raw_path.is_file():
        return None
    metadata = load_raw_metadata(raw_path)
    detected = metadata.get("detected_language") or detect_text_language(
        cleaned_path.read_text(encoding="utf-8", errors="replace")
    )
    return save_cleaned_cache_entry(
        video_id,
        original_url=original_url,
        canonical_url=canonical_url,
        raw_path=raw_path,
        cleaned_path=cleaned_path,
        cleaner_provider=metadata.get("cleaner_provider") if mode == "formatted" else "formatter",
        language=language,
        mode=mode,
        title=metadata.get("title"),
        video_duration_seconds=metadata.get("video_duration_seconds"),
        detected_language=detected,
    )


def empty_result(video_id: str, canonical_url: str, language: str | None, mode: str) -> dict[str, Any]:
    """Result dictionary skeleton shared by fresh runs and cache hits."""
    return {
        "cache_key": transcript_cache_key(video_id, language, mode),
        "canonical_url": canonical_url,
        "video_id": video_id,
        "title": None,
        "language": language_label(language),
        "detected_language": None,
        "mode": mode,
        "requested_mode": mode,
        "format_version": FORMAT_VERSION,
        "audio_path": None,
        "raw_transcript_path": None,
        "cleaned_transcript_path": None,
        "used_cached_raw_transcript": False,
        "used_cached_cleaned_transcript": False,
        "transcription_info": None,
        "transcription_provider": None,
        "video_duration_seconds": None,
        "whisper_estimate_seconds": None,
        "cleaner_provider": None,
        "cleaner_error": None,
        "cleaner_stats": None,
        "cleaner_partial": False,
        "audio_deleted": False,
        "audio_delete_error": None,
    }


def find_cached_result(
    youtube_url: str,
    *,
    clean: bool = True,
    language: str | None = DEFAULT_LANGUAGE,
    **_ignored: Any,
) -> dict[str, Any] | None:
    """Return the finished result for a request when this service already has it.

    Purpose:
        Let the pipeline (and the API, before queueing) answer a repeated request for
        the same video, language and mode without downloading or transcribing.
    Args:
        youtube_url: YouTube lecture URL.
        clean: Requested mode flag (True = formatted, False = fast).
        language: Requested language ('auto'/None is its own key).
        **_ignored: Other request fields, accepted so a request dict can be passed.
    Returns:
        A result dictionary shaped like `process_youtube_url`'s, with
        `used_cached_cleaned_transcript` True, or None when nothing current exists.
        It neither emits progress nor deletes audio.
    Connects to:
        Calls `get_cached_cleaned_entry` and `find_existing_cleaned_output`.
    """
    video_id = url_to_mp3.extract_youtube_video_id(youtube_url)
    if not video_id:
        return None
    canonical_url = url_to_mp3.force_single_video_url(youtube_url)
    mode = mode_for(clean)
    entry = get_cached_cleaned_entry(video_id, language, mode) or find_existing_cleaned_output(
        video_id,
        original_url=youtube_url,
        canonical_url=canonical_url,
        language=language,
        mode=mode,
    )
    if not entry:
        return None
    result = empty_result(video_id, canonical_url, language, mode)
    result.update(
        {
            "title": entry.get("title"),
            "detected_language": entry.get("detected_language"),
            "raw_transcript_path": entry.get("raw_transcript_path"),
            "cleaned_transcript_path": entry.get("cleaned_transcript_path"),
            "cleaner_provider": entry.get("cleaner_provider"),
            "cleaner_stats": entry.get("cleaner_stats"),
            "video_duration_seconds": entry.get("video_duration_seconds"),
            "used_cached_cleaned_transcript": True,
        }
    )
    return result


def clean_transcript_with_preferred_model(
    raw_path: Path,
    *,
    allow_ollama_fallback: bool,
    progress_callback: ProgressCallback | None = None,
    language: str | None = None,
    segments: list[dict[str, Any]] | None = None,
    output_path: Path | None = None,
    stats: dict[str, Any] | None = None,
) -> tuple[Path | None, str | None, str | None]:
    """Clean a raw transcript in its own language with OpenRouter, then optionally Ollama.

    Purpose:
        Implement the model-selection policy of the formatted mode.
    Args:
        raw_path: Raw Whisper transcript to clean.
        allow_ollama_fallback: Whether an OpenRouter failure may start local Ollama.
        progress_callback: Optional callback for formatting-stage updates.
        language: Lecture language passed to the cleaner's prompt and guards.
        segments: Optional Whisper segments aligned with the raw text.
        output_path: Destination of the cleaned transcript.
        stats: Optional dictionary filled with per-chunk cleaner counters.
    Returns:
        `(cleaned_path, provider_name, cloud_error)`; the path and provider are None
        when no LLM produced a usable chunk, so the caller formats deterministically.
    Workflow:
        Attempts OpenRouter (which already falls back per chunk), records any failure,
        then tries Ollama when allowed and enabled (OLLAMA_LOCAL_FALLBACK).
    Connects to:
        Calls both cleaner entry points and `emit_progress`; used by
        `process_youtube_url`.
    """
    errors: list[str] = []
    options = {"language": language, "segments": segments, "output_path": output_path, "stats": stats}

    try:
        emit_progress(progress_callback, "formatting", detail="Formatting transcript with OpenRouter")
        print("Running OpenRouter cleaner ...")
        cleaned = clean_transcript_file_with_openrouter(raw_path, progress_callback=progress_callback, **options)
        if cleaned:
            return cleaned, "openrouter", None
    except (CloudCleanerUnavailable, Exception) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        print(f"OpenRouter cleaner unavailable: {errors[-1]}")
    cloud_error = "; ".join(errors) or None

    if not allow_ollama_fallback:
        return None, None, cloud_error

    if os.environ.get("OLLAMA_LOCAL_FALLBACK", "true").strip().lower() in {"false", "0", "no"}:
        return None, None, cloud_error

    emit_progress(progress_callback, "formatting", detail="OpenRouter unavailable. Formatting transcript with Ollama")
    print("Running Ollama cleaner ...")
    try:
        cleaned = clean_transcript_file(raw_path, progress_callback=progress_callback, **options)
    except Exception as exc:
        errors.append(f"Ollama {type(exc).__name__}: {exc}")
        print(f"Ollama cleaner unavailable: {errors[-1]}")
        return None, None, "; ".join(errors)
    if not cleaned:
        return None, None, cloud_error
    return cleaned, "ollama", cloud_error


def find_existing_raw_output(video_id: str, language: str | None = DEFAULT_LANGUAGE) -> Path | None:
    """Find the raw Whisper output of a video in the requested language.

    Purpose:
        Reuse transcription work when no cleaned result is available.
    Args:
        video_id: YouTube video ID.
        language: Requested language ('auto'/None is its own key).
    Returns:
        Path to the raw transcript, or None when absent. Legacy title-named files are
        never matched, so an English raw text cannot serve an Arabic request.
    Connects to:
        Called by `process_youtube_url` before audio download.
    """
    path = raw_output_path(video_id, language)
    return path if path.is_file() else None


def delete_audio_file(audio_path: Path) -> tuple[bool, str | None]:
    """Delete a downloaded audio file after successful job processing.

    Purpose:
        Reduce repository and disk usage after the transcript result is safely stored.
    Args:
        audio_path: MP3 file to remove.
    Returns:
        `(deleted, error_message)`; a missing file returns `(False, None)`.
    Workflow:
        Checks existence, attempts `Path.unlink`, and converts OS errors to result data.
    Connects to:
        Called by `process_youtube_url` before a downloaded job returns successfully.
    """
    if not audio_path.exists():
        return False, None

    try:
        audio_path.unlink()
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"

    return True, None


def delete_audio_files_for_video(
    video_id: str,
    downloads_dir: Path = Path("downloads"),
) -> tuple[bool, str | None]:
    """Delete any cached MP3 files associated with a YouTube video.

    Purpose:
        Remove audio left by earlier interrupted runs when a later job completes from a
        raw or cleaned transcript cache hit.
    Args:
        video_id: YouTube video ID used to name downloaded files.
        downloads_dir: Directory containing temporary MP3 files.
    Returns:
        `(deleted, error_message)` where `deleted` is True when at least one file was
        removed and `error_message` describes any files Windows could not delete.
    Workflow:
        Finds `{id}.mp3` and legacy `{id}_{title}.mp3` files and delegates deletion of
        each file to `delete_audio_file`.
    Connects to:
        Calls `delete_audio_file`; used by successful cache-return paths in
        `process_youtube_url`.
    """
    if not downloads_dir.exists():
        return False, None

    deleted_any = False
    errors = []
    candidates = [downloads_dir / f"{video_id}.mp3", *downloads_dir.glob(f"{video_id}_*.mp3")]
    for audio_path in candidates:
        if not audio_path.exists():
            continue
        deleted, error = delete_audio_file(audio_path)
        deleted_any = deleted_any or deleted
        if error:
            errors.append(f"{audio_path.name}: {error}")

    return deleted_any, "; ".join(errors) or None


def print_final_output(cleaned_path: Path) -> None:
    """Print a cleaned transcript path and its contents for CLI users.

    Purpose:
        Present the final pipeline result when the main module runs directly.
    Args:
        cleaned_path: Existing cleaned transcript file.
    Returns:
        None.
    Workflow:
        Prints the absolute file location, then reads and prints UTF-8 content.
    Connects to:
        Called by `main` after `process_youtube_url` returns a cleaned path.
    """
    print("Final transcript:")
    print(cleaned_path.resolve())
    print("\n===== FINAL OUTPUT =====\n")
    print(cleaned_path.read_text(encoding="utf-8", errors="replace"))


def build_initial_prompt(language: str | None = DEFAULT_LANGUAGE) -> str | None:
    """Build the Faster-Whisper context prompt for one lecture language.

    Purpose:
        Whisper reads `initial_prompt` as the transcript that came before, not as an
        instruction, so the prompt is a short sample written the way the lecture
        should be transcribed: Arabic script with English terms inline for Arabic,
        plain English for English.
    Args:
        language: Lecture language code, or None for auto-detection.
    Returns:
        Prompt text for 'ar' and 'en', otherwise None (no bias towards any language).
    Connects to:
        Called by `transcribe_audio_local`.
    """
    code = normalize_language(language)
    if code == "ar":
        return "محاضرة جامعية. سنشرح اليوم الـ algorithm والـ data structure خطوة بخطوة، ثم نحل بعض الأمثلة."
    if code == "en":
        return "University lecture. Today we explain the algorithm and the data structure step by step, then solve some examples."
    return None


def get_whisper_model(
    *,
    model_path: str = MODEL_PATH,
    device: str = DEVICE,
    compute_type: str = COMPUTE_TYPE,
) -> "WhisperModel":
    """Load or reuse a Faster-Whisper model instance.

    Purpose:
        Avoid expensive model reloads across sequential API jobs.
    Args:
        model_path: Local model directory or model identifier.
        device: Execution device such as `cuda` or `cpu`.
        compute_type: Faster-Whisper numerical precision configuration.
    Returns:
        A cached, ready-to-use `WhisperModel`.
    Workflow:
        Builds a configuration key, locks shared state, creates the model once, and
        returns the cached instance on later calls.
    Connects to:
        Called by `transcribe_audio`; accesses `whisper_models` under
        `whisper_model_lock`.
    """
    from faster_whisper import WhisperModel

    key = (model_path, device, compute_type)
    with whisper_model_lock:
        model = whisper_models.get(key)
        if model is None:
            print(f"Loading faster-whisper model from: {model_path}")
            model = WhisperModel(
                model_path,
                device=device,
                compute_type=compute_type,
            )
            whisper_models[key] = model
        else:
            print("Using already loaded faster-whisper model.")
        return model


def _write_raw_transcript(output_path: Path, text: str, segments: list[dict[str, Any]] | None) -> Path:
    """Write raw Whisper text laid out in paragraphs (whitespace changes only)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paragraphize(text, segments), encoding="utf-8")
    return output_path


def transcribe_audio_local(
    audio_path: Path,
    *,
    model_path: str = MODEL_PATH,
    model_size: str = MODEL_SIZE,
    device: str = DEVICE,
    compute_type: str = COMPUTE_TYPE,
    language: str | None = DEFAULT_LANGUAGE,
    video_duration_seconds: int | float | None = None,
    progress_callback: ProgressCallback | None = None,
    output_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Transcribe an audio file with Faster-Whisper and save raw text.

    Purpose:
        Convert downloaded lecture audio into the raw transcript used by all cleaners.
    Args:
        audio_path: Existing audio file to transcribe.
        model_path: Local model directory or model identifier.
        model_size: Display name used in logs.
        device: Execution device.
        compute_type: Model numerical precision.
        language: Whisper language code; None or 'auto' lets Whisper detect it.
        video_duration_seconds: YouTube video length used to estimate Whisper time.
        progress_callback: Optional callback for transcription-stage updates.
        output_path: Raw transcript destination (defaults to OutputForWhisper/).
    Returns:
        `(raw_transcript_path, metadata)` with the detected language and the timed
        `segments`.
    Raises:
        FileNotFoundError: If the audio path does not exist.
    Workflow:
        Loads the cached model, transcribes with VAD, beam search, the language's
        sample prompt and anti-loop settings, keeps segment timing, and writes the text
        laid out in paragraphs from the segment pauses.
    Connects to:
        Calls `get_whisper_model`, `build_initial_prompt`, and `emit_progress`; called by
        `transcribe_audio` when cloud transcription is unavailable or disabled.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"File not found: {audio_path.resolve()}")

    if device == "auto":
        import ctranslate2

        device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
    if compute_type == "auto":
        compute_type = "int8_float16" if device == "cuda" else "int8"

    code = normalize_language(language)
    whisper_estimate_seconds = estimate_whisper_seconds(video_duration_seconds)
    progress_details = {
        "video_duration_seconds": video_duration_seconds,
        "estimated_stage_seconds": whisper_estimate_seconds,
        "transcription_provider": "local",
    }

    emit_progress(
        progress_callback,
        "transcribing",
        detail="Loading faster-whisper model",
        **progress_details,
    )
    print(f"Loading faster-whisper model: {model_size} on {device} ({compute_type}) ...")
    model = get_whisper_model(
        model_path=model_path,
        device=device,
        compute_type=compute_type,
    )

    emit_progress(
        progress_callback,
        "transcribing",
        detail="Whisper is transcribing the lecture",
        **progress_details,
    )
    print("Transcribing ...")
    kwargs: dict[str, Any] = {
        "language": code,
        "vad_filter": True,
        "beam_size": 5,
        # Without conditioning on earlier text, one hallucinated phrase cannot loop.
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 2.4,
        "no_speech_threshold": 0.6,
    }
    initial_prompt = build_initial_prompt(code)
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt

    segment_iter, info = model.transcribe(audio_path.as_posix(), **kwargs)

    segments = []
    for segment in segment_iter:
        text = (getattr(segment, "text", "") or "").strip()
        if text:
            segments.append({"start": getattr(segment, "start", None), "end": getattr(segment, "end", None), "text": text})
    text = " ".join(segment["text"] for segment in segments)

    print("\n===== TRANSCRIPT =====\n")
    print(text)

    out = _write_raw_transcript(
        output_path or RAW_OUTPUT_DIR / f"{audio_path.stem}_transcript.txt", text, segments
    )

    reported_language = getattr(info, "language", None)
    print(f"\nSaved to: {out.resolve()}")
    print("\n===== INFO =====")
    print("Detected language:", reported_language)
    print("Language probability:", getattr(info, "language_probability", "N/A"))

    metadata = {
        "provider": "local",
        "model": model_size,
        "device": device,
        "requested_language": code,
        "detected_language": resolve_detected_language(text, reported_language, code),
        "language_probability": getattr(info, "language_probability", None),
        "video_duration_seconds": video_duration_seconds,
        "whisper_estimate_seconds": whisper_estimate_seconds,
        "segments": segments,
    }
    emit_progress(
        progress_callback,
        "transcribing",
        detail="Whisper transcription finished",
        **progress_details,
    )
    return out, metadata


def transcribe_audio(
    audio_path: Path,
    *,
    model_path: str = MODEL_PATH,
    model_size: str = MODEL_SIZE,
    device: str = DEVICE,
    compute_type: str = COMPUTE_TYPE,
    language: str | None = DEFAULT_LANGUAGE,
    video_duration_seconds: int | float | None = None,
    progress_callback: ProgressCallback | None = None,
    output_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Prefer OpenRouter Whisper, falling back to local Faster-Whisper on failure.

    Args:
        audio_path: Downloaded MP3 file to transcribe in its original language.
        model_path, model_size, device, compute_type: Local fallback settings.
        language: Input language code; None or 'auto' detects the language.
        video_duration_seconds: Lecture duration for splitting and progress estimates.
        progress_callback: Optional callback feeding API job status.
        output_path: Raw transcript destination (defaults to OutputForWhisper/).
    Returns:
        Raw transcript path and metadata identifying the actual transcription provider,
        the detected language, and the timed `segments`.
    Workflow:
        Calls OpenRouter by default, saves only its complete output laid out in
        paragraphs, or transcribes the original audio locally if the cloud request
        fails. WHISPER_BACKEND=local skips cloud requests; WHISPER_LOCAL_FALLBACK=false
        disables local fallback.
    Connects to:
        Called by process_youtube_url; uses transcribe_with_openrouter or
        transcribe_audio_local.
    """
    if not audio_path.is_file():
        raise FileNotFoundError(f"File not found: {audio_path.resolve()}")
    backend = os.environ.get("WHISPER_BACKEND", "openrouter").strip().lower()
    if backend not in {"openrouter", "local"}:
        raise ValueError("WHISPER_BACKEND must be openrouter or local.")
    code = normalize_language(language)
    out_path = Path(output_path) if output_path else RAW_OUTPUT_DIR / f"{audio_path.stem}_transcript.txt"
    cloud_error = None
    if backend == "openrouter":
        try:
            text, metadata = transcribe_with_openrouter(
                audio_path, language=code,
                video_duration_seconds=video_duration_seconds,
                progress_callback=progress_callback,
            )
        except CloudTranscriptionUnavailable as exc:
            cloud_error = str(exc)
            if os.environ.get("WHISPER_LOCAL_FALLBACK", "true").strip().lower() in {"false", "0", "no"}:
                raise
            print(f"OpenRouter transcription unavailable: {cloud_error}")
            print("Falling back to local Faster-Whisper ...")
            emit_progress(
                progress_callback, "transcribing",
                detail="Cloud transcription unavailable. Starting local Whisper",
                transcription_provider="local",
                transcription_error=cloud_error,
                video_duration_seconds=video_duration_seconds,
                estimated_stage_seconds=estimate_whisper_seconds(video_duration_seconds),
            )
        else:
            out = _write_raw_transcript(out_path, text, metadata.get("segments"))
            print(f"OpenRouter transcript saved to: {out.resolve()}")
            return out, metadata

    out, metadata = transcribe_audio_local(
        audio_path, model_path=model_path, model_size=model_size,
        device=device, compute_type=compute_type, language=code,
        video_duration_seconds=video_duration_seconds,
        progress_callback=progress_callback,
        output_path=out_path,
    )
    if cloud_error:
        metadata["cloud_error"] = cloud_error
    return out, metadata


def process_youtube_url(
    youtube_url: str,
    *,
    clean: bool = True,
    skip_audio_cache: bool = False,
    use_cached_outputs: bool = True,
    language: str | None = DEFAULT_LANGUAGE,
    progress_callback: ProgressCallback | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Run the complete cached YouTube-to-transcript pipeline.

    Purpose:
        Coordinate URL validation, cache reuse, audio download, Whisper transcription,
        formatting in the lecture's own language, cache persistence, and MP3 cleanup.
    Args:
        youtube_url: YouTube lecture URL to process.
        clean: True = formatted mode (LLM cleaner with per-chunk deterministic
            fallback); False = fast mode (deterministic formatter only, no LLM).
        skip_audio_cache: Forces yt-dlp to download audio again.
        use_cached_outputs: Allows reuse of cleaned and raw transcript files.
        language: Requested language: a code ('ar', 'en', ...) or 'auto'/None/'' to
            detect it. Threaded into transcription and cleaning.
        progress_callback: Optional callback receiving pipeline stage updates.
        **_ignored: Other request fields (e.g. `callback_url`), accepted and ignored
            so an API request dict can be passed through.
    Returns:
        A result dictionary: `video_id`, `title`, `language` (requested: 'auto' or a
        code), `detected_language`, `mode` (mode of the produced cleaned transcript),
        `requested_mode`, `format_version`, `cache_key`, both transcript paths, cache
        flags, provider information, cleaner stats, and audio deletion status.
    Raises:
        ValueError: If the URL does not contain a valid video ID.
        Download or transcription exceptions when required work fails.
    Workflow:
        Returns a cached result for the same (video, language, mode, format version);
        otherwise reuses the raw transcript of the same (video, language) or downloads
        and transcribes (emitting a progress event with `title` and
        `video_duration_seconds` once the download finishes). Fast mode formats the raw
        text deterministically. Formatted mode runs the LLM cleaner; if no LLM produced
        any usable chunk, or the provider stopped part-way and left the remaining
        chunks in their plain layout, the output is returned with `mode='fast'` so it
        is never stored or reused as a formatted result (isolated rejected chunks keep
        `formatted`). Both transcript kinds always exist.
    Connects to:
        Orchestrates helpers in this module, `url_to_mp3`, `clean_with_Llama` and
        `transcript_format`; called by API background jobs and `main`.
    """
    emit_progress(progress_callback, "checking_cache", detail="Checking saved transcripts")
    video_id = url_to_mp3.extract_youtube_video_id(youtube_url)
    if not video_id:
        raise ValueError("Input is NOT a valid YouTube video URL")

    canonical_url = url_to_mp3.force_single_video_url(youtube_url)
    requested_language = normalize_language(language)
    requested_mode = mode_for(clean)

    if use_cached_outputs:
        cached = find_cached_result(youtube_url, clean=clean, language=language)
        if cached:
            emit_progress(
                progress_callback,
                "cache_hit",
                detail="Using a saved transcript",
                title=cached.get("title"),
                video_duration_seconds=cached.get("video_duration_seconds"),
                detected_language=cached.get("detected_language"),
            )
            audio_deleted, audio_delete_error = delete_audio_files_for_video(video_id)
            cached["audio_deleted"] = audio_deleted
            cached["audio_delete_error"] = audio_delete_error
            return cached

    result = empty_result(video_id, canonical_url, language, requested_mode)
    raw_path = raw_output_path(video_id, language)
    audio_path: Path | None = None
    segments: list[dict[str, Any]] | None = None

    if use_cached_outputs and raw_path.is_file():
        raw_metadata = load_raw_metadata(raw_path)
        segments = raw_metadata.get("segments")
        result.update(
            {
                "raw_transcript_path": str(raw_path),
                "used_cached_raw_transcript": True,
                "title": raw_metadata.get("title"),
                "video_duration_seconds": raw_metadata.get("video_duration_seconds"),
                "transcription_provider": raw_metadata.get("transcription_provider"),
                "detected_language": normalize_language(raw_metadata.get("detected_language")),
            }
        )
        emit_progress(
            progress_callback,
            "checking_cache",
            detail="Using the saved Whisper transcript",
            title=result["title"],
            video_duration_seconds=result["video_duration_seconds"],
        )
    else:
        emit_progress(progress_callback, "downloading", detail="Downloading audio from YouTube")
        audio_path_text, video_metadata = url_to_mp3.download_youtube_mp3(
            youtube_url,
            skip_cache=skip_audio_cache,
            return_metadata=True,
        )
        video_metadata = video_metadata or {}
        video_duration_seconds = video_metadata.get("duration")
        title = video_metadata.get("title")
        audio_path = Path(audio_path_text)
        result.update(
            {
                "audio_path": str(audio_path),
                "title": title,
                "video_duration_seconds": video_duration_seconds,
                "whisper_estimate_seconds": estimate_whisper_seconds(video_duration_seconds),
            }
        )
        emit_progress(
            progress_callback,
            "downloading",
            detail="Audio download finished",
            title=title,
            video_duration_seconds=video_duration_seconds,
        )

        raw_path, transcription_info = transcribe_audio(
            audio_path,
            language=requested_language,
            video_duration_seconds=video_duration_seconds,
            progress_callback=progress_callback,
            output_path=raw_path,
        )
        transcription_info = dict(transcription_info or {})
        segments = transcription_info.pop("segments", None)
        result.update(
            {
                "raw_transcript_path": str(raw_path),
                "transcription_info": transcription_info,
                "transcription_provider": transcription_info.get("provider"),
                "detected_language": normalize_language(transcription_info.get("detected_language")),
            }
        )

    raw_text = Path(raw_path).read_text(encoding="utf-8", errors="replace")
    if not result["detected_language"]:
        result["detected_language"] = resolve_detected_language(raw_text, None, requested_language)
    if audio_path is not None:
        save_raw_metadata(
            raw_path,
            {
                "video_id": video_id,
                "language": result["language"],
                "detected_language": result["detected_language"],
                "title": result["title"],
                "video_duration_seconds": result["video_duration_seconds"],
                "transcription_provider": result["transcription_provider"],
                "segments": segments,
            },
        )

    cleaning_language = requested_language or result["detected_language"]
    emit_progress(
        progress_callback,
        "formatting",
        detail="Formatting the transcript",
        title=result["title"],
        detected_language=result["detected_language"],
    )
    produced_mode = requested_mode
    cleaned: Path | None = None
    if clean:
        stats: dict[str, Any] = {}
        cleaned, cleaner_provider, cleaner_error = clean_transcript_with_preferred_model(
            Path(raw_path),
            allow_ollama_fallback=True,
            progress_callback=progress_callback,
            language=cleaning_language,
            segments=segments,
            output_path=cleaned_output_path(video_id, language, "formatted"),
            stats=stats,
        )
        result["cleaner_provider"] = cleaner_provider
        result["cleaner_error"] = cleaner_error
        if cleaned:
            result["cleaner_stats"] = stats or None
            result["cleaner_partial"] = bool(stats.get("fallback_chunks"))
            if stats.get("stopped_reason"):
                # The provider gave up part-way (no credit, rejected key, repeated
                # failures), so the rest of the lecture kept its plain layout. Stored
                # as fast, and moved off the formatted file name that cache recovery
                # would revive, so a later formatted request formats it again.
                produced_mode = "fast"
                result["cleaner_error"] = cleaner_error or stats["stopped_reason"]
                partial_path = cleaned_output_path(video_id, language, "fast")
                Path(cleaned).replace(partial_path)
                cleaned = partial_path
        else:
            # Nothing came back from any model: the deterministic layout is a fast
            # result and must not be cached or stored as a formatted one.
            produced_mode = "fast"

    if not cleaned:
        emit_progress(progress_callback, "formatting", detail="Arranging the transcript into paragraphs")
        cleaned = cleaned_output_path(video_id, language, "fast")
        cleaned.parent.mkdir(parents=True, exist_ok=True)
        cleaned.write_text(format_transcript(raw_text, segments, cleaning_language), encoding="utf-8")
        result["cleaner_provider"] = "formatter"

    result.update(
        {
            "cleaned_transcript_path": str(cleaned),
            "mode": produced_mode,
            "cache_key": transcript_cache_key(video_id, language, produced_mode),
        }
    )

    emit_progress(progress_callback, "saving", detail="Saving transcript cache")
    save_cleaned_cache_entry(
        video_id,
        original_url=youtube_url,
        canonical_url=canonical_url,
        raw_path=Path(raw_path),
        cleaned_path=cleaned,
        cleaner_provider=result["cleaner_provider"],
        language=language,
        mode=produced_mode,
        title=result["title"],
        video_duration_seconds=result["video_duration_seconds"],
        detected_language=result["detected_language"],
        cleaner_stats=result["cleaner_stats"],
    )

    emit_progress(progress_callback, "saving", detail="Removing temporary audio")
    if audio_path is not None:
        audio_deleted, audio_delete_error = delete_audio_file(audio_path)
    else:
        audio_deleted, audio_delete_error = delete_audio_files_for_video(video_id)
    result["audio_deleted"] = audio_deleted
    result["audio_delete_error"] = audio_delete_error

    return result


def main() -> None:
    """Run the transcript pipeline interactively from the command line.

    Purpose:
        Provide a direct non-API entry point for local processing and debugging.
    Args:
        None.
    Returns:
        None.
    Workflow:
        Prompts for a YouTube URL, runs the pipeline without output-cache reuse, handles
        errors, and prints either the cleaned result or raw transcript location.
    Connects to:
        Calls `url_to_mp3.prompt_for_youtube_url`, `process_youtube_url`, and
        `print_final_output`.
    """
    youtube_url = url_to_mp3.prompt_for_youtube_url("Enter YouTube lecture link: ")

    try:
        result = process_youtube_url(youtube_url, use_cached_outputs=False)
    except Exception as exc:
        print(f"\n Error: {exc}")
        return

    cleaned_path = result.get("cleaned_transcript_path")
    if cleaned_path and Path(cleaned_path).exists():
        print_final_output(Path(cleaned_path))
    else:
        print("\nPipeline completed.")
        print(f"Raw transcript: {Path(result['raw_transcript_path']).resolve()}")


if __name__ == "__main__":
    main()
