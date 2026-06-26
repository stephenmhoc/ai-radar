import unittest

from podcast_radar.transcriber import _episode_prompt_context


class TranscriberTests(unittest.TestCase):
    def test_episode_prompt_context_includes_metadata(self) -> None:
        context = _episode_prompt_context(
            {
                "feed_name": "Example Podcast",
                "title": "An AI Episode",
                "hosts_json": '["Host One", "Host Two"]',
                "description": "<p>Guest from OpenAI talks about model behavior.</p>",
            }
        )

        self.assertEqual(context["feed_name"], "Example Podcast")
        self.assertEqual(context["episode_title"], "An AI Episode")
        self.assertEqual(context["episode_hosts"], "Host One, Host Two")
        self.assertIn("Guest from OpenAI", context["episode_description"])


if __name__ == "__main__":
    unittest.main()
