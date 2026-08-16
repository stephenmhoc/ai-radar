"""Coordinator side of the transcription broker.

ai-radar runs on the homelab, which has no GPU. Instead of transcribing inline it
enqueues a job for a Mac worker with Metal and imports the transcript when it
comes back. See the transcribe-broker repo for the payload contract.

Stdlib only, matching the rest of this package.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
from typing import Any

from . import storage
from .config import Config
from .transcriber import _episode_prompt_context

SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 25 * 1024 * 1024
SOURCE = "ai-radar"
DEFAULT_LEASE_HOURS = 6

# Only the worker knows these; everything else is resolved before dispatch.
WORKER_PLACEHOLDERS = ("audio_path", "output_stem", "output_dir", "model")


class DispatchError(RuntimeError):
    pass


class _LeaveUnknown(dict):
    """format_map helper that leaves unrecognised placeholders untouched.

    Prompts contain braces we neither own nor want to crash on.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def now_iso() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat()


def migrate(conn: sqlite3.Connection) -> None:
    """Additive migration; radar_items is untouched."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transcription_jobs (
          job_id TEXT PRIMARY KEY,
          item_id INTEGER NOT NULL REFERENCES radar_items(id),
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          worker_id TEXT,
          error_message TEXT,
          dispatched_at TEXT NOT NULL,
          leased_until TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transcription_jobs_status
          ON transcription_jobs(status);
        CREATE INDEX IF NOT EXISTS idx_transcription_jobs_item
          ON transcription_jobs(item_id);
        """
    )
    conn.commit()


# -- identity ------------------------------------------------------------------


def natural_key_for(item: dict[str, Any]) -> dict[str, str]:
    """Cross-machine identity for a radar item.

    radar_items.id is a local autoincrement and means nothing on the worker, so
    identity rides on the appearance's UNIQUE(source_id, external_id) instead.
    """
    source_name = str(item.get("feed_name") or "")
    external_id = str(item.get("guid") or "")
    if not source_name or not external_id:
        raise DispatchError(f"item {item.get('id')} has no (source, external_id) natural key")
    return {"source_name": source_name, "external_id": external_id}


def job_id_for(natural_key: dict[str, str]) -> str:
    canonical = json.dumps(natural_key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{SOURCE}-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _media_kind(item: dict[str, Any]) -> str:
    medium = str(item.get("medium") or "")
    audio_type = str(item.get("audio_type") or "")
    return "youtube" if medium == "youtube" or audio_type == "video/youtube" else "audio"


def _remote_args(config: Config, item: dict[str, Any]) -> list[str]:
    """Resolve prompt placeholders, but leave the worker's own paths alone.

    The configured model path points at this machine's cache, which does not exist
    on the worker, so it is replaced with {model} for the agent to fill in.
    """
    context = dict(_episode_prompt_context(item))
    context["episode_id"] = str(item.get("id") or "")
    for placeholder in WORKER_PLACEHOLDERS:
        context[placeholder] = "{" + placeholder + "}"

    args: list[str] = []
    expect_model = False
    for arg in config.transcription.args:
        if expect_model:
            args.append("{model}")
            expect_model = False
            continue
        if arg in ("-m", "--model"):
            args.append(arg)
            expect_model = True
            continue
        args.append(str(arg).format_map(_LeaveUnknown(context)))
    if expect_model:  # trailing -m with no value
        args.append("{model}")
    return args


def build_job(config: Config, item: dict[str, Any]) -> dict[str, Any]:
    natural_key = natural_key_for(item)
    media_url = str(item.get("audio_url") or "")
    if not media_url:
        raise DispatchError(f"item {item.get('id')} has no media URL")
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id_for(natural_key),
        "source": SOURCE,
        "natural_key": natural_key,
        "label": f"{item.get('feed_name') or '?'} — {item.get('title') or '?'}",
        "media": {
            "url": media_url,
            "kind": _media_kind(item),
            "max_bytes": int(config.transcription.max_audio_mb) * 1024 * 1024,
        },
        "transcription": {
            "command": config.transcription.command,
            "args": _remote_args(config, item),
            "output_path": config.transcription.output_path,
        },
        "created_at": now_iso(),
    }


# -- enqueue -------------------------------------------------------------------


def enqueue_pending(
    config: Config,
    conn: sqlite3.Connection,
    queue_root: pathlib.Path,
    *,
    limit: int | None = None,
    published_since: str | None = None,
    lease_hours: int = DEFAULT_LEASE_HOURS,
) -> dict[str, int]:
    migrate(conn)
    pending_dir = (queue_root / "pending").expanduser()
    pending_dir.mkdir(parents=True, exist_ok=True)
    expire_stale_leases(conn, lease_hours=lease_hours)

    stats = {"considered": 0, "enqueued": 0, "already_queued": 0, "skipped": 0}
    since = published_since or config.app.processed_after
    for item in storage.episodes_for_status(conn, ("relevant",), limit=limit, published_since=since):
        stats["considered"] += 1
        # Items that already carry text (blogs, X threads) never need a worker.
        if item.get("content_text"):
            stats["skipped"] += 1
            continue
        try:
            job = build_job(config, item)
        except DispatchError:
            stats["skipped"] += 1
            continue

        existing = conn.execute(
            "SELECT status FROM transcription_jobs WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()
        if existing is not None and existing["status"] == "pending":
            stats["already_queued"] += 1
            continue

        _write_json(pending_dir / f"{job['job_id']}.json", job)
        leased_until = (
            _datetime.datetime.now(_datetime.timezone.utc) + _datetime.timedelta(hours=lease_hours)
        ).replace(microsecond=0).isoformat()
        conn.execute(
            """
            INSERT INTO transcription_jobs
              (job_id, item_id, status, attempts, dispatched_at, leased_until, updated_at)
            VALUES (?, ?, 'pending', COALESCE(
                (SELECT attempts FROM transcription_jobs WHERE job_id = ?), 0) + 1, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              status = 'pending',
              attempts = transcription_jobs.attempts + 1,
              error_message = NULL,
              dispatched_at = excluded.dispatched_at,
              leased_until = excluded.leased_until,
              updated_at = excluded.updated_at
            """,
            (job["job_id"], int(item["id"]), job["job_id"], now_iso(), leased_until, now_iso()),
        )
        conn.commit()
        stats["enqueued"] += 1
    return stats


def expire_stale_leases(conn: sqlite3.Connection, *, lease_hours: int) -> int:
    """Release jobs whose lease elapsed so they can be re-dispatched."""
    migrate(conn)
    cursor = conn.execute(
        """
        UPDATE transcription_jobs
        SET status = 'expired', updated_at = ?
        WHERE status = 'pending' AND leased_until IS NOT NULL AND leased_until < ?
        """,
        (now_iso(), now_iso()),
    )
    conn.commit()
    return cursor.rowcount or 0


def kick(worker_ssh: str, command: str, *, timeout: int = 30) -> bool:
    """Tell the worker there is work. Best effort by design.

    A sleeping or unreachable Mac is not an error: the worker's own timer will
    collect the job on its next run, so this never blocks the pipeline.
    """
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", worker_ssh, command],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - never let a kick failure stop a run
        print(f"worker kick failed (job stays queued): {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-300:]
        print(f"worker kick failed (job stays queued): {detail}", file=sys.stderr)
        return False
    return True


# -- import --------------------------------------------------------------------


def import_results(
    config: Config,
    conn: sqlite3.Connection,
    queue_root: pathlib.Path,
) -> dict[str, int]:
    migrate(conn)
    queue_root = queue_root.expanduser()
    results_dir = queue_root / "results"
    pending_dir = queue_root / "pending"
    results_dir.mkdir(parents=True, exist_ok=True)

    stats = {"found": 0, "imported": 0, "skipped": 0, "failed": 0, "errors": 0, "other_source": 0}
    for path in sorted(results_dir.glob("*.json")):
        stats["found"] += 1
        try:
            payload = _read_json(path)
            if str(payload.get("source") or "") != SOURCE:
                # podsearch shares this queue. Leave its bundles for its own
                # importer rather than failing on a natural key we cannot match.
                stats["other_source"] += 1
                continue
            outcome = _import_one(config, conn, payload)
        except Exception as exc:  # noqa: BLE001 - keep bad bundles for inspection
            if conn.in_transaction:
                conn.rollback()
            print(f"failed to import {path.name}: {exc}", file=sys.stderr)
            stats["errors"] += 1
            continue
        # Clearing pending/ is what makes the job stop being redelivered.
        (pending_dir / f"{payload.get('job_id')}.json").unlink(missing_ok=True)
        path.unlink()
        stats[outcome] += 1
    return stats


def _import_one(config: Config, conn: sqlite3.Connection, payload: dict[str, Any]) -> str:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported result schema: {payload.get('schema_version')!r}")
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        raise ValueError("result is missing job_id")

    if "error" in payload:
        return _record_failure(conn, job_id, payload)

    transcript = payload.get("transcript")
    if not isinstance(transcript, dict):
        raise ValueError("result has neither transcript nor error")
    text = transcript.get("text")
    digest = transcript.get("sha256")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("result has no transcript text")
    if not isinstance(digest, str) or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        # A mismatch means truncation in transit; leave the job pending to retry.
        raise ValueError("result checksum does not match its transcript")

    natural_key = payload.get("natural_key")
    if not isinstance(natural_key, dict):
        raise ValueError("result is missing natural_key")
    item_id = _item_for_natural_key(conn, natural_key)
    if item_id is None:
        raise ValueError(f"no radar item matches {natural_key}")

    conn.execute("BEGIN IMMEDIATE")
    current = conn.execute(
        "SELECT status, content_text FROM radar_items WHERE id = ?", (item_id,)
    ).fetchone()
    if current is None:
        conn.rollback()
        raise ValueError(f"radar item {item_id} disappeared")
    # Idempotent: a result can legitimately arrive twice if a push was retried.
    if current["content_text"]:
        _close_job(conn, job_id, "done", worker_id=_worker_id(payload))
        conn.commit()
        return "skipped"

    transcript_dir = config.transcription.transcript_dir
    transcript_dir.mkdir(parents=True, exist_ok=True)
    output = transcript_dir / f"{item_id}-{job_id}.txt"
    temp = output.with_name(f".{output.name}.import")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, output)

    storage.set_content(conn, item_id, text, output)
    completed_at = transcript.get("completed_at")
    if isinstance(completed_at, str) and completed_at:
        conn.execute(
            "UPDATE radar_items SET content_prepared_at = ? WHERE id = ?",
            (completed_at, item_id),
        )
    _close_job(conn, job_id, "done", worker_id=_worker_id(payload))
    conn.commit()
    return "imported"


def _record_failure(conn: sqlite3.Connection, job_id: str, payload: dict[str, Any]) -> str:
    error = payload.get("error") or {}
    message = str(error.get("message") or "unknown worker failure")
    stage = str(error.get("stage") or "transcribe")
    row = conn.execute(
        "SELECT item_id FROM transcription_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    _close_job(conn, job_id, "failed", worker_id=_worker_id(payload), error=f"[{stage}] {message}")
    if row is not None:
        storage.mark_processing_failed(
            conn,
            int(row["item_id"]),
            stage="transcription",
            reason=f"remote transcription failed [{stage}]: {message}",
        )
    conn.commit()
    return "failed"


def _close_job(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    *,
    worker_id: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE transcription_jobs
        SET status = ?, worker_id = COALESCE(?, worker_id), error_message = ?, updated_at = ?
        WHERE job_id = ?
        """,
        (status, worker_id, error, now_iso(), job_id),
    )


def _item_for_natural_key(conn: sqlite3.Connection, natural_key: dict[str, Any]) -> int | None:
    source_name = natural_key.get("source_name")
    external_id = natural_key.get("external_id")
    if not isinstance(source_name, str) or not isinstance(external_id, str):
        return None
    row = conn.execute(
        """
        SELECT appearances.item_id AS item_id
        FROM appearances
        JOIN sources ON sources.id = appearances.source_id
        WHERE sources.name = ? AND appearances.external_id = ?
        """,
        (source_name, external_id),
    ).fetchone()
    if row is None:
        return None
    item_id = int(row["item_id"])
    # Follow a merge so a transcript lands on the surviving item.
    merged = conn.execute(
        "SELECT merged_into_item_id FROM radar_items WHERE id = ?", (item_id,)
    ).fetchone()
    if merged is not None and merged["merged_into_item_id"]:
        return int(merged["merged_into_item_id"])
    return item_id


def _worker_id(payload: dict[str, Any]) -> str | None:
    worker = payload.get("worker")
    if isinstance(worker, dict) and isinstance(worker.get("id"), str):
        return worker["id"]
    return None


def queue_status(conn: sqlite3.Connection, queue_root: pathlib.Path) -> dict[str, int]:
    migrate(conn)
    queue_root = queue_root.expanduser()
    counts = {
        "pending_files": len(list((queue_root / "pending").glob("*.json"))) if (queue_root / "pending").exists() else 0,
        "result_files": len(list((queue_root / "results").glob("*.json"))) if (queue_root / "results").exists() else 0,
    }
    for row in conn.execute("SELECT status, COUNT(*) AS total FROM transcription_jobs GROUP BY status"):
        counts[f"jobs_{row['status']}"] = int(row["total"])
    return counts


# -- io ------------------------------------------------------------------------


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError(f"bundle is too large: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("bundle is not an object")
    return payload


def _write_json(path: pathlib.Path, payload: Any) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)
    return path
