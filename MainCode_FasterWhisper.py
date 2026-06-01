import os
from pathlib import Path
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


def get_output_paths_for_audio(audio_path: Path) -> tuple[Path, Path]:
    raw_transcript = Path("OutputForWhisper") / f"{audio_path.stem}_transcript.txt"
    cleaned_transcript = Path("OutputForOllama") / f"{audio_path.stem}_transcript_cleanedv5.txt"
    return raw_transcript, cleaned_transcript


def print_final_output(cleaned_path: Path) -> None:
    print("Cached audio and final Ollama output found for this file:")
    print(cleaned_path.resolve())
    print("\n===== FINAL OLLAMA OUTPUT =====\n")
    print(cleaned_path.read_text(encoding="utf-8", errors="ignore"))


def build_initial_prompt() -> str:
    return (
        "This is a university lecture in Arabic with English technical terms.\n"
        "Keep English technical terms correctly when they appear.\n"
    )


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
    print(f"Loading faster-whisper model from: {model_path}")
    model = WhisperModel(
        model_path,
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
    audio_path = Path(
        url_to_mp3.download_youtube_mp3(
            youtube_url,
            skip_cache=skip_audio_cache,
        )
    )
    raw_path, cleaned_path = get_output_paths_for_audio(audio_path)
    result: dict[str, Any] = {
        "audio_path": str(audio_path),
        "raw_transcript_path": str(raw_path),
        "cleaned_transcript_path": str(cleaned_path) if clean else None,
        "used_cached_raw_transcript": False,
        "used_cached_cleaned_transcript": False,
        "transcription_info": None,
    }

    if clean and use_cached_outputs and cleaned_path.exists():
        result["used_cached_cleaned_transcript"] = True
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
        cleaned = clean_transcript_file(raw_path)
        result["cleaned_transcript_path"] = str(cleaned) if cleaned else None

    return result


def main() -> None:
    youtube_url = url_to_mp3.prompt_for_youtube_url("Enter YouTube lecture link: ")

    try:
        result = process_youtube_url(youtube_url)
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
