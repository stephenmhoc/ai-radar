import dataclasses
import json
import pathlib
import sqlite3
import tempfile
import unittest

from podcast_radar import distributed, storage
from podcast_radar.config import AppConfig, Config, SourceConfig, TranscriptionConfig


def build_config(root: pathlib.Path) -> Config:
    app_fields = {field.name for field in dataclasses.fields(AppConfig)}
    app_kwargs = {}
    if "database_path" in app_fields:
        app_kwargs["database_path"] = root / "radar.sqlite3"
    if "public_dir" in app_fields:
        app_kwargs["public_dir"] = root / "public"
    app = dataclasses.replace(AppConfig(), **app_kwargs) if app_kwargs else AppConfig()

    transcription = TranscriptionConfig(
        command="whisper-cli",
        args=(
            "-m",
            "/Users/someone/.cache/whisper.cpp/model.bin",
            "-f",
            "{audio_path}",
            "-otxt",
            "-of",
            "{output_stem}",
            "--prompt",
            "Show: {feed_name}. Episode: {episode_title}.",
        ),
        output_path="{output_stem}.txt",
        audio_dir=root / "audio",
        transcript_dir=root / "transcripts",
        mode="remote",
        queue_root=root / "queue",
    )
    config_fields = {field.name for field in dataclasses.fields(Config)}
    kwargs = {"app": app, "transcription": transcription}
    for name in config_fields:
        if name in kwargs:
            continue
        if name in ("feeds", "labs", "sources"):
            kwargs[name] = ()
    # Fill any remaining required fields with their dataclass defaults.
    for field in dataclasses.fields(Config):
        if field.name in kwargs:
            continue
        if field.default is not dataclasses.MISSING:
            kwargs[field.name] = field.default
        elif field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            kwargs[field.name] = field.default_factory()  # type: ignore[misc]
        else:
            kwargs[field.name] = type(field.type)() if isinstance(field.type, type) else None
    return Config(**kwargs)


def seed_relevant_item(conn: sqlite3.Connection, *, external_id: str = "ep-1") -> int:
    source_id = storage.upsert_source(
        conn,
        SourceConfig(kind="podcast", name="Test Podcast", url="https://example.com/feed"),
    )
    item_id, _ = storage.upsert_appearance(
        conn,
        source_id,
        {
            "external_id": external_id,
            "title": "An Episode About Models",
            "description": "A guest from OpenAI.",
            "url": "https://example.com/ep",
            "media_url": "https://example.com/ep.mp3",
            "media_type": "audio/mpeg",
            "published_at": "2026-08-01T00:00:00+00:00",
        },
    )
    conn.execute("UPDATE radar_items SET status = 'relevant' WHERE id = ?", (item_id,))
    conn.commit()
    return int(item_id)


class DistributedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.config = build_config(self.root)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        storage.migrate(self.conn)
        distributed.migrate(self.conn)
        self.queue = self.config.transcription.queue_root

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def enqueue(self):
        return distributed.enqueue_pending(self.config, self.conn, self.queue)

    def pending_jobs(self):
        return sorted((self.queue / "pending").glob("*.json"))


class Enqueue(DistributedTestCase):
    def test_relevant_item_produces_a_job(self) -> None:
        seed_relevant_item(self.conn)
        stats = self.enqueue()
        self.assertEqual(stats["enqueued"], 1)
        jobs = self.pending_jobs()
        self.assertEqual(len(jobs), 1)
        job = json.loads(jobs[0].read_text())
        self.assertEqual(job["source"], "ai-radar")
        self.assertEqual(job["natural_key"], {"source_name": "Test Podcast", "external_id": "ep-1"})
        self.assertEqual(job["media"]["url"], "https://example.com/ep.mp3")
        self.assertEqual(job["media"]["kind"], "audio")

    def test_prompt_placeholders_are_resolved_but_worker_paths_are_not(self) -> None:
        seed_relevant_item(self.conn)
        self.enqueue()
        job = json.loads(self.pending_jobs()[0].read_text())
        args = job["transcription"]["args"]
        self.assertIn("{audio_path}", args)
        self.assertIn("{output_stem}", args)
        prompt = next(arg for arg in args if arg.startswith("Show:"))
        self.assertIn("Test Podcast", prompt)
        self.assertIn("An Episode About Models", prompt)

    def test_local_model_path_is_replaced_with_a_worker_placeholder(self) -> None:
        seed_relevant_item(self.conn)
        self.enqueue()
        args = json.loads(self.pending_jobs()[0].read_text())["transcription"]["args"]
        # The coordinator's model path does not exist on the worker.
        self.assertNotIn("/Users/someone/.cache/whisper.cpp/model.bin", args)
        self.assertEqual(args[args.index("-m") + 1], "{model}")

    def test_enqueue_is_idempotent(self) -> None:
        seed_relevant_item(self.conn)
        self.enqueue()
        stats = self.enqueue()
        self.assertEqual(stats["enqueued"], 0)
        self.assertEqual(stats["already_queued"], 1)
        self.assertEqual(len(self.pending_jobs()), 1)

    def test_items_that_already_have_text_are_not_queued(self) -> None:
        item_id = seed_relevant_item(self.conn)
        self.conn.execute(
            "UPDATE radar_items SET content_text = ? WHERE id = ?", ("already here", item_id)
        )
        self.conn.commit()
        stats = self.enqueue()
        self.assertEqual(stats["enqueued"], 0)
        self.assertEqual(stats["skipped"], 1)

    def test_job_id_is_stable_across_runs(self) -> None:
        seed_relevant_item(self.conn)
        self.enqueue()
        first = self.pending_jobs()[0].name
        self.conn.execute("UPDATE transcription_jobs SET status = 'expired'")
        self.conn.commit()
        self.enqueue()
        self.assertEqual([p.name for p in self.pending_jobs()], [first])


class ImportResults(DistributedTestCase):
    def _result_for(self, job: dict, text: str) -> pathlib.Path:
        import hashlib

        payload = {
            "schema_version": 1,
            "job_id": job["job_id"],
            "source": "ai-radar",
            "natural_key": job["natural_key"],
            "transcript": {
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "completed_at": "2026-08-15T23:00:00+00:00",
            },
            "worker": {"id": "mac-mini", "host": "mac-mini.local"},
        }
        path = self.queue / "results" / f"{job['job_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _enqueued_job(self) -> dict:
        seed_relevant_item(self.conn)
        self.enqueue()
        return json.loads(self.pending_jobs()[0].read_text())

    def test_transcript_lands_on_the_item(self) -> None:
        job = self._enqueued_job()
        self._result_for(job, "the transcript body")
        stats = distributed.import_results(self.config, self.conn, self.queue)
        self.assertEqual(stats["imported"], 1)
        row = self.conn.execute(
            "SELECT status, content_text FROM radar_items LIMIT 1"
        ).fetchone()
        self.assertEqual(row["content_text"], "the transcript body")
        self.assertEqual(row["status"], "transcribed")

    def test_import_clears_the_pending_job_so_it_stops_being_redelivered(self) -> None:
        job = self._enqueued_job()
        self._result_for(job, "body")
        distributed.import_results(self.config, self.conn, self.queue)
        self.assertEqual(self.pending_jobs(), [])
        self.assertEqual(list((self.queue / "results").glob("*.json")), [])

    def test_bad_checksum_is_rejected_and_job_stays_queued(self) -> None:
        job = self._enqueued_job()
        path = self._result_for(job, "body")
        payload = json.loads(path.read_text())
        payload["transcript"]["text"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")

        stats = distributed.import_results(self.config, self.conn, self.queue)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["imported"], 0)
        # The job must remain pending so it is retried rather than silently lost.
        self.assertEqual(len(self.pending_jobs()), 1)
        row = self.conn.execute("SELECT content_text FROM radar_items LIMIT 1").fetchone()
        self.assertIsNone(row["content_text"])

    def test_importing_the_same_result_twice_is_safe(self) -> None:
        job = self._enqueued_job()
        self._result_for(job, "body")
        distributed.import_results(self.config, self.conn, self.queue)
        self._result_for(job, "body")
        stats = distributed.import_results(self.config, self.conn, self.queue)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["imported"], 0)

    def test_unknown_natural_key_is_reported_not_applied(self) -> None:
        job = self._enqueued_job()
        path = self._result_for(job, "body")
        payload = json.loads(path.read_text())
        payload["natural_key"] = {"source_name": "Nope", "external_id": "missing"}
        path.write_text(json.dumps(payload), encoding="utf-8")
        stats = distributed.import_results(self.config, self.conn, self.queue)
        self.assertEqual(stats["errors"], 1)

    def test_worker_failure_marks_the_item_failed(self) -> None:
        job = self._enqueued_job()
        payload = {
            "schema_version": 1,
            "job_id": job["job_id"],
            "source": "ai-radar",
            "natural_key": job["natural_key"],
            "error": {"message": "yt-dlp exited 1", "stage": "download"},
            "worker": {"id": "mac-mini", "host": "mac-mini.local"},
        }
        path = self.queue / "results" / f"{job['job_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

        stats = distributed.import_results(self.config, self.conn, self.queue)
        self.assertEqual(stats["failed"], 1)
        row = self.conn.execute("SELECT status FROM radar_items LIMIT 1").fetchone()
        self.assertEqual(row["status"], "transcription_failed")
        job_row = self.conn.execute(
            "SELECT status, error_message FROM transcription_jobs LIMIT 1"
        ).fetchone()
        self.assertEqual(job_row["status"], "failed")
        self.assertIn("yt-dlp", job_row["error_message"])


class Leases(DistributedTestCase):
    def test_expired_lease_allows_requeue(self) -> None:
        seed_relevant_item(self.conn)
        self.enqueue()
        self.conn.execute(
            "UPDATE transcription_jobs SET leased_until = '2000-01-01T00:00:00+00:00'"
        )
        self.conn.commit()
        stats = self.enqueue()
        self.assertEqual(stats["enqueued"], 1)

    def test_attempts_increment_on_requeue(self) -> None:
        seed_relevant_item(self.conn)
        self.enqueue()
        self.conn.execute(
            "UPDATE transcription_jobs SET leased_until = '2000-01-01T00:00:00+00:00'"
        )
        self.conn.commit()
        self.enqueue()
        attempts = self.conn.execute("SELECT attempts FROM transcription_jobs").fetchone()[0]
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
