from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
from typing import Any, Iterable

from .config import Config, FeedConfig


PUBLIC_STATUSES = ("relevant", "transcribed", "published", "transcription_failed", "summary_failed")


def connect(config: Config) -> sqlite3.Connection:
    config.app.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.app.database_path)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS feeds (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          url TEXT NOT NULL UNIQUE,
          hosts_json TEXT NOT NULL DEFAULT '[]',
          image_url TEXT,
          homepage_url TEXT,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS episodes (
          id INTEGER PRIMARY KEY,
          feed_id INTEGER NOT NULL REFERENCES feeds(id),
          guid TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT,
          episode_url TEXT,
          audio_url TEXT,
          audio_type TEXT,
          image_url TEXT,
          published_at TEXT,
          duration TEXT,
          raw_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'new',
          skip_reason TEXT,
          relevance_score REAL,
          labs_json TEXT NOT NULL DEFAULT '[]',
          matched_people_json TEXT NOT NULL DEFAULT '[]',
          hosts_json TEXT NOT NULL DEFAULT '[]',
          guests_json TEXT NOT NULL DEFAULT '[]',
          judged_at TEXT,
          transcript_path TEXT,
          transcript_text TEXT,
          transcribed_at TEXT,
          summary_title TEXT,
          summary_text TEXT,
          summary_html TEXT,
          key_points_json TEXT NOT NULL DEFAULT '[]',
          topics_json TEXT NOT NULL DEFAULT '[]',
          summarized_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(feed_id, guid)
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
        CREATE INDEX IF NOT EXISTS idx_episodes_published ON episodes(published_at);

        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY,
          episode_id INTEGER NOT NULL REFERENCES episodes(id),
          stage TEXT NOT NULL,
          model TEXT NOT NULL,
          prompt_json TEXT NOT NULL,
          response_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return [] if default is None else default
    return json.loads(value)


def upsert_feed(conn: sqlite3.Connection, feed: FeedConfig) -> int:
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO feeds (name, url, hosts_json, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          name = excluded.name,
          hosts_json = excluded.hosts_json,
          active = excluded.active,
          updated_at = excluded.updated_at
        """,
        (feed.name, feed.url, dumps(list(feed.hosts)), int(feed.active), timestamp, timestamp),
    )
    row = conn.execute("SELECT id FROM feeds WHERE url = ?", (feed.url,)).fetchone()
    if row is None:
        raise RuntimeError(f"feed upsert failed for {feed.url}")
    return int(row["id"])


def update_feed_metadata(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    image_url: str | None,
    homepage_url: str | None,
) -> None:
    conn.execute(
        """
        UPDATE feeds
        SET image_url = COALESCE(?, image_url),
            homepage_url = COALESCE(?, homepage_url),
            updated_at = ?
        WHERE id = ?
        """,
        (image_url or None, homepage_url or None, now_iso(), feed_id),
    )


def upsert_episode(conn: sqlite3.Connection, feed_id: int, episode: dict[str, Any]) -> tuple[int, bool]:
    timestamp = now_iso()
    existing = conn.execute(
        "SELECT id FROM episodes WHERE feed_id = ? AND guid = ?",
        (feed_id, episode["guid"]),
    ).fetchone()
    inserted = existing is None
    if inserted:
        conn.execute(
            """
            INSERT INTO episodes (
              feed_id, guid, title, description, episode_url, audio_url, audio_type,
              image_url, published_at, duration, raw_json, hosts_json,
              created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feed_id,
                episode["guid"],
                episode["title"],
                episode.get("description"),
                episode.get("episode_url"),
                episode.get("audio_url"),
                episode.get("audio_type"),
                episode.get("image_url"),
                episode.get("published_at"),
                episode.get("duration"),
                dumps(episode.get("raw", {})),
                dumps(episode.get("hosts", [])),
                timestamp,
                timestamp,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE episodes
            SET title = ?,
                description = ?,
                episode_url = ?,
                audio_url = ?,
                audio_type = ?,
                image_url = ?,
                published_at = ?,
                duration = ?,
                raw_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                episode["title"],
                episode.get("description"),
                episode.get("episode_url"),
                episode.get("audio_url"),
                episode.get("audio_type"),
                episode.get("image_url"),
                episode.get("published_at"),
                episode.get("duration"),
                dumps(episode.get("raw", {})),
                timestamp,
                int(existing["id"]),
            ),
        )
    row = conn.execute(
        "SELECT id FROM episodes WHERE feed_id = ? AND guid = ?",
        (feed_id, episode["guid"]),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"episode upsert failed for {episode['guid']}")
    return int(row["id"]), inserted


def add_decision(
    conn: sqlite3.Connection,
    episode_id: int,
    *,
    stage: str,
    model: str,
    prompt: dict[str, Any],
    response: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO decisions (episode_id, stage, model, prompt_json, response_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (episode_id, stage, model, dumps(prompt), dumps(response), now_iso()),
    )


def set_judgement(conn: sqlite3.Connection, episode_id: int, result: dict[str, Any]) -> None:
    include = bool(result.get("include"))
    status = "relevant" if include else "skipped"
    conn.execute(
        """
        UPDATE episodes
        SET status = ?,
            skip_reason = ?,
            relevance_score = ?,
            labs_json = ?,
            matched_people_json = ?,
            guests_json = ?,
            judged_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            result.get("reason"),
            result.get("confidence"),
            dumps(_as_list(result.get("labs"))),
            dumps(_as_list(result.get("matched_people"))),
            dumps(_as_list(result.get("guest_names"))),
            now_iso(),
            now_iso(),
            episode_id,
        ),
    )


def set_transcript(conn: sqlite3.Connection, episode_id: int, transcript: str, path: pathlib.Path) -> None:
    conn.execute(
        """
        UPDATE episodes
        SET transcript_text = ?,
            transcript_path = ?,
            transcribed_at = ?,
            status = 'transcribed',
            updated_at = ?
        WHERE id = ?
        """,
        (transcript, str(path), now_iso(), now_iso(), episode_id),
    )


def set_summary(conn: sqlite3.Connection, episode_id: int, result: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE episodes
        SET summary_title = ?,
            summary_text = ?,
            summary_html = ?,
            key_points_json = ?,
            topics_json = ?,
            hosts_json = CASE WHEN ? != '[]' THEN ? ELSE hosts_json END,
            guests_json = CASE WHEN ? != '[]' THEN ? ELSE guests_json END,
            labs_json = CASE WHEN ? != '[]' THEN ? ELSE labs_json END,
            summarized_at = ?,
            status = 'published',
            updated_at = ?
        WHERE id = ?
        """,
        (
            result.get("title"),
            result.get("summary"),
            result.get("summary_html"),
            dumps(_as_list(result.get("key_points"))),
            dumps(_as_list(result.get("topics"))),
            dumps(_as_list(result.get("hosts"))),
            dumps(_as_list(result.get("hosts"))),
            dumps(_as_list(result.get("guests"))),
            dumps(_as_list(result.get("guests"))),
            dumps(_as_list(result.get("labs"))),
            dumps(_as_list(result.get("labs"))),
            now_iso(),
            now_iso(),
            episode_id,
        ),
    )


def mark_failed(conn: sqlite3.Connection, episode_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE episodes SET status = 'failed', skip_reason = ?, updated_at = ? WHERE id = ?",
        (reason, now_iso(), episode_id),
    )


def mark_processing_failed(conn: sqlite3.Connection, episode_id: int, *, stage: str, reason: str) -> None:
    if stage not in {"transcription", "summary"}:
        raise ValueError(f"unsupported processing failure stage: {stage}")
    status = "transcription_failed" if stage == "transcription" else "summary_failed"
    conn.execute(
        "UPDATE episodes SET status = ?, skip_reason = ?, updated_at = ? WHERE id = ?",
        (status, reason, now_iso(), episode_id),
    )


def episodes_for_status(
    conn: sqlite3.Connection,
    statuses: Iterable[str],
    *,
    limit: int | None = None,
    published_since: str | None = None,
) -> list[sqlite3.Row]:
    values = tuple(statuses)
    placeholders = ",".join("?" for _ in values)
    sql = f"""
        SELECT episodes.*, feeds.name AS feed_name, feeds.url AS feed_url,
               feeds.image_url AS feed_image_url, feeds.hosts_json AS feed_hosts_json
        FROM episodes
        JOIN feeds ON feeds.id = episodes.feed_id
        WHERE episodes.status IN ({placeholders})
    """
    params: list[Any] = list(values)
    if published_since:
        sql += " AND (episodes.published_at IS NULL OR episodes.published_at >= ?)"
        params.append(published_since)
    sql += " ORDER BY COALESCE(episodes.published_at, episodes.created_at) DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def public_episodes(conn: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in PUBLIC_STATUSES)
    sql = """
        SELECT episodes.*, feeds.name AS feed_name, feeds.url AS feed_url,
               feeds.image_url AS feed_image_url, feeds.homepage_url AS feed_homepage_url,
               feeds.hosts_json AS feed_hosts_json
        FROM episodes
        JOIN feeds ON feeds.id = episodes.feed_id
        WHERE episodes.status IN ({placeholders})
        ORDER BY COALESCE(episodes.published_at, episodes.summarized_at, episodes.created_at) DESC
    """.format(placeholders=placeholders)
    params: list[Any] = list(PUBLIC_STATUSES)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def episode_by_id(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT episodes.*, feeds.name AS feed_name, feeds.url AS feed_url,
               feeds.image_url AS feed_image_url, feeds.hosts_json AS feed_hosts_json
        FROM episodes
        JOIN feeds ON feeds.id = episodes.feed_id
        WHERE episodes.id = ?
        """,
        (episode_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"episode {episode_id} not found")
    return row


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS count FROM episodes GROUP BY status").fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]
