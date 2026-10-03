from functools import partial
from pathlib import Path
import os
import re
import time
from typing import Any, Callable

import requests

from transcript_format import (
    ARABIC_SCRIPT_LANGUAGES,
    CODE_SWITCHED_ARABIC_RATIO,
    MIN_DETECTION_LETTERS,
    arabic_letter_count,
    arabic_ratio,
    collapse_loops,
    comparable_words,
    detect_text_language,
    format_transcript,
    language_name,
    letter_count,
    normalize_language,
    repair_mojibake,
    split_sentences,
)

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
# Arabic needs about twice the tokens per character of English on Llama 3.1, so the
# local model gets smaller Arabic chunks to keep the whole answer inside num_predict.
OLLAMA_ARABIC_CHUNK_CHARS = 2000
OPENROUTER_TIMEOUT_SECONDS = 300
OPENROUTER_MAX_TOKENS = 8192
# Reasoning tokens count against max_tokens on gpt-oss; copy-editing needs little.
OPENROUTER_REASONING_EFFORT = "low"
OLLAMA_NUM_PREDICT = 4096
OLLAMA_NUM_CTX = 8192

# HTTP retries for one generation call (seconds slept between attempts).
CLEANER_RETRY_DELAYS = (2, 6)
MAX_RETRY_AFTER_SECONDS = 30
TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# The provider is unusable for every chunk (bad key, no credit, unknown model).
FATAL_HTTP_STATUSES = frozenset({401, 402, 403, 404})
# After this many chunks in a row whose generation failed, stop calling the provider.
MAX_CONSECUTIVE_CHUNK_FAILURES = 3

ARABIC_CHAR_RE = re.compile(r"[؀-ۿ]")


class CleanerUnavailable(RuntimeError):
    """Signal that a cleaning provider cannot be used for this transcript at all.

    Purpose:
        Distinguish "this provider is down or misconfigured" (stop calling it) from a
        single failed chunk (retry once, then format that chunk deterministically).
    Workflow:
        Raised for missing credentials, rejected authentication or payment, an unknown
        model, an unreachable local server, or a provider that produced no usable chunk.
    Connects to:
        Raised by the generators and `clean_transcript_with_generator`; handled by the
        preferred-model pipeline, which then tries the next provider or the formatter.
    """


class CloudCleanerUnavailable(CleanerUnavailable):
    """The remote OpenRouter cleaner cannot be used for this transcript."""


class ChunkCleaningError(RuntimeError):
    """One generation call failed (timeout, server error, truncated or empty answer).

    Args:
        retryable: Whether asking again with a stricter prompt can help (truncated or
            empty answers); exhausted network retries are not retried again.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


# =========================
# PROMPTS
# =========================
def build_system_rules(language: str | None = None) -> str:
    """Build the copy-editor system prompt for one lecture language.

    Purpose:
        Lock the cleaner to the language the lecture was spoken in. The cleaner must
        never translate or transliterate; Arabic stays Arabic and English technical
        terms said in English stay in Latin script.
    Args:
        language: Lecture language code ('ar', 'en', ...) or None when unknown.
    Returns:
        The system prompt sent with every chunk of that lecture.
    Connects to:
        Bound into `openrouter_generate` and `ollama_generate` by the file-level
        cleaner entry points.
    """
    name = language_name(language)
    if name:
        language_rule = (
            f"- The lecture's main language is {name}. Sentences spoken in {name} stay in {name}, "
            f"and headings are written in {name}."
        )
    else:
        language_rule = (
            "- Write every sentence in the language and script it was spoken in, and write headings "
            "in the lecture's main language."
        )
    return f"""You are a copy-editor for ASR lecture transcripts. You are NOT a translator and NOT a summarizer.

Your job is to return the SAME lecture, sentence for sentence, laid out so a student can study from it.

Language rules (most important):
- Keep every sentence in the language it was spoken. Never translate, in either direction, and never transliterate.
- Arabic speech stays Arabic, written in Arabic script, with dialect words kept as spoken.
- Technical terms, code, names, and formulas the lecturer said in English stay in English, in Latin script, inline in the sentence.
{language_rule}

Absolute rules:
- Reproduce the transcript in FULL. Every sentence in the input must survive into the output.
- Never summarize, condense, compress, paraphrase into notes, or "tighten" the wording.
- Never drop recaps, repetitions the lecturer makes on purpose, asides, examples, digressions, or sentences that merely restate an earlier point. The lecturer said them; they stay.
- Restructuring is allowed; deleting content is not. When you turn sentences into a list, each item keeps the lecturer's own words.

The only edits you may make:
- Punctuation and sentence boundaries (use the Arabic marks ، ؛ ؟ in Arabic sentences), and capitalization of Latin-script words.
- Delete pure ASR noise: stutters, immediately repeated words or phrases, and filler sounds such as "آه", "إمم", "uh", "um" ("يعني" only where it is pure filler).
- Repair broken encoding artifacts such as "â€™", "â€“", "â€œ".
- Keep paragraphs short: two to four sentences, separated by a blank line.
- Insert a short "## " heading (two to six words) wherever the lecturer moves to a new topic, and a "### " sub-heading for a distinct sub-topic inside it. A heading is a signpost placed ABOVE the full text, never a replacement for it. Do not start your answer with a heading unless a new topic begins there.
- Whenever the lecturer lists things (types, parts, properties, advantages and disadvantages, examples, rules, differences), put each item on its own "- " line, keeping the sentence that introduces the list above it.
- Whenever the lecturer explains a procedure, algorithm or sequence of steps, write it as "1. " numbered items in order.
- When the lecturer defines a term, write the definition on its own line as "**term**: definition".
- Put the key terms of each paragraph in **bold** the first time they are explained (a few per paragraph at most).
- Keep every number, symbol, equation, name, and technical term exactly as spoken.
- Never guess what a garbled term "really" was. If the recognizer produced
  something odd like "GFS" or "EFAS", leave it exactly as it is. A reader can
  decode a mishearing; a confident wrong symbol silently corrupts the lecture.

Output format: only these Markdown elements are allowed: "## " and "### " headings, paragraphs separated by blank lines, "- " bullets, "1. " numbered items, and **bold**. No tables, code blocks, horizontal rules, or other Markdown.

Do not add information or commentary, do not mention these instructions, and do not add a preamble such as "Here is the transcript". Return only the edited transcript.
"""


# Language-neutral rules, kept for callers that use the module constant directly.
SYSTEM_RULES = build_system_rules(None)


def make_language_hint(language: str | None) -> str:
    """Describe the lecture language at the top of each user prompt."""
    name = language_name(language)
    if name:
        return (
            f"Lecture language: {name} ({normalize_language(language)}). Keep the text in {name}; "
            "words the lecturer said in another language stay exactly as spoken. Do not translate."
        )
    return "Lecture language: not specified. Keep every sentence in the language it is written in. Do not translate."


def make_user_prompt(text: str, language: str | None = None) -> str:
    """Wrap one transcript chunk in the model's user prompt.

    Purpose:
        Give OpenRouter or Ollama a consistent instruction around each transcript chunk.
    Args:
        text: Transcript content to clean.
        language: Lecture language code used as a hint, or None when unknown.
    Returns:
        A complete user prompt containing the language hint and the transcript.
    Connects to:
        Called by `clean_transcript_with_generator` before invoking a model generator.
    """
    word_count = len(text.split())
    return f"""{make_language_hint(language)}

Copy-edit this transcript chunk so it reads well. Do NOT take notes on it.

This chunk contains about {word_count} words. Your output must contain about the
same number of words. Returning far fewer means you summarized, which is a failure.

Keep every sentence the lecturer said, in the same order and the same language,
including recaps, restatements, examples, and asides. Keep prose as prose; do not
convert explanation into bullet points. Only fix punctuation, paragraph breaks, ASR
stutters, filler sounds, and broken encoding such as "â€™" or "â€œ".
Add a "## " heading only where a clearly new topic begins, above the full text.
Keep every number, equation, name, and technical term exactly as spoken. Do not
guess at garbled terms: leave an odd token such as "GFS" or "EFAS" untouched
rather than replacing it with a symbol you think was meant.

Transcript chunk:
{text}
"""


RETRY_CORRECTIONS = {
    "translated": (
        "Your previous answer changed the language of the lecture. That is not allowed. "
        "Keep every sentence in the language and script it is written in below: Arabic stays "
        "Arabic in Arabic script, English stays English. Never translate."
    ),
    "too_short": (
        "Your previous answer dropped part of the lecture. That is not allowed. Every sentence "
        "below must appear in your answer, in the original order."
    ),
    "too_long": (
        "Your previous answer added text that is not in the lecture. Do not add explanations, "
        "examples, or commentary; only copy-edit what is below."
    ),
    "failed": (
        "Your previous answer was cut off or empty. Return the complete edited chunk and nothing else."
    ),
}


def make_retry_prompt(text: str, language: str | None, reason: str | None) -> str:
    """Build the stricter second-attempt prompt for a rejected chunk."""
    correction = RETRY_CORRECTIONS.get(reason or "failed", RETRY_CORRECTIONS["failed"])
    return f"{correction}\n\n{make_user_prompt(text, language)}"


# =========================
# GUARDS
# =========================
# A faithful copy-edit keeps roughly the source word count; under the lower bound the
# model dropped speech, over the upper bound it invented text.
MIN_COVERAGE_RATIO = 0.85
MAX_COVERAGE_RATIO = 1.6
MIN_COVERAGE_WORDS = 10
# Output Arabic share below this fraction of the input share means it was translated.
SCRIPT_KEEP_FACTOR = 0.6

MARKDOWN_SCAFFOLD_RE = re.compile(r"(?m)^\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s+)|[*_`|]+")
HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s")
# Fillers the cleaner may delete; they do not count as source words, so a heavily
# dialectal Arabic chunk is not rejected for losing them.
FILLER_WORDS = frozenset({"آه", "اه", "أه", "إمم", "امم", "يعني", "يعنى", "um", "uh", "uhm", "umm", "erm", "hmm"})
_WORD_EDGE_PUNCTUATION = "\"'.,;:!?()[]{}«»…،؛؟-"


def count_source_words(text: str) -> int:
    """Count source words, ignoring pure filler sounds the cleaner is allowed to delete."""
    return sum(1 for word in text.split() if word.strip(_WORD_EDGE_PUNCTUATION).lower() not in FILLER_WORDS)


def count_body_words(text: str) -> int:
    """Count output words, excluding heading lines and Markdown markers."""
    body = "\n".join(line for line in text.splitlines() if not HEADING_LINE_RE.match(line))
    return len(MARKDOWN_SCAFFOLD_RE.sub(" ", body).split())


def coverage_ratio(source: str, cleaned: str) -> float:
    """Report how much of a source chunk survived into the cleaned text.

    Purpose:
        Detect a summarizing (or inventing) cleaner mechanically instead of trusting
        the prompt.
    Returns:
        Cleaned body words divided by source words; 1.0 when the source is empty.
    Workflow:
        Headings and Markdown markers do not count, so invented headings cannot hide
        dropped speech; filler sounds do not count on the source side.
    """
    source_words = count_source_words(source)
    if not source_words:
        return 1.0
    return count_body_words(cleaned) / source_words


def validate_chunk(source: str, output: str, language: str | None = None) -> str | None:
    """Check one cleaned chunk and return why it is unusable, or None when it is fine.

    Args:
        source: The chunk sent to the model.
        output: The model's cleaned chunk.
        language: Lecture language code, when known.
    Returns:
        'failed' for an empty answer, 'translated' when the script changed (Arabic
        source answered mostly in Latin script, or the reverse), 'too_short' or
        'too_long' when the word count left the 0.85-1.6 coverage band.
    Workflow:
        The Arabic guard covers code-switched chunks too: an Arabic programming
        lecture read out with code can have only 10-30% Arabic letters, and its
        English rendering must still be rejected. For an Arabic-script lecture the
        Arabic letters themselves must also survive.
    """
    if not output.strip():
        return "failed"
    if letter_count(source) >= MIN_DETECTION_LETTERS:
        source_ratio = arabic_ratio(source)
        output_ratio = arabic_ratio(output)
        source_arabic = arabic_letter_count(source)
        if source_arabic >= MIN_DETECTION_LETTERS:
            if source_ratio >= CODE_SWITCHED_ARABIC_RATIO and output_ratio < SCRIPT_KEEP_FACTOR * source_ratio:
                return "translated"
            if (normalize_language(language) in ARABIC_SCRIPT_LANGUAGES
                    and arabic_letter_count(output) < SCRIPT_KEEP_FACTOR * source_arabic):
                return "translated"
        if source_ratio < 0.15 and output_ratio >= source_ratio + 0.3:
            return "translated"
    if count_source_words(source) >= MIN_COVERAGE_WORDS:
        ratio = coverage_ratio(source, output)
        if ratio < MIN_COVERAGE_RATIO:
            return "too_short"
        if ratio > MAX_COVERAGE_RATIO:
            return "too_long"
    return None


_CODE_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)
_PREAMBLE_RES = (
    re.compile(
        r"^\s*(?:\*\*)?(?:here(?:'s| is| are)|below is|below are)\b[^\n]{0,80}?"
        r"\b(?:transcript|text|chunk|version)\b[^\n]{0,40}?:(?:\*\*)?(?:\s*\n|\s+|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:\*\*)?(?:cleaned|edited|corrected|copy-edited|formatted|revised)\s+"
        r"(?:transcript|text|chunk)(?:\*\*)?\s*:?\s*(?:\*\*)?\s*\n",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:\*\*)?(?:إليك|اليك|إليكم|اليكم|فيما يلي|هذا هو|هذه هي)\b[^\n]{0,80}?"
        # Whole words only: "النصيحة", "النصف" and "النصوص" are lecture words.
        r"(?:النص|التفريغ|النسخة)(?![ء-ي])[^\n]{0,40}?:(?:\*\*)?(?:\s*\n|\s+|$)"
    ),
)


def _spoken_in_source(preamble: str, source: str | None) -> bool:
    """Whether a would-be preamble is lecture speech, i.e. its words are in the source chunk."""
    if not source:
        return False
    filler = set(comparable_words(" ".join(FILLER_WORDS)))
    words = [word for word in comparable_words(preamble) if word not in filler]
    if not words:
        return False
    spoken = [word for word in comparable_words(source) if word not in filler]
    return f" {' '.join(words)} " in f" {' '.join(spoken)} "


def strip_model_wrapping(text: str, source: str | None = None) -> str:
    """Remove a code fence or a preamble line such as "Here is the cleaned transcript:".

    Args:
        text: The model's answer.
        source: The chunk the model was given. A matching opening that is also in
            the source ("Here is the second version of the algorithm: ...", "هذا هو
            النص الكامل للمسألة: ...") is the lecturer speaking, so it is kept.
    Returns:
        The answer without its wrapping. Only lines that talk about the transcript
        itself are removed, so lecture speech that happens to end with a colon is kept.
    """
    text = (text or "").strip()
    fenced = _CODE_FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    for pattern in _PREAMBLE_RES:
        match = pattern.match(text)
        if match:
            if not _spoken_in_source(match.group(0), source):
                text = text[match.end():].lstrip()
            break
    return text.strip()


def has_arabic_script(text: str) -> bool:
    """Return whether text contains Arabic-script characters."""
    return bool(ARABIC_CHAR_RE.search(text))


# =========================
# CHUNKING AND LAYOUT
# =========================
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


def _pack(units: list[str], max_chars: int, separator: str) -> list[str]:
    """Greedily join units with `separator` into pieces of at most `max_chars`."""
    pieces = []
    buf = ""
    for unit in units:
        if len(unit) > max_chars:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.extend(split_long_text(unit, max_chars))
            continue
        if not buf:
            buf = unit
        elif len(buf) + len(separator) + len(unit) <= max_chars:
            buf = f"{buf}{separator}{unit}"
        else:
            pieces.append(buf)
            buf = unit
    if buf:
        pieces.append(buf)
    return pieces


def split_text(text: str, max_chars: int):
    """Build size-limited chunks made of whole paragraphs.

    Purpose:
        Prepare transcript requests that fit model context and output limits without
        cutting through sentences or paragraphs.
    Args:
        text: Complete transcript text, paragraphs separated by blank lines.
        max_chars: Preferred maximum characters per generated chunk.
    Returns:
        Ordered transcript chunks; paragraphs inside a chunk keep their blank lines.
    Workflow:
        Packs whole paragraphs; a paragraph larger than one chunk is split into
        sentences (Arabic-aware), and a sentence larger than one chunk by words.
    Connects to:
        Calls `split_sentences` and `split_long_text`; used by the shared cleaner.
    """
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", (text or "").strip()) if part.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if buf:
                chunks.append("\n\n".join(buf))
                buf, size = [], 0
            chunks.extend(_pack(split_sentences(paragraph) or [paragraph], max_chars, " "))
            continue
        if buf and size + 2 + len(paragraph) > max_chars:
            chunks.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(paragraph)
        size += len(paragraph) + 2
    if buf:
        chunks.append("\n\n".join(buf))
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
    text = re.sub(r"[^\w؀-ۿ]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _dedupe_key(unit: str) -> str:
    """Comparison key for a line or sentence; empty when the unit must never be dropped.

    Short units ("No. No.") and units without letters (numbers, symbols) are never
    treated as duplicates, so emphasis and numeric content survive.
    """
    normalized = normalize_for_compare(unit)
    if len(normalized.split()) < 3 or not re.search(r"[^\W\d_]", normalized):
        return ""
    return normalized


def collapse_repeated_word_blocks(text: str, max_block_words: int = 30) -> str:
    """Backward-compatible name for `transcript_format.collapse_loops`."""
    return collapse_loops(text, max_block_words)


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
        sentence level. Units shorter than three words or without letters are kept.
    Connects to:
        Calls `normalize_for_compare`; used on the combined cleaner output.
    """
    lines = [line.strip() for line in text.splitlines()]
    cleaned_lines = []
    previous = ""

    for line in lines:
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue

        key = _dedupe_key(line)
        if key and key == previous:
            continue

        cleaned_lines.append(line)
        previous = key

    deduped_lines = []
    for line in cleaned_lines:
        if not line:
            deduped_lines.append("")
            continue

        parts = re.split(r"(?<=[\.\!\?؟])\s+", line)
        merged = []
        previous = ""
        for part in parts:
            part = part.strip()
            if not part:
                continue
            key = _dedupe_key(part)
            if key and key == previous:
                continue
            merged.append(part)
            previous = key
        deduped_lines.append(" ".join(merged))

    text = "\n".join(deduped_lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_HEADING_RE = re.compile(r"^#{1,3}\s")
_RULE_LINE_RE = re.compile(r"(?:-{3,}|\*{3,}|_{3,})")
_INLINE_LIST_START_RE = re.compile(r"[:：][ \t]+(?=-[ \t]+(?:\*\*)?[^\W\d_])")
_INLINE_ITEM_RE = re.compile(r"[ \t]+-[ \t]+(?=(?:\*\*)?[^\W\d_])")


def _split_inline_list(line: str, next_line: str) -> str:
    """Put a list glued after a colon on its own lines ("Items: - arrays - trees").

    Only a real list is split: two or more dash items after the colon, or one item
    followed by a "- " line. A dash after other punctuation is prose, such as a
    Whisper self-interruption ("لماذا؟ - لأن الذاكرة محدودة"), and a lone dash after a
    colon may be a minus sign ("النتيجة هي: - س تربيع"); both stay as written.
    """
    match = _INLINE_LIST_START_RE.search(line)
    if not match:
        return line
    items = [item.strip() for item in _INLINE_ITEM_RE.split(" " + line[match.end():])]
    items = [item for item in items if item]
    if len(items) < 2 and not next_line.lstrip().startswith("- "):
        return line
    return line[:match.start() + 1] + "\n" + "\n".join(f"- {item}" for item in items)


def normalize_markdown_layout(text: str) -> str:
    """Clean Markdown layout after model generation, without touching the math.

    Purpose:
        Keep headings and bullets on their own lines in any script while leaving
        expressions such as "(a + b) - c", "|x|" and "3. 2." exactly as written.
    Args:
        text: Generated transcript text.
    Returns:
        Text in the Markdown subset (headings, paragraphs, "- " bullets, numbered
        items, bold) with repaired encoding and tidy blank lines.
    Workflow:
        Repairs mojibake; moves a "## " heading glued to the previous sentence onto
        its own paragraph; splits a dash list glued after a colon onto its own lines
        (`_split_inline_list`; prose dashes and minus signs stay); maps "* " bullets
        to "- ", deep headings to "### ", drops horizontal rules, and keeps blank
        lines around headings.
    Connects to:
        Called after each model response and on the final combined transcript.
    """
    text = repair_mojibake(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<=\S)[ \t]+(#{2,3}[ \t]+)(?=\S)", r"\n\n\1", text)
    lines = text.split("\n")
    text = "\n".join(
        _split_inline_list(line, lines[index + 1] if index + 1 < len(lines) else "")
        for index, line in enumerate(lines)
    )

    cleaned: list[str] = []
    previous_heading = False
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line or _RULE_LINE_RE.fullmatch(line):
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            previous_heading = False
            continue
        line = re.sub(r"^[*+•]\s+", "- ", line)
        line = re.sub(r"^#{4,6}\s+", "### ", line)
        heading = bool(_HEADING_RE.match(line))
        if (heading or previous_heading) and cleaned and cleaned[-1] != "":
            cleaned.append("")
        cleaned.append(line)
        previous_heading = heading

    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


# =========================
# PROVIDERS
# =========================
def _env_int(name: str, default: int) -> int:
    """Read a positive integer setting, keeping the default for missing or bad values."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def ollama_generate(prompt: str, system: str | None = None) -> str:
    """Generate cleaned transcript text with the local Ollama server.

    Purpose:
        Provide the offline cleaner used when OpenRouter is unavailable.
    Args:
        prompt: Fully constructed transcript-cleaning prompt.
        system: System prompt for the lecture language (language-neutral by default).
    Returns:
        Ollama's stripped response text.
    Raises:
        CleanerUnavailable: When the server is unreachable or the model is missing.
        ChunkCleaningError: When this generation failed, was truncated, or was empty.
    Workflow:
        Sends a non-streaming request with an output budget large enough for Arabic
        chunks (`OLLAMA_NUM_PREDICT`) and an explicit context size (`OLLAMA_NUM_CTX`).
    Connects to:
        Bound to a language and passed to `clean_transcript_with_generator` by
        `clean_transcript_file`.
    """
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "system": system or SYSTEM_RULES,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "top_p": 0.9,
            "num_predict": _env_int("OLLAMA_NUM_PREDICT", OLLAMA_NUM_PREDICT),
            "num_ctx": _env_int("OLLAMA_NUM_CTX", OLLAMA_NUM_CTX),
        },
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=600)
    except requests.ConnectionError as exc:
        raise CleanerUnavailable(f"Ollama is not reachable ({type(exc).__name__}).") from exc
    except requests.RequestException as exc:
        raise ChunkCleaningError(f"Ollama request failed ({type(exc).__name__}).", retryable=False) from exc
    if response.status_code == 404:
        raise CleanerUnavailable(f"Ollama model {MODEL} is not available (HTTP 404).")
    if not response.ok:
        raise ChunkCleaningError(f"Ollama returned HTTP {response.status_code}.", retryable=False)
    try:
        data = response.json()
    except ValueError as exc:
        raise ChunkCleaningError("Ollama response was not JSON.") from exc
    if not isinstance(data, dict):
        raise ChunkCleaningError("Ollama response has an invalid shape.")
    if data.get("done_reason") == "length":
        raise ChunkCleaningError("Ollama output was truncated (done_reason=length).")
    content = (data.get("response") or "").strip()
    if not content:
        raise ChunkCleaningError("Ollama returned an empty response.")
    return content


def load_local_env(path: Path = Path(".env")) -> None:
    """Load basic environment variables from a local `.env` file.

    Purpose:
        Make local cloud-model credentials available without hard-coding secrets.
    Args:
        path: Path to the environment file.
    Returns:
        None.
    Workflow:
        Reads non-comment `KEY=VALUE` lines, preserving existing server environment values.
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
            os.environ.setdefault(key, value)


def _retry_after_seconds(response: Any) -> float | None:
    """Read a numeric Retry-After header, capped so one chunk cannot stall the job."""
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


_reasoning_rejected = False


def _post_openrouter(url: str, payload: dict, headers: dict, timeout_seconds: int) -> Any:
    """POST one chat completion, retrying transient failures with backoff.

    Raises:
        CloudCleanerUnavailable: For HTTP 401/402/403/404 (no chunk can succeed).
        ChunkCleaningError: When the transient retries are exhausted or the request
            is rejected for this prompt.
    """
    global _reasoning_rejected
    attempts = len(CLEANER_RETRY_DELAYS) + 1
    last_error = "OpenRouter cleaner request was not sent."
    attempt = 0
    while attempt < attempts:
        attempt += 1
        delay = None
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
        except requests.RequestException as exc:
            last_error = f"OpenRouter cleaner request failed ({type(exc).__name__})."
        else:
            status = response.status_code
            if status in FATAL_HTTP_STATUSES:
                raise CloudCleanerUnavailable(f"OpenRouter cleaner returned HTTP {status}.")
            if status == 400 and "reasoning" in payload:
                # Some routed models reject the reasoning option; ask once without it.
                _reasoning_rejected = True
                payload = {key: value for key, value in payload.items() if key != "reasoning"}
                attempt -= 1
                continue
            if status in TRANSIENT_HTTP_STATUSES:
                last_error = f"OpenRouter cleaner returned HTTP {status}."
                delay = _retry_after_seconds(response)
            elif not response.ok:
                raise ChunkCleaningError(f"OpenRouter cleaner returned HTTP {status}.", retryable=False)
            else:
                try:
                    return response.json()
                except ValueError:
                    last_error = "OpenRouter cleaner response was not JSON."
        if attempt < attempts:
            time.sleep(delay if delay is not None else CLEANER_RETRY_DELAYS[attempt - 1])
    raise ChunkCleaningError(last_error, retryable=False)


def openrouter_generate(prompt: str, system: str | None = None) -> str:
    """Generate cleaned transcript text with OpenRouter's chat-completions API.

    Purpose:
        Use the configured cloud model as the preferred transcript cleaner.
    Args:
        prompt: Fully constructed transcript-cleaning prompt.
        system: System prompt for the lecture language (language-neutral by default).
    Returns:
        Non-empty response text from the first model choice.
    Raises:
        CloudCleanerUnavailable: Missing credentials, or a rejection that affects
            every chunk (HTTP 401/402/403/404).
        ChunkCleaningError: Transient failures after retries, truncated output
            (finish_reason=length), or empty/null content.
    Workflow:
        Loads local environment values, parses numeric settings defensively, sends the
        request with retries, and validates the response shape and finish reason.
    Connects to:
        Calls `load_local_env`; bound to a language and passed to the shared cleaner
        by `clean_transcript_file_with_openrouter`.
    """
    load_local_env()
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise CloudCleanerUnavailable("OPENROUTER_API_KEY is not set.")

    model = os.environ.get("OPENROUTER_MODEL", OPENROUTER_MODEL).strip() or OPENROUTER_MODEL
    url = os.environ.get("OPENROUTER_API_URL", OPENROUTER_URL).strip() or OPENROUTER_URL
    max_tokens = _env_int("OPENROUTER_MAX_TOKENS", OPENROUTER_MAX_TOKENS)
    timeout_seconds = _env_int("OPENROUTER_TIMEOUT_SECONDS", OPENROUTER_TIMEOUT_SECONDS)

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system or SYSTEM_RULES},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": max_tokens,
    }
    effort = os.environ.get("OPENROUTER_REASONING_EFFORT", OPENROUTER_REASONING_EFFORT).strip().lower()
    if effort and effort not in {"none", "off", "false", "0", "default"} and not _reasoning_rejected:
        payload["reasoning"] = {"effort": effort, "exclude": True}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://lecturescribe.app"),
        "X-Title": os.environ.get("OPENROUTER_APP_NAME", "LectureScribe AI"),
    }

    data = _post_openrouter(url, payload, headers, timeout_seconds)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices[0], dict):
        raise ChunkCleaningError("OpenRouter returned no choices.")

    choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = (message.get("content") or "").strip()
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise ChunkCleaningError("OpenRouter output was truncated (finish_reason=length).")
    if not content:
        usage = data.get("usage") or {}
        completion_tokens = usage.get("completion_tokens", "unknown") if isinstance(usage, dict) else "unknown"
        raise ChunkCleaningError(
            "OpenRouter returned an empty response "
            f"(finish_reason={finish_reason or 'unknown'}, completion_tokens={completion_tokens})."
        )

    return content


def groq_generate(prompt: str) -> str:
    """Backward-compatible wrapper for the old Groq function name."""
    return openrouter_generate(prompt)


ProgressCallback = Callable[[str, dict[str, Any]], None]


# =========================
# CLEANING
# =========================
def _clean_chunk(
    generate_fn: Callable[[str], str],
    chunk: str,
    language: str | None,
) -> tuple[str | None, list[str]]:
    """Clean one chunk: first attempt, then one stricter retry when it is rejected.

    Returns:
        `(cleaned_or_None, reasons)` where `reasons` lists why each rejected attempt
        failed ('failed', 'translated', 'too_short', 'too_long').
    Raises:
        CleanerUnavailable: Propagated so the caller can stop using the provider.
    """
    reasons: list[str] = []
    for attempt in (1, 2):
        prompt = make_user_prompt(chunk, language) if attempt == 1 else make_retry_prompt(chunk, language, reasons[-1])
        try:
            output = generate_fn(prompt)
        except CleanerUnavailable:
            raise
        except ChunkCleaningError as exc:
            print(f"Chunk generation failed: {exc}")
            reasons.append("failed")
            if not exc.retryable:
                break
            continue
        except Exception as exc:  # An unexpected provider error must cost one chunk, not the lecture.
            print(f"Chunk generation failed: {type(exc).__name__}: {exc}")
            reasons.append("failed")
            continue
        output = normalize_markdown_layout(strip_model_wrapping(output if isinstance(output, str) else "", chunk))
        reason = validate_chunk(chunk, output, language)
        if reason is None:
            return output, reasons
        print(f"Chunk rejected: {reason}")
        reasons.append(reason)
    return None, reasons


def clean_transcript_with_generator(
    input_txt: Path,
    generate_fn: Callable[[str], str],
    provider_name: str,
    chunk_chars: int = CHUNK_CHARS,
    progress_callback: ProgressCallback | None = None,
    *,
    language: str | None = None,
    segments: list[dict[str, Any]] | None = None,
    output_path: Path | None = None,
    stats: dict[str, Any] | None = None,
    max_consecutive_failures: int = MAX_CONSECUTIVE_CHUNK_FAILURES,
) -> Path | None:
    """Clean a transcript in its own language using a supplied model-generation function.

    Purpose:
        Share file, chunking, guards, fallback, progress, and output logic across
        providers. One bad chunk must never discard the lecture.
    Args:
        input_txt: Raw transcript file to clean.
        generate_fn: Callable that accepts a prompt and returns cleaned text.
        provider_name: Human-readable provider name used in logs and progress updates.
        chunk_chars: Preferred maximum chunk size for this provider.
        progress_callback: Optional callback receiving `(stage, details)` updates.
        language: Lecture language code; detected from the script when None.
        segments: Optional Whisper segments aligned with the raw text.
        output_path: Where to write the result (defaults to OutputForOllama/).
        stats: Optional dictionary filled with per-chunk counters.
        max_consecutive_failures: Chunks in a row whose generation may fail before the
            provider is no longer called for this transcript.
    Returns:
        Path to the cleaned transcript, or None for a missing or empty input file.
    Raises:
        CleanerUnavailable: When the provider produced no usable chunk at all, so the
            caller can try another provider or the deterministic formatter.
    Workflow:
        Lays the raw text out with `format_transcript` (hallucinations and loops
        removed, paragraphs), packs whole paragraphs into chunks, and cleans each with
        a language-locked prompt. A chunk whose answer is empty, truncated,
        translated, or outside the coverage band is retried once with a stricter
        prompt and otherwise kept in its deterministic layout. Nothing is ever
        translated and no call ever covers the whole transcript.
    Connects to:
        Wrapped by `clean_transcript_file` and `clean_transcript_file_with_openrouter`.
    """
    if not input_txt.exists():
        print(f"Input file not found: {input_txt.resolve()}")
        return None

    raw = input_txt.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        print("Input file is empty.")
        return None

    code = normalize_language(language) or detect_text_language(raw)
    prepared = format_transcript(raw, segments, code)
    if not prepared:
        print("Transcript contains no speech after removing hallucinations.")
        return None

    output_txt = Path(output_path) if output_path else Path("OutputForOllama") / f"{input_txt.stem}_cleanedv5.txt"
    output_txt.parent.mkdir(parents=True, exist_ok=True)

    chunks = split_text(prepared, chunk_chars)
    total = len(chunks)
    print(f"Loaded text. Chunks: {total}")
    if progress_callback:
        progress_callback(
            "formatting",
            {
                "detail": f"{provider_name} is preparing transcript chunks",
                "chunk_index": 0,
                "chunk_total": total,
            },
        )

    cleaned_chunks: list[str] = []
    llm_chunks = 0
    fallback_chunks = 0
    retried_chunks = 0
    rejections: dict[str, int] = {}
    consecutive_failures = 0
    stopped_reason: str | None = None

    for index, chunk in enumerate(chunks, start=1):
        if progress_callback:
            progress_callback(
                "formatting",
                {
                    "detail": f"{provider_name} cleaning chunk {index} of {total}",
                    "chunk_index": index,
                    "chunk_total": total,
                },
            )
        if stopped_reason:
            cleaned_chunks.append(chunk)
            fallback_chunks += 1
            continue

        print(f"{provider_name} cleaning chunk {index}/{total} ...")
        try:
            output, reasons = _clean_chunk(generate_fn, chunk, code)
        except CleanerUnavailable as exc:
            if not llm_chunks:
                raise
            stopped_reason = str(exc)
            print(f"{provider_name} stopped: {stopped_reason}. Remaining chunks keep their plain layout.")
            cleaned_chunks.append(chunk)
            fallback_chunks += 1
            continue

        for reason in reasons:
            rejections[reason] = rejections.get(reason, 0) + 1
        if reasons:
            retried_chunks += 1

        if output is not None:
            cleaned_chunks.append(output)
            llm_chunks += 1
            consecutive_failures = 0
            continue

        print(f"{provider_name} chunk {index}/{total} kept in its plain layout ({', '.join(reasons)}).")
        cleaned_chunks.append(chunk)
        fallback_chunks += 1
        consecutive_failures = consecutive_failures + 1 if reasons and reasons[-1] == "failed" else 0
        if consecutive_failures >= max_consecutive_failures:
            message = f"{provider_name} failed on {consecutive_failures} chunks in a row"
            if not llm_chunks:
                raise CleanerUnavailable(message)
            stopped_reason = message

    if not llm_chunks:
        raise CleanerUnavailable(
            f"{provider_name} produced no usable chunk ({', '.join(sorted(rejections)) or 'no output'})."
        )

    cleaned = normalize_markdown_layout(dedupe_consecutive_units("\n\n".join(cleaned_chunks)))
    output_txt.write_text(cleaned, encoding="utf-8")

    if stats is not None:
        stats.update(
            {
                "provider": provider_name,
                "language": code,
                "chunks": total,
                "llm_chunks": llm_chunks,
                "fallback_chunks": fallback_chunks,
                "retried_chunks": retried_chunks,
                "rejections": rejections,
                "stopped_reason": stopped_reason,
            }
        )

    print("\n DONE")
    print(f"Saved cleaned transcript to: {output_txt.resolve()}")

    return output_txt


def _cleaning_language(input_txt: Path, language: str | None) -> str | None:
    """Use the requested language, or detect it from the raw transcript's script."""
    code = normalize_language(language)
    if code or not input_txt.exists():
        return code
    return detect_text_language(input_txt.read_text(encoding="utf-8", errors="replace"))


def clean_transcript_file(
    input_txt: Path,
    progress_callback: ProgressCallback | None = None,
    *,
    language: str | None = None,
    segments: list[dict[str, Any]] | None = None,
    output_path: Path | None = None,
    stats: dict[str, Any] | None = None,
) -> Path | None:
    """Clean a transcript with the local Ollama provider.

    Purpose:
        Expose a simple Ollama-specific entry point for the pipeline and standalone use.
    Args:
        input_txt: Raw transcript file to clean.
        progress_callback: Optional pipeline progress callback.
        language: Lecture language code; detected when None.
        segments: Optional Whisper segments aligned with the raw text.
        output_path: Destination file (defaults to OutputForOllama/).
        stats: Optional dictionary filled with per-chunk counters.
    Returns:
        Path to the cleaned file, or None when the input is unavailable or empty.
    Workflow:
        Binds the language-locked system prompt to `ollama_generate` and uses smaller
        chunks for Arabic.
    Connects to:
        Calls `clean_transcript_with_generator`; used by the preferred-model pipeline
        and this module's command-line entry point.
    """
    code = _cleaning_language(input_txt, language)
    chunk_chars = OLLAMA_ARABIC_CHUNK_CHARS if code in ARABIC_SCRIPT_LANGUAGES else CHUNK_CHARS
    return clean_transcript_with_generator(
        input_txt,
        partial(ollama_generate, system=build_system_rules(code)),
        "Ollama",
        chunk_chars,
        progress_callback,
        language=code,
        segments=segments,
        output_path=output_path,
        stats=stats,
    )


def clean_transcript_file_with_openrouter(
    input_txt: Path,
    progress_callback: ProgressCallback | None = None,
    *,
    language: str | None = None,
    segments: list[dict[str, Any]] | None = None,
    output_path: Path | None = None,
    stats: dict[str, Any] | None = None,
) -> Path | None:
    """Clean a transcript with the remote OpenRouter provider.

    Purpose:
        Expose the preferred cloud-cleaning operation to the main pipeline.
    Args:
        input_txt: Raw transcript file to clean.
        progress_callback: Optional pipeline progress callback.
        language: Lecture language code; detected when None.
        segments: Optional Whisper segments aligned with the raw text.
        output_path: Destination file (defaults to OutputForOllama/).
        stats: Optional dictionary filled with per-chunk counters.
    Returns:
        Path to the cleaned file, or None when the input is unavailable or empty.
    Raises:
        CleanerUnavailable: When OpenRouter cannot return any usable cleaned chunk.
    Workflow:
        Binds the language-locked system prompt to `openrouter_generate` and uses the
        cloud chunk size.
    Connects to:
        Calls `clean_transcript_with_generator`; used by
        `clean_transcript_with_preferred_model`.
    """
    code = _cleaning_language(input_txt, language)
    return clean_transcript_with_generator(
        input_txt,
        partial(openrouter_generate, system=build_system_rules(code)),
        "OpenRouter",
        CLOUD_CHUNK_CHARS,
        progress_callback,
        language=code,
        segments=segments,
        output_path=output_path,
        stats=stats,
    )


def clean_transcript_file_with_groq(
    input_txt: Path,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    """Backward-compatible wrapper for the old Groq cleaner function name."""
    return clean_transcript_file_with_openrouter(input_txt, progress_callback)


# Run this file alone to clean a transcript without the Whisper step: put the raw
# transcript next to this file and set INPUT_TXT. The result is written to
# OutputForOllama/<input stem>_cleanedv5.txt in the transcript's own language.
if __name__ == "__main__":
    INPUT_TXT = Path("Qbqc5MoGk5E_IUG Renewable energy Lab 7 _ Broken solar panel part 3_transcript.txt")
    clean_transcript_file(INPUT_TXT)
