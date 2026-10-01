"""Deterministic transcript language and layout helpers.

Pure functions with no third-party imports. They are shared by the transcription
pipeline (raw paragraphing and language detection), the LLM cleaner (chunking,
translation guard and per-chunk fallback) and the fast mode, which formats the
Whisper text without any model.

Every transformation keeps the lecture in the language it was spoken. The raw view
(`paragraphize`) changes whitespace only; the fast view (`format_transcript`) also
removes known Whisper hallucinations and loops, but never numbers or real content.
"""

import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


# Bump whenever cleaned output would differ for the same raw transcript, so caches
# on both LectureScribe and EduFusion stop reusing older results.
FORMAT_VERSION = "source-language-v2"

ARABIC_LETTER_RE = re.compile(
    "[ء-غف-يٮٯٱ-ۓەۮۯ"
    "ۺ-ۼۿݐ-ݿࢠ-ࣉﭐ-ﷻﹰ-ﻼ]"
)
LATIN_LETTER_RE = re.compile("[A-Za-zÀ-ÖØ-öø-ɏ]")
ARABIC_RATIO_THRESHOLD = 0.3
MIN_DETECTION_LETTERS = 20
# A transcript whose Arabic letters are at least this share is code-switched Arabic
# (an Arabic lecture read out with code or English terms), not English text.
CODE_SWITCHED_ARABIC_RATIO = 0.1
ARABIC_SCRIPT_LANGUAGES = frozenset({"ar", "fa", "ur", "ps", "ku", "sd", "ug"})

# Every language Whisper can report (its tokenizer table), so a name such as
# "polish" maps to a code instead of being mistaken for "unknown".
LANGUAGE_NAMES = {
    "ar": "Arabic",
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "tr": "Turkish",
    "fa": "Persian",
    "ur": "Urdu",
    "he": "Hebrew",
    "hi": "Hindi",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "id": "Indonesian",
    "ms": "Malay",
    "pl": "Polish",
    "ca": "Catalan",
    "sv": "Swedish",
    "fi": "Finnish",
    "vi": "Vietnamese",
    "uk": "Ukrainian",
    "el": "Greek",
    "cs": "Czech",
    "ro": "Romanian",
    "da": "Danish",
    "hu": "Hungarian",
    "ta": "Tamil",
    "no": "Norwegian",
    "th": "Thai",
    "hr": "Croatian",
    "bg": "Bulgarian",
    "lt": "Lithuanian",
    "la": "Latin",
    "mi": "Maori",
    "ml": "Malayalam",
    "cy": "Welsh",
    "sk": "Slovak",
    "te": "Telugu",
    "lv": "Latvian",
    "bn": "Bengali",
    "sr": "Serbian",
    "az": "Azerbaijani",
    "sl": "Slovenian",
    "kn": "Kannada",
    "et": "Estonian",
    "mk": "Macedonian",
    "br": "Breton",
    "eu": "Basque",
    "is": "Icelandic",
    "hy": "Armenian",
    "ne": "Nepali",
    "mn": "Mongolian",
    "bs": "Bosnian",
    "kk": "Kazakh",
    "sq": "Albanian",
    "sw": "Swahili",
    "gl": "Galician",
    "mr": "Marathi",
    "pa": "Punjabi",
    "si": "Sinhala",
    "km": "Khmer",
    "sn": "Shona",
    "yo": "Yoruba",
    "so": "Somali",
    "af": "Afrikaans",
    "oc": "Occitan",
    "ka": "Georgian",
    "be": "Belarusian",
    "tg": "Tajik",
    "sd": "Sindhi",
    "gu": "Gujarati",
    "am": "Amharic",
    "yi": "Yiddish",
    "lo": "Lao",
    "uz": "Uzbek",
    "fo": "Faroese",
    "ht": "Haitian Creole",
    "ps": "Pashto",
    "tk": "Turkmen",
    "nn": "Nynorsk",
    "mt": "Maltese",
    "sa": "Sanskrit",
    "lb": "Luxembourgish",
    "my": "Myanmar",
    "bo": "Tibetan",
    "tl": "Tagalog",
    "mg": "Malagasy",
    "as": "Assamese",
    "tt": "Tatar",
    "ln": "Lingala",
    "ha": "Hausa",
    "ba": "Bashkir",
    "jw": "Javanese",
    "su": "Sundanese",
}
# Whisper reports full language names ("arabic"); some tools use ISO-639-2 codes.
LANGUAGE_ALIASES = {
    **{name.lower(): code for code, name in LANGUAGE_NAMES.items()},
    # Other names Whisper accepts for the same languages.
    "burmese": "my",
    "valencian": "ca",
    "flemish": "nl",
    "haitian": "ht",
    "letzeburgesch": "lb",
    "pushto": "ps",
    "panjabi": "pa",
    "moldavian": "ro",
    "moldovan": "ro",
    "sinhalese": "si",
    "castilian": "es",
    "mandarin": "zh",
    "farsi": "fa",
    "ara": "ar",
    "eng": "en",
    "fra": "fr",
    "fre": "fr",
    "deu": "de",
    "ger": "de",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "nld": "nl",
    "dut": "nl",
    "tur": "tr",
    "fas": "fa",
    "per": "fa",
    "urd": "ur",
    "heb": "he",
    "hin": "hi",
    "rus": "ru",
    "zho": "zh",
    "chi": "zh",
    "jpn": "ja",
    "kor": "ko",
    "ind": "id",
    "msa": "ms",
    "may": "ms",
}
AUTO_LANGUAGE_VALUES = frozenset({"", "auto", "detect", "automatic", "none", "null"})

# Layout targets for the deterministic formatter.
SENTENCE_MAX_WORDS = 40
SENTENCE_SOFT_WORDS = 20
SENTENCE_MIN_PIECE_WORDS = 8
SENTENCE_PAUSE_SECONDS = 0.8
PARAGRAPH_TARGET_WORDS = 90
PARAGRAPH_MAX_WORDS = 140
PARAGRAPH_MIN_SENTENCES = 3
PARAGRAPH_MAX_SENTENCES = 6
PARAGRAPH_PAUSE_SECONDS = 1.5
PARAGRAPH_PAUSE_MIN_WORDS = 40
LOOP_MAX_BLOCK_WORDS = 30
LOOP_MIN_REPEATS = 3

_TRAILING_CLOSERS = "\"'”’»)]"
_SENTENCE_END_RE = re.compile("[.!?؟…]+[" + re.escape(_TRAILING_CLOSERS) + "]*$")
_STRONG_CLAUSE_RE = re.compile("[،؛;]+[" + re.escape(_TRAILING_CLOSERS) + "]*$")
_WEAK_CLAUSE_RE = re.compile("[,:]+[" + re.escape(_TRAILING_CLOSERS) + "]*$")
_ABBREVIATIONS = frozenset(
    {"dr", "mr", "mrs", "ms", "prof", "eg", "ie", "etc", "vs", "fig", "eq", "jr", "sr", "approx", "dept"}
)
# A paragraph must not begin with something the Markdown renderer would read as a
# heading, bullet or numbered item.
_MARKDOWN_START_RE = re.compile(r"#{1,6}|[-*+•]|\d{1,3}[.)]")

_INVISIBLE_RE = re.compile("[­​‎‏‪-‮⁦-⁩﻿]")
_MOJIBAKE_REPLACEMENTS = (
    ("â€™", "'"),
    ("â€˜", "'"),
    ("â€œ", '"'),
    ("â€\u009d", '"'),
    ("â€“", "-"),
    ("â€”", "-"),
    ("â€‘", "-"),
    ("â€¯", " "),
    ("â€¢", "-"),
    ("â€¦", "..."),
    ("â†\u0090", "<-"),
    ("â†’", "->"),
    ("Â ", " "),
    ("Â ", " "),
)
# A stray "Â" is only mojibake when it precedes a Latin-1 symbol ("Â°", "Â©").
_STRAY_A_CIRCUMFLEX_RE = re.compile("Â(?=[¡-¿])")

_ARABIC_DIACRITICS_RE = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
                              "ة": "ه", "ى": "ي"})
_ARABIC_PUNCTUATION_RE = re.compile("(?<=[ء-يٱ-ۓ])([,;?])")
_ARABIC_PUNCTUATION = {",": "،", ";": "؛", "?": "؟"}

# Non-speech tags Whisper writes over music or applause.
_NON_SPEECH_TAG_RE = re.compile(
    r"^[\[(（]\s*(?:music|applause|laughter|silence|موسيقى|"
    r"تصفيق|ضحك|صمت)\s*[\])）]"
    r"[.,!،]*$|^[♪♫]+$",
    re.IGNORECASE,
)
# Credits Whisper copies from subtitle files. They are never lecture speech, so they
# are removed wherever they appear. Longest phrases first.
_CREDIT_PHRASES = (
    "Subtitles by the Amara.org community",
    "ترجمة نانسي قنقر",
    "نانسي قنقر",
    "Amara.org",
)
# Stock outros Whisper invents over silence or music. Real lecturers can say these
# too, so they are removed only when they form a whole sentence or segment. The
# patterns match the folded form produced by `_match_form` (lowercase, no
# punctuation or diacritics, Arabic letter variants unified).
_OUTRO_PATTERNS = (
    r"(?:thanks|thank you)(?: (?:so|very) much)?(?: (?:all|everyone|guys))? for watching"
    r"(?: (?:and )?see you(?: (?:next time|in the next (?:video|one)))?)?",
    r"(?:please )?(?:like and )?subscribe(?: to (?:the|my|our) channel)?(?: and (?:like|share)(?: the video)?)?",
    r"subtitles by the amaraorg community",
    "(?:لا تنسوا (?:ال)?اشتراك|"
    "اشتركوا|اشترك|الاشتراك)"
    "(?: في القناه| بالقناه)"
    "(?: و(?:تفعيل|فعلوا|فعل) "
    "(?:زر )?الجرس)?",
    "شكرا(?: جزيلا)?(?: لكم)?(?: علي)? "
    "(?:المشاهده|للمشاهده|"
    "مشاهدتكم|لمشاهدتكم|"
    "المتابعه|متابعتكم|"
    "لمتابعتكم)",
    "ترجمه نانسي قنقر",
)
_OUTRO_RE = re.compile("^(?:" + "|".join(_OUTRO_PATTERNS) + ")$")


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
def letter_count(text: str) -> int:
    """Count Arabic and Latin letters, ignoring digits, punctuation and marks."""
    return len(ARABIC_LETTER_RE.findall(text or "")) + len(LATIN_LETTER_RE.findall(text or ""))


def arabic_letter_count(text: str) -> int:
    """Count Arabic-script letters only."""
    return len(ARABIC_LETTER_RE.findall(text or ""))


def arabic_ratio(text: str) -> float:
    """Return Arabic letters / (Arabic + Latin letters), or 0.0 when there are none.

    Purpose:
        Measure which script a transcript is written in, for language detection and
        for the cleaner's translation guard.
    """
    arabic = len(ARABIC_LETTER_RE.findall(text or ""))
    latin = len(LATIN_LETTER_RE.findall(text or ""))
    total = arabic + latin
    return arabic / total if total else 0.0


def detect_text_language(text: str) -> str | None:
    """Guess the transcript language from its script.

    Returns:
        'ar' when at least 30% of the letters are Arabic, 'en' otherwise, and None
        when the text has fewer than 20 letters (too little to judge).
    """
    if letter_count(text) < MIN_DETECTION_LETTERS:
        return None
    return "ar" if arabic_ratio(text) >= ARABIC_RATIO_THRESHOLD else "en"


def normalize_language(value: Any) -> str | None:
    """Normalise a language code or name to lowercase ISO-639-1.

    Purpose:
        One place that maps request values ('auto', 'ar', 'AR', 'ar-SA') and Whisper
        names ('arabic') to the code used in prompts, caches and results.
    Returns:
        A two-letter code, or None for 'auto', empty, None and unknown values (all of
        which mean "detect automatically").
    """
    if value is None:
        return None
    text = str(value).strip().lower().replace("_", "-")
    if text in AUTO_LANGUAGE_VALUES:
        return None
    base = text.split("-", 1)[0]
    if base in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[base]
    if re.fullmatch(r"[a-z]{2}", base):
        return base
    return None


def language_label(value: Any) -> str:
    """Return the requested-language label used in cache keys and results ('auto' or a code)."""
    return normalize_language(value) or "auto"


def language_name(value: Any) -> str | None:
    """Return the English name of a language code ('ar' -> 'Arabic'), or None when unknown."""
    code = normalize_language(value)
    if not code:
        return None
    return LANGUAGE_NAMES.get(code)


def resolve_detected_language(text: str, reported: Any = None, requested: Any = None) -> str | None:
    """Decide which language a produced transcript is actually written in.

    Args:
        text: The transcript text.
        reported: Language reported by Whisper (verbose_json `language`), if any.
        requested: Language the job asked for, if any.
    Returns:
        A language code, or None when nothing is known.
    Workflow:
        The script decides between Arabic-script and Latin-script languages because
        it reflects what was written, even when Whisper was forced to a language.
        Within a script the reported language, then the requested one, is preferred
        so that e.g. French is not reported as English. Code-switched Arabic (an
        Arabic programming lecture read out with code and English terms) can fall
        under the 30% Arabic-letter threshold; with at least 10% Arabic letters an
        Arabic verdict from Whisper or the request wins, so it is not labelled English.
    """
    reported_code = normalize_language(reported)
    requested_code = normalize_language(requested)
    by_script = detect_text_language(text)
    if by_script == "ar":
        for candidate in (reported_code, requested_code):
            if candidate in ARABIC_SCRIPT_LANGUAGES:
                return candidate
        return "ar"
    if by_script == "en":
        if arabic_ratio(text) >= CODE_SWITCHED_ARABIC_RATIO:
            for candidate in (reported_code, requested_code):
                if candidate in ARABIC_SCRIPT_LANGUAGES:
                    return candidate
        for candidate in (reported_code, requested_code):
            if candidate and candidate not in ARABIC_SCRIPT_LANGUAGES:
                return candidate
        return "en"
    return reported_code or requested_code


# ---------------------------------------------------------------------------
# Character-level clean-up
# ---------------------------------------------------------------------------
def repair_mojibake(text: str) -> str:
    """Repair common UTF-8 text that was decoded as Windows-1252 ("â€™" -> "'")."""
    for bad, good in _MOJIBAKE_REPLACEMENTS:
        text = text.replace(bad, good)
    return _STRAY_A_CIRCUMFLEX_RE.sub("", text)


def remove_invisible_characters(text: str) -> str:
    """Drop zero-width and bidi control characters that break matching and RTL rendering."""
    return _INVISIBLE_RE.sub("", text)


def _match_form(token: str) -> str:
    """Fold a token for comparisons: case, punctuation, diacritics and letter variants."""
    folded = unicodedata.normalize("NFKC", token).lower()
    folded = _ARABIC_DIACRITICS_RE.sub("", folded).translate(_ARABIC_FOLD)
    return "".join(char for char in folded if char.isalnum())


def comparable_words(text: str) -> list[str]:
    """Words folded for comparison: no case, punctuation, diacritics or letter variants."""
    return [form for form in (_match_form(word) for word in (text or "").split()) if form]


def _has_digit(form: str) -> bool:
    return any(char.isdigit() for char in form)


# Values lecturers read out as words: bits, digits, truth values and signal levels.
# "صفر واحد واحد واحد" is the bit string 0111, not a loop. Stored in `_match_form`
# folding (ة -> ه, أ/إ -> ا), so every spelling variant matches.
_VALUE_WORDS = frozenset(
    _match_form(word)
    for word in (
        "صفر", "واحد", "واحدة", "اثنين", "اثنان", "اثنتين", "اثنتان", "ثنين", "تنين", "اتنين",
        "ثلاثة", "ثلاث", "تلاتة", "تلات", "أربعة", "أربع", "خمسة", "خمس", "ستة", "ست",
        "سبعة", "سبع", "ثمانية", "ثماني", "ثمان", "تمانية", "تماني", "تسعة", "تسع", "عشرة", "عشر",
        "صح", "غلط", "خطأ",
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "true", "false", "high", "low", "on", "off",
    )
)
_HEX_BYTE_RE = re.compile("[0-9a-f]{2}")


def _is_value_form(form: str) -> bool:
    """A single character, a hex byte ("ff") or a spoken value word ("one", "صفر")."""
    return len(form) <= 1 or form in _VALUE_WORDS or _HEX_BYTE_RE.fullmatch(form) is not None


def _is_protected_block(forms: Sequence[str]) -> bool:
    """Blocks with a number, or made only of values, are never loops.

    Truth tables, matrices, bit strings and MAC addresses ("0 1 1 0", "x x x",
    "one one one zero", "FF FF FF FF", "true true false") repeat legitimately.
    """
    return any(_has_digit(form) for form in forms) or all(_is_value_form(form) for form in forms)


# ---------------------------------------------------------------------------
# Token-level engine shared by the public helpers
# ---------------------------------------------------------------------------
def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _segment_field(segment: Any, name: str) -> Any:
    if isinstance(segment, Mapping):
        return segment.get(name)
    return getattr(segment, name, None)


def _segment_breaks(tokens: Sequence[str], segments: Iterable[Any] | None) -> dict[int, float] | None:
    """Map Whisper segment ends onto token indices, with the pause after each.

    Returns:
        `{token_index: pause_seconds}` for every segment end except the last, or None
        when there are no segments or their words do not line up with the text (the
        text is then laid out from punctuation alone).
    """
    if not segments:
        return None
    items = []
    for segment in segments:
        count = len(str(_segment_field(segment, "text") or "").split())
        if count:
            items.append((count, _float_or_none(_segment_field(segment, "start")),
                          _float_or_none(_segment_field(segment, "end"))))
    if not items or sum(count for count, _, _ in items) != len(tokens):
        return None
    breaks: dict[int, float] = {}
    index = -1
    for position, (count, _start, end) in enumerate(items):
        index += count
        if position + 1 < len(items):
            next_start = items[position + 1][1]
            pause = next_start - end if next_start is not None and end is not None else 0.0
            breaks[index] = max(0.0, pause)
    return breaks


def _filter_tokens(
    tokens: Sequence[str],
    breaks: dict[int, float] | None,
    keep: Sequence[bool],
) -> tuple[list[str], dict[int, float] | None]:
    """Drop tokens while moving their segment boundaries onto the previous kept token."""
    kept: list[str] = []
    new_breaks: dict[int, float] | None = {} if breaks is not None else None
    for index, token in enumerate(tokens):
        if keep[index]:
            kept.append(token)
        if new_breaks is not None and index in breaks and kept:
            last = len(kept) - 1
            new_breaks[last] = max(new_breaks.get(last, 0.0), breaks[index])
    if new_breaks is not None and kept:
        new_breaks.pop(len(kept) - 1, None)
    return kept, new_breaks


def _ends_sentence(token: str) -> bool:
    """Whether a token ends a sentence (., !, ?, ؟, …) and is not an abbreviation."""
    if not _SENTENCE_END_RE.search(token):
        return False
    stripped = token.rstrip(_TRAILING_CLOSERS)
    if stripped.endswith(".") and not stripped.endswith(".."):
        core = stripped.rstrip(".").lower()
        if core.replace(".", "") in _ABBREVIATIONS:
            return False
        if len(core) == 1 and core.isalpha():
            return False
    return True


def _best_cut(tokens: Sequence[str], start: int, end: int, breaks: dict[int, float] | None) -> int:
    """Pick where to split an over-long sentence: clause mark, then pause, then word budget."""
    lowest = start + SENTENCE_MIN_PIECE_WORDS - 1
    strong = boundary = weak = None
    for index in range(end, lowest - 1, -1):
        token = tokens[index]
        if strong is None and _STRONG_CLAUSE_RE.search(token):
            strong = index
        if boundary is None and breaks is not None and index in breaks:
            boundary = index
        if weak is None and _WEAK_CLAUSE_RE.search(token):
            weak = index
    for candidate in (strong, boundary, weak):
        if candidate is not None:
            return candidate
    return end


def _sentence_spans(
    tokens: Sequence[str],
    breaks: dict[int, float] | None,
    max_words: int = SENTENCE_MAX_WORDS,
) -> list[tuple[int, int]]:
    """Split tokens into sentence-like `(start, end)` spans.

    Sentences end at . ! ? ؟ …; a long pause between Whisper segments also ends a
    sentence of at least 20 words. Anything longer than `max_words` (unpunctuated
    ASR, Arabic joined only by ، or ؛) is cut at the best clause mark, segment
    boundary or, failing both, the word budget.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for index, token in enumerate(tokens):
        length = index - start + 1
        ends = _ends_sentence(token)
        if (not ends and breaks is not None and length >= SENTENCE_SOFT_WORDS
                and breaks.get(index, 0.0) >= SENTENCE_PAUSE_SECONDS):
            ends = True
        if ends:
            spans.append((start, index + 1))
            start = index + 1
        elif length >= max_words:
            cut = _best_cut(tokens, start, index, breaks)
            spans.append((start, cut + 1))
            start = cut + 1
    if start < len(tokens):
        spans.append((start, len(tokens)))
    return spans


def _group_paragraphs(
    tokens: Sequence[str],
    spans: Sequence[tuple[int, int]],
    breaks: dict[int, float] | None,
) -> list[tuple[int, int]]:
    """Group sentence spans into paragraphs of roughly 3-6 sentences / 90 words."""
    paragraphs: list[tuple[int, int]] = []
    if not spans:
        return paragraphs
    first = spans[0][0]
    sentences = 0
    words = 0
    for position, (start, end) in enumerate(spans):
        sentences += 1
        words += end - start
        if position + 1 == len(spans):
            break
        if _MARKDOWN_START_RE.fullmatch(tokens[spans[position + 1][0]]):
            continue
        pause = breaks.get(end - 1, 0.0) if breaks is not None else 0.0
        if (words >= PARAGRAPH_MAX_WORDS
                or (sentences >= PARAGRAPH_MAX_SENTENCES and words >= 30)
                or (sentences >= PARAGRAPH_MIN_SENTENCES and words >= PARAGRAPH_TARGET_WORDS)
                or (pause >= PARAGRAPH_PAUSE_SECONDS and words >= PARAGRAPH_PAUSE_MIN_WORDS)):
            paragraphs.append((first, end))
            first = end
            sentences = 0
            words = 0
    paragraphs.append((first, spans[-1][1]))
    return paragraphs


def _join_paragraphs(tokens: Sequence[str], paragraphs: Sequence[tuple[int, int]]) -> str:
    return "\n\n".join(" ".join(tokens[start:end]) for start, end in paragraphs if end > start)


def _phrase_forms(phrase: str) -> list[str]:
    return [form for form in (_match_form(word) for word in phrase.split()) if form]


_CREDIT_FORMS = [_phrase_forms(phrase) for phrase in _CREDIT_PHRASES]


def _junk_keep_mask(tokens: Sequence[str]) -> list[bool]:
    """Mark non-speech tags and subtitle credits for removal (anywhere in the text)."""
    keep = [not _NON_SPEECH_TAG_RE.match(token) for token in tokens]
    forms = [_match_form(token) for token in tokens]
    for phrase in _CREDIT_FORMS:
        size = len(phrase)
        if not size:
            continue
        index = 0
        while index + size <= len(forms):
            if keep[index] and forms[index:index + size] == phrase:
                for offset in range(size):
                    keep[index + offset] = False
                index += size
            else:
                index += 1
    return keep


def _is_outro(tokens: Sequence[str]) -> bool:
    form = " ".join(part for part in (_match_form(token) for token in tokens) if part)
    return bool(form) and _OUTRO_RE.fullmatch(form) is not None


def _segment_spans(token_count: int, breaks: dict[int, float]) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for end in sorted(breaks):
        spans.append((start, end + 1))
        start = end + 1
    if start < token_count:
        spans.append((start, token_count))
    return spans


def _loop_keep_mask(tokens: Sequence[str], max_block: int = LOOP_MAX_BLOCK_WORDS) -> list[bool]:
    """Mark Whisper repetition loops for removal.

    A block of 1-30 words repeated at least three times in a row is a loop. Blocks of
    up to three words keep two copies ("no no", "لا لا" stay natural emphasis);
    longer blocks keep one. Blocks with a number, or made only of values (single
    characters, hex bytes, spoken digits and truth values), are never collapsed, so
    "0 0 0 1", "1 0 0 1 0 0 1 0 0" or "صفر واحد واحد واحد صفر" survive.
    """
    forms = [_match_form(token) for token in tokens]
    total = len(tokens)
    keep = [True] * total
    index = 0
    while index < total:
        advanced = False
        for size in range(1, min(max_block, (total - index) // LOOP_MIN_REPEATS) + 1):
            if forms[index] != forms[index + size] or forms[index] != forms[index + 2 * size]:
                continue
            block = forms[index:index + size]
            if _is_protected_block(block):
                continue
            repeats = 1
            while (index + (repeats + 1) * size <= total
                   and forms[index + repeats * size:index + (repeats + 1) * size] == block):
                repeats += 1
            if repeats < LOOP_MIN_REPEATS:
                continue
            copies = 2 if size <= 3 else 1
            for position in range(index + copies * size, index + repeats * size):
                keep[position] = False
            index += repeats * size
            advanced = True
            break
        if not advanced:
            index += 1
    return keep


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def split_sentences(text: str, max_words: int = SENTENCE_MAX_WORDS) -> list[str]:
    """Split text into sentence-like units, Arabic-aware.

    Handles . ! ? ؟ … as sentence ends, splits over-long sentences at ، ؛ ; (then
    , :) and cuts unpunctuated ASR text by a word budget. Whitespace is normalised.
    """
    tokens = (text or "").split()
    return [" ".join(tokens[start:end]) for start, end in _sentence_spans(tokens, None, max_words)]


def strip_hallucinations(text: str) -> str:
    """Remove known Whisper hallucinations without touching real speech.

    Subtitle credits ("ترجمة نانسي قنقر", "Subtitles by the Amara.org community") and
    non-speech tags ("[Music]", "[موسيقى]") are removed wherever they appear; stock
    outros ("Thanks for watching", "اشتركوا في القناة", "شكراً للمشاهدة") only when
    they make up a whole sentence. Paragraph breaks are kept.
    """
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", (text or "").strip()):
        tokens = paragraph.split()
        keep = _junk_keep_mask(tokens)
        tokens = [token for token, kept in zip(tokens, keep) if kept]
        sentences = [
            " ".join(tokens[start:end])
            for start, end in _sentence_spans(tokens, None)
            if not _is_outro(tokens[start:end])
        ]
        if sentences:
            paragraphs.append(" ".join(sentences))
    return "\n\n".join(paragraphs)


def collapse_loops(text: str, max_block: int = LOOP_MAX_BLOCK_WORDS) -> str:
    """Collapse Whisper repetition loops line by line (see `_loop_keep_mask`)."""
    lines = []
    for line in (text or "").split("\n"):
        tokens = line.split()
        keep = _loop_keep_mask(tokens, max_block)
        lines.append(" ".join(token for token, kept in zip(tokens, keep) if kept))
    return "\n".join(lines)


def paragraphize(text: str, segments: Iterable[Any] | None = None) -> str:
    """Lay out raw Whisper text in paragraphs, changing whitespace only.

    Args:
        text: Whisper text in the spoken language.
        segments: Optional Whisper segments (`text`, `start`, `end`); their pauses
            guide sentence and paragraph breaks when their words match the text.
    Returns:
        The same words in the same order, joined by single spaces within a paragraph
        and blank lines between paragraphs.
    """
    tokens = (text or "").split()
    if not tokens:
        return ""
    breaks = _segment_breaks(tokens, list(segments) if segments else None)
    spans = _sentence_spans(tokens, breaks)
    return _join_paragraphs(tokens, _group_paragraphs(tokens, spans, breaks))


def format_transcript(
    text: str,
    segments: Iterable[Any] | None = None,
    language: str | None = None,
) -> str:
    """Produce the fast-mode transcript: readable paragraphs with no model involved.

    Args:
        text: Raw Whisper text (as written by the pipeline).
        segments: Optional Whisper segments aligned with `text`.
        language: Lecture language when known; detected from the script otherwise.
    Returns:
        Markdown-subset text (blank-line separated paragraphs) in the original
        language: mojibake and invisible controls repaired, non-speech tags,
        subtitle credits and whole-sentence stock outros removed, repetition loops
        collapsed (never numbers), Arabic punctuation used between Arabic words, and
        sentences grouped into paragraphs of about 3-6 sentences.
    Connects to:
        Used for fast mode, as the LLM input layout, and as the per-chunk fallback
        of the LLM cleaner.
    """
    original = (text or "").split()
    if not original:
        return ""
    breaks = _segment_breaks(original, list(segments) if segments else None)

    code = normalize_language(language) or detect_text_language(text)
    arabic_script = code in ARABIC_SCRIPT_LANGUAGES
    tokens: list[str] = []
    new_breaks: dict[int, float] | None = {} if breaks is not None else None
    for index, token in enumerate(original):
        repaired = remove_invisible_characters(repair_mojibake(token))
        if arabic_script:
            repaired = _ARABIC_PUNCTUATION_RE.sub(lambda match: _ARABIC_PUNCTUATION[match.group(1)], repaired)
        tokens.extend(repaired.split())
        if new_breaks is not None and index in breaks and tokens:
            last = len(tokens) - 1
            new_breaks[last] = max(new_breaks.get(last, 0.0), breaks[index])
    if new_breaks is not None and tokens:
        new_breaks.pop(len(tokens) - 1, None)
    breaks = new_breaks

    keep = _junk_keep_mask(tokens)
    if breaks is not None:
        for start, end in _segment_spans(len(tokens), breaks):
            if _is_outro(tokens[start:end]):
                keep[start:end] = [False] * (end - start)
    tokens, breaks = _filter_tokens(tokens, breaks, keep)

    tokens, breaks = _filter_tokens(tokens, breaks, _loop_keep_mask(tokens))

    keep = [True] * len(tokens)
    for start, end in _sentence_spans(tokens, breaks):
        if _is_outro(tokens[start:end]):
            keep[start:end] = [False] * (end - start)
    tokens, breaks = _filter_tokens(tokens, breaks, keep)
    if not tokens:
        return ""

    spans = _sentence_spans(tokens, breaks)
    return _join_paragraphs(tokens, _group_paragraphs(tokens, spans, breaks))
