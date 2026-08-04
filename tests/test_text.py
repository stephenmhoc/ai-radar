import unittest

from podcast_radar.text import clean_text, slugify, split_sentences, strip_html, truncate


class TruncateTests(unittest.TestCase):
    def test_short_values_pass_through(self) -> None:
        self.assertEqual(truncate("hello", 10), "hello")
        self.assertEqual(truncate("hello", 5), "hello")

    def test_truncated_value_never_exceeds_budget(self) -> None:
        value = "word " * 200

        for max_chars in (1, 5, 13, 14, 21, 100):
            with self.subTest(max_chars=max_chars):
                self.assertLessEqual(len(truncate(value, max_chars)), max_chars)

    def test_truncated_value_keeps_leading_content(self) -> None:
        value = "The guest explains post-training in detail. " * 20

        truncated = truncate(value, 60)

        self.assertTrue(truncated.startswith("The guest explains"))
        self.assertTrue(truncated.endswith("[truncated]"))

    def test_non_positive_budget_is_unlimited(self) -> None:
        self.assertEqual(truncate("hello", 0), "hello")
        self.assertEqual(truncate("hello", -3), "hello")


class TextHelperTests(unittest.TestCase):
    def test_strip_html_flattens_markup_and_entities(self) -> None:
        self.assertEqual(strip_html("<p>Sam &amp; Greg</p><p>talk</p>"), "Sam & Greg\n\ntalk")

    def test_clean_text_collapses_runs_of_blank_lines_and_spaces(self) -> None:
        self.assertEqual(clean_text("  a\r\n\r\n\r\n b  \t c "), "a\n\n b c")

    def test_slugify_falls_back_when_nothing_survives(self) -> None:
        self.assertEqual(slugify("Sam Altman: Models!"), "sam-altman-models")
        self.assertEqual(slugify("——", fallback="item"), "item")

    def test_split_sentences_keeps_abbreviations_intact(self) -> None:
        sentences = split_sentences("He joined the U.S. team. Then he left.")

        self.assertEqual(sentences, ["He joined the U.S. team.", "Then he left."])


if __name__ == "__main__":
    unittest.main()
