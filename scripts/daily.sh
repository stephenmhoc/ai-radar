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

deploy_site() {
  if [ -z "${AI_RADAR_DEPLOY_PROJECT:-}" ]; then
    return
  fi
  npm run verify:site
  npx -y wrangler pages deploy public \
    --project-name "$AI_RADAR_DEPLOY_PROJECT" \
    --branch "${AI_RADAR_DEPLOY_BRANCH:-main}"
}

python3 -m podcast_radar --config config.toml ingest --since "$SINCE"
python3 -m podcast_radar --config config.toml judge --since "$SINCE"
python3 -m podcast_radar --config config.toml build-site
deploy_site

python3 -m podcast_radar --config config.toml process --since "$SINCE"
python3 -m podcast_radar --config config.toml build-site
deploy_site
