"""The shared-corpus coordinator.

The point of moving off the broker is that the Mac transcribes a given episode
once, no matter which project wanted it, so these check that a transcript
somebody else already made is used rather than requested again.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

from podcast_radar import transcribe_anything


class FakeService:
    def __init__(self, answers):
        self.answers = answers
        self.requested = []
        self.fetched = []

    def request_transcript(self, url, *, title="", duration_seconds=None, prompt=""):
        self.requested.append({"url": url, "title": title, "duration": duration_seconds, "prompt": prompt})
        return self.answers.get(url, {"slug": "new-slug", "status": "queued"})

    def fetch(self, slug):
        self.fetched.append(slug)
        return self.answers.get(slug, {"slug": slug, "status": "queued"})


class TranscribeAnythingTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE radar_items (id INTEGER PRIMARY KEY)")
        self.conn.execute("INSERT INTO radar_items (id) VALUES (7)")
        transcribe_anything.migrate(self.conn)

    def job_row(self, item_id=7):
        return self.conn.execute(
            "SELECT * FROM transcribe_anything_jobs WHERE item_id = ?", (item_id,)
        ).fetchone()

    def test_remembers_what_it_asked_for(self):
        transcribe_anything._remember(self.conn, 7, "abc123", "waiting")
        row = self.job_row()
        self.assertEqual(row["slug"], "abc123")
        self.assertEqual(row["status"], "waiting")

    def test_a_second_answer_updates_rather_than_duplicating(self):
        transcribe_anything._remember(self.conn, 7, "abc123", "waiting")
        transcribe_anything._remember(self.conn, 7, "abc123", "done")
        rows = self.conn.execute("SELECT count(*) FROM transcribe_anything_jobs").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(self.job_row()["status"], "done")

    def test_duration_parsing(self):
        self.assertEqual(transcribe_anything._seconds("00:53:41"), 3221)
        self.assertEqual(transcribe_anything._seconds("12:30"), 750)
        self.assertEqual(transcribe_anything._seconds("900"), 900)
        self.assertIsNone(transcribe_anything._seconds(""))
        self.assertIsNone(transcribe_anything._seconds("not a duration"))

    def test_an_http_error_becomes_a_readable_failure(self):
        client = transcribe_anything.Client("http://example.invalid", "token")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertRaises(transcribe_anything.TranscribeAnythingError):
                client.fetch("abc")

    def test_a_client_needs_somewhere_to_go_and_something_to_prove_it(self):
        with self.assertRaises(transcribe_anything.TranscribeAnythingError):
            transcribe_anything.Client("", "token")
        with self.assertRaises(transcribe_anything.TranscribeAnythingError):
            transcribe_anything.Client("http://example.invalid", "")


if __name__ == "__main__":
    unittest.main()
