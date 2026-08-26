from __future__ import annotations

import subprocess
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
    "llm_errors": 0,
}


class ScheduledCycleTests(unittest.TestCase):
    def test_success_flushes_the_reporter(self) -> None:
        reporter = RecordingReporter()
        radar = mock.Mock()
        radar.lookback_days_from_env.return_value = 7
        radar.run_cycle.return_value = GOOD_STATS
        with (
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
        self.assertTrue(reporter.closed)

    def test_pipeline_failure_is_reported_with_its_phase(self) -> None:
        reporter = RecordingReporter()
        with mock.patch.object(
            scheduled_cycle,
            "command",
            side_effect=subprocess.CalledProcessError(1, ["git", "pull"]),
        ):
            result = scheduled_cycle.run_scheduled_cycle(reporter)

        self.assertEqual(result, 1)
        self.assertEqual(reporter.exceptions[0]["tags"]["phase"], "git-pull")
        self.assertTrue(reporter.closed)


class RecordingReporter:
    def __init__(self) -> None:
        self.exceptions: list[dict[str, object]] = []
        self.closed = False

    def capture_exception(self, exception: BaseException, **context: object) -> None:
        self.exceptions.append({"exception": exception, **context})

    def close(self) -> None:
        self.closed = True
