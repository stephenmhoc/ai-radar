from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import pathlib
import subprocess
import sys
import time
from collections.abc import Iterator

from error_reporter import ErrorReporter


ROOT = pathlib.Path(__file__).resolve().parent
LOCK_PATH = pathlib.Path("/tmp/ai-radar-publication.lock")
HEARTBEAT_PATH = ROOT / "var/scheduler-heartbeat.json"
PUSH_ATTEMPTS = 3


class DegradedCycleError(RuntimeError):
    pass


class PublicationLockedError(RuntimeError):
    pass


def write_heartbeat(state: str, *, exit_code: int | None = None, path: pathlib.Path = HEARTBEAT_PATH) -> None:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    value: dict[str, object] = {"version": 1}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                value.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    value["state"] = state
    if state == "running":
        value["last_started_at"] = now
    else:
        value["last_finished_at"] = now
        value["last_exit_code"] = exit_code
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def command(*args: str, root: pathlib.Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, check=True, text=True)


def command_output(*args: str, root: pathlib.Path = ROOT) -> str:
    result = subprocess.run(
        args,
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@contextlib.contextmanager
def publication_lock(path: pathlib.Path = LOCK_PATH) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublicationLockedError("another AI Radar publication cycle is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_clean_worktree(*, root: pathlib.Path = ROOT) -> None:
    status = command_output("git", "status", "--porcelain", "--untracked-files=no", root=root)
    if status:
        raise RuntimeError("worktree has tracked changes from an incomplete prior cycle")


def ahead_behind(*, root: pathlib.Path = ROOT) -> tuple[int, int]:
    raw = command_output(
        "git",
        "rev-list",
        "--left-right",
        "--count",
        "HEAD...origin/main",
        root=root,
    )
    ahead, behind = raw.split()
    return int(ahead), int(behind)


def reconcile_with_remote(*, root: pathlib.Path = ROOT) -> tuple[int, int]:
    command("git", "fetch", "origin", "main", root=root)
    ahead, behind = ahead_behind(root=root)
    if behind and ahead:
        result = subprocess.run(
            ["git", "rebase", "origin/main"],
            cwd=root,
            check=False,
            text=True,
        )
        if result.returncode:
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            raise subprocess.CalledProcessError(result.returncode, result.args)
    elif behind:
        command("git", "merge", "--ff-only", "origin/main", root=root)
    return ahead_behind(root=root)


def publish_ahead_commits(*, root: pathlib.Path = ROOT) -> None:
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        ahead, behind = reconcile_with_remote(root=root)
        if behind:
            raise RuntimeError("remote reconciliation left the checkout behind origin/main")
        if not ahead:
            return
        result = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            cwd=root,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            return
        last_error = subprocess.CalledProcessError(result.returncode, result.args)
        if attempt < PUSH_ATTEMPTS:
            delay = 2 ** (attempt - 1)
            print(f"warning: Git push failed; retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def load_radar_module():
    # Import after Git reconciliation so this cycle executes the freshly pulled
    # collector rather than the copy that existed when the container started.
    import radar

    return radar


def _run_locked_cycle(reporter: ErrorReporter) -> int:
    phase = "git-sync"
    try:
        ensure_clean_worktree()
        publish_ahead_commits()
        release = command_output("git", "rev-parse", "HEAD")
        reporter.set_tags({"worktree_release": release})

        phase = "collect"
        radar = load_radar_module()
        settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json")
        lookback_days = radar.lookback_days_from_env()
        stats = radar.run_cycle(settings, lookback_days=lookback_days, reporter=reporter)
        radar.print_stats(stats)
        degraded = stats["source_errors"] > 0 or stats["llm_errors"] > 0

        phase = "tests"
        command(sys.executable, "radar.py", "doctor")
        command(sys.executable, "-m", "unittest", "discover", "-s", "tests")
        command(
            sys.executable,
            "-m",
            "py_compile",
            "radar.py",
            "scheduled_cycle.py",
            "scheduler_watchdog.py",
            "error_reporter.py",
            "scripts/check_archive_evolution.py",
        )

        phase = "git-stage"
        command(
            "git",
            "add",
            "data/items.json",
            "public/index.html",
            "public/feeds.html",
            "public/feed.xml",
            "public/_headers",
        )
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=ROOT,
            check=False,
            text=True,
        )
        if diff.returncode == 1:
            phase = "git-commit"
            command("git", "commit", "-m", "Update AI Radar static feed")
        elif diff.returncode == 0:
            print("AI Radar generated content is already current")
        else:
            raise subprocess.CalledProcessError(diff.returncode, diff.args)

        phase = "git-publish"
        publish_ahead_commits()

        if degraded:
            raise DegradedCycleError(
                f"cycle completed with source_errors={stats['source_errors']} "
                f"and llm_errors={stats['llm_errors']}"
            )
        return 0
    except Exception as exc:  # noqa: BLE001 - every scheduled failure must reach Sentry
        if not isinstance(exc, DegradedCycleError):
            reporter.capture_exception(
                exc,
                tags={"phase": phase, "run": "scheduled"},
                fingerprint=["ai-radar", "scheduled", phase],
            )
        print(f"error: scheduled cycle failed during {phase}: {exc}", file=sys.stderr)
        return 1


def run_scheduled_cycle(reporter: ErrorReporter) -> int:
    result = 1
    heartbeat_started = False
    try:
        with publication_lock():
            write_heartbeat("running")
            heartbeat_started = True
            result = _run_locked_cycle(reporter)
    except PublicationLockedError as exc:
        reporter.capture_exception(
            exc,
            tags={"phase": "lock", "run": "scheduled"},
            fingerprint=["ai-radar", "scheduled", "lock"],
        )
        print(f"error: {exc}", file=sys.stderr)
        result = 1
    except Exception as exc:  # noqa: BLE001 - heartbeat failures must be visible
        reporter.capture_exception(
            exc,
            tags={"phase": "heartbeat", "run": "scheduled"},
            fingerprint=["ai-radar", "scheduled", "heartbeat"],
        )
        print(f"error: scheduled heartbeat failed: {exc}", file=sys.stderr)
        result = 1
    finally:
        if heartbeat_started:
            try:
                write_heartbeat("finished", exit_code=result)
            except Exception as exc:  # noqa: BLE001
                reporter.capture_exception(
                    exc,
                    tags={"phase": "heartbeat", "run": "scheduled"},
                    fingerprint=["ai-radar", "scheduled", "heartbeat"],
                )
                print(f"error: failed to finish scheduler heartbeat: {exc}", file=sys.stderr)
                result = 1
        reporter.close()
    return result


def main() -> int:
    reporter = ErrorReporter.build_from_env(root=ROOT)
    return run_scheduled_cycle(reporter)


if __name__ == "__main__":
    raise SystemExit(main())
