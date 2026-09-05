"""Shared publisher types, text handling, media identity, and bounded reads."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import pathlib
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

RETRYABLE_HTTP_CODES = frozenset({408, 409, 425, 429})



APPEARANCE_PRESENTATION = {
    "podcast": ("Podcast", "Episode"),
    "youtube": ("YouTube", "Video"),
    "newsletter": ("Newsletter", "Issue"),
}



MAX_LLM_RESPONSE_BYTES = 1024 * 1024



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
    max_output_tokens: int



@dataclass(frozen=True)
class Settings:
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



class LLMTruncationError(RadarError):
    """The model stopped at the output-token cap, so retrying cannot help."""



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



def escape_public_text(value: str) -> str:
    # Cloudflare's email-obfuscation feature injects a JavaScript decoder when
    # prose happens to contain an address. A zero-width break keeps the visible
    # text intact while preserving this site's script-free contract.
    return html.escape(value).replace("@", "&#8203;@")



def source_family(name: str) -> str:
    return (
        re.sub(r"\s*[—-]\s*(?:Newsletter|YouTube)\s*$", "", name, flags=re.IGNORECASE)
        .strip()
        .casefold()
    )



def source_display_name(appearance_value: dict[str, Any]) -> str:
    name = clean_text(str(appearance_value.get("source") or ""))
    kind = str(appearance_value.get("kind") or "")
    if kind in {"newsletter", "youtube"}:
        name = re.sub(
            rf"\s*[—-]\s*{re.escape(APPEARANCE_PRESENTATION[kind][0])}\s*$",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip()
    return name



def ordered_appearances(item: dict[str, Any]) -> list[dict[str, Any]]:
    order = {kind: index for index, kind in enumerate(APPEARANCE_PRESENTATION)}
    values = [
        value
        for value in item.get("appearances", [])
        if isinstance(value, dict) and public_http_url(value.get("url"))
    ]
    return sorted(
        values,
        key=lambda value: (
            order.get(str(value.get("kind") or ""), len(order)),
            source_display_name(value).casefold(),
            clean_text(str(value.get("title") or "")).casefold(),
        ),
    )



def public_http_url(value: object) -> str | None:
    url = clean_text(str(value or ""))
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return url



def require_public_http_url(value: object, *, label: str) -> str:
    url = public_http_url(value)
    if url is None:
        raise RadarError(f"{label} must be an HTTP(S) URL")
    return url



def parse_timestamp(value: object) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp exactly as written, or return None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None



def parse_utc_timestamp(value: object) -> dt.datetime | None:
    """Parse a timestamp and normalize it to an aware UTC instant."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)



def _read_bounded(response: Any, *, max_bytes: int, label: str) -> bytes:
    content_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise RadarError(f"{label} exceeded {max_bytes} bytes")
        except ValueError:
            pass
    value = response.read(max_bytes + 1)
    if len(value) > max_bytes:
        raise RadarError(f"{label} exceeded {max_bytes} bytes")
    return value



def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]



def youtube_video_id(appearance_value: dict[str, Any]) -> str:
    url = str(appearance_value.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    query_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        path_id = parsed.path.strip("/").split("/", 1)[0]
        if path_id:
            return path_id
    path_match = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]+)", parsed.path)
    if path_match:
        return path_match.group(1)
    guid = str(appearance_value.get("guid") or "")
    guid_match = re.search(r"(?:yt:video:)?([A-Za-z0-9_-]{6,})$", guid)
    return guid_match.group(1) if guid_match else guid



def is_youtube_short(appearance_value: dict[str, Any]) -> bool:
    if appearance_value.get("kind") != "youtube":
        return False
    path = urllib.parse.urlparse(str(appearance_value.get("url") or "")).path
    return re.search(r"(?:^|/)shorts(?:/|$)", path, flags=re.IGNORECASE) is not None



def media_identity(appearance_value: dict[str, Any]) -> tuple[str, str, str]:
    kind = str(appearance_value.get("kind") or "")
    if kind == "youtube":
        return (kind, "", youtube_video_id(appearance_value))
    return (
        kind,
        str(appearance_value.get("family") or appearance_value.get("source") or "").casefold(),
        str(appearance_value.get("guid") or ""),
    )
