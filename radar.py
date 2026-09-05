from __future__ import annotations

import argparse
import copy
import datetime as dt
import email.utils
import html
import http.client
import ipaddress
import json
import os
import pathlib
import re
import socket
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from error_reporter import ErrorReporter

from radar_common import (
    LLMSettings,
    RETRYABLE_HTTP_CODES,
    RadarError,
    Settings,
    Source,
    _read_bounded,
    clean_text,
    is_youtube_short,
    media_identity,
    parse_timestamp,
    parse_utc_timestamp,
    public_http_url,
    require_public_http_url,
    source_family,
    stable_id,
    strip_html,
)

from editorial import (
    summarize_group,
    summary_contract_errors,
)

from rendering import (
    GENERATED_FILES,
    render_feeds_html,
    render_headers,
    render_html,
    render_rss,
)


ARCHIVE_VERSION = 1
DEFAULT_ARCHIVE = pathlib.Path("data/items.json")
DEFAULT_CONFIG = pathlib.Path("config.toml")
ALLOWED_STATUSES = frozenset({"seen", "deferred", "skipped", "published"})
APPEARANCE_KINDS = frozenset({"newsletter", "podcast", "youtube"})
MAX_FEED_BYTES = 16 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_APPEARANCE_DESCRIPTION_CHARS = 24_000
YOUTUBE_RETRY_DELAY_SECONDS = 60
YOUTUBE_OUTAGE_MIN_SOURCES = 3
ITEM_FIELDS = frozenset(
    {
        "id",
        "status",
        "title",
        "source_title",
        "short_summary",
        "long_summary",
        "reason",
        "published_at",
        "first_seen_at",
        "appearances",
        "links",
    }
)
APPEARANCE_FIELDS = frozenset(
    {
        "id",
        "kind",
        "source",
        "family",
        "guid",
        "title",
        "description",
        "url",
        "published_at",
        "hosts",
    }
)


@dataclass(frozen=True)
class SourceFailure:
    source: Source
    error: BaseException
    stage: str


def load_settings(config_path: pathlib.Path, archive_path: pathlib.Path) -> Settings:
    config_path = config_path.expanduser().resolve()
    root = config_path.parent
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    app = raw.get("app", {})
    site = raw.get("site", {})
    llm = raw.get("llm", {})
    sources: list[Source] = []

    for feed in raw.get("feeds", []):
        if not feed.get("active", True):
            continue
        name = str(feed["name"])
        sources.append(
            Source(
                kind="podcast",
                name=name,
                feed_url=str(feed["url"]),
                homepage_url=str(feed.get("homepage_url") or feed["url"]),
                family=source_family(name),
                hosts=tuple(str(value) for value in feed.get("hosts", [])),
            )
        )

    for newsletter in raw.get("newsletters", []):
        if not newsletter.get("active", True):
            continue
        name = str(newsletter["name"])
        feed_url = str(newsletter["feed_url"])
        sources.append(
            Source(
                kind="newsletter",
                name=name,
                feed_url=feed_url,
                homepage_url=str(newsletter.get("url") or feed_url),
                family=source_family(name),
                hosts=tuple(str(value) for value in newsletter.get("authors", [])),
            )
        )

    for source in raw.get("sources", []):
        if not source.get("active", True) or source.get("kind") != "youtube":
            continue
        name = str(source["name"])
        feed_url = youtube_feed_url(source)
        if not feed_url:
            print(f"warning: skipping YouTube source without a public feed: {name}", file=sys.stderr)
            continue
        sources.append(
            Source(
                kind="youtube",
                name=name,
                feed_url=feed_url,
                homepage_url=str(source.get("url") or feed_url),
                family=source_family(name),
                hosts=tuple(str(value) for value in source.get("people", [])),
            )
        )

    roster: list[str] = []
    for lab in raw.get("labs", []):
        people = ", ".join(str(person) for person in lab.get("people", []))
        aliases = ", ".join(str(alias) for alias in lab.get("aliases", []))
        roster.append(f"{lab['name']} (aliases: {aliases}; people: {people})")

    resolved_archive = archive_path if archive_path.is_absolute() else root / archive_path
    public_value = pathlib.Path(str(app.get("public_dir", "public")))
    public_dir = public_value if public_value.is_absolute() else root / public_value
    settings = Settings(
        archive_path=resolved_archive,
        public_dir=public_dir,
        user_agent=str(app.get("user_agent", "ai-radar/1.0")),
        base_url=str(site.get("base_url", "https://ai-radar.merimerimeri.com")).rstrip("/"),
        title=str(site.get("title", "AI Radar")),
        description=str(
            site.get(
                "description",
                "Noteworthy AI conversations and writing, summarized from publisher notes.",
            )
        ),
        sources=tuple(sources),
        roster=tuple(roster),
        llm=LLMSettings(
            base_url=str(llm.get("base_url", "https://openrouter.ai/api/v1")),
            api_key_env=str(llm.get("api_key_env", "OPENROUTER_API_KEY")),
            model=str(llm.get("model", "openrouter/auto")),
            temperature=float(llm.get("temperature", 0.1)),
            max_metadata_chars=int(llm.get("max_metadata_chars", 12000)),
            timeout_seconds=int(llm.get("timeout_seconds", 120)),
            max_attempts=int(llm.get("max_attempts", 4)),
            retry_backoff_seconds=float(llm.get("retry_backoff_seconds", 2.0)),
            max_retry_sleep_seconds=float(llm.get("max_retry_sleep_seconds", 60.0)),
            max_output_tokens=int(llm.get("max_output_tokens", 1200)),
        ),
    )
    validate_settings(settings)
    return settings


def youtube_feed_url(source: dict[str, Any]) -> str | None:
    if source.get("feed_url"):
        return str(source["feed_url"])
    playlist = str(source.get("playlist_url") or "")
    parsed = urllib.parse.urlparse(playlist)
    playlist_id = urllib.parse.parse_qs(parsed.query).get("list", [""])[0]
    if playlist_id:
        return f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
    channel_id = str(source.get("external_id") or "")
    if channel_id:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    return None


def validate_fetch_destination(url: str) -> str:
    url = require_public_http_url(url, label="feed URL")
    hostname = urllib.parse.urlparse(url).hostname or ""
    if hostname.casefold() == "localhost" or hostname.endswith(".localhost"):
        raise RadarError(f"refusing local feed host: {hostname}")
    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RadarError(f"feed host could not be resolved: {hostname}") from exc
    for address in {entry[4][0] for entry in addresses}:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise RadarError(f"feed host resolved to an invalid address: {hostname}") from exc
        if not ip.is_global:
            raise RadarError(f"refusing non-public feed address for {hostname}")
    return url


def validate_settings(settings: Settings) -> None:
    require_public_http_url(settings.base_url, label="site base_url")
    if settings.llm.max_attempts < 1:
        raise RadarError("llm.max_attempts must be at least one")
    if settings.llm.max_output_tokens < 1:
        raise RadarError("llm.max_output_tokens must be at least one")
    names: set[str] = set()
    for source in settings.sources:
        if source.kind not in APPEARANCE_KINDS:
            raise RadarError(f"unsupported source kind: {source.kind}")
        if source.name in names:
            raise RadarError(f"duplicate source name: {source.name}")
        names.add(source.name)
        require_public_http_url(source.feed_url, label=f"feed URL for {source.name}")
        require_public_http_url(source.homepage_url, label=f"homepage URL for {source.name}")


def load_archive(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": ARCHIVE_VERSION, "items": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_archive(value, label=str(path))
    return value


def save_archive(path: pathlib.Path, archive: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(archive, ensure_ascii=False, indent=2) + "\n")


def write_text_atomic(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _valid_timestamp(value: object, *, optional: bool = False) -> bool:
    if value is None:
        return optional
    parsed = parse_timestamp(value)
    return parsed is not None and parsed.tzinfo is not None


def validate_archive(archive: object, *, label: str = "archive") -> dict[str, int]:
    errors: list[str] = []
    if not isinstance(archive, dict):
        raise RadarError(f"{label} must be a JSON object")
    if archive.get("version") != ARCHIVE_VERSION or not isinstance(archive.get("items"), list):
        raise RadarError(f"unsupported archive format: {label}")

    item_ids: set[str] = set()
    appearance_ids: set[str] = set()
    media_owners: dict[tuple[str, str, str], str] = {}
    status_counts: dict[str, int] = {status: 0 for status in ALLOWED_STATUSES}
    for index, item_value in enumerate(archive["items"]):
        prefix = f"items[{index}]"
        if not isinstance(item_value, dict):
            errors.append(f"{prefix} was not an object")
            continue
        item = item_value
        if set(item) != ITEM_FIELDS:
            errors.append(f"{prefix} had unexpected fields")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            errors.append(f"{prefix}.id was invalid")
            item_id = prefix
        elif item_id in item_ids:
            errors.append(f"duplicate item id: {item_id}")
        item_ids.add(str(item_id))
        status = item.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{prefix}.status was invalid")
        else:
            status_counts[str(status)] += 1
        for field in ("title", "source_title", "short_summary", "long_summary", "reason"):
            if not isinstance(item.get(field), str):
                errors.append(f"{prefix}.{field} was not text")
        if not _valid_timestamp(item.get("published_at"), optional=True):
            errors.append(f"{prefix}.published_at was invalid")
        if not _valid_timestamp(item.get("first_seen_at")):
            errors.append(f"{prefix}.first_seen_at was invalid")

        appearances = item.get("appearances")
        if not isinstance(appearances, list) or not appearances:
            errors.append(f"{prefix}.appearances was empty or invalid")
            appearances = []
        kinds: set[str] = set()
        for appearance_index, appearance_value in enumerate(appearances):
            appearance_prefix = f"{prefix}.appearances[{appearance_index}]"
            if not isinstance(appearance_value, dict):
                errors.append(f"{appearance_prefix} was not an object")
                continue
            appearance_item = appearance_value
            if set(appearance_item) != APPEARANCE_FIELDS:
                errors.append(f"{appearance_prefix} had unexpected fields")
            appearance_id = appearance_item.get("id")
            if not isinstance(appearance_id, str) or not appearance_id:
                errors.append(f"{appearance_prefix}.id was invalid")
            elif appearance_id in appearance_ids:
                errors.append(f"duplicate appearance id: {appearance_id}")
            else:
                appearance_ids.add(appearance_id)
            kind = appearance_item.get("kind")
            if kind not in APPEARANCE_KINDS:
                errors.append(f"{appearance_prefix}.kind was invalid")
            elif kind in kinds:
                errors.append(f"{prefix} had more than one {kind} appearance")
            else:
                kinds.add(str(kind))
            for field in ("source", "family", "guid", "title", "description"):
                if not isinstance(appearance_item.get(field), str):
                    errors.append(f"{appearance_prefix}.{field} was not text")
            if public_http_url(appearance_item.get("url")) is None:
                errors.append(f"{appearance_prefix}.url was unsafe")
            if not _valid_timestamp(appearance_item.get("published_at"), optional=True):
                errors.append(f"{appearance_prefix}.published_at was invalid")
            hosts = appearance_item.get("hosts")
            if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
                errors.append(f"{appearance_prefix}.hosts was invalid")
            identity = media_identity(appearance_item)
            if not identity[2]:
                errors.append(f"{appearance_prefix} had no canonical media identity")
            owner = media_owners.get(identity)
            if owner is not None and owner != item_id:
                errors.append(f"media identity {identity!r} belonged to both {owner} and {item_id}")
            else:
                media_owners[identity] = str(item_id)

        links = item.get("links")
        expected_links = source_links(
            appearances,
            preferred_title=str(item.get("source_title") or item.get("title") or ""),
        )
        if not isinstance(links, dict) or links != expected_links:
            errors.append(f"{prefix}.links did not match its appearances")
        if status == "published":
            if not links:
                errors.append(f"{prefix} was published without a link")
            errors.extend(
                f"{prefix}.{message}"
                for message in summary_contract_errors(
                    title=str(item.get("title") or ""),
                    short_summary=str(item.get("short_summary") or ""),
                    long_summary=str(item.get("long_summary") or ""),
                    reason=str(item.get("reason") or ""),
                    freshly_generated=False,
                )
            )
        if status == "deferred" and (item.get("short_summary") or item.get("long_summary")):
            errors.append(f"{prefix} was deferred with a summary")

    if errors:
        shown = "; ".join(errors[:20])
        suffix = f"; and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise RadarError(f"{label} validation failed: {shown}{suffix}")
    return {
        "items": len(archive["items"]),
        "appearances": len(appearance_ids),
        **{status: status_counts[status] for status in sorted(status_counts)},
    }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _fetch_once(url: str, *, user_agent: str, timeout: int, max_bytes: int) -> bytes:
    opener = urllib.request.build_opener(_NoRedirectHandler())
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_fetch_destination(current_url)
        request = urllib.request.Request(current_url, headers={"User-Agent": user_agent})
        try:
            with opener.open(request, timeout=timeout) as response:
                return _read_bounded(response, max_bytes=max_bytes, label="feed response")
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise
            location = exc.headers.get("Location")
            if not location:
                raise RadarError(f"feed redirect {exc.code} had no Location header") from exc
            current_url = urllib.parse.urljoin(current_url, location)
    raise RadarError(f"feed exceeded {MAX_REDIRECTS} redirects")


def fetch_bytes(
    url: str,
    *,
    user_agent: str,
    timeout: int = 45,
    max_attempts: int = 3,
    max_bytes: int = MAX_FEED_BYTES,
) -> bytes:
    error: BaseException | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            return _fetch_once(url, user_agent=user_agent, timeout=timeout, max_bytes=max_bytes)
        except urllib.error.HTTPError as exc:
            error = exc
            retryable = exc.code in RETRYABLE_HTTP_CODES or 500 <= exc.code < 600
            if not retryable or attempt >= max_attempts:
                raise RadarError(f"feed HTTP {exc.code}: {url[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            error = exc
            if attempt >= max_attempts:
                raise RadarError(f"feed request failed: {exc}") from exc
        delay = min(2 ** (attempt - 1), 8)
        print(f"warning: feed request failed: {error}; retrying in {delay}s", file=sys.stderr)
        time.sleep(delay)
    raise RadarError("feed request failed")


def parse_feed(xml_bytes: bytes, source: Source) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    if local_name(root.tag) == "feed":
        return parse_atom(root, source)
    channel = first_child(root, "channel")
    if channel is None:
        channel = root
    entries: list[dict[str, Any]] = []
    for element in children(channel, "item"):
        title = element_text(element, "title")
        if not title:
            continue
        description = feed_entry_description(element, source, atom=False)
        url = public_http_url(element_text(element, "link")) or source.homepage_url
        guid = element_text(element, "guid") or url or stable_id(title, element_text(element, "pubDate") or "")
        entries.append(
            appearance(
                source,
                guid=guid,
                title=title,
                description=description,
                url=url,
                published_at=parse_date(element_text(element, "pubDate") or element_text(element, "published")),
            )
        )
    return entries


def parse_atom(root: ET.Element, source: Source) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for element in children(root, "entry"):
        title = element_text(element, "title")
        if not title:
            continue
        url = (
            public_http_url(atom_link(element, "alternate"))
            or public_http_url(atom_link(element, None))
            or source.homepage_url
        )
        guid = element_text(element, "id") or url or stable_id(title, element_text(element, "published") or "")
        description = feed_entry_description(element, source, atom=True)
        entries.append(
            appearance(
                source,
                guid=guid,
                title=title,
                description=description,
                url=url,
                published_at=parse_date(element_text(element, "published") or element_text(element, "updated")),
            )
        )
    return entries


def feed_entry_description(element: ET.Element, source: Source, *, atom: bool) -> str:
    if source.kind == "newsletter":
        fields = ("content", "summary", "description") if atom else (
            "encoded",
            "content",
            "description",
            "summary",
        )
    else:
        fields = ("summary", "description", "content") if atom else (
            "description",
            "summary",
            "encoded",
        )
    for field in fields:
        value = element_text(element, field)
        if value:
            return strip_html(value)
    return ""


def appearance(
    source: Source,
    *,
    guid: str,
    title: str,
    description: str,
    url: str,
    published_at: str | None,
) -> dict[str, Any]:
    url = require_public_http_url(url, label=f"episode URL from {source.name}")
    identity = stable_id(source.kind, source.name, guid)
    return {
        "id": identity,
        "kind": source.kind,
        "source": source.name,
        "family": source.family,
        "guid": guid,
        "title": clean_text(title),
        "description": clean_text(description)[:MAX_APPEARANCE_DESCRIPTION_CHARS],
        "url": url,
        "published_at": published_at,
        "hosts": list(source.hosts),
    }


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if local_name(child.tag) == name]


def first_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in list(element) if local_name(child.tag) == name), None)


def element_text(element: ET.Element, name: str) -> str:
    for descendant in element.iter():
        if descendant is not element and local_name(descendant.tag) == name and descendant.text:
            return clean_text(descendant.text)
    return ""


def atom_link(element: ET.Element, rel: str | None) -> str | None:
    for child in children(element, "link"):
        child_rel = child.attrib.get("rel")
        if rel is None or child_rel == rel:
            return child.attrib.get("href")
    return None


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        parsed = parse_timestamp(value)
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def normalized_title(value: str) -> str:
    value = html.unescape(value).casefold()
    value = re.sub(r"\b(full episode|podcast|video|official)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def close_in_time(first: str | None, second: str | None, days: int = 10) -> bool:
    left = parse_utc_timestamp(first)
    right = parse_utc_timestamp(second)
    if left is None or right is None:
        return True
    return abs(left - right) <= dt.timedelta(days=days)


def title_score(first: str, second: str) -> float:
    left = normalized_title(first)
    right = normalized_title(second)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def appearance_owners(
    archive: dict[str, Any],
) -> tuple[
    dict[str, tuple[dict[str, Any], dict[str, Any]]],
    dict[tuple[str, str, str], tuple[dict[str, Any], dict[str, Any]]],
]:
    by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    by_media: dict[tuple[str, str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for item in archive["items"]:
        for appearance_value in item.get("appearances", []):
            by_id[str(appearance_value["id"])] = (item, appearance_value)
            by_media[media_identity(appearance_value)] = (item, appearance_value)
    return by_id, by_media


def matching_score(
    appearances: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    title: str | None = None,
    published_at: str | None = None,
) -> float:
    """Exact identity wins; fuzzy matches must preserve every medium's identity."""
    if any(media_identity(value) == media_identity(candidate) for value in appearances):
        return 2.0
    if not appearances or is_youtube_short(candidate):
        return 0.0
    if any(is_youtube_short(value) or value["kind"] == candidate["kind"] for value in appearances):
        return 0.0
    exemplar = appearances[0]
    if not close_in_time(published_at or exemplar.get("published_at"), candidate.get("published_at")):
        return 0.0
    score = title_score(title or exemplar["title"], candidate["title"])
    if candidate["family"] in {value["family"] for value in appearances}:
        score += 0.08
    return score if score >= 0.84 else 0.0


def matching_item(archive: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    best_score = 0.0
    best = None
    for item in archive["items"]:
        score = matching_score(
            item["appearances"], candidate,
            title=item.get("source_title") or item.get("title"),
            published_at=item.get("published_at"),
        )
        if score > best_score:
            best_score, best = score, item
    return best


def group_candidates(candidates: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda value: value.get("published_at") or ""):
        best_score = 0.0
        match = None
        for group in groups:
            score = matching_score(group, candidate)
            if score > best_score:
                best_score, match = score, group
        if match is None:
            groups.append([candidate])
        else:
            existing = next(
                (value for value in match if media_identity(value) == media_identity(candidate)),
                None,
            )
            if existing is not None:
                refresh_appearance(existing, candidate)
            else:
                match.append(candidate)
    return groups


def add_appearances(item: dict[str, Any], appearances: list[dict[str, Any]]) -> int:
    known = {value["id"] for value in item.get("appearances", [])}
    known_media = {media_identity(value) for value in item.get("appearances", [])}
    known_kinds = {value["kind"] for value in item.get("appearances", [])}
    added = 0
    for value in appearances:
        if value["id"] in known or media_identity(value) in known_media or value["kind"] in known_kinds:
            continue
        item.setdefault("appearances", []).append(value)
        known.add(value["id"])
        known_media.add(media_identity(value))
        known_kinds.add(value["kind"])
        added += 1
    item["links"] = source_links(
        item["appearances"], preferred_title=str(item.get("source_title") or item.get("title") or "")
    )
    return added


def source_links(
    appearances: list[dict[str, Any]],
    *,
    preferred_title: str = "",
) -> dict[str, str]:
    links: dict[str, str] = {}
    for kind in ("podcast", "youtube", "newsletter"):
        values = [value for value in appearances if value.get("kind") == kind]
        if preferred_title:
            values.sort(
                key=lambda value: (
                    title_score(preferred_title, str(value.get("title") or "")),
                    str(value.get("url") or ""),
                ),
                reverse=True,
            )
        for value in values:
            url = public_http_url(value.get("url"))
            if url:
                links[kind] = url
                break
    return links


def canonicalize_group(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_kind: dict[str, dict[str, Any]] = {}
    for value in group:
        kind = str(value["kind"])
        current = by_kind.get(kind)
        if current is not None and media_identity(current) != media_identity(value):
            raise RadarError(f"group contained distinct {kind} appearances")
        if current is None or (
            len(clean_text(value.get("description") or "")),
            str(value.get("id") or ""),
        ) > (
            len(clean_text(current.get("description") or "")),
            str(current.get("id") or ""),
        ):
            by_kind[kind] = value
    return [by_kind[kind] for kind in sorted(by_kind)]


def refresh_appearance(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    changed = False
    if len(clean_text(candidate.get("description") or "")) > len(
        clean_text(existing.get("description") or "")
    ):
        existing["description"] = candidate["description"]
        changed = True
    if not existing.get("published_at") and candidate.get("published_at"):
        existing["published_at"] = candidate["published_at"]
        changed = True
    hosts = sorted({str(value) for value in existing.get("hosts", []) + candidate.get("hosts", []) if value})
    if hosts != existing.get("hosts", []):
        existing["hosts"] = hosts
        changed = True
    return changed


def update_item_from_result(
    item: dict[str, Any],
    result: dict[str, Any],
    group: list[dict[str, Any]],
) -> None:
    best = max(group, key=lambda value: len(clean_text(value.get("description") or "")))
    dates = sorted(value["published_at"] for value in group if value.get("published_at"))
    item.update(
        {
            "status": result["status"],
            "title": result["title"],
            "source_title": best["title"],
            "short_summary": result["short_summary"],
            "long_summary": result["long_summary"],
            "reason": result["reason"],
            "published_at": dates[0] if dates else item.get("published_at"),
            "appearances": group,
            "links": source_links(group, preferred_title=best["title"]),
        }
    )


def is_youtube_rss_outage(failures: list[SourceFailure], *, youtube_source_count: int) -> bool:
    youtube_fetch_failures = sum(
        failure.source.kind == "youtube" and failure.stage == "fetch" for failure in failures
    )
    return (
        youtube_fetch_failures >= YOUTUBE_OUTAGE_MIN_SOURCES
        and youtube_fetch_failures * 2 >= youtube_source_count
    )


def report_source_failures(
    settings: Settings,
    failures: list[SourceFailure],
    reporter: ErrorReporter,
    *,
    retry_attempts: int,
    retry_recoveries: int,
) -> None:
    youtube_count = sum(source.kind == "youtube" for source in settings.sources)
    outage = is_youtube_rss_outage(failures, youtube_source_count=youtube_count)
    latch = settings.public_dir.parent / "var/youtube-rss-outage-alerted"
    outage_failures = [failure for failure in failures
                      if outage and failure.source.kind == "youtube" and failure.stage == "fetch"]
    ordinary_failures = [failure for failure in failures if failure not in outage_failures]
    if not outage:
        try:
            latch.unlink(missing_ok=True)
        except OSError as exc:
            print(f"warning: YouTube outage alert latch could not be cleared: {exc}", file=sys.stderr)

    for batch, is_outage in ((outage_failures, True), (ordinary_failures, False)):
        if not batch:
            continue
        if is_outage and latch.exists():
            print("warning: continuing YouTube RSS outage already reported; suppressing repeat Sentry event",
                  file=sys.stderr)
            continue
        message = (f"YouTube RSS outage: {len(batch)} of {youtube_count} source(s) failed after delayed retry"
                   if is_outage else f"{len(batch)} source(s) failed during collection")
        event_id = reporter.capture_exception(
            RadarError(message),
            tags={
                "phase": "source", "source_error_count": len(batch),
                "youtube_source_error_count": sum(f.source.kind == "youtube" and f.stage == "fetch" for f in batch),
                "youtube_rss_outage": str(is_outage).lower(),
            },
            extra={
                "failures": [{"source": f.source.name, "kind": f.source.kind,
                              "stage": f.stage, "error": str(f.error)[:500]} for f in batch],
                "youtube_retry": {"attempted": retry_attempts, "recovered": retry_recoveries,
                                  "delay_seconds": YOUTUBE_RETRY_DELAY_SECONDS},
            },
            fingerprint=["ai-radar", "source", "youtube-rss-outage" if is_outage else "cycle"],
        )
        if is_outage and event_id:
            try:
                write_text_atomic(latch, str(event_id) + "\n")
            except OSError as exc:
                print(f"warning: YouTube outage alert latch could not be written: {exc}", file=sys.stderr)


def run_cycle(
    settings: Settings,
    *,
    lookback_days: int,
    reporter: ErrorReporter | None = None,
) -> dict[str, int]:
    reporter = reporter or ErrorReporter(None, status="disabled")
    archive = load_archive(settings.archive_path)
    # Refresh deferred records on copies so failed model calls preserve their retry trigger.
    working_archive = {
        "items": [copy.deepcopy(item) if item["status"] == "deferred" else item
                  for item in archive["items"]]
    }
    by_id, by_media = appearance_owners(working_archive)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    collected: list[dict[str, Any]] = []
    source_failures: list[SourceFailure] = []
    youtube_retry_candidates: list[tuple[Source, BaseException]] = []
    youtube_retry_recoveries = 0
    reevaluate: dict[str, dict[str, Any]] = {}

    def process_entries(source: Source, entries: list[dict[str, Any]]) -> None:
        undated = sum(not entry.get("published_at") for entry in entries)
        if undated:
            detail = f"{undated} of {len(entries)} feed entries had no valid date"
            if undated == len(entries):
                # Only a feed whose dates are entirely unusable is a real failure.
                # An undated entry can never clear the lookback cutoff, so alerting
                # on a handful of them would re-report the same stale archive rows
                # on every cycle, forever.
                error = RadarError(detail)
                source_failures.append(SourceFailure(source, error, "metadata"))
                print(f"warning: source metadata failed: {source.name}: {error}", file=sys.stderr)
            else:
                print(f"warning: source metadata partly unusable: {source.name}: {detail}", file=sys.stderr)
        for entry in entries:
            if not is_recent(entry.get("published_at"), cutoff):
                continue
            owner = by_id.get(entry["id"]) or by_media.get(media_identity(entry))
            if owner is not None:
                item, existing = owner
                if refresh_appearance(existing, entry) and item.get("status") == "deferred":
                    reevaluate[str(item["id"])] = item
                continue
            collected.append(entry)

    for source in settings.sources:
        try:
            entries = parse_feed(fetch_bytes(source.feed_url, user_agent=settings.user_agent), source)
        except Exception as exc:  # noqa: BLE001 - one broken feed must not block all sources
            if source.kind == "youtube":
                youtube_retry_candidates.append((source, exc))
                print(
                    f"warning: YouTube source failed; delayed retry queued: {source.name}: {exc}",
                    file=sys.stderr,
                )
            else:
                source_failures.append(SourceFailure(source, exc, "fetch"))
                print(f"warning: source failed: {source.name}: {exc}", file=sys.stderr)
            continue
        process_entries(source, entries)

    if youtube_retry_candidates:
        print(
            f"warning: retrying {len(youtube_retry_candidates)} YouTube source(s) "
            f"after {YOUTUBE_RETRY_DELAY_SECONDS}s",
            file=sys.stderr,
        )
        time.sleep(YOUTUBE_RETRY_DELAY_SECONDS)
        for source, _initial_error in youtube_retry_candidates:
            try:
                entries = parse_feed(fetch_bytes(source.feed_url, user_agent=settings.user_agent), source)
            except Exception as exc:  # noqa: BLE001 - report only after the delayed retry
                source_failures.append(SourceFailure(source, exc, "fetch"))
                print(f"warning: YouTube source retry failed: {source.name}: {exc}", file=sys.stderr)
                continue
            youtube_retry_recoveries += 1
            print(f"YouTube source recovered after retry: {source.name}")
            process_entries(source, entries)

    report_source_failures(
        settings, source_failures, reporter,
        retry_attempts=len(youtube_retry_candidates), retry_recoveries=youtube_retry_recoveries,
    )

    matched = 0
    new_items = 0
    published = 0
    skipped = 0
    deferred = 0
    reevaluated = 0
    llm_errors = 0
    unmatched: list[dict[str, Any]] = []
    for candidate in collected:
        if is_youtube_short(candidate):
            unmatched.append(candidate)
            continue
        item = matching_item(working_archive, candidate)
        if item is None:
            unmatched.append(candidate)
            continue
        added = add_appearances(item, [candidate])
        matched += added
        if added and item.get("status") == "deferred":
            reevaluate[str(item["id"])] = item

    evaluations = [(item, canonicalize_group(item["appearances"])) for item in reevaluate.values()]
    evaluations.extend((None, canonicalize_group(group)) for group in group_candidates(unmatched))
    for existing, group in evaluations:
        try:
            result = summarize_group(settings, group)
        except RadarError as exc:
            llm_errors += 1
            print(f"warning: item deferred: {group[0]['title']}: {exc}", file=sys.stderr)
            reporter.capture_exception(
                exc,
                tags={"phase": "summary", "model": settings.llm.model},
                extra={"source": group[0]["source"], "title": group[0]["title"]},
                fingerprint=["ai-radar", "summary", settings.llm.model],
            )
            continue
        item = existing if existing is not None else {
            "id": stable_id(group[0]["id"]),
            "first_seen_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        }
        update_item_from_result(item, result, group)
        if existing is None:
            archive["items"].append(item)
            new_items += 1
        else:
            original = next(value for value in archive["items"] if value["id"] == item["id"])
            original.update(item)
            reevaluated += int(result["status"] != "deferred")
        if result["status"] == "published":
            published += 1
        elif result["status"] == "skipped":
            skipped += 1
        elif existing is None:
            deferred += 1

    validate_archive(archive)
    save_archive(settings.archive_path, archive)
    build_site(settings, archive)
    stats = {
        "sources": len(settings.sources),
        "source_errors": len(source_failures),
        "youtube_retry_attempts": len(youtube_retry_candidates),
        "youtube_retry_recoveries": youtube_retry_recoveries,
        "new_appearances": len(collected),
        "matched_appearances": matched,
        "new_items": new_items,
        "published": published,
        "skipped": skipped,
        "deferred": deferred,
        "reevaluated": reevaluated,
        "llm_errors": llm_errors,
    }
    return stats


def reconsider_item(settings: Settings, *, match: str) -> dict[str, int]:
    needle = clean_text(match).casefold()
    if not needle:
        raise RadarError("reconsider match must not be empty")
    archive = load_archive(settings.archive_path)
    matches = [
        item
        for item in archive["items"]
        if item.get("status") != "published"
        and needle
        in "\n".join(
            (str(item.get("title") or ""), str(item.get("source_title") or ""))
        ).casefold()
    ]
    if not matches:
        raise RadarError(f"no unpublished item matched: {match}")
    if len(matches) != 1:
        raise RadarError(f"reconsider match was ambiguous ({len(matches)} items): {match}")

    item = matches[0]
    sources = {(source.kind, source.name): source for source in settings.sources}
    fetched: dict[tuple[str, str], list[dict[str, Any]]] = {}
    fetch_errors: list[str] = []
    refreshed = 0
    for existing in item["appearances"]:
        source_key = (str(existing.get("kind") or ""), str(existing.get("source") or ""))
        source = sources.get(source_key)
        if source is None:
            continue
        if source_key not in fetched:
            try:
                fetched[source_key] = parse_feed(
                    fetch_bytes(source.feed_url, user_agent=settings.user_agent), source
                )
            except Exception as exc:  # noqa: BLE001 - another appearance may supply enough notes
                fetch_errors.append(f"{source.name}: {exc}")
                fetched[source_key] = []
        candidate = next(
            (
                value
                for value in fetched[source_key]
                if media_identity(value) == media_identity(existing)
            ),
            None,
        )
        if candidate is not None and refresh_appearance(existing, candidate):
            refreshed += 1

    group = canonicalize_group(item["appearances"])
    result = summarize_group(settings, group)
    if result["status"] == "deferred":
        detail = f"; fetch errors: {'; '.join(fetch_errors)}" if fetch_errors else ""
        raise RadarError(f"matched item still had insufficient publisher notes{detail}")
    update_item_from_result(item, result, group)
    validate_archive(archive)
    save_archive(settings.archive_path, archive)
    build_site(settings, archive)
    return {
        "matched": 1,
        "refreshed_appearances": refreshed,
        "published": int(result["status"] == "published"),
        "skipped": int(result["status"] == "skipped"),
    }


def is_recent(value: str | None, cutoff: dt.datetime) -> bool:
    parsed = parse_utc_timestamp(value)
    return parsed is not None and parsed >= cutoff


def build_site(settings: Settings, archive: dict[str, Any] | None = None) -> dict[str, int]:
    archive = archive or load_archive(settings.archive_path)
    validate_archive(archive)
    items = sorted(
        (item for item in archive["items"] if item.get("status") == "published"),
        key=lambda value: value.get("published_at") or "",
        reverse=True,
    )
    rendered = {
        "index.html": render_html(settings, items),
        "feeds.html": render_feeds_html(settings),
        "feed.xml": render_rss(settings, items),
        "_headers": render_headers(),
    }
    settings.public_dir.mkdir(parents=True, exist_ok=True)
    for name in GENERATED_FILES:
        write_text_atomic(settings.public_dir / name, rendered[name])
    return {"items": len(items), "rss_items": len(items), "feeds": len(settings.sources)}


def print_stats(stats: dict[str, int]) -> None:
    for key, value in stats.items():
        print(f"{key}={value}")


def lookback_days_from_env() -> int:
    raw = os.environ.get("AI_RADAR_LOOKBACK_DAYS", "7")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RadarError("AI_RADAR_LOOKBACK_DAYS must be an integer") from exc
    if value <= 0:
        raise RadarError("AI_RADAR_LOOKBACK_DAYS must be greater than zero")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-radar")
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    parser.add_argument("--archive", type=pathlib.Path, default=DEFAULT_ARCHIVE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Fetch new items, summarize them, and rebuild the site.")
    run_parser.add_argument("--lookback-days", type=int, default=7)
    reconsider_parser = subparsers.add_parser(
        "reconsider",
        help="Re-fetch and rejudge one unpublished item after an editorial-policy change.",
    )
    reconsider_parser.add_argument("--match", required=True, help="Unique title substring to reconsider.")
    subparsers.add_parser("build-site", help="Regenerate static HTML and RSS from the archive.")
    subparsers.add_parser("doctor", help="Validate configuration and tracked archive state.")
    args = parser.parse_args(argv)
    root = args.config.expanduser().resolve().parent
    reporter = ErrorReporter.build_from_env(root=root)
    try:
        settings = load_settings(args.config, args.archive)
        if args.command == "doctor":
            archive = load_archive(settings.archive_path)
            archive_stats = validate_archive(archive, label=str(settings.archive_path))
            print(f"archive={settings.archive_path}")
            print(f"public_dir={settings.public_dir}")
            print(f"sources={len(settings.sources)}")
            print(f"podcast_sources={sum(source.kind == 'podcast' for source in settings.sources)}")
            print(f"youtube_sources={sum(source.kind == 'youtube' for source in settings.sources)}")
            print(f"newsletter_sources={sum(source.kind == 'newsletter' for source in settings.sources)}")
            for key, value in archive_stats.items():
                print(f"archive_{key}={value}")
            print(f"sentry={reporter.status}")
            if settings.llm.api_key_env and not os.environ.get(settings.llm.api_key_env):
                print(
                    f"warning: {settings.llm.api_key_env} is not set; "
                    "run will fail every summary and persist no new items"
                )
            return 0
        if args.command == "build-site":
            print_stats(build_site(settings))
            return 0
        if args.command == "run":
            if args.lookback_days <= 0:
                raise RadarError("--lookback-days must be greater than zero")
            print_stats(run_cycle(settings, lookback_days=args.lookback_days, reporter=reporter))
            return 0
        if args.command == "reconsider":
            print_stats(reconsider_item(settings, match=args.match))
            return 0
    except Exception as exc:  # noqa: BLE001 - command failures must be reported before exit
        reporter.capture_exception(
            exc,
            tags={"command": args.command},
            fingerprint=["ai-radar", "command", str(args.command), type(exc).__name__],
        )
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        reporter.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
