from __future__ import annotations

import contextlib
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import scheduled_cycle


GOOD_STATS = {
    "sources": 1,
    "source_errors": 0,
    "new_appearances": 0,
    "matched_appearances": 0,
    "new_items": 0,
    "published": 0,
    "skipped": 0,
    "deferred": 0,
    "reevaluated": 0,
    "llm_errors": 0,
}


def git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def configure_repo(root: pathlib.Path) -> None:
    git(root, "config", "user.name", "Test Publisher")
    git(root, "config", "user.email", "publisher@example.invalid")


class ScheduledCycleTests(unittest.TestCase):
    def test_success_flushes_reporter_and_updates_release_tag(self) -> None:
        reporter = RecordingReporter()
        radar = mock.Mock()
        radar.lookback_days_from_env.return_value = 7
        radar.run_cycle.return_value = GOOD_STATS
        with (
            mock.patch.object(scheduled_cycle, "write_heartbeat"),
            mock.patch.object(scheduled_cycle, "publication_lock", return_value=contextlib.nullcontext()),
            mock.patch.object(scheduled_cycle, "ensure_clean_worktree"),
            mock.patch.object(scheduled_cycle, "publish_ahead_commits"),
            mock.patch.object(scheduled_cycle, "command_output", return_value="abc123"),
            mock.patch.object(scheduled_cycle, "command"),
            mock.patch.object(scheduled_cycle, "load_radar_module", return_value=radar),
            mock.patch.object(
                scheduled_cycle.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ),
        ):
            result = scheduled_cycle.run_scheduled_cycle(reporter)

        self.assertEqual(result, 0)
        self.assertEqual(reporter.tags, {"worktree_release": "abc123"})
        self.assertTrue(reporter.closed)

    def test_pipeline_failure_is_reported_with_phase(self) -> None:
        reporter = RecordingReporter()
        with (
            mock.patch.object(scheduled_cycle, "write_heartbeat"),
            mock.patch.object(scheduled_cycle, "publication_lock", return_value=contextlib.nullcontext()),
            mock.patch.object(scheduled_cycle, "ensure_clean_worktree", side_effect=RuntimeError("dirty")),
        ):
            result = scheduled_cycle.run_scheduled_cycle(reporter)

        self.assertEqual(result, 1)
        self.assertEqual(reporter.exceptions[0]["tags"]["phase"], "git-sync")
        self.assertTrue(reporter.closed)

    def test_lock_failure_is_reported(self) -> None:
        reporter = RecordingReporter()
        with (
            mock.patch.object(scheduled_cycle, "write_heartbeat"),
            mock.patch.object(
                scheduled_cycle,
                "publication_lock",
                side_effect=scheduled_cycle.PublicationLockedError("locked"),
            ),
        ):
            result = scheduled_cycle.run_scheduled_cycle(reporter)
        self.assertEqual(result, 1)
        self.assertEqual(reporter.exceptions[0]["tags"]["phase"], "lock")

    def test_heartbeat_is_atomic_and_preserves_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "heartbeat.json"
            scheduled_cycle.write_heartbeat("running", path=path)
            scheduled_cycle.write_heartbeat("finished", exit_code=1, path=path)
            value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "finished")
        self.assertEqual(value["last_exit_code"], 1)
        self.assertIn("last_started_at", value)
        self.assertIn("last_finished_at", value)

    def test_publication_lock_blocks_a_second_process_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "cycle.lock"
            with scheduled_cycle.publication_lock(path):
                with self.assertRaises(scheduled_cycle.PublicationLockedError):
                    with scheduled_cycle.publication_lock(path):
                        pass


class GitTransactionTests(unittest.TestCase):
    def test_transient_push_failure_is_retried(self) -> None:
        with (
            mock.patch.object(scheduled_cycle, "reconcile_with_remote", return_value=(1, 0)),
            mock.patch.object(
                scheduled_cycle.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 1),
                    subprocess.CompletedProcess([], 0),
                ],
            ) as run,
            mock.patch.object(scheduled_cycle.time, "sleep"),
        ):
            scheduled_cycle.publish_ahead_commits()
        self.assertEqual(run.call_count, 2)

    def test_existing_ahead_commit_is_pushed_without_new_generated_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            publisher = root / "publisher"
            subprocess.run(["git", "clone", remote, publisher], check=True, capture_output=True)
            configure_repo(publisher)
            (publisher / "base.txt").write_text("base\n", encoding="utf-8")
            git(publisher, "add", "base.txt")
            git(publisher, "commit", "-m", "base")
            git(publisher, "branch", "-M", "main")
            git(publisher, "push", "-u", "origin", "main")
            (publisher / "publication.txt").write_text("publication\n", encoding="utf-8")
            git(publisher, "add", "publication.txt")
            git(publisher, "commit", "-m", "publication")

            scheduled_cycle.publish_ahead_commits(root=publisher)

            self.assertEqual(
                git(publisher, "rev-parse", "HEAD"),
                git(remote, "rev-parse", "refs/heads/main"),
            )

    def test_concurrent_remote_commit_is_rebased_without_force_push(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            publisher = root / "publisher"
            subprocess.run(["git", "clone", remote, publisher], check=True, capture_output=True)
            configure_repo(publisher)
            (publisher / "base.txt").write_text("base\n", encoding="utf-8")
            git(publisher, "add", "base.txt")
            git(publisher, "commit", "-m", "base")
            git(publisher, "branch", "-M", "main")
            git(publisher, "push", "-u", "origin", "main")

            other = root / "other"
            subprocess.run(["git", "clone", "--branch", "main", remote, other], check=True, capture_output=True)
            configure_repo(other)
            (publisher / "publisher.txt").write_text("publisher\n", encoding="utf-8")
            git(publisher, "add", "publisher.txt")
            git(publisher, "commit", "-m", "publisher")
            (other / "human.txt").write_text("human\n", encoding="utf-8")
            git(other, "add", "human.txt")
            git(other, "commit", "-m", "human")
            git(other, "push", "origin", "main")

            scheduled_cycle.publish_ahead_commits(root=publisher)

            self.assertTrue((publisher / "human.txt").exists())
            self.assertTrue((publisher / "publisher.txt").exists())
            self.assertEqual(
                git(publisher, "rev-parse", "HEAD"),
                git(remote, "rev-parse", "refs/heads/main"),
            )


class RecordingReporter:
    def __init__(self) -> None:
        self.exceptions: list[dict[str, object]] = []
        self.tags: dict[str, object] = {}
        self.closed = False

    def capture_exception(self, exception: BaseException, **context: object) -> None:
        self.exceptions.append({"exception": exception, **context})

    def set_tags(self, tags: dict[str, object]) -> None:
        self.tags.update(tags)

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
