from __future__ import annotations

from .config import Config
from . import collectors, distributed, llm, site, storage, transcribe_anything, transcriber


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
        {f"ingest_{key}": value for key, value in collectors.collect(config, conn, published_since=since).items()}
    )
    judged = judge_pending(
        config,
        conn,
        limit=limit,
        published_since=since,
        feed_names=feed_names,
        search_text=search_text,
    )
    # Dispatch before summarizing so the worker transcribes in parallel with the
    # coordinator's LLM work instead of after it.
    if config.transcription.mode == "remote":
        dispatch = dispatch_transcriptions(config, conn, limit=limit, published_since=since)
        stats.update({f"dispatch_{key}": value for key, value in dispatch.items()})
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
    stats["rendered"] = rendered.get("items", rendered["episodes"])
    return stats


def collect_transcriptions(config: Config, conn) -> dict[str, int]:
    """Pick up finished transcripts, from whichever coordinator is in use."""
    if config.transcription.mode == "service":
        return transcribe_anything.collect(config, conn)

    return distributed.import_results(config, conn, config.transcription.queue_root)


def dispatch_transcriptions(
    config: Config,
    conn,
    *,
    limit: int | None = None,
    published_since: str | None = None,
) -> dict[str, int]:
    """Queue transcription work for the Mac worker and nudge it.

    The kick is best effort: if the Mac is asleep the job stays queued and the
    worker's own timer collects it, so this never blocks the run.
    """
    if config.transcription.mode == "service":
        # One shared corpus: anything already transcribed comes back now, and
        # anything new is queued once for the whole fleet.
        return transcribe_anything.dispatch(
            config, conn, limit=limit, published_since=published_since
        )

    stats = distributed.enqueue_pending(
        config,
        conn,
        config.transcription.queue_root,
        limit=limit,
        published_since=published_since,
        lease_hours=config.transcription.lease_hours,
    )
    if stats.get("enqueued") and config.transcription.worker_ssh and config.transcription.worker_command:
        stats["kicked"] = int(
            distributed.kick(config.transcription.worker_ssh, config.transcription.worker_command)
        )
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
    remote = config.transcription.mode in ("remote", "service")
    if remote:
        # Import first: transcripts that came back since the last run become
        # 'transcribed', and the second loop below then summarizes them.
        collect_transcriptions(config, conn)
    for episode in storage.episodes_for_status(
        conn,
        ("relevant",),
        limit=limit,
        published_since=since,
        feed_names=feed_names,
        search_text=search_text,
    ):
        try:
            if episode.get("content_text"):
                storage.set_content(conn, int(episode["id"]), str(episode["content_text"]))
                conn.commit()
            elif remote:
                # Nothing to do inline; the item stays 'relevant' until a worker
                # returns its transcript. dispatch_transcriptions() queues it.
                continue
            else:
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
