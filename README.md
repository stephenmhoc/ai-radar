# AI Radar

AI Radar is a deliberately small, database-free publisher. A Docker worker on
the homelab reads podcast and newsletter feeds, asks an
OpenRouter-compatible model to select and summarize noteworthy items from
publisher notes, and commits
the resulting static archive, HTML pages, and RSS feed to GitHub. Cloudflare
Pages serves the tracked `public/` directory.

There are no transcripts, downloads, images, JavaScript, external stylesheets,
detail pages, or runtime database. Inline CSS gives the text-only pages an
editorial, responsive layout without adding runtime assets.

Every published item keeps its AI Radar headline and summary while clearly
identifying each original appearance by medium, source name, and publisher
title. The title links directly to the episode, video, or newsletter issue.

AI Radar keeps two daily calendars in the edition timezone, America/New_York
(`[editions]` in `config.toml`):

- **RSS editions** close at 7 p.m. RSS carries one entry per closed edition,
  published at the close, listing each item's headline (naming its primary
  source), long summary, and complete source list.
- **Site days** are calendar days closing at midnight. The site shows
  **Today**, everything published since midnight, followed by **Recent**, the
  last seven closed days, so a quiet day still shows recent work and Today does
  not empty when the 7 p.m. edition goes out. The homepage is built to be read
  at a glance: one line per item, grouped by day, with the followed person in
  bold and medium tags (POD, VID, TXT) that link to each appearance; clicking a
  headline opens its short summary and sources in place. The newest items (Today,
  or the latest closed day when Today is empty) carry a marker. A **People**
  roster lists followed people heard from in the last 30 days, newest first,
  with how long ago and their latest headline, and collapses everyone else into
  one quiet line. Ages count from the archive's own day close, never the build
  time, so rebuilds stay deterministic. `/archive.html` lists every past day,
  newest first with a month index, with summaries visible so the browser's find
  reaches them; the site stays script-free.

The scheduled run records each closed edition and day in `data/items.json`
before it collects new items. An item joins the period in which AI Radar first
saw it, but never one that has already closed: an item published later, such as
a deferred item, joins the open edition and day rather than rewriting one
already delivered. Recorded editions and days are immutable, so rendering stays
deterministic. `python3 radar.py close-editions` closes finished editions and
days without fetching feeds or calling the model.

## Files

- `config.toml`: podcast, newsletter, and optional YouTube feeds, the
  followed-people roster, and LLM config
- `data/items.json`: durable canonical publication and seen-item state
- `public/index.html`: Today, Recent calendar days, and the People roster, one
  line per item with short summaries that open in place
- `public/archive.html`: every past calendar day, newest first, with a month index
- `public/feeds.html`: monitored feeds, grouped by medium
- `public/feed.xml`: one RSS entry per daily edition using long summaries
- `radar.py`: feed collection, matching, archive validation, and CLI orchestration
- `editorial.py`: editorial prompt, model requests, and summary validation
- `rendering.py`: deterministic HTML/RSS rendering and generated-file manifest
- `radar_common.py`: shared types, text handling, and media identity
- `scripts/verify.py`: shared offline verification for local work, CI, and the worker
- `scripts/eval_editorial.py` and `evals/editorial_cases.json`: labeled editorial
  evals for comparing models and prompt changes (real model requests)
- `scheduled_cycle.py`: locked pull-to-push production transaction
- `scheduler_watchdog.py`: Docker health check and missed-schedule Sentry alert
- `error_reporter.py`: Sentry exception reporting
- `deploy/`: version-controlled Docker and Ofelia stack templates
- `.github/workflows/ci.yml`: independent Python and container verification
- `INFRA.md`: production architecture, operations, recovery, and verification
- `AGENTS.md`: repository-wide instructions for future coding agents

## Local commands

The publisher requires Python 3.11+. Publishing uses the standard library; the
worker image also installs the Sentry SDK and `tzdata` (for the edition
timezone) from `requirements.txt`.

Local development uses [mise](https://mise.jdx.dev/) to activate Python 3.12
and ShellCheck. Run `mise install` once after cloning, then create the ignored
virtual environment and install the pinned Python dependency:

```bash
python -m venv .venv
.venv/bin/python -m pip install --requirement requirements.txt
```

Python 3.11 is also supported by CI and can be invoked locally with
`mise exec python@3.11 -- python` when a compatibility check is needed.

```bash
.venv/bin/python radar.py build-site
.venv/bin/python scripts/verify.py
```

To fetch and summarize new items, set the configured API key and run:

```bash
export OPENROUTER_API_KEY="..."
python3 radar.py run --lookback-days 7
```

AI Radar follows a short list of people, configured as `[[people]]` in
`config.toml`: major lab CEOs, the Muse, Codex, and Claude Code leads, and a few
industry-defining figures such as Ilya Sutskever and Andrej Karpathy. An item
qualifies only when one of them is an actual guest, interviewee, central speaker,
or author. Mentions, third-party analysis, news roundups quoting them, and
colleagues at the same companies do not qualify; newsletter issues qualify only
as an interview with, or writing by, a followed person. Interview notes written in
the third person under a guest-named title ("Sam Altman on ...") count as
participation; a host narrating someone's life from books or other sources does
not.

The model must also list the followed people it is relying on, and an inclusion
is checked locally before publication: at least one listed name must be a
configured person (or alias), named as a whole word in the issue title or byline
for newsletters, or in the title, hosts, or publisher notes for other media.
Otherwise the item is stored as skipped with a `Local check:` reason, without a
retry.

The only configured YouTube source is Stripe's channel, which publishes Stripe
Sessions talks. YouTube support remains in code so archived YouTube appearances
keep rendering and more channels can be added under `[[sources]]`. YouTube Shorts are skipped locally without an LLM call, and other
brief promotional, highlight, launch, quote, and social clips are editorially
excluded.

When that policy changes, one previously excluded item can be safely refreshed
from its configured feeds and rejudged without replaying the archive:

```bash
python3 radar.py reconsider --match "unique title text"
```

`reconsider` makes one real editorial model request after refreshing the matched
item's publisher notes. It rejects empty, missing, ambiguous, and already
published matches.

Sparse publisher notes produce a `deferred` record that is reconsidered only
when richer metadata arrives. Refreshed notes and new appearances are applied
together with a successful editorial result; a failed attempt leaves the old
record intact so the same richer metadata can trigger the next cycle's retry.
Model, provider, and local-validation failures
are not persisted at all, so a later run can retry them; a missing API key
therefore fails every summary rather than deferring anything. New editorial decisions use
one strict structured-output decision shape returning both the one-to-two-sentence
site summary and four-to-eight-sentence RSS summary, plus the qualifying
`followed_people`. The application validates
the result locally, caps output tokens, and logs the actual routed model and
token usage. The prompt still targets at most 55 words for `short_summary`, but
the local length check accepts up to 82 words. A result above that 1.5x tolerance
is retried with explicit feedback to shorten it to the 55-word target; the error
reaches Sentry only if the bounded attempts are exhausted.

`[llm] temperature` is optional. When it is omitted no temperature is sent,
which some models require: requests demand providers that support every sent
parameter, so a model whose providers reject `temperature` would otherwise find
no endpoint.

Cloudflare hosting remains free static Pages. OpenRouter billing is separate:
the configured `anthropic/claude-haiku-5.5` is paid (about $0.0007 per editorial
decision), and the token cap limits output size but does not make calls free.
Haiku 5.5 replaced `openrouter/auto`, which in practice routed to Gemini 3.1
Flash Lite and relied on the local followed-people check to catch its
inclusions of roundups and unfollowed guests; on the editorial evals Haiku 5.5
judged every case correctly before that check.

### Editorial evals

`evals/editorial_cases.json` labels 48 archive items and 2 synthetic adversarial
examples (a host-narrated biography and a commentary video, each titled after a
followed person) for the followed-people rule in five categories: real guests or
authors, brief clips, third-party analysis, news roundups, and guests who are not
followed (including colleagues of followed people). `scripts/eval_editorial.py` replays each item's stored
appearances through the real editorial path with only the model overridden,
then reports final accuracy, the model's raw accuracy before the local
followed-people check, people matching, retries, routed models, tokens, and
latency, and lists every case where runs disagree. Cases decided without a
model request, such as YouTube Shorts, are left out of the raw score.

```bash
OPENROUTER_API_KEY=... python3 scripts/eval_editorial.py --repeat 2 \
  --model anthropic/claude-haiku-5.5 \
  --model openrouter/auto
```

Each model run uses the configured temperature unless `--omit-temperature MODEL`
is given; production configures none. Each pass makes about 43 real requests per
model (a few cents) and writes nothing to the archive. Run it before changing the model, editorial prompt, or
decision schema. The case labels are validated offline by the test suite; the
model runs are not part of `scripts/verify.py`.

## Hosting and scheduling

Cloudflare Pages is connected to GitHub `main` with no build command and
`public` as its output directory. The static Pages project remains on the free
plan; no Functions, Workers, D1, KV, or R2 resources are used.

Dockge manages `/opt/ai-radar` and `/opt/scheduler` on the homelab. Ofelia runs
`/app/deploy/run-cycle.sh` hourly at minute 17. A process lock covers the entire
transaction. Each run:

1. Validates a clean checkout and publishes any previously stranded commit.
2. Records every daily edition that closed since the last run and, when that
   changed the archive, verifies, commits, and pushes it on its own, so a later
   collection failure cannot hold back the day's RSS entry.
3. Fetches sources with bounded responses, safe redirects, and retries. Failed
   YouTube feeds get one shared delayed retry before they count as errors. A
   source fails on metadata only when no entry in its feed carries a valid date.
4. Defers sparse metadata or obtains one locally validated structured OpenRouter
   decision, retrying correctable provider failures within the bounded attempt
   policy.
5. Validates `data/items.json` and rebuilds all tracked static files.
6. Runs the complete local verification suite.
7. Commits generated changes, safely rebases a concurrent `main` update, and
   pushes without force. The push triggers Cloudflare Pages.

Set `AI_RADAR_SENTRY_DSN` only in the homelab `.env`. Pipeline, source, LLM,
validation, Git, test, lock, heartbeat, and unexpected failures report to
Sentry. Docker invokes `scheduler_watchdog.py` independently of Ofelia; a
missing or stale heartbeat marks the worker unhealthy and emits one grouped
Sentry event per outage. Widespread YouTube failures use a separate RSS-outage
fingerprint after the delayed retry and report only once until a recovered cycle
clears the local alert latch. Other source failures, including undated YouTube
feeds, remain independently reportable during a YouTube fetch outage. Malformed
or locally invalid LLM structured
responses use the existing bounded retry policy, with corrective validation
feedback, and reach Sentry only if every attempt fails. A response truncated at
the output-token cap is reported
immediately instead, since retrying buys the same truncation. A total Docker-host
or network outage still requires an external monitor because the failed host
cannot report its own loss.

Matching uses one rule for new groups and archived items: exact media identity
wins, fuzzy matching only crosses media, and every group preserves distinct
same-medium items. Repeated copies of the same video enrich its existing
appearance, including within one run: when a video arrives from several feeds and
only one copy fuzzy-matches an item, the other copies join that appearance rather
than creating a second owner. Shorts remain isolated from fuzzy matching.

## Continuous integration

GitHub Actions runs on every pull request and `main` push with Python 3.11 and
3.12. Both CI and the scheduled worker call `scripts/verify.py`. The verifier
makes no model requests, leaves production artifacts untouched, and strips
production reporting/model credentials from its test subprocesses. It validates
the archive, proves all static output is deterministic, runs the tests, and
compiles Python. CI additionally validates Compose, checks the shell entrypoint,
builds the Docker image, and runs the verifier inside it. Pull requests also
enforce append-only archive evolution and preservation of existing long
summaries. Dependabot covers Python, Docker, and GitHub Actions dependencies.

Secrets remain only in `/opt/ai-radar/.env` and `/opt/ai-radar/secrets/`; neither
path is part of this public repository.

Production editorial requests allow up to 2,400 output tokens to accommodate
routed model responses without truncation. Summary prose limits and strict
validation are unchanged; truncated responses still fail rather than publishing
partial content. This ceiling is not a requested summary length.
