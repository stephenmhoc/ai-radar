from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from .config import Config, FeedConfig
from . import storage
from .text import clean_text, strip_html


@dataclass(frozen=True)
class ParsedFeed:
    title: str
    homepage_url: str | None
    image_url: str | None
    episodes: list[dict[str, Any]]


def fetch_feed(url: str, user_agent: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_feed(xml_bytes: bytes, fallback_name: str, hosts: tuple[str, ...] = ()) -> ParsedFeed:
    root = ET.fromstring(xml_bytes)
    if _local(root.tag) == "feed":
        return _parse_atom(root, fallback_name, hosts)
    channel = _first(root, "channel") or root
    title = _text(channel, "title") or fallback_name
    homepage_url = _text(channel, "link")
    image_url = _feed_image(channel)
    episodes: list[dict[str, Any]] = []
    for item in _children(channel, "item"):
        episode = _parse_rss_item(item, image_url, hosts)
        if episode:
            episodes.append(episode)
    return ParsedFeed(title=title, homepage_url=homepage_url, image_url=image_url, episodes=episodes)


def ingest(config: Config, conn, *, limit_per_feed: int | None = None) -> dict[str, int]:
    stats = {"feeds": 0, "feed_errors": 0, "episodes_seen": 0, "episodes_inserted": 0}
    for feed in config.active_feeds:
        feed_id = storage.upsert_feed(conn, feed)
        try:
            parsed = parse_feed(fetch_feed(feed.url, config.app.user_agent), feed.name, feed.hosts)
        except Exception as exc:  # noqa: BLE001 - continue monitoring other feeds
            stats["feed_errors"] += 1
            print(f"warning: feed failed: {feed.name}: {exc}", file=sys.stderr)
            conn.commit()
            continue
        storage.update_feed_metadata(
            conn,
            feed_id,
            image_url=parsed.image_url,
            homepage_url=parsed.homepage_url,
        )
        stats["feeds"] += 1
        for episode in parsed.episodes[:limit_per_feed]:
            episode["hosts"] = list(feed.hosts)
            _, inserted = storage.upsert_episode(conn, feed_id, episode)
            stats["episodes_seen"] += 1
            if inserted:
                stats["episodes_inserted"] += 1
        conn.commit()
    return stats


def _parse_rss_item(item: ET.Element, feed_image_url: str | None, hosts: tuple[str, ...]) -> dict[str, Any] | None:
    title = clean_text(_text(item, "title"))
    if not title:
        return None
    description_html = _text(item, "description") or _text(item, "summary") or _text(item, "encoded")
    description = strip_html(description_html)
    episode_url = _text(item, "link")
    guid = _text(item, "guid") or episode_url or _stable_guid(title, _text(item, "pubDate"))
    audio_url, audio_type = _enclosure(item)
    image_url = _item_image(item) or feed_image_url
    published_at = _parse_date(_text(item, "pubDate") or _text(item, "published"))
    return {
        "guid": guid,
        "title": title,
        "description": description,
        "episode_url": episode_url,
        "audio_url": audio_url,
        "audio_type": audio_type,
        "image_url": image_url,
        "published_at": published_at,
        "duration": _text(item, "duration"),
        "hosts": list(hosts),
        "raw": {
            "guid": guid,
            "title": title,
            "episode_url": episode_url,
            "audio_url": audio_url,
            "published_at": published_at,
        },
    }


def _parse_atom(root: ET.Element, fallback_name: str, hosts: tuple[str, ...]) -> ParsedFeed:
    title = _text(root, "title") or fallback_name
    homepage_url = _atom_link(root, rel="alternate") or _atom_link(root, rel=None)
    image_url = _text(root, "logo") or _text(root, "icon")
    episodes: list[dict[str, Any]] = []
    for entry in _children(root, "entry"):
        entry_title = clean_text(_text(entry, "title"))
        if not entry_title:
            continue
        description_html = _text(entry, "summary") or _text(entry, "content")
        episode_url = _atom_link(entry, rel="alternate") or _atom_link(entry, rel=None)
        audio_url = _atom_link(entry, rel="enclosure")
        guid = _text(entry, "id") or episode_url or _stable_guid(entry_title, _text(entry, "published"))
        episodes.append(
            {
                "guid": guid,
                "title": entry_title,
                "description": strip_html(description_html),
                "episode_url": episode_url,
                "audio_url": audio_url,
                "audio_type": None,
                "image_url": _item_image(entry) or image_url,
                "published_at": _parse_date(_text(entry, "published") or _text(entry, "updated")),
                "duration": _text(entry, "duration"),
                "hosts": list(hosts),
                "raw": {
                    "guid": guid,
                    "title": entry_title,
                    "episode_url": episode_url,
                    "audio_url": audio_url,
                },
            }
        )
    return ParsedFeed(title=title, homepage_url=homepage_url, image_url=image_url, episodes=episodes)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) == name]


def _first(element: ET.Element, name: str) -> ET.Element | None:
    for child in list(element):
        if _local(child.tag) == name:
            return child
    return None


def _text(element: ET.Element, name: str) -> str | None:
    child = _first(element, name)
    if child is not None and child.text:
        return clean_text(child.text)
    for descendant in element.iter():
        if descendant is element:
            continue
        if _local(descendant.tag) == name and descendant.text:
            return clean_text(descendant.text)
    return None


def _feed_image(channel: ET.Element) -> str | None:
    image = _first(channel, "image")
    if image is not None:
        url = _text(image, "url")
        if url:
            return url
    return _item_image(channel)


def _item_image(element: ET.Element) -> str | None:
    for descendant in element.iter():
        local = _local(descendant.tag)
        if local in {"image", "thumbnail", "content"}:
            href = descendant.attrib.get("href") or descendant.attrib.get("url")
            if href and _looks_like_image(href):
                return href
    return None


def _enclosure(item: ET.Element) -> tuple[str | None, str | None]:
    for descendant in item.iter():
        if _local(descendant.tag) != "enclosure":
            continue
        url = descendant.attrib.get("url")
        media_type = descendant.attrib.get("type")
        if url and (not media_type or media_type.startswith("audio/")):
            return url, media_type
    for descendant in item.iter():
        if _local(descendant.tag) in {"content", "link"}:
            url = descendant.attrib.get("url") or descendant.attrib.get("href")
            media_type = descendant.attrib.get("type")
            if url and (media_type or "").startswith("audio/"):
                return url, media_type
    return None, None


def _atom_link(element: ET.Element, rel: str | None) -> str | None:
    for child in _children(element, "link"):
        child_rel = child.attrib.get("rel")
        if rel is None or child_rel == rel:
            return child.attrib.get("href")
    return None


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _stable_guid(title: str, published: str | None) -> str:
    value = f"{title}|{published or ''}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _looks_like_image(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in (".jpg", ".jpeg", ".png", ".webp", ".gif", "image"))
