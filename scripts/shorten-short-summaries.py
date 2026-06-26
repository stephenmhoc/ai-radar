#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from podcast_radar import site, storage  # noqa: E402
from podcast_radar.config import load_config  # noqa: E402
from podcast_radar.llm import LLMClient, LLMError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)

    with storage.connect(config) as conn:
        candidates = longest_candidates(conn, limit=args.limit)
        if args.dry_run:
            print_candidates(candidates)
            return 0
        if not candidates:
            print("updated=0")
            return 0

        prompt = build_prompt(candidates)
        response = LLMClient(config).chat_json(system=SYSTEM_PROMPT, user=prompt)
        updates = normalize_response(response, candidates)
        for episode_id, short_summary in updates.items():
            conn.execute(
                """
                UPDATE episodes
                SET short_summary_text = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (short_summary, storage.now_iso(), episode_id),
            )
        conn.commit()

    for item in candidates:
        short_summary = updates.get(item["id"])
        if short_summary:
            print(f"{item['id']}\t{len(short_summary)}\t{short_summary}")
    print(f"updated={len(updates)}")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rewrite the longest compact episode summaries.")
    parser.add_argument("--config", default="config.toml", help="Path to config TOML.")
    parser.add_argument("--limit", type=int, default=20, help="Number of longest compact summaries to rewrite.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected episodes without calling the LLM.")
    return parser.parse_args(argv)


def longest_candidates(conn, *, limit: int) -> list[dict[str, Any]]:
    rows = storage.public_episodes(conn, limit=None)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        guests = site._people(row["guests_json"]) or site._people(row["matched_people_json"])
        compact = site._topline(row, guests=guests)
        candidates.append(
            {
                "id": int(row["id"]),
                "title": str(row["summary_title"] or row["title"]),
                "guests": guests,
                "current": compact,
                "summary": str(row["summary_text"] or ""),
                "key_points": site._people(row["key_points_json"])[:4],
            }
        )
    return sorted(candidates, key=lambda item: len(item["current"]), reverse=True)[:limit]


def print_candidates(candidates: list[dict[str, Any]]) -> None:
    for item in candidates:
        print(f"{len(item['current'])}\t{item['id']}\t{item['current']}")


def build_prompt(candidates: list[dict[str, Any]]) -> str:
    episodes = [
        {
            "id": item["id"],
            "title": item["title"],
            "guests": item["guests"],
            "current_compact_summary": item["current"],
            "key_points": item["key_points"],
            "summary_excerpt": item["summary"][:700],
        }
        for item in candidates
    ]
    return f"""
Rewrite each compact podcast summary.

Rules:
- Return one short_summary per id.
- Each short_summary must be one complete sentence or noun phrase.
- Aim for 45-75 characters. Never exceed 80 characters.
- Do not cut off mid-sentence.
- Preserve the main technical point or guest/lab signal.
- Do not include guest names unless needed for clarity.

Episodes:
{json.dumps(episodes, ensure_ascii=False, indent=2)}

Return strict JSON with:
items: array of objects with id and short_summary
""".strip()


def normalize_response(response: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[int, str]:
    candidate_ids = {item["id"] for item in candidates}
    raw_items = response.get("items")
    if not isinstance(raw_items, list):
        raise LLMError("short summary response missing items array")

    updates: dict[int, str] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            episode_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if episode_id not in candidate_ids:
            continue
        short_summary = " ".join(str(item.get("short_summary", "")).split())
        if not short_summary:
            continue
        if len(short_summary) > 90:
            raise LLMError(f"short summary for episode {episode_id} is too long: {len(short_summary)} chars")
        updates[episode_id] = short_summary

    missing = candidate_ids - set(updates)
    if missing:
        raise LLMError(f"short summary response missing episode ids: {sorted(missing)}")
    return updates


SYSTEM_PROMPT = "You write concise technical podcast blurbs. Return only valid JSON."


if __name__ == "__main__":
    raise SystemExit(main())
