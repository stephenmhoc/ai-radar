#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import pathlib
import sqlite3
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from podcast_radar import storage, transcriber  # noqa: E402
from podcast_radar.cli import transcription_preflight_warnings  # noqa: E402
from podcast_radar.config import Config, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)

    warnings = transcription_preflight_warnings(config)
    if warnings:
        for warning in warnings:
            print(f"error: {warning}", file=sys.stderr)
        return 2

    with optional_lock(config, disabled=args.no_lock):
        with storage.connect(config) as conn:
            episodes = backlog_episodes(
                conn,
                retry_failed=args.retry_failed,
                limit=args.limit,
                year=args.year,
                since=args.since,
                until=args.until,
                oldest_first=args.oldest_first,
            )
            if args.dry_run:
                print(f"candidates={len(episodes)}")
                for episode in episodes:
                    print(f"{episode['id']}\t{episode['status']}\t{episode['published_at']}\t{episode['feed_name']}\t{episode['title']}")
                return 0
            return transcribe_backlog(config, conn, episodes, quiet_command=not args.show_command_output)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe backlog episodes only. Does not run transcript verification, summarization, site build, or deploy."
    )
    parser.add_argument("--config", default="config.toml", help="Path to config TOML.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum episodes to transcribe.")
    parser.add_argument("--year", type=int, default=None, help="Only transcribe episodes published in this calendar year.")
    parser.add_argument("--since", default=None, help="Only transcribe episodes published on or after this ISO date/time.")
    parser.add_argument("--until", default=None, help="Only transcribe episodes published before this ISO date/time.")
    parser.add_argument("--oldest-first", action="store_true", help="Transcribe oldest matching episodes first.")
    parser.add_argument("--retry-failed", action="store_true", help="Also retry episodes currently marked transcription_failed.")
    parser.add_argument("--dry-run", action="store_true", help="List matching episodes without transcribing.")
    parser.add_argument("--show-command-output", action="store_true", help="Let the transcription backend write directly to stdout/stderr.")
    parser.add_argument("--no-lock", action="store_true", help="Skip the local backlog lock.")
    return parser.parse_args(argv)


@contextmanager
def optional_lock(config: Config, *, disabled: bool) -> Iterator[None]:
    if disabled:
        yield
        return
    run_dir = config.app.state_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / "transcribe-backlog.lock"
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"another transcript backlog run is already active: {lock_path}", file=sys.stderr)
            raise SystemExit(0)
        yield


def backlog_episodes(
    conn: sqlite3.Connection,
    *,
    retry_failed: bool,
    limit: int | None,
    year: int | None,
    since: str | None,
    until: str | None,
    oldest_first: bool,
) -> list[sqlite3.Row]:
    statuses = ["relevant"]
    if retry_failed:
        statuses.append("transcription_failed")
    placeholders = ",".join("?" for _ in statuses)
    sql = f"""
        SELECT episodes.*, feeds.name AS feed_name, feeds.url AS feed_url,
               feeds.image_url AS feed_image_url, feeds.hosts_json AS feed_hosts_json
        FROM episodes
        JOIN feeds ON feeds.id = episodes.feed_id
        WHERE episodes.status IN ({placeholders})
          AND episodes.audio_url IS NOT NULL
          AND trim(episodes.audio_url) != ''
          AND (episodes.transcript_text IS NULL OR length(trim(episodes.transcript_text)) = 0)
    """
    params: list[Any] = list(statuses)
    if year is not None:
        sql += " AND episodes.published_at >= ? AND episodes.published_at < ?"
        params.extend((f"{year:04d}-01-01", f"{year + 1:04d}-01-01"))
    if since:
        sql += " AND (episodes.published_at IS NULL OR episodes.published_at >= ?)"
        params.append(since)
    if until:
        sql += " AND (episodes.published_at IS NULL OR episodes.published_at < ?)"
        params.append(until)
    direction = "ASC" if oldest_first else "DESC"
    sql += f" ORDER BY COALESCE(episodes.published_at, episodes.created_at) {direction}, episodes.id {direction}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def transcribe_backlog(
    config: Config,
    conn: sqlite3.Connection,
    episodes: Iterable[sqlite3.Row],
    *,
    quiet_command: bool,
) -> int:
    successes = 0
    failures = 0
    total_started = time.monotonic()
    episodes = list(episodes)
    print(f"candidates={len(episodes)}")
    for episode in episodes:
        started = time.monotonic()
        episode_id = int(episode["id"])
        print(f"starting id={episode_id} podcast={episode['feed_name']} title={episode['title']}", flush=True)
        try:
            path = transcriber.transcribe_episode(config, conn, episode, command_output=not quiet_command)
        except Exception as exc:  # noqa: BLE001 - preserve per-episode progress across long backlog runs
            failures += 1
            storage.mark_processing_failed(
                conn,
                episode_id,
                stage="transcription",
                reason=f"transcription failed: {exc}",
            )
            conn.commit()
            print(f"failed id={episode_id} seconds={time.monotonic() - started:.2f} error={exc}", flush=True)
            continue

        refreshed = storage.episode_by_id(conn, episode_id)
        transcript_chars = len(str(refreshed["transcript_text"] or ""))
        successes += 1
        print(
            f"done id={episode_id} seconds={time.monotonic() - started:.2f} chars={transcript_chars} path={path}",
            flush=True,
        )

    print("summary:")
    print(f"successes={successes}")
    print(f"failures={failures}")
    print(f"total_seconds={time.monotonic() - total_started:.2f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
