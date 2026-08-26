from __future__ import annotations

import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import scheduler_watchdog


class SchedulerWatchdogTests(unittest.TestCase):
    def test_fresh_heartbeat_is_healthy_and_clears_prior_alert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            heartbeat = root / "heartbeat.json"
            alert = root / "alerted"
            grace = root / "grace"
            heartbeat.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "state": "finished",
                        "last_started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            alert.write_text("old-event\n", encoding="utf-8")
            grace.write_text("2026-01-01T00:00:00+00:00\n", encoding="utf-8")
            with (
                mock.patch.object(scheduler_watchdog, "HEARTBEAT_PATH", heartbeat),
                mock.patch.object(scheduler_watchdog, "ALERT_PATH", alert),
                mock.patch.object(scheduler_watchdog, "GRACE_PATH", grace),
            ):
                result = scheduler_watchdog.main()
            self.assertEqual(result, 0)
            self.assertFalse(alert.exists())
            self.assertFalse(grace.exists())

    def test_stale_heartbeat_reports_to_sentry_only_once_per_outage(self) -> None:
        reporter = RecordingReporter()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            heartbeat = root / "heartbeat.json"
            alert = root / "alerted"
            grace = root / "grace"
            heartbeat.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "state": "finished",
                        "last_started_at": (
                            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4)
                        ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(scheduler_watchdog, "HEARTBEAT_PATH", heartbeat),
                mock.patch.object(scheduler_watchdog, "ALERT_PATH", alert),
                mock.patch.object(scheduler_watchdog, "GRACE_PATH", grace),
                mock.patch.object(
                    scheduler_watchdog.ErrorReporter,
                    "build_from_env",
                    return_value=reporter,
                ),
            ):
                self.assertEqual(scheduler_watchdog.main(), 1)
                self.assertEqual(scheduler_watchdog.main(), 1)
            self.assertTrue(alert.exists())
        self.assertEqual(len(reporter.exceptions), 1)
        self.assertEqual(
            reporter.exceptions[0]["fingerprint"],
            ["ai-radar", "scheduler", "heartbeat-stale"],
        )

    def test_missing_heartbeat_has_an_initial_grace_period(self) -> None:
        reporter = RecordingReporter()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with (
                mock.patch.object(scheduler_watchdog, "HEARTBEAT_PATH", root / "missing.json"),
                mock.patch.object(scheduler_watchdog, "ALERT_PATH", root / "alerted"),
                mock.patch.object(scheduler_watchdog, "GRACE_PATH", root / "grace"),
                mock.patch.object(
                    scheduler_watchdog.ErrorReporter,
                    "build_from_env",
                    return_value=reporter,
                ),
            ):
                self.assertEqual(scheduler_watchdog.main(), 0)
        self.assertEqual(len(reporter.exceptions), 0)

    def test_missing_heartbeat_after_grace_is_unhealthy(self) -> None:
        reporter = RecordingReporter()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            grace = root / "grace"
            grace.write_text(
                (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4)).isoformat(),
                encoding="utf-8",
            )
            with (
                mock.patch.object(scheduler_watchdog, "HEARTBEAT_PATH", root / "missing.json"),
                mock.patch.object(scheduler_watchdog, "ALERT_PATH", root / "alerted"),
                mock.patch.object(scheduler_watchdog, "GRACE_PATH", grace),
                mock.patch.object(
                    scheduler_watchdog.ErrorReporter,
                    "build_from_env",
                    return_value=reporter,
                ),
            ):
                self.assertEqual(scheduler_watchdog.main(), 1)
        self.assertEqual(len(reporter.exceptions), 1)


class RecordingReporter:
    def __init__(self) -> None:
        self.exceptions: list[dict[str, object]] = []
        self.closed = False

    def capture_exception(self, exception: BaseException, **context: object) -> str:
        self.exceptions.append({"exception": exception, **context})
        return "event-id"

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
