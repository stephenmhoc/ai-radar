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

    def test_topline_uses_short_summary_instead_of_long_key_point(self) -> None:
        episode = {
            "short_summary_text": "Benchmarks miss test-time compute.",
            "key_points_json": storage.dumps(
                [
                    "Traditional single-number benchmark scores are misleading because they do not account "
                    "for test-time compute; this longer key point belongs on the detail page."
                ]
            ),
            "summary_text": "",
            "summary_html": "",
            "summary_title": "Noam Brown on benchmarks",
            "title": "Why traditional benchmarks fail modern AI models",
        }

        self.assertEqual(site._topline(episode, guests=["Noam Brown"]), "Noam Brown: Benchmarks miss test-time compute.")

    def test_short_summary_fallback_does_not_show_oversized_fragment(self) -> None:
        long_unpunctuated_point = (
            "Traditional single-number benchmark scores are misleading because they do not account for "
            "test-time compute and this unpunctuated thought should not be rendered as a giant card headline"
        )
        episode = {
            "short_summary_text": "",
            "key_points_json": storage.dumps([long_unpunctuated_point]),
            "summary_text": "",
            "summary_html": "",
            "summary_title": "Noam Brown on benchmarks",
            "title": "Why traditional benchmarks fail modern AI models",
        }

        self.assertEqual(site._topline(episode, guests=["Noam Brown"]), "Noam Brown on benchmarks")
        self.assertNotIn("unpunctuated thought", site._topline(episode, guests=["Noam Brown"]))

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
                        "summary": "Sam's summary text",
                        "short_summary": "Sam explains practical model tradeoffs.",
                        "summary_html": "<p>Sam's summary text</p>",
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
            self.assertTrue((root / "public" / "sources" / "index.html").exists())
            self.assertTrue((root / "public" / "assets" / "logo.svg").exists())
            self.assertTrue((root / "public" / "assets" / "logo.png").exists())
            self.assertTrue((root / "public" / "favicon.ico").exists())
            self.assertTrue((root / "public" / "favicon.png").exists())
            self.assertTrue((root / "public" / "apple-touch-icon.png").exists())
            self.assertTrue(any((root / "public" / "assets" / "social").glob("ai-radar-*.*")))
            self.assertTrue(any((root / "public" / "assets" / "social").glob("episode-*.*")))
            self.assertTrue(any((root / "public" / "assets").glob("style-*.css")))
            home_card_svg = next((root / "public" / "assets" / "social").glob("ai-radar-*.svg")).read_text()
            self.assertIn("Updated 2024-06-18", home_card_svg)
            self.assertNotIn("verified episodes", home_card_svg)
            self.assertNotIn("podcasts · Latest", home_card_svg)
            self.assertEqual(home_card_svg.count(">AI Radar</text>"), 1)
            self.assertEqual((root / "public" / "CNAME").read_text().strip(), "radar.example.com")
            robots = (root / "public" / "robots.txt").read_text()
            self.assertIn("Sitemap: https://radar.example.com/sitemap.xml", robots)
            index = (root / "public" / "index.html").read_text()
            self.assertIn('/assets/style-', index)
            self.assertNotIn('href="/assets/style.css"', index)
            self.assertIn('<link rel="canonical" href="https://radar.example.com/">', index)
            self.assertIn('<meta property="og:type" content="website">', index)
            self.assertIn('<meta property="og:title" content="AI Radar">', index)
            self.assertIn('<meta property="og:image" content="https://radar.example.com/assets/social/ai-radar-', index)
            self.assertIn('<meta property="og:image:secure_url" content="https://radar.example.com/assets/social/ai-radar-', index)
            self.assertIn('<meta property="og:image:width" content="1200">', index)
            self.assertIn('<meta property="og:image:height" content="630">', index)
            self.assertIn('<meta property="og:image:alt" content="AI Radar social preview card">', index)
            self.assertIn('<meta name="twitter:card" content="summary_large_image">', index)
            self.assertIn('<meta name="twitter:image" content="https://radar.example.com/assets/social/ai-radar-', index)
            self.assertIn('<meta name="twitter:image:alt" content="AI Radar social preview card">', index)
            self.assertIn('type="application/ld+json"', index)
            self.assertIn('"@type":"CollectionPage"', index)
            self.assertIn('"hasPart":[{"@type":"PodcastEpisode"', index)
            self.assertIn('class="skip-link" href="#main-content"', index)
            self.assertIn("Made with ♥ by", index)
            self.assertIn('href="/sources/">Monitored sources</a>', index)
            self.assertIn('href="https://merimerimeri.com/">MeriMeriMeri Software</a>', index)
            self.assertIn("Copyright 2026", index)
            self.assertIn('<main id="main-content" tabindex="-1" aria-labelledby="episode-list-title">', index)
            self.assertIn('class="visually-hidden" id="episode-list-title"', index)
            self.assertIn("Sam Altman", index)
            self.assertIn("data-filter=\"openai\"", index)
            self.assertIn("data-labs=\"openai\"", index)
            self.assertIn('data-search-input aria-controls="episode-list"', index)
            self.assertNotIn("data-result-count", index)
            self.assertNotIn("data-result-label", index)
            self.assertNotIn('class="search-meta"', index)
            self.assertIn("data-pagination", index)
            self.assertIn("data-page-next", index)
            self.assertIn('aria-label="Next page"', index)
            self.assertIn("Use our RSS feed to stay up to date", index)
            self.assertIn('class="follow-link"', index)
            self.assertIn('href="https://radar.example.com/feed.xml"', index)
            self.assertIn('data-copy-url="https://radar.example.com/feed.xml"', index)
            self.assertIn('<link rel="icon" href="/favicon.ico" sizes="any">', index)
            self.assertIn('<link rel="icon" href="/assets/logo.svg" type="image/svg+xml">', index)
            self.assertIn('<link rel="icon" href="/assets/logo.png" type="image/png" sizes="512x512">', index)
            self.assertIn('<link rel="apple-touch-icon" href="/apple-touch-icon.png">', index)
            self.assertIn('data-copy-status role="status" aria-live="polite"', index)
            self.assertIn('aria-label="Show OpenAI items, 1 item"', index)
            self.assertIn('class="content-grid"', index)
            self.assertIn('class="side-rail"', index)
            self.assertIn('<a class="episode-card"', index)
            self.assertNotIn("data-topic-filter", index)
            self.assertNotIn("data-feed-filter", index)
            self.assertNotIn("data-sort", index)
            self.assertNotIn("rss-text-link", index)
            self.assertNotIn('class="briefing"', index)
            self.assertNotIn("briefings", index.lower())
            self.assertIn("Sam Altman: Sam explains practical model tradeoffs.", index)
            self.assertIn('<p class="source-line">', index)
            self.assertIn('<span class="source-main"><strong>Example</strong><span class="source-title"> · Sam Altman on models</span></span>', index)
            self.assertIn('<span class="source-details">2024-06-18 · 1h 00m</span>', index)
            self.assertIn("Example · Sam Altman on models · 1h 00m", index)
            self.assertIn('class="rail-card newest-card"', index)
            self.assertIn("<span>Newest items</span>", index)
            self.assertNotIn("1 total", index)
            self.assertNotIn("Episode details", index)
            self.assertNotIn("Verified brief", index)
            self.assertNotIn('<p class="summary">', index)
            self.assertNotIn("Summary and transcript ready", index)
            rss = (root / "public" / "feed.xml").read_text()
            rss_root = ET.fromstring(rss)
            namespaces = {
                "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
            }
            self.assertIn("https://radar.example.com/episodes/", rss)
            self.assertIn("<title>Sam Altman on models</title>", rss)
            channel_image_url = rss_root.findtext("./channel/image/url") or ""
            self.assertEqual(channel_image_url, "https://radar.example.com/assets/logo.png")
            self.assertEqual(rss_root.findtext("./channel/image/width"), "144")
            self.assertEqual(rss_root.findtext("./channel/image/height"), "144")
            channel_itunes_image = rss_root.find("./channel/itunes:image", namespaces)
            self.assertIsNotNone(channel_itunes_image)
            self.assertEqual(channel_itunes_image.attrib.get("href"), channel_image_url)
            item_itunes_image = rss_root.find("./channel/item/itunes:image", namespaces)
            self.assertIsNone(item_itunes_image)
            self.assertNotIn("media:thumbnail", rss)
            self.assertNotIn("/assets/social/", channel_image_url)
            rss_description = rss_root.findtext("./channel/item/description") or ""
            self.assertIn("Sam explains practical model tradeoffs.", rss_description)
            self.assertNotIn("Sam's summary text", rss_description)
            self.assertNotIn("Original podcast", rss_description)
            self.assertNotIn("https://example.com/episode-1", rss_description)
            self.assertIn("<h3>Podcast details</h3>", rss_description)
            self.assertIn("<strong>Podcast:</strong> Example", rss_description)
            self.assertIn("<strong>Title:</strong> Sam Altman on models", rss_description)
            self.assertIn("<strong>Guests:</strong> Sam Altman", rss_description)
            self.assertIn("<strong>Release date:</strong> June 18, 2024", rss_description)
            self.assertIn("<strong>Duration:</strong> 1h 00m", rss_description)
            self.assertIn('<a href="https://radar.example.com/episodes/', rss_description)
            self.assertIn(">AI Radar page</a>", rss_description)
            self.assertTrue(rss_description.endswith(">AI Radar page</a>"))
            self.assertEqual(rss_description.count("<a "), 1)
            self.assertIn("<pubDate>Tue, 18 Jun 2024 10:00:00 GMT</pubDate>", rss)
            self.assertNotIn("<![CDATA[", rss)
            self.assertIn("&lt;strong&gt;Podcast:&lt;/strong&gt;", rss)
            self.assertIn("&lt;strong&gt;Guests:&lt;/strong&gt;", rss)
            self.assertNotIn("<strong>Where they work:</strong>", rss)
            self.assertNotIn("<h3>Summary</h3>", rss)
            self.assertNotIn("Go to episode", rss)
            page = next((root / "public" / "episodes").glob("*/index.html")).read_text()
            self.assertIn("<title>Sam Altman on models | AI Radar</title>", page)
            self.assertIn('<link rel="canonical" href="https://radar.example.com/episodes/', page)
            self.assertIn('<meta property="og:type" content="article">', page)
            self.assertIn('<meta property="article:published_time" content="2024-06-18T10:00:00+00:00">', page)
            self.assertIn('<meta property="og:image" content="https://radar.example.com/assets/social/episode-', page)
            self.assertIn('<meta property="og:image:width" content="1200">', page)
            self.assertIn('<meta property="og:image:height" content="630">', page)
            self.assertIn('<meta property="og:image:alt" content="AI Radar preview for Sam Altman on models">', page)
            self.assertIn('<meta name="twitter:card" content="summary_large_image">', page)
            self.assertIn('<meta name="twitter:image" content="https://radar.example.com/assets/social/episode-', page)
            self.assertIn('<meta name="twitter:image:alt" content="AI Radar preview for Sam Altman on models">', page)
            self.assertIn('"@type":"PodcastEpisode"', page)
            self.assertIn('"duration":"PT1H"', page)
            self.assertIn('"image":"https://radar.example.com/assets/social/episode-', page)
            self.assertIn('"guest":[{"@type":"Person","name":"Sam Altman"}]', page)
            self.assertIn('"associatedMedia":{"@type":"AudioObject","contentUrl":"https://cdn.example.com/episode-1.mp3"', page)
            self.assertIn("Why it matters", page)
            self.assertIn('class="skip-link" href="#main-content"', page)
            self.assertIn("Made with ♥ by", page)
            self.assertIn("Copyright 2026", page)
            self.assertIn('<main class="detail" id="main-content" tabindex="-1">', page)
            self.assertIn("Item facts", page)
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
            self.assertIn("<loc>https://radar.example.com/sources/</loc>", sitemap)
            self.assertIn("<loc>https://radar.example.com/episodes/", sitemap)
            self.assertIn("<lastmod>2024-06-18</lastmod>", sitemap)
            sources = (root / "public" / "sources" / "index.html").read_text()
            self.assertIn("Monitored sources", sources)
            self.assertIn("Example", sources)
            self.assertIn(">Podcast</span>", sources)
            self.assertIn("A source appears here even when it has no qualifying published item yet.", sources)

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

    def test_multimodal_item_renders_all_media_and_source_links(self) -> None:
        appearances = [
            {
                "medium": "podcast",
                "url": "https://example.com/listen",
                "feed_url": "https://example.com/feed.xml",
                "source_url": "https://example.com/feed.xml",
            },
            {
                "medium": "youtube",
                "url": "https://youtube.com/watch?v=one",
                "source_url": "https://youtube.com/channel/example",
            },
        ]
        episode = {
            "id": 42,
            "medium": "podcast",
            "media_kinds_json": storage.dumps(["podcast", "youtube"]),
            "appearances_json": storage.dumps(appearances),
            "feed_url": "https://example.com/feed.xml",
        }

        badges = site.render_media_badges(episode)
        links = site.render_source_links(episode)
        tools = site.render_podcast_tools(episode)

        self.assertIn(">Podcast</span>", badges)
        self.assertIn(">YouTube</span>", badges)
        self.assertIn("Listen to podcast", links)
        self.assertIn("Watch on YouTube", links)
        self.assertEqual(links.count("external-link"), 2)
        self.assertIn("https://example.com/feed.xml", tools)

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
            self.assertIn("No relevant items yet", index)
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
