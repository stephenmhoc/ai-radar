from __future__ import annotations

import datetime as dt
import dataclasses
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Any

from . import dates, feeds, net, storage
from .config import Config, SourceConfig
from .text import clean_text, strip_html


def collect(
    config: Config,
    conn,
    *,
    limit_per_source: int | None = None,
    published_since: str | None = None,
    source_names: tuple[str, ...] = (),
) -> dict[str, int]:
    stats = {
        "sources": 0,
        "source_errors": 0,
        "items_seen": 0,
        "items_before_since": 0,
        "appearances_inserted": 0,
    }
    since = dates.parse_cutoff(published_since or config.app.processed_after)
    selected_names = set(source_names)
    for source in config.active_sources:
        if selected_names and source.name not in selected_names:
            continue
        source_id = storage.upsert_source(conn, source)
        try:
            appearances, metadata = collect_source(config, conn, source_id, source, since=since)
        except Exception as exc:  # noqa: BLE001 - one platform must not block the others
            stats["source_errors"] += 1
            storage.update_source_metadata(conn, source_id, error=str(exc))
            conn.commit()
            print(f"warning: source failed: {source.name} ({source.kind}): {exc}", file=sys.stderr)
            continue
        storage.update_source_metadata(
            conn,
            source_id,
            image_url=metadata.get("image_url"),
            homepage_url=metadata.get("homepage_url"),
            cursor=metadata.get("cursor"),
        )
        stats["sources"] += 1
        kept = 0
        for appearance in appearances:
            if since is not None and not feeds.entry_is_since(appearance, since):
                stats["items_before_since"] += 1
                continue
            _, inserted = storage.upsert_appearance(conn, source_id, appearance)
            stats["items_seen"] += 1
            if inserted:
                stats["appearances_inserted"] += 1
            kept += 1
            if limit_per_source is not None and kept >= limit_per_source:
                break
        conn.commit()
    # Preserve the old stat names in logs and scripts while exposing generic names.
    stats.update(
        {
            "feeds": stats["sources"],
            "feed_errors": stats["source_errors"],
            "episodes_seen": stats["items_seen"],
            "episodes_before_since": stats["items_before_since"],
            "episodes_inserted": stats["appearances_inserted"],
        }
    )
    return stats


def collect_source(
    config: Config,
    conn,
    source_id: int,
    source: SourceConfig,
    *,
    since: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    if source.kind == "blog" and not source.feed_url:
        return _collect_blog_index(config, source, since=since)
    if source.kind in {"podcast", "blog"}:
        return _collect_rss(config, source, since=since)
    if source.kind == "youtube":
        return _collect_youtube(config, source, since=since, conn=conn)
    if source.kind == "x":
        row = conn.execute("SELECT cursor FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _collect_x(config, source, cursor=str(row["cursor"] or "") if row else "")
    raise ValueError(f"unsupported source kind: {source.kind}")


def _collect_rss(
    config: Config,
    source: SourceConfig,
    *,
    since: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    feed_url = source.feed_url or source.url
    parsed = feeds.parse_feed(
        feeds.fetch_feed(feed_url, config.app.user_agent),
        source.name,
        source.hosts,
    )
    appearances: list[dict[str, Any]] = []
    for entry in parsed.episodes:
        appearance = {
            **entry,
            "external_id": entry["guid"],
            "url": entry.get("episode_url"),
            "media_url": entry.get("audio_url"),
            "media_type": entry.get("audio_type"),
            "authors": list(source.people or source.hosts or tuple(entry.get("authors", []))),
        }
        if source.kind == "blog":
            appearance["media_url"] = None
            appearance["media_type"] = "text/html"
            # Keep old entries in the result so the caller's cutoff accounting
            # remains accurate, but do not re-download their article pages.
            if since is None or feeds.entry_is_since(appearance, since):
                appearance["content_text"] = _article_text(
                    str(entry.get("episode_url") or ""),
                    fallback=str(entry.get("description") or ""),
                    user_agent=config.app.user_agent,
                )
                if str(appearance.get("title") or "").strip().casefold() in {"", "-", "untitled"}:
                    appearance["title"] = _derived_title(str(appearance["content_text"]), source.name)
        appearances.append(appearance)
    return appearances, {
        "image_url": parsed.image_url,
        "homepage_url": parsed.homepage_url,
        "cursor": None,
    }


def _collect_blog_index(
    config: Config,
    source: SourceConfig,
    *,
    since: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    """Collect a small author site that exposes article links but no RSS feed."""
    homepage = _fetch_html(source.url, user_agent=config.app.user_agent)
    index = _BlogHTMLParser()
    index.feed(homepage)
    appearances: list[dict[str, Any]] = []
    for url in index.article_urls(source.url):
        try:
            body = _fetch_html(url, user_agent=config.app.user_agent)
        except Exception:  # noqa: BLE001 - one unavailable article should not block the source
            continue
        metadata = _BlogHTMLParser()
        metadata.feed(body)
        article = _ArticleParser()
        article.feed(body)
        content = article.text()
        if not content:
            continue
        title = metadata.page_title() or _derived_title(content, source.name)
        published_at = metadata.published_at(body)
        appearance = {
            "external_id": url,
            "title": title,
            "description": content[:1000],
            "url": url,
            "media_url": None,
            "media_type": "text/html",
            "published_at": published_at,
            "authors": list(source.people or (source.name,)),
            "content_text": content,
            "raw": {"article_url": url, "index_url": source.url},
        }
        appearances.append(appearance)
    return appearances, {
        "image_url": index.image_url,
        "homepage_url": source.url,
        "cursor": appearances[0]["external_id"] if appearances else None,
    }


def _collect_youtube(
    config: Config,
    source: SourceConfig,
    *,
    since: dt.datetime | None = None,
    conn=None,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    if source.playlist_url:
        return _collect_youtube_playlist(config, source, since=since, conn=conn)
    if not source.external_id:
        raise ValueError(f"YouTube source requires external_id channel ID: {source.name}")
    env_name = source.api_key_env or "YOUTUBE_API_KEY"
    api_key = os.environ.get(env_name, "")
    if not api_key:
        if source.feed_url:
            return _collect_youtube_feed(config, source)
        raise ValueError(f"missing {env_name} for {source.name}; configure feed_url for public-feed collection")
    channel_response = _get_json(
        "https://www.googleapis.com/youtube/v3/channels",
        {
            "part": "snippet,contentDetails",
            "id": source.external_id,
            "key": api_key,
        },
        config.app.user_agent,
    )
    channels = channel_response.get("items", [])
    if not channels:
        raise ValueError(f"YouTube channel not found: {source.external_id}")
    channel = channels[0]
    uploads = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads:
        raise ValueError(f"YouTube channel has no uploads playlist: {source.name}")
    playlist = _get_json(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        {
            "part": "snippet,contentDetails",
            "playlistId": uploads,
            "maxResults": "50",
            "key": api_key,
        },
        config.app.user_agent,
    )
    video_ids = [
        str(item.get("contentDetails", {}).get("videoId") or "")
        for item in playlist.get("items", [])
    ]
    video_ids = [video_id for video_id in video_ids if video_id]
    videos: dict[str, dict[str, Any]] = {}
    if video_ids:
        response = _get_json(
            "https://www.googleapis.com/youtube/v3/videos",
            {
                "part": "snippet,contentDetails,status",
                "id": ",".join(video_ids),
                "key": api_key,
            },
            config.app.user_agent,
        )
        videos = {str(item["id"]): item for item in response.get("items", [])}

    appearances: list[dict[str, Any]] = []
    for video_id in video_ids:
        video = videos.get(video_id)
        if not video or video.get("status", {}).get("privacyStatus") != "public":
            continue
        snippet = video.get("snippet", {})
        live_status = snippet.get("liveBroadcastContent")
        if live_status == "upcoming":
            continue
        duration = _youtube_duration(video.get("contentDetails", {}).get("duration"))
        title = clean_text(str(snippet.get("title") or ""))
        if not title:
            continue
        url = f"https://www.youtube.com/watch?v={video_id}"
        appearances.append(
            {
                "external_id": video_id,
                "title": title,
                "description": clean_text(str(snippet.get("description") or "")),
                "url": url,
                "media_url": url,
                "media_type": "video/youtube",
                "image_url": _youtube_thumbnail(snippet),
                "published_at": _iso_datetime(snippet.get("publishedAt")),
                "duration": duration,
                "authors": list(source.people or (source.name,)),
                "raw": {"video_id": video_id, "channel_id": source.external_id},
            }
        )
    snippet = channel.get("snippet", {})
    return appearances, {
        "image_url": _youtube_thumbnail(snippet),
        "homepage_url": source.url,
        "cursor": video_ids[0] if video_ids else None,
    }


def _collect_youtube_playlist(
    config: Config,
    source: SourceConfig,
    *,
    since: dt.datetime | None = None,
    conn=None,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    executable = shutil.which("yt-dlp")
    if not executable:
        raise RuntimeError(f"yt-dlp is required for YouTube playlist collection: {source.name}")
    command = [
        executable,
        "--flat-playlist",
        "--dump-json",
        "--skip-download",
        "--ignore-errors",
        "--no-warnings",
        "--playlist-end",
        "500",
    ]
    command.append(source.playlist_url)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    playlist_entries: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            video = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = str(video.get("id") or "").strip()
        title = clean_text(str(video.get("title") or ""))
        if not video_id or not title or video.get("live_status") == "is_upcoming":
            continue
        playlist_entries.append(video)
    if result.returncode and not playlist_entries:
        reason = clean_text(result.stderr)[-500:] or f"yt-dlp exited with {result.returncode}"
        raise RuntimeError(f"YouTube playlist collection failed for {source.name}: {reason}")

    recent_by_id: dict[str, dict[str, Any]] = {}
    if source.external_id:
        feed_source = dataclasses.replace(
            source,
            feed_url=f"https://www.youtube.com/feeds/videos.xml?channel_id={source.external_id}",
        )
        try:
            recent, _ = _collect_youtube_feed(config, feed_source)
            recent_by_id = {str(item["external_id"]): item for item in recent}
        except Exception as exc:  # noqa: BLE001 - playlist history remains usable
            print(f"warning: recent YouTube feed failed: {source.name}: {exc}", file=sys.stderr)

    appearances: list[dict[str, Any]] = []
    for video in playlist_entries:
        video_id = str(video["id"])
        title = clean_text(str(video.get("title") or ""))
        recent = recent_by_id.get(video_id)
        if recent is not None:
            match = (
                _matching_podcast_appearance(
                    conn,
                    source,
                    video,
                    published_at=recent.get("published_at"),
                )
                if conn is not None
                else None
            )
            match_fields = (
                {
                    "canonical_item_id": int(match["item_id"]),
                    "raw": {
                        **dict(recent.get("raw", {})),
                        "playlist_id": str(video.get("playlist_id") or ""),
                        "playlist_url": source.playlist_url,
                        "podcast_url": str(match["url"] or ""),
                        "podcast_item_id": int(match["item_id"]),
                    },
                }
                if match is not None
                else {
                    "raw": {
                        **dict(recent.get("raw", {})),
                        "playlist_id": str(video.get("playlist_id") or ""),
                        "playlist_url": source.playlist_url,
                    }
                }
            )
            appearances.append(
                {
                    **recent,
                    "duration": _duration_string(video.get("duration")) or recent.get("duration"),
                    **match_fields,
                }
            )
            continue

        match = _matching_podcast_appearance(conn, source, video) if conn is not None else None
        if match is None:
            continue
        url = str(video.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}")
        appearances.append(
            {
                "external_id": video_id,
                "title": title,
                "description": clean_text(str(video.get("description") or "")),
                "url": url,
                "media_url": url,
                "media_type": "video/youtube",
                "image_url": _yt_dlp_thumbnail(video),
                "published_at": str(match["published_at"] or "") or None,
                "duration": _duration_string(video.get("duration")),
                "authors": list(source.people or (str(video.get("channel") or source.name),)),
                "raw": {
                    "video_id": video_id,
                    "channel_id": str(video.get("channel_id") or source.external_id),
                    "playlist_id": str(video.get("playlist_id") or ""),
                    "playlist_url": source.playlist_url,
                    "podcast_url": str(match["url"] or ""),
                    "podcast_item_id": int(match["item_id"]),
                },
                "canonical_item_id": int(match["item_id"]),
            }
        )
    return appearances, {
        "image_url": appearances[0].get("image_url") if appearances else None,
        "homepage_url": source.url,
        "cursor": appearances[0]["external_id"] if appearances else None,
    }


def _matching_podcast_appearance(
    conn,
    source: SourceConfig,
    video: dict[str, Any],
    *,
    published_at: Any = None,
):
    base_name = source.name.removesuffix(" — YouTube")
    rows = conn.execute(
        """
        SELECT appearances.item_id, appearances.title, appearances.published_at,
               appearances.duration, appearances.url
        FROM appearances
        JOIN sources ON sources.id = appearances.source_id
        WHERE sources.kind = 'podcast'
          AND (sources.name = ? OR sources.name LIKE ?)
        ORDER BY appearances.published_at DESC
        """,
        (base_name, f"{base_name}%"),
    ).fetchall()
    video_title = _normalized_match_title(str(video.get("title") or ""))
    video_duration = dates.duration_seconds(video.get("duration"))
    best = None
    best_score = 0.0
    for row in rows:
        podcast_title = _normalized_match_title(str(row["title"] or ""))
        if not video_title or not podcast_title:
            continue
        title_score = SequenceMatcher(None, video_title, podcast_title).ratio()
        duration_close = dates.durations_close(video_duration, row["duration"])
        date_close = dates.datetimes_close(published_at, row["published_at"], days=4)
        token_score, shared_tokens = _title_token_score(video_title, podcast_title)
        if duration_close and date_close:
            score = 1.1
        elif video_title == podcast_title:
            score = 1.0 if duration_close else 0.9
        elif duration_close and title_score >= 0.78:
            score = title_score
        elif duration_close and shared_tokens >= 2 and token_score >= 0.3:
            score = token_score
        else:
            continue
        if score > best_score:
            best = row
            best_score = score
    return best


def _normalized_match_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _title_token_score(first: str, second: str) -> tuple[float, int]:
    ignored = {
        "a", "an", "and", "at", "for", "from", "in", "of", "on", "or", "the", "to", "with",
        "episode", "ep", "full", "podcast", "show", "video",
    }
    left = {token for token in first.split() if token not in ignored and len(token) > 1}
    right = {token for token in second.split() if token not in ignored and len(token) > 1}
    shared = len(left & right)
    if not left or not right:
        return 0.0, shared
    return shared / min(len(left), len(right)), shared


def _collect_youtube_feed(
    config: Config,
    source: SourceConfig,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    parsed = feeds.parse_feed(
        feeds.fetch_feed(source.feed_url, config.app.user_agent),
        source.name,
    )
    appearances: list[dict[str, Any]] = []
    for entry in parsed.episodes:
        url = str(entry.get("episode_url") or "")
        if urllib.parse.urlparse(url).path.startswith("/shorts/"):
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        video_id = str((query.get("v") or [entry.get("guid") or url])[0])
        appearances.append(
            {
                **entry,
                "external_id": video_id,
                "url": url,
                "media_url": url,
                "media_type": "video/youtube",
                "authors": list(source.people or (source.name,)),
                "raw": {"video_id": video_id, "channel_id": source.external_id},
            }
        )
    return appearances, {
        "image_url": parsed.image_url,
        "homepage_url": parsed.homepage_url or source.url,
        "cursor": appearances[0]["external_id"] if appearances else None,
    }


def _collect_x(
    config: Config,
    source: SourceConfig,
    *,
    cursor: str,
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    token = _required_secret(source, default_env="X_BEARER_TOKEN")
    if not source.external_id:
        raise ValueError(f"X source requires external_id user ID: {source.name}")
    params = {
        "max_results": "100",
        "exclude": "retweets",
        "tweet.fields": "article,author_id,conversation_id,created_at,entities,note_tweet,public_metrics,referenced_tweets,text",
    }
    if cursor:
        params["since_id"] = cursor
    response = _get_json(
        f"https://api.x.com/2/users/{urllib.parse.quote(source.external_id)}/tweets",
        params,
        config.app.user_agent,
        bearer_token=token,
    )
    posts = [post for post in response.get("data", []) if str(post.get("author_id")) == source.external_id]
    post_ids = {str(post.get("id")) for post in posts}
    roots: dict[str, list[dict[str, Any]]] = {}
    for post in posts:
        post_id = str(post.get("id") or "")
        conversation_id = str(post.get("conversation_id") or post_id)
        is_reply = any(ref.get("type") == "replied_to" for ref in post.get("referenced_tweets", []))
        if is_reply and conversation_id not in post_ids:
            continue
        roots.setdefault(conversation_id, []).append(post)

    appearances: list[dict[str, Any]] = []
    for conversation_id, thread in roots.items():
        thread.sort(key=lambda post: (str(post.get("created_at") or ""), int(str(post.get("id") or "0"))))
        root = next((post for post in thread if str(post.get("id")) == conversation_id), thread[0])
        parts = [_x_post_text(post) for post in thread]
        content = "\n\n".join(part for part in parts if part)
        if not _substantial_x_item(content, thread):
            continue
        post_id = str(root.get("id"))
        url = f"{source.url.rstrip('/')}/status/{post_id}"
        title = _x_title(content, source.name)
        appearances.append(
            {
                "external_id": conversation_id,
                "title": title,
                "description": content[:1000],
                "url": url,
                "media_type": "text/x-thread",
                "published_at": _iso_datetime(root.get("created_at")),
                "authors": list(source.people or (source.name,)),
                "content_text": content,
                "raw": {
                    "conversation_id": conversation_id,
                    "post_ids": [str(post.get("id")) for post in thread],
                    "post_count": len(thread),
                },
            }
        )
    newest_id = max((int(str(post.get("id") or "0")) for post in posts), default=0)
    return appearances, {
        "image_url": None,
        "homepage_url": source.url,
        "cursor": str(newest_id) if newest_id else cursor or None,
    }


def _required_secret(source: SourceConfig, *, default_env: str) -> str:
    env_name = source.api_key_env or default_env
    value = os.environ.get(env_name, "")
    if not value:
        raise ValueError(f"missing {env_name} for {source.name}")
    return value


def _get_json(
    url: str,
    params: dict[str, str],
    user_agent: str,
    *,
    bearer_token: str = "",
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    headers = {"User-Agent": user_agent}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = urllib.request.Request(f"{url}?{query}", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _article_text(url: str, *, fallback: str, user_agent: str) -> str:
    if not url:
        return clean_text(strip_html(fallback))
    try:
        body = _fetch_html(url, user_agent=user_agent)
    except Exception:  # noqa: BLE001 - RSS text is a valid fallback
        return clean_text(strip_html(fallback))
    parser = _ArticleParser()
    parser.feed(body)
    extracted = parser.text()
    return extracted if len(extracted) >= len(clean_text(strip_html(fallback))) else clean_text(strip_html(fallback))


def _fetch_html(url: str, *, user_agent: str) -> str:
    return net.fetch_text(url, user_agent=user_agent, purpose="article fetch")


class _BlogHTMLParser(HTMLParser):
    ARTICLE_PATHS = ("/essay/", "/post/", "/blog/")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.meta: dict[str, str] = {}
        self.image_url: str | None = None
        self._in_title = False
        self._title: list[str] = []
        self._time_depth = 0
        self._time_text: list[str] = []
        self._time_datetime: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            content = values.get("content", "").strip()
            if key and content:
                self.meta[key] = content
                if key in {"og:image", "twitter:image"} and not self.image_url:
                    self.image_url = content
        elif tag == "time":
            self._time_depth += 1
            self._time_datetime = self._time_datetime or values.get("datetime") or None

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "time" and self._time_depth:
            self._time_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        if self._time_depth:
            self._time_text.append(data)

    def article_urls(self, base_url: str) -> list[str]:
        base = urllib.parse.urlparse(base_url)
        urls: list[str] = []
        for link in self.links:
            absolute = urllib.parse.urljoin(base_url, link)
            parsed = urllib.parse.urlparse(absolute)
            if parsed.netloc.casefold() != base.netloc.casefold():
                continue
            if not any(parsed.path.startswith(prefix) for prefix in self.ARTICLE_PATHS):
                continue
            normalized = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
            if normalized not in urls:
                urls.append(normalized)
        return urls

    def page_title(self) -> str:
        value = self.meta.get("og:title") or self.meta.get("twitter:title") or " ".join(self._title)
        value = re.sub(r"\s+", " ", clean_text(html.unescape(value))).strip()
        for separator in (" | ", " — ", " – "):
            if separator in value:
                value = value.split(separator)[-1].strip()
        return value

    def published_at(self, body: str) -> str | None:
        candidates = [
            self.meta.get("article:published_time", ""),
            self.meta.get("date", ""),
            self._time_datetime or "",
            clean_text(" ".join(self._time_text)),
        ]
        json_date = re.search(r'"datePublished"\s*:\s*"([^"]+)"', body, flags=re.IGNORECASE)
        if json_date:
            candidates.insert(0, json_date.group(1))
        visible = clean_text(strip_html(body))
        month_date = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"(?:\s+(\d{1,2})(?:st|nd|rd|th)?[,]?)?\s+(20\d{2})\b",
            visible,
            flags=re.IGNORECASE,
        )
        if month_date:
            candidates.append(month_date.group(0))
        for value in candidates:
            parsed = _parse_blog_date(value)
            if parsed:
                return parsed
        return None


def _parse_blog_date(value: str) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    iso = _iso_datetime(value)
    if iso != value or re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", value):
        return iso
    value = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", value, flags=re.IGNORECASE).replace(",", "")
    for pattern in ("%B %d %Y", "%B %Y"):
        try:
            parsed = dt.datetime.strptime(value, pattern).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        return parsed.isoformat()
    return None


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored = 0
        self._article = 0
        self._paragraph = 0
        self._current: list[str] = []
        self._article_paragraphs: list[str] = []
        self._fallback_paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "nav", "footer", "header", "aside", "noscript"}:
            self._ignored += 1
        if tag in {"article", "main"}:
            self._article += 1
        if tag in {"p", "h1", "h2", "h3", "li", "blockquote"} and not self._ignored:
            self._paragraph += 1
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "h1", "h2", "h3", "li", "blockquote"} and self._paragraph:
            value = clean_text(html.unescape(" ".join(self._current)))
            if len(value) >= 25:
                self._fallback_paragraphs.append(value)
                if self._article:
                    self._article_paragraphs.append(value)
            self._paragraph -= 1
            self._current = []
        if tag in {"article", "main"} and self._article:
            self._article -= 1
        if tag in {"script", "style", "nav", "footer", "header", "aside", "noscript"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if self._paragraph and not self._ignored:
            self._current.append(data)

    def text(self) -> str:
        paragraphs = self._article_paragraphs or self._fallback_paragraphs
        return "\n\n".join(dict.fromkeys(paragraphs))


def _youtube_thumbnail(snippet: dict[str, Any]) -> str | None:
    thumbnails = snippet.get("thumbnails", {})
    for name in ("maxres", "standard", "high", "medium", "default"):
        url = thumbnails.get(name, {}).get("url")
        if url:
            return str(url)
    return None


def _yt_dlp_thumbnail(video: dict[str, Any]) -> str | None:
    if video.get("thumbnail"):
        return str(video["thumbnail"])
    candidates = [item for item in video.get("thumbnails", []) if item.get("url")]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (int(item.get("width") or 0), int(item.get("height") or 0)))
    return str(candidates[-1]["url"])


def _duration_string(value: Any) -> str | None:
    try:
        total = int(float(value))
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _youtube_duration(value: Any) -> str | None:
    seconds = dates.duration_seconds(value)
    if seconds is None:
        return None
    return _duration_string(seconds)


def _iso_datetime(value: Any) -> str | None:
    if not value:
        return None
    parsed = dates.parse_datetime(value)
    if parsed is None:
        return str(value)
    return parsed.replace(microsecond=0).isoformat()


def _x_post_text(post: dict[str, Any]) -> str:
    note = post.get("note_tweet") or {}
    article = post.get("article") or {}
    article_title = str(article.get("title") or "").strip()
    text = str(note.get("text") or post.get("text") or "").strip()
    return "\n".join(part for part in (article_title, text) if part)


def _substantial_x_item(content: str, thread: list[dict[str, Any]]) -> bool:
    if len(content) >= 600:
        return True
    if len(thread) >= 3 and len(content) >= 350:
        return True
    return any(post.get("article") for post in thread) and len(content) >= 180


def _x_title(content: str, source_name: str) -> str:
    first = clean_text(content.splitlines()[0] if content else "")
    if len(first) > 100:
        first = first[:100].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return first or f"{source_name} on X"


def _derived_title(content: str, source_name: str) -> str:
    first = clean_text(content.splitlines()[0] if content else "")
    if len(first) > 110:
        first = first[:110].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return first or f"New writing from {source_name}"
