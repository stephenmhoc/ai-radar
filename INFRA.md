# AI Radar Infrastructure

This document describes the current production architecture and operating model
for AI Radar. It intentionally contains no secret values, private network
addresses, Cloudflare account identifiers, or deploy-key material because this
repository is public.

## Architecture at a glance

```text
Podcast and YouTube feeds
          |
          v
Homelab Docker worker -----> OpenRouter Auto Router
          |                         |
          |                    one structured result
          |                 (short + long summaries)
          v
Static archive, HTML, and RSS in Git
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
| Homelab worker | Fetch sources, classify episodes, request summaries, render static files, test, commit, and push | Git checkout mounted at `/app` |
| OpenRouter | Select a compatible model through `openrouter/auto` and return one strict structured result per new candidate | None owned by AI Radar |
| GitHub | Canonical repository, publication history, and handoff to hosting | `main`, especially `data/` and `public/` |
| Cloudflare Pages | Serve the static site and RSS feed at the public domain | Deployed copy of `public/` |
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

- `config.toml` defines sources, editorial rules, public URLs, and LLM settings.
- `data/items.json` is the canonical database-free archive. It records seen,
  skipped, and published items and is committed to Git.
- `public/index.html` is the generated static site.
- `public/feed.xml` is the generated RSS feed.
- `public/_headers` sets the RSS content type on Cloudflare Pages.
- `radar.py` performs collection, selection, summarization, and rendering.
- `scheduled_cycle.py` owns the pull-to-push production transaction.
- `error_reporter.py` owns Sentry event reporting.
- `deploy/` contains the version-controlled templates for both homelab stacks.

The static outputs are build artifacts, but they are intentionally tracked.
Git therefore contains both the source archive and the exact files that
Cloudflare serves. At the 2026-08-25 validation point, the archive contained
1,748 records and the site and RSS feed each exposed 224 published episodes.
These counts should grow over time.

### Summary contract

New candidates use a single OpenRouter chat-completions call. The configured
model is `openrouter/auto`; the request uses a strict JSON schema and requires a
provider that supports the requested parameters. The result contains:

- `include`: editorial inclusion decision.
- `title`: display title.
- `short_summary`: one or two sentences and at most 55 words, used by the site.
- `long_summary`: four to eight sentences, used by RSS.
- `reason`: concise inclusion or exclusion rationale.

The application validates the returned field names and types locally. Included
items also have local length checks. Provider or validation failures leave the
candidate unprocessed so a later cycle can retry it.

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
`application/rss+xml; charset=utf-8`. Cloudflare also serves the site with the
security and cache headers observed on the public endpoint. Do not assume a
successful Git push is a successful release: verify the Pages check and both
public URLs.

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
run an HTTP server.

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
configuration, and executes `scheduled_cycle.py`. A cycle performs these phases
in order:

1. Fast-forward pull `origin/main`.
2. Load the freshly pulled `radar.py`.
3. Fetch all configured sources using the configured lookback window.
4. Group duplicate podcast/YouTube appearances into canonical candidates.
5. For new candidates with adequate publisher notes, make one structured
   OpenRouter request and store the decision and both summaries.
6. Rebuild `data/items.json`, `public/index.html`, and `public/feed.xml`.
7. Run the full Python unit-test suite.
8. Stage only `data/items.json` and the files under `public/` owned by the
   publisher.
9. If staged output changed, commit as `AI Radar` and push `HEAD:main`.
10. Let the GitHub integration trigger Cloudflare Pages.

Individual source and LLM failures are isolated so other sources can finish.
After generated changes are safely tested and pushed, any source or LLM error
still marks the scheduled cycle degraded and returns a nonzero exit. This makes
partial upstream failures visible rather than silently treating them as a
healthy run.

## Sentry and logs

AI Radar sends error events to its Sentry project when
`AI_RADAR_SENTRY_DSN` is present. Events include deployment environment, Git
release, application, host, failing phase, and source/model tags where
applicable. Source failures use stable fingerprints so repeated upstream errors
group together.

AI Radar does not use a Sentry cron monitor. The free-plan cron-monitor slot is
reserved for another homelab application; AI Radar uses ordinary error events.
The scheduler logs are therefore the primary schedule/run record, while Sentry
is the failure record.

Useful read-only checks on the homelab:

```bash
cd /opt/ai-radar
docker compose ps
docker logs --tail 100 ai-radar
docker logs --tail 100 scheduler
docker inspect ai-radar --format '{{json .Config.Labels}}'
cd /opt/ai-radar/repo && git status --short && git log -1 --oneline
```

The unit tests intentionally emit sample warning/error text while exercising
Sentry and degraded-cycle behavior. Distinguish those fixture messages from the
collection statistics and the Ofelia job's final exit status.

## Verification and release checks

Before committing a code or content change locally:

```bash
python3 radar.py doctor
python3 radar.py build-site
python3 -m unittest discover -s tests -v
python3 -m py_compile radar.py scheduled_cycle.py error_reporter.py
git diff --check
git status --short
```

After pushing:

1. Confirm local `HEAD` equals GitHub `main` with `git ls-remote`.
2. Confirm the GitHub check named `Cloudflare Pages` completed successfully for
   that commit.
3. Request `/` and `/feed.xml` and require HTTP 200.
4. Confirm the site contains the expected short summary and the RSS item
   contains the expected long summary.
5. Confirm the homelab checkout reached the same commit and is clean.
6. Confirm both `ai-radar` and `scheduler` containers are running and the
   Ofelia labels still describe the expected job.
7. For scheduler changes, observe a real Ofelia-triggered run; container uptime
   alone is not proof that collection and publication work.

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
The collector reports the affected source to Sentry, continues with other
sources, and marks the overall run degraded. Multiple simultaneous YouTube RSS
failures are not evidence that the summary-model change is broken; reproduce
the individual feed requests and compare consecutive scheduled runs before
changing source configuration.

### OpenRouter or structured-output failures

An API, routing, JSON-schema, or local-validation failure increments
`llm_errors` and leaves the item eligible for a later retry. Check the model tag,
HTTP response, and Sentry event. Do not loosen schema enforcement or silently
accept prose to make a run appear healthy.

### Git pull or push failures

Check the cycle phase and use the mounted repository deploy key. Verify file
permissions, the repository-scoped key in GitHub, and the strict known-hosts
file. Never substitute a broad personal key or disable host verification.

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
   site/RSS verification.

No SQLite restore is required. The committed JSON archive is the publication
state, and Git history provides its recovery path.
