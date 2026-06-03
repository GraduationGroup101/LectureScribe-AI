import json
import os
from pathlib import Path
from threading import Lock
from time import time
from typing import Any

from faster_whisper import WhisperModel

import url_to_mp3
from clean_with_Llama import clean_transcript_file


os.environ["PATH"] += os.pathsep + r"C:\Users\Mahmoud\Downloads\Compressed\ffmpeg-8.1-essentials_build\ffmpeg-8.1-essentials_build\bin"

MODEL_SIZE = "large-v3"
DEVICE = "cuda"
COMPUTE_TYPE = "int8_float16"
MODEL_PATH = r"C:\Users\Mahmoud\models\faster-whisper-large-v3"
DEFAULT_LANGUAGE = "ar"
TRANSCRIPT_CACHE_FILE = Path("transcript_cache.json")
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


def get_output_paths_for_audio(audio_path: Path) -> tuple[Path, Path]:
    raw_transcript = Path("OutputForWhisper") / f"{audio_path.stem}_transcript.txt"
    cleaned_transcript = Path("OutputForOllama") / f"{audio_path.stem}_transcript_cleanedv5.txt"
    return raw_transcript, cleaned_transcript


def list_ollama_output_filenames(output_dir: Path = Path("OutputForOllama")) -> list[str]:
    if not output_dir.exists():
        return []
    return sorted(path.name for path in output_dir.iterdir() if path.is_file())


def load_transcript_cache() -> dict[str, Any]:
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
    cache = load_transcript_cache()
    entry = cache["videos"].get(video_id)
    if not isinstance(entry, dict):
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
) -> dict[str, Any]:
    cache = load_transcript_cache()
    existing = cache["videos"].get(video_id, {})
    now = time()
    entry = {
        "video_id": video_id,
        "canonical_url": canonical_url,
        "original_url": original_url,
        "raw_transcript_path": str(raw_path),
        "cleaned_transcript_path": str(cleaned_path),
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
    output_dir = Path("OutputForOllama")
    if not output_dir.exists():
        return None

    matches = sorted(
        output_dir.glob(f"{video_id}_*_transcript_cleanedv5.txt"),
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


def delete_audio_file(audio_path: Path) -> tuple[bool, str | None]:
    if not audio_path.exists():
        return False, None

    try:
        audio_path.unlink()
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"

    return True, None


def print_final_output(cleaned_path: Path) -> None:
    print("Cached audio and final Ollama output found for this file:")
    print(cleaned_path.resolve())
    print("\n===== FINAL OLLAMA OUTPUT =====\n")
    print(cleaned_path.read_text(encoding="utf-8", errors="ignore"))


def build_initial_prompt() -> str:
    return (
        "This is a university lecture in Arabic with English technical terms.\n"
        "Keep English technical terms correctly when they appear.\n"
        "Correctly handle Arabic diacritics and punctuation.\n"
        "Transcribe the lecture accurately, preserving the original language and technical terms.\n"
    )


def get_whisper_model(
    *,
    model_path: str = MODEL_PATH,
    device: str = DEVICE,
    compute_type: str = COMPUTE_TYPE,
) -> WhisperModel:
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
) -> tuple[Path, dict[str, Any]]:
    if not audio_path.exists():
        raise FileNotFoundError(f"File not found: {audio_path.resolve()}")

    print(f"Loading faster-whisper model: {model_size} on {device} ({compute_type}) ...")
    model = get_whisper_model(
        model_path=model_path,
        device=device,
        compute_type=compute_type,
    )

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
    return out, metadata


def process_youtube_url(
    youtube_url: str,
    *,
    clean: bool = True,
    skip_audio_cache: bool = False,
    use_cached_outputs: bool = True,
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, Any]:
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
        "audio_deleted": False,
        "audio_delete_error": None,
    }

    if clean and use_cached_outputs:
        cached_entry = get_cached_cleaned_entry(video_id)
        if not cached_entry:
            cached_entry = find_existing_cleaned_output(
                video_id,
                original_url=youtube_url,
                canonical_url=canonical_url,
            )
        if cached_entry:
            result.update(
                {
                    "raw_transcript_path": cached_entry.get("raw_transcript_path"),
                    "cleaned_transcript_path": cached_entry.get("cleaned_transcript_path"),
                    "used_cached_cleaned_transcript": True,
                }
            )
            return result

    audio_path = Path(
        url_to_mp3.download_youtube_mp3(
            youtube_url,
            skip_cache=skip_audio_cache,
        )
    )
    raw_path, cleaned_path = get_output_paths_for_audio(audio_path)
    result["audio_path"] = str(audio_path)
    result["raw_transcript_path"] = str(raw_path)
    result["cleaned_transcript_path"] = str(cleaned_path) if clean else None

    if clean and use_cached_outputs and cleaned_path.exists():
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
        )
        result["raw_transcript_path"] = str(raw_path)
        result["transcription_info"] = transcription_info

    if clean:
        print("Running Ollama cleaner ...")
        cleaned = clean_transcript_file(raw_path)
        result["cleaned_transcript_path"] = str(cleaned) if cleaned else None
        if cleaned:
            save_cleaned_cache_entry(
                video_id,
                original_url=youtube_url,
                canonical_url=canonical_url,
                raw_path=raw_path,
                cleaned_path=cleaned,
            )
            audio_deleted, audio_delete_error = delete_audio_file(audio_path)
            result["audio_deleted"] = audio_deleted
            result["audio_delete_error"] = audio_delete_error

    return result


def main() -> None:
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
