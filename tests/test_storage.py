import sqlite3
import unittest

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


if __name__ == "__main__":
    unittest.main()
