"""Replay labeled archive items through the live editorial path and score each model.

Every case names an item in data/items.json; its stored appearances go through
`editorial.summarize_group` exactly as a scheduled cycle would send them, with
only the model overridden. This makes real OpenRouter requests (at least one per
case per model) and never writes the archive or generated files.

    python3 scripts/eval_editorial.py --model openrouter/auto \
        --model anthropic/claude-haiku-5.5 --omit-temperature anthropic/claude-haiku-5.5

Cases that never reach a model (for example YouTube Shorts) still guard the local
rules, but they are left out of each model's raw score.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import editorial  # noqa: E402
import radar  # noqa: E402
from radar_common import RadarError, Settings  # noqa: E402

CASES_PATH = ROOT / "evals/editorial_cases.json"
CATEGORIES = ("guest", "clip", "third_party", "roundup", "unfollowed")


def canonical_people(settings: Settings, names: list[str]) -> list[str]:
    """Map list names and aliases to canonical followed names; drop anyone not followed."""
    keys = {name.casefold() for name in names}
    return sorted(group[0] for group in settings.people if keys & {name.casefold() for name in group})


def load_cases(settings: Settings, archive: dict[str, Any], path: pathlib.Path = CASES_PATH) -> list[dict[str, Any]]:
    """Load the labeled cases, rejecting any label that could not be scored honestly."""
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    items = {str(item["id"]): item for item in archive["items"]}
    followed = {group[0] for group in settings.people}
    seen: set[str] = set()
    for case in cases:
        case_id = case["id"]
        if case_id in seen:
            raise RadarError(f"eval case {case_id} is duplicated")
        seen.add(case_id)
        synthetic = "appearances" in case
        if synthetic != case_id.startswith("synthetic-"):
            raise RadarError(f"eval case {case_id} must be named synthetic- exactly when it has inline appearances")
        if not synthetic and case_id not in items:
            raise RadarError(f"eval case {case_id} is not in the archive")
        if case["expect"] not in {"include", "exclude"}:
            raise RadarError(f"eval case {case_id} has invalid expect: {case['expect']}")
        if case["category"] not in CATEGORIES:
            raise RadarError(f"eval case {case_id} has invalid category: {case['category']}")
        unknown = set(case["followed_people"]) - followed
        if unknown:
            raise RadarError(f"eval case {case_id} names unfollowed people: {sorted(unknown)}")
        if (case["expect"] == "include") != bool(case["followed_people"]):
            raise RadarError(f"eval case {case_id} must list followed_people exactly when included")
        if (case["expect"] == "include") != (case["category"] == "guest"):
            raise RadarError(f"eval case {case_id} category does not match its expectation")
        if not synthetic:
            case["appearances"] = items[case_id]["appearances"]
    return cases


def run_case(settings: Settings, case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the editorial path and record final and raw model judgments."""
    responses: list[dict[str, Any]] = []
    started = time.monotonic()
    outcome: dict[str, Any] = {"id": case["id"], "error": None, "status": None, "reason": ""}
    try:
        result = editorial.summarize_group(settings, case["appearances"], observer=responses.append)
        outcome.update(status=result["status"], reason=result["reason"])
    except RadarError as exc:
        outcome["error"] = str(exc)
    outcome["seconds"] = round(time.monotonic() - started, 1)
    outcome["attempts"] = len(responses)
    outcome["routed_models"] = sorted({str(value["model"]) for value in responses if value["model"]})
    outcome["prompt_tokens"] = sum(int(value["usage"].get("prompt_tokens") or 0) for value in responses)
    outcome["completion_tokens"] = sum(int(value["usage"].get("completion_tokens") or 0) for value in responses)
    outcome["cost"] = sum(float(value["usage"].get("cost") or 0) for value in responses)
    last = responses[-1]["response"] if responses else {}
    outcome["raw_include"] = last.get("include") if isinstance(last.get("include"), bool) else None
    claimed = last.get("followed_people")
    outcome["raw_people"] = [str(name) for name in claimed] if isinstance(claimed, list) else []
    return outcome


def grade(settings: Settings, case: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Score one outcome against its label."""
    expected = case["expect"] == "include"
    published = outcome["status"] == "published"
    people = canonical_people(settings, outcome["raw_people"])
    return {
        **outcome,
        "expect": case["expect"],
        "category": case["category"],
        "correct": outcome["error"] is None and published == expected,
        # Decided without a model request, so it says nothing about the model.
        "local": outcome["error"] is None and outcome["attempts"] == 0,
        # The model's own call, before the local followed-people check.
        "raw_correct": outcome["raw_include"] is not None and outcome["raw_include"] == expected,
        "people_match": (not expected) or people == sorted(case["followed_people"]),
    }


def summarize(graded: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for value in graded:
        by_category[value["category"]][0] += value["correct"]
        by_category[value["category"]][1] += 1
    included = [value for value in graded if value["expect"] == "include"]
    judged = [value for value in graded if not value["local"]]
    return {
        "cases": len(graded),
        "correct": sum(value["correct"] for value in graded),
        "decided_locally": len(graded) - len(judged),
        "raw_correct": f"{sum(value['raw_correct'] for value in judged)}/{len(judged)}",
        "false_includes": sum(value["status"] == "published" and value["expect"] == "exclude" for value in graded),
        "missed_includes": sum(value["status"] != "published" and value["expect"] == "include" for value in graded),
        "people_match": f"{sum(value['people_match'] for value in included)}/{len(included)}",
        "failures": sum(value["error"] is not None for value in graded),
        "retried": sum(value["attempts"] > 1 for value in graded),
        "by_category": {key: f"{hit}/{total}" for key, (hit, total) in sorted(by_category.items())},
        "routed_models": dict(collections.Counter(model for value in graded for model in value["routed_models"])),
        "prompt_tokens": sum(value["prompt_tokens"] for value in graded),
        "completion_tokens": sum(value["completion_tokens"] for value in graded),
        "cost": round(sum(value["cost"] for value in graded), 4),
        "seconds_p50": sorted(value["seconds"] for value in graded)[len(graded) // 2] if graded else 0,
    }


def evaluate(
    settings: Settings,
    cases: list[dict[str, Any]],
    model: str,
    workers: int,
    *,
    omit_temperature: bool = False,
) -> dict[str, Any]:
    temperature = None if omit_temperature else settings.llm.temperature
    model_settings = replace(settings, llm=replace(settings.llm, model=model, temperature=temperature))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        outcomes = list(pool.map(lambda case: run_case(model_settings, case), cases))
    graded = [grade(settings, case, outcome) for case, outcome in zip(cases, outcomes)]
    return {"model": model, "summary": summarize(graded), "cases": graded}


def render(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for result in results:
        summary = result["summary"]
        lines.append(f"== {result['model']}")
        for key, value in summary.items():
            lines.append(f"  {key}: {value}")
        for value in result["cases"]:
            if value["correct"] and not value["local"] and not value["raw_correct"]:
                lines.append(
                    f"  RAW  {value['id']} [{value['category']}, expect {value['expect']}]: "
                    f"raw_include={value['raw_include']} people={value['raw_people']}; "
                    f"local check: {value['reason']}"
                )
            if not value["correct"] or not value["people_match"]:
                problem = value["error"] or (
                    f"status={value['status']} raw_include={value['raw_include']} "
                    f"people={value['raw_people']} reason={value['reason']}"
                )
                lines.append(f"  MISS {value['id']} [{value['category']}, expect {value['expect']}]: {problem}")
    if len(results) > 1:
        lines.append("== disagreements")
        for index, case in enumerate(results[0]["cases"]):
            verdicts = [result["cases"][index] for result in results]
            if len({value["correct"] for value in verdicts}) > 1:
                summary = ", ".join(
                    f"{result['model']}={'ok' if value['correct'] else 'MISS'}"
                    for result, value in zip(results, verdicts)
                )
                lines.append(f"  {case['id']} [{case['category']}]: {summary}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", action="append", help="Model to evaluate; repeat to compare. Defaults to config.")
    parser.add_argument(
        "--omit-temperature", action="append", default=[], metavar="MODEL",
        help="Send no temperature for this model (some providers reject it); repeatable.",
    )
    parser.add_argument("--case", action="append", help="Only run these case IDs.")
    parser.add_argument("--repeat", type=int, default=1, help="Run each model this many times to expose flaky cases.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--json-out", type=pathlib.Path, help="Write full per-case results here.")
    args = parser.parse_args(argv)
    settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json")
    if settings.llm.api_key_env and not os.environ.get(settings.llm.api_key_env):
        print(f"error: {settings.llm.api_key_env} is required; evals make real model requests", file=sys.stderr)
        return 2
    cases = load_cases(settings, radar.load_archive(settings.archive_path))
    if args.case:
        cases = [case for case in cases if case["id"] in set(args.case)]
    results = []
    for model in args.model or [settings.llm.model]:
        for run in range(1, max(1, args.repeat) + 1):
            result = evaluate(settings, cases, model, args.workers, omit_temperature=model in args.omit_temperature)
            if args.repeat > 1:
                result["model"] = f"{model} #{run}"
            results.append(result)
    print(render(results))
    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if all(result["summary"]["correct"] == result["summary"]["cases"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
