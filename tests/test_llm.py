import dataclasses
import io
import pathlib
import unittest
import urllib.error
from unittest import mock

from podcast_radar.config import AppConfig, Config, FeedConfig, LLMConfig, LabConfig, SiteConfig, TranscriptionConfig
from podcast_radar import llm
from podcast_radar.llm import (
    LLMError,
    _confidence,
    _normalize_judge,
    _normalize_summary,
    build_summary_prompt,
    extract_json,
)


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

    def test_judge_allows_explicit_roster_investor(self) -> None:
        result = _normalize_judge(
            {
                "include": True,
                "confidence": 0.9,
                "labs": ["Atreides"],
                "matched_people": ["Gavin Baker"],
                "guest_names": ["Gavin Baker"],
                "reason": "Gavin Baker is the episode guest.",
            },
            _config(),
        )

        self.assertTrue(result["include"])
        self.assertEqual(result["labs"], ["Atreides Management"])
        self.assertEqual(result["matched_people"], ["Gavin Baker"])

    def test_judge_allows_substantial_physical_ai_without_named_guest(self) -> None:
        config = dataclasses.replace(
            _config(),
            labs=_config().labs + (LabConfig(name="Physical AI", aliases=("Physical AI",)),),
        )
        result = _normalize_judge(
            {
                "include": True,
                "confidence": 0.9,
                "labs": ["Physical AI"],
                "matched_people": [],
                "guest_names": [],
                "reason": "The item is about deploying AI-controlled industrial robots.",
            },
            config,
        )

        self.assertTrue(result["include"])
        self.assertEqual(result["labs"], ["Physical AI"])
        self.assertEqual(result["matched_people"], [])

    def test_confidence_survives_unusable_model_values(self) -> None:
        self.assertEqual(_confidence(0.75), 0.75)
        self.assertEqual(_confidence("0.75"), 0.75)
        self.assertEqual(_confidence("high"), 0.0)
        self.assertEqual(_confidence(None), 0.0)
        self.assertEqual(_confidence(True), 0.0)
        self.assertEqual(_confidence(float("nan")), 0.0)
        self.assertEqual(_confidence(90), 1.0)
        self.assertEqual(_confidence(-2), 0.0)

    def test_unparseable_confidence_does_not_discard_the_judgement(self) -> None:
        result = _normalize_judge(
            {
                "include": True,
                "confidence": "very high",
                "labs": ["OpenAI"],
                "matched_people": ["Sam Altman"],
                "guest_names": ["Sam Altman"],
                "reason": "Sam Altman is the guest.",
            },
            _config(),
        )

        self.assertTrue(result["include"])
        self.assertEqual(result["confidence"], 0.0)

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

class LLMRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.slept: list[float] = []
        sleep_patch = mock.patch.object(llm.time, "sleep", self.slept.append)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _run(self, responses: list[object], *, llm_config: LLMConfig | None = None) -> dict:
        self.attempts = 0

        def fake_urlopen(request, timeout=None):
            self.attempts += 1
            outcome = responses[self.attempts - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResponse(outcome)

        with mock.patch.object(llm.urllib.request, "urlopen", fake_urlopen):
            return llm._raw_request_json(
                "https://llm.example/v1/chat/completions",
                {"model": "test"},
                {"Content-Type": "application/json"},
                llm=llm_config or LLMConfig(),
            )

    def test_rate_limit_is_retried_until_it_succeeds(self) -> None:
        result = self._run([_http_error(429), _http_error(503), '{"ok": true}'])

        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.attempts, 3)
        self.assertEqual(self.slept, [2.0, 4.0])

    def test_retry_after_header_overrides_backoff(self) -> None:
        result = self._run([_http_error(429, retry_after="7"), '{"ok": true}'])

        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.slept, [7.0])

    def test_retry_after_is_capped(self) -> None:
        self._run([_http_error(429, retry_after="9000"), '{"ok": true}'])

        self.assertEqual(self.slept, [LLMConfig().max_retry_sleep_seconds])

    def test_client_errors_fail_immediately(self) -> None:
        with self.assertRaises(LLMError) as caught:
            self._run([_http_error(401), '{"ok": true}'])

        self.assertIn("HTTP 401", str(caught.exception))
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.slept, [])

    def test_network_errors_are_retried_then_surfaced(self) -> None:
        error = urllib.error.URLError("connection reset")

        with self.assertRaises(LLMError) as caught:
            self._run([error, error], llm_config=LLMConfig(max_attempts=2))

        self.assertIn("connection reset", str(caught.exception))
        self.assertEqual(self.attempts, 2)
        self.assertEqual(self.slept, [2.0])

    def test_retries_can_be_disabled(self) -> None:
        with self.assertRaises(LLMError):
            self._run([_http_error(429), '{"ok": true}'], llm_config=LLMConfig(max_attempts=1))

        self.assertEqual(self.attempts, 1)


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError(
        "https://llm.example/v1/chat/completions",
        code,
        "error",
        headers,
        io.BytesIO(b'{"error": "upstream"}'),
    )


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
            LabConfig(name="Atreides Management", aliases=("Atreides",), people=("Gavin Baker",)),
        ),
    )


if __name__ == "__main__":
    unittest.main()
