import pathlib
import unittest

from podcast_radar import config as config_module
from podcast_radar.config import load_config

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10
    tomllib = None


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class FallbackTOMLParserTests(unittest.TestCase):
    """The fallback parser runs unattended on Python 3.9 and 3.10.

    Nothing else would notice if it started mis-reading the real config, so it
    is pinned against tomllib on the file the service actually ships with.
    """

    @unittest.skipIf(tomllib is None, "tomllib is unavailable on this interpreter")
    def test_matches_tomllib_on_the_shipped_config(self) -> None:
        text = (REPO_ROOT / "config.toml").read_text(encoding="utf-8")

        self.assertEqual(config_module._parse_basic_toml(text), tomllib.loads(text))

    def test_parses_the_shapes_the_config_uses(self) -> None:
        parsed = config_module._parse_basic_toml(
            """
            # a leading comment
            [app]
            lookback_days = 14
            temperature = 0.1
            keep_audio = false
            user_agent = "ai-radar/0.1"  # trailing comment
            hash_note = "a # inside a string"

            [[labs]]
            name = "OpenAI"
            aliases = ["OpenAI"]
            people = [
              "Sam Altman",
              "Greg Brockman",
            ]

            [[labs]]
            name = "Anthropic"
            """
        )

        self.assertEqual(parsed["app"]["lookback_days"], 14)
        self.assertAlmostEqual(parsed["app"]["temperature"], 0.1)
        self.assertIs(parsed["app"]["keep_audio"], False)
        self.assertEqual(parsed["app"]["user_agent"], "ai-radar/0.1")
        self.assertEqual(parsed["app"]["hash_note"], "a # inside a string")
        self.assertEqual([lab["name"] for lab in parsed["labs"]], ["OpenAI", "Anthropic"])
        self.assertEqual(parsed["labs"][0]["people"], ["Sam Altman", "Greg Brockman"])


class LoadConfigTests(unittest.TestCase):
    def test_shipped_config_loads_with_resolved_paths(self) -> None:
        config = load_config(REPO_ROOT / "config.toml")

        self.assertTrue(config.app.database_path.is_absolute())
        self.assertTrue(config.app.public_dir.is_absolute())
        self.assertTrue(config.labs)
        self.assertTrue(config.active_sources)
        self.assertTrue(config.watched_people)

    def test_unsupported_source_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            config_module._source({"kind": "newsletter", "name": "X", "url": "https://example.com"})


if __name__ == "__main__":
    unittest.main()
