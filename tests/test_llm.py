import pathlib
import unittest

from podcast_radar.config import AppConfig, Config, FeedConfig, LLMConfig, LabConfig, SiteConfig, TranscriptionConfig
from podcast_radar.llm import _normalize_judge, extract_json


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
