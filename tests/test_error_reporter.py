from __future__ import annotations

import pathlib
import unittest
from unittest import mock

import error_reporter


class ErrorReporterTests(unittest.TestCase):
    def test_missing_dsn_is_explicitly_disabled(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            reporter = error_reporter.ErrorReporter.build_from_env(root=pathlib.Path.cwd())
        self.assertFalse(reporter.enabled)
        self.assertIn(error_reporter.SENTRY_DSN_ENV, reporter.status)
