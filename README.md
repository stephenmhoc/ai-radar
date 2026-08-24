# AI Radar

Local service that watches podcasts, YouTube channels, blogs, and X accounts for substantial public material from technical members and executives at major AI labs. Every source is normalized into the same classifier, summarizer, static site, and RSS pipeline.

The public site is generated into `public/` and is designed to be deployed to a subdomain such as:

```text
https://ai-radar.merimerimeri.com
```

## What It Does

- Collects configured podcast RSS feeds, YouTube channels, blog feeds, and X timelines.
- Stores one canonical Radar item plus every place it appeared. A podcast and YouTube cross-post therefore publish once while linking to both.
- Asks one LLM classifier whether each item has a qualifying guest, speaker, or verified author from a configured target organization. The current targets include OpenAI, Anthropic, Google DeepMind, Meta, xAI, NVIDIA, Microsoft, Replit, Hugging Face, CoreWeave, Applied Intuition, Atreides Management, and Atoms. It also covers substantial Physical AI material about AI-enabled robots, machines, vehicles, drones, and industrial automation.
- Skips non-matching items before expensive content preparation where possible.
- Locally transcribes qualifying podcast and YouTube appearances; blog articles and substantial X threads already arrive as normalized text.
- Runs the same final full-text classifier and summarizer for every medium.
- Renders one mixed stream and RSS feed with visible medium labels and all known source links.

## Sources and deduplication

Legacy `[[feeds]]` entries remain supported as podcast sources. New media use `[[sources]]`:

```toml
[[sources]]
kind = "blog"
name = "Sam Altman"
url = "https://blog.samaltman.com/"
feed_url = "https://blog.samaltman.com/posts.atom"
people = ["Sam Altman"]

[[sources]]
kind = "blog"
name = "Example feedless author site"
url = "https://example.com/"
people = ["Example Person"]

[[sources]]
kind = "youtube"
name = "Example YouTube show"
url = "https://www.youtube.com/playlist?list=PLAYLIST_ID"
playlist_url = "https://www.youtube.com/playlist?list=PLAYLIST_ID"
people = []

[[sources]]
kind = "x"
name = "Example person"
url = "https://x.com/example"
external_id = "NUMERIC_X_USER_ID"
people = ["Example Person"]
api_key_env = "X_BEARER_TOKEN"
```

Blogs normally use RSS or Atom through `feed_url`. A feedless author site can omit it; the collector follows same-site `/essay/`, `/post/`, and `/blog/` links and extracts their article text and publication dates. YouTube show playlists use `playlist_url` and `yt-dlp`, which supports dated backfills instead of the public Atom feed's short recent window. General channel sources can still use `feed_url`; setting `YOUTUBE_API_KEY` switches those to the official Data API for richer metadata such as exact duration. Transcribing selected YouTube videos additionally requires `yt-dlp`; the resulting audio still uses the configured local Whisper command. X collection uses `X_BEARER_TOKEN`, excludes reposts, reconstructs same-author threads, and applies a deterministic substantiality floor before the shared classifier.

The generated `/sources/` page lists every active source grouped by medium. It is the public inventory of what the Radar actually monitors.

Cross-medium appearances are merged automatically only with strong evidence: identical normalized full text, an explicit cross-link, or a highly similar title, publication window, and duration. Borderline matches enter a review queue:

```bash
python3 -m podcast_radar --config config.toml duplicates
python3 -m podcast_radar --config config.toml merge-items 123 456
```

## Quick Start

From this directory:

```bash
python3 -m podcast_radar --config config.toml doctor
```

Set your LLM key if using the default OpenRouter-compatible config:

```bash
export OPENROUTER_API_KEY="..."
```

Install the local transcription backend. The current local config uses `whisper-cli` from whisper.cpp with the quantized large-v3 Turbo GGML model:

```bash
brew install whisper-cpp
mkdir -p ~/.cache/whisper.cpp
curl -L -f --max-time 120 \
  -o ~/.cache/whisper.cpp/ggml-large-v3-turbo-q5_0.bin \
  'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin?download=true'
```

```toml
[transcription]
command = "whisper-cli"
args = ["-m", "/Users/merimerimeri/.cache/whisper.cpp/ggml-large-v3-turbo-q5_0.bin", "-f", "{audio_path}", "-otxt", "-of", "{output_stem}", "-l", "en", "-bs", "1", "-bo", "1", "-np", "--prompt", "AI podcast transcript. Common terms: OpenAI, Anthropic, Google DeepMind, DeepMind, Meta AI, xAI, NVIDIA, Replit, Hugging Face, CoreWeave, Applied Intuition, ChatGPT, Claude, Gemini, GPT-4, GPT-5, GPT-5.1, o3, Sora, Codex, MCP, model behavior, post-training, reinforcement learning, reasoning models, steerability, inference, agents. Episode metadata: {feed_name}. {episode_title}. Hosts: {episode_hosts}. Description: {episode_description}"]
output_path = "{output_stem}.txt"
```

Run the pipeline:

```bash
python3 -m podcast_radar --config config.toml run
```

Backfill from a fixed date:

```bash
python3 -m podcast_radar --config config.toml ingest --since 2026-01-01
python3 -m podcast_radar --config config.toml judge --since 2026-01-01
python3 -m podcast_radar --config config.toml process --since 2026-01-01
python3 -m podcast_radar --config config.toml build-site
```

Scope judging and processing to one or more exact feed names:

```bash
python3 -m podcast_radar --config config.toml judge --since 2025-01-01 --feed "AI & I"
python3 -m podcast_radar --config config.toml process --since 2025-01-01 --feed "AI & I"
```

Collection can likewise be limited to exact source names, which is useful for a focused historical backfill:

```bash
python3 -m podcast_radar --config config.toml ingest --since 2025-01-01 --source "Dwarkesh Podcast — YouTube"
```

Scope by title/description text for targeted backfills:

```bash
python3 -m podcast_radar --config config.toml judge --since 2025-01-01 --match "Gavin Baker"
python3 -m podcast_radar --config config.toml process --since 2025-01-01 --match "Gavin Baker"
```

Serve the generated site locally:

```bash
python3 -m podcast_radar --config config.toml serve-site --port 8088
```

Open:

```text
http://127.0.0.1:8088/
```

## Pipeline Commands

```bash
python3 -m podcast_radar --config config.toml ingest
python3 -m podcast_radar --config config.toml ingest --since 2026-01-01
python3 -m podcast_radar --config config.toml judge --limit 10
python3 -m podcast_radar --config config.toml process --limit 3
python3 -m podcast_radar --config config.toml build-site
python3 -m podcast_radar --config config.toml list
```

The main Radar item statuses are:

- `new`: source metadata has been stored but not judged.
- `skipped`: the LLM decided the item is not relevant or substantial.
- `relevant`: the metadata-only prefilter selected the item for full-text preparation; it is not public.
- `transcribed`: normalized full text exists and full-text verification passed; it is still not public until summarization succeeds. The legacy status name is retained for migration compatibility.
- `published`: full-text verification passed and the shared summary/detail page has been rendered.
- `merged`: the item was a duplicate and its appearances now belong to another canonical item.
- `transcription_failed` / `summary_failed` / `failed`: an item-specific step failed; the reason is stored in `skip_reason`, and the item is not public.

## LLM Configuration

The default provider is OpenAI-compatible and points at OpenRouter:

```toml
[llm]
provider = "openai_compatible"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
model = "minimax/minimax-m3"
```

For a local Ollama-compatible path:

```toml
[llm]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "llama3.1"
api_key_env = ""
```

Transient provider failures are retried automatically. Rate limits (HTTP 429), request timeouts, and 5xx responses back off exponentially and honour a `Retry-After` header when the provider sends one; authentication and request errors still fail immediately so a misconfigured run stops right away. Tune the behaviour under `[llm]`:

```toml
[llm]
timeout_seconds = 120
max_attempts = 4            # set to 1 to disable retries
retry_backoff_seconds = 2.0 # doubles per attempt
max_retry_sleep_seconds = 60.0
```

The judge prompts use the configured lab roster as seed examples for people, not a hard allowlist. Lab labels remain restricted to configured targets and represent where the qualifying person works, not what organizations were discussed; `Physical AI` is the explicit coverage-category exception. Metadata judging is only a prefilter; a second decision over normalized full text determines whether the item is publishable. Podcasts and broad video channels require a qualifying guest or central speaker, except for substantial Physical AI coverage. A watched person's blog or X account can qualify through verified authorship, but routine and promotional posts remain excluded.

## Local Transcription

Transcription is deliberately a command wrapper so the service can use whichever local backend is best on the Mac.

For the local Apple Silicon whisper.cpp setup:

```toml
[transcription]
provider = "command"
command = "whisper-cli"
args = ["-m", "/Users/merimerimeri/.cache/whisper.cpp/ggml-large-v3-turbo-q5_0.bin", "-f", "{audio_path}", "-otxt", "-of", "{output_stem}", "-l", "en", "-bs", "1", "-bo", "1", "-np", "--prompt", "AI podcast transcript. Common terms: OpenAI, Anthropic, Google DeepMind, DeepMind, Meta AI, xAI, NVIDIA, Replit, Hugging Face, CoreWeave, Applied Intuition, ChatGPT, Claude, Gemini, GPT-4, GPT-5, GPT-5.1, o3, Sora, Codex, MCP, model behavior, post-training, reinforcement learning, reasoning models, steerability, inference, agents. Episode metadata: {feed_name}. {episode_title}. Hosts: {episode_hosts}. Description: {episode_description}"]
output_path = "{output_stem}.txt"
```

The `-l en`, `-bs 1`, and `-bo 1` flags favor speed for English podcasts, while `-np` keeps launchd logs quiet. The `--prompt` glossary nudges Whisper toward common AI lab names, show names, technical terms, and per-episode metadata such as `{episode_title}` and `{episode_description}`. The description fragment is capped so the generated prompt stays below Whisper's initial-context ceiling. On an Apple M4 Mac mini with Metal, this model transcribed a 25-minute OpenAI Podcast episode in about 94 seconds and a technical 10-minute sample in about 61 seconds. It was about 44% slower than `small.en` across those samples, while improving important names and technical terms.

For a custom wrapper script:

```toml
[transcription]
provider = "command"
command = "scripts/transcribe-local"
args = ["{audio_path}", "{output_stem}.txt"]
output_path = "{output_stem}.txt"
```

The command must write the transcript to `output_path`. Audio is deleted after a successful transcript unless `keep_audio = true`.

To transcribe backlog candidates without running transcript verification, summarization, site build, or deploy:

```bash
scripts/transcribe-backlog.py --config config.toml --year 2025
```

The backlog script only selects `relevant` episodes that still have an empty transcript, so it can be stopped and rerun without duplicating completed transcripts. Use `--dry-run` to preview candidates, `--limit N` for a smaller batch, and `--retry-failed` to include episodes currently marked `transcription_failed`.

## Public Site

The generated site is static. The config writes:

```text
public/index.html
public/feed.xml
public/episodes/<episode>/index.html
public/CNAME
```

The included `wrangler.toml` is ready for Cloudflare Pages direct upload:

```bash
python3 -m podcast_radar --config config.toml build-site
npm run verify:site
wrangler pages deploy public --project-name ai-radar
```

Point DNS for `ai-radar.merimerimeri.com` at the Pages project, or change `[site].base_url` and `[site].cname` in `config.toml`.

`npm run verify:site` starts a local static server, opens the generated site in Playwright Chromium at desktop and mobile viewport sizes, checks the lab filter and episode action links, and writes screenshots to `var/site-checks/`. The daily and backfill scripts run this browser check before each Cloudflare Pages deploy.

## Hourly LaunchAgent

After the LLM provider and transcription command are configured, install an hourly macOS LaunchAgent. It runs a rolling 2-hour lookback, so reruns overlap safely; duplicate feed items are upserted by `(feed_id, guid)` rather than inserted twice.

```bash
python3 -m podcast_radar --config config.toml launchd-install \
  --interval-minutes 60 \
  --lookback-hours 2 \
  --deploy-project ai-radar
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.merimeri.ai-radar.plist
```

The scheduled runner is `scripts/daily.sh`. It calculates `now - AI_RADAR_LOOKBACK_HOURS`, ingests every active source, and sends the normalized items through the shared classifier. Podcast and YouTube candidates are transcribed locally before final verification; blog posts and substantial X threads already supply their source text. After the rolling window, each run also judges 10 older new items and processes one older relevant item, so missed historical work drains instead of remaining permanently outside the lookback. Set `AI_RADAR_BACKLOG_JUDGE_LIMIT` or `AI_RADAR_BACKLOG_PROCESS_LIMIT` to tune those bounds or to `0` to disable one stage. It then rebuilds the site and deploys `public/` to Cloudflare Pages.

The generated LaunchAgent has `RunAtLoad = true`, so after the machine restarts and the user session is loaded, it runs once immediately in addition to the hourly schedule. The 2-hour lookback is intentional: it gives the service overlap after restarts, sleep, delayed publication, or a missed hourly run, while source identities and canonical-item deduplication prevent repeat entries. The runner also takes a local lock, so an hourly launch exits cleanly if the previous run is still processing.

The daily lock records both a timestamp and owner PID in `var/run/daily.lock/`. If the owner process no longer exists, the next run removes the orphaned lock immediately. Older lock formats without a PID are removed after 24 hours; set `AI_RADAR_LOCK_MAX_AGE_HOURS` to override that fallback window. Failed pipeline or deploy commands are run in a child shell so the parent always removes its lock before returning the failure to `launchd`.

The runner also adds the user's standard `mise` shim directories to the deployment PATH. This lets the LaunchAgent find `node`, `npm`, and `npx` even though it does not load an interactive shell profile.

Hourly processing flow:

1. `launchd` starts `scripts/daily.sh`.
2. `scripts/daily.sh` loads ignored local secrets from `var/secrets.env`.
3. The script computes the rolling cutoff from `AI_RADAR_LOOKBACK_HOURS`.
4. `ingest --since <cutoff>` fetches every active source and upserts source appearances. Strong cross-medium matches attach to one canonical Radar item; ambiguous matches enter the duplicate review queue.
5. `judge --since <cutoff>` asks the LLM whether each new item is substantial and relevant. For audio and video, this is only a candidate prefilter.
6. `process --since <cutoff>` obtains canonical text: local transcription for podcast and YouTube appearances, or the collected article/thread text for blogs and X.
7. The LLM runs the same full-text verification pass for every medium. The canonical text is the source of truth for authors, speakers, affiliations, and substance.
8. A bounded backlog pass judges 10 older candidates and processes one older relevant item per run by default.
9. Items that fail verification are marked skipped and never appear publicly.
10. Items that pass are summarized once, regardless of how many sources carried the material.
11. `build-site` and Wrangler deploy only published items with verified source text and summaries. Their detail pages link to every known appearance.

Radar items are public on the website and RSS feed only after full-text verification and summarization. Metadata-only candidates, transcription failures, summary failures, and false positives stay out of the static site.

Local secrets can be stored outside Git in `var/secrets.env`, for example:

```bash
OPENROUTER_API_KEY=...
```

Logs go to:

```text
var/logs/launchd.out.log
var/logs/launchd.err.log
```

## Development Checks

```bash
python3 -m compileall podcast_radar
python3 -m unittest discover -s tests
npm run verify:site
```
