from unittest import TestCase

import clean_with_Llama as cleaner
from clean_with_Llama import normalize_markdown_layout


class StudyLayoutRulesTests(TestCase):
    def test_rules_ask_for_study_structure_without_dropping_content(self):
        rules = cleaner.build_system_rules("ar")
        for expected in ('"### "', '"- " line', '"1. " numbered', "**term**: definition", "**bold**", "deleting content is not"):
            self.assertIn(expected, rules)
        self.assertNotIn("Prose stays prose", rules)
        self.assertIn("Reproduce the transcript in FULL", rules)

    def test_sub_headings_and_definitions_survive_layout_normalisation(self):
        text = "## طبقة النقل\n\n### بروتوكول TCP\n\n**المنفذ**: رقم يحدد التطبيق.\n\n- النوع الأول\n- النوع الثاني"
        out = normalize_markdown_layout(text)
        self.assertIn("### بروتوكول TCP", out)
        self.assertIn("**المنفذ**: رقم يحدد التطبيق.", out)
        self.assertIn("- النوع الثاني", out)
