import unittest

from podcast_radar.feeds import parse_feed


RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Example AI Podcast</title>
    <link>https://example.com</link>
    <image><url>https://example.com/art.jpg</url></image>
    <item>
      <title>Sam Altman on models</title>
      <guid>episode-1</guid>
      <link>https://example.com/episode-1</link>
      <pubDate>Tue, 18 Jun 2024 10:00:00 GMT</pubDate>
      <description><![CDATA[<p>A conversation with Sam Altman.</p>]]></description>
      <enclosure url="https://cdn.example.com/episode-1.mp3" type="audio/mpeg" />
      <itunes:duration>01:02:03</itunes:duration>
    </item>
  </channel>
</rss>
"""


class FeedParsingTests(unittest.TestCase):
    def test_parse_rss_podcast_item(self) -> None:
        parsed = parse_feed(RSS, "Fallback", ("Host One",))

        self.assertEqual(parsed.title, "Example AI Podcast")
        self.assertEqual(parsed.homepage_url, "https://example.com")
        self.assertEqual(parsed.image_url, "https://example.com/art.jpg")
        self.assertEqual(len(parsed.episodes), 1)
        episode = parsed.episodes[0]
        self.assertEqual(episode["guid"], "episode-1")
        self.assertEqual(episode["title"], "Sam Altman on models")
        self.assertEqual(episode["audio_url"], "https://cdn.example.com/episode-1.mp3")
        self.assertEqual(episode["description"], "A conversation with Sam Altman.")
        self.assertEqual(episode["hosts"], ["Host One"])
        self.assertEqual(episode["published_at"], "2024-06-18T10:00:00+00:00")


if __name__ == "__main__":
    unittest.main()

