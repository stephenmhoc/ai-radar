from __future__ import annotations

import argparse
import http.server
import os
import pathlib
import shutil
import socketserver
import sys
from functools import partial

from .config import Config, load_config
from . import collectors, launchd, llm, pipeline, site, storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="podcast-radar")
    parser.add_argument("--config", default="config.toml", help="Path to config TOML.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Check config, local tools, and storage.")

    ingest_parser = subparsers.add_parser("ingest", help="Collect configured podcast, YouTube, blog, and X sources.")
    ingest_parser.add_argument("--limit-per-feed", type=int, default=None)
    ingest_parser.add_argument("--since", default=None, help="Only ingest episodes published on or after this date.")

    judge_parser = subparsers.add_parser("judge", help="Ask the LLM to classify new radar items.")
    judge_parser.add_argument("--limit", type=int, default=None)
    judge_parser.add_argument("--since", default=None, help="Only judge episodes published on or after this date.")
    judge_parser.add_argument("--feed", action="append", default=[], help="Only judge episodes from this feed name. Repeatable.")
    judge_parser.add_argument("--match", default=None, help="Only judge episodes whose title or description contains this text.")

    process_parser = subparsers.add_parser("process", help="Prepare content and summarize relevant radar items.")
    process_parser.add_argument("--limit", type=int, default=None)
    process_parser.add_argument("--since", default=None, help="Only process episodes published on or after this date.")
    process_parser.add_argument("--feed", action="append", default=[], help="Only process episodes from this feed name. Repeatable.")
    process_parser.add_argument("--match", default=None, help="Only process episodes whose title or description contains this text.")

    run_parser = subparsers.add_parser("run", help="Run ingest, judge, process, and site build.")
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument("--since", default=None, help="Only run on episodes published on or after this date.")
    run_parser.add_argument("--feed", action="append", default=[], help="Only judge/process episodes from this feed name. Repeatable.")
    run_parser.add_argument("--match", default=None, help="Only judge/process episodes whose title or description contains this text.")

    subparsers.add_parser("build-site", help="Render public static site and RSS feed.")

    list_parser = subparsers.add_parser("list", help="List radar item status counts.")
    list_parser.add_argument("--status", default=None)
    list_parser.add_argument("--limit", type=int, default=20)

    subparsers.add_parser("duplicates", help="List ambiguous cross-medium duplicate candidates.")
    merge_parser = subparsers.add_parser("merge-items", help="Merge two confirmed duplicate radar items.")
    merge_parser.add_argument("first_item_id", type=int)
    merge_parser.add_argument("second_item_id", type=int)

    serve_parser = subparsers.add_parser("serve-site", help="Serve the generated static site locally.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8088)

    launchd_parser = subparsers.add_parser("launchd-install", help="Install a macOS LaunchAgent.")
    launchd_parser.add_argument("--hour", type=int, default=8)
    launchd_parser.add_argument("--minute", type=int, default=30)
    launchd_parser.add_argument("--interval-minutes", type=int, default=None)
    launchd_parser.add_argument("--lookback-hours", type=int, default=2)
    launchd_parser.add_argument("--deploy-project", default="ai-radar")
    launchd_parser.add_argument("--deploy-branch", default="main")

    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.command == "doctor":
        return doctor(config)
    if args.command == "launchd-install":
        if args.interval_minutes is not None and args.interval_minutes <= 0:
            print("error: --interval-minutes must be greater than 0", file=sys.stderr)
            return 2
        if args.lookback_hours <= 0:
            print("error: --lookback-hours must be greater than 0", file=sys.stderr)
            return 2
        path = launchd.install(
            config,
            hour=args.hour,
            minute=args.minute,
            lookback_hours=args.lookback_hours,
            deploy_project=args.deploy_project,
            deploy_branch=args.deploy_branch,
            interval_minutes=args.interval_minutes,
        )
        print(f"wrote={path}")
        print(f"load with: launchctl bootstrap gui/$(id -u) {path}")
        return 0
    if args.command in {"judge", "process", "run"}:
        llm_error = llm_preflight_error(config)
        if llm_error:
            print(f"error: {llm_error}", file=sys.stderr)
            return 2
    with storage.connect(config) as conn:
        if args.command == "ingest":
            stats = collectors.collect(config, conn, limit_per_source=args.limit_per_feed, published_since=args.since)
            _print_stats(stats)
            return 0
        if args.command == "judge":
            try:
                print(
                    f"judged={pipeline.judge_pending(config, conn, limit=args.limit, published_since=args.since, feed_names=tuple(args.feed), search_text=args.match)}"
                )
            except llm.LLMError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0
        if args.command == "process":
            try:
                print(
                    f"processed={pipeline.process_relevant(config, conn, limit=args.limit, published_since=args.since, feed_names=tuple(args.feed), search_text=args.match)}"
                )
            except llm.LLMError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0
        if args.command == "run":
            try:
                _print_stats(
                    pipeline.run(
                        config,
                        conn,
                        limit=args.limit,
                        published_since=args.since,
                        feed_names=tuple(args.feed),
                        search_text=args.match,
                    )
                )
            except llm.LLMError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0
        if args.command == "build-site":
            _print_stats(site.build_site(config, conn))
            return 0
        if args.command == "list":
            return list_status(conn, args.status, args.limit)
        if args.command == "duplicates":
            candidates = storage.pending_dedupe_candidates(conn)
            for candidate in candidates:
                first = storage.item_by_id(conn, int(candidate["item_id"]))
                second = storage.item_by_id(conn, int(candidate["candidate_item_id"]))
                print(
                    f"{candidate['id']}\t{candidate['score']:.2f}\t{first['id']}:{first['title']}\t{second['id']}:{second['title']}\t{candidate['reason']}"
                )
            print(f"pending_duplicates={len(candidates)}")
            return 0
        if args.command == "merge-items":
            canonical = storage.merge_items(
                conn,
                args.first_item_id,
                args.second_item_id,
                reason="confirmed from CLI",
            )
            conn.commit()
            print(f"canonical_item_id={canonical}")
            return 0
    if args.command == "serve-site":
        return serve_site(config, args.host, args.port)
    parser.error(f"unknown command: {args.command}")
    return 2


def doctor(config: Config) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    if not config.active_sources:
        errors.append("no sources configured")
    if not config.labs:
        errors.append("no labs configured")
    if config.llm.provider == "openai_compatible" and config.llm.api_key_env:
        if not os.environ.get(config.llm.api_key_env):
            warnings.append(f"{config.llm.api_key_env} is not set; judging/summarization will fail")
    if config.llm.provider == "ollama" and not config.llm.base_url:
        errors.append("llm.base_url is required for ollama")
    warnings.extend(transcription_preflight_warnings(config))
    config.app.state_dir.mkdir(parents=True, exist_ok=True)
    youtube_active = any(source.kind == "youtube" for source in config.sources if source.active)
    if youtube_active and shutil.which("yt-dlp") is None:
        warnings.append("yt-dlp is not installed; YouTube candidates cannot be transcribed")
    for source in config.sources:
        if not source.active or source.kind not in {"youtube", "x"}:
            continue
        env_name = source.api_key_env or ("YOUTUBE_API_KEY" if source.kind == "youtube" else "X_BEARER_TOKEN")
        if not os.environ.get(env_name):
            if source.kind == "youtube" and source.feed_url:
                continue
            warnings.append(f"{env_name} is not set; {source.name} collection will be skipped")
    with storage.connect(config) as conn:
        counts = storage.status_counts(conn)
        source_counts = storage.source_counts(conn)
    print(f"config_root={config.root}")
    print(f"database={config.app.database_path}")
    print(f"public_dir={config.app.public_dir}")
    print(f"active_feeds={len(config.active_feeds)}")
    print(f"active_sources={len(config.active_sources)}")
    print(f"source_kind_counts={source_counts}")
    print(f"watched_labs={len(config.labs)}")
    print(f"seed_people={len(config.watched_people)}")
    print(f"episode_status_counts={counts}")
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


def llm_preflight_error(config: Config) -> str | None:
    if config.llm.provider == "openai_compatible" and config.llm.api_key_env:
        if not os.environ.get(config.llm.api_key_env):
            return f"{config.llm.api_key_env} is not set"
    return None


def transcription_preflight_warnings(config: Config) -> list[str]:
    if config.transcription.provider != "command":
        return []

    warnings: list[str] = []
    if shutil.which(config.transcription.command) is None:
        warnings.append(f"transcription command not found: {config.transcription.command}")

    model_path = _configured_model_path(config.transcription.args)
    if model_path is None:
        return warnings
    if "{" in model_path or "}" in model_path:
        return warnings

    path = pathlib.Path(model_path).expanduser()
    if not path.is_absolute():
        path = config.root / path
    if not path.exists():
        warnings.append(f"transcription model not found: {path}")
    return warnings


def _configured_model_path(args: tuple[str, ...]) -> str | None:
    for index, arg in enumerate(args):
        if arg in {"-m", "--model"} and index + 1 < len(args):
            return args[index + 1]
        for prefix in ("-m=", "--model="):
            if arg.startswith(prefix):
                return arg[len(prefix) :]
    return None


def list_status(conn, status: str | None, limit: int) -> int:
    if status:
        episodes = storage.episodes_for_status(conn, (status,), limit=limit)
        for episode in episodes:
            print(f"{episode['id']}\t{episode['status']}\t{episode['feed_name']}\t{episode['title']}")
    else:
        _print_stats(storage.status_counts(conn))
    return 0


def serve_site(config: Config, host: str, port: int) -> int:
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(config.app.public_dir))
    with socketserver.TCPServer((host, port), handler) as server:
        print(f"serving http://{host}:{port}/ from {config.app.public_dir}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 130
    return 0


def _print_stats(stats: dict[str, int]) -> None:
    for key, value in stats.items():
        print(f"{key}={value}")
