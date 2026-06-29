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
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "openai/gpt-oss-120b"

# split text into chunks of roughly this many characters
CHUNK_CHARS = 3500
CLOUD_CHUNK_CHARS = 3000
OPENROUTER_TIMEOUT_SECONDS = 300
OPENROUTER_MAX_TOKENS = 8192

ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06ff]")

# =========================
# PROMPT
# =========================
SYSTEM_RULES = """You format ASR lecture transcripts into faithful English text.
Rules:
- Output English only. Translate any Arabic or mixed Arabic-English text into English.
- Preserve every lecture detail, fact, example, number, symptom, drug, test, and technical term.
- Do not summarize, shorten, merge unrelated points, or replace a term with a different term.
- Do not add information that is not present in the transcript.
- Keep the original lecture order.
- Fix only obvious ASR issues: broken encoding, punctuation, repeated phrases, filler words, and stuttering.
- Use clear Markdown: headings, short paragraphs, and bullet points only when the transcript naturally lists items, steps, symptoms, examples, or comparisons.
- Put each bullet, numbered item, and table row on its own line.
- Keep technical explanations correct. Do not make cumulative ACKs, sequence numbers, or other technical terms mean something they do not mean.
- If a word or phrase is unclear, keep the closest safe wording  .
- Do not invent information, add commentary, or mention these instructions.
- Return only the final formatted transcript.
"""


def make_user_prompt(text: str) -> str:
    """Wrap one transcript chunk in the model's user prompt.

    Purpose:
        Give OpenRouter or Ollama a consistent instruction around each transcript chunk.
    Args:
        text: Transcript content to clean.
    Returns:
        A complete user prompt containing the transcript.
    Workflow:
        Prefixes the provided text with a short cleaning instruction.
    Connects to:
        Called by `clean_transcript_with_generator` before invoking a model generator.
    """
    return f"""Format this transcript chunk into faithful English notes.
Do not summarize. Do not delete any details. Do not change facts or technical terms.
Keep the lecture order and keep all examples, numbers, symptoms, tests, medicines, and exceptions.
Use bullet points only for natural lists, steps, symptoms, examples, or comparisons.
Use short paragraphs for normal explanation text.
If the transcript contains encoding artifacts such as "â€™", "â€“", or "â€œ", repair them.
Use clean Markdown. Do not put multiple bullets on one line.
Do not add unrelated sections or examples that are not supported by this transcript chunk.

Transcript:
{text}
"""


def has_arabic_script(text: str) -> bool:
    """Return whether text still contains Arabic-script characters."""
    return bool(ARABIC_CHAR_RE.search(text))


def make_english_repair_prompt(text: str) -> str:
    """Build a strict repair prompt for outputs that still contain Arabic text."""
    return f"""The text below still contains Arabic or mixed-language content.
Translate it into English only while preserving every detail, fact, example, number, and technical term.
Do not summarize, shorten, add new information, or change the formatting style.
Repair broken encoding artifacts and keep each bullet on its own line.

Text:
{text}
"""


class CloudCleanerUnavailable(RuntimeError):
    """Signal that the remote cloud cleaner cannot provide a usable result.

    Purpose:
        Distinguish recoverable cloud-cleaner failures from general pipeline errors.
    Args:
        Inherits the standard exception message arguments from `RuntimeError`.
    Returns:
        Not applicable; this class represents an exception.
    Workflow:
        Raised for missing credentials, request failures, or empty cloud responses.
    Connects to:
        Raised by `openrouter_generate` and handled by the preferred-model pipeline.
    """
    pass


def split_long_text(text: str, max_chars: int):
    """Split oversized text into chunks without breaking individual words.

    Purpose:
        Keep model requests under the configured character limit.
    Args:
        text: Long sentence or paragraph to divide.
        max_chars: Preferred maximum characters per chunk.
    Returns:
        A list of word-boundary chunks in their original order.
    Workflow:
        Adds words to a buffer until the next word would exceed the limit, then starts
        a new chunk.
    Connects to:
        Called by `split_text` when a sentence is larger than one model chunk.
    """
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
    """Divide text into sentence-like units using Arabic and English punctuation.

    Purpose:
        Preserve natural boundaries when preparing transcript model chunks.
    Args:
        text: Transcript paragraph to split.
    Returns:
        A list of non-empty sentence-like strings.
    Workflow:
        Normalizes whitespace and splits after `.`, `!`, `?`, or Arabic question marks.
    Connects to:
        Called by `split_text`.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    parts = re.split(r"(?<=[\.\!\?\u061f])\s+", text)
    return [part.strip() for part in parts if part.strip()]


def split_text(text: str, max_chars: int):
    """Build size-limited chunks while preserving paragraph and sentence order.

    Purpose:
        Prepare transcript requests that fit model context and output limits.
    Args:
        text: Complete transcript text.
        max_chars: Preferred maximum characters per generated chunk.
    Returns:
        Ordered transcript chunks.
    Workflow:
        Splits paragraphs, then sentences, combines units while under the limit, and
        delegates oversized units to `split_long_text`.
    Connects to:
        Calls `split_sentences` and `split_long_text`; used by the shared cleaner.
    """
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
    """Normalize text for duplicate detection without changing saved output.

    Purpose:
        Make punctuation, case, and whitespace differences irrelevant during comparison.
    Args:
        text: Sentence or line to normalize.
    Returns:
        Lowercase text containing normalized word and Arabic-character spacing.
    Workflow:
        Lowercases, replaces non-word punctuation with spaces, and collapses whitespace.
    Connects to:
        Called by `dedupe_consecutive_units`.
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def collapse_repeated_word_blocks(text: str, max_block_words: int = 12) -> str:
    """Collapse immediately repeated sequences of words.

    Purpose:
        Remove common ASR loops before sending text to a language model.
    Args:
        text: Raw transcript text.
        max_block_words: Largest repeated word-block size to detect.
    Returns:
        Text with adjacent repeated blocks reduced to one copy.
    Workflow:
        Scans left to right, searches longest-first for repeated blocks, and advances
        past all repeated copies when a match is found.
    Connects to:
        Called by `clean_transcript_with_generator` before chunking.
    """
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
    """Remove consecutive duplicate lines and sentences from model output.

    Purpose:
        Prevent repeated model or ASR content from appearing in the final transcript.
    Args:
        text: Transcript content to deduplicate.
    Returns:
        Cleaned text with adjacent duplicate units removed.
    Workflow:
        Deduplicates normalized lines, limits blank lines, then repeats the process at
        sentence level.
    Connects to:
        Calls `normalize_for_compare`; used per chunk and on final combined output.
    """
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

    deduped_lines = []
    for line in cleaned_lines:
        if not line:
            deduped_lines.append("")
            continue

        parts = re.split(r"(?<=[\.\!\?\u061f])\s+", line)
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
        deduped_lines.append(" ".join(merged))

    text = "\n".join(deduped_lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def repair_mojibake(text: str) -> str:
    """Repair common UTF-8 text displayed as Windows-1252 artifacts."""
    replacements = {
        "â€™": "'",
        "â€˜": "'",
        "â€œ": '"',
        "â€": '"',
        "â€“": "-",
        "â€”": "-",
        "â€‘": "-",
        "â€¯": " ",
        "Â ": " ",
        "Â": "",
        "â†": "<-",
        "â†’": "->",
        "â€¢": "-",
        "â€¦": "...",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def normalize_markdown_layout(text: str) -> str:
    """Clean Markdown layout after model generation.

    Purpose:
        Keep bullets, numbered lists, tables, and headings readable even when the model
        returns several items on one line.
    Args:
        text: Generated transcript text.
    Returns:
        Text with repaired encoding, list spacing, and paragraph breaks.
    Workflow:
        Fixes mojibake, splits glued list markers onto new lines, trims whitespace, and
        limits blank lines.
    Connects to:
        Called after each model response and once on the final combined transcript.
    """
    text = repair_mojibake(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Split glued bullets like "text. - Next point" and "text. 1. Next step".
    text = re.sub(r"(?<=[\.\!\?:\)])\s+-\s+(?=(?:\*\*)?[A-Za-z0-9`])", "\n- ", text)
    text = re.sub(r"(?<=[\.\!\?:\)])\s+(\d+\.\s+)(?=(?:\*\*)?[A-Za-z])", r"\n\1", text)

    # Put Markdown headings that were glued to the previous sentence on a new paragraph.
    text = re.sub(r"(?<=[a-z0-9\.\)])\s+(#{1,4}\s+)", r"\n\n\1", text)

    # Keep horizontal rules and table rows readable.
    text = re.sub(r"\s+---\s+", "\n\n---\n\n", text)
    text = re.sub(r"\s+(\|[^|\n]+(?:\|[^|\n]+)+\|)", r"\n\1", text)

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    cleaned = []
    for line in lines:
        if not line:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        cleaned.append(line)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def ollama_generate(prompt: str) -> str:
    """Generate cleaned transcript text with the local Ollama server.

    Purpose:
        Provide the offline cleaner used when local formatting is selected or required.
    Args:
        prompt: Fully constructed transcript-cleaning prompt.
    Returns:
        Ollama's stripped response text.
    Raises:
        requests.RequestException: If the local API is unavailable or returns an error.
    Workflow:
        Builds the generation payload, sends a non-streaming request, validates the HTTP
        response, and extracts the `response` field.
    Connects to:
        Passed to `clean_transcript_with_generator` by `clean_transcript_file`.
    """
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
    """Load basic environment variables from a local `.env` file.

    Purpose:
        Make local cloud-model credentials available without hard-coding secrets.
    Args:
        path: Path to the environment file.
    Returns:
        None.
    Workflow:
        Reads non-comment `KEY=VALUE` lines and sets them in the process environment.
    Connects to:
        Called by `openrouter_generate` before reading `OPENROUTER_API_KEY`.
    """
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def openrouter_generate(prompt: str) -> str:
    """Generate cleaned transcript text with OpenRouter's chat-completions API.

    Purpose:
        Use the configured cloud model as the preferred transcript cleaner.
    Args:
        prompt: Fully constructed transcript-cleaning prompt.
    Returns:
        Non-empty response text from the first model choice.
    Raises:
        CloudCleanerUnavailable: If credentials are missing, the request fails, or the
            service returns no usable text.
    Workflow:
        Loads local environment values, creates an authenticated request, validates the
        response shape, and extracts the first message content.
    Connects to:
        Calls `load_local_env`; passed to the shared cleaner by
        `clean_transcript_file_with_openrouter`.
    """
    load_local_env()
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise CloudCleanerUnavailable("OPENROUTER_API_KEY is not set.")

    model = os.environ.get("OPENROUTER_MODEL", OPENROUTER_MODEL).strip() or OPENROUTER_MODEL
    url = os.environ.get("OPENROUTER_API_URL", OPENROUTER_URL).strip() or OPENROUTER_URL
    max_tokens = int(os.environ.get("OPENROUTER_MAX_TOKENS", OPENROUTER_MAX_TOKENS))
    timeout_seconds = int(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", OPENROUTER_TIMEOUT_SECONDS))

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://lecturescribe.app"),
        "X-Title": os.environ.get("OPENROUTER_APP_NAME", "LectureScribe AI"),
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CloudCleanerUnavailable(f"OpenRouter cleaner failed: {exc}") from exc

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise CloudCleanerUnavailable("OpenRouter returned no choices.")

    message = choices[0].get("message") or {}
    content = message.get("content", "").strip()
    if not content:
        finish_reason = choices[0].get("finish_reason", "unknown")
        usage = data.get("usage") or {}
        completion_tokens = usage.get("completion_tokens", "unknown")
        raise CloudCleanerUnavailable(
            "OpenRouter returned an empty response "
            f"(finish_reason={finish_reason}, completion_tokens={completion_tokens})."
        )

    return content


def groq_generate(prompt: str) -> str:
    """Backward-compatible wrapper for the old Groq function name."""
    return openrouter_generate(prompt)


ProgressCallback = Callable[[str, dict[str, Any]], None]


def clean_transcript_with_generator(
    input_txt: Path,
    generate_fn: Callable[[str], str],
    provider_name: str,
    chunk_chars: int = CHUNK_CHARS,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    """Clean a transcript using a supplied model-generation function.

    Purpose:
        Share file, chunking, progress, deduplication, and output logic across providers.
    Args:
        input_txt: Raw transcript file to clean.
        generate_fn: Callable that accepts a prompt and returns cleaned text.
        provider_name: Human-readable provider name used in logs and progress updates.
        chunk_chars: Preferred maximum chunk size for this provider.
        progress_callback: Optional callback receiving `(stage, details)` updates.
    Returns:
        Path to the cleaned transcript, or None for a missing or empty input file.
    Workflow:
        Reads and pre-deduplicates the transcript, splits it into chunks, invokes the
        provider for each chunk, deduplicates responses, and saves the combined output.
    Connects to:
        Calls prompt/chunk/deduplication helpers and provider functions; wrapped by
        `clean_transcript_file` and `clean_transcript_file_with_openrouter`.
    """
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
        out = normalize_markdown_layout(out)
        if has_arabic_script(out):
            print(f"{provider_name} repair pass for chunk {i}/{len(chunks)} ...")
            if progress_callback:
                progress_callback(
                    "formatting",
                    {
                        "detail": f"{provider_name} translating remaining Arabic in chunk {i} of {len(chunks)}",
                        "chunk_index": i,
                        "chunk_total": len(chunks),
                    },
                )
            out = generate_fn(make_english_repair_prompt(out))
            out = normalize_markdown_layout(out)
        cleaned_chunks.append(normalize_markdown_layout(dedupe_consecutive_units(out)))

    cleaned = "\n\n".join(cleaned_chunks).strip()
    cleaned = normalize_markdown_layout(dedupe_consecutive_units(cleaned))
    if has_arabic_script(cleaned):
        print(f"{provider_name} final English repair pass ...")
        if progress_callback:
            progress_callback(
                "formatting",
                {
                    "detail": f"{provider_name} translating remaining Arabic in final transcript",
                    "chunk_index": len(chunks),
                    "chunk_total": len(chunks),
                },
            )
        cleaned = normalize_markdown_layout(
            dedupe_consecutive_units(generate_fn(make_english_repair_prompt(cleaned)))
        )
    cleaned = normalize_markdown_layout(cleaned)
    output_txt.write_text(cleaned, encoding="utf-8")

    print("\n DONE")
    print(f"Saved cleaned transcript to: {output_txt.resolve()}")

    return output_txt


def clean_transcript_file(
    input_txt: Path,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    """Clean a transcript with the local Ollama provider.

    Purpose:
        Expose a simple Ollama-specific entry point for the pipeline and standalone use.
    Args:
        input_txt: Raw transcript file to clean.
        progress_callback: Optional pipeline progress callback.
    Returns:
        Path to the cleaned file, or None when the input is unavailable or empty.
    Workflow:
        Configures the shared cleaner with `ollama_generate` and Ollama's chunk size.
    Connects to:
        Calls `clean_transcript_with_generator`; used by the preferred-model pipeline
        and this module's command-line entry point.
    """
    return clean_transcript_with_generator(
        input_txt,
        ollama_generate,
        "Ollama",
        CHUNK_CHARS,
        progress_callback,
    )


def clean_transcript_file_with_openrouter(
    input_txt: Path,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    """Clean a transcript with the remote OpenRouter provider.

    Purpose:
        Expose the preferred cloud-cleaning operation to the main pipeline.
    Args:
        input_txt: Raw transcript file to clean.
        progress_callback: Optional pipeline progress callback.
    Returns:
        Path to the cleaned file, or None when the input is unavailable or empty.
    Raises:
        CloudCleanerUnavailable: When OpenRouter cannot return usable cleaned text.
    Workflow:
        Configures the shared cleaner with `openrouter_generate` and the smaller cloud
        chunk size.
    Connects to:
        Calls `clean_transcript_with_generator`; used by
        `clean_transcript_with_preferred_model`.
    """
    return clean_transcript_with_generator(
        input_txt,
        openrouter_generate,
        "OpenRouter",
        CLOUD_CHUNK_CHARS,
        progress_callback,
    )


def clean_transcript_file_with_groq(
    input_txt: Path,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    """Backward-compatible wrapper for the old Groq cleaner function name."""
    return clean_transcript_file_with_openrouter(input_txt, progress_callback)


# if you want to run this file alone to clean a transcript without running the faster-whisper code, you can do that by putting the name of the transcript file in the same directory as this cleaner.py file and then run it. it will produce a cleaned version of the transcript with the name "OutputForOllama_" + input_file_name + "_cleanedv5.txt"
if __name__ == "__main__":
    INPUT_TXT = Path("Qbqc5MoGk5E_IUG Renewable energy Lab 7 _ Broken solar panel part 3_transcript.txt")
    clean_transcript_file(INPUT_TXT)
