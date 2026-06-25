# AI Radar

Local service that watches podcast RSS feeds for episodes featuring technical members or executives from major AI labs, transcribes matching episodes with a local model, summarizes them with an LLM, and publishes a static site plus RSS feed.

The public site is generated into `public/` and is designed to be deployed to a subdomain such as:

```text
https://llm-podcasts.merimerimeri.com
```

## What It Does

- Fetches configured podcast feeds.
- Stores episode metadata in SQLite.
- Asks an LLM to judge whether each new episode has a qualifying guest from OpenAI, Anthropic, Google DeepMind, Meta, xAI, or NVIDIA.
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

Install or point the config at a local transcription command. The default assumes `whisper-cli` from whisper.cpp:

```toml
[transcription]
command = "whisper-cli"
args = ["-m", "models/ggml-large-v3-turbo.bin", "-f", "{audio_path}", "-otxt", "-of", "{output_stem}"]
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
- `relevant`: LLM decided the episode should be transcribed.
- `transcribed`: local transcript exists and is stored in SQLite.
- `published`: summary and transcript page have been rendered.
- `failed`: an episode-specific step failed; the reason is stored in `skip_reason`.

## LLM Configuration

The default provider is OpenAI-compatible and points at OpenRouter:

```toml
[llm]
provider = "openai_compatible"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
model = "openai/gpt-4.1-mini"
```

For a local Ollama-compatible path:

```toml
[llm]
provider = "ollama"
base_url = "http://127.0.0.1:11434"
model = "llama3.1"
api_key_env = ""
```

The judge prompt uses the configured lab roster as seed examples, not a hard allowlist. It is allowed to include other current or recent qualifying people when the feed metadata clearly states their lab affiliation.

## Local Transcription

Transcription is deliberately a command wrapper so the service can use whichever local backend is best on the Mac.

For whisper.cpp:

```toml
[transcription]
provider = "command"
command = "whisper-cli"
args = ["-m", "models/ggml-large-v3-turbo.bin", "-f", "{audio_path}", "-otxt", "-of", "{output_stem}"]
output_path = "{output_stem}.txt"
```

For a custom wrapper script:

```toml
[transcription]
provider = "command"
command = "scripts/transcribe-local"
args = ["{audio_path}", "{output_stem}.txt"]
output_path = "{output_stem}.txt"
```

The command must write the transcript to `output_path`. Audio is deleted after a successful transcript unless `keep_audio = true`.

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
wrangler pages deploy public --project-name ai-radar
```

Point DNS for `llm-podcasts.merimerimeri.com` at the Pages project, or change `[site].base_url` and `[site].cname` in `config.toml`.

## Daily LaunchAgent

After the LLM provider and transcription command are configured, install a daily macOS LaunchAgent. It runs a rolling lookback, so reruns overlap safely; duplicate feed items are upserted by `(feed_id, guid)` rather than inserted twice.

```bash
python3 -m podcast_radar --config config.toml launchd-install \
  --hour 8 \
  --minute 30 \
  --lookback-hours 36 \
  --deploy-project ai-radar
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.merimeri.ai-radar.plist
```

The scheduled runner is `scripts/daily.sh`. It calculates `now - AI_RADAR_LOOKBACK_HOURS`, runs `podcast_radar run --since <cutoff>`, rebuilds the static site, and deploys `public/` to Cloudflare Pages.

The generated LaunchAgent has `RunAtLoad = true`, so after the machine restarts and the user session is loaded, it runs once immediately in addition to the daily 8:30 AM schedule. The 36-hour lookback is intentional: it gives the service overlap after restarts, sleep, delayed feed publication, or a missed daily run, while the database uniqueness constraint prevents duplicate episodes.

Daily processing flow:

1. `launchd` starts `scripts/daily.sh`.
2. `scripts/daily.sh` loads ignored local secrets from `var/secrets.env`.
3. The script computes the rolling cutoff from `AI_RADAR_LOOKBACK_HOURS`.
4. `podcast_radar run --since <cutoff>` fetches active feeds, upserts episodes, asks the LLM to judge new items, transcribes relevant items locally, summarizes them, and rebuilds the static site.
5. The script deploys `public/` to Cloudflare Pages with Wrangler.

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
```
