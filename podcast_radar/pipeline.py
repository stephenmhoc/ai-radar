from __future__ import annotations

from .config import Config
from . import feeds, llm, site, storage, transcriber


def run(
    config: Config,
    conn,
    *,
    limit: int | None = None,
    published_since: str | None = None,
    feed_names: tuple[str, ...] = (),
    search_text: str | None = None,
) -> dict[str, int]:
    stats: dict[str, int] = {}
    since = published_since or config.app.processed_after
    stats.update(
        {f"ingest_{key}": value for key, value in feeds.ingest(config, conn, published_since=since).items()}
    )
    judged = judge_pending(
        config,
        conn,
        limit=limit,
        published_since=since,
        feed_names=feed_names,
        search_text=search_text,
    )
    processed = process_relevant(
        config,
        conn,
        limit=limit,
        published_since=since,
        feed_names=feed_names,
        search_text=search_text,
    )
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
    feed_names: tuple[str, ...] = (),
    search_text: str | None = None,
) -> int:
    count = 0
    since = published_since or config.app.processed_after
    for episode in storage.episodes_for_status(
        conn,
        ("new",),
        limit=limit,
        published_since=since,
        feed_names=feed_names,
        search_text=search_text,
    ):
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
    feed_names: tuple[str, ...] = (),
    search_text: str | None = None,
) -> int:
    count = 0
    since = published_since or config.app.processed_after
    for episode in storage.episodes_for_status(
        conn,
        ("relevant",),
        limit=limit,
        published_since=since,
        feed_names=feed_names,
        search_text=search_text,
    ):
        try:
            transcriber.transcribe_episode(config, conn, episode)
        except Exception as exc:  # noqa: BLE001 - preserve per-episode progress
            storage.mark_processing_failed(
                conn,
                int(episode["id"]),
                stage="transcription",
                reason=f"transcription failed: {exc}",
            )
            conn.commit()
            continue
        refreshed = storage.episode_by_id(conn, int(episode["id"]))
        try:
            verification = llm.verify_transcript_episode(config, conn, refreshed)
            if not verification["include"]:
                continue
            refreshed = storage.episode_by_id(conn, int(episode["id"]))
            llm.summarize_episode(config, conn, refreshed)
            count += 1
        except Exception as exc:  # noqa: BLE001 - preserve per-episode progress
            storage.mark_processing_failed(
                conn,
                int(episode["id"]),
                stage="summary",
                reason=f"summary failed: {exc}",
            )
            conn.commit()
    for episode in storage.episodes_for_status(
        conn,
        ("transcribed", "summary_failed"),
        limit=limit,
        published_since=since,
        feed_names=feed_names,
        search_text=search_text,
    ):
        try:
            verification = llm.verify_transcript_episode(config, conn, episode)
            if not verification["include"]:
                continue
            refreshed = storage.episode_by_id(conn, int(episode["id"]))
            llm.summarize_episode(config, conn, refreshed)
            count += 1
        except Exception as exc:  # noqa: BLE001 - preserve per-episode progress
            storage.mark_processing_failed(
                conn,
                int(episode["id"]),
                stage="summary",
                reason=f"summary failed: {exc}",
            )
            conn.commit()
    return count
