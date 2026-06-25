from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

_ABBREVIATIONS = {
    "a.i",
    "dr",
    "e.g",
    "i.e",
    "mr",
    "mrs",
    "ms",
    "prof",
    "u.k",
    "u.s",
    "vs",
}


class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._chunks.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "h1", "h2", "h3"}:
            self._chunks.append("\n")

    def text(self) -> str:
        return "\n".join(self._chunks)


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    parser = _HTMLTextParser()
    parser.feed(value)
    return clean_text(html.unescape(parser.text()))


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max_chars - 20].rstrip() + "\n\n[truncated]"


def slugify(value: str, fallback: str = "episode") -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value.lower()).strip("-")
    return slug or fallback


def escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def paragraphs_to_html(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    blocks = re.split(r"\n\s*\n", value)
    rendered: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if lines and all(line.startswith(("- ", "* ")) for line in lines):
            items = "".join(f"<li>{escape(line[2:].strip())}</li>" for line in lines)
            rendered.append(f"<ul>{items}</ul>")
        else:
            rendered.append(f"<p>{escape(' '.join(lines))}</p>")
    return "\n".join(rendered)


def transcript_to_html(value: str) -> str:
    return "\n".join(f'<p class="transcript-sentence">{escape(sentence)}</p>' for sentence in split_sentences(value))


def split_sentences(value: str) -> list[str]:
    text = re.sub(r"\s+", " ", clean_text(value)).strip()
    if not text:
        return []

    sentences: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in ".!?":
            index += 1
            continue

        end = index + 1
        while end < len(text) and text[end] in ".!?":
            end += 1
        while end < len(text) and text[end] in "\"')]}’”":
            end += 1

        next_index = end
        while next_index < len(text) and text[next_index].isspace():
            next_index += 1

        if next_index == len(text) or next_index > end:
            sentence = text[start:end].strip()
            if not _ends_with_abbreviation(sentence) or next_index == len(text):
                sentences.append(sentence)
                start = next_index
                index = next_index
                continue

        index = end

    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _ends_with_abbreviation(value: str) -> bool:
    normalized = value.lower().rstrip("\"')]}’”")
    match = re.search(r"(?:^|\s)([a-z](?:[a-z.]*)?)\.$", normalized)
    return bool(match and match.group(1) in _ABBREVIATIONS)


def comma_join(values: list[str]) -> str:
    return ", ".join(value for value in values if value)
