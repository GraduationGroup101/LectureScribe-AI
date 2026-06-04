from pathlib import Path
import os
import requests
import re
from typing import Any, Callable

# =========================
# SETTINGS
# =========================
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.1:8b-instruct-q4_K_M"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"

# split text into chunks of roughly this many characters
CHUNK_CHARS = 3500
GROQ_CHUNK_CHARS = 1200

# =========================
# PROMPT
# =========================
SYSTEM_RULES = """You are a transcript formatter and translator for university lectures.
Your job: format ASR transcript text, then translate the formatted result into English.
Rules:
- Final output must be English.
- First preserve the transcript content and structure, then translate it faithfully into English.
- Do not add new ideas, examples, explanations, or lecture details.
- Do not delete ideas, examples, explanations, or lecture details.
- Do not paraphrase beyond what is necessary for faithful English translation.
- Do not summarize.
- Preserve the original order of words, sentences, and ideas as much as English allows.
- Keep English technical terms, variables, equations, function names, code-like phrases, punctuation, and numbers clear.
- Improve visual formatting with line breaks, paragraph breaks, and indentation.
- Return only the formatted English transcript.
"""


def make_user_prompt(text: str) -> str:
    return f"""Format this transcript, then translate the final formatted result to English.
Do not summarize. Do not add new information. Do not delete any information.
Preserve the original order and keep technical terms, variables, equations, code-like phrases, and numbers clear.

Transcript:
{text}
"""


class CloudCleanerUnavailable(RuntimeError):
    pass


def split_long_text(text: str, max_chars: int):
    """Split a long paragraph or sentence on word boundaries."""
    words = text.split()
    chunks = []
    buf = ""

    for word in words:
        candidate = f"{buf} {word}".strip()
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                chunks.append(buf)
            buf = word

    if buf:
        chunks.append(buf)

    return chunks


def split_sentences(text: str):
    """Split text into sentence-like units."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    parts = re.split(r"(?<=[\.\!\?\u061f])\s+", text)
    return [part.strip() for part in parts if part.strip()]


def split_text(text: str, max_chars: int):
    """Split text into natural chunks with paragraph and sentence boundaries."""
    parts = re.split(r"\n{2,}", text.strip())
    chunks = []
    buf = ""

    for p in parts:
        p = p.strip()
        if not p:
            continue

        sentences = split_sentences(p)
        units = sentences if sentences else [p]

        for unit in units:
            if len(unit) > max_chars:
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.extend(split_long_text(unit, max_chars))
                continue

            if not buf:
                buf = unit
            elif len(buf) + len(unit) + 1 <= max_chars:
                buf = f"{buf} {unit}".strip()
            else:
                chunks.append(buf)
                buf = unit

    if buf:
        chunks.append(buf)

    return chunks


def normalize_for_compare(text: str) -> str:
    """Normalize text so repeated phrases can be compared reliably."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def collapse_repeated_word_blocks(text: str, max_block_words: int = 12) -> str:
    """Collapse immediately repeated word blocks in a single pass."""
    words = text.split()
    if not words:
        return text

    out = []
    i = 0
    n = len(words)

    while i < n:
        best_len = 0
        best_repeat = 1

        for block_len in range(min(max_block_words, n - i) // 2, 0, -1):
            block = words[i:i + block_len]
            repeat = 1
            while i + (repeat + 1) * block_len <= n and words[i + repeat * block_len:i + (repeat + 1) * block_len] == block:
                repeat += 1
            if repeat > 1:
                best_len = block_len
                best_repeat = repeat
                break

        if best_len:
            out.extend(words[i:i + best_len])
            i += best_len * best_repeat
        else:
            out.append(words[i])
            i += 1

    return " ".join(out)


def dedupe_consecutive_units(text: str) -> str:
    """Remove consecutive duplicate sentences or lines from text."""
    lines = [line.strip() for line in text.splitlines()]
    cleaned_lines = []
    previous = ""

    for line in lines:
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue

        normalized = normalize_for_compare(line)
        if normalized and normalized == previous:
            continue

        cleaned_lines.append(line)
        previous = normalized

    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    parts = re.split(r"(?<=[\.\!\?\u061f])\s+", text)
    merged = []
    previous = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        normalized = normalize_for_compare(part)
        if normalized and normalized == previous:
            continue
        merged.append(part)
        previous = normalized

    return " ".join(merged).strip()


def ollama_generate(prompt: str) -> str:
    """Call Ollama local API."""
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "system": SYSTEM_RULES,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "top_p": 0.9,
            "num_predict": 1200,
        },
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=600)
    r.raise_for_status()
    data = r.json()
    return data.get("response", "").strip()


def load_local_env(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE lines from .env without overriding real env vars."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def groq_generate(prompt: str) -> str:
    """Call Groq's OpenAI-compatible chat completions API."""
    load_local_env()
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise CloudCleanerUnavailable("GROQ_API_KEY is not set.")

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "top_p": 0.9,
        "max_completion_tokens": 4096,
        "reasoning_effort": "low",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(GROQ_URL, json=payload, headers=headers, timeout=180)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise CloudCleanerUnavailable(f"Groq cleaner failed: {exc}") from exc

    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise CloudCleanerUnavailable("Groq returned no choices.")

    message = choices[0].get("message") or {}
    content = message.get("content", "").strip()
    if not content:
        finish_reason = choices[0].get("finish_reason", "unknown")
        usage = data.get("usage") or {}
        completion_tokens = usage.get("completion_tokens", "unknown")
        raise CloudCleanerUnavailable(
            "Groq returned an empty response "
            f"(finish_reason={finish_reason}, completion_tokens={completion_tokens})."
        )

    return content


ProgressCallback = Callable[[str, dict[str, Any]], None]


def clean_transcript_with_generator(
    input_txt: Path,
    generate_fn: Callable[[str], str],
    provider_name: str,
    chunk_chars: int = CHUNK_CHARS,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    if not input_txt.exists():
        print(f"Input file not found: {input_txt.resolve()}")
        return None

    raw = input_txt.read_text(encoding="utf-8", errors="ignore").strip()
    if not raw:
        print("Input file is empty.")
        return None

    # Remove obvious repeated blocks before chunking so the model sees less noise.
    raw = collapse_repeated_word_blocks(raw)

    output_dir = Path("OutputForOllama")
    output_dir.mkdir(exist_ok=True)
    output_txt = output_dir / f"{input_txt.stem}_cleanedv5.txt"

    chunks = split_text(raw, chunk_chars)
    print(f"Loaded text. Chunks: {len(chunks)}")
    if progress_callback:
        progress_callback(
            "formatting",
            {
                "detail": f"{provider_name} is preparing transcript chunks",
                "chunk_index": 0,
                "chunk_total": len(chunks),
            },
        )

    cleaned_chunks = []
    for i, ch in enumerate(chunks, start=1):
        print(f"{provider_name} cleaning chunk {i}/{len(chunks)} ...")
        if progress_callback:
            progress_callback(
                "formatting",
                {
                    "detail": f"{provider_name} cleaning chunk {i} of {len(chunks)}",
                    "chunk_index": i,
                    "chunk_total": len(chunks),
                },
            )
        out = generate_fn(make_user_prompt(ch))
        cleaned_chunks.append(dedupe_consecutive_units(out))

    cleaned = "\n\n".join(cleaned_chunks).strip()
    cleaned = dedupe_consecutive_units(cleaned)
    output_txt.write_text(cleaned, encoding="utf-8")

    print("\n DONE")
    print(f"Saved cleaned transcript to: {output_txt.resolve()}")

    return output_txt


def clean_transcript_file(
    input_txt: Path,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    return clean_transcript_with_generator(
        input_txt,
        ollama_generate,
        "Ollama",
        CHUNK_CHARS,
        progress_callback,
    )


def clean_transcript_file_with_groq(
    input_txt: Path,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    return clean_transcript_with_generator(
        input_txt,
        groq_generate,
        "Groq",
        GROQ_CHUNK_CHARS,
        progress_callback,
    )


# if you want to run this file alone to clean a transcript without running the faster-whisper code, you can do that by putting the name of the transcript file in the same directory as this cleaner.py file and then run it. it will produce a cleaned version of the transcript with the name "OutputForOllama_" + input_file_name + "_cleanedv5.txt"
if __name__ == "__main__":
    INPUT_TXT = Path("Qbqc5MoGk5E_IUG Renewable energy Lab 7 _ Broken solar panel part 3_transcript.txt")
    clean_transcript_file(INPUT_TXT)
