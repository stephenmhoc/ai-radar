import pathlib
import tempfile
import unittest
import xml.etree.ElementTree as ET

from podcast_radar.config import (
    AppConfig,
    Config,
    FeedConfig,
    LLMConfig,
    LabConfig,
    SiteConfig,
    TranscriptionConfig,
)
from podcast_radar import site, storage


class SiteGenerationTests(unittest.TestCase):
    def test_compact_sentence_never_cuts_mid_thought(self) -> None:
        value = (
            "Noam Brown: Traditional single-number benchmark scores are misleading because they don't "
            "account for test-time compute; GPT-5.5 looked like a small improvement on paper but was "
            "substantially better in practice due to more efficient reasoning"
        )

        compacted = site._compact_sentence(value, max_chars=92)

        self.assertEqual(
            compacted,
            "Noam Brown: Traditional single-number benchmark scores are misleading because they don't "
            "account for test-time compute.",
        )

    def test_soft_sentence_limits_do_not_slice_unpunctuated_text(self) -> None:
        value = (
            "Traditional single-number benchmark scores are misleading because they don't account for "
            "test-time compute and this unpunctuated thought should stay intact"
        )

        self.assertEqual(site._compact_sentence(value, max_chars=72), value)
        self.assertEqual(site._first_sentence(value), value)

    def test_build_site_and_rss_from_published_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = Config(
                root=root,
                app=AppConfig(
                    database_path=root / "var" / "radar.sqlite3",
                    public_dir=root / "public",
                    state_dir=root / "var",
                ),
                llm=LLMConfig(),
                transcription=TranscriptionConfig(
                    audio_dir=root / "var" / "audio",
                    transcript_dir=root / "var" / "transcripts",
                ),
                site=SiteConfig(base_url="https://radar.example.com", cname="radar.example.com"),
                feeds=(FeedConfig(name="Example", url="https://example.com/feed", hosts=("Host One",)),),
                labs=(LabConfig(name="OpenAI", people=("Sam Altman",)),),
            )
            with storage.connect(config) as conn:
                feed_id = storage.upsert_feed(conn, config.feeds[0])
                episode_id, _ = storage.upsert_episode(
                    conn,
                    feed_id,
                    {
                        "guid": "episode-1",
                        "title": "Sam Altman on models",
                        "description": "A useful episode",
                        "episode_url": "https://example.com/episode-1",
                        "audio_url": "https://cdn.example.com/episode-1.mp3",
                        "audio_type": "audio/mpeg",
                        "image_url": "https://example.com/art.jpg",
                        "published_at": "2024-06-18T10:00:00+00:00",
                        "duration": "01:00:00",
                        "hosts": ["Host One"],
                        "raw": {},
                    },
                )
                storage.set_judgement(
                    conn,
                    episode_id,
                    {
                        "include": True,
                        "confidence": 0.95,
                        "labs": ["OpenAI"],
                        "matched_people": ["Sam Altman"],
                        "guest_names": ["Sam Altman"],
                        "reason": "OpenAI executive guest",
                    },
                )
                storage.set_transcript(
                    conn,
                    episode_id,
                    "Dr. Sam introduced the model\nacross wrapped transcript lines. It changed the eval terms. We talked next.",
                    root / "transcript.txt",
                )
                storage.set_summary(
                    conn,
                    episode_id,
                    {
                        "title": "Sam Altman on models",
                        "summary": "Summary text",
                        "summary_html": "<p>Summary text</p>",
                        "key_points": ["Point one"],
                        "topics": ["models"],
                        "hosts": ["Host One"],
                        "guests": ["Sam Altman"],
                        "labs": ["OpenAI"],
                    },
                )
                result = site.build_site(config, conn)

            self.assertEqual(result["episodes"], 1)
            self.assertEqual(result["rss_items"], 1)
            self.assertTrue((root / "public" / "index.html").exists())
            self.assertTrue((root / "public" / "feed.xml").exists())
            self.assertTrue((root / "public" / "sitemap.xml").exists())
            self.assertTrue(any((root / "public" / "assets").glob("style-*.css")))
            self.assertEqual((root / "public" / "CNAME").read_text().strip(), "radar.example.com")
            robots = (root / "public" / "robots.txt").read_text()
            self.assertIn("Sitemap: https://radar.example.com/sitemap.xml", robots)
            index = (root / "public" / "index.html").read_text()
            self.assertIn('/assets/style-', index)
            self.assertNotIn('href="/assets/style.css"', index)
            self.assertIn('<link rel="canonical" href="https://radar.example.com/">', index)
            self.assertIn('<meta property="og:type" content="website">', index)
            self.assertIn('<meta property="og:title" content="AI Radar">', index)
            self.assertIn('<meta name="twitter:card" content="summary_large_image">', index)
            self.assertIn('type="application/ld+json"', index)
            self.assertIn('"@type":"CollectionPage"', index)
            self.assertIn('"hasPart":[{"@type":"PodcastEpisode"', index)
            self.assertIn('class="skip-link" href="#main-content"', index)
            self.assertIn('<main id="main-content" tabindex="-1" aria-labelledby="episode-list-title">', index)
            self.assertIn('class="visually-hidden" id="episode-list-title"', index)
            self.assertIn("Sam Altman", index)
            self.assertIn("data-filter=\"openai\"", index)
            self.assertIn("data-labs=\"openai\"", index)
            self.assertIn('data-search-input aria-controls="episode-list"', index)
            self.assertIn("data-result-count", index)
            self.assertIn('role="status" aria-live="polite"', index)
            self.assertIn("data-pagination", index)
            self.assertIn("data-page-next", index)
            self.assertIn('aria-label="Next page"', index)
            self.assertIn("Use our RSS feed to stay up to date", index)
            self.assertIn('class="follow-link"', index)
            self.assertIn('href="https://radar.example.com/feed.xml"', index)
            self.assertIn('data-copy-url="https://radar.example.com/feed.xml"', index)
            self.assertIn('data-copy-status role="status" aria-live="polite"', index)
            self.assertIn('aria-label="Show OpenAI episodes, 1 episodes"', index)
            self.assertIn('class="content-grid"', index)
            self.assertIn('class="side-rail"', index)
            self.assertIn('<a class="episode-card"', index)
            self.assertNotIn("data-topic-filter", index)
            self.assertNotIn("data-feed-filter", index)
            self.assertNotIn("data-sort", index)
            self.assertNotIn("rss-text-link", index)
            self.assertNotIn('class="briefing"', index)
            self.assertNotIn("briefings", index.lower())
            self.assertIn("Sam Altman: Point one", index)
            self.assertIn('<p class="source-line">', index)
            self.assertIn('<span class="source-main"><strong>Example</strong><span class="source-title"> · Sam Altman on models</span></span>', index)
            self.assertIn('<span class="source-details">2024-06-18 · 1h 00m</span>', index)
            self.assertIn("Example · Sam Altman on models · 1h 00m", index)
            self.assertIn('class="rail-card newest-card"', index)
            self.assertNotIn("Episode details", index)
            self.assertNotIn("Verified brief", index)
            self.assertNotIn('<p class="summary">', index)
            self.assertNotIn("Summary and transcript ready", index)
            rss = (root / "public" / "feed.xml").read_text()
            rss_root = ET.fromstring(rss)
            self.assertIn("https://radar.example.com/episodes/", rss)
            self.assertIn("<title>Sam Altman on models</title>", rss)
            rss_description = rss_root.findtext("./channel/item/description") or ""
            self.assertIn("Summary text", rss_description)
            self.assertIn("Original podcast: https://example.com/episode-1", rss_description)
            self.assertIn("AI Radar page: https://radar.example.com/episodes/", rss_description)
            self.assertIn("<pubDate>Tue, 18 Jun 2024 10:00:00 GMT</pubDate>", rss)
            self.assertNotIn("<![CDATA[", rss)
            self.assertNotIn("<strong>Podcast:</strong>", rss)
            self.assertNotIn("<strong>Episode:</strong>", rss)
            self.assertNotIn("<strong>Guests:</strong>", rss)
            self.assertNotIn("<strong>Where they work:</strong>", rss)
            self.assertNotIn("<h3>Summary</h3>", rss)
            self.assertNotIn("Go to episode", rss)
            page = next((root / "public" / "episodes").glob("*/index.html")).read_text()
            self.assertIn("<title>Sam Altman on models | AI Radar</title>", page)
            self.assertIn('<link rel="canonical" href="https://radar.example.com/episodes/', page)
            self.assertIn('<meta property="og:type" content="article">', page)
            self.assertIn('<meta property="article:published_time" content="2024-06-18T10:00:00+00:00">', page)
            self.assertIn('<meta property="og:image" content="https://example.com/art.jpg">', page)
            self.assertIn('<meta name="twitter:card" content="summary_large_image">', page)
            self.assertIn('"@type":"PodcastEpisode"', page)
            self.assertIn('"duration":"PT1H"', page)
            self.assertIn('"guest":[{"@type":"Person","name":"Sam Altman"}]', page)
            self.assertIn('"associatedMedia":{"@type":"AudioObject","contentUrl":"https://cdn.example.com/episode-1.mp3"', page)
            self.assertIn("Why it matters", page)
            self.assertIn('class="skip-link" href="#main-content"', page)
            self.assertIn('<main class="detail" id="main-content" tabindex="-1">', page)
            self.assertIn("Episode facts", page)
            self.assertIn("Point one", page)
            self.assertIn(
                '<p class="transcript-sentence">Dr. Sam introduced the model across wrapped transcript lines.</p>',
                page,
            )
            self.assertIn('<p class="transcript-sentence">It changed the eval terms.</p>', page)
            self.assertIn('<p class="transcript-sentence">We talked next.</p>', page)
            self.assertIn('class="podcast-tools"', page)
            self.assertIn('Podcast feed URL', page)
            self.assertIn('value="https://example.com/feed"', page)
            self.assertIn('data-copy-url="https://example.com/feed"', page)
            self.assertIn('aria-label="Copy podcast feed URL"', page)
            self.assertNotIn('Open original episode', page)
            sitemap = (root / "public" / "sitemap.xml").read_text()
            self.assertIn("<loc>https://radar.example.com/</loc>", sitemap)
            self.assertIn("<loc>https://radar.example.com/episodes/", sitemap)
            self.assertIn("<lastmod>2024-06-18</lastmod>", sitemap)

    def test_source_link_falls_back_to_podcast_homepage_and_feed_url(self) -> None:
        episode = {
            "id": 42,
            "episode_url": "",
            "feed_homepage_url": "https://podcast.example.com",
            "feed_url": "https://podcast.example.com/feed.xml",
        }

        link = site.source_link(episode)
        tools = site.render_podcast_tools(episode)

        self.assertIn('href="https://podcast.example.com"', link)
        self.assertIn("Original podcast", link)
        self.assertIn('value="https://podcast.example.com/feed.xml"', tools)
        self.assertIn('data-copy-url="https://podcast.example.com/feed.xml"', tools)
        self.assertNotIn("Original podcast", tools)

    def test_relevant_episode_is_not_public_before_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = Config(
                root=root,
                app=AppConfig(
                    database_path=root / "var" / "radar.sqlite3",
                    public_dir=root / "public",
                    state_dir=root / "var",
                ),
                llm=LLMConfig(),
                transcription=TranscriptionConfig(
                    audio_dir=root / "var" / "audio",
                    transcript_dir=root / "var" / "transcripts",
                ),
                site=SiteConfig(base_url="https://radar.example.com", cname=""),
                feeds=(FeedConfig(name="Example", url="https://example.com/feed", hosts=("Host One",)),),
                labs=(LabConfig(name="Anthropic", people=("Fiona Fung",)),),
            )
            with storage.connect(config) as conn:
                feed_id = storage.upsert_feed(conn, config.feeds[0])
                episode_id, _ = storage.upsert_episode(
                    conn,
                    feed_id,
                    {
                        "guid": "episode-2",
                        "title": "Fiona Fung on engineering",
                        "description": "A useful episode about AI engineering.",
                        "episode_url": "https://example.com/episode-2",
                        "audio_url": "https://cdn.example.com/episode-2.mp3",
                        "audio_type": "audio/mpeg",
                        "image_url": "",
                        "published_at": "2026-06-18T10:00:00+00:00",
                        "duration": "01:00:00",
                        "hosts": ["Host One"],
                        "raw": {},
                    },
                )
                storage.set_judgement(
                    conn,
                    episode_id,
                    {
                        "include": True,
                        "confidence": 0.95,
                        "labs": ["Anthropic"],
                        "matched_people": ["Fiona Fung"],
                        "guest_names": ["Fiona Fung"],
                        "reason": "Anthropic engineering leader guest",
                    },
                )
                result = site.build_site(config, conn)

            self.assertEqual(result["episodes"], 0)
            self.assertEqual(result["rss_items"], 0)
            index = (root / "public" / "index.html").read_text()
            self.assertIn("No relevant episodes yet", index)
            self.assertFalse(any((root / "public" / "episodes").glob("*/index.html")))
            rss = (root / "public" / "feed.xml").read_text()
            self.assertNotIn("<item>", rss)
            self.assertNotIn("Fiona Fung on engineering", rss)

    def test_google_filter_is_consolidated_with_google_deepmind(self) -> None:
        controls = site.render_controls(
            None,
            [
                {"labs_json": storage.dumps(["Google"])},
                {"labs_json": storage.dumps(["Google DeepMind", "Google"])},
            ],
        )

        self.assertIn('data-filter="google-deepmind"', controls)
        self.assertIn("Google DeepMind <span>2</span>", controls)
        self.assertNotIn('data-filter="google"', controls)


if __name__ == "__main__":
    unittest.main()
