from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
from typing import Any


SENTRY_DSN_ENV = "AI_RADAR_SENTRY_DSN"
SENTRY_ENVIRONMENT_ENV = "AI_RADAR_SENTRY_ENVIRONMENT"
SENTRY_RELEASE_ENV = "AI_RADAR_SENTRY_RELEASE"
MONITOR_SLUG = "ai-radar-hourly"


class ErrorReporter:
    def __init__(self, sdk: Any | None, *, status: str) -> None:
        self._sdk = sdk
        self.status = status

    @property
    def enabled(self) -> bool:
        return self._sdk is not None

    @classmethod
    def build_from_env(cls, *, root: pathlib.Path) -> ErrorReporter:
        dsn = os.environ.get(SENTRY_DSN_ENV, "").strip()
        if not dsn:
            return cls(None, status=f"disabled ({SENTRY_DSN_ENV} is not set)")

        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=dsn,
                environment=os.environ.get(SENTRY_ENVIRONMENT_ENV, "production").strip() or "production",
                release=sentry_release(root),
                send_default_pii=False,
                traces_sample_rate=0.0,
            )
            sentry_sdk.set_tags({"app": "ai-radar", "host": socket.gethostname()})
            return cls(sentry_sdk, status="enabled")
        except Exception as exc:  # noqa: BLE001 - reporting must never hide the original work
            print(f"warning: Sentry failed to initialize: {exc}", file=sys.stderr)
            return cls(None, status=f"disabled (initialization failed: {exc})")

    def capture_exception(
        self,
        exception: BaseException,
        *,
        tags: dict[str, object] | None = None,
        extra: dict[str, object] | None = None,
        fingerprint: list[str] | None = None,
    ) -> None:
        if self._sdk is None:
            return
        try:
            with self._sdk.push_scope() as scope:
                for key, value in (tags or {}).items():
                    scope.set_tag(key, value)
                for key, value in (extra or {}).items():
                    scope.set_extra(key, value)
                if fingerprint:
                    scope.fingerprint = fingerprint
                self._sdk.capture_exception(exception)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: Sentry failed to capture an exception: {exc}", file=sys.stderr)

    def start_check_in(self) -> str | None:
        if self._sdk is None:
            return None
        try:
            from sentry_sdk.crons import capture_checkin

            return capture_checkin(
                monitor_slug=MONITOR_SLUG,
                status="in_progress",
                monitor_config={
                    "schedule": {"type": "crontab", "value": cron_schedule()},
                    "timezone": os.environ.get("TZ", "America/New_York"),
                    "checkin_margin": 15,
                    "max_runtime": 50,
                    "failure_issue_threshold": 1,
                    "recovery_threshold": 1,
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"warning: Sentry failed to start the scheduled check-in: {exc}", file=sys.stderr)
            return None

    def finish_check_in(
        self,
        check_in_id: str | None,
        *,
        ok: bool,
        duration: float,
    ) -> None:
        if self._sdk is None or check_in_id is None:
            return
        try:
            from sentry_sdk.crons import capture_checkin

            capture_checkin(
                monitor_slug=MONITOR_SLUG,
                check_in_id=check_in_id,
                status="ok" if ok else "error",
                duration=duration,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"warning: Sentry failed to finish the scheduled check-in: {exc}", file=sys.stderr)

    def close(self) -> None:
        if self._sdk is None:
            return
        try:
            self._sdk.flush(timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: Sentry failed to flush events: {exc}", file=sys.stderr)


def cron_schedule() -> str:
    raw = os.environ.get("AI_RADAR_SCHEDULE_MINUTE", "17").strip()
    try:
        minute = int(raw)
    except ValueError:
        minute = 17
    if not 0 <= minute <= 59:
        minute = 17
    return f"{minute} * * * *"


def sentry_release(root: pathlib.Path) -> str | None:
    explicit = os.environ.get(SENTRY_RELEASE_ENV, "").strip()
    if explicit:
        return explicit
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
