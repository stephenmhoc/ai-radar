import unittest

from podcast_radar import net


class RequireHttpURLTests(unittest.TestCase):
    def test_http_and_https_are_allowed(self) -> None:
        self.assertEqual(net.require_http_url("https://example.com/a"), "https://example.com/a")
        self.assertEqual(net.require_http_url("  http://example.com/a  "), "http://example.com/a")

    def test_local_and_exotic_schemes_are_refused(self) -> None:
        for url in (
            "file:///etc/passwd",
            "FILE:///etc/passwd",
            "ftp://example.com/audio.mp3",
            "data:text/html;base64,PHA+aGk8L3A+",
            "",
            "   ",
        ):
            with self.subTest(url=url):
                with self.assertRaises(net.UnsupportedURLError):
                    net.require_http_url(url, purpose="article fetch")

    def test_refusal_names_the_purpose(self) -> None:
        with self.assertRaises(net.UnsupportedURLError) as caught:
            net.require_http_url("file:///etc/passwd", purpose="audio download")

        self.assertIn("audio download", str(caught.exception))


class CollectorFetchTests(unittest.TestCase):
    def test_article_fetch_refuses_a_local_file_url(self) -> None:
        from podcast_radar import collectors

        with self.assertRaises(net.UnsupportedURLError):
            collectors._fetch_html("file:///etc/passwd", user_agent="ai-radar/test")

    def test_article_text_falls_back_when_the_link_is_not_fetchable(self) -> None:
        from podcast_radar import collectors

        text = collectors._article_text(
            "file:///etc/passwd",
            fallback="<p>The feed description survives.</p>",
            user_agent="ai-radar/test",
        )

        self.assertEqual(text, "The feed description survives.")


if __name__ == "__main__":
    unittest.main()
