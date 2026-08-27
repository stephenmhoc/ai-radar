# Instructions for Future Agents

These instructions apply to the entire AI Radar repository. Read `README.md`
and `INFRA.md` before changing code, data, deployment, or production state.

## Preserve the architecture

- AI Radar is a database-free static publisher.
- The homelab performs scheduled collection and generation; it is not the web
  origin.
- GitHub `main` is the durable handoff and production source.
- Cloudflare Pages serves the tracked `public/` directory through its Git
  integration.
- Scheduling belongs to the shared Ofelia Docker container through labels on
  the AI Radar worker. Do not add cron, systemd timers, or LaunchAgents.
- Dockge manages `/opt/ai-radar` and `/opt/scheduler` on the homelab.
- Stay within free Cloudflare products unless the user explicitly approves a
  different architecture and cost.
- Do not add transcription, media downloads, images, JavaScript, an application
  server, or a runtime database unless explicitly requested.

## Treat these contracts as intentional

- `data/items.json` is canonical state and belongs in Git.
- `public/index.html`, `public/feeds.html`, `public/feed.xml`, and
  `public/_headers` are generated, tracked production artifacts. `radar.py`'s
  `GENERATED_FILES` is the single list of them; the scheduled cycle stages
  exactly that list, so a new artifact cannot be rendered but left uncommitted.
- The site uses `short_summary`; RSS uses `long_summary`.
- The site and RSS identify each appearance by medium, source name, and original
  publisher title. RSS item titles name the primary source and use the standard
  `source` element when its configured feed is available.
- A short summary is one or two sentences and no more than 55 words.
- New editorial decisions use one OpenRouter call with strict structured output
  for `include`, `title`, `short_summary`, `long_summary`, and `reason`.
- A malformed structured response is retryable within the configured bounded
  LLM attempt policy; report it only if every attempt fails. A response the
  provider truncated at the output-token cap (`finish_reason` of `length`) is
  the exception: it fails immediately as `LLMTruncationError`, because every
  retry would buy the same truncation.
- The configured model is `openrouter/auto`, and routing must require providers
  that support the requested structured-output parameters.
- Validate model output locally even when the provider claims schema support.
- Do not send the existing archive back through OpenRouter without explicit
  approval. For archive-wide transformations, preserve every prior long summary
  and prove the migration is lossless.
- Sparse notes should be deferred and reconsidered only after richer metadata
  arrives; invented or weakly grounded summaries are worse than no publication.
- A canonical item may have at most one podcast appearance, one YouTube
  appearance, and one newsletter appearance. YouTube identity is the
  source-independent video ID; never group distinct same-medium items through
  fuzzy title matching.
- Frontier-lab interviews remain the highest-priority editorial signal. Also
  admit unusually substantive AI research, infrastructure, product/engineering,
  strategy, policy, and Physical AI work. Newsletter issues need original
  reporting, research, interviews, or durable analysis; exclude generic
  roundups and incidental AI mentions.
- Keep source failures isolated and report them, but preserve the scheduled
  cycle's degraded/nonzero result when any source or LLM error occurred.
- An undated feed entry can never clear the lookback cutoff, so a source counts
  as failed on metadata only when no entry in its feed has a valid date. A
  partial mix is a stderr warning: a few malformed archive rows must not alert
  on every hourly cycle forever.
- Published summaries have one rule set, `summary_contract_errors`. Stored items
  are held to the shape rules only; the prose rules apply to freshly generated
  summaries, because imported `legacy-*` long summaries predate them.
- Retry failed YouTube fetches once as a shared delayed pass before reporting
  them. Widespread failures that survive the retry use the dedicated
  `youtube-rss-outage` Sentry fingerprint and a persistent once-per-outage alert
  latch; recovered failures do not degrade the cycle and a recovered cycle
  clears the latch.
- Pipeline, Git, validation, test, lock, heartbeat, and unexpected failures must
  reach Sentry. Keep the Docker heartbeat watchdog independent of Ofelia so a
  stopped scheduler is observable while the worker and host remain alive.

## Work from current evidence

- Inspect the current worktree before editing. Existing changes belong to the
  user unless proven otherwise; preserve unrelated work.
- Verify current repository files and live runtime rather than relying only on
  older AI Radar history. This project previously used SQLite, transcription,
  macOS scheduling, and direct Wrangler deployments; those are obsolete.
- For homelab claims, check the actual Compose files, container labels, mounts,
  Git SHA/status, Ofelia logs, and a completed run. A running container alone is
  not proof of a healthy publisher.
- For Cloudflare claims, check the commit's `Cloudflare Pages` status and both
  public endpoints. A successful Git push alone is not proof of deployment.
- When investigating Sentry failures, distinguish unit-test fixture messages
  from live collection statistics and the job's final result.
- For scheduler liveness, inspect both Ofelia logs and the worker's Docker
  health/heartbeat; neither one alone proves the complete path.
- A burst of YouTube RSS errors may be upstream and transient. Probe individual
  feed URLs and compare runs before editing many sources or blaming the LLM.

## Security and public-repository rules

- This repository is public. Never add private IPs, tailnet hostnames, account
  IDs, Sentry DSNs, API keys, private keys, tokens, cookie material, or secret
  values to files, commits, logs, screenshots, or responses.
- Homelab secrets stay in `/opt/ai-radar/.env` and
  `/opt/ai-radar/secrets/`.
- Git operations in the worker use the repository-scoped deploy key and strict
  known-hosts file mounted under `/run/secrets/`. Do not use a broad personal
  key and do not disable `StrictHostKeyChecking`.
- Do not print `.env` contents. If necessary, inspect environment variable names
  only.
- Do not add Cloudflare Functions, Workers, D1, KV, R2, or another paid/limited
  service without explaining why static Pages is insufficient and obtaining
  approval.

## Editing guidance

- Make the smallest coherent change and keep implementation, tests,
  documentation, generated files, and deployment configuration consistent.
- After every change, always check and update the relevant documentation before
  considering the work complete.
- Edit the version-controlled templates in `deploy/`; if production Compose
  state changes, update the corresponding Dockge stack and verify the live
  container matches.
- Rebuild the homelab image when `deploy/Dockerfile` or `requirements.txt`
  changes. Bind-mounted Python/config/static-file changes normally require only
  a pull, not an image rebuild.
- Keep the site text-only and accessible. It may be visually polished with
  inline CSS, but should not gain images, remote assets, client-side scripts, or
  decorative list markers unless the user asks.
- Preserve deterministic rendering. Re-running `build-site` with unchanged
  archive/config must not alter output.
- Preserve `public/feeds.html` as a deterministic rendering of every active
  podcast, YouTube, and newsletter source in `config.toml`, and keep its link
  visually secondary in the header.
- Avoid broad archive rewrites when a narrow migration is sufficient. When a
  large JSON diff is necessary, compare IDs, order, statuses, links, and old/new
  semantic fields programmatically.
- Update `README.md`, `INFRA.md`, and this file when their documented contracts
  change.

## Required local verification

For ordinary code, content, or rendering changes, run:

```bash
python3 radar.py doctor
python3 radar.py build-site
python3 -m unittest discover -s tests -v
python3 -m py_compile radar.py scheduled_cycle.py scheduler_watchdog.py error_reporter.py scripts/check_archive_evolution.py
git diff --check
git status --short
```

Add focused tests for every changed contract. At minimum, summary changes must
prove the site/RSS split, schema payload, local validation, archive compatibility,
and deterministic output.

GitHub Actions is an independent check on every pull request and `main` push;
Ofelia remains the only production publisher schedule. Keep action permissions
read-only, pin third-party actions to commit SHAs, and never expose production
secrets to pull-request code.

Do not make a synthetic OpenRouter request merely to test connectivity when a
payload/unit test is sufficient. A real cycle may cost money or publish content.
If a real production cycle is required, say that it is state-changing before
running it and report whether it made LLM calls, changed files, committed, and
pushed.

## Release and deployment

Do not commit, push, deploy, or run a production cycle when the user asks only
for a review, diagnosis, or plan. When the user authorizes release:

1. Review the complete scoped diff and ensure no secret or unrelated file is
   staged.
2. Run the required local verification.
3. Commit the coherent change and push `main`.
4. Confirm local `HEAD` and GitHub `main` are the same SHA using `git ls-remote`.
5. Require a successful GitHub check named `Cloudflare Pages`.
6. Verify HTTP 200 from the site and RSS URLs and check the changed content in
   each output, not only headers. Also verify `/feeds.html` when sources or
   rendering changed.
7. Sync and verify `/opt/ai-radar/repo` through the existing private homelab
   access when production worker behavior changed.
8. Confirm the homelab checkout is clean and on the released SHA.
9. Confirm Dockge's `ai-radar` and `scheduler` containers and Ofelia labels are
   correct.
10. For scheduling changes, observe a complete scheduler-triggered run and
    confirm the worker health check reads a fresh heartbeat.

Normal deployment is Git-based. Do not use direct `wrangler pages deploy` unless
the user explicitly requests it or Git integration is being recovered. Do not
delete or recreate the Pages project as routine troubleshooting.

The homelab checkout's deploy key is mounted only inside the worker. A direct
host-side `git pull` may fail host/key verification. Prefer the existing
container execution path or reproduce the exact scoped `GIT_SSH_COMMAND` from
`deploy/run-cycle.sh`; never weaken it.

## Completion standard

Report concrete evidence:

- Files changed and the contract implemented.
- Tests and static validation performed.
- Archive/site/RSS counts when content changed.
- Commit SHA and remote parity when pushed.
- Cloudflare Pages check and live endpoint/content verification when deployed.
- Homelab Git/container/scheduler evidence when runtime behavior changed.
- Any remaining upstream failure or unverified boundary, especially whether a
  live OpenRouter request was intentionally avoided.

Do not call the system healthy based only on a PID, an `Up` container, a clean
unit-test run, or a Git push. For AI Radar, healthy means the scheduled path can
complete, Git contains the expected static state, and Cloudflare serves the
matching site and RSS feed.
