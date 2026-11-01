"""Shared, offline verification for local work, CI, and scheduled publication."""
from __future__ import annotations

import os
import pathlib
import py_compile
import subprocess
import sys
import tempfile
from dataclasses import replace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402
from error_reporter import ErrorReporter  # noqa: E402

MAX_REPORTED_OUTPUT_CHARS = 4000


def verify_generated(settings: radar.Settings) -> None:
    """Compare two isolated builds with every existing production artifact."""
    archive = radar.load_archive(settings.archive_path)
    existing = radar.generated_paths(settings.public_dir)
    expected = {name: (settings.public_dir / name).read_bytes() for name in existing}
    with tempfile.TemporaryDirectory() as directory:
        temporary = replace(settings, public_dir=pathlib.Path(directory))
        for _ in range(2):
            radar.build_site(temporary, archive)
            built = radar.generated_paths(temporary.public_dir)
            if built != existing:
                changed = sorted(built ^ existing)
                raise radar.RadarError(f"generated {changed[0]} is stale or nondeterministic; run build-site")
            for name, value in expected.items():
                if (temporary.public_dir / name).read_bytes() != value:
                    raise radar.RadarError(f"generated {name} is stale or nondeterministic; run build-site")


def run_recording_output(args: list[str], environment: dict[str, str]) -> None:
    """Run a check, keeping its output on the failure so the report names what broke."""
    result = subprocess.run(args, cwd=ROOT, env=environment, check=False, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sys.stdout.write(result.stdout)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, output=result.stdout)


def failure_context(exc: Exception) -> dict[str, object]:
    output = getattr(exc, "output", None)
    return {"output_tail": output[-MAX_REPORTED_OUTPUT_CHARS:]} if isinstance(output, str) else {}


def verify() -> None:
    # Test fixtures must never inherit production reporting or model credentials.
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"AI_RADAR_SENTRY_DSN", "OPENROUTER_API_KEY"}}
    run_recording_output([sys.executable, "radar.py", "doctor"], environment)
    settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json")
    verify_generated(settings)
    run_recording_output([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], environment)
    for directory in (ROOT, ROOT / "scripts", ROOT / "tests"):
        for path in sorted(directory.glob("*.py")):
            py_compile.compile(str(path), doraise=True)
    if (ROOT / ".git").exists():
        subprocess.run(["git", "diff", "--check", "HEAD"], cwd=ROOT, check=True)


def main() -> int:
    reporter = ErrorReporter.build_from_env(root=ROOT)
    try:
        verify()
    except Exception as exc:  # noqa: BLE001 - standalone verification failures must reach Sentry
        reporter.capture_exception(exc, tags={"phase": "verification"}, extra=failure_context(exc),
                                   fingerprint=["ai-radar", "verification", type(exc).__name__])
        print(f"error: verification failed: {exc}", file=sys.stderr)
        return 1
    finally:
        reporter.close()
    print("Publisher verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
