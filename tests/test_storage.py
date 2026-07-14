import sqlite3
import unittest

from podcast_radar.config import FeedConfig
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


if __name__ == "__main__":
    unittest.main()
