import unittest

from podcast_radar.llm import extract_json


class LLMTests(unittest.TestCase):
    def test_extract_json_from_wrapped_response(self) -> None:
        parsed = extract_json('Here is the result:\n{"include": true, "confidence": 0.9}\nThanks')
        self.assertEqual(parsed["include"], True)
        self.assertAlmostEqual(parsed["confidence"], 0.9)


if __name__ == "__main__":
    unittest.main()

