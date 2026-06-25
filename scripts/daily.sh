#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."
mkdir -p var/logs

if [ -f var/secrets.env ]; then
  set -a
  source var/secrets.env
  set +a
fi

LOOKBACK_HOURS=${AI_RADAR_LOOKBACK_HOURS:-36}
SINCE=$(
  python3 - <<'PY'
import datetime as dt
import os

hours = int(os.environ.get("AI_RADAR_LOOKBACK_HOURS", "36"))
cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
print(cutoff.replace(microsecond=0).isoformat())
PY
)

export PYTHONDONTWRITEBYTECODE=1

python3 -m podcast_radar --config config.toml run --since "$SINCE"

if [ -n "${AI_RADAR_DEPLOY_PROJECT:-}" ]; then
  npx -y wrangler pages deploy public \
    --project-name "$AI_RADAR_DEPLOY_PROJECT" \
    --branch "${AI_RADAR_DEPLOY_BRANCH:-main}"
fi
