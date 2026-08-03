from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import urllib.parse
import urllib.request

from .config import Config
from . import storage
from .text import clean_text, slugify, strip_html


class TranscriptionError(RuntimeError):
    pass


MAX_PROMPT_DESCRIPTION_CHARS = 240


def transcribe_episode(config: Config, conn, episode, *, command_output: bool = True) -> pathlib.Path:
    if config.transcription.provider != "command":
        raise TranscriptionError(f"unsupported transcription.provider: {config.transcription.provider}")
    audio_url = episode["audio_url"]
    if not audio_url:
        raise TranscriptionError("item has no transcribable media")
    executable = shutil.which(config.transcription.command)
    if executable is None:
        raise TranscriptionError(f"transcription command not found: {config.transcription.command}")

    config.transcription.audio_dir.mkdir(parents=True, exist_ok=True)
    config.transcription.transcript_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{episode['id']}-{slugify(episode['title'])[:80]}"
    is_youtube = str(_episode_value(episode, "medium") or "") == "youtube" or str(
        _episode_value(episode, "audio_type") or ""
    ) == "video/youtube"
    audio_path = config.transcription.audio_dir / f"{stem}{'.wav' if is_youtube else _audio_suffix(audio_url)}"
    output_stem = config.transcription.transcript_dir / stem
    output_path = pathlib.Path(
        config.transcription.output_path.format(
            audio_path=audio_path,
            output_stem=output_stem,
            output_dir=config.transcription.transcript_dir,
            episode_id=episode["id"],
        )
    )

    if not output_path.exists():
        if not audio_path.exists():
            if is_youtube:
                download_youtube_audio(audio_url, audio_path, command_output=command_output)
            else:
                download_audio(config, audio_url, audio_path)
        context = {
            "audio_path": str(audio_path),
            "output_stem": str(output_stem),
            "output_dir": str(config.transcription.transcript_dir),
            "episode_id": str(episode["id"]),
            **_episode_prompt_context(episode),
        }
        args = [arg.format(**context) for arg in config.transcription.args]
        _run_transcription_command([executable, *args], command_output=command_output)
    transcript = clean_text(output_path.read_text(encoding="utf-8"))
    if not transcript:
        raise TranscriptionError(f"transcription command produced an empty file: {output_path}")
    storage.set_content(conn, int(episode["id"]), transcript, output_path)
    conn.commit()
    if not config.transcription.keep_audio and audio_path.exists():
        audio_path.unlink()
    return output_path


def _run_transcription_command(command: list[str], *, command_output: bool) -> None:
    if command_output:
        subprocess.run(command, check=True)
        return

    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout
    if detail:
        detail = detail[-2000:]
        raise TranscriptionError(f"transcription command failed with exit {result.returncode}: {detail}")
    raise TranscriptionError(f"transcription command failed with exit {result.returncode}")


def download_audio(config: Config, audio_url: str, output_path: pathlib.Path) -> None:
    max_bytes = config.transcription.max_audio_mb * 1024 * 1024
    request = urllib.request.Request(audio_url, headers={"User-Agent": config.app.user_agent})
    with urllib.request.urlopen(request, timeout=180) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise TranscriptionError(f"audio is larger than max_audio_mb: {length} bytes")
        written = 0
        with output_path.open("wb") as fh:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise TranscriptionError("audio exceeded max_audio_mb while downloading")
                fh.write(chunk)


def download_youtube_audio(url: str, output_path: pathlib.Path, *, command_output: bool) -> None:
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise TranscriptionError("yt-dlp is required to transcribe YouTube appearances")
    command = [
        executable,
        "--no-playlist",
        "--extract-audio",
        "--audio-format",
        "wav",
        "--postprocessor-args",
        "ffmpeg:-ar 16000 -ac 1",
        "--output",
        str(output_path),
        url,
    ]
    _run_transcription_command(command, command_output=command_output)
    if not output_path.exists():
        raise TranscriptionError(f"yt-dlp did not create expected audio file: {output_path}")


def _audio_suffix(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = pathlib.Path(path).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    return ".mp3"


def _episode_prompt_context(episode) -> dict[str, str]:
    hosts = _json_list(_episode_value(episode, "hosts_json"))
    description = strip_html(str(_episode_value(episode, "description") or ""))
    if len(description) > MAX_PROMPT_DESCRIPTION_CHARS:
        clipped = description[:MAX_PROMPT_DESCRIPTION_CHARS].rsplit(" ", 1)[0]
        description = clipped or description[:MAX_PROMPT_DESCRIPTION_CHARS]
    return {
        "feed_name": str(_episode_value(episode, "feed_name") or ""),
        "episode_title": str(_episode_value(episode, "title") or ""),
        "episode_hosts": ", ".join(hosts),
        "episode_description": description,
        "source_name": str(_episode_value(episode, "feed_name") or ""),
        "item_title": str(_episode_value(episode, "title") or ""),
        "item_authors": ", ".join(hosts),
        "item_description": description,
    }


def _episode_value(episode, key: str):
    try:
        return episode[key]
    except (KeyError, IndexError):
        return None


def _json_list(value) -> list[str]:
    if not value:
        return []
    try:
        items = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    return [str(item) for item in items if str(item).strip()]
