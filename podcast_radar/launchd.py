from __future__ import annotations

import os
import pathlib
import plistlib
import sys

from .config import Config


LABEL = "com.merimeri.ai-radar"


def install(config: Config, *, hour: int, minute: int) -> pathlib.Path:
    log_dir = config.app.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "podcast_radar",
            "--config",
            str(config.root / "config.toml"),
            "run",
        ],
        "WorkingDirectory": str(config.root),
        "RunAtLoad": False,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"),
        },
    }
    target = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        plistlib.dump(plist, fh, sort_keys=False)
    return target
