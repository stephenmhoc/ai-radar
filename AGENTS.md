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
- `public/index.html`, `public/feed.xml`, and `public/_headers` are generated,
  tracked production artifacts.
- The site uses `short_summary`; RSS uses `long_summary`.
- A short summary is one or two sentences and no more than 55 words.
- New editorial decisions use one OpenRouter call with strict structured output
  for `include`, `title`, `short_summary`, `long_summary`, and `reason`.
- The configured model is `openrouter/auto`, and routing must require providers
  that support the requested structured-output parameters.
- Validate model output locally even when the provider claims schema support.
- Do not send the existing archive back through OpenRouter without explicit
  approval. For archive-wide transformations, preserve every prior long summary
  and prove the migration is lossless.
- Sparse notes should be skipped; invented or weakly grounded summaries are
  worse than no publication.
- Keep source failures isolated and report them, but preserve the scheduled
  cycle's degraded/nonzero result when any source or LLM error occurred.

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
python3 -m py_compile radar.py scheduled_cycle.py error_reporter.py
git diff --check
git status --short
```

Add focused tests for every changed contract. At minimum, summary changes must
prove the site/RSS split, schema payload, local validation, archive compatibility,
and deterministic output.

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
   each output, not only headers.
7. Sync and verify `/opt/ai-radar/repo` through the existing private homelab
   access when production worker behavior changed.
8. Confirm the homelab checkout is clean and on the released SHA.
9. Confirm Dockge's `ai-radar` and `scheduler` containers and Ofelia labels are
   correct.
10. For scheduling changes, observe a complete scheduler-triggered run.

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
