from __future__ import annotations

import dataclasses
import ast
import json
import pathlib
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10
    tomllib = None


@dataclasses.dataclass(frozen=True)
class AppConfig:
    timezone: str = "America/New_York"
    database_path: pathlib.Path = pathlib.Path("var/radar.sqlite3")
    public_dir: pathlib.Path = pathlib.Path("public")
    state_dir: pathlib.Path = pathlib.Path("var")
    processed_after: str | None = None
    lookback_days: int = 14
    user_agent: str = "ai-radar/0.1"


@dataclasses.dataclass(frozen=True)
class LLMConfig:
    provider: str = "openai_compatible"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    model: str = "minimax/minimax-m3"
    temperature: float = 0.1
    max_metadata_chars: int = 12000
    max_transcript_chars: int = 130000
    timeout_seconds: int = 120
    max_attempts: int = 4
    retry_backoff_seconds: float = 2.0
    max_retry_sleep_seconds: float = 60.0


@dataclasses.dataclass(frozen=True)
class TranscriptionConfig:
    provider: str = "command"
    command: str = "whisper-cli"
    args: tuple[str, ...] = (
        "-m",
        "models/ggml-large-v3-turbo.bin",
        "-f",
        "{audio_path}",
        "-otxt",
        "-of",
        "{output_stem}",
    )
    output_path: str = "{output_stem}.txt"
    audio_dir: pathlib.Path = pathlib.Path("var/audio")
    transcript_dir: pathlib.Path = pathlib.Path("var/transcripts")
    max_audio_mb: int = 750
    keep_audio: bool = False
    # "local" transcribes in-process with whisper-cli. "remote" hands the work to
    # a Mac worker with Metal through the transcription broker, which is how this
    # runs on the homelab where there is no GPU.
    mode: str = "local"
    queue_root: pathlib.Path = pathlib.Path("var/queue")
    worker_ssh: str = ""
    worker_command: str = ""
    lease_hours: int = 6


@dataclasses.dataclass(frozen=True)
class SiteConfig:
    title: str = "AI Radar"
    description: str = "Substantial podcasts, videos, articles, and threads from major AI lab leaders."
    base_url: str = "https://llm-podcasts.merimerimeri.com"
    cname: str = "llm-podcasts.merimerimeri.com"
    max_items: int = 100
    rss_title: str = "AI Radar"
    rss_description: str = "Substantial public appearances and writing from major AI lab leaders."


@dataclasses.dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    hosts: tuple[str, ...] = ()
    active: bool = True


@dataclasses.dataclass(frozen=True)
class SourceConfig:
    kind: str
    name: str
    url: str
    external_id: str = ""
    feed_url: str = ""
    playlist_url: str = ""
    people: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    active: bool = True
    api_key_env: str = ""


@dataclasses.dataclass(frozen=True)
class LabConfig:
    name: str
    aliases: tuple[str, ...] = ()
    people: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Config:
    root: pathlib.Path
    app: AppConfig
    llm: LLMConfig
    transcription: TranscriptionConfig
    site: SiteConfig
    feeds: tuple[FeedConfig, ...]
    labs: tuple[LabConfig, ...]
    sources: tuple[SourceConfig, ...] = ()

    @property
    def active_feeds(self) -> tuple[FeedConfig, ...]:
        return tuple(feed for feed in self.feeds if feed.active)

    @property
    def active_sources(self) -> tuple[SourceConfig, ...]:
        podcast_sources = tuple(
            SourceConfig(
                kind="podcast",
                name=feed.name,
                url=feed.url,
                feed_url=feed.url,
                hosts=feed.hosts,
                active=feed.active,
            )
            for feed in self.feeds
            if feed.active
        )
        return podcast_sources + tuple(source for source in self.sources if source.active)

    @property
    def watched_people(self) -> list[str]:
        people: list[str] = []
        for lab in self.labs:
            people.extend(lab.people)
        return sorted(set(people))


def load_config(path: str | pathlib.Path) -> Config:
    config_path = pathlib.Path(path).expanduser().resolve()
    raw = _load_toml(config_path)
    root = config_path.parent
    return Config(
        root=root,
        app=_app(raw.get("app", {}), root),
        llm=_dataclass_from_dict(LLMConfig, raw.get("llm", {})),
        transcription=_transcription(raw.get("transcription", {}), root),
        site=_dataclass_from_dict(SiteConfig, raw.get("site", {})),
        feeds=tuple(_feed(item) for item in raw.get("feeds", [])),
        labs=tuple(_lab(item) for item in raw.get("labs", [])),
        sources=tuple(_source(item) for item in raw.get("sources", [])),
    )


def _app(raw: dict[str, Any], root: pathlib.Path) -> AppConfig:
    config = _dataclass_from_dict(AppConfig, raw)
    return dataclasses.replace(
        config,
        database_path=_resolve(root, config.database_path),
        public_dir=_resolve(root, config.public_dir),
        state_dir=_resolve(root, config.state_dir),
    )


def _transcription(raw: dict[str, Any], root: pathlib.Path) -> TranscriptionConfig:
    config = _dataclass_from_dict(TranscriptionConfig, raw)
    return dataclasses.replace(
        config,
        args=tuple(config.args),
        audio_dir=_resolve(root, config.audio_dir),
        transcript_dir=_resolve(root, config.transcript_dir),
        queue_root=_resolve(root, config.queue_root),
    )


def _feed(raw: dict[str, Any]) -> FeedConfig:
    return FeedConfig(
        name=str(raw["name"]),
        url=str(raw["url"]),
        hosts=tuple(str(value) for value in raw.get("hosts", [])),
        active=bool(raw.get("active", True)),
    )


def _source(raw: dict[str, Any]) -> SourceConfig:
    kind = str(raw["kind"]).strip().lower()
    if kind not in {"podcast", "youtube", "blog", "x"}:
        raise ValueError(f"unsupported source kind: {kind}")
    return SourceConfig(
        kind=kind,
        name=str(raw["name"]),
        url=str(raw["url"]),
        external_id=str(raw.get("external_id", "")),
        feed_url=str(raw.get("feed_url", "")),
        playlist_url=str(raw.get("playlist_url", "")),
        people=tuple(str(value) for value in raw.get("people", [])),
        hosts=tuple(str(value) for value in raw.get("hosts", [])),
        active=bool(raw.get("active", True)),
        api_key_env=str(raw.get("api_key_env", "")),
    )


def _lab(raw: dict[str, Any]) -> LabConfig:
    return LabConfig(
        name=str(raw["name"]),
        aliases=tuple(str(value) for value in raw.get("aliases", [])),
        people=tuple(str(value) for value in raw.get("people", [])),
    )


def _dataclass_from_dict(cls: type[Any], raw: dict[str, Any]) -> Any:
    field_names = {field.name for field in dataclasses.fields(cls)}
    values = {key: value for key, value in raw.items() if key in field_names}
    return cls(**values)


def _resolve(root: pathlib.Path, path: pathlib.Path | str) -> pathlib.Path:
    value = pathlib.Path(path).expanduser()
    if value.is_absolute():
        return value
    return root / value


def _load_toml(path: pathlib.Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)
    return _parse_basic_toml(text)


def _parse_basic_toml(text: str) -> dict[str, Any]:
    """Parse the small TOML subset used by config.toml on Python 3.9."""
    root: dict[str, Any] = {}
    current: dict[str, Any] = root
    lines = iter(enumerate(text.splitlines(), start=1))
    for line_number, line in lines:
        stripped = _strip_comment(line).strip()
        if not stripped:
            continue
        if stripped.startswith("[[") and stripped.endswith("]]"):
            name = stripped[2:-2].strip()
            table: dict[str, Any] = {}
            root.setdefault(name, []).append(table)
            current = table
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            current = root.setdefault(name, {})
            continue
        if "=" not in stripped:
            raise ValueError(f"invalid TOML line {line_number}: {line}")
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith("[") and not raw_value.endswith("]"):
            parts = [raw_value]
            for _, continuation in lines:
                continued = _strip_comment(continuation).strip()
                if continued:
                    parts.append(continued)
                if continued.endswith("]"):
                    break
            raw_value = " ".join(parts)
        current[key] = _parse_value(raw_value)
    return root


def _strip_comment(line: str) -> str:
    in_string = False
    escaped = False
    output: list[str] = []
    for char in line:
        if char == "\\" and in_string:
            escaped = not escaped
            output.append(char)
            continue
        if char == '"' and not escaped:
            in_string = not in_string
        escaped = False
        if char == "#" and not in_string:
            break
        output.append(char)
    return "".join(output)


def _parse_value(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("[") and value.endswith("]"):
        return ast.literal_eval(value)
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value
