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


def verify_generated(settings: radar.Settings) -> None:
    """Compare two isolated builds with every existing production artifact."""
    archive = radar.load_archive(settings.archive_path)
    expected = {name: (settings.public_dir / name).read_bytes() for name in radar.GENERATED_FILES}
    with tempfile.TemporaryDirectory() as directory:
        temporary = replace(settings, public_dir=pathlib.Path(directory))
        for _ in range(2):
            radar.build_site(temporary, archive)
            for name, value in expected.items():
                if (temporary.public_dir / name).read_bytes() != value:
                    raise radar.RadarError(f"generated {name} is stale or nondeterministic; run build-site")


def verify() -> None:
    # Test fixtures must never inherit production reporting or model credentials.
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"AI_RADAR_SENTRY_DSN", "OPENROUTER_API_KEY"}}
    subprocess.run([sys.executable, "radar.py", "doctor"], cwd=ROOT, env=environment, check=True)
    settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json")
    verify_generated(settings)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                   cwd=ROOT, env=environment, check=True)
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
        reporter.capture_exception(exc, tags={"phase": "verification"},
                                   fingerprint=["ai-radar", "verification", type(exc).__name__])
        print(f"error: verification failed: {exc}", file=sys.stderr)
        return 1
    finally:
        reporter.close()
    print("Publisher verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
