# AI Radar

AI Radar is a deliberately small, database-free publisher. A Docker worker on
the homelab reads podcast, YouTube, and newsletter feeds, asks an
OpenRouter-compatible model to select and summarize noteworthy items from
publisher notes, and commits
the resulting static archive, HTML pages, and RSS feed to GitHub. Cloudflare
Pages serves the tracked `public/` directory.

There are no transcripts, downloads, images, JavaScript, external stylesheets,
detail pages, or runtime database. Inline CSS gives the text-only pages an
editorial, responsive layout without adding runtime assets.

## Files

- `config.toml`: podcast, YouTube, and newsletter feeds, editorial roster, and
  LLM config
- `data/items.json`: durable canonical publication and seen-item state
- `public/index.html`: item archive using short summaries
- `public/feeds.html`: monitored podcast, YouTube, and newsletter feeds
- `public/feed.xml`: RSS feed using long summaries
- `radar.py`: collection, classification, validation, and static rendering
- `scheduled_cycle.py`: locked pull-to-push production transaction
- `scheduler_watchdog.py`: Docker health check and missed-schedule Sentry alert
- `error_reporter.py`: Sentry exception reporting
- `deploy/`: version-controlled Docker and Ofelia stack templates
- `.github/workflows/ci.yml`: independent Python and container verification
- `INFRA.md`: production architecture, operations, recovery, and verification
- `AGENTS.md`: repository-wide instructions for future coding agents

## Local commands

The publisher requires Python 3.11+. Publishing uses the standard library; the
worker image also installs the Sentry SDK from `requirements.txt`.

```bash
python3 radar.py doctor
python3 radar.py build-site
python3 -m unittest discover -s tests -v
python3 -m py_compile radar.py scheduled_cycle.py scheduler_watchdog.py error_reporter.py scripts/check_archive_evolution.py
```

To fetch and summarize new items, set the configured API key and run:

```bash
export OPENROUTER_API_KEY="..."
python3 radar.py run --lookback-days 7
```

Lab interviews remain the highest-priority signal. The editorial policy also
admits consequential work on frontier research, AI infrastructure, important AI
products and companies, AI-native engineering, strategy, policy, and Physical
AI. Newsletter issues must offer original reporting, research, interviews, or
durable analysis; generic roundups and incidental AI mentions are excluded.

When that policy changes, one previously excluded item can be safely refreshed
from its configured feeds and rejudged without replaying the archive:

```bash
python3 radar.py reconsider --match "unique title text"
```

`reconsider` makes one real editorial model request after refreshing the matched
item's publisher notes. It rejects empty, missing, ambiguous, and already
published matches.

Sparse publisher notes produce a `deferred` record that is reconsidered only
when richer metadata arrives. Model, provider, and local-validation failures
are not persisted at all, so a later run can retry them; a missing API key
therefore fails every summary rather than deferring anything. New editorial decisions use
one strict structured-output request returning both the one-to-two-sentence
site summary and four-to-eight-sentence RSS summary. The application validates
the result locally, caps output tokens, and logs the actual routed model and
token usage.

Cloudflare hosting remains free static Pages. OpenRouter billing is separate:
`openrouter/auto` can select paid models, so the token cap limits output size but
does not make model calls free.

## Hosting and scheduling

Cloudflare Pages is connected to GitHub `main` with no build command and
`public` as its output directory. The static Pages project remains on the free
plan; no Functions, Workers, D1, KV, or R2 resources are used.

Dockge manages `/opt/ai-radar` and `/opt/scheduler` on the homelab. Ofelia runs
`/app/deploy/run-cycle.sh` hourly at minute 17. A process lock covers the entire
transaction. Each run:

1. Validates a clean checkout and publishes any previously stranded commit.
2. Fetches sources with bounded responses, safe redirects, and retries. Failed
   YouTube feeds get one shared delayed retry before they count as errors. A
   source fails on metadata only when no entry in its feed carries a valid date.
3. Defers sparse metadata or makes one structured OpenRouter request.
4. Validates `data/items.json` and rebuilds all tracked static files.
5. Runs the complete local verification suite.
6. Commits generated changes, safely rebases a concurrent `main` update, and
   pushes without force. The push triggers Cloudflare Pages.

Set `AI_RADAR_SENTRY_DSN` only in the homelab `.env`. Pipeline, source, LLM,
validation, Git, test, lock, heartbeat, and unexpected failures report to
Sentry. Docker invokes `scheduler_watchdog.py` independently of Ofelia; a
missing or stale heartbeat marks the worker unhealthy and emits one grouped
Sentry event per outage. Widespread YouTube failures use a separate RSS-outage
fingerprint after the delayed retry and report only once until a recovered cycle
clears the local alert latch. Malformed LLM structured responses use the existing
bounded retry policy and reach Sentry only if every attempt fails. A response
truncated at the output-token cap is reported immediately instead, since retrying
buys the same truncation. A total Docker-host or network outage still requires an
external monitor because the failed host cannot report its own loss.

## Continuous integration

GitHub Actions runs on every pull request and `main` push with Python 3.11 and
3.12. It validates the archive, proves all static output is deterministic, runs
the tests, compiles every entrypoint, validates Compose, checks the shell
entrypoint, builds the Docker image, and tests inside it. Pull requests also
enforce append-only archive evolution and preservation of existing long
summaries. Dependabot covers Python, Docker, and GitHub Actions dependencies.

Secrets remain only in `/opt/ai-radar/.env` and `/opt/ai-radar/secrets/`; neither
path is part of this public repository.
