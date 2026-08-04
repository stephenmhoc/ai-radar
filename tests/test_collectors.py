import os
import pathlib
import sqlite3
import subprocess
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
          <entry>
            <id>yt:video:short-1</id>
            <title>A short promotional clip</title>
            <link rel="alternate" href="https://www.youtube.com/shorts/short-1"/>
            <published>2026-08-01T13:00:00Z</published>
          </entry>
        </feed>"""

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "podcast_radar.collectors.feeds.fetch_feed", return_value=xml
        ):
            appearances, metadata = collectors._collect_youtube(_config(source), source)

        self.assertEqual(len(appearances), 1)
        self.assertEqual(appearances[0]["external_id"], "video-1")
        self.assertEqual(appearances[0]["media_type"], "video/youtube")
        self.assertEqual(appearances[0]["description"], "Detailed public-feed description.")
        self.assertEqual(appearances[0]["authors"], ["Example Channel"])
        self.assertEqual(metadata["homepage_url"], "https://www.youtube.com/@example")

    def test_youtube_playlist_collector_combines_fast_history_with_recent_feed(self) -> None:
        source = SourceConfig(
            kind="youtube",
            name="Example Podcast — YouTube",
            url="https://www.youtube.com/@example",
            external_id="channel-1",
            playlist_url="https://www.youtube.com/playlist?list=podcast-1",
        )
        video = {
            "id": "video-1",
            "title": "A substantial AI conversation",
            "webpage_url": "https://www.youtube.com/watch?v=video-1",
            "channel": "Example Channel",
            "channel_id": "channel-1",
            "playlist_id": "podcast-1",
            "duration": 3723,
            "thumbnail": "https://img/video.jpg",
        }
        recent = {
            "external_id": "video-1",
            "title": "A substantial AI conversation",
            "description": "The full episode description.",
            "url": "https://www.youtube.com/watch?v=video-1",
            "media_url": "https://www.youtube.com/watch?v=video-1",
            "media_type": "video/youtube",
            "published_at": "2026-08-01T12:00:00+00:00",
            "authors": ["Example Channel"],
            "raw": {"video_id": "video-1"},
        }
        completed = subprocess.CompletedProcess(
            args=["yt-dlp"], returncode=0, stdout=f"{collectors.json.dumps(video)}\n", stderr=""
        )

        with mock.patch("podcast_radar.collectors.shutil.which", return_value="/opt/bin/yt-dlp"), mock.patch(
            "podcast_radar.collectors.subprocess.run", return_value=completed
        ) as run, mock.patch("podcast_radar.collectors._collect_youtube_feed", return_value=([recent], {})):
            appearances, metadata = collectors._collect_youtube(
                _config(source),
                source,
                since=collectors.dt.datetime(2026, 1, 1, tzinfo=collectors.dt.timezone.utc),
            )

        command = run.call_args.args[0]
        self.assertIn("--flat-playlist", command)
        self.assertNotIn("--dateafter", command)
        self.assertEqual(appearances[0]["external_id"], "video-1")
        self.assertEqual(appearances[0]["duration"], "01:02:03")
        self.assertEqual(appearances[0]["media_type"], "video/youtube")
        self.assertEqual(appearances[0]["authors"], ["Example Channel"])
        self.assertEqual(metadata["cursor"], "video-1")

    def test_youtube_playlist_collector_rejects_failed_empty_listing(self) -> None:
        source = SourceConfig(
            kind="youtube",
            name="Example Podcast — YouTube",
            url="https://www.youtube.com/@example",
            playlist_url="https://www.youtube.com/@example/videos",
        )
        completed = subprocess.CompletedProcess(args=["yt-dlp"], returncode=1, stdout="", stderr="unavailable")

        with mock.patch("podcast_radar.collectors.shutil.which", return_value="/opt/bin/yt-dlp"), mock.patch(
            "podcast_radar.collectors.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                collectors._collect_youtube(
                    _config(source),
                    source,
                    since=collectors.dt.datetime(2026, 8, 1, tzinfo=collectors.dt.timezone.utc),
                )

    def test_playlist_match_uses_same_show_date_and_duration_when_titles_differ(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        podcast_source = SourceConfig(
            kind="podcast",
            name="Invest Like the Best",
            url="https://example.com/feed.xml",
        )
        source_id = storage.upsert_source(conn, podcast_source)
        item_id, _ = storage.upsert_appearance(
            conn,
            source_id,
            {
                "external_id": "pod-1",
                "title": "Sam Altman - How to Make an Abundant Future - [Invest Like the Best, EP.484]",
                "url": "https://example.com/pod-1",
                "published_at": "2026-07-28T08:00:00+00:00",
                "duration": "53:33",
            },
        )
        youtube_source = SourceConfig(
            kind="youtube",
            name="Invest Like the Best — YouTube",
            url="https://youtube.com/@example",
        )

        match = collectors._matching_podcast_appearance(
            conn,
            youtube_source,
            {"title": "Sam Altman on AGI, Compute, and Human Agency", "duration": 3348},
            published_at="2026-07-28T12:00:00+00:00",
        )

        self.assertIsNotNone(match)
        self.assertEqual(match["item_id"], item_id)

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
