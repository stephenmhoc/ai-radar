from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

from error_reporter import ErrorReporter


ROOT = pathlib.Path(__file__).resolve().parent
HEARTBEAT_PATH = ROOT / "var/scheduler-heartbeat.json"
ALERT_PATH = ROOT / "var/scheduler-watchdog-alerted"
GRACE_PATH = ROOT / "var/scheduler-watchdog-grace"
DEFAULT_MAX_AGE_SECONDS = 9_000


class SchedulerHeartbeatError(RuntimeError):
    pass


def heartbeat_age_seconds(
    path: pathlib.Path | None = None,
    *,
    now: dt.datetime | None = None,
) -> tuple[float | None, dict[str, object]]:
    path = path or HEARTBEAT_PATH
    if not path.exists():
        return None, {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchedulerHeartbeatError(f"scheduler heartbeat could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise SchedulerHeartbeatError("scheduler heartbeat was not a JSON object")
    timestamp = value.get("last_started_at")
    if not isinstance(timestamp, str):
        raise SchedulerHeartbeatError("scheduler heartbeat had no last_started_at timestamp")
    try:
        started = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchedulerHeartbeatError("scheduler heartbeat timestamp was invalid") from exc
    if started.tzinfo is None:
        raise SchedulerHeartbeatError("scheduler heartbeat timestamp had no timezone")
    current = now or dt.datetime.now(dt.timezone.utc)
    return max(0.0, (current - started).total_seconds()), value


def max_age_seconds_from_env() -> int:
    raw = os.environ.get("AI_RADAR_HEARTBEAT_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE_SECONDS))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SchedulerHeartbeatError("AI_RADAR_HEARTBEAT_MAX_AGE_SECONDS must be an integer") from exc
    if value < 3_600:
        raise SchedulerHeartbeatError("AI_RADAR_HEARTBEAT_MAX_AGE_SECONDS must be at least 3600")
    return value


def missing_heartbeat_is_in_grace(
    *,
    max_age_seconds: int,
    path: pathlib.Path | None = None,
) -> bool:
    path = path or GRACE_PATH
    now = dt.datetime.now(dt.timezone.utc)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(now.replace(microsecond=0).isoformat() + "\n", encoding="utf-8")
        return True
    try:
        started = dt.datetime.fromisoformat(path.read_text(encoding="utf-8").strip().replace("Z", "+00:00"))
    except (OSError, ValueError) as exc:
        raise SchedulerHeartbeatError(f"scheduler watchdog grace timestamp was invalid: {exc}") from exc
    if started.tzinfo is None:
        raise SchedulerHeartbeatError("scheduler watchdog grace timestamp had no timezone")
    return (now - started).total_seconds() <= max_age_seconds


def report_once(error: SchedulerHeartbeatError, *, extra: dict[str, object]) -> None:
    if ALERT_PATH.exists():
        return
    reporter = ErrorReporter.build_from_env(root=ROOT)
    try:
        event_id = reporter.capture_exception(
            error,
            tags={"phase": "scheduler-watchdog", "run": "docker-healthcheck"},
            extra=extra,
            fingerprint=["ai-radar", "scheduler", "heartbeat-stale"],
        )
        if event_id:
            ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
            ALERT_PATH.write_text(str(event_id) + "\n", encoding="utf-8")
    finally:
        reporter.close()


def main() -> int:
    max_age: int | None = None
    heartbeat: dict[str, object] = {}
    try:
        max_age = max_age_seconds_from_env()
        age, heartbeat = heartbeat_age_seconds()
        if age is None:
            if missing_heartbeat_is_in_grace(max_age_seconds=max_age):
                print("scheduler heartbeat is awaiting its first scheduled run")
                return 0
            raise SchedulerHeartbeatError("scheduler heartbeat does not exist")
        if age > max_age:
            raise SchedulerHeartbeatError(
                f"scheduler heartbeat is stale ({int(age)} seconds; limit is {max_age})"
            )
    except SchedulerHeartbeatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        report_once(exc, extra={"heartbeat": heartbeat, "max_age_seconds": max_age})
        return 1

    ALERT_PATH.unlink(missing_ok=True)
    GRACE_PATH.unlink(missing_ok=True)
    print(f"scheduler heartbeat age_seconds={int(age)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
