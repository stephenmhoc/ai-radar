import pathlib
import unittest

from podcast_radar.config import AppConfig, Config, FeedConfig, LLMConfig, LabConfig, SiteConfig, TranscriptionConfig
from podcast_radar.llm import _normalize_judge, _normalize_summary, build_summary_prompt, extract_json


class LLMTests(unittest.TestCase):
    def test_extract_json_from_wrapped_response(self) -> None:
        parsed = extract_json('Here is the result:\n{"include": true, "confidence": 0.9}\nThanks')
        self.assertEqual(parsed["include"], True)
        self.assertAlmostEqual(parsed["confidence"], 0.9)

    def test_judge_requires_configured_target_lab(self) -> None:
        result = _normalize_judge(
            {
                "include": True,
                "confidence": 0.9,
                "labs": ["Intel"],
                "matched_people": ["Lip-Bu Tan"],
                "guest_names": ["Lip-Bu Tan"],
                "reason": "Intel CEO",
            },
            _config(),
        )
        self.assertFalse(result["include"])
        self.assertEqual(result["labs"], [])
        self.assertEqual(result["matched_people"], [])
        self.assertIn("No configured target-lab guest affiliation", result["reason"])

    def test_summary_canonicalizes_labs_to_target_aliases(self) -> None:
        result = _normalize_judge(
            {
                "include": True,
                "confidence": 0.9,
                "labs": ["Google", "Microsoft"],
                "matched_people": ["Guest"],
                "guest_names": ["Guest"],
                "reason": "Guest works at Google",
            },
            _config(),
        )
        self.assertEqual(result["labs"], ["Google DeepMind"])

    def test_judge_rejects_target_person_who_is_only_mentioned(self) -> None:
        result = _normalize_judge(
            {
                "include": True,
                "confidence": 0.9,
                "labs": ["OpenAI"],
                "matched_people": ["Sam Altman"],
                "guest_names": ["Lip-Bu Tan"],
                "reason": "Sam Altman was discussed.",
            },
            _config(),
        )
        self.assertFalse(result["include"])
        self.assertEqual(result["labs"], [])
        self.assertEqual(result["matched_people"], [])
        self.assertIn("No qualifying target-lab guest was named", result["reason"])

    def test_summary_prompt_requests_long_and_short_summaries(self) -> None:
        prompt = build_summary_prompt(
            _config(),
            {
                "feed_name": "Example",
                "title": "Model behavior",
                "episode_url": "https://example.com/episode",
                "guests_json": '["Guest"]',
                "labs_json": '["OpenAI"]',
                "transcript_text": "Guest discusses model behavior.",
            },
        )

        self.assertIn("summary: 2-4 concise paragraphs", prompt["user"])
        self.assertIn("short_summary: one complete sentence", prompt["user"])
        self.assertIn("roughly 80 characters", prompt["user"])

    def test_normalize_summary_keeps_short_summary_bounded(self) -> None:
        result = _normalize_summary(
            {
                "title": "Noam Brown on benchmarks",
                "summary": "A long summary paragraph.",
                "short_summary": (
                    "Traditional benchmark grids miss test-time compute; GPT-5.5 looked small on paper "
                    "but better in practice."
                ),
                "key_points": ["A much longer point that should not be needed."],
            }
        )

        self.assertEqual(result["short_summary"], "Traditional benchmark grids miss test-time compute.")
        self.assertLessEqual(len(result["short_summary"]), 120)

def _config() -> Config:
    return Config(
        root=pathlib.Path("."),
        app=AppConfig(),
        llm=LLMConfig(),
        transcription=TranscriptionConfig(),
        site=SiteConfig(),
        feeds=(FeedConfig(name="Example", url="https://example.com/feed"),),
        labs=(
            LabConfig(name="OpenAI", aliases=("OpenAI",), people=("Sam Altman",)),
            LabConfig(name="Google DeepMind", aliases=("Google", "Google DeepMind", "DeepMind"), people=("Jeff Dean",)),
            LabConfig(name="NVIDIA", aliases=("NVIDIA", "Nvidia"), people=("Jensen Huang",)),
        ),
    )


if __name__ == "__main__":
    unittest.main()
