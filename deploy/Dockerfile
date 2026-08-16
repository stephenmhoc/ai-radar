# Node base because the only non-Python dependency is wrangler, used to publish
# the built site to Cloudflare Pages. Python here is stdlib-only (ai-radar
# declares no dependencies), so there is nothing to pip install.
#
# Playwright is deliberately absent: `npm run verify:site` stays on the Mac. Its
# browsers are ~2.5GB and this VM has 2 vCPU shared with the monitoring stack.
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      ca-certificates \
      curl \
      openssh-client \
      rsync \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp is needed for YouTube *playlist collection*, not just downloading, so
# the coordinator needs it even though transcription happens on the Mac. The
# standalone binary avoids pulling in pip; ffmpeg is not needed here because
# audio extraction only ever runs on the worker.
RUN curl -fsSL -o /usr/local/bin/yt-dlp \
      https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux \
    && chmod +x /usr/local/bin/yt-dlp \
    && yt-dlp --version

# Pinned into the image rather than fetched by `npx -y` at deploy time, so an
# hourly run never depends on the npm registry being reachable.
RUN npm install -g wrangler@4 && npm cache clean --force

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY podcast_radar ./podcast_radar
COPY deploy/run-cycle.sh ./deploy/run-cycle.sh
RUN chmod +x ./deploy/run-cycle.sh

ENTRYPOINT ["python3", "-m", "podcast_radar"]
