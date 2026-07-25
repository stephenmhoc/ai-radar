# AI Radar

Local service that watches podcast RSS feeds for episodes featuring technical members or executives from major AI labs, transcribes matching episodes with a local model, summarizes them with an LLM, and publishes a static site plus RSS feed.

The public site is generated into `public/` and is designed to be deployed to a subdomain such as:

```text
https://ai-radar.merimerimeri.com
```

## What It Does

- Fetches configured podcast feeds.
- Stores episode metadata in SQLite.
- Asks an LLM to judge whether each new episode has a qualifying guest from a configured target organization. The current targets include OpenAI, Anthropic, Google DeepMind, Meta, xAI, NVIDIA, Replit, Hugging Face, CoreWeave, Applied Intuition, and Atreides Management.
- Skips non-matching episodes without downloading audio.
- Downloads and transcribes matching episodes with a local command such as `whisper-cli` or an MLX Whisper wrapper.
- Summarizes the transcript with the configured LLM.
- Stores transcripts in SQLite and renders transcript pages in the static site.
- Renders `public/index.html`, `public/feed.xml`, and per-episode pages.

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

The main episode statuses are:

- `new`: feed metadata has been stored but not judged.
- `skipped`: LLM decided the episode is not relevant.
- `relevant`: metadata-only LLM prefilter decided the episode should be transcribed; this is an internal candidate and is not public.
- `transcribed`: local transcript exists and transcript-based verification passed; this is still not public until summarization succeeds.
- `published`: transcript-based verification passed and summary plus transcript page have been rendered.
- `transcription_failed` / `summary_failed` / `failed`: an episode-specific step failed; the reason is stored in `skip_reason`, and the episode is not public.

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

The judge prompts use the configured lab roster as seed examples for people, not a hard allowlist of people. Lab labels themselves are restricted to the configured target labs. Metadata judging is only a prefilter; after local transcription, a second transcript-based judge decides whether the episode is actually publishable and confirms that labels represent where the guest works, not what companies were discussed.

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

The scheduled runner is `scripts/daily.sh`. It calculates `now - AI_RADAR_LOOKBACK_HOURS`, ingests new episodes, and uses metadata-only judging as an internal prefilter for transcription. It does not publish those candidates. It then transcribes candidates locally, asks the LLM to make a second publication decision using the transcript, summarizes verified episodes, rebuilds the site, and deploys `public/` to Cloudflare Pages.

The generated LaunchAgent has `RunAtLoad = true`, so after the machine restarts and the user session is loaded, it runs once immediately in addition to the hourly schedule. The 2-hour lookback is intentional: it gives the service overlap after restarts, sleep, delayed feed publication, or a missed hourly run, while the database uniqueness constraint prevents duplicate episodes. The runner also takes a local lock, so an hourly launch exits cleanly if the previous run is still processing.

The daily lock records both a timestamp and owner PID in `var/run/daily.lock/`. If the owner process no longer exists, the next run removes the orphaned lock immediately. Older lock formats without a PID are removed after 24 hours; set `AI_RADAR_LOCK_MAX_AGE_HOURS` to override that fallback window. Failed pipeline or deploy commands are run in a child shell so the parent always removes its lock before returning the failure to `launchd`.

The runner also adds the user's standard `mise` shim directories to the deployment PATH. This lets the LaunchAgent find `node`, `npm`, and `npx` even though it does not load an interactive shell profile.

Hourly processing flow:

1. `launchd` starts `scripts/daily.sh`.
2. `scripts/daily.sh` loads ignored local secrets from `var/secrets.env`.
3. The script computes the rolling cutoff from `AI_RADAR_LOOKBACK_HOURS`.
4. `ingest --since <cutoff>` fetches active feeds and upserts episodes. Duplicate feed items are updated by `(feed_id, guid)`.
5. `judge --since <cutoff>` asks the LLM to decide which new episodes are worth transcribing. This is only a candidate prefilter.
6. `process --since <cutoff>` transcribes candidate episodes locally.
7. After transcription, the LLM runs a transcript-based verification pass. The transcript is treated as the source of truth for who the guest is and where they work.
8. If transcript verification says the guest is not a current or recent technical/executive member of a configured target lab, the episode is marked skipped and never appears on the site.
9. If transcript verification passes, the episode is summarized.
10. `build-site` and Wrangler deploy only published episodes with both transcript and summary available.

Episodes are public on the website and RSS feed only after transcript-based verification and summarization. Metadata-only candidates, transcription failures, summary failures, and transcript false positives stay out of the static site.

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
