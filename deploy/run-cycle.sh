#!/bin/sh
set -eu

cd /app
export GIT_SSH_COMMAND="ssh -i /run/secrets/github_deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/secrets/github_known_hosts"

git pull --ff-only origin main
python3 radar.py run --lookback-days "${AI_RADAR_LOOKBACK_DAYS:-7}"
python3 -m unittest discover -s tests

git add data/items.json public/index.html public/feed.xml public/_headers
if git diff --cached --quiet; then
  echo "AI Radar is already current"
  exit 0
fi

git commit -m "Update AI Radar static feed"
git push origin HEAD:main
