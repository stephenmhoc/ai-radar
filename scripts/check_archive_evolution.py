from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402


def load(path: pathlib.Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    radar.validate_archive(value, label=str(path))
    return value


def validate_evolution(before: dict[str, object], after: dict[str, object]) -> None:
    before_items = {item["id"]: item for item in before["items"]}  # type: ignore[index]
    after_items = {item["id"]: item for item in after["items"]}  # type: ignore[index]
    errors: list[str] = []
    missing = sorted(set(before_items) - set(after_items))
    if missing:
        errors.append(f"removed {len(missing)} canonical item(s): {', '.join(missing[:10])}")
    for item_id in sorted(set(before_items) & set(after_items)):
        old = before_items[item_id]
        new = after_items[item_id]
        if old["status"] == "published" and new["status"] != "published":
            errors.append(f"published item {item_id} changed status to {new['status']}")
        if old["long_summary"] and new["long_summary"] != old["long_summary"]:
            errors.append(f"item {item_id} changed an existing long summary")
        old_media = {radar.media_identity(value) for value in old["appearances"]}
        new_media = {radar.media_identity(value) for value in new["appearances"]}
        removed_media = old_media - new_media
        if removed_media:
            errors.append(f"item {item_id} removed canonical media: {sorted(removed_media)!r}")
    if errors:
        raise radar.RadarError("archive evolution validation failed: " + "; ".join(errors[:20]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=pathlib.Path)
    parser.add_argument("after", type=pathlib.Path)
    args = parser.parse_args()
    try:
        validate_evolution(load(args.before), load(args.after))
    except (OSError, json.JSONDecodeError, radar.RadarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("archive evolution is append-only and summary-preserving")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
