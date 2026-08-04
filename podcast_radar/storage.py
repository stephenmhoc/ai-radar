from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sqlite3
from difflib import SequenceMatcher
from typing import Any, Iterable

from .config import Config, FeedConfig, SourceConfig


PUBLIC_STATUSES = ("published",)
AUTO_MERGE_SCORE = 0.95
REVIEW_SCORE = 0.82


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

        -- Legacy tables remain in place so existing databases can be migrated
        -- without changing stable episode IDs or RSS GUIDs.
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
          short_summary_text TEXT,
          summary_html TEXT,
          key_points_json TEXT NOT NULL DEFAULT '[]',
          topics_json TEXT NOT NULL DEFAULT '[]',
          summarized_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(feed_id, guid)
        );

        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY,
          episode_id INTEGER NOT NULL REFERENCES episodes(id),
          stage TEXT NOT NULL,
          model TEXT NOT NULL,
          prompt_json TEXT NOT NULL,
          response_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL CHECK(kind IN ('podcast', 'youtube', 'blog', 'x')),
          name TEXT NOT NULL,
          url TEXT NOT NULL,
          external_id TEXT NOT NULL DEFAULT '',
          feed_url TEXT,
          people_json TEXT NOT NULL DEFAULT '[]',
          hosts_json TEXT NOT NULL DEFAULT '[]',
          image_url TEXT,
          homepage_url TEXT,
          cursor TEXT,
          last_success_at TEXT,
          last_error TEXT,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(kind, url)
        );

        CREATE TABLE IF NOT EXISTS radar_items (
          id INTEGER PRIMARY KEY,
          title TEXT NOT NULL,
          description TEXT,
          image_url TEXT,
          published_at TEXT,
          duration TEXT,
          status TEXT NOT NULL DEFAULT 'new',
          skip_reason TEXT,
          relevance_score REAL,
          labs_json TEXT NOT NULL DEFAULT '[]',
          matched_people_json TEXT NOT NULL DEFAULT '[]',
          authors_json TEXT NOT NULL DEFAULT '[]',
          hosts_json TEXT NOT NULL DEFAULT '[]',
          guests_json TEXT NOT NULL DEFAULT '[]',
          judged_at TEXT,
          content_path TEXT,
          content_text TEXT,
          content_hash TEXT,
          content_prepared_at TEXT,
          summary_title TEXT,
          summary_text TEXT,
          short_summary_text TEXT,
          summary_html TEXT,
          key_points_json TEXT NOT NULL DEFAULT '[]',
          topics_json TEXT NOT NULL DEFAULT '[]',
          summarized_at TEXT,
          merged_into_item_id INTEGER REFERENCES radar_items(id),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS appearances (
          id INTEGER PRIMARY KEY,
          item_id INTEGER NOT NULL REFERENCES radar_items(id),
          source_id INTEGER NOT NULL REFERENCES sources(id),
          external_id TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT,
          url TEXT,
          media_url TEXT,
          media_type TEXT,
          image_url TEXT,
          published_at TEXT,
          duration TEXT,
          authors_json TEXT NOT NULL DEFAULT '[]',
          raw_json TEXT NOT NULL DEFAULT '{}',
          content_text TEXT,
          content_hash TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(source_id, external_id)
        );

        CREATE TABLE IF NOT EXISTS item_decisions (
          id INTEGER PRIMARY KEY,
          item_id INTEGER NOT NULL REFERENCES radar_items(id),
          stage TEXT NOT NULL,
          model TEXT NOT NULL,
          prompt_json TEXT NOT NULL,
          response_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dedupe_candidates (
          id INTEGER PRIMARY KEY,
          item_id INTEGER NOT NULL REFERENCES radar_items(id),
          candidate_item_id INTEGER NOT NULL REFERENCES radar_items(id),
          score REAL NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL,
          UNIQUE(item_id, candidate_item_id)
        );

        CREATE INDEX IF NOT EXISTS idx_radar_items_status ON radar_items(status);
        CREATE INDEX IF NOT EXISTS idx_radar_items_published ON radar_items(published_at);
        CREATE INDEX IF NOT EXISTS idx_appearances_item ON appearances(item_id);
        CREATE INDEX IF NOT EXISTS idx_appearances_published ON appearances(published_at);
        CREATE INDEX IF NOT EXISTS idx_appearances_content_hash ON appearances(content_hash);
        """
    )
    _ensure_column(conn, "episodes", "short_summary_text", "TEXT")
    _migrate_legacy_rows(conn)
    conn.commit()


def _migrate_legacy_rows(conn: sqlite3.Connection) -> None:
    episode_columns = _columns(conn, "episodes")
    feed_columns = _columns(conn, "feeds")
    required_episode_columns = {
        "id", "feed_id", "guid", "title", "description", "episode_url", "audio_url",
        "audio_type", "image_url", "published_at", "duration", "raw_json", "status",
        "skip_reason", "relevance_score", "labs_json", "matched_people_json", "hosts_json",
        "guests_json", "judged_at", "transcript_path", "transcript_text", "transcribed_at",
        "summary_title", "summary_text", "short_summary_text", "summary_html", "key_points_json",
        "topics_json", "summarized_at", "created_at", "updated_at",
    }
    if not required_episode_columns.issubset(episode_columns) or not {
        "id", "name", "url", "hosts_json", "image_url", "homepage_url", "active", "created_at", "updated_at"
    }.issubset(feed_columns):
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO sources (
          id, kind, name, url, feed_url, hosts_json, image_url, homepage_url,
          active, created_at, updated_at
        )
        SELECT id, 'podcast', name, url, url, hosts_json, image_url, homepage_url,
               active, created_at, updated_at
        FROM feeds
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO radar_items (
          id, title, description, image_url, published_at, duration, status, skip_reason,
          relevance_score, labs_json, matched_people_json, hosts_json, guests_json,
          judged_at, content_path, content_text, content_hash, content_prepared_at,
          summary_title, summary_text, short_summary_text, summary_html, key_points_json,
          topics_json, summarized_at, created_at, updated_at
        )
        SELECT id, title, description, image_url, published_at, duration, status, skip_reason,
               relevance_score, labs_json, matched_people_json, hosts_json, guests_json,
               judged_at, transcript_path, transcript_text, NULL,
               transcribed_at, summary_title, summary_text, short_summary_text, summary_html,
               key_points_json, topics_json, summarized_at, created_at, updated_at
        FROM episodes
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO appearances (
          id, item_id, source_id, external_id, title, description, url, media_url,
          media_type, image_url, published_at, duration, authors_json, raw_json,
          content_text, content_hash, created_at, updated_at
        )
        SELECT id, id, feed_id, guid, title, description, episode_url, audio_url,
               audio_type, image_url, published_at, duration, hosts_json, raw_json,
               transcript_text, NULL,
               created_at, updated_at
        FROM episodes
        """
    )
    for row in conn.execute(
        "SELECT id, content_text FROM radar_items WHERE content_text IS NOT NULL AND content_text != '' AND content_hash IS NULL"
    ):
        content_hash = _content_hash(str(row["content_text"]))
        conn.execute("UPDATE radar_items SET content_hash = ? WHERE id = ?", (content_hash, int(row["id"])))
        conn.execute("UPDATE appearances SET content_hash = ? WHERE item_id = ?", (content_hash, int(row["id"])))
    if {"id", "episode_id", "stage", "model", "prompt_json", "response_json", "created_at"}.issubset(
        _columns(conn, "decisions")
    ):
        conn.execute(
            """
            INSERT OR IGNORE INTO item_decisions (id, item_id, stage, model, prompt_json, response_json, created_at)
            SELECT id, episode_id, stage, model, prompt_json, response_json, created_at
            FROM decisions
            """
        )


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return [] if default is None else default
    return json.loads(value)


def upsert_source(conn: sqlite3.Connection, source: SourceConfig) -> int:
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO sources (
          kind, name, url, external_id, feed_url, people_json, hosts_json,
          active, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, url) DO UPDATE SET
          name = excluded.name,
          external_id = excluded.external_id,
          feed_url = excluded.feed_url,
          people_json = excluded.people_json,
          hosts_json = excluded.hosts_json,
          active = excluded.active,
          updated_at = excluded.updated_at
        """,
        (
            source.kind,
            source.name,
            source.url,
            source.external_id,
            source.feed_url or None,
            dumps(list(source.people)),
            dumps(list(source.hosts)),
            int(source.active),
            timestamp,
            timestamp,
        ),
    )
    row = conn.execute("SELECT id FROM sources WHERE kind = ? AND url = ?", (source.kind, source.url)).fetchone()
    if row is None:
        raise RuntimeError(f"source upsert failed for {source.url}")
    return int(row["id"])


def upsert_feed(conn: sqlite3.Connection, feed: FeedConfig) -> int:
    return upsert_source(
        conn,
        SourceConfig(
            kind="podcast",
            name=feed.name,
            url=feed.url,
            feed_url=feed.url,
            hosts=feed.hosts,
            active=feed.active,
        ),
    )


def update_source_metadata(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    image_url: str | None = None,
    homepage_url: str | None = None,
    cursor: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE sources
        SET image_url = COALESCE(?, image_url),
            homepage_url = COALESCE(?, homepage_url),
            cursor = COALESCE(?, cursor),
            last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
            last_error = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            image_url or None,
            homepage_url or None,
            cursor,
            error,
            now_iso(),
            error,
            now_iso(),
            source_id,
        ),
    )


def update_feed_metadata(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    image_url: str | None,
    homepage_url: str | None,
) -> None:
    update_source_metadata(conn, feed_id, image_url=image_url, homepage_url=homepage_url)


def upsert_appearance(
    conn: sqlite3.Connection,
    source_id: int,
    appearance: dict[str, Any],
) -> tuple[int, bool]:
    timestamp = now_iso()
    external_id = str(appearance.get("external_id") or appearance.get("guid") or appearance.get("url") or "")
    if not external_id:
        external_id = hashlib.sha256(
            f"{appearance.get('title', '')}|{appearance.get('published_at', '')}".encode("utf-8")
        ).hexdigest()
    existing = conn.execute(
        "SELECT id, item_id FROM appearances WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    content_text = str(appearance.get("content_text") or "").strip()
    content_hash = _content_hash(content_text)
    authors = _as_list(appearance.get("authors") or appearance.get("hosts"))
    hinted_item_id = _valid_canonical_hint(conn, appearance.get("canonical_item_id"))
    if existing is not None:
        item_id = int(existing["item_id"])
        conn.execute(
            """
            UPDATE appearances
            SET title = ?, description = ?, url = ?, media_url = ?, media_type = ?,
                image_url = ?, published_at = ?, duration = ?, authors_json = ?, raw_json = ?,
                content_text = COALESCE(NULLIF(?, ''), content_text),
                content_hash = COALESCE(?, content_hash), updated_at = ?
            WHERE id = ?
            """,
            (
                appearance["title"],
                appearance.get("description"),
                appearance.get("url") or appearance.get("episode_url"),
                appearance.get("media_url") or appearance.get("audio_url"),
                appearance.get("media_type") or appearance.get("audio_type"),
                appearance.get("image_url"),
                appearance.get("published_at"),
                appearance.get("duration"),
                dumps(authors),
                dumps(appearance.get("raw", {})),
                content_text,
                content_hash,
                timestamp,
                int(existing["id"]),
            ),
        )
        if hinted_item_id is not None and hinted_item_id != item_id:
            conn.execute(
                "UPDATE appearances SET item_id = ?, updated_at = ? WHERE id = ?",
                (hinted_item_id, timestamp, int(existing["id"])),
            )
            remaining = conn.execute(
                "SELECT COUNT(*) FROM appearances WHERE item_id = ?",
                (item_id,),
            ).fetchone()[0]
            if remaining == 0:
                conn.execute(
                    """
                    UPDATE radar_items
                    SET status = 'merged', merged_into_item_id = ?,
                        skip_reason = 'official cross-medium source match', updated_at = ?
                    WHERE id = ?
                    """,
                    (hinted_item_id, timestamp, item_id),
                )
                conn.execute(
                    "UPDATE dedupe_candidates SET status = 'merged' WHERE item_id = ? OR candidate_item_id = ?",
                    (item_id, item_id),
                )
            else:
                _refresh_item_media(conn, item_id)
            item_id = hinted_item_id
        _refresh_item_from_appearance(conn, item_id, appearance, authors, content_text, content_hash)
        return item_id, False

    duplicate = None if hinted_item_id is not None else _best_duplicate(conn, source_id, appearance, content_hash)
    if hinted_item_id is not None:
        item_id = hinted_item_id
    elif duplicate and duplicate[1] >= AUTO_MERGE_SCORE:
        item_id = duplicate[0]
    else:
        cursor = conn.execute(
            """
            INSERT INTO radar_items (
              title, description, image_url, published_at, duration, authors_json,
              hosts_json, content_text, content_hash, content_prepared_at,
              created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                appearance["title"],
                appearance.get("description"),
                appearance.get("image_url"),
                appearance.get("published_at"),
                appearance.get("duration"),
                dumps(authors),
                dumps(_as_list(appearance.get("hosts"))),
                content_text or None,
                content_hash,
                timestamp if content_text else None,
                timestamp,
                timestamp,
            ),
        )
        item_id = int(cursor.lastrowid)

    conn.execute(
        """
        INSERT INTO appearances (
          item_id, source_id, external_id, title, description, url, media_url,
          media_type, image_url, published_at, duration, authors_json, raw_json,
          content_text, content_hash, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            source_id,
            external_id,
            appearance["title"],
            appearance.get("description"),
            appearance.get("url") or appearance.get("episode_url"),
            appearance.get("media_url") or appearance.get("audio_url"),
            appearance.get("media_type") or appearance.get("audio_type"),
            appearance.get("image_url"),
            appearance.get("published_at"),
            appearance.get("duration"),
            dumps(authors),
            dumps(appearance.get("raw", {})),
            content_text or None,
            content_hash,
            timestamp,
            timestamp,
        ),
    )
    _refresh_item_from_appearance(conn, item_id, appearance, authors, content_text, content_hash)
    if duplicate and duplicate[1] >= REVIEW_SCORE and duplicate[1] < AUTO_MERGE_SCORE:
        _record_dedupe_candidate(conn, item_id, duplicate[0], duplicate[1], duplicate[2])
    return item_id, True


def _valid_canonical_hint(conn: sqlite3.Connection, value: Any) -> int | None:
    if value is None:
        return None
    try:
        item_id = int(value)
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        "SELECT id FROM radar_items WHERE id = ? AND merged_into_item_id IS NULL",
        (item_id,),
    ).fetchone()
    return item_id if row is not None else None


def upsert_episode(conn: sqlite3.Connection, feed_id: int, episode: dict[str, Any]) -> tuple[int, bool]:
    return upsert_appearance(
        conn,
        feed_id,
        {
            **episode,
            "external_id": episode.get("guid"),
            "url": episode.get("episode_url"),
            "media_url": episode.get("audio_url"),
            "media_type": episode.get("audio_type"),
            "authors": episode.get("hosts", []),
        },
    )


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
        INSERT INTO item_decisions (item_id, stage, model, prompt_json, response_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (episode_id, stage, model, dumps(prompt), dumps(response), now_iso()),
    )


def set_judgement(
    conn: sqlite3.Connection,
    episode_id: int,
    result: dict[str, Any],
    *,
    include_status: str = "relevant",
) -> None:
    include = bool(result.get("include"))
    status = include_status if include else "skipped"
    conn.execute(
        """
        UPDATE radar_items
        SET status = ?, skip_reason = ?, relevance_score = ?, labs_json = ?,
            matched_people_json = ?, guests_json = ?, judged_at = ?, updated_at = ?
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


def set_content(
    conn: sqlite3.Connection,
    item_id: int,
    content: str,
    path: pathlib.Path | None = None,
) -> int:
    normalized = " ".join(content.split())
    content_hash = _content_hash(normalized)
    conn.execute(
        """
        UPDATE radar_items
        SET content_text = ?, content_path = ?, content_hash = ?, content_prepared_at = ?,
            status = 'transcribed', updated_at = ?
        WHERE id = ?
        """,
        (normalized, str(path) if path else None, content_hash, now_iso(), now_iso(), item_id),
    )
    primary = _primary_appearance_id(conn, item_id)
    if primary is not None:
        conn.execute(
            "UPDATE appearances SET content_text = ?, content_hash = ?, updated_at = ? WHERE id = ?",
            (normalized, content_hash, now_iso(), primary),
        )
    if content_hash:
        duplicate = conn.execute(
            """
            SELECT item_id FROM appearances
            WHERE content_hash = ? AND item_id != ?
            ORDER BY item_id LIMIT 1
            """,
            (content_hash, item_id),
        ).fetchone()
        if duplicate is not None:
            item_id = merge_items(conn, item_id, int(duplicate["item_id"]), reason="identical full text")
    return item_id


def set_transcript(conn: sqlite3.Connection, episode_id: int, transcript: str, path: pathlib.Path) -> None:
    set_content(conn, episode_id, transcript, path)


def set_summary(conn: sqlite3.Connection, episode_id: int, result: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE radar_items
        SET summary_title = ?, summary_text = ?, short_summary_text = ?, summary_html = ?,
            key_points_json = ?, topics_json = ?,
            hosts_json = CASE WHEN ? != '[]' THEN ? ELSE hosts_json END,
            guests_json = CASE WHEN ? != '[]' THEN ? ELSE guests_json END,
            summarized_at = ?, status = 'published', updated_at = ?
        WHERE id = ?
        """,
        (
            result.get("title"),
            result.get("summary"),
            result.get("short_summary"),
            result.get("summary_html"),
            dumps(_as_list(result.get("key_points"))),
            dumps(_as_list(result.get("topics"))),
            dumps(_as_list(result.get("hosts"))),
            dumps(_as_list(result.get("hosts"))),
            dumps(_as_list(result.get("guests"))),
            dumps(_as_list(result.get("guests"))),
            now_iso(),
            now_iso(),
            episode_id,
        ),
    )


def merge_items(conn: sqlite3.Connection, first_item_id: int, second_item_id: int, *, reason: str) -> int:
    if first_item_id == second_item_id:
        return first_item_id
    rows = conn.execute(
        "SELECT * FROM radar_items WHERE id IN (?, ?)",
        (first_item_id, second_item_id),
    ).fetchall()
    if len(rows) != 2:
        raise KeyError("cannot merge missing radar item")
    published = [row for row in rows if row["status"] == "published"]
    canonical_row = min(published, key=lambda row: int(row["id"])) if published else min(
        rows, key=lambda row: int(row["id"])
    )
    canonical = int(canonical_row["id"])
    duplicate = second_item_id if canonical == first_item_id else first_item_id
    canonical_data = next(row for row in rows if int(row["id"]) == canonical)
    duplicate_data = next(row for row in rows if int(row["id"]) == duplicate)
    _preserve_merged_item_state(conn, canonical_data, duplicate_data)
    conn.execute("UPDATE appearances SET item_id = ? WHERE item_id = ?", (canonical, duplicate))
    conn.execute("UPDATE item_decisions SET item_id = ? WHERE item_id = ?", (canonical, duplicate))
    conn.execute(
        "UPDATE radar_items SET status = 'merged', merged_into_item_id = ?, skip_reason = ?, updated_at = ? WHERE id = ?",
        (canonical, reason, now_iso(), duplicate),
    )
    conn.execute(
        "UPDATE dedupe_candidates SET status = 'merged' WHERE item_id = ? OR candidate_item_id = ?",
        (duplicate, duplicate),
    )
    _refresh_item_media(conn, canonical)
    return canonical


def _preserve_merged_item_state(
    conn: sqlite3.Connection,
    canonical: sqlite3.Row,
    duplicate: sqlite3.Row,
) -> None:
    priorities = {
        "failed": 0,
        "skipped": 0,
        "transcription_failed": 1,
        "new": 2,
        "relevant": 3,
        "summary_failed": 4,
        "transcribed": 4,
        "published": 5,
    }
    canonical_status = str(canonical["status"])
    duplicate_status = str(duplicate["status"])
    status = canonical_status
    status_source = canonical
    if priorities.get(duplicate_status, 0) > priorities.get(canonical_status, 0):
        status = duplicate_status
        status_source = duplicate

    def prefer(name: str, *, empty_json: bool = False):
        value = canonical[name]
        missing = value is None or value == "" or (empty_json and value == "[]")
        return duplicate[name] if missing else value

    conn.execute(
        """
        UPDATE radar_items
        SET status = ?, skip_reason = ?, relevance_score = ?, labs_json = ?,
            matched_people_json = ?, authors_json = ?, hosts_json = ?, guests_json = ?,
            judged_at = ?, content_path = ?, content_text = ?, content_hash = ?,
            content_prepared_at = ?, summary_title = ?, summary_text = ?,
            short_summary_text = ?, summary_html = ?, key_points_json = ?, topics_json = ?,
            summarized_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            status_source["skip_reason"],
            prefer("relevance_score"),
            prefer("labs_json", empty_json=True),
            prefer("matched_people_json", empty_json=True),
            prefer("authors_json", empty_json=True),
            prefer("hosts_json", empty_json=True),
            prefer("guests_json", empty_json=True),
            prefer("judged_at"),
            prefer("content_path"),
            prefer("content_text"),
            prefer("content_hash"),
            prefer("content_prepared_at"),
            prefer("summary_title"),
            prefer("summary_text"),
            prefer("short_summary_text"),
            prefer("summary_html"),
            prefer("key_points_json", empty_json=True),
            prefer("topics_json", empty_json=True),
            prefer("summarized_at"),
            now_iso(),
            int(canonical["id"]),
        ),
    )


def mark_failed(conn: sqlite3.Connection, episode_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE radar_items SET status = 'failed', skip_reason = ?, updated_at = ? WHERE id = ?",
        (reason, now_iso(), episode_id),
    )


def mark_processing_failed(conn: sqlite3.Connection, episode_id: int, *, stage: str, reason: str) -> None:
    if stage not in {"transcription", "summary", "content"}:
        raise ValueError(f"unsupported processing failure stage: {stage}")
    status = "transcription_failed" if stage in {"transcription", "content"} else "summary_failed"
    conn.execute(
        "UPDATE radar_items SET status = ?, skip_reason = ?, updated_at = ? WHERE id = ?",
        (status, reason, now_iso(), episode_id),
    )


def items_for_status(
    conn: sqlite3.Connection,
    statuses: Iterable[str],
    *,
    limit: int | None = None,
    published_since: str | None = None,
    source_names: Iterable[str] | None = None,
    search_text: str | None = None,
) -> list[dict[str, Any]]:
    values = tuple(statuses)
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    sql = f"SELECT DISTINCT radar_items.id FROM radar_items WHERE radar_items.status IN ({placeholders}) AND radar_items.merged_into_item_id IS NULL"
    params: list[Any] = list(values)
    if published_since:
        sql += " AND (radar_items.published_at IS NULL OR radar_items.published_at >= ?)"
        params.append(published_since)
    names = tuple(source_names or ())
    if names:
        name_placeholders = ",".join("?" for _ in names)
        sql += f" AND EXISTS (SELECT 1 FROM appearances JOIN sources ON sources.id = appearances.source_id WHERE appearances.item_id = radar_items.id AND sources.name IN ({name_placeholders}))"
        params.extend(names)
    if search_text:
        pattern = f"%{search_text}%"
        sql += " AND (radar_items.title LIKE ? OR radar_items.description LIKE ?)"
        params.extend([pattern, pattern])
    sql += " ORDER BY COALESCE(radar_items.published_at, radar_items.created_at) DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    ids = [int(row["id"]) for row in conn.execute(sql, params)]
    return [_item_row(conn, item_id) for item_id in ids]


def episodes_for_status(
    conn: sqlite3.Connection,
    statuses: Iterable[str],
    *,
    limit: int | None = None,
    published_since: str | None = None,
    feed_names: Iterable[str] | None = None,
    search_text: str | None = None,
) -> list[dict[str, Any]]:
    return items_for_status(
        conn,
        statuses,
        limit=limit,
        published_since=published_since,
        source_names=feed_names,
        search_text=search_text,
    )


def public_items(conn: sqlite3.Connection, *, limit: int | None = None) -> list[dict[str, Any]]:
    return items_for_status(conn, PUBLIC_STATUSES, limit=limit)


def public_episodes(conn: sqlite3.Connection, *, limit: int | None = None) -> list[dict[str, Any]]:
    return public_items(conn, limit=limit)


def item_by_id(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT merged_into_item_id FROM radar_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"radar item {item_id} not found")
    resolved = int(row["merged_into_item_id"] or item_id)
    return _item_row(conn, resolved)


def episode_by_id(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any]:
    return item_by_id(conn, episode_id)


def appearances_for_item(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT appearances.*, sources.kind AS medium, sources.name AS source_name,
               sources.url AS source_url, sources.feed_url, sources.homepage_url,
               sources.image_url AS source_image_url, sources.hosts_json AS source_hosts_json
        FROM appearances JOIN sources ON sources.id = appearances.source_id
        WHERE appearances.item_id = ?
        ORDER BY CASE sources.kind WHEN 'podcast' THEN 0 WHEN 'youtube' THEN 1 WHEN 'blog' THEN 2 ELSE 3 END,
                 appearances.published_at, appearances.id
        """,
        (item_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def pending_dedupe_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM dedupe_candidates WHERE status = 'pending' ORDER BY score DESC, created_at DESC"
        )
    )


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS count FROM radar_items GROUP BY status").fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def source_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT kind, COUNT(*) AS count FROM sources WHERE active = 1 GROUP BY kind").fetchall()
    return {str(row["kind"]): int(row["count"]) for row in rows}


def _item_row(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    item = conn.execute("SELECT * FROM radar_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise KeyError(f"radar item {item_id} not found")
    appearances = appearances_for_item(conn, item_id)
    if not appearances:
        raise KeyError(f"radar item {item_id} has no appearances")
    primary = appearances[0]
    value = dict(item)
    media = []
    for appearance in appearances:
        kind = str(appearance["medium"])
        if kind not in media:
            media.append(kind)
    value.update(
        {
            "feed_id": primary["source_id"],
            "guid": primary["external_id"],
            "feed_name": primary["source_name"],
            "feed_url": primary["feed_url"] or primary["source_url"],
            "feed_homepage_url": primary["homepage_url"],
            "feed_image_url": primary["source_image_url"],
            "feed_hosts_json": primary["source_hosts_json"],
            "episode_url": primary["url"],
            "audio_url": primary["media_url"],
            "audio_type": primary["media_type"],
            "transcript_path": value.get("content_path"),
            "transcript_text": value.get("content_text"),
            "transcribed_at": value.get("content_prepared_at"),
            "medium": primary["medium"],
            "media_kinds_json": dumps(media),
            "appearances_json": dumps(appearances),
        }
    )
    return value


def _refresh_item_from_appearance(
    conn: sqlite3.Connection,
    item_id: int,
    appearance: dict[str, Any],
    authors: list[str],
    content_text: str,
    content_hash: str | None,
) -> None:
    conn.execute(
        """
        UPDATE radar_items
        SET title = CASE WHEN status = 'new' THEN ? ELSE title END,
            description = COALESCE(description, ?), image_url = COALESCE(image_url, ?),
            published_at = CASE WHEN published_at IS NULL OR ? < published_at THEN ? ELSE published_at END,
            duration = COALESCE(duration, ?),
            authors_json = CASE WHEN authors_json = '[]' THEN ? ELSE authors_json END,
            status = CASE
              WHEN ? IS NOT NULL AND content_hash IS NOT NULL AND content_hash != ?
                   AND status IN ('skipped', 'failed', 'transcription_failed', 'summary_failed') THEN 'new'
              ELSE status
            END,
            content_text = COALESCE(NULLIF(?, ''), content_text),
            content_hash = COALESCE(?, content_hash),
            content_prepared_at = CASE WHEN ? IS NOT NULL THEN ? ELSE content_prepared_at END,
            updated_at = ?
        WHERE id = ?
        """,
        (
            appearance["title"],
            appearance.get("description"),
            appearance.get("image_url"),
            appearance.get("published_at"),
            appearance.get("published_at"),
            appearance.get("duration"),
            dumps(authors),
            content_hash,
            content_hash,
            content_text,
            content_hash,
            content_hash,
            now_iso(),
            now_iso(),
            item_id,
        ),
    )
    _refresh_item_media(conn, item_id)


def _refresh_item_media(conn: sqlite3.Connection, item_id: int) -> None:
    appearance = conn.execute(
        """
        SELECT appearances.image_url, appearances.published_at, appearances.duration
        FROM appearances JOIN sources ON sources.id = appearances.source_id
        WHERE appearances.item_id = ?
        ORDER BY CASE sources.kind WHEN 'podcast' THEN 0 WHEN 'youtube' THEN 1 WHEN 'blog' THEN 2 ELSE 3 END,
                 appearances.id LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    if appearance is None:
        return
    conn.execute(
        """
        UPDATE radar_items SET image_url = COALESCE(image_url, ?),
          published_at = COALESCE(published_at, ?), duration = COALESCE(duration, ?), updated_at = ?
        WHERE id = ?
        """,
        (appearance["image_url"], appearance["published_at"], appearance["duration"], now_iso(), item_id),
    )


def _best_duplicate(
    conn: sqlite3.Connection,
    source_id: int,
    appearance: dict[str, Any],
    content_hash: str | None,
) -> tuple[int, float, str] | None:
    source = conn.execute("SELECT kind FROM sources WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        raise KeyError(f"source {source_id} not found")
    incoming_kind = str(source["kind"])
    rows = conn.execute(
        """
        SELECT radar_items.id, radar_items.title, radar_items.description, radar_items.published_at,
               radar_items.duration, radar_items.content_hash, appearances.url,
               appearances.description AS appearance_description, sources.kind
        FROM radar_items
        JOIN appearances ON appearances.item_id = radar_items.id
        JOIN sources ON sources.id = appearances.source_id
        WHERE radar_items.merged_into_item_id IS NULL AND sources.kind != ?
        ORDER BY radar_items.id DESC LIMIT 400
        """,
        (incoming_kind,),
    ).fetchall()
    best: tuple[int, float, str] | None = None
    for row in rows:
        score, reason = _duplicate_score(appearance, content_hash, row)
        if score < REVIEW_SCORE:
            continue
        if best is None or score > best[1]:
            best = (int(row["id"]), score, reason)
    return best


def _duplicate_score(appearance: dict[str, Any], content_hash: str | None, row: sqlite3.Row) -> tuple[float, str]:
    if content_hash and row["content_hash"] == content_hash:
        return 1.0, "identical full-text hash"
    incoming_blob = " ".join(
        str(value or "")
        for value in (appearance.get("description"), dumps(appearance.get("raw", {})))
    )
    existing_url = str(row["url"] or "")
    incoming_url = str(appearance.get("url") or appearance.get("episode_url") or "")
    existing_blob = f"{row['description'] or ''} {row['appearance_description'] or ''}"
    if existing_url and existing_url in incoming_blob:
        return 1.0, "new appearance links to existing appearance"
    if incoming_url and incoming_url in existing_blob:
        return 1.0, "existing appearance links to new appearance"

    left = _normalized_title(str(appearance.get("title") or ""))
    right = _normalized_title(str(row["title"] or ""))
    if len(left.split()) < 4 or len(right.split()) < 4:
        return 0.0, "title too short"
    title_score = SequenceMatcher(None, left, right).ratio()
    if not _dates_close(appearance.get("published_at"), row["published_at"], days=4):
        return title_score * 0.6, "similar title outside publication window"
    incoming_description = _normalized_description(str(appearance.get("description") or ""))
    existing_description = _normalized_description(
        str(row["appearance_description"] or row["description"] or "")
    )
    if (
        len(incoming_description) >= 200
        and incoming_description == existing_description
        and title_score >= 0.75
    ):
        return 0.99, "same substantial description, similar title, and publication window"
    duration_matches = _durations_close(appearance.get("duration"), row["duration"])
    if left == right:
        if duration_matches:
            return 0.98, "same normalized title, publication window, and duration"
        return 0.90, "same normalized title and publication window; duration unavailable"
    if title_score >= 0.92 and duration_matches:
        return 0.96, "similar title, publication window, and duration"
    if title_score >= 0.88:
        return 0.86, "similar title and publication window"
    return 0.0, "no strong duplicate evidence"


def _record_dedupe_candidate(
    conn: sqlite3.Connection,
    item_id: int,
    candidate_item_id: int,
    score: float,
    reason: str,
) -> None:
    left, right = sorted((item_id, candidate_item_id))
    conn.execute(
        """
        INSERT INTO dedupe_candidates (item_id, candidate_item_id, score, reason, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_id, candidate_item_id) DO UPDATE SET score = excluded.score, reason = excluded.reason
        """,
        (left, right, score, reason, now_iso()),
    )


def _primary_appearance_id(conn: sqlite3.Connection, item_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT appearances.id FROM appearances JOIN sources ON sources.id = appearances.source_id
        WHERE appearances.item_id = ?
        ORDER BY CASE sources.kind WHEN 'podcast' THEN 0 WHEN 'youtube' THEN 1 WHEN 'blog' THEN 2 ELSE 3 END,
                 appearances.id LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    return int(row["id"]) if row else None


def _content_hash(content: str) -> str | None:
    normalized = " ".join(content.split()).casefold()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalized_title(value: str) -> str:
    value = re.sub(r"\b(full|video|podcast|episode|interview|watch|listen)\b", " ", value.casefold())
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _normalized_description(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _dates_close(first: Any, second: Any, *, days: int) -> bool:
    left = _parse_datetime(first)
    right = _parse_datetime(second)
    if left is None or right is None:
        return False
    return abs((left - right).total_seconds()) <= days * 86400


def _parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _durations_close(first: Any, second: Any) -> bool:
    left = _duration_seconds(first)
    right = _duration_seconds(second)
    if left is None or right is None:
        return False
    return abs(left - right) <= max(120, min(left, right) * 0.12)


def _duration_seconds(value: Any) -> int | None:
    if not value:
        return None
    parts = str(value).split(":")
    if not all(part.isdigit() for part in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]
