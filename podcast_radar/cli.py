from __future__ import annotations

import argparse
import http.server
import os
import shutil
import socketserver
import sys
from functools import partial

from .config import Config, load_config
from . import feeds, launchd, llm, pipeline, site, storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="podcast-radar")
    parser.add_argument("--config", default="config.toml", help="Path to config TOML.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Check config, local tools, and storage.")

    ingest_parser = subparsers.add_parser("ingest", help="Fetch podcast feeds into SQLite.")
    ingest_parser.add_argument("--limit-per-feed", type=int, default=None)
    ingest_parser.add_argument("--since", default=None, help="Only ingest episodes published on or after this date.")

    judge_parser = subparsers.add_parser("judge", help="Ask the LLM to classify new episodes.")
    judge_parser.add_argument("--limit", type=int, default=None)
    judge_parser.add_argument("--since", default=None, help="Only judge episodes published on or after this date.")

    process_parser = subparsers.add_parser("process", help="Transcribe and summarize relevant episodes.")
    process_parser.add_argument("--limit", type=int, default=None)
    process_parser.add_argument("--since", default=None, help="Only process episodes published on or after this date.")

    run_parser = subparsers.add_parser("run", help="Run ingest, judge, process, and site build.")
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument("--since", default=None, help="Only run on episodes published on or after this date.")

    subparsers.add_parser("build-site", help="Render public static site and RSS feed.")

    list_parser = subparsers.add_parser("list", help="List episode status counts.")
    list_parser.add_argument("--status", default=None)
    list_parser.add_argument("--limit", type=int, default=20)

    serve_parser = subparsers.add_parser("serve-site", help="Serve the generated static site locally.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8088)

    launchd_parser = subparsers.add_parser("launchd-install", help="Install a daily macOS LaunchAgent.")
    launchd_parser.add_argument("--hour", type=int, default=8)
    launchd_parser.add_argument("--minute", type=int, default=30)

    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.command == "doctor":
        return doctor(config)
    if args.command == "launchd-install":
        path = launchd.install(config, hour=args.hour, minute=args.minute)
        print(f"wrote={path}")
        print(f"load with: launchctl load {path}")
        return 0
    if args.command in {"judge", "process", "run"}:
        llm_error = llm_preflight_error(config)
        if llm_error:
            print(f"error: {llm_error}", file=sys.stderr)
            return 2
    with storage.connect(config) as conn:
        if args.command == "ingest":
            stats = feeds.ingest(config, conn, limit_per_feed=args.limit_per_feed, published_since=args.since)
            _print_stats(stats)
            return 0
        if args.command == "judge":
            try:
                print(f"judged={pipeline.judge_pending(config, conn, limit=args.limit, published_since=args.since)}")
            except llm.LLMError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0
        if args.command == "process":
            try:
                print(f"processed={pipeline.process_relevant(config, conn, limit=args.limit, published_since=args.since)}")
            except llm.LLMError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0
        if args.command == "run":
            try:
                _print_stats(pipeline.run(config, conn, limit=args.limit, published_since=args.since))
            except llm.LLMError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0
        if args.command == "build-site":
            _print_stats(site.build_site(config, conn))
            return 0
        if args.command == "list":
            return list_status(conn, args.status, args.limit)
    if args.command == "serve-site":
        return serve_site(config, args.host, args.port)
    parser.error(f"unknown command: {args.command}")
    return 2


def doctor(config: Config) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    if not config.feeds:
        errors.append("no feeds configured")
    if not config.labs:
        errors.append("no labs configured")
    if config.llm.provider == "openai_compatible" and config.llm.api_key_env:
        if not os.environ.get(config.llm.api_key_env):
            warnings.append(f"{config.llm.api_key_env} is not set; judging/summarization will fail")
    if config.llm.provider == "ollama" and not config.llm.base_url:
        errors.append("llm.base_url is required for ollama")
    if config.transcription.provider == "command":
        if shutil.which(config.transcription.command) is None:
            warnings.append(f"transcription command not found: {config.transcription.command}")
    config.app.state_dir.mkdir(parents=True, exist_ok=True)
    with storage.connect(config) as conn:
        counts = storage.status_counts(conn)
    print(f"config_root={config.root}")
    print(f"database={config.app.database_path}")
    print(f"public_dir={config.app.public_dir}")
    print(f"active_feeds={len(config.active_feeds)}")
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
