#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."
mkdir -p var/logs

export PYTHONDONTWRITEBYTECODE=1

python3 -m podcast_radar --config config.toml build-site
npm run verify:site
npx -y wrangler pages deploy public --project-name ai-radar --branch main

python3 -m podcast_radar --config config.toml process --since 2026-01-01
python3 -m podcast_radar --config config.toml build-site
npm run verify:site
npx -y wrangler pages deploy public --project-name ai-radar --branch main
