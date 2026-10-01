"""Language preservation, deterministic formatting, cleaner guards and cache keys.

No network, no faster-whisper: every provider call is stubbed.
"""

import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import clean_with_Llama as cleaner
import MainCode_FasterWhisper as pipeline
import transcript_format as tf


ARABIC_SENTENCES = [
    "اليوم سنتحدث عن هياكل البيانات وأهميتها في البرمجة.",
    "نبدأ بالمصفوفات لأنها أبسط بنية لتخزين العناصر بشكل متتابع في الذاكرة.",
    "ثم ننتقل إلى القوائم المترابطة ونقارن بينها وبين المصفوفات من حيث التعقيد.",
    "بعد ذلك نشرح الأشجار الثنائية وكيف نبحث فيها عن عنصر معين بسرعة.",
    "وفي النهاية نحل بعض التمارين ونكتب الـ code الخاص بكل عملية.",
]
ARABIC_TEXT = " ".join(ARABIC_SENTENCES * 2)
ENGLISH_TEXT = (
    "Today we talk about data structures and why they matter. We start with arrays because they "
    "store elements contiguously in memory. Then we move to linked lists and compare their complexity. "
    "After that we explain binary trees and how to search them quickly. Finally we solve exercises."
)
URL = "https://youtu.be/nBDFtTDXLAs"
VIDEO_ID = "nBDFtTDXLAs"
# An Arabic programming lecture read out with code: under 30% Arabic letters.
CODE_HEAVY_ARABIC = (
    "هلأ بنكتب int x equals zero و بعدين for int i equals zero i less than n "
    "و بعدها return sum"
)
CODE_HEAVY_IN_ENGLISH = (
    "Now we write int x equals zero and then for int i equals zero i less than n "
    "and after that return sum"
)


def english_of(_prompt: str) -> str:
    """A cleaner that ignores the rules and translates everything into English."""
    return "Today we will talk about data structures and their importance in programming, " * 6


def echo_chunk(prompt: str) -> str:
    """A well-behaved cleaner: returns the transcript chunk unchanged."""
    return prompt.split("Transcript chunk:\n", 1)[1].strip()


class LanguageHelpersTests(unittest.TestCase):
    def test_arabic_ratio_and_detection(self):
        self.assertGreater(tf.arabic_ratio(ARABIC_TEXT), 0.9)
        self.assertEqual(tf.arabic_ratio(ENGLISH_TEXT), 0.0)
        self.assertEqual(tf.detect_text_language(ARABIC_TEXT), "ar")
        self.assertEqual(tf.detect_text_language(ENGLISH_TEXT), "en")
        # Arabic lecture with English technical terms is still Arabic.
        self.assertEqual(tf.detect_text_language("نستخدم الـ binary search tree لتسريع البحث في البيانات"), "ar")
        self.assertIsNone(tf.detect_text_language("ok 123"))
        self.assertEqual(tf.arabic_ratio("123 + 456"), 0.0)

    def test_normalize_language(self):
        for value in (None, "", "auto", "AUTO", " detect "):
            self.assertIsNone(tf.normalize_language(value))
        self.assertEqual(tf.normalize_language("ar"), "ar")
        self.assertEqual(tf.normalize_language("AR"), "ar")
        self.assertEqual(tf.normalize_language("ar-SA"), "ar")
        self.assertEqual(tf.normalize_language("arabic"), "ar")
        self.assertEqual(tf.normalize_language("English"), "en")
        self.assertEqual(tf.normalize_language("fr"), "fr")
        self.assertIsNone(tf.normalize_language("klingon"))
        self.assertEqual(tf.language_label(None), "auto")
        self.assertEqual(tf.language_label("Arabic"), "ar")

    def test_resolve_detected_language_trusts_the_script(self):
        self.assertEqual(tf.resolve_detected_language(ARABIC_TEXT, "english"), "ar")
        self.assertEqual(tf.resolve_detected_language(ENGLISH_TEXT, "arabic"), "en")
        self.assertEqual(tf.resolve_detected_language(ENGLISH_TEXT, "fr"), "fr")
        self.assertEqual(tf.resolve_detected_language("", None, "ar"), "ar")
        self.assertIsNone(tf.resolve_detected_language("", None, None))

    def test_code_switched_arabic_is_not_labelled_english(self):
        self.assertLess(tf.arabic_ratio(CODE_HEAVY_ARABIC), tf.ARABIC_RATIO_THRESHOLD)
        self.assertEqual(tf.resolve_detected_language(CODE_HEAVY_ARABIC, "arabic"), "ar")
        self.assertEqual(tf.resolve_detected_language(CODE_HEAVY_ARABIC, None, "ar"), "ar")
        # With no Arabic verdict the script still decides.
        self.assertEqual(tf.resolve_detected_language(CODE_HEAVY_ARABIC, "english"), "en")
        # English text quoting a little Arabic stays English.
        quoting = ENGLISH_TEXT + " The Arabic greeting is مرحبا بكم جميعا في محاضرة اليوم عن هياكل البيانات."
        self.assertTrue(0.1 <= tf.arabic_ratio(quoting) < 0.3, tf.arabic_ratio(quoting))
        self.assertEqual(tf.resolve_detected_language(quoting, "english"), "en")
        # A pure English translation of an Arabic request is still reported as English.
        self.assertEqual(tf.resolve_detected_language(CODE_HEAVY_IN_ENGLISH, "arabic", "ar"), "en")

    def test_every_whisper_language_name_is_mapped(self):
        for name, code in (("polish", "pl"), ("swahili", "sw"), ("Haitian Creole", "ht"), ("burmese", "my")):
            self.assertEqual(tf.normalize_language(name), code)
        self.assertEqual(tf.language_name("pl"), "Polish")


class FormatterTests(unittest.TestCase):
    def test_split_sentences_handles_arabic_marks(self):
        text = "هل فهمتم الفكرة؟ نعم فهمناها جيدا. ممتاز! لننتقل"
        self.assertEqual(
            tf.split_sentences(text),
            ["هل فهمتم الفكرة؟", "نعم فهمناها جيدا.", "ممتاز!", "لننتقل"],
        )
        long_clauses = "، ".join(["هذه جملة طويلة فيها عدة كلمات"] * 12)
        pieces = tf.split_sentences(long_clauses)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece.split()) <= tf.SENTENCE_MAX_WORDS for piece in pieces))
        self.assertTrue(all(piece.endswith("،") for piece in pieces[:-1]))

    def test_unpunctuated_text_is_split_by_word_budget(self):
        text = " ".join(f"كلمة{i}" for i in range(130))
        pieces = tf.split_sentences(text)
        self.assertEqual(" ".join(pieces), text)
        self.assertTrue(all(len(piece.split()) <= tf.SENTENCE_MAX_WORDS for piece in pieces))

    def test_abbreviations_do_not_end_sentences(self):
        self.assertEqual(len(tf.split_sentences("Dr. Smith uses e.g. arrays. Then we stop.")), 2)

    def test_paragraphize_changes_whitespace_only(self):
        for text in (ARABIC_TEXT * 3, ENGLISH_TEXT * 4, " ".join(f"word{i}" for i in range(400))):
            with self.subTest(text=text[:20]):
                laid_out = tf.paragraphize(text)
                self.assertEqual(laid_out.split(), text.split())
                self.assertIn("\n\n", laid_out)
                for paragraph in laid_out.split("\n\n"):
                    self.assertLessEqual(len(paragraph.split()), tf.PARAGRAPH_MAX_WORDS + tf.SENTENCE_MAX_WORDS)

    def test_paragraphize_uses_segment_pauses(self):
        segments = [
            {"start": 0.0, "end": 9.0, "text": " ".join(["alpha"] * 30)},
            {"start": 9.1, "end": 18.0, "text": " ".join(["beta"] * 30)},
            {"start": 21.0, "end": 25.0, "text": "gamma delta epsilon"},
        ]
        text = " ".join(segment["text"] for segment in segments)
        laid_out = tf.paragraphize(text, segments)
        self.assertEqual(laid_out.split(), text.split())
        self.assertTrue(laid_out.endswith("\n\ngamma delta epsilon"))
        # Misaligned segments are ignored instead of corrupting the text.
        self.assertEqual(tf.paragraphize("one two three", [{"text": "one"}]).split(), ["one", "two", "three"])

    def test_paragraph_never_starts_with_markdown_marker(self):
        sentences = [f"Sentence number {i} explains a point in detail for the class." for i in range(12)]
        sentences[6] = "- this line starts with a dash."
        text = " ".join(sentences)
        for laid_out in (tf.paragraphize(text), tf.format_transcript(text)):
            for paragraph in laid_out.split("\n\n"):
                self.assertFalse(re.match(r"(#{1,6}|[-*+]|\d+[.)])\s", paragraph))

    def test_hallucinations_are_removed_but_real_speech_stays(self):
        text = (
            "We finished the proof of the theorem. Thanks for watching! "
            "[Music] Subtitles by the Amara.org community "
            "اشتركوا في القناة. ترجمة نانسي قنقر"
        )
        stripped = tf.strip_hallucinations(text)
        self.assertEqual(stripped, "We finished the proof of the theorem.")
        # Inside a real sentence the phrase is speech, not an outro.
        kept = "I want to say thanks for watching the whole series with me."
        self.assertEqual(tf.strip_hallucinations(kept), kept)

    def test_loops_collapse_but_numbers_and_emphasis_survive(self):
        self.assertEqual(
            tf.collapse_loops("Truth table: 0 0 0 1 1 0 1 1 is the input."),
            "Truth table: 0 0 0 1 1 0 1 1 is the input.",
        )
        self.assertEqual(tf.collapse_loops("matrix 1 0 0 1 0 0 1 0 0"), "matrix 1 0 0 1 0 0 1 0 0")
        self.assertEqual(tf.collapse_loops("very very important, x x x x"), "very very important, x x x x")
        self.assertEqual(tf.collapse_loops("شوي شوي لا لا لا لا"), "شوي شوي لا لا")
        self.assertEqual(tf.collapse_loops("step 1 step 1 step 1"), "step 1 step 1 step 1")
        loop = " ".join(["this is the same sentence again"] * 6) + " end"
        self.assertEqual(tf.collapse_loops(loop), "this is the same sentence again end")

    def test_values_read_out_as_words_are_never_collapsed(self):
        for text in (
            "القيمة هي صفر واحد واحد واحد صفر",
            "the bits are one one one zero",
            "the bits are one, one, one, zero",
            "the broadcast MAC address is FF FF FF FF FF FF",
            "الناتج ثلاثة ثلاثة ثلاثة",
            "the outputs are true true true false",
            "the signal stays HIGH HIGH HIGH then LOW",
        ):
            with self.subTest(text=text):
                self.assertEqual(tf.collapse_loops(text), text)
                self.assertEqual(tf.format_transcript(text).split(), text.split())
        # Ordinary words still collapse.
        self.assertEqual(tf.collapse_loops("so so so so we start"), "so so we start")

    def test_format_transcript_arabic(self):
        raw = ARABIC_TEXT + " " + " ".join(["نعم نعم"] * 5) + " ترجمة نانسي قنقر"
        formatted = tf.format_transcript(raw, language="ar")
        self.assertNotIn("نانسي", formatted)
        self.assertGreater(tf.arabic_ratio(formatted), 0.9)
        self.assertIn("\n\n", formatted)
        for sentence in ARABIC_SENTENCES:
            self.assertIn(sentence, formatted)
        self.assertNotRegex(formatted, r"(?m)^(#|- |\d+\. )")

    def test_format_transcript_uses_arabic_punctuation_between_arabic_words(self):
        formatted = tf.format_transcript("هل فهمتم? نعم, فهمنا الفكرة. The value is 3, not 4?", language="ar")
        self.assertIn("فهمتم؟", formatted)
        self.assertIn("نعم،", formatted)
        self.assertIn("3, not 4?", formatted)

    def test_format_transcript_english_with_segments(self):
        segments = [
            {"start": 0.0, "end": 5.0, "text": "So today we look at sorting."},
            {"start": 5.1, "end": 6.0, "text": "Thanks for watching!"},
            {"start": 6.1, "end": 9.0, "text": "Bubble sort swaps neighbours, 3 2 1 becomes 1 2 3."},
        ]
        text = " ".join(segment["text"] for segment in segments)
        formatted = tf.format_transcript(text, segments, "en")
        self.assertEqual(
            formatted,
            "So today we look at sorting. Bubble sort swaps neighbours, 3 2 1 becomes 1 2 3.",
        )

    def test_mojibake_and_invisible_characters_are_repaired(self):
        self.assertEqual(tf.format_transcript("Itâ€™s a​ test."), "It's a test.")


class MarkdownLayoutTests(unittest.TestCase):
    def test_math_is_not_corrupted(self):
        for text in (
            "The result is (a + b) - c and f(x) - g(x).",
            "the absolute value |x| and |y| are equal",
            "Version 3. 2. Something new",
            "We subtract: x - 5 from the total.",
        ):
            with self.subTest(text=text):
                self.assertEqual(cleaner.normalize_markdown_layout(text), text)

    def test_arabic_headings_and_bullets_are_recognised(self):
        self.assertEqual(
            cleaner.normalize_markdown_layout("شرح الفكرة الأولى ## الموضوع الثاني"),
            "شرح الفكرة الأولى\n\n## الموضوع الثاني",
        )
        self.assertEqual(
            cleaner.normalize_markdown_layout("سؤال؟ ## عنوان جديد\nنص الفقرة"),
            "سؤال؟\n\n## عنوان جديد\n\nنص الفقرة",
        )
        self.assertEqual(
            cleaner.normalize_markdown_layout("العناصر هي: - المصفوفات\n- القوائم"),
            "العناصر هي:\n- المصفوفات\n- القوائم",
        )
        self.assertEqual(
            cleaner.normalize_markdown_layout("Three structures: - arrays - linked lists - trees"),
            "Three structures:\n- arrays\n- linked lists\n- trees",
        )

    def test_prose_dashes_and_minus_signs_are_not_turned_into_bullets(self):
        for text in (
            "لماذا؟ - لأن الذاكرة محدودة",
            "النتيجة هي: - س تربيع",
            "x = 5. - Next we look at the loop.",
            "Really! - said the student.",
        ):
            with self.subTest(text=text):
                self.assertEqual(cleaner.normalize_markdown_layout(text), text)

    def test_layout_is_limited_to_the_markdown_subset(self):
        text = "Intro\n---\n* first\n* second\n#### Deep heading\nBody"
        self.assertEqual(
            cleaner.normalize_markdown_layout(text),
            "Intro\n\n- first\n- second\n\n### Deep heading\n\nBody",
        )

    def test_dedupe_keeps_short_and_numeric_repeats(self):
        self.assertEqual(cleaner.dedupe_consecutive_units("No. No. 1 0 1. 1 0 1."), "No. No. 1 0 1. 1 0 1.")
        self.assertEqual(
            cleaner.dedupe_consecutive_units("This is repeated here. This is repeated here."),
            "This is repeated here.",
        )


class PromptTests(unittest.TestCase):
    def test_system_rules_never_ask_for_translation(self):
        for language in ("ar", "en", None):
            with self.subTest(language=language):
                rules = cleaner.build_system_rules(language)
                self.assertNotIn("English only", rules)
                self.assertNotRegex(rules, r"(?i)\btranslate (arabic|it|into|the)\b")
                self.assertIn("Never translate", rules)
                self.assertIn("Arabic script", rules)
        self.assertIn("main language is Arabic", cleaner.build_system_rules("ar"))
        self.assertIn("main language is English", cleaner.build_system_rules("en"))
        self.assertNotIn("translat", cleaner.SYSTEM_RULES.replace("Never translate", "").replace("NOT a translator", ""))

    def test_user_prompt_carries_the_language_hint(self):
        prompt = cleaner.make_user_prompt("نص المحاضرة", "ar")
        self.assertIn("Lecture language: Arabic (ar)", prompt)
        self.assertIn("Do not translate", prompt)
        self.assertTrue(prompt.rstrip().endswith("نص المحاضرة"))
        self.assertIn("not specified", cleaner.make_user_prompt("text", None))
        self.assertIn("changed the language", cleaner.make_retry_prompt("نص", "ar", "translated"))

    def test_no_english_repair_helpers_remain(self):
        self.assertFalse(hasattr(cleaner, "make_english_repair_prompt"))


class GuardTests(unittest.TestCase):
    def test_script_guard(self):
        source = ARABIC_TEXT
        self.assertEqual(cleaner.validate_chunk(source, english_of("")), "translated")
        self.assertIsNone(cleaner.validate_chunk(source, source))
        # Same Arabic with a few English terms still passes.
        self.assertIsNone(cleaner.validate_chunk(source, source.replace("البرمجة", "programming")))
        # English must not turn into Arabic either.
        self.assertEqual(cleaner.validate_chunk(ENGLISH_TEXT, ARABIC_TEXT), "translated")

    def test_script_guard_covers_code_switched_arabic(self):
        source = " ".join([CODE_HEAVY_ARABIC] * 3)
        translated = " ".join([CODE_HEAVY_IN_ENGLISH] * 3)
        self.assertLess(tf.arabic_ratio(source), tf.ARABIC_RATIO_THRESHOLD)
        for language in ("ar", None):
            with self.subTest(language=language):
                self.assertEqual(cleaner.validate_chunk(source, translated, language), "translated")
                self.assertIsNone(cleaner.validate_chunk(source, source, language))
        # An Arabic lecture must keep its Arabic letters, whatever the ratio says.
        self.assertEqual(
            cleaner.validate_chunk(ARABIC_TEXT, ARABIC_TEXT[: len(ARABIC_TEXT) // 3] + " " + english_of(""), "ar"),
            "translated",
        )

    def test_coverage_bounds(self):
        words = " ".join(f"word{i}" for i in range(100))
        self.assertEqual(cleaner.validate_chunk(words, " ".join(words.split()[:70])), "too_short")
        self.assertEqual(cleaner.validate_chunk(words, words + " " + words), "too_long")
        self.assertIsNone(cleaner.validate_chunk(words, "## Heading\n\n" + words))
        # Invented headings do not count towards coverage.
        self.assertEqual(
            cleaner.validate_chunk(words, "\n".join(f"## heading {i}" for i in range(40)) + "\n" + " ".join(words.split()[:60])),
            "too_short",
        )

    def test_preamble_is_stripped(self):
        self.assertEqual(cleaner.strip_model_wrapping("Here is the cleaned transcript:\n\nBody text."), "Body text.")
        self.assertEqual(cleaner.strip_model_wrapping("إليك النص بعد التحرير:\nنص المحاضرة"), "نص المحاضرة")
        self.assertEqual(cleaner.strip_model_wrapping("```\nBody\n```"), "Body")
        # Lecture speech that ends with a colon is not a preamble.
        self.assertEqual(cleaner.strip_model_wrapping("Here is the formula:\nE = mc^2"), "Here is the formula:\nE = mc^2")

    def test_lecture_sentences_that_look_like_preambles_are_kept(self):
        sentences = (
            "هذه هي النصيحة الأهم: لا تحفظوا الكود بل افهموه.",
            "فيما يلي النصف الثاني من المحاضرة: سنشرح الـ heap.",
            "هذا هو النص الكامل للمسألة: لدينا مصفوفة من الأعداد.",
            "Here is the second version of the algorithm: we sort first.",
        )
        # The source is what the lecturer said, so an opening found there is speech.
        source = "يعني " + " ".join(sentences)
        for sentence in sentences:
            with self.subTest(sentence=sentence):
                self.assertEqual(cleaner.strip_model_wrapping(sentence, source), sentence)
        # "النصيحة" and "النصف" are not the noun "النص" at all.
        for sentence in sentences[:2]:
            self.assertEqual(cleaner.strip_model_wrapping(sentence), sentence)
        # A real preamble is not in the source and is still removed.
        self.assertEqual(cleaner.strip_model_wrapping("إليك النص بعد التحرير:\n" + sentences[2], source), sentences[2])
        self.assertEqual(
            cleaner.strip_model_wrapping("Here is the cleaned transcript:\n\n" + sentences[3], source), sentences[3]
        )


class CleanerPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        sleeper = patch("clean_with_Llama.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def write(self, text: str) -> Path:
        path = self.directory / "raw.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def clean(self, text, generate, language="ar", chunk_chars=300, stats=None):
        return cleaner.clean_transcript_with_generator(
            self.write(text), generate, "Stub", chunk_chars,
            language=language, output_path=self.directory / "out.md", stats=stats,
        )

    def test_arabic_is_kept_and_no_repair_pass_runs(self):
        generate = Mock(side_effect=echo_chunk)
        stats = {}
        output = self.clean(ARABIC_TEXT, generate, stats=stats)
        text = output.read_text(encoding="utf-8")
        self.assertGreater(tf.arabic_ratio(text), 0.9)
        self.assertEqual(generate.call_count, stats["chunks"])
        self.assertEqual(stats["fallback_chunks"], 0)
        for call in generate.call_args_list:
            self.assertIn("Lecture language: Arabic (ar)", call.args[0])
            self.assertNotIn("Translate", call.args[0])

    def test_translated_chunk_is_retried_then_kept_in_arabic(self):
        prompts = []

        def generate(prompt):
            prompts.append(prompt)
            # The first chunk is always translated; every other chunk is echoed.
            if "اليوم سنتحدث" in prompt.split("Transcript chunk:\n", 1)[1][:40]:
                return english_of(prompt)
            return echo_chunk(prompt)

        stats = {}
        text = self.clean(ARABIC_TEXT, generate, stats=stats).read_text(encoding="utf-8")
        self.assertNotIn("Today we will talk", text)
        self.assertGreater(tf.arabic_ratio(text), 0.9)
        self.assertIn(ARABIC_SENTENCES[0], text)
        self.assertGreaterEqual(stats["fallback_chunks"], 1)
        self.assertGreaterEqual(stats["llm_chunks"], 1)
        self.assertGreaterEqual(stats["rejections"]["translated"], 2)
        self.assertTrue(any("changed the language" in prompt for prompt in prompts))

    def test_truncated_chunk_falls_back_for_that_chunk_only(self):
        calls = {"count": 0}

        def generate(prompt):
            calls["count"] += 1
            if calls["count"] <= 2:
                raise cleaner.ChunkCleaningError("finish_reason=length")
            return echo_chunk(prompt)

        stats = {}
        text = self.clean(ARABIC_TEXT, generate, stats=stats).read_text(encoding="utf-8")
        self.assertEqual(stats["fallback_chunks"], 1)
        self.assertEqual(stats["llm_chunks"], stats["chunks"] - 1)
        for sentence in ARABIC_SENTENCES:
            self.assertIn(sentence, text)

    def test_provider_unavailable_before_any_chunk_raises(self):
        generate = Mock(side_effect=cleaner.CloudCleanerUnavailable("OPENROUTER_API_KEY is not set."))
        with self.assertRaises(cleaner.CleanerUnavailable):
            self.clean(ARABIC_TEXT, generate)
        generate.assert_called_once()

    def test_provider_lost_mid_lecture_keeps_finished_chunks(self):
        calls = {"count": 0}

        def generate(prompt):
            calls["count"] += 1
            if calls["count"] > 1:
                raise cleaner.CloudCleanerUnavailable("HTTP 402")
            return echo_chunk(prompt).replace("اليوم", "**اليوم**", 1)

        stats = {}
        text = self.clean(ARABIC_TEXT, generate, stats=stats).read_text(encoding="utf-8")
        self.assertIn("**اليوم**", text)
        self.assertEqual(stats["llm_chunks"], 1)
        self.assertEqual(stats["fallback_chunks"], stats["chunks"] - 1)
        self.assertIn("402", stats["stopped_reason"])

    def test_every_chunk_rejected_raises_so_the_caller_can_fall_back(self):
        with self.assertRaises(cleaner.CleanerUnavailable):
            self.clean(ARABIC_TEXT, english_of)

    def test_preamble_from_model_is_removed(self):
        text = self.clean(
            ENGLISH_TEXT, lambda prompt: "Here is the cleaned transcript:\n\n" + echo_chunk(prompt),
            language="en", chunk_chars=5000,
        ).read_text(encoding="utf-8")
        self.assertFalse(text.startswith("Here is"))
        self.assertTrue(text.startswith("Today we talk"))

    def test_chunks_keep_whole_paragraphs(self):
        paragraphs = ["فقرة " + " ".join(["كلمة"] * 20) + "." for _ in range(6)]
        chunks = cleaner.split_text("\n\n".join(paragraphs), 300)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("\n\n".join(chunks).split("\n\n"), paragraphs)

    def test_ollama_budget_fits_arabic_chunks(self):
        response = Mock(ok=True, status_code=200, json=Mock(return_value={"response": "نص", "done_reason": "stop"}))
        with patch("clean_with_Llama.requests.post", return_value=response) as post:
            self.assertEqual(cleaner.ollama_generate("prompt", system="rules"), "نص")
        options = post.call_args.kwargs["json"]["options"]
        self.assertGreaterEqual(options["num_predict"], 4096)
        self.assertEqual(options["num_ctx"], 8192)
        self.assertEqual(post.call_args.kwargs["json"]["system"], "rules")
        response.json.return_value = {"response": "نص مقطوع", "done_reason": "length"}
        with patch("clean_with_Llama.requests.post", return_value=response):
            with self.assertRaises(cleaner.ChunkCleaningError):
                cleaner.ollama_generate("prompt")


class OpenRouterCleanerTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        environment.start()
        self.addCleanup(environment.stop)
        sleeper = patch("clean_with_Llama.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        cleaner._reasoning_rejected = False

    def reply(self, content="نص", finish_reason="stop", status=200):
        body = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
                "usage": {"completion_tokens": 5}}
        return Mock(ok=status == 200, status_code=status, headers={}, json=Mock(return_value=body))

    def test_language_rules_are_sent_as_system_prompt(self):
        with patch("clean_with_Llama.requests.post", return_value=self.reply()) as post:
            self.assertEqual(cleaner.openrouter_generate("prompt", system=cleaner.build_system_rules("ar")), "نص")
        messages = post.call_args.kwargs["json"]["messages"]
        self.assertIn("main language is Arabic", messages[0]["content"])

    def test_truncated_or_null_content_is_a_chunk_failure(self):
        for reply in (self.reply(finish_reason="length"), self.reply(content=None), self.reply(content="  ")):
            with self.subTest(reply=reply):
                with patch("clean_with_Llama.requests.post", return_value=reply):
                    with self.assertRaises(cleaner.ChunkCleaningError):
                        cleaner.openrouter_generate("prompt")

    def test_payment_or_auth_errors_stop_the_provider(self):
        for status in (401, 402):
            with patch("clean_with_Llama.requests.post", return_value=self.reply(status=status)) as post:
                with self.assertRaises(cleaner.CloudCleanerUnavailable):
                    cleaner.openrouter_generate("prompt")
            post.assert_called_once()

    def test_transient_errors_are_retried(self):
        replies = [self.reply(status=503), self.reply(status=429), self.reply("نص سليم")]
        with patch("clean_with_Llama.requests.post", side_effect=replies) as post:
            self.assertEqual(cleaner.openrouter_generate("prompt"), "نص سليم")
        self.assertEqual(post.call_count, 3)

    def test_reasoning_option_is_dropped_when_the_model_rejects_it(self):
        replies = [self.reply(status=400), self.reply("نص")]
        with patch("clean_with_Llama.requests.post", side_effect=replies) as post:
            self.assertEqual(cleaner.openrouter_generate("prompt"), "نص")
        first, second = (call.kwargs["json"] for call in post.call_args_list)
        self.assertEqual(first["reasoning"], {"effort": "low", "exclude": True})
        self.assertNotIn("reasoning", second)
        # A 400 without the option is a failure of this chunk only.
        with patch("clean_with_Llama.requests.post", return_value=self.reply(status=400)):
            with self.assertRaises(cleaner.ChunkCleaningError):
                cleaner.openrouter_generate("prompt")

    def test_bad_numeric_settings_fall_back_to_defaults(self):
        with patch.dict(os.environ, {"OPENROUTER_MAX_TOKENS": "lots", "OPENROUTER_TIMEOUT_SECONDS": "-1"}):
            with patch("clean_with_Llama.requests.post", return_value=self.reply()) as post:
                cleaner.openrouter_generate("prompt")
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], cleaner.OPENROUTER_MAX_TOKENS)
        self.assertEqual(post.call_args.kwargs["timeout"], cleaner.OPENROUTER_TIMEOUT_SECONDS)


class PipelineCacheTests(unittest.TestCase):
    """process_youtube_url keys every cache by language, mode and format version."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(temporary.name)
        self.audio = Path("downloads") / f"{VIDEO_ID}.mp3"
        self.texts = {"ar": ARABIC_TEXT, "en": ENGLISH_TEXT, None: ARABIC_TEXT}
        self.download = patch.object(pipeline.url_to_mp3, "download_youtube_mp3", side_effect=self.fake_download)
        self.download_mock = self.download.start()
        self.addCleanup(self.download.stop)
        self.cloud = patch.object(pipeline, "transcribe_with_openrouter", side_effect=self.fake_cloud)
        self.cloud_mock = self.cloud.start()
        self.addCleanup(self.cloud.stop)
        environment = patch.dict(os.environ, {"WHISPER_BACKEND": "openrouter", "OLLAMA_LOCAL_FALLBACK": "false"})
        environment.start()
        self.addCleanup(environment.stop)

    def fake_download(self, url, skip_cache=False, return_metadata=False):
        self.audio.parent.mkdir(exist_ok=True)
        self.audio.write_bytes(b"audio")
        return str(self.audio), {"title": "محاضرة هياكل البيانات 🌎", "duration": 600, "video_id": VIDEO_ID}

    def fake_cloud(self, audio_path, *, language, video_duration_seconds=None, progress_callback=None):
        text = self.texts[language]
        return text, {
            "provider": "openrouter",
            "requested_language": language,
            "detected_language": tf.detect_text_language(text),
            "segments": [],
        }

    def run_pipeline(self, language, clean=False, **extra):
        return pipeline.process_youtube_url(URL, clean=clean, language=language, **extra)

    def test_result_carries_contract_fields_and_early_title(self):
        events = []
        result = self.run_pipeline(
            "ar", progress_callback=lambda stage, details: events.append((stage, details)),
            callback_url="https://example.test/hook",
        )
        for key, value in {
            "video_id": VIDEO_ID,
            "title": "محاضرة هياكل البيانات 🌎",
            "language": "ar",
            "detected_language": "ar",
            "mode": "fast",
            "requested_mode": "fast",
            "format_version": tf.FORMAT_VERSION,
            "video_duration_seconds": 600,
            "cache_key": f"{VIDEO_ID}:ar:fast:{tf.FORMAT_VERSION}",
        }.items():
            self.assertEqual(result[key], value, key)
        title_events = [details for stage, details in events if details.get("title")]
        self.assertEqual(title_events[0]["title"], "محاضرة هياكل البيانات 🌎")
        self.assertEqual(title_events[0]["video_duration_seconds"], 600)
        # File names never contain the title.
        for key in ("raw_transcript_path", "cleaned_transcript_path"):
            self.assertNotIn("محاضرة", result[key])
            self.assertTrue(Path(result[key]).is_file())
        self.assertFalse(self.audio.exists())

    def test_language_and_mode_keep_separate_caches(self):
        first = self.run_pipeline("ar")
        self.assertEqual(self.download_mock.call_count, 1)

        again = self.run_pipeline("ar")
        self.assertTrue(again["used_cached_cleaned_transcript"])
        self.assertEqual(again["title"], first["title"])
        self.assertEqual(again["detected_language"], "ar")
        self.assertEqual(self.download_mock.call_count, 1)

        english = self.run_pipeline("en")
        self.assertFalse(english["used_cached_cleaned_transcript"])
        self.assertEqual(self.download_mock.call_count, 2)
        self.assertEqual(english["detected_language"], "en")
        self.assertNotEqual(english["cleaned_transcript_path"], first["cleaned_transcript_path"])

        auto = self.run_pipeline("auto")
        self.assertEqual(auto["language"], "auto")
        self.assertFalse(auto["used_cached_cleaned_transcript"])
        self.assertIsNone(self.cloud_mock.call_args.kwargs["language"])
        self.assertEqual(auto["detected_language"], "ar")

        cache = json.loads(Path("transcript_cache.json").read_text(encoding="utf-8"))["videos"]
        self.assertEqual(
            sorted(cache),
            sorted(f"{VIDEO_ID}:{lang}:fast:{tf.FORMAT_VERSION}" for lang in ("ar", "en", "auto")),
        )

    def test_formatted_request_reuses_raw_but_not_fast_output(self):
        def fake_cleaner(raw_path, **kwargs):
            kwargs["output_path"].write_text("## عنوان\n\nنص", encoding="utf-8")
            return kwargs["output_path"]

        self.run_pipeline("ar", clean=False)
        with patch.object(pipeline, "clean_transcript_file_with_openrouter", side_effect=fake_cleaner) as cloud_cleaner:
            formatted = self.run_pipeline("ar", clean=True)
        self.assertEqual(self.download_mock.call_count, 1)
        self.assertTrue(formatted["used_cached_raw_transcript"])
        self.assertFalse(formatted["used_cached_cleaned_transcript"])
        self.assertEqual(formatted["mode"], "formatted")
        self.assertEqual(formatted["cleaner_provider"], "openrouter")
        self.assertEqual(formatted["title"], "محاضرة هياكل البيانات 🌎")
        self.assertEqual(cloud_cleaner.call_args.kwargs["language"], "ar")
        self.assertEqual(Path(formatted["cleaned_transcript_path"]).read_text(encoding="utf-8"), "## عنوان\n\nنص")

    def test_failed_formatting_is_cached_as_fast_not_formatted(self):
        with patch.object(pipeline, "clean_transcript_file_with_openrouter",
                          side_effect=cleaner.CloudCleanerUnavailable("OPENROUTER_API_KEY is not set.")):
            result = self.run_pipeline("ar", clean=True)
        self.assertEqual(result["mode"], "fast")
        self.assertEqual(result["requested_mode"], "formatted")
        self.assertIn("OPENROUTER_API_KEY", result["cleaner_error"])
        self.assertTrue(Path(result["cleaned_transcript_path"]).is_file())
        self.assertIsNone(pipeline.find_cached_result(URL, clean=True, language="ar"))
        self.assertIsNotNone(pipeline.find_cached_result(URL, clean=False, language="ar"))

    def test_provider_stopping_part_way_is_stored_as_fast(self):
        calls = {"count": 0}

        def openrouter(prompt, system=None):
            calls["count"] += 1
            if calls["count"] > 1:
                raise cleaner.CloudCleanerUnavailable("OpenRouter cleaner returned HTTP 402.")
            return echo_chunk(prompt)

        with patch.object(cleaner, "openrouter_generate", side_effect=openrouter), \
                patch.object(cleaner, "CLOUD_CHUNK_CHARS", 300):
            result = self.run_pipeline("ar", clean=True)
        self.assertEqual(result["cleaner_stats"]["llm_chunks"], 1)
        self.assertGreater(result["cleaner_stats"]["fallback_chunks"], 1)
        self.assertEqual(result["mode"], "fast")
        self.assertEqual(result["requested_mode"], "formatted")
        self.assertTrue(result["cleaner_partial"])
        self.assertIn("402", result["cleaner_error"])
        self.assertEqual(result["cache_key"], f"{VIDEO_ID}:ar:fast:{tf.FORMAT_VERSION}")
        self.assertEqual(Path(result["cleaned_transcript_path"]), pipeline.cleaned_output_path(VIDEO_ID, "ar", "fast"))
        # Nothing is left under the formatted name for cache recovery to revive.
        self.assertFalse(pipeline.cleaned_output_path(VIDEO_ID, "ar", "formatted").exists())
        cache = json.loads(Path("transcript_cache.json").read_text(encoding="utf-8"))["videos"]
        self.assertFalse(any(":formatted:" in key for key in cache))
        self.assertIsNone(pipeline.find_cached_result(URL, clean=True, language="ar"))

        # Once the provider works again, a formatted request formats the lecture.
        with patch.object(cleaner, "openrouter_generate", side_effect=lambda prompt, system=None: echo_chunk(prompt)), \
                patch.object(cleaner, "CLOUD_CHUNK_CHARS", 300):
            again = self.run_pipeline("ar", clean=True)
        self.assertFalse(again["used_cached_cleaned_transcript"])
        self.assertEqual(again["mode"], "formatted")
        self.assertIsNotNone(pipeline.find_cached_result(URL, clean=True, language="ar"))

    def test_isolated_rejected_chunk_keeps_formatted_mode(self):
        calls = {"count": 0}

        def openrouter(prompt, system=None):
            calls["count"] += 1
            # The first chunk is translated twice (rejected), every other one is fine.
            return english_of(prompt) if calls["count"] <= 2 else echo_chunk(prompt)

        with patch.object(cleaner, "openrouter_generate", side_effect=openrouter), \
                patch.object(cleaner, "CLOUD_CHUNK_CHARS", 300):
            result = self.run_pipeline("ar", clean=True)
        self.assertEqual(result["mode"], "formatted")
        self.assertTrue(result["cleaner_partial"])
        self.assertIsNone(result["cleaner_stats"]["stopped_reason"])

    def test_auto_request_cleans_in_the_detected_language(self):
        with patch.object(pipeline, "clean_transcript_with_preferred_model", return_value=(None, None, "off")) as preferred:
            result = self.run_pipeline(None, clean=True)
        self.assertEqual(result["language"], "auto")
        self.assertEqual(preferred.call_args.kwargs["language"], "ar")

    def test_legacy_outputs_are_never_rediscovered(self):
        Path("OutputForOllama").mkdir()
        Path("OutputForWhisper").mkdir()
        Path(f"OutputForOllama/{VIDEO_ID}_Lecture_transcript_cleanedv5.txt").write_text("English translation", encoding="utf-8")
        Path(f"OutputForWhisper/{VIDEO_ID}_Lecture_english_transcript.txt").write_text("English", encoding="utf-8")
        Path("transcript_cache.json").write_text(json.dumps({"videos": {VIDEO_ID: {
            "video_id": VIDEO_ID,
            "cleaned_transcript_path": f"OutputForOllama/{VIDEO_ID}_Lecture_transcript_cleanedv5.txt",
            "prompt_version": "original-language-v1",
        }}}), encoding="utf-8")
        self.assertIsNone(pipeline.find_cached_result(URL, clean=True, language="ar"))
        self.assertIsNone(pipeline.find_existing_raw_output(VIDEO_ID, "ar"))
        result = self.run_pipeline("ar")
        self.assertFalse(result["used_cached_cleaned_transcript"])
        self.assertFalse(result["used_cached_raw_transcript"])
        cache = json.loads(Path("transcript_cache.json").read_text(encoding="utf-8"))["videos"]
        self.assertNotIn(VIDEO_ID, cache)

    def test_current_files_are_rediscovered_when_the_cache_file_is_lost(self):
        first = self.run_pipeline("ar")
        Path("transcript_cache.json").unlink()
        again = self.run_pipeline("ar")
        self.assertTrue(again["used_cached_cleaned_transcript"])
        self.assertEqual(again["title"], first["title"])
        self.assertEqual(self.download_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
