from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .config import Config
from . import storage
from .text import paragraphs_to_html, strip_html, truncate


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
        provider = self.config.llm.provider
        if provider == "openai_compatible":
            return self._openai_compatible(system=system, user=user)
        if provider == "ollama":
            return self._ollama(system=system, user=user)
        raise LLMError(f"unsupported llm.provider: {provider}")

    def _openai_compatible(self, *, system: str, user: str) -> dict[str, Any]:
        api_key = os.environ.get(self.config.llm.api_key_env, "")
        if self.config.llm.api_key_env and not api_key:
            raise LLMError(f"missing API key env var: {self.config.llm.api_key_env}")
        url = self.config.llm.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.llm.model,
            "temperature": self.config.llm.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return _request_json(url, payload, headers)

    def _ollama(self, *, system: str, user: str) -> dict[str, Any]:
        url = self.config.llm.base_url.rstrip("/") + "/api/chat"
        payload = {
            "model": self.config.llm.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": self.config.llm.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        response = _raw_request_json(url, payload, {"Content-Type": "application/json"})
        content = response.get("message", {}).get("content", "")
        return extract_json(content)


def judge_episode(config: Config, conn, episode) -> dict[str, Any]:
    prompt = build_judge_prompt(config, episode)
    client = LLMClient(config)
    response = client.chat_json(system=JUDGE_SYSTEM, user=prompt["user"])
    result = _normalize_judge(response, config)
    storage.add_decision(
        conn,
        int(episode["id"]),
        stage="judge",
        model=config.llm.model,
        prompt=prompt,
        response=result,
    )
    storage.set_judgement(conn, int(episode["id"]), result)
    conn.commit()
    return result


def summarize_episode(config: Config, conn, episode) -> dict[str, Any]:
    prompt = build_summary_prompt(config, episode)
    client = LLMClient(config)
    response = client.chat_json(system=SUMMARY_SYSTEM, user=prompt["user"])
    result = _normalize_summary(response)
    storage.add_decision(
        conn,
        int(episode["id"]),
        stage="summary",
        model=config.llm.model,
        prompt=prompt,
        response=result,
    )
    storage.set_summary(conn, int(episode["id"]), result)
    conn.commit()
    return result


def verify_transcript_episode(config: Config, conn, episode) -> dict[str, Any]:
    prompt = build_transcript_judge_prompt(config, episode)
    client = LLMClient(config)
    response = client.chat_json(system=JUDGE_SYSTEM, user=prompt["user"])
    result = _normalize_judge(response, config)
    storage.add_decision(
        conn,
        int(episode["id"]),
        stage="transcript_judge",
        model=config.llm.model,
        prompt=prompt,
        response=result,
    )
    include_status = "published" if str(episode["status"]) == "published" else "transcribed"
    storage.set_judgement(conn, int(episode["id"]), result, include_status=include_status)
    conn.commit()
    return result


def build_judge_prompt(config: Config, episode) -> dict[str, str]:
    roster_lines: list[str] = []
    for lab in config.labs:
        aliases = ", ".join(lab.aliases)
        people = ", ".join(lab.people)
        roster_lines.append(f"- {lab.name} (aliases: {aliases}): {people}")
    medium = _item_value(episode, "medium") or "podcast"
    authors = _item_value(episode, "authors_json") or "[]"
    user = f"""
Decide whether this {medium} item is worth preparing as a candidate for AI Radar.

This is a metadata-only prefilter. It does not publish the item. Include only if it likely features or was
substantially authored by a person who is a current or recent technical member, founder, executive, senior research leader, engineering
leader, product leader, AI infrastructure leader, or explicitly listed roster person at one of the
target organizations below.

Target organizations and seed roster examples:
{chr(10).join(roster_lines)}

Rules:
- "labs" must contain only target organizations where the qualifying guest currently works or recently worked.
- "matched_people" must contain only people who appear to be actual guests, interviewees, named speakers, or verified authors of this item.
- Do not add labs merely because they are discussed, invested, partnered, collaborated, competed, mentioned, or employ someone who is not the guest.
- For podcasts and YouTube channels, do not include items that only discuss target labs without a qualifying guest or central speaker.
- For a watched person's own blog or X account, authorship may establish the person, but routine, promotional, or insubstantial posts do not qualify.
- Do not include journalists, investors, analysts, commentators, customers, or partners unless they are explicitly listed in the target roster below.
- If an apparent guest works only at an organization absent from the target roster, return include=false unless the metadata verifies a current or recent target-organization role for that guest.
- You may include qualifying people who are not in the seed roster if metadata or strong world knowledge verify their target-lab affiliation.
- Be conservative when the guest or affiliation is ambiguous.
- If no qualifying target-lab guest is found, return include=false, labs=[], and matched_people=[].

Medium: {medium}
Source: {episode['feed_name']}
Title: {episode['title']}
Published: {episode['published_at'] or 'unknown'}
Original URL: {episode['episode_url'] or 'unknown'}
Known authors or hosts: {authors}

Metadata:
{truncate(strip_html(episode['description'] or ''), config.llm.max_metadata_chars)}

Return strict JSON with:
include: boolean
confidence: number from 0 to 1
labs: array of target organization names where qualifying guests work or recently worked
matched_people: array of qualifying guest people only
guest_names: array of all apparent guests
reason: concise string
"""
    return {"user": user.strip()}


def build_transcript_judge_prompt(config: Config, episode) -> dict[str, str]:
    roster_lines: list[str] = []
    for lab in config.labs:
        aliases = ", ".join(lab.aliases)
        people = ", ".join(lab.people)
        roster_lines.append(f"- {lab.name} (aliases: {aliases}): {people}")
    transcript = truncate(episode["transcript_text"] or "", config.llm.max_transcript_chars)
    medium = _item_value(episode, "medium") or "podcast"
    authors = _item_value(episode, "authors_json") or "[]"
    user = f"""
Make the final publication decision for this AI Radar {medium} item using its normalized full text.

The site labels are people affiliations, not topics. Include the item only if its full text,
metadata, or your strong world knowledge verifies that an actual guest, central speaker, or author is a current or recent
technical member, founder, executive, senior research leader, engineering leader, product leader,
AI infrastructure leader, or explicitly listed roster person at one of the target organizations below.

Target organizations and seed roster examples:
{chr(10).join(roster_lines)}

Rules:
- "labs" must contain only target organizations where qualifying guests work or recently worked.
- "matched_people" must contain only qualifying guests, central speakers, or verified authors, not people merely mentioned.
- A target-lab person who is only discussed, quoted, referenced in news, or mentioned by another guest does not qualify.
- Do not add labs because they are discussed, invested in, partnered with, collaborated with, competed with, or mentioned.
- Do not include an item whose main guest or author works only at an organization absent from the target roster unless that person is explicitly listed in the target roster below.
- Podcast and video introductions often state who the guest is. Prefer that over topic mentions later.
- A substantial blog post or X thread on a watched person's verified source may qualify through authorship; routine reactions, promotions, and short updates do not.
- If the full text shows that the candidate was a false positive, return include=false, labs=[], and matched_people=[].

Medium: {medium}
Source: {episode['feed_name']}
Title: {episode['title']}
Published: {episode['published_at'] or 'unknown'}
Original URL: {episode['episode_url'] or 'unknown'}
Known authors or hosts: {authors}
Candidate guests from metadata: {episode['guests_json']}
Candidate labs from metadata: {episode['labs_json']}

Metadata:
{truncate(strip_html(episode['description'] or ''), config.llm.max_metadata_chars)}

Normalized full text:
{transcript}

Return strict JSON with:
include: boolean
confidence: number from 0 to 1
labs: array of target organization names where qualifying guests work or recently worked
matched_people: array of qualifying guest people only
guest_names: array of all apparent guests
reason: concise string explaining the guest affiliation evidence
"""
    return {"user": user.strip()}


def build_summary_prompt(config: Config, episode) -> dict[str, str]:
    transcript = truncate(episode["transcript_text"] or "", config.llm.max_transcript_chars)
    medium = _item_value(episode, "medium") or "podcast"
    user = f"""
Summarize this {medium} item for someone tracking what major AI labs and their leaders are saying in public.

Medium: {medium}
Source: {episode['feed_name']}
Title: {episode['title']}
Original URL: {episode['episode_url'] or 'unknown'}
Verified people: {_item_value(episode, 'matched_people_json') or _item_value(episode, 'guests_json') or '[]'}
Verified affiliations: {_item_value(episode, 'labs_json') or '[]'}

Normalized full text:
{transcript}

Return strict JSON with:
title: short display title
summary: 2-4 concise paragraphs
short_summary: one complete sentence, roughly 80 characters and no more than 120 characters, for compact cards and feeds
key_points: array of 4-8 concrete bullets
topics: array of short topic tags
hosts: array of host names if identifiable
guests: array of guest names if identifiable
"""
    return {"user": user.strip()}


def extract_json(content: str) -> dict[str, Any]:
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMError("LLM response did not contain JSON") from None
        return json.loads(content[start : end + 1])


def _request_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    response = _raw_request_json(url, payload, headers)
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected LLM response shape: {response}") from exc
    return extract_json(content)


def _raw_request_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"LLM request failed with HTTP {exc.code}: {detail[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc


def _normalize_judge(response: dict[str, Any], config: Config) -> dict[str, Any]:
    labs = _canonical_labs(config, response.get("labs"))
    guests = _list(response.get("guest_names"))
    matched_people = _matched_guest_people(response.get("matched_people"), guests)
    include = bool(response.get("include")) and bool(labs) and bool(matched_people)
    reason = str(response.get("reason", "")).strip()
    if bool(response.get("include")) and not labs:
        reason = (reason + " " if reason else "") + "No configured target-lab guest affiliation was verified."
    elif bool(response.get("include")) and not matched_people:
        reason = (reason + " " if reason else "") + "No qualifying target-lab guest was named."
    return {
        "include": include,
        "confidence": _confidence(response.get("confidence")),
        "labs": labs if include else [],
        "matched_people": matched_people if include else [],
        "guest_names": guests,
        "reason": reason,
    }


def _confidence(value: Any) -> float:
    """Coerce a model-supplied confidence into a 0-1 float.

    Models occasionally answer with "high", "90%", or null. None of those are
    worth failing an otherwise usable judgement over, so unparseable values
    fall back to 0.0 and out-of-range numbers are clamped.
    """
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence != confidence:  # NaN
        return 0.0
    return min(1.0, max(0.0, confidence))


def _normalize_summary(response: dict[str, Any]) -> dict[str, Any]:
    summary = str(response.get("summary", "")).strip()
    key_points = _list(response.get("key_points"))
    title = str(response.get("title", "")).strip()
    short_summary = _normalize_short_summary(
        response.get("short_summary"),
        summary=summary,
        key_points=key_points,
        title=title,
    )
    return {
        "title": title,
        "summary": summary,
        "short_summary": short_summary,
        "summary_html": paragraphs_to_html(summary),
        "key_points": key_points,
        "topics": _list(response.get("topics")),
        "hosts": _list(response.get("hosts")),
        "guests": _list(response.get("guests")),
    }


def _normalize_short_summary(value: Any, *, summary: str, key_points: list[str], title: str) -> str:
    candidates = [
        str(value or "").strip(),
        *(point.strip() for point in key_points),
        _first_complete_sentence(summary),
        title,
    ]
    for candidate in candidates:
        short = _short_complete_text(candidate)
        if short:
            return short
    return ""


def _first_complete_sentence(value: str) -> str:
    normalized = " ".join(value.split())
    for index, char in enumerate(normalized):
        if char in ".!?" and index >= 36:
            return normalized[: index + 1]
    return normalized


def _short_complete_text(value: str, *, target_chars: int = 80, max_chars: int = 120) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return ""
    if len(normalized) <= target_chars:
        return normalized
    earliest = 40
    for index, char in enumerate(normalized[: max_chars + 1]):
        if char in ".!?;" and index >= earliest:
            text = normalized[: index + 1].rstrip()
            if text.endswith(";"):
                text = text[:-1].rstrip(" ,;:") + "."
            return text
    if len(normalized) <= max_chars:
        return normalized
    return ""


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _canonical_labs(config: Config, value: Any) -> list[str]:
    aliases: dict[str, str] = {}
    for lab in config.labs:
        aliases[lab.name.casefold()] = lab.name
        for alias in lab.aliases:
            aliases[alias.casefold()] = lab.name

    canonical: list[str] = []
    seen: set[str] = set()
    for item in _list(value):
        lab = aliases.get(item.casefold())
        if lab and lab not in seen:
            canonical.append(lab)
            seen.add(lab)
    return canonical


def _matched_guest_people(matched_people: Any, guest_names: list[str]) -> list[str]:
    matched = _list(matched_people)
    if not guest_names:
        return matched
    guest_keys = {_name_key(guest) for guest in guest_names}
    return [person for person in matched if _name_key(person) in guest_keys]


def _name_key(value: str) -> str:
    return " ".join(value.casefold().replace(".", " ").split())


def _item_value(item, key: str):
    try:
        return item[key]
    except (KeyError, IndexError):
        return None


JUDGE_SYSTEM = """You are a conservative multimodal AI Radar filter. Return only valid JSON."""

SUMMARY_SYSTEM = """You summarize normalized technical AI source material. Return only valid JSON."""
