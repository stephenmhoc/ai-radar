from __future__ import annotations

import pathlib
import subprocess
import sys

from error_reporter import ErrorReporter


ROOT = pathlib.Path(__file__).resolve().parent


class DegradedCycleError(RuntimeError):
    pass


def command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, check=True, text=True)


def load_radar_module():
    # Import after the pull so this cycle executes the newly checked-out
    # collector rather than the copy that existed when the container started.
    import radar

    return radar


def run_scheduled_cycle(reporter: ErrorReporter) -> int:
    phase = "git-pull"
    try:
        command("git", "pull", "--ff-only", "origin", "main")

        phase = "collect"
        radar = load_radar_module()
        settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json")
        lookback_days = radar.lookback_days_from_env()
        stats = radar.run_cycle(settings, lookback_days=lookback_days, reporter=reporter)
        radar.print_stats(stats)
        degraded = stats["source_errors"] > 0 or stats["llm_errors"] > 0

        phase = "tests"
        command(sys.executable, "-m", "unittest", "discover", "-s", "tests")

        phase = "git-stage"
        command("git", "add", "data/items.json", "public/index.html", "public/feed.xml", "public/_headers")
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=ROOT,
            check=False,
            text=True,
        )
        if diff.returncode == 1:
            phase = "git-commit"
            command("git", "commit", "-m", "Update AI Radar static feed")
            phase = "git-push"
            command("git", "push", "origin", "HEAD:main")
        elif diff.returncode == 0:
            print("AI Radar is already current")
        else:
            raise subprocess.CalledProcessError(diff.returncode, diff.args)

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
    finally:
        reporter.close()


def main() -> int:
    reporter = ErrorReporter.build_from_env(root=ROOT)
    return run_scheduled_cycle(reporter)


if __name__ == "__main__":
    raise SystemExit(main())
