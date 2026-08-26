# AI Radar

AI Radar is a deliberately small, database-free publisher. A Docker worker on
the homelab reads podcast and YouTube feeds, asks an OpenRouter-compatible model
to select and summarize noteworthy episodes from publisher notes, and commits
the resulting static archive, HTML page, and RSS feed to GitHub. Cloudflare
Pages serves the tracked `public/` directory.

There are no transcripts, downloads, images, JavaScript, external stylesheets,
detail pages, or runtime database. A small inline stylesheet gives the text-only
archive an editorial, responsive layout without adding runtime assets.

## Files

- `config.toml`: podcast feeds, YouTube feeds, editorial roster, and LLM config
- `data/items.json`: durable, append-only publication and seen-item state
- `public/index.html`: plain bulleted episode list
- `public/feed.xml`: RSS feed
- `radar.py`: collection, classification, summarization, and static rendering
- `error_reporter.py`: Sentry exception reporting
- `scheduled_cycle.py`: the complete pull, publish, test, commit, and push cycle
- `deploy/compose.yaml`: long-running worker registered with the Docker scheduler
- `deploy/scheduler.compose.yaml`: shared Ofelia scheduler stack for Dockge

## Local commands

The publisher requires Python 3.11+ for TOML support. The publishing logic uses
the standard library; the worker image also installs the Sentry SDK from
`requirements.txt`.

```bash
python3 radar.py doctor
python3 radar.py build-site
python3 -m unittest discover -s tests
```

To fetch and summarize new episodes, set the configured API key and run:

```bash
export OPENROUTER_API_KEY="..."
python3 radar.py run --lookback-days 7
```

The run is conservative: an item with sparse publisher notes is skipped, and a
model/provider failure leaves the item unprocessed so a later run can retry it.

## Hosting and scheduling

Cloudflare Pages is connected to the GitHub repository with:

- production branch: `main`
- build command: none
- build output directory: `public`

On the homelab, Dockge manages two stacks:

- `/opt/ai-radar`: the worker and its dedicated GitHub deploy key
- `/opt/scheduler`: Ofelia, the shared Docker-native schedule service

The worker label schedules `/app/deploy/run-cycle.sh` hourly at minute 17. A
cycle pulls `main`, fetches and summarizes new entries, rebuilds static output,
commits changed `data/` and `public/` files, and pushes. That push triggers the
Cloudflare Pages deployment.

Set `AI_RADAR_SENTRY_DSN` in the homelab stack's `.env` to report source,
summary, pipeline, and unexpected command failures. Reports are tagged with the
deployment environment, Git release, app, host, and failing phase. Individual
source failures remain isolated so the rest of a cycle can finish. Sentry's
free plan permits one cron monitor, which remains assigned to Life; AI Radar
uses error events only and does not require a paid monitor.

Secrets stay only in `/opt/ai-radar/.env` and `/opt/ai-radar/secrets/`; neither
path is part of the repository.
