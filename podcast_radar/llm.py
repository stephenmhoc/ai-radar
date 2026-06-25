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
    result = _normalize_judge(response)
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


def build_judge_prompt(config: Config, episode) -> dict[str, str]:
    roster_lines: list[str] = []
    for lab in config.labs:
        aliases = ", ".join(lab.aliases)
        people = ", ".join(lab.people)
        roster_lines.append(f"- {lab.name} (aliases: {aliases}): {people}")
    user = f"""
Decide whether this podcast episode should be processed for the AI lab podcast radar.

Include the episode only if it likely features a guest who is a current or recent technical member,
founder, executive, senior research leader, engineering leader, product leader, or AI infrastructure
leader from one of the target organizations. Do not include episodes that only discuss these companies
without a qualifying guest. Do not include journalists, investors, analysts, or commentators unless they
also hold a qualifying role at a target lab.

Target organizations and seed roster examples:
{chr(10).join(roster_lines)}

You may include qualifying people who are not in the seed roster if the episode metadata clearly names
their affiliation with a target organization. Be conservative when the guest is ambiguous.

Podcast: {episode['feed_name']}
Title: {episode['title']}
Published: {episode['published_at'] or 'unknown'}
Episode URL: {episode['episode_url'] or 'unknown'}

Metadata:
{truncate(strip_html(episode['description'] or ''), config.llm.max_metadata_chars)}

Return strict JSON with:
include: boolean
confidence: number from 0 to 1
labs: array of target organization names
matched_people: array of qualifying people found in the metadata
guest_names: array of all apparent guests
reason: concise string
"""
    return {"user": user.strip()}


def build_summary_prompt(config: Config, episode) -> dict[str, str]:
    transcript = truncate(episode["transcript_text"] or "", config.llm.max_transcript_chars)
    user = f"""
Summarize this podcast episode for someone tracking what major AI labs are saying in public.

Podcast: {episode['feed_name']}
Title: {episode['title']}
Episode URL: {episode['episode_url'] or 'unknown'}
Candidate guests from judging: {episode['guests_json']}
Candidate labs from judging: {episode['labs_json']}

Transcript:
{transcript}

Return strict JSON with:
title: short display title
summary: 2-4 concise paragraphs
key_points: array of 4-8 concrete bullets
topics: array of short topic tags
hosts: array of host names if identifiable
guests: array of guest names if identifiable
labs: array of target labs discussed by the relevant guest
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


def _normalize_judge(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "include": bool(response.get("include")),
        "confidence": float(response.get("confidence", 0.0) or 0.0),
        "labs": _list(response.get("labs")),
        "matched_people": _list(response.get("matched_people")),
        "guest_names": _list(response.get("guest_names")),
        "reason": str(response.get("reason", "")).strip(),
    }


def _normalize_summary(response: dict[str, Any]) -> dict[str, Any]:
    summary = str(response.get("summary", "")).strip()
    key_points = _list(response.get("key_points"))
    return {
        "title": str(response.get("title", "")).strip(),
        "summary": summary,
        "summary_html": paragraphs_to_html(summary),
        "key_points": key_points,
        "topics": _list(response.get("topics")),
        "hosts": _list(response.get("hosts")),
        "guests": _list(response.get("guests")),
        "labs": _list(response.get("labs")),
    }


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


JUDGE_SYSTEM = """You are a conservative podcast filter. Return only valid JSON."""

SUMMARY_SYSTEM = """You summarize technical AI podcast transcripts. Return only valid JSON."""
