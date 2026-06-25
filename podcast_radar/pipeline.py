from __future__ import annotations

from .config import Config
from . import feeds, llm, site, storage, transcriber


def run(config: Config, conn, *, limit: int | None = None, published_since: str | None = None) -> dict[str, int]:
    stats: dict[str, int] = {}
    since = published_since or config.app.processed_after
    stats.update(
        {f"ingest_{key}": value for key, value in feeds.ingest(config, conn, published_since=since).items()}
    )
    judged = judge_pending(config, conn, limit=limit, published_since=since)
    processed = process_relevant(config, conn, limit=limit, published_since=since)
    rendered = site.build_site(config, conn)
    stats["judged"] = judged
    stats["processed"] = processed
    stats["rendered"] = rendered["episodes"]
    return stats


def judge_pending(
    config: Config,
    conn,
    *,
    limit: int | None = None,
    published_since: str | None = None,
) -> int:
    count = 0
    since = published_since or config.app.processed_after
    for episode in storage.episodes_for_status(conn, ("new",), limit=limit, published_since=since):
        try:
            llm.judge_episode(config, conn, episode)
            count += 1
        except llm.LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve per-episode progress
            storage.mark_failed(conn, int(episode["id"]), f"judge failed: {exc}")
            conn.commit()
    return count


def process_relevant(
    config: Config,
    conn,
    *,
    limit: int | None = None,
    published_since: str | None = None,
) -> int:
    count = 0
    since = published_since or config.app.processed_after
    for episode in storage.episodes_for_status(conn, ("relevant",), limit=limit, published_since=since):
        try:
            transcriber.transcribe_episode(config, conn, episode)
            refreshed = storage.episode_by_id(conn, int(episode["id"]))
            llm.summarize_episode(config, conn, refreshed)
            count += 1
        except llm.LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve per-episode progress
            storage.mark_failed(conn, int(episode["id"]), f"process failed: {exc}")
            conn.commit()
    for episode in storage.episodes_for_status(conn, ("transcribed",), limit=limit, published_since=since):
        try:
            llm.summarize_episode(config, conn, episode)
            count += 1
        except llm.LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            storage.mark_failed(conn, int(episode["id"]), f"summary failed: {exc}")
            conn.commit()
    return count
