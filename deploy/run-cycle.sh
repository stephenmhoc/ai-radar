#!/bin/sh
# One ai-radar cycle on the homelab coordinator.
#
# Differs from scripts/daily.sh (the macOS version) in two ways:
#   - transcription is dispatched to a Mac worker instead of run inline
#   - `npm run verify:site` is not run here; it stays on the Mac
#
# The locking daily.sh does is handled by systemd instead: the timer's service is
# Type=oneshot, and systemd will not start a second instance while one is active.
set -eu

cd /app

CONFIG=${AI_RADAR_CONFIG:-/app/config.toml}
LOOKBACK_HOURS=${AI_RADAR_LOOKBACK_HOURS:-2}
BACKLOG_JUDGE_LIMIT=${AI_RADAR_BACKLOG_JUDGE_LIMIT:-10}
BACKLOG_PROCESS_LIMIT=${AI_RADAR_BACKLOG_PROCESS_LIMIT:-1}

SINCE=$(python3 - <<PY
import datetime as dt
cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=${LOOKBACK_HOURS})
print(cutoff.replace(microsecond=0).isoformat())
PY
)

radar() {
  python3 -m podcast_radar --config "$CONFIG" "$@"
}

echo "--- cycle since $SINCE"

# 1. Collect transcripts the Mac finished since the last cycle. Doing this first
#    means anything that came back is summarizable in this same run.
echo "--- import-transcripts"
radar import-transcripts

echo "--- ingest"
radar ingest --since "$SINCE"

echo "--- judge"
radar judge --since "$SINCE"

# 2. Queue newly relevant media and wake the worker. The Mac then transcribes
#    while this container spends the next few minutes on LLM summarization.
echo "--- dispatch-transcriptions"
radar dispatch-transcriptions --since "$SINCE"

echo "--- process"
radar process --since "$SINCE"

if [ "$BACKLOG_JUDGE_LIMIT" -gt 0 ]; then
  echo "--- judge backlog"
  radar judge --limit "$BACKLOG_JUDGE_LIMIT"
fi
if [ "$BACKLOG_PROCESS_LIMIT" -gt 0 ]; then
  echo "--- process backlog"
  radar process --limit "$BACKLOG_PROCESS_LIMIT"
fi

# 3. Dispatch again: the backlog pass can surface newly relevant items.
echo "--- dispatch-transcriptions (backlog)"
radar dispatch-transcriptions

echo "--- build-site"
radar build-site

if [ -n "${AI_RADAR_DEPLOY_PROJECT:-}" ]; then
  if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
    echo "CLOUDFLARE_API_TOKEN is unset; built the site but skipping deploy" >&2
    exit 0
  fi
  echo "--- deploy"
  wrangler pages deploy public \
    --project-name "$AI_RADAR_DEPLOY_PROJECT" \
    --branch "${AI_RADAR_DEPLOY_BRANCH:-main}"
fi

echo "--- queue status"
radar queue-status
