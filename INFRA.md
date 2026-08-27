# AI Radar Infrastructure

This document describes the current production architecture and operating model
for AI Radar. It intentionally contains no secret values, private network
addresses, Cloudflare account identifiers, or deploy-key material because this
repository is public.

## Architecture at a glance

```text
Podcast, YouTube, and newsletter feeds
          |
          v
Homelab Docker worker -----> OpenRouter Auto Router
          |                         |
          |                    one structured result
          |                 (short + long summaries)
          v
Static archive, HTML pages, and RSS in Git
          |
          v
GitHub public repository, main branch
          |
          v
Cloudflare Pages Git deployment
          |
          v
https://ai-radar.merimerimeri.com

Failures from collection, summarization, and the scheduled pipeline
are sent to Sentry.
```

The homelab is a publisher, not a web origin. It does not accept public AI Radar
traffic and exposes no AI Radar container port. If the homelab is offline, the
existing site and RSS feed remain available from Cloudflare; only updates stop.

## Component responsibilities

| Component | Responsibility | Durable state |
| --- | --- | --- |
| Homelab worker | Fetch sources, classify items, request summaries, render static files, test, commit, and push | Git checkout mounted at `/app` |
| OpenRouter | Select a compatible model through `openrouter/auto` and return one strict structured result per new candidate | None owned by AI Radar |
| GitHub | Canonical repository, publication history, and handoff to hosting | `main`, especially `data/` and `public/` |
| Cloudflare Pages | Serve the static item archive, source list, and RSS feed | Deployed copy of `public/` |
| Ofelia | Run the worker on an explicit Docker-native schedule | Docker labels on the worker |
| Dockge | Manage the worker and scheduler Compose stacks | `/opt/ai-radar` and `/opt/scheduler` |
| Sentry | Receive source, LLM, pipeline-phase, and unexpected failures | Sentry project events |

There is deliberately no runtime database, transcription service, application
server, Cloudflare Worker, D1 database, KV namespace, R2 bucket, or homelab web
origin in this design.

## Repository and static state

The public repository is
[`stephenmhoc/ai-radar`](https://github.com/stephenmhoc/ai-radar). `main` is the
production branch.

Important paths:

- `config.toml` defines podcast, YouTube, and newsletter sources, editorial rules, public URLs, and LLM settings.
- `data/items.json` is the canonical database-free archive. It records seen,
  skipped, and published items and is committed to Git.
- `public/index.html` is the generated static item archive.
- `public/feeds.html` is the generated list of all active podcast, YouTube, and
  newsletter feeds and shares the site's inline visual system.
- `public/feed.xml` is the generated RSS feed.
- `public/_headers` sets the RSS content type and static security headers on
  Cloudflare Pages.
- `radar.py` performs collection, selection, summarization, targeted
  reconsideration, and rendering.
- `scheduled_cycle.py` owns the pull-to-push production transaction.
- `error_reporter.py` owns Sentry event reporting.
- `deploy/` contains the version-controlled templates for both homelab stacks.

The static outputs are build artifacts, but they are intentionally tracked.
Git therefore contains both the source archive and the exact files that
Cloudflare serves. At the 2026-08-26 newsletter expansion point, the archive
contained 1,745 canonical records, 2,635 unique appearances, 229 published
items, and no deferred sparse records. The site and RSS feed each exposed the
same 229 published items, and the feeds page listed all 55 active sources,
including eight newsletters. The preceding repair removed 245 incorrectly
grouped appearances and 18 empty duplicate/corrupt seen records without changing
any existing long summary or calling OpenRouter. Future archive evolution is
append-only unless an explicit, verified repair is required.

The generated site and RSS preserve the AI Radar editorial headline and summary
while presenting every appearance with its medium, readable source name, and
original publisher title linked to the source item. RSS titles also name the
primary source and expose it through the standard RSS `source` element when the
configured feed is available. This is derived entirely from canonical archive
and configuration state; it does not require an archive migration or model call.

### Summary contract

New candidates use a single OpenRouter chat-completions call. The configured
model is `openrouter/auto`; the request uses a strict JSON schema and requires a
provider that supports the requested parameters. The result contains:

- `include`: editorial inclusion decision.
- `title`: display title.
- `short_summary`: one or two sentences and at most 55 words, used by the site.
- `long_summary`: four to eight sentences, used by RSS.
- `reason`: concise inclusion or exclusion rationale.

The application validates the exact returned fields, types, sentence counts,
and size limits locally. The response has a configured output-token ceiling,
and logs record the actual routed model and token usage. Provider or validation
failures leave the candidate unprocessed so a later cycle can retry it. Sparse
notes produce a `deferred` record and are reconsidered only if an existing
appearance gains better notes or a matching appearance adds useful metadata.

Publisher notes, titles, URLs, and names are explicitly treated as untrusted
prompt data. The local archive validator also requires unique item and
appearance IDs, source-independent YouTube video identity, at most one podcast
appearance, one YouTube appearance, and one newsletter appearance per canonical
item, safe HTTP(S) links, valid timezone-aware timestamps, allowed statuses, and
exact generated links. Newsletter RSS parsing prefers full encoded article
content over teaser descriptions and bounds stored publisher text at 24,000
characters per appearance. Newsletter-only items with fewer than 400 characters
of publisher text are deferred without a model call so paywall teasers do not
create weak summaries or repeated validation failures.

Frontier-lab interviews remain the primary editorial signal. The same structured
decision can include unusually substantive frontier-model research, AI
infrastructure, AI-native software and engineering, strategy, policy, and
Physical AI work. Newsletter issues require original reporting, research,
interviews, or durable analysis; generic link roundups and incidental AI mentions
remain out of scope. A targeted `radar.py reconsider --match` command exists for
one unpublished item after a policy change, so a narrow correction does not send
the existing archive back through OpenRouter.

The Cloudflare portion of this architecture remains free static Pages.
OpenRouter is a separate service: `openrouter/auto` can route to paid models.
`max_output_tokens` bounds response size and the logs expose routed-model usage,
but neither setting makes Auto Router free.

The historical archive was migrated without sending old episodes back through
OpenRouter: prior summaries became `long_summary`, and short versions were
derived locally. Do not re-summarize the archive through a paid or remote API
without explicit approval.

## Cloudflare Pages

Cloudflare Pages project: `ai-radar`.

Current Git integration:

- Repository: `stephenmhoc/ai-radar`
- Production branch: `main`
- Build command: none
- Build output directory: `public`
- Custom domain: `ai-radar.merimerimeri.com`
- Public site: `https://ai-radar.merimerimeri.com/`
- Public source list: `https://ai-radar.merimerimeri.com/feeds.html`
- Public RSS: `https://ai-radar.merimerimeri.com/feed.xml`

A push to `main` triggers Pages automatically. The Cloudflare Pages check on the
GitHub commit is the deployment record. Direct Wrangler uploads are not part of
the normal release path and can create deployment history that is harder to
reconcile with Git; use them only for an explicitly requested recovery or
diagnostic operation.

The Cloudflare side intentionally uses only static Pages hosting. There are no
Pages Functions or other Cloudflare storage/compute dependencies. Preserve this
shape unless the user explicitly expands the architecture, and check free-plan
eligibility before adding any Cloudflare product.

`public/_headers` ensures `/feed.xml` is served as
`application/rss+xml; charset=utf-8`. It also supplies a restrictive static
Content Security Policy, referrer policy, frame denial, and MIME sniffing
protection. Do not assume a
successful Git push is a successful release: verify the Pages check and both
public pages plus RSS.

## Homelab runtime

The worker runs on the Docker homelab host under Dockge. Access is private and
should use the operator's existing SSH/Tailscale configuration; do not publish
the host address in this repository.

### AI Radar stack

- Dockge stack directory: `/opt/ai-radar`
- Live Compose file: `/opt/ai-radar/compose.yaml`
- Git checkout: `/opt/ai-radar/repo`
- Environment file: `/opt/ai-radar/.env`
- Secret files: `/opt/ai-radar/secrets/`
- Compose service: `worker`
- Container: `ai-radar`
- Image: `ai-radar:static`
- Restart policy: `unless-stopped`
- Container working directory: `/app`
- Container command between runs: `sleep infinity`

The checkout is bind-mounted read/write from `/opt/ai-radar/repo` to `/app`.
This lets each scheduled run pull the latest application code without rebuilding
the image. Rebuild the image when `deploy/Dockerfile` or `requirements.txt`
changes, because those affect installed OS/Python dependencies rather than only
bind-mounted code.

The image is based on `python:3.12-slim` and adds CA certificates, Git, the
OpenSSH client, and the Python dependencies in `requirements.txt`. It does not
run an HTTP server. `.dockerignore` keeps Git metadata, local caches, and other
development-only files out of the image context.

The worker has a Docker health check independent of Ofelia. Every scheduled
invocation atomically updates `var/scheduler-heartbeat.json` when it starts and
finishes. Docker runs `scheduler_watchdog.py` every 30 minutes after a two-hour
startup grace period. If the last start is more than 9,000 seconds old, the
watchdog reports one grouped Sentry event for the outage and marks the worker
unhealthy. A fresh heartbeat clears the local alert latch so a later outage can
report again. `AI_RADAR_HEARTBEAT_MAX_AGE_SECONDS` can raise the threshold but
may not set it below one hour.

### Scheduler stack

- Dockge stack directory: `/opt/scheduler`
- Live Compose file: `/opt/scheduler/compose.yaml`
- Compose service and container: `scheduler`
- Image: `mcuadros/ofelia:0.3.22`
- Restart policy: `unless-stopped`
- Command: `daemon --docker`

Ofelia discovers jobs through Docker labels. The scheduler mounts the Docker
socket read-only. Docker-socket access is still highly privileged in practice,
so the scheduler image and configuration must be treated as trusted
infrastructure.

AI Radar's labels define:

```text
ofelia.enabled=true
ofelia.job-exec.ai-radar.schedule=0 17 * * * *
ofelia.job-exec.ai-radar.command=/app/deploy/run-cycle.sh
ofelia.job-exec.ai-radar.no-overlap=true
```

Ofelia uses a six-field cron expression here. The job starts at minute 17 of
every hour in the stack's `America/New_York` timezone, and overlapping runs are
disabled. Do not add a parallel Linux cron, systemd timer, or macOS LaunchAgent;
Docker labels are the scheduling source of truth.

## Secrets and Git authentication

The following values exist only on the homelab and must never be committed,
printed, or copied into documentation:

- `OPENROUTER_API_KEY`
- `AI_RADAR_SENTRY_DSN`
- Any optional environment override such as `AI_RADAR_SENTRY_ENVIRONMENT`

`AI_RADAR_LOOKBACK_DAYS` is configuration rather than a credential but remains
in the homelab `.env` with the deployment settings.

GitHub write access uses a repository-scoped SSH deploy key:

- Private key on host: `/opt/ai-radar/secrets/github_deploy_key`
- Known-hosts file on host: `/opt/ai-radar/secrets/github_known_hosts`
- Container mounts: `/run/secrets/github_deploy_key` and
  `/run/secrets/github_known_hosts`, both read-only

`deploy/run-cycle.sh` sets `GIT_SSH_COMMAND` with the mounted key,
`IdentitiesOnly=yes`, strict host-key checking, and the dedicated known-hosts
file. A `git pull` run directly as root in `/opt/ai-radar/repo` does not
automatically have this configuration and may fail. Use the container's
scheduled path or reproduce its scoped `GIT_SSH_COMMAND`; do not weaken SSH
host-key checking.

## Scheduled publication transaction

`/app/deploy/run-cycle.sh` changes to `/app`, establishes the scoped Git SSH
configuration, and executes `scheduled_cycle.py`. A nonblocking process lock
covers the complete transaction, including manual `docker exec` invocations. A
cycle performs these phases in order:

1. Write a running heartbeat and require a clean tracked worktree.
2. Fetch `origin/main`, safely rebase any local publication commit when the
   remote advanced, and push any commit stranded by an earlier transient push
   failure. Force pushes are never used.
3. Tag Sentry with the freshly synchronized worktree SHA and load `radar.py`.
4. Fetch all configured sources using the lookback window, bounded response
   sizes (16 MiB per feed), public-only redirect destinations, and bounded
   retries for transient failures. YouTube failures, including 404 responses,
   receive one shared delayed retry after 60 seconds before they count as
   source errors.
5. Match exact media identity first, allow fuzzy matching only across media,
   and retain at most one podcast, one YouTube, and one newsletter appearance
   per item.
6. Defer sparse metadata or obtain one locally validated structured OpenRouter
   decision and both summaries. Malformed responses and locally invalid results
   retry within the configured bounded LLM attempt policy before they count as
   errors.
7. Validate and atomically save `data/items.json`; rebuild `public/index.html`,
   `public/feeds.html`, `public/feed.xml`, and `public/_headers`.
8. Run `doctor`, all Python tests, and entrypoint compilation.
9. Stage only the canonical archive and generated public artifacts. Commit as
   `AI Radar` when the staged output changed.
10. Re-fetch, safely reconcile any concurrent `main` update, and push every
    ahead commit, including one left by a previous failed push.
11. Write the finished heartbeat and let the Git integration trigger Pages.

Individual source and LLM failures are isolated so other sources can finish.
Remaining source failures are aggregated into one Sentry event per cycle after
retries, while each deferred LLM failure retains its item context. A YouTube
failure that recovers on the delayed pass does not reach Sentry or degrade the
cycle. If at least three and at least half of the configured YouTube sources
still fail fetches, Sentry uses a dedicated `youtube-rss-outage` fingerprint
with retry and recovery counts. A persistent latch under `var/` suppresses
repeat Sentry events during the same continuous outage and is cleared as soon
as a cycle falls below the outage threshold. After safe publication, any
remaining source or LLM error still marks the cycle degraded and returns
nonzero. Git, test, validation, lock, heartbeat, and unexpected failures also
produce Sentry events.

## Sentry and logs

AI Radar sends error events to its Sentry project when
`AI_RADAR_SENTRY_DSN` is present. Events include deployment environment, Git
release, freshly synchronized worktree SHA, application, host, failing phase,
and source/model tags where applicable. Stable fingerprints group repeated
source-cycle, summary, pipeline-phase, and stale-heartbeat failures.

AI Radar does not use a Sentry cron monitor. The free-plan cron-monitor slot is
reserved for another homelab application; the Docker health check provides
independent Ofelia liveness detection using ordinary Sentry error events. The
scheduler logs remain the primary schedule/run record, while Sentry is the
failure record. A complete Docker-host, power, or network outage cannot be
self-reported; guaranteeing notification for that boundary requires an
external heartbeat monitor.

Useful read-only checks on the homelab:

```bash
cd /opt/ai-radar
docker compose ps
docker logs --tail 100 ai-radar
docker logs --tail 100 scheduler
docker inspect ai-radar --format '{{json .Config.Labels}}'
docker inspect ai-radar --format '{{json .State.Health}}'
cd /opt/ai-radar/repo && git status --short && git log -1 --oneline
```

The unit tests intentionally emit sample warning/error text while exercising
Sentry and degraded-cycle behavior. Distinguish those fixture messages from the
collection statistics and the Ofelia job's final exit status.

## GitHub continuous integration

`.github/workflows/ci.yml` runs on pull requests and pushes to `main` with
read-only repository permissions. Python 3.11 and 3.12 jobs run `doctor`, rebuild
every public artifact and require no diff, execute all tests, compile every
entrypoint, and run `git diff --check`. Pull requests compare the candidate
archive with the base branch and reject removed canonical items, downgraded
published items, removed media, or changes to existing nonempty long summaries.

A separate container job validates both Compose templates, checks
`deploy/run-cycle.sh` with ShellCheck, builds `deploy/Dockerfile`, and runs the
doctor and tests inside the resulting image. Dependabot checks pip, Docker, and
GitHub Actions weekly. Actions are an independent verification path, not the
publisher schedule; Ofelia remains the only production scheduler.

GitHub secret scanning, push protection, dependency alerts, and automated
security updates are enabled for the public repository. Non-provider pattern
and validity checks are not available on the repository's current plan.

`main` cannot require pre-existing CI checks without also blocking the
repository deploy key's direct generated-content pushes. Moving the worker to a
branch-and-PR publisher would add GitHub credentials and orchestration, so the
current design runs CI immediately after every push and relies on Pages keeping
the previous successful deployment if its commit check fails.

## Verification and release checks

Before committing a code or content change locally:

```bash
python3 radar.py doctor
python3 radar.py build-site
python3 -m unittest discover -s tests -v
python3 -m py_compile radar.py scheduled_cycle.py scheduler_watchdog.py error_reporter.py scripts/check_archive_evolution.py
git diff --check
git status --short
```

After pushing:

1. Confirm local `HEAD` equals GitHub `main` with `git ls-remote`.
2. Confirm all `CI` jobs and the `Cloudflare Pages` check completed successfully
   for that commit.
3. Request `/`, `/feeds.html`, and `/feed.xml` and require HTTP 200.
4. Confirm the site contains the expected short summary, the feeds page contains
   all configured sources, and the RSS item
   contains the expected long summary.
5. Confirm the homelab checkout reached the same commit and is clean.
6. Confirm both `ai-radar` and `scheduler` containers are running and the
   Ofelia labels still describe the expected job.
7. For scheduler changes, observe a real Ofelia-triggered run and require a
   fresh healthy Docker heartbeat; container uptime alone is not proof that
   collection and publication work.

A manual production cycle is state-changing because it may discover content,
call OpenRouter, commit, push, and deploy:

```bash
docker exec ai-radar /app/deploy/run-cycle.sh
```

Run it only when a real publication cycle is intended. Inspect its complete
output and final exit code.

## Failure modes and recovery

### Source failures

Podcast or YouTube RSS endpoints can return timeouts, 404s, or 5xx responses.
The collector retries transient timeouts, 429s, and 5xx responses, aggregates
remaining failures into one Sentry event with per-source context, continues
with other sources, and marks the overall run degraded. Because YouTube's RSS
service can return transient 404s across active channels, all failed YouTube
fetches receive one batch retry after 60 seconds. Failures are reported only
after that pass, and widespread remaining failures use the dedicated outage
fingerprint once per continuous outage. The local alert latch resets after a
cycle falls below the broad-outage threshold, while each affected cycle remains
degraded and nonzero. Multiple simultaneous YouTube RSS failures are not
evidence that the summary-model change is broken; reproduce the individual feed
requests and compare consecutive scheduled runs before changing source
configuration.

### OpenRouter or structured-output failures

An API, routing, JSON-schema, or local-validation failure increments
`llm_errors` and leaves the item eligible for a later retry. Malformed structured
output, including locally invalid summaries, first uses the current cycle's
bounded LLM retries and reaches Sentry only if every attempt fails. Check the
model tag, HTTP response, and Sentry event. Do not loosen schema enforcement or
silently accept prose to make a run appear healthy.

### Git pull or push failures

The next cycle automatically retries an ahead commit left by a transient push
failure. Concurrent nonconflicting `main` changes are safely rebased before
push. A rebase conflict is aborted, reported, and leaves the publication commit
intact for manual resolution. Check the cycle phase and mounted deploy key;
never substitute a broad personal key, force push, or disable host verification.

### Scheduler or heartbeat failures

Check both Ofelia logs and `docker inspect ai-radar --format
'{{json .State.Health}}'`. A stale or missing heartbeat should have one grouped
Sentry event and an unhealthy worker state. Confirm the worker still has the
health-check definition, the scheduler labels remain intact, and the heartbeat
file can be written under `/app/var`. The watchdog cannot report when the
Docker host itself or its network is completely unavailable.

### Cloudflare deployment failures

The site remains on the previous successful Pages deployment. Check the
Cloudflare Pages commit check, confirm that `public/` exists on `main`, and
verify the Pages project still points at `main` with no build command and
`public` as its output. Avoid creating a separate Pages project during repair.

### Homelab loss

The public site remains online but stops receiving updates. Recovery requires:

1. A Docker host with Dockge.
2. A clone of the public repository at `/opt/ai-radar/repo`.
3. The worker Compose stack at `/opt/ai-radar`.
4. Restored `.env`, repository deploy key, and pinned GitHub known-hosts file
   from secure backup or newly rotated credentials.
5. The shared Ofelia stack at `/opt/scheduler`.
6. A successful test run, real scheduled cycle, Git push, Pages check, and live
   site, feeds page, and RSS verification.

No SQLite restore is required. The committed JSON archive is the publication
state, and Git history provides its recovery path.
