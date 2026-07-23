#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."
mkdir -p var/logs

NODE_RUNTIME_PATH=${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}
for runtime_dir in "${HOME:-}/.local/share/mise/shims" "${HOME:-}/.local/bin"; do
  if [ -n "${HOME:-}" ] && [ -d "$runtime_dir" ]; then
    NODE_RUNTIME_PATH="$NODE_RUNTIME_PATH:$runtime_dir"
  fi
done
if [ -n "${AI_RADAR_DEPLOY_PROJECT:-}" ] && ! (
  export PATH="$NODE_RUNTIME_PATH"
  command -v node >/dev/null && command -v npm >/dev/null && command -v npx >/dev/null
); then
  echo "ai-radar deploy requires node, npm, and npx; checked PATH=$NODE_RUNTIME_PATH" >&2
  exit 127
fi

LOCK_DIR="var/run/daily.lock"
LOCK_MAX_AGE_HOURS=${AI_RADAR_LOCK_MAX_AGE_HOURS:-24}
mkdir -p var/run
python3 - "$LOCK_DIR" "$LOCK_MAX_AGE_HOURS" <<'PY'
import datetime as dt
import os
import pathlib
import shutil
import sys

lock_dir = pathlib.Path(sys.argv[1])
max_age_hours = float(sys.argv[2])
if not lock_dir.exists() or max_age_hours <= 0:
    raise SystemExit

now = dt.datetime.now(dt.timezone.utc)
pid_path = lock_dir / "pid"
if pid_path.is_file():
    try:
        owner_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        owner_pid = None
    if owner_pid and owner_pid > 0:
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            shutil.rmtree(lock_dir)
            print(f"removed orphaned ai-radar lock for pid {owner_pid}")
            raise SystemExit
        except PermissionError:
            raise SystemExit
        else:
            raise SystemExit

started_at = None
timestamp_path = lock_dir / "started_at"
if timestamp_path.is_file():
    try:
        value = timestamp_path.read_text(encoding="utf-8").strip()
        started_at = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.timezone.utc)
    except (OSError, ValueError):
        started_at = None

if started_at is None:
    try:
        started_at = dt.datetime.fromtimestamp(lock_dir.stat().st_mtime, tz=dt.timezone.utc)
    except FileNotFoundError:
        raise SystemExit

age = now - started_at
if age.total_seconds() > max_age_hours * 3600:
    shutil.rmtree(lock_dir)
    print(f"removed stale ai-radar lock from {started_at.isoformat()} (age {age})")
PY
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "ai-radar run already in progress; skipping"
  exit 0
fi
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$LOCK_DIR/started_at"
print -r -- "$$" > "$LOCK_DIR/pid"
cleanup_lock() {
  rm -f "$LOCK_DIR/pid"
  rm -f "$LOCK_DIR/started_at"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT

set +e
(
  set -euo pipefail

  if [ -f var/secrets.env ]; then
    set -a
    source var/secrets.env
    set +a
  fi

  LOOKBACK_HOURS=${AI_RADAR_LOOKBACK_HOURS:-2}
  SINCE=$(
    python3 - <<'PY'
import datetime as dt
import os

hours = int(os.environ.get("AI_RADAR_LOOKBACK_HOURS", "2"))
cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
print(cutoff.replace(microsecond=0).isoformat())
PY
  )

  export PYTHONDONTWRITEBYTECODE=1

  deploy_site() {
    if [ -z "${AI_RADAR_DEPLOY_PROJECT:-}" ]; then
      return
    fi
    PATH="$NODE_RUNTIME_PATH" npm run verify:site
    PATH="$NODE_RUNTIME_PATH" npx -y wrangler pages deploy public \
      --project-name "$AI_RADAR_DEPLOY_PROJECT" \
      --branch "${AI_RADAR_DEPLOY_BRANCH:-main}"
  }

  python3 -m podcast_radar --config config.toml ingest --since "$SINCE"
  python3 -m podcast_radar --config config.toml judge --since "$SINCE"
  python3 -m podcast_radar --config config.toml process --since "$SINCE"
  python3 -m podcast_radar --config config.toml build-site
  deploy_site
)
RUN_STATUS=$?
cleanup_lock
trap - EXIT
if [ "$RUN_STATUS" -ne 0 ]; then
  echo "ai-radar run failed with exit status $RUN_STATUS" >&2
fi
exit "$RUN_STATUS"
