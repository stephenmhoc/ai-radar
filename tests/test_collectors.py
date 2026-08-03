import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from podcast_radar import collectors, storage
from podcast_radar.config import (
    AppConfig,
    Config,
    LLMConfig,
    SiteConfig,
    SourceConfig,
    TranscriptionConfig,
)


class CollectorTests(unittest.TestCase):
    def test_youtube_collector_normalizes_video_appearance(self) -> None:
        source = SourceConfig(
            kind="youtube",
            name="Example Channel",
            url="https://www.youtube.com/channel/channel-1",
            external_id="channel-1",
            people=("Sam Altman",),
            api_key_env="TEST_YOUTUBE_KEY",
        )
        responses = [
            {
                "items": [
                    {
                        "snippet": {"thumbnails": {"high": {"url": "https://img/channel.jpg"}}},
                        "contentDetails": {"relatedPlaylists": {"uploads": "uploads-1"}},
                    }
                ]
            },
            {"items": [{"contentDetails": {"videoId": "video-1"}}]},
            {
                "items": [
                    {
                        "id": "video-1",
                        "status": {"privacyStatus": "public"},
                        "snippet": {
                            "title": "Sam Altman on models",
                            "description": "A substantial conversation.",
                            "publishedAt": "2026-08-01T12:00:00Z",
                            "liveBroadcastContent": "none",
                            "thumbnails": {"high": {"url": "https://img/video.jpg"}},
                        },
                        "contentDetails": {"duration": "PT1H2M3S"},
                    }
                ]
            },
        ]

        with mock.patch.dict(os.environ, {"TEST_YOUTUBE_KEY": "key"}), mock.patch(
            "podcast_radar.collectors._get_json", side_effect=responses
        ):
            appearances, metadata = collectors._collect_youtube(_config(source), source)

        self.assertEqual(len(appearances), 1)
        self.assertEqual(appearances[0]["external_id"], "video-1")
        self.assertEqual(appearances[0]["media_type"], "video/youtube")
        self.assertEqual(appearances[0]["duration"], "01:02:03")
        self.assertEqual(appearances[0]["authors"], ["Sam Altman"])
        self.assertEqual(metadata["cursor"], "video-1")

    def test_youtube_collector_uses_public_feed_without_api_key(self) -> None:
        source = SourceConfig(
            kind="youtube",
            name="Example Channel",
            url="https://www.youtube.com/@example",
            external_id="channel-1",
            feed_url="https://www.youtube.com/feeds/videos.xml?channel_id=channel-1",
        )
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
          <title>Example Channel</title>
          <link rel="alternate" href="https://www.youtube.com/@example"/>
          <entry>
            <id>yt:video:video-1</id>
            <title>A substantial AI conversation</title>
            <link rel="alternate" href="https://www.youtube.com/watch?v=video-1"/>
            <published>2026-08-01T12:00:00Z</published>
            <media:group>
              <media:thumbnail url="https://img/video.jpg"/>
              <media:description>Detailed public-feed description.</media:description>
            </media:group>
          </entry>
        </feed>"""

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "podcast_radar.collectors.feeds.fetch_feed", return_value=xml
        ):
            appearances, metadata = collectors._collect_youtube(_config(source), source)

        self.assertEqual(appearances[0]["external_id"], "video-1")
        self.assertEqual(appearances[0]["media_type"], "video/youtube")
        self.assertEqual(appearances[0]["description"], "Detailed public-feed description.")
        self.assertEqual(appearances[0]["authors"], ["Example Channel"])
        self.assertEqual(metadata["homepage_url"], "https://www.youtube.com/@example")

    def test_x_collector_combines_same_author_thread_and_drops_short_post(self) -> None:
        source = SourceConfig(
            kind="x",
            name="Sam Altman",
            url="https://x.com/sama",
            external_id="42",
            people=("Sam Altman",),
            api_key_env="TEST_X_KEY",
        )
        response = {
            "data": [
                {
                    "id": "100",
                    "author_id": "42",
                    "conversation_id": "100",
                    "created_at": "2026-08-01T12:00:00Z",
                    "text": "First part. " + ("Important model details. " * 20),
                    "referenced_tweets": [],
                },
                {
                    "id": "101",
                    "author_id": "42",
                    "conversation_id": "100",
                    "created_at": "2026-08-01T12:01:00Z",
                    "note_tweet": {"text": "Second part. " + ("Evaluation evidence. " * 15)},
                    "referenced_tweets": [{"type": "replied_to", "id": "100"}],
                },
                {
                    "id": "102",
                    "author_id": "42",
                    "conversation_id": "102",
                    "created_at": "2026-08-01T13:00:00Z",
                    "text": "Thanks!",
                    "referenced_tweets": [],
                },
            ]
        }

        with mock.patch.dict(os.environ, {"TEST_X_KEY": "key"}), mock.patch(
            "podcast_radar.collectors._get_json", return_value=response
        ):
            appearances, metadata = collectors._collect_x(_config(source), source, cursor="")

        self.assertEqual(len(appearances), 1)
        self.assertEqual(appearances[0]["external_id"], "100")
        self.assertEqual(appearances[0]["raw"]["post_count"], 2)
        self.assertIn("Second part", appearances[0]["content_text"])
        self.assertEqual(metadata["cursor"], "102")

    def test_article_parser_prefers_main_article_content(self) -> None:
        parser = collectors._ArticleParser()
        parser.feed(
            "<html><nav><p>This navigation sentence should be ignored completely.</p></nav>"
            "<article><h1>A useful title for the article</h1>"
            "<p>This is the substantial article paragraph containing technical details.</p></article>"
            "<footer><p>This footer sentence should also be ignored completely.</p></footer></html>"
        )

        text = parser.text()

        self.assertIn("substantial article paragraph", text)
        self.assertNotIn("navigation", text)
        self.assertNotIn("footer", text)

    def test_feedless_blog_collector_discovers_and_normalizes_articles(self) -> None:
        source = SourceConfig(
            kind="blog",
            name="Dario Amodei",
            url="https://example.com/",
            people=("Dario Amodei",),
        )
        homepage = """
        <html><head><meta property="og:image" content="https://example.com/dario.jpg"></head><body>
          <a href="/essay/a-useful-essay">Useful essay</a>
          <a href="/essay/a-useful-essay?ref=home#top">Duplicate</a>
          <a href="https://elsewhere.example/post/not-ours">External</a>
          <a href="/about">About</a>
        </body></html>
        """
        article = """
        <html><head><title>Dario Amodei —&nbsp;A Useful Essay</title></head><body>
          <main><h1>A Useful Essay</h1><p>January 2026</p>
          <p>This is a substantial article paragraph about model development and its consequences.</p></main>
        </body></html>
        """

        with mock.patch(
            "podcast_radar.collectors._fetch_html", side_effect=[homepage, article]
        ):
            appearances, metadata = collectors._collect_blog_index(_config(source), source)

        self.assertEqual(len(appearances), 1)
        self.assertEqual(appearances[0]["external_id"], "https://example.com/essay/a-useful-essay")
        self.assertEqual(appearances[0]["title"], "A Useful Essay")
        self.assertEqual(appearances[0]["published_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(appearances[0]["media_type"], "text/html")
        self.assertEqual(appearances[0]["authors"], ["Dario Amodei"])
        self.assertIn("model development", appearances[0]["content_text"])
        self.assertEqual(metadata["image_url"], "https://example.com/dario.jpg")

    def test_collect_source_routes_feedless_blog_to_index_collector(self) -> None:
        source = SourceConfig(kind="blog", name="Example", url="https://example.com/")
        with mock.patch(
            "podcast_radar.collectors._collect_blog_index", return_value=([], {"homepage_url": source.url})
        ) as collect_index:
            appearances, metadata = collectors.collect_source(_config(source), None, 1, source)

        self.assertEqual(appearances, [])
        self.assertEqual(metadata["homepage_url"], source.url)
        collect_index.assert_called_once()


def _config(source: SourceConfig) -> Config:
    root = pathlib.Path(tempfile.gettempdir()) / "ai-radar-collector-tests"
    return Config(
        root=root,
        app=AppConfig(database_path=root / "radar.sqlite3", public_dir=root / "public", state_dir=root),
        llm=LLMConfig(),
        transcription=TranscriptionConfig(),
        site=SiteConfig(),
        feeds=(),
        labs=(),
        sources=(source,),
    )


if __name__ == "__main__":
    unittest.main()
