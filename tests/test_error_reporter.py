from __future__ import annotations

import pathlib
import sys
import types
import unittest
from unittest import mock

import error_reporter


class ErrorReporterTests(unittest.TestCase):
    def test_missing_dsn_is_explicitly_disabled(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            reporter = error_reporter.ErrorReporter.build_from_env(root=pathlib.Path.cwd())
        self.assertFalse(reporter.enabled)
        self.assertIn(error_reporter.SENTRY_DSN_ENV, reporter.status)

    def test_events_never_carry_frame_locals_or_default_pii(self) -> None:
        sdk = types.SimpleNamespace(init=mock.Mock(), set_tags=mock.Mock())
        with mock.patch.dict("os.environ", {error_reporter.SENTRY_DSN_ENV: "https://k@example.invalid/1"}), \
             mock.patch.dict(sys.modules, {"sentry_sdk": sdk}):
            reporter = error_reporter.ErrorReporter.build_from_env(root=pathlib.Path.cwd())
        self.assertTrue(reporter.enabled)
        options = sdk.init.call_args.kwargs
        self.assertIs(options["include_local_variables"], False)
        self.assertIs(options["send_default_pii"], False)
        self.assertEqual(options["traces_sample_rate"], 0.0)

    def test_capture_tags_and_flush_use_sdk_without_masking_work(self) -> None:
        sdk = FakeSDK()
        reporter = error_reporter.ErrorReporter(sdk, status="enabled")
        reporter.set_tags({"worktree_release": "abc123"})
        event_id = reporter.capture_exception(
            RuntimeError("broken"),
            tags={"phase": "tests"},
            extra={"detail": "value"},
            fingerprint=["ai-radar", "tests"],
        )
        reporter.close()

        self.assertEqual(event_id, "event-id")
        self.assertEqual(sdk.tags, {"worktree_release": "abc123"})
        self.assertEqual(sdk.scope.tags, {"phase": "tests"})
        self.assertEqual(sdk.scope.extra, {"detail": "value"})
        self.assertEqual(sdk.scope.fingerprint, ["ai-radar", "tests"])
        self.assertTrue(sdk.flushed)


class FakeScope:
    def __init__(self) -> None:
        self.tags: dict[str, object] = {}
        self.extra: dict[str, object] = {}
        self.fingerprint: list[str] = []

    def set_tag(self, key: str, value: object) -> None:
        self.tags[key] = value

    def set_extra(self, key: str, value: object) -> None:
        self.extra[key] = value


class FakeContext:
    def __init__(self, scope: FakeScope) -> None:
        self.scope = scope

    def __enter__(self) -> FakeScope:
        return self.scope

    def __exit__(self, *args: object) -> None:
        return None


class FakeSDK:
    def __init__(self) -> None:
        self.scope = FakeScope()
        self.tags: dict[str, object] = {}
        self.flushed = False

    def set_tag(self, key: str, value: object) -> None:
        self.tags[key] = value

    def new_scope(self) -> FakeContext:
        return FakeContext(self.scope)

    def capture_exception(self, _exception: BaseException) -> str:
        return "event-id"

    def flush(self, *, timeout: float) -> None:
        self.flushed = timeout == 5.0
