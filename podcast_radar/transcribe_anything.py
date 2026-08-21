"""Ask Transcribe Anything for transcripts instead of driving a worker directly.

The rsync broker in distributed.py has this project handing jobs straight to the
Mac. That works, but ai-radar and every other project were each doing it with
their own queue and their own idea of what had already been transcribed, so the
same episode could be transcribed more than once.

Transcribe Anything keeps one corpus keyed on the canonical media URL. Asking it
for an episode either returns a transcript that already exists -- made for this
project, for podsearch, or by somebody pasting the link into the website -- or
queues the work once. Either way the Mac does it at most once.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import distributed, storage
from .config import Config

TIMEOUT = 60


class TranscribeAnythingError(RuntimeError):
    pass


def migrate(conn: sqlite3.Connection) -> None:
    """Track what we've asked for. Separate from the broker's own table so the
    two dispatch paths can coexist and be switched between."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcribe_anything_jobs (
          item_id INTEGER PRIMARY KEY REFERENCES radar_items(id),
          slug TEXT NOT NULL,
          status TEXT NOT NULL,
          requested_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          error_message TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS transcribe_anything_jobs_status ON transcribe_anything_jobs(status)"
    )
    conn.commit()


class Client:
    def __init__(self, base_url: str, token: str, *, timeout: int = TIMEOUT):
        if not base_url:
            raise TranscribeAnythingError("no service_url configured")
        if not token:
            raise TranscribeAnythingError(
                "no token: set service_token, or TRANSCRIBE_ANYTHING_TOKEN in the environment"
            )
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request_transcript(
        self,
        url: str,
        *,
        title: str = "",
        duration_seconds: int | None = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"url": url}
        if title:
            fields["title"] = title
        if duration_seconds:
            fields["duration_seconds"] = int(duration_seconds)
        if prompt:
            fields["prompt"] = prompt
        return self._call("POST", "/api/v1/transcripts", fields)

    def fetch(self, slug: str) -> dict[str, Any]:
        return self._call("GET", f"/api/v1/transcripts/{urllib.parse.quote(slug)}")

    def _call(self, method: str, path: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        data = urllib.parse.urlencode(fields).encode() if fields else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"message": body.decode(errors="replace")[:200]}
            if exc.code == 404:
                return {}
            raise TranscribeAnythingError(
                f"{method} {path} -> {exc.code}: {payload.get('message', '')}"
            ) from exc
        except OSError as exc:
            # URLError subclasses OSError, but a read timeout can surface as a
            # bare TimeoutError, which URLError alone would let through.
            reason = getattr(exc, "reason", exc)
            raise TranscribeAnythingError(f"cannot reach {self.base_url}: {reason}") from exc


def client_for(config: Config) -> Client:
    token = config.transcription.service_token or os.environ.get("TRANSCRIBE_ANYTHING_TOKEN", "")
    return Client(config.transcription.service_url, token)


def _store(config: Config, conn: sqlite3.Connection, item_id: int, text: str) -> None:
    path = pathlib.Path(config.transcription.transcript_dir).expanduser() / f"item-{item_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    storage.set_transcript(conn, item_id, text, path)
    conn.commit()


def _remember(conn: sqlite3.Connection, item_id: int, slug: str, status: str, error: str | None = None) -> None:
    now = storage.now_iso()
    conn.execute(
        """
        INSERT INTO transcribe_anything_jobs (item_id, slug, status, requested_at, updated_at, error_message)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
          slug = excluded.slug, status = excluded.status,
          updated_at = excluded.updated_at, error_message = excluded.error_message
        """,
        (item_id, slug, status, now, now, error),
    )
    conn.commit()


def dispatch(
    config: Config,
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    published_since: str | None = None,
) -> dict[str, int]:
    """Ask for a transcript for every relevant item that hasn't got one.

    An item whose audio somebody already transcribed comes back complete on the
    spot; that is the whole point, and it costs the Mac nothing.
    """
    migrate(conn)
    service = client_for(config)

    stats = {"considered": 0, "already_had": 0, "requested": 0, "waiting": 0, "skipped": 0, "failed": 0}
    since = published_since or config.app.processed_after

    for item in storage.episodes_for_status(conn, ("relevant",), limit=limit, published_since=since):
        stats["considered"] += 1

        if item.get("content_text"):
            stats["skipped"] += 1
            continue

        media_url = str(item.get("audio_url") or "")
        if not media_url:
            stats["skipped"] += 1
            continue

        item_id = int(item["id"])
        try:
            answer = service.request_transcript(
                media_url,
                title=str(item.get("title") or ""),
                duration_seconds=_seconds(item.get("duration")),
                prompt=_prompt_for(config, item),
            )
        except TranscribeAnythingError as exc:
            _remember(conn, item_id, "", "failed", str(exc)[:300])
            stats["failed"] += 1
            continue

        slug = str(answer.get("slug") or "")
        transcript = answer.get("transcript") or {}
        text = transcript.get("text")

        if text:
            _store(config, conn, item_id, text)
            _remember(conn, item_id, slug, "done")
            stats["already_had"] += 1
        else:
            _remember(conn, item_id, slug, "waiting")
            stats["requested" if answer.get("status") == "queued" else "waiting"] += 1

    return stats


def collect(config: Config, conn: sqlite3.Connection, *, limit: int | None = None) -> dict[str, int]:
    """Pick up transcripts for anything we asked about and are still waiting on."""
    migrate(conn)
    service = client_for(config)

    stats = {"checked": 0, "imported": 0, "waiting": 0, "removed": 0, "failed": 0}
    rows = conn.execute(
        "SELECT item_id, slug FROM transcribe_anything_jobs WHERE status = 'waiting' ORDER BY updated_at"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()

    for row in rows:
        stats["checked"] += 1
        item_id, slug = int(row["item_id"]), str(row["slug"])
        if not slug:
            continue

        try:
            answer = service.fetch(slug)
        except TranscribeAnythingError as exc:
            _remember(conn, item_id, slug, "waiting", str(exc)[:300])
            stats["failed"] += 1
            continue

        status = str(answer.get("status") or "")
        text = (answer.get("transcript") or {}).get("text")

        if text:
            _store(config, conn, item_id, text)
            _remember(conn, item_id, slug, "done")
            stats["imported"] += 1
        elif status in ("removed", "failed"):
            # Taken down, or the fleet gave up. Either way stop asking.
            _remember(conn, item_id, slug, status, answer.get("error"))
            stats["removed" if status == "removed" else "failed"] += 1
        else:
            stats["waiting"] += 1

    return stats


def _prompt_for(config: Config, item: dict[str, Any]) -> str:
    """The vocabulary hint this project already sends its own worker.

    Reuses the --prompt out of the configured whisper args, with the episode's
    own metadata filled in, so moving to the shared service doesn't quietly
    make transcripts worse at spelling "Anthropic".
    """
    try:
        args = distributed._remote_args(config, item)
    except Exception:  # noqa: BLE001 - a prompt is a nicety, never a reason to fail
        return ""

    for flag, value in zip(args, args[1:]):
        if flag == "--prompt":
            return str(value)
    return ""


def _seconds(value: Any) -> int | None:
    if not value:
        return None
    raw = str(value).strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return None
