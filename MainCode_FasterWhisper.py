import json
import os
from pathlib import Path
import sys
from threading import Lock
from time import time
from typing import Any, Callable

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from faster_whisper import WhisperModel

import url_to_mp3
from clean_with_Llama import (
    CloudCleanerUnavailable,
    clean_transcript_file,
    clean_transcript_file_with_groq,
)


os.environ["PATH"] += os.pathsep + r"C:\Users\Mahmoud\Downloads\Compressed\ffmpeg-8.1-essentials_build\ffmpeg-8.1-essentials_build\bin"

MODEL_SIZE = "large-v3"
DEVICE = "cuda"
COMPUTE_TYPE = "int8_float16"
MODEL_PATH = r"C:\Users\Mahmoud\models\faster-whisper-large-v3"
DEFAULT_LANGUAGE = "ar"
TRANSCRIPT_CACHE_FILE = Path("transcript_cache.json")
TRANSCRIPT_PROMPT_VERSION = "original-language-v1"
whisper_model_lock = Lock()
whisper_models: dict[tuple[str, str, str], WhisperModel] = {}
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


def get_output_paths_for_audio(audio_path: Path) -> tuple[Path, Path]:
    """Derive raw and cleaned transcript paths from an MP3 path.

    Purpose:
        Keep output naming consistent across cache checks and new processing.
    Args:
        audio_path: Downloaded or cached audio file path.
    Returns:
        A `(raw_transcript_path, cleaned_transcript_path)` tuple.
    Workflow:
        Uses the audio filename stem and the project's two output directories.
    Connects to:
        Called by `process_youtube_url` after audio download.
    """
    raw_transcript = Path("OutputForWhisper") / f"{audio_path.stem}_transcript.txt"
    cleaned_transcript = Path("OutputForOllama") / f"{audio_path.stem}_transcript_cleanedv5.txt"
    return raw_transcript, cleaned_transcript


def list_ollama_output_filenames(output_dir: Path = Path("OutputForOllama")) -> list[str]:
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
    """Load and normalize the persistent video-to-transcript cache.

    Purpose:
        Restore cleaned transcript locations across API and machine restarts.
    Args:
        None.
    Returns:
        A dictionary containing normalized `videos` and `llama_folder_filenames` fields.
    Workflow:
        Reads JSON when available, recovers from invalid files, validates field types,
        and refreshes missing filename inventory data.
    Connects to:
        Calls `list_ollama_output_filenames`; used by cache lookup and cache writes.
    """
    if not TRANSCRIPT_CACHE_FILE.exists():
        return {"videos": {}, "llama_folder_filenames": list_ollama_output_filenames()}

    try:
        data = json.loads(TRANSCRIPT_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"videos": {}, "llama_folder_filenames": list_ollama_output_filenames()}

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
        Removes obsolete per-video filename fields, refreshes the cleaned-folder
        inventory, writes a temporary JSON file, and replaces the live cache file.
    Connects to:
        Calls `list_ollama_output_filenames`; used by all cache mutation functions.
    """
    videos = cache.get("videos", {})
    if isinstance(videos, dict):
        for entry in videos.values():
            if isinstance(entry, dict):
                for field in LEGACY_FILENAME_CACHE_FIELDS:
                    entry.pop(field, None)

    cache["llama_folder_filenames"] = list_ollama_output_filenames()
    tmp = TRANSCRIPT_CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(TRANSCRIPT_CACHE_FILE)


def get_cached_cleaned_entry(video_id: str) -> dict[str, Any] | None:
    """Return a valid cleaned-transcript cache entry for a YouTube video.

    Purpose:
        Skip download, Whisper, and cleaning when a current cleaned result exists.
    Args:
        video_id: Stable YouTube video ID used as the cache key.
    Returns:
        The cache entry when valid, otherwise None.
    Workflow:
        Loads the cache, validates prompt version and file existence, removes obsolete
        fields, refreshes inventory metadata, and deletes stale entries.
    Connects to:
        Calls cache load/save helpers; called by `process_youtube_url`.
    """
    cache = load_transcript_cache()
    entry = cache["videos"].get(video_id)
    if not isinstance(entry, dict):
        return None

    if entry.get("prompt_version") != TRANSCRIPT_PROMPT_VERSION:
        cache["videos"].pop(video_id, None)
        save_transcript_cache(cache)
        return None

    cleaned_path = entry.get("cleaned_transcript_path")
    if cleaned_path and Path(cleaned_path).exists():
        llama_folder_filenames = list_ollama_output_filenames()
        changed = False
        for field in LEGACY_FILENAME_CACHE_FIELDS:
            if field in entry:
                entry.pop(field, None)
                changed = True
        if entry.get("llama_folder_filenames") != llama_folder_filenames:
            cache["llama_folder_filenames"] = llama_folder_filenames
            changed = True
        if changed:
            save_transcript_cache(cache)
        return entry

    cache["videos"].pop(video_id, None)
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
) -> dict[str, Any]:
    """Create or update a video's cleaned-transcript cache entry.

    Purpose:
        Record enough metadata to reuse a completed result on future requests.
    Args:
        video_id: YouTube video ID used as the cache key.
        original_url: URL submitted by the user.
        canonical_url: Normalized single-video URL.
        raw_path: Path to the Faster-Whisper transcript.
        cleaned_path: Path to the Groq or Ollama result.
        cleaner_provider: Provider that produced the cleaned file, when known.
    Returns:
        The cache entry that was saved.
    Workflow:
        Preserves the original creation time, updates paths and timestamps, then writes
        the complete cache atomically.
    Connects to:
        Calls cache load/save helpers; used by output discovery and the main pipeline.
    """
    cache = load_transcript_cache()
    existing = cache["videos"].get(video_id, {})
    now = time()
    entry = {
        "video_id": video_id,
        "canonical_url": canonical_url,
        "original_url": original_url,
        "raw_transcript_path": str(raw_path),
        "cleaned_transcript_path": str(cleaned_path),
        "cleaner_provider": cleaner_provider,
        "prompt_version": TRANSCRIPT_PROMPT_VERSION,
        "created_at": existing.get("created_at", now) if isinstance(existing, dict) else now,
        "updated_at": now,
    }
    cache["videos"][video_id] = entry
    save_transcript_cache(cache)
    return entry


def find_existing_cleaned_output(
    video_id: str,
    *,
    original_url: str,
    canonical_url: str,
) -> dict[str, Any] | None:
    """Discover an existing cleaned file and register it in the JSON cache.

    Purpose:
        Recover reusable outputs that exist on disk but are missing from cache metadata.
    Args:
        video_id: YouTube video ID that prefixes output filenames.
        original_url: URL submitted for the current request.
        canonical_url: Normalized URL saved with the recovered entry.
    Returns:
        A newly saved cache entry, or None when no matching file exists.
    Workflow:
        Searches cleaned outputs by video ID, selects the newest eligible file, derives
        its raw transcript path, and stores a cache entry.
    Connects to:
        Calls `save_cleaned_cache_entry`; called by `process_youtube_url`.
    """
    output_dir = Path("OutputForOllama")
    if not output_dir.exists():
        return None

    matches = sorted(
        (
            path
            for path in output_dir.glob(f"{video_id}_*_transcript_cleanedv5.txt")
            if "_english_transcript_cleanedv5" not in path.name
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return None

    cleaned_path = matches[0]
    raw_name = cleaned_path.name.replace("_cleanedv5.txt", ".txt")
    raw_path = Path("OutputForWhisper") / raw_name
    return save_cleaned_cache_entry(
        video_id,
        original_url=original_url,
        canonical_url=canonical_url,
        raw_path=raw_path,
        cleaned_path=cleaned_path,
    )


def clean_transcript_with_preferred_model(
    raw_path: Path,
    *,
    allow_ollama_fallback: bool,
    progress_callback: ProgressCallback | None = None,
) -> tuple[Path | None, str | None, str | None]:
    """Clean a raw transcript using Groq first and optionally Ollama second.

    Purpose:
        Implement the model-selection policy shared by fast and better-format modes.
    Args:
        raw_path: Faster-Whisper transcript to clean.
        allow_ollama_fallback: Whether a Groq failure should start local Ollama cleaning.
        progress_callback: Optional callback for formatting-stage updates.
    Returns:
        `(cleaned_path, provider_name, groq_error)`; values may be None when no cleaner
        succeeds or fallback is disabled.
    Workflow:
        Attempts Groq, records any failure, returns early in fast mode, or invokes Ollama
        as the better-format fallback.
    Connects to:
        Calls both cleaner entry points and `emit_progress`; used by
        `process_youtube_url`.
    """
    groq_error = None

    try:
        emit_progress(progress_callback, "formatting", detail="Formatting transcript with Groq")
        print("Running Groq cleaner ...")
        cleaned = clean_transcript_file_with_groq(raw_path, progress_callback=progress_callback)
        if cleaned:
            return cleaned, "groq", None
    except (CloudCleanerUnavailable, Exception) as exc:
        groq_error = f"{type(exc).__name__}: {exc}"
        print(f"Groq cleaner unavailable: {groq_error}")

    if not allow_ollama_fallback:
        return None, None, groq_error

    emit_progress(progress_callback, "formatting", detail="Groq unavailable. Formatting transcript with Ollama")
    print("Running Ollama cleaner ...")
    cleaned = clean_transcript_file(raw_path, progress_callback=progress_callback)
    return cleaned, "ollama", groq_error


def find_existing_raw_output(video_id: str) -> Path | None:
    """Find the newest raw Faster-Whisper output for a YouTube video.

    Purpose:
        Reuse transcription work when no cleaned result is available.
    Args:
        video_id: YouTube video ID expected at the start of the filename.
    Returns:
        Path to the newest matching transcript, or None when absent.
    Workflow:
        Searches the raw-output directory, removes duplicate resolved paths, sorts by
        modification time, and selects the newest file.
    Connects to:
        Called by `process_youtube_url` before audio download.
    """
    output_dir = Path("OutputForWhisper")
    if not output_dir.exists():
        return None

    patterns = (f"{video_id}_*_transcript.txt",)
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(output_dir.glob(pattern))

    matches = sorted(
        {path.resolve(): path for path in matches}.values(),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def delete_audio_file(audio_path: Path) -> tuple[bool, str | None]:
    """Delete a downloaded audio file after successful transcript cleaning.

    Purpose:
        Reduce repository and disk usage while retaining raw and cleaned text outputs.
    Args:
        audio_path: MP3 file to remove.
    Returns:
        `(deleted, error_message)`; a missing file returns `(False, None)`.
    Workflow:
        Checks existence, attempts `Path.unlink`, and converts OS errors to result data.
    Connects to:
        Called by `process_youtube_url` after a cleaned result is secured.
    """
    if not audio_path.exists():
        return False, None

    try:
        audio_path.unlink()
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"

    return True, None


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
    print("Cached audio and final Ollama output found for this file:")
    print(cleaned_path.resolve())
    print("\n===== FINAL OLLAMA OUTPUT =====\n")
    print(cleaned_path.read_text(encoding="utf-8", errors="ignore"))


def build_initial_prompt() -> str:
    """Build the Faster-Whisper context prompt for mixed-language lectures.

    Purpose:
        Encourage original-language transcription while preserving English terminology.
    Args:
        None.
    Returns:
        Prompt text passed to Faster-Whisper's `initial_prompt` option.
    Workflow:
        Combines fixed language, script, terminology, and repetition instructions.
    Connects to:
        Called by `transcribe_audio`.
    """
    return (
        "This is a university lecture in Arabic with English technical terms.\n"
        "Transcribe the speech in the original spoken language.\n"
        "Do not translate Arabic speech into English.\n"
        "Write Arabic speech using Arabic script.\n"
        "Keep English technical terms correctly when they appear.\n"
        "Avoid repeated outro text, repeated greetings, and invented phrases.\n"
    )


def get_whisper_model(
    *,
    model_path: str = MODEL_PATH,
    device: str = DEVICE,
    compute_type: str = COMPUTE_TYPE,
) -> WhisperModel:
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


def transcribe_audio(
    audio_path: Path,
    *,
    model_path: str = MODEL_PATH,
    model_size: str = MODEL_SIZE,
    device: str = DEVICE,
    compute_type: str = COMPUTE_TYPE,
    language: str = DEFAULT_LANGUAGE,
    progress_callback: ProgressCallback | None = None,
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
        language: Whisper language code supplied to transcription.
        progress_callback: Optional callback for transcription-stage updates.
    Returns:
        `(raw_transcript_path, metadata)` containing detected language information.
    Raises:
        FileNotFoundError: If the audio path does not exist.
    Workflow:
        Loads the cached model, transcribes with VAD, beam search, and the initial
        prompt, joins segments, writes UTF-8 text, and reports metadata.
    Connects to:
        Calls `get_whisper_model`, `build_initial_prompt`, and `emit_progress`; called by
        `process_youtube_url`.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"File not found: {audio_path.resolve()}")

    emit_progress(progress_callback, "transcribing", detail="Loading faster-whisper model")
    print(f"Loading faster-whisper model: {model_size} on {device} ({compute_type}) ...")
    model = get_whisper_model(
        model_path=model_path,
        device=device,
        compute_type=compute_type,
    )

    emit_progress(progress_callback, "transcribing", detail="Whisper is transcribing the lecture")
    print("Transcribing ...")
    kwargs: dict[str, Any] = {
        "language": language,
        "vad_filter": True,
        "beam_size": 5,
        "initial_prompt": build_initial_prompt(),
    }

    segments, info = model.transcribe(audio_path.as_posix(), **kwargs)

    text = " ".join(seg.text.strip() for seg in segments)

    print("\n===== TRANSCRIPT =====\n")
    print(text)

    output_dir = Path("OutputForWhisper")
    output_dir.mkdir(exist_ok=True)

    out = output_dir / f"{audio_path.stem}_transcript.txt"
    out.write_text(text, encoding="utf-8")

    print(f"\nSaved to: {out.resolve()}")
    print("\n===== INFO =====")
    print("Detected language:", info.language)
    print("Language probability:", getattr(info, "language_probability", "N/A"))

    metadata = {
        "detected_language": info.language,
        "language_probability": getattr(info, "language_probability", None),
    }
    emit_progress(progress_callback, "transcribing", detail="Whisper transcription finished")
    return out, metadata


def process_youtube_url(
    youtube_url: str,
    *,
    clean: bool = True,
    skip_audio_cache: bool = False,
    use_cached_outputs: bool = True,
    language: str = DEFAULT_LANGUAGE,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run the complete cached YouTube-to-transcript pipeline.

    Purpose:
        Coordinate URL validation, cache reuse, audio download, Whisper transcription,
        model cleaning, cache persistence, and MP3 cleanup.
    Args:
        youtube_url: YouTube lecture URL to process.
        clean: Enables Ollama fallback when Groq is unavailable.
        skip_audio_cache: Forces yt-dlp to download audio again.
        use_cached_outputs: Allows reuse of cleaned and raw transcript files.
        language: Whisper language code.
        progress_callback: Optional callback receiving pipeline stage updates.
    Returns:
        A result dictionary containing output paths, cache flags, provider information,
        transcription metadata, and audio deletion status.
    Raises:
        ValueError: If the URL does not contain a valid video ID.
        Download, transcription, or cleaner exceptions when required work fails.
    Workflow:
        Checks cleaned then raw caches, downloads only when needed, transcribes missing
        raw text, tries Groq and optional Ollama, saves successful cleaned output in the
        cache, and deletes the MP3 after cleaning.
    Connects to:
        Orchestrates helpers in this module, `url_to_mp3`, and `clean_with_Llama`; called
        by API background jobs and `main`.
    """
    emit_progress(progress_callback, "checking_cache", detail="Checking saved transcripts")
    video_id = url_to_mp3.extract_youtube_video_id(youtube_url)
    if not video_id:
        raise ValueError("Input is NOT a valid YouTube video URL")

    canonical_url = url_to_mp3.force_single_video_url(youtube_url)
    result: dict[str, Any] = {
        "cache_key": video_id,
        "canonical_url": canonical_url,
        "audio_path": None,
        "raw_transcript_path": None,
        "cleaned_transcript_path": None,
        "used_cached_raw_transcript": False,
        "used_cached_cleaned_transcript": False,
        "transcription_info": None,
        "cleaner_provider": None,
        "cleaner_error": None,
        "audio_deleted": False,
        "audio_delete_error": None,
    }

    if use_cached_outputs:
        cached_entry = get_cached_cleaned_entry(video_id)
        if not cached_entry:
            cached_entry = find_existing_cleaned_output(
                video_id,
                original_url=youtube_url,
                canonical_url=canonical_url,
            )
        if cached_entry:
            emit_progress(progress_callback, "cache_hit", detail="Using saved Ollama output")
            result.update(
                {
                    "raw_transcript_path": cached_entry.get("raw_transcript_path"),
                    "cleaned_transcript_path": cached_entry.get("cleaned_transcript_path"),
                    "cleaner_provider": cached_entry.get("cleaner_provider"),
                    "used_cached_cleaned_transcript": True,
                }
            )
            return result

    if use_cached_outputs:
        existing_raw_path = find_existing_raw_output(video_id)
        if existing_raw_path:
            result["raw_transcript_path"] = str(existing_raw_path)
            result["used_cached_raw_transcript"] = True

            cleaned, cleaner_provider, cleaner_error = clean_transcript_with_preferred_model(
                existing_raw_path,
                allow_ollama_fallback=clean,
                progress_callback=progress_callback,
            )
            result["cleaned_transcript_path"] = str(cleaned) if cleaned else None
            result["cleaner_provider"] = cleaner_provider
            result["cleaner_error"] = cleaner_error
            if cleaned:
                save_cleaned_cache_entry(
                    video_id,
                    original_url=youtube_url,
                    canonical_url=canonical_url,
                    raw_path=existing_raw_path,
                    cleaned_path=cleaned,
                    cleaner_provider=cleaner_provider,
                )
            elif not clean:
                emit_progress(progress_callback, "cache_hit", detail="Using saved Whisper output")
            return result

    emit_progress(progress_callback, "downloading", detail="Downloading audio from YouTube")
    audio_path = Path(
        url_to_mp3.download_youtube_mp3(
            youtube_url,
            skip_cache=skip_audio_cache,
        )
    )
    emit_progress(progress_callback, "downloading", detail="Audio download finished")
    raw_path, cleaned_path = get_output_paths_for_audio(audio_path)
    result["audio_path"] = str(audio_path)
    result["raw_transcript_path"] = str(raw_path)
    result["cleaned_transcript_path"] = str(cleaned_path)

    if use_cached_outputs and cleaned_path.exists():
        emit_progress(progress_callback, "cache_hit", detail="Using saved cleaned transcript")
        save_cleaned_cache_entry(
            video_id,
            original_url=youtube_url,
            canonical_url=canonical_url,
            raw_path=raw_path,
            cleaned_path=cleaned_path,
        )
        audio_deleted, audio_delete_error = delete_audio_file(audio_path)
        result["used_cached_cleaned_transcript"] = True
        result["audio_deleted"] = audio_deleted
        result["audio_delete_error"] = audio_delete_error
        return result

    if use_cached_outputs and raw_path.exists():
        result["used_cached_raw_transcript"] = True
    else:
        raw_path, transcription_info = transcribe_audio(
            audio_path,
            language=language,
            progress_callback=progress_callback,
        )
        result["raw_transcript_path"] = str(raw_path)
        result["transcription_info"] = transcription_info

    cleaned, cleaner_provider, cleaner_error = clean_transcript_with_preferred_model(
        raw_path,
        allow_ollama_fallback=clean,
        progress_callback=progress_callback,
    )
    result["cleaned_transcript_path"] = str(cleaned) if cleaned else None
    result["cleaner_provider"] = cleaner_provider
    result["cleaner_error"] = cleaner_error
    if cleaned:
        emit_progress(progress_callback, "saving", detail="Saving cache and deleting audio")
        save_cleaned_cache_entry(
            video_id,
            original_url=youtube_url,
            canonical_url=canonical_url,
            raw_path=raw_path,
            cleaned_path=cleaned,
            cleaner_provider=cleaner_provider,
        )
        audio_deleted, audio_delete_error = delete_audio_file(audio_path)
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
