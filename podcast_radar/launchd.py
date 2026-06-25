from __future__ import annotations

import pathlib
import plistlib

from .config import Config


LABEL = "com.merimeri.ai-radar"


def install(
    config: Config,
    *,
    hour: int,
    minute: int,
    lookback_hours: int,
    deploy_project: str,
    deploy_branch: str,
) -> pathlib.Path:
    log_dir = config.app.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    runner = config.root / "scripts" / "daily.sh"
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            "/bin/zsh",
            str(runner),
        ],
        "WorkingDirectory": str(config.root),
        "RunAtLoad": True,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "AI_RADAR_LOOKBACK_HOURS": str(lookback_hours),
            "AI_RADAR_DEPLOY_PROJECT": deploy_project,
            "AI_RADAR_DEPLOY_BRANCH": deploy_branch,
        },
    }
    target = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        plistlib.dump(plist, fh, sort_keys=False)
    return target
