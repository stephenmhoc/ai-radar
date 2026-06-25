import pathlib
import tempfile
import unittest

from podcast_radar.config import AppConfig, Config, FeedConfig, LLMConfig, LabConfig, SiteConfig, TranscriptionConfig
from podcast_radar import site, storage


class SiteGenerationTests(unittest.TestCase):
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
                storage.set_transcript(conn, episode_id, "Transcript text", root / "transcript.txt")
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
            self.assertTrue((root / "public" / "index.html").exists())
            self.assertTrue((root / "public" / "feed.xml").exists())
            self.assertEqual((root / "public" / "CNAME").read_text().strip(), "radar.example.com")
            index = (root / "public" / "index.html").read_text()
            self.assertIn("Sam Altman", index)
            rss = (root / "public" / "feed.xml").read_text()
            self.assertIn("https://radar.example.com/episodes/", rss)


if __name__ == "__main__":
    unittest.main()

