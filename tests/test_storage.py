import sqlite3
import unittest

from podcast_radar.config import FeedConfig, SourceConfig
from podcast_radar import storage


class StorageMigrationTests(unittest.TestCase):
    def test_migrate_adds_short_summary_to_existing_episode_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE episodes (
              id INTEGER PRIMARY KEY,
              status TEXT NOT NULL DEFAULT 'new',
              published_at TEXT
            )
            """
        )

        storage.migrate(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(episodes)")}
        self.assertIn("short_summary_text", columns)

    def test_episodes_for_status_filters_by_feed_name(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        alpha_id = storage.upsert_feed(conn, FeedConfig(name="Alpha", url="https://alpha.example/feed"))
        beta_id = storage.upsert_feed(conn, FeedConfig(name="Beta", url="https://beta.example/feed"))
        storage.upsert_episode(
            conn,
            alpha_id,
            {
                "guid": "alpha-1",
                "title": "Alpha episode",
                "description": "",
                "episode_url": "",
                "audio_url": "",
                "audio_type": "",
                "image_url": "",
                "published_at": "2025-01-02T00:00:00+00:00",
                "duration": "",
                "hosts": [],
                "raw": {},
            },
        )
        storage.upsert_episode(
            conn,
            beta_id,
            {
                "guid": "beta-1",
                "title": "Beta episode",
                "description": "",
                "episode_url": "",
                "audio_url": "",
                "audio_type": "",
                "image_url": "",
                "published_at": "2025-01-02T00:00:00+00:00",
                "duration": "",
                "hosts": [],
                "raw": {},
            },
        )

        episodes = storage.episodes_for_status(conn, ("new",), feed_names=("Beta",))

        self.assertEqual([episode["title"] for episode in episodes], ["Beta episode"])

        matched = storage.episodes_for_status(conn, ("new",), search_text="Alpha")

        self.assertEqual([episode["title"] for episode in matched], ["Alpha episode"])

    def test_cross_posted_podcast_and_youtube_share_one_item(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        podcast_id = storage.upsert_source(
            conn,
            SourceConfig(kind="podcast", name="Example Podcast", url="https://example.com/feed.xml"),
        )
        youtube_id = storage.upsert_source(
            conn,
            SourceConfig(kind="youtube", name="Example Channel", url="https://youtube.com/channel/example"),
        )
        podcast_item_id, _ = storage.upsert_appearance(
            conn,
            podcast_id,
            {
                "external_id": "pod-1",
                "title": "Sam Altman on the future of reasoning models",
                "url": "https://example.com/pod-1",
                "media_url": "https://example.com/pod-1.mp3",
                "media_type": "audio/mpeg",
                "published_at": "2026-08-01T12:00:00+00:00",
                "duration": "01:02:00",
            },
        )
        youtube_item_id, inserted = storage.upsert_appearance(
            conn,
            youtube_id,
            {
                "external_id": "video-1",
                "title": "Sam Altman on the future of reasoning models (Full Video)",
                "url": "https://youtube.com/watch?v=video-1",
                "media_url": "https://youtube.com/watch?v=video-1",
                "media_type": "video/youtube",
                "published_at": "2026-08-02T12:00:00+00:00",
                "duration": "01:01:30",
            },
        )

        self.assertTrue(inserted)
        self.assertEqual(youtube_item_id, podcast_item_id)
        item = storage.item_by_id(conn, podcast_item_id)
        self.assertEqual(storage.loads(item["media_kinds_json"]), ["podcast", "youtube"])
        self.assertEqual(len(storage.loads(item["appearances_json"])), 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM radar_items").fetchone()[0], 1)

    def test_official_source_hint_repairs_an_existing_unmerged_appearance(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        podcast_id = storage.upsert_source(
            conn,
            SourceConfig(kind="podcast", name="Example Podcast", url="https://example.com/feed.xml"),
        )
        youtube_id = storage.upsert_source(
            conn,
            SourceConfig(kind="youtube", name="Example Podcast — YouTube", url="https://youtube.com/example"),
        )
        podcast_item_id, _ = storage.upsert_appearance(
            conn,
            podcast_id,
            {
                "external_id": "pod-1",
                "title": "A podcast title",
                "url": "https://example.com/pod-1",
                "published_at": "2025-01-01T00:00:00+00:00",
                "duration": "01:00:00",
            },
        )
        youtube_item_id, _ = storage.upsert_appearance(
            conn,
            youtube_id,
            {
                "external_id": "video-1",
                "title": "A differently formatted video title",
                "url": "https://youtube.com/watch?v=video-1",
                "duration": "01:00:00",
            },
        )
        self.assertNotEqual(youtube_item_id, podcast_item_id)

        repaired_item_id, inserted = storage.upsert_appearance(
            conn,
            youtube_id,
            {
                "external_id": "video-1",
                "title": "A differently formatted video title",
                "url": "https://youtube.com/watch?v=video-1",
                "published_at": "2025-01-01T00:00:00+00:00",
                "duration": "01:00:00",
                "canonical_item_id": podcast_item_id,
            },
        )

        self.assertFalse(inserted)
        self.assertEqual(repaired_item_id, podcast_item_id)
        self.assertEqual(len(storage.appearances_for_item(conn, podcast_item_id)), 2)
        merged = conn.execute("SELECT status FROM radar_items WHERE id = ?", (youtube_item_id,)).fetchone()
        self.assertEqual(merged["status"], "merged")

    def test_duplicate_window_counts_items_not_appearances(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        podcast_id = storage.upsert_source(
            conn,
            SourceConfig(kind="podcast", name="Example Podcast", url="https://example.com/feed.xml"),
        )
        blog_id = storage.upsert_source(
            conn,
            SourceConfig(kind="blog", name="Example blog", url="https://example.com/blog"),
        )
        youtube_id = storage.upsert_source(
            conn,
            SourceConfig(kind="youtube", name="Example Podcast — YouTube", url="https://youtube.com/example"),
        )
        target_item_id, _ = storage.upsert_appearance(
            conn,
            podcast_id,
            {
                "external_id": "pod-1",
                "title": "Sam Altman on reasoning models and post-training",
                "url": "https://example.com/pod-1",
                "published_at": "2026-08-01T12:00:00+00:00",
                "duration": "01:02:00",
            },
        )
        # A newer item carrying two appearances used to consume the whole
        # comparison window and hide every older item from deduplication.
        noise_item_id, _ = storage.upsert_appearance(
            conn,
            podcast_id,
            {
                "external_id": "pod-2",
                "title": "An entirely unrelated conversation about hardware supply",
                "url": "https://example.com/pod-2",
                "published_at": "2026-08-03T12:00:00+00:00",
                "duration": "00:31:00",
            },
        )
        storage.upsert_appearance(
            conn,
            blog_id,
            {
                "external_id": "article-2",
                "title": "An entirely unrelated conversation about hardware supply",
                "url": "https://example.com/blog/hardware",
                "published_at": "2026-08-03T12:00:00+00:00",
                "canonical_item_id": noise_item_id,
            },
        )
        self.assertEqual(len(storage.appearances_for_item(conn, noise_item_id)), 2)

        original_window = storage.DUPLICATE_WINDOW_ITEMS
        storage.DUPLICATE_WINDOW_ITEMS = 2
        try:
            youtube_item_id, _ = storage.upsert_appearance(
                conn,
                youtube_id,
                {
                    "external_id": "video-1",
                    "title": "Sam Altman on reasoning models and post-training (Full Video)",
                    "url": "https://youtube.com/watch?v=video-1",
                    "published_at": "2026-08-02T12:00:00+00:00",
                    "duration": "01:01:30",
                },
            )
        finally:
            storage.DUPLICATE_WINDOW_ITEMS = original_window

        self.assertEqual(youtube_item_id, target_item_id)

    def test_identical_full_text_deduplicates_blog_and_x(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        blog_id = storage.upsert_source(
            conn,
            SourceConfig(kind="blog", name="Leader blog", url="https://example.com/blog"),
        )
        x_id = storage.upsert_source(
            conn,
            SourceConfig(kind="x", name="Leader on X", url="https://x.com/leader"),
        )
        content = "A substantial explanation of model behavior and evaluation. " * 20
        first_id, _ = storage.upsert_appearance(
            conn,
            blog_id,
            {
                "external_id": "article-1",
                "title": "How we evaluate reasoning systems",
                "url": "https://example.com/blog/evals",
                "published_at": "2026-08-01T12:00:00+00:00",
                "content_text": content,
            },
        )
        second_id, _ = storage.upsert_appearance(
            conn,
            x_id,
            {
                "external_id": "thread-1",
                "title": "A thread about our evaluation approach",
                "url": "https://x.com/leader/status/1",
                "published_at": "2026-08-01T13:00:00+00:00",
                "content_text": content,
            },
        )

        self.assertEqual(second_id, first_id)
        self.assertEqual(len(storage.appearances_for_item(conn, first_id)), 2)

    def test_same_title_without_runtime_requires_duplicate_review(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        podcast_id = storage.upsert_source(
            conn,
            SourceConfig(kind="podcast", name="Example Podcast", url="https://example.com/feed.xml"),
        )
        youtube_id = storage.upsert_source(
            conn,
            SourceConfig(kind="youtube", name="Example Channel", url="https://youtube.com/channel/example"),
        )
        first_id, _ = storage.upsert_appearance(
            conn,
            podcast_id,
            {
                "external_id": "pod-1",
                "title": "A conversation about AI agents",
                "url": "https://example.com/pod-1",
                "published_at": "2026-08-01T12:00:00+00:00",
            },
        )
        second_id, _ = storage.upsert_appearance(
            conn,
            youtube_id,
            {
                "external_id": "video-1",
                "title": "A conversation about AI agents",
                "url": "https://youtube.com/watch?v=video-1",
                "published_at": "2026-08-01T15:00:00+00:00",
            },
        )

        self.assertNotEqual(second_id, first_id)
        candidates = storage.pending_dedupe_candidates(conn)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["reason"],
            "same normalized title and publication window; duration unavailable",
        )

    def test_same_substantial_description_auto_merges_without_runtime(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        podcast_id = storage.upsert_source(
            conn,
            SourceConfig(kind="podcast", name="Example Podcast", url="https://example.com/feed.xml"),
        )
        youtube_id = storage.upsert_source(
            conn,
            SourceConfig(kind="youtube", name="Example Channel", url="https://youtube.com/channel/example"),
        )
        description = "A specific account of the researchers, architecture, evidence, and conclusions. " * 8
        first_id, _ = storage.upsert_appearance(
            conn,
            podcast_id,
            {
                "external_id": "pod-1",
                "title": "Building an automated AI research lab",
                "description": description,
                "url": "https://example.com/pod-1",
                "published_at": "2026-08-01T12:00:00+00:00",
            },
        )
        second_id, _ = storage.upsert_appearance(
            conn,
            youtube_id,
            {
                "external_id": "video-1",
                "title": "Building an Automated AI Research Lab (Full Video)",
                "description": description,
                "url": "https://youtube.com/watch?v=video-1",
                "published_at": "2026-08-01T15:00:00+00:00",
            },
        )

        self.assertEqual(second_id, first_id)
        self.assertEqual(len(storage.appearances_for_item(conn, first_id)), 2)
        self.assertEqual(storage.pending_dedupe_candidates(conn), [])

    def test_manual_merge_preserves_prepared_content_on_canonical_item(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        storage.migrate(conn)
        podcast_id = storage.upsert_source(
            conn,
            SourceConfig(kind="podcast", name="Example Podcast", url="https://example.com/feed.xml"),
        )
        youtube_id = storage.upsert_source(
            conn,
            SourceConfig(kind="youtube", name="Example Channel", url="https://youtube.com/channel/example"),
        )
        appearance = {
            "title": "A conversation about model evaluation",
            "published_at": "2026-08-01T12:00:00+00:00",
        }
        first_id, _ = storage.upsert_appearance(
            conn,
            podcast_id,
            {**appearance, "external_id": "pod-1", "url": "https://example.com/pod-1"},
        )
        second_id, _ = storage.upsert_appearance(
            conn,
            youtube_id,
            {**appearance, "external_id": "video-1", "url": "https://youtube.com/watch?v=video-1"},
        )
        content = "Substantial prepared source text. " * 30
        storage.set_content(conn, second_id, content)

        canonical = storage.merge_items(conn, first_id, second_id, reason="confirmed test duplicate")
        item = storage.item_by_id(conn, canonical)

        self.assertEqual(canonical, first_id)
        self.assertEqual(item["status"], "transcribed")
        self.assertEqual(item["content_text"], " ".join(content.split()))
        self.assertEqual(len(storage.appearances_for_item(conn, canonical)), 2)
        self.assertEqual(storage.pending_dedupe_candidates(conn), [])


if __name__ == "__main__":
    unittest.main()
