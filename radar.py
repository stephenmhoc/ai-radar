from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Any


ARCHIVE_VERSION = 1
DEFAULT_ARCHIVE = pathlib.Path("data/items.json")
DEFAULT_CONFIG = pathlib.Path("config.toml")
RETRYABLE_HTTP_CODES = frozenset({408, 409, 425, 429})


@dataclass(frozen=True)
class Source:
    kind: str
    name: str
    feed_url: str
    homepage_url: str
    family: str
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key_env: str
    model: str
    temperature: float
    max_metadata_chars: int
    timeout_seconds: int
    max_attempts: int
    retry_backoff_seconds: float
    max_retry_sleep_seconds: float


@dataclass(frozen=True)
class Settings:
    root: pathlib.Path
    archive_path: pathlib.Path
    public_dir: pathlib.Path
    user_agent: str
    base_url: str
    title: str
    description: str
    sources: tuple[Source, ...]
    roster: tuple[str, ...]
    llm: LLMSettings


class RadarError(RuntimeError):
    pass


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def text(self) -> str:
        return clean_text("\n".join(self.parts))


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextParser()
    parser.feed(value)
    return parser.text()


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
    return Settings(
        root=root,
        archive_path=resolved_archive,
        public_dir=public_dir,
        user_agent=str(app.get("user_agent", "ai-radar/1.0")),
        base_url=str(site.get("base_url", "https://ai-radar.merimerimeri.com")).rstrip("/"),
        title=str(site.get("title", "AI Radar")),
        description=str(
            site.get(
                "description",
                "Noteworthy AI podcast and video episodes, summarized from publisher notes.",
            )
        ),
        sources=tuple(sources),
        roster=tuple(roster),
        llm=LLMSettings(
            base_url=str(llm.get("base_url", "https://openrouter.ai/api/v1")),
            api_key_env=str(llm.get("api_key_env", "OPENROUTER_API_KEY")),
            model=str(llm.get("model", "minimax/minimax-m3")),
            temperature=float(llm.get("temperature", 0.1)),
            max_metadata_chars=int(llm.get("max_metadata_chars", 12000)),
            timeout_seconds=int(llm.get("timeout_seconds", 120)),
            max_attempts=int(llm.get("max_attempts", 4)),
            retry_backoff_seconds=float(llm.get("retry_backoff_seconds", 2.0)),
            max_retry_sleep_seconds=float(llm.get("max_retry_sleep_seconds", 60.0)),
        ),
    )


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


def source_family(name: str) -> str:
    return re.sub(r"\s*[—-]\s*YouTube\s*$", "", name, flags=re.IGNORECASE).strip().casefold()


def load_archive(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": ARCHIVE_VERSION, "items": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != ARCHIVE_VERSION or not isinstance(value.get("items"), list):
        raise RadarError(f"unsupported archive format: {path}")
    return value


def save_archive(path: pathlib.Path, archive: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(archive, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def fetch_bytes(url: str, *, user_agent: str, timeout: int = 45) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RadarError(f"refusing non-HTTP URL: {url[:200]}")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_feed(xml_bytes: bytes, source: Source) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    if local_name(root.tag) == "feed":
        return parse_atom(root, source)
    channel = first_child(root, "channel") or root
    entries: list[dict[str, Any]] = []
    for element in children(channel, "item"):
        title = element_text(element, "title")
        if not title:
            continue
        description = strip_html(
            element_text(element, "description")
            or element_text(element, "summary")
            or element_text(element, "encoded")
        )
        url = element_text(element, "link")
        guid = element_text(element, "guid") or url or stable_id(title, element_text(element, "pubDate") or "")
        entries.append(
            appearance(
                source,
                guid=guid,
                title=title,
                description=description,
                url=url or source.homepage_url,
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
        url = atom_link(element, "alternate") or atom_link(element, None) or source.homepage_url
        guid = element_text(element, "id") or url or stable_id(title, element_text(element, "published") or "")
        description = strip_html(
            element_text(element, "summary")
            or element_text(element, "description")
            or element_text(element, "content")
        )
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


def appearance(
    source: Source,
    *,
    guid: str,
    title: str,
    description: str,
    url: str,
    published_at: str | None,
) -> dict[str, Any]:
    identity = stable_id(source.kind, source.name, guid)
    return {
        "id": identity,
        "kind": source.kind,
        "source": source.name,
        "family": source.family,
        "guid": guid,
        "title": clean_text(title),
        "description": clean_text(description),
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
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def normalized_title(value: str) -> str:
    value = html.unescape(value).casefold()
    value = re.sub(r"\b(full episode|podcast|video|official)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def close_in_time(first: str | None, second: str | None, days: int = 10) -> bool:
    if not first or not second:
        return True
    try:
        left = dt.datetime.fromisoformat(first.replace("Z", "+00:00"))
        right = dt.datetime.fromisoformat(second.replace("Z", "+00:00"))
    except ValueError:
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


def all_appearance_ids(archive: dict[str, Any]) -> set[str]:
    return {
        str(appearance_value["id"])
        for item in archive["items"]
        for appearance_value in item.get("appearances", [])
    }


def matching_item(archive: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    best: tuple[float, dict[str, Any]] | None = None
    for item in archive["items"]:
        if not close_in_time(item.get("published_at"), candidate.get("published_at")):
            continue
        score = title_score(str(item.get("source_title") or item.get("title") or ""), candidate["title"])
        families = {str(value.get("family") or "") for value in item.get("appearances", [])}
        if candidate["family"] in families:
            score += 0.08
        if score >= 0.84 and (best is None or score > best[0]):
            best = (score, item)
    return best[1] if best else None


def group_candidates(candidates: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda value: value.get("published_at") or ""):
        match = None
        for group in groups:
            exemplar = group[0]
            if not close_in_time(exemplar.get("published_at"), candidate.get("published_at")):
                continue
            score = title_score(exemplar["title"], candidate["title"])
            if exemplar["family"] == candidate["family"]:
                score += 0.08
            if score >= 0.84:
                match = group
                break
        if match is None:
            groups.append([candidate])
        else:
            match.append(candidate)
    return groups


def add_appearances(item: dict[str, Any], appearances: list[dict[str, Any]]) -> int:
    known = {value["id"] for value in item.get("appearances", [])}
    added = 0
    for value in appearances:
        if value["id"] in known:
            continue
        item.setdefault("appearances", []).append(value)
        known.add(value["id"])
        added += 1
    item["links"] = source_links(item["appearances"])
    return added


def source_links(appearances: list[dict[str, Any]]) -> dict[str, str]:
    links: dict[str, str] = {}
    for value in appearances:
        kind = str(value.get("kind") or "")
        url = str(value.get("url") or "")
        if kind in {"podcast", "youtube"} and url:
            links.setdefault(kind, url)
    return links


def summarize_group(settings: Settings, group: list[dict[str, Any]]) -> dict[str, Any]:
    best = max(group, key=lambda value: len(value.get("description") or ""))
    notes = clean_text(best.get("description") or "")
    if len(notes) < 80:
        return {
            "include": False,
            "title": best["title"],
            "summary": "",
            "reason": "Publisher notes were too sparse to summarize reliably.",
        }
    roster = "\n".join(f"- {value}" for value in settings.roster)
    appearances_text = "\n".join(
        f"- {value['kind']}: {value['source']} — {value['title']} — {value['url']}"
        for value in group
    )
    prompt = f"""
Decide whether this podcast or video belongs in AI Radar and summarize it using only the publisher-provided notes.

AI Radar includes substantial appearances by current or recent technical members, founders, executives,
senior research/engineering/product/infrastructure leaders, and explicitly listed people at these targets:
{roster}

It also includes substantial Physical AI episodes about AI-enabled robots, machines, vehicles, drones,
or industrial automation. Do not qualify an item merely because a target organization is discussed.
For interview shows, a qualifying person must be an actual guest or central speaker. Be conservative.

Appearances:
{appearances_text}

Known hosts/authors: {', '.join(best.get('hosts') or []) or 'unknown'}
Published: {best.get('published_at') or 'unknown'}

Publisher notes:
{notes[: settings.llm.max_metadata_chars]}

Return strict JSON with exactly these fields:
include: boolean
title: concise factual display title
summary: one concise paragraph of 2-4 sentences grounded only in the publisher notes
reason: concise inclusion or exclusion reason
""".strip()
    response = llm_json(
        settings.llm,
        system=(
            "You are a conservative editor. Never invent episode content. Return JSON only. "
            "If the supplied notes cannot support a useful summary, set include=false."
        ),
        user=prompt,
    )
    include = bool(response.get("include"))
    title = clean_text(str(response.get("title") or best["title"]))
    summary = clean_text(str(response.get("summary") or ""))
    if include and (not title or len(summary) < 60):
        include = False
        summary = ""
        reason = "The model did not return a sufficiently grounded summary."
    else:
        reason = clean_text(str(response.get("reason") or ""))
    return {"include": include, "title": title, "summary": summary, "reason": reason}


def llm_json(settings: LLMSettings, *, system: str, user: str) -> dict[str, Any]:
    api_key = os.environ.get(settings.api_key_env, "")
    if settings.api_key_env and not api_key:
        raise RadarError(f"missing API key env var: {settings.api_key_env}")
    payload = {
        "model": settings.model,
        "temperature": settings.temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload).encode("utf-8")
    url = settings.base_url.rstrip("/") + "/chat/completions"
    for attempt in range(1, max(1, settings.max_attempts) + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
            content = raw["choices"][0]["message"]["content"]
            return extract_json(content)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error = RadarError(f"LLM HTTP {exc.code}: {detail[:500]}")
            retryable = exc.code in RETRYABLE_HTTP_CODES or 500 <= exc.code < 600
            if attempt >= settings.max_attempts or not retryable:
                raise error from exc
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            error = RadarError(f"LLM request failed: {exc}")
            if attempt >= settings.max_attempts:
                raise error from exc
        delay = min(settings.retry_backoff_seconds * 2 ** (attempt - 1), settings.max_retry_sleep_seconds)
        print(f"warning: {error}; retrying in {delay:.1f}s", file=sys.stderr)
        time.sleep(delay)
    raise RadarError("LLM request failed")


def extract_json(content: str) -> dict[str, Any]:
    content = content.strip()
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise RadarError("LLM response did not contain JSON") from None
        value = json.loads(content[start : end + 1])
    if not isinstance(value, dict):
        raise RadarError("LLM response was not a JSON object")
    return value


def run_cycle(settings: Settings, *, lookback_days: int) -> dict[str, int]:
    if settings.llm.api_key_env and not os.environ.get(settings.llm.api_key_env):
        raise RadarError(f"missing API key env var: {settings.llm.api_key_env}")
    archive = load_archive(settings.archive_path)
    known = all_appearance_ids(archive)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    collected: list[dict[str, Any]] = []
    source_errors = 0
    for source in settings.sources:
        try:
            entries = parse_feed(fetch_bytes(source.feed_url, user_agent=settings.user_agent), source)
        except Exception as exc:  # noqa: BLE001 - one broken feed must not block all sources
            source_errors += 1
            print(f"warning: source failed: {source.name}: {exc}", file=sys.stderr)
            continue
        for entry in entries:
            if entry["id"] in known or not is_recent(entry.get("published_at"), cutoff):
                continue
            collected.append(entry)

    matched = 0
    new_items = 0
    published = 0
    skipped = 0
    llm_errors = 0
    unmatched: list[dict[str, Any]] = []
    for candidate in collected:
        item = matching_item(archive, candidate)
        if item is None:
            unmatched.append(candidate)
            continue
        matched += add_appearances(item, [candidate])

    for group in group_candidates(unmatched):
        try:
            result = summarize_group(settings, group)
        except RadarError as exc:
            llm_errors += 1
            print(f"warning: item deferred: {group[0]['title']}: {exc}", file=sys.stderr)
            continue
        best = max(group, key=lambda value: len(value.get("description") or ""))
        dates = sorted(value["published_at"] for value in group if value.get("published_at"))
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        item = {
            "id": stable_id(group[0]["id"]),
            "status": "published" if result["include"] else "skipped",
            "title": result["title"],
            "source_title": best["title"],
            "summary": result["summary"],
            "reason": result["reason"],
            "published_at": dates[0] if dates else None,
            "first_seen_at": now,
            "appearances": group,
            "links": source_links(group),
        }
        archive["items"].append(item)
        new_items += 1
        if result["include"]:
            published += 1
        else:
            skipped += 1

    save_archive(settings.archive_path, archive)
    build_site(settings, archive)
    stats = {
        "sources": len(settings.sources),
        "source_errors": source_errors,
        "new_appearances": len(collected),
        "matched_appearances": matched,
        "new_items": new_items,
        "published": published,
        "skipped": skipped,
        "llm_errors": llm_errors,
    }
    return stats


def is_recent(value: str | None, cutoff: dt.datetime) -> bool:
    if not value:
        return True
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed >= cutoff


def build_site(settings: Settings, archive: dict[str, Any] | None = None) -> dict[str, int]:
    archive = archive or load_archive(settings.archive_path)
    items = sorted(
        (item for item in archive["items"] if item.get("status") == "published"),
        key=lambda value: value.get("published_at") or "",
        reverse=True,
    )
    settings.public_dir.mkdir(parents=True, exist_ok=True)
    (settings.public_dir / "index.html").write_text(render_html(settings, items), encoding="utf-8")
    (settings.public_dir / "feed.xml").write_text(render_rss(settings, items), encoding="utf-8")
    (settings.public_dir / "_headers").write_text(
        "/feed.xml\n  Content-Type: application/rss+xml; charset=utf-8\n",
        encoding="utf-8",
    )
    return {"items": len(items), "rss_items": len(items)}


def render_html(settings: Settings, items: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in items:
        links = render_links(item.get("links", {}), separator=" · ")
        date = date_label(item.get("published_at"))
        summary = html.escape(str(item.get("summary") or ""))
        rows.append(
            "<li>"
            f'<time datetime="{html.escape(str(item.get("published_at") or ""))}">{html.escape(date)}</time>'
            f" — {html.escape(str(item['title']))}"
            f"{(' — ' + links) if links else ''}"
            f"<br>{summary}"
            "</li>"
        )
    body = "\n".join(rows) or "<li>No episodes yet.</li>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(settings.title)}</title>
  <meta name="description" content="{html.escape(settings.description, quote=True)}">
  <link rel="alternate" type="application/rss+xml" title="{html.escape(settings.title, quote=True)}" href="/feed.xml">
</head>
<body>
  <header>
    <h1>{html.escape(settings.title)}</h1>
    <p>{html.escape(settings.description)}</p>
    <p><a href="/feed.xml">RSS feed</a></p>
  </header>
  <main>
    <ul>
      {body}
    </ul>
  </main>
</body>
</html>
"""


def render_links(links: dict[str, str], *, separator: str) -> str:
    values: list[str] = []
    if links.get("podcast"):
        values.append(
            f'<a href="{html.escape(links["podcast"], quote=True)}" rel="noopener noreferrer">Podcast</a>'
        )
    if links.get("youtube"):
        values.append(
            f'<a href="{html.escape(links["youtube"], quote=True)}" rel="noopener noreferrer">YouTube</a>'
        )
    return separator.join(values)


def render_rss(settings: Settings, items: list[dict[str, Any]]) -> str:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = settings.title
    ET.SubElement(channel, "link").text = settings.base_url + "/"
    ET.SubElement(channel, "description").text = settings.description
    ET.SubElement(channel, "language").text = "en-us"
    for item in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = str(item["title"])
        links = item.get("links", {})
        primary_link = links.get("podcast") or links.get("youtube") or settings.base_url + "/"
        ET.SubElement(node, "link").text = primary_link
        guid = ET.SubElement(node, "guid", {"isPermaLink": "false"})
        guid.text = "ai-radar:" + str(item["id"])
        published_at = item.get("published_at")
        if published_at:
            try:
                value = dt.datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
                ET.SubElement(node, "pubDate").text = email.utils.format_datetime(value)
            except ValueError:
                pass
        description_parts = [str(item.get("summary") or "")]
        source_html = render_links(links, separator=" | ")
        if source_html:
            description_parts.extend(["<br><br>", source_html])
        ET.SubElement(node, "description").text = "".join(description_parts)
    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"


def date_label(value: str | None) -> str:
    if not value:
        return "Unknown date"
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10]


def print_stats(stats: dict[str, int]) -> None:
    for key, value in stats.items():
        print(f"{key}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-radar")
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    parser.add_argument("--archive", type=pathlib.Path, default=DEFAULT_ARCHIVE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Fetch new episodes, summarize them, and rebuild the site.")
    run_parser.add_argument("--lookback-days", type=int, default=7)
    subparsers.add_parser("build-site", help="Regenerate static HTML and RSS from the archive.")
    subparsers.add_parser("doctor", help="Validate configuration and tracked archive state.")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config, args.archive)
        if args.command == "doctor":
            archive = load_archive(settings.archive_path)
            print(f"archive={settings.archive_path}")
            print(f"public_dir={settings.public_dir}")
            print(f"sources={len(settings.sources)}")
            print(f"podcast_sources={sum(source.kind == 'podcast' for source in settings.sources)}")
            print(f"youtube_sources={sum(source.kind == 'youtube' for source in settings.sources)}")
            print(f"archive_items={len(archive['items'])}")
            if settings.llm.api_key_env and not os.environ.get(settings.llm.api_key_env):
                print(f"warning: {settings.llm.api_key_env} is not set; run will defer new items")
            return 0
        if args.command == "build-site":
            print_stats(build_site(settings))
            return 0
        if args.command == "run":
            if args.lookback_days <= 0:
                raise RadarError("--lookback-days must be greater than zero")
            print_stats(run_cycle(settings, lookback_days=args.lookback_days))
            return 0
    except (RadarError, OSError, ValueError, ET.ParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
