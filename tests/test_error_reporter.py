from __future__ import annotations

import os
import pathlib
import unittest
from unittest import mock

import error_reporter


class ErrorReporterTests(unittest.TestCase):
    def test_cron_schedule_uses_the_scheduler_minute(self) -> None:
        with mock.patch.dict(os.environ, {"AI_RADAR_SCHEDULE_MINUTE": "23"}):
            self.assertEqual(error_reporter.cron_schedule(), "23 * * * *")

    def test_cron_schedule_rejects_invalid_values(self) -> None:
        for value in ("nope", "-1", "60"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"AI_RADAR_SCHEDULE_MINUTE": value}
            ):
                self.assertEqual(error_reporter.cron_schedule(), "17 * * * *")

    def test_missing_dsn_is_explicitly_disabled(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            reporter = error_reporter.ErrorReporter.build_from_env(root=pathlib.Path.cwd())
        self.assertFalse(reporter.enabled)
        self.assertIn(error_reporter.SENTRY_DSN_ENV, reporter.status)
