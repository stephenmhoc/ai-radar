from __future__ import annotations

import pathlib
import shutil
import subprocess
import urllib.parse
import urllib.request

from .config import Config
from . import storage
from .text import clean_text, slugify


class TranscriptionError(RuntimeError):
    pass


def transcribe_episode(config: Config, conn, episode) -> pathlib.Path:
    if config.transcription.provider != "command":
        raise TranscriptionError(f"unsupported transcription.provider: {config.transcription.provider}")
    audio_url = episode["audio_url"]
    if not audio_url:
        raise TranscriptionError("episode has no audio enclosure")
    executable = shutil.which(config.transcription.command)
    if executable is None:
        raise TranscriptionError(f"transcription command not found: {config.transcription.command}")

    config.transcription.audio_dir.mkdir(parents=True, exist_ok=True)
    config.transcription.transcript_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{episode['id']}-{slugify(episode['title'])[:80]}"
    audio_path = config.transcription.audio_dir / f"{stem}{_audio_suffix(audio_url)}"
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
        download_audio(config, audio_url, audio_path)
        context = {
            "audio_path": str(audio_path),
            "output_stem": str(output_stem),
            "output_dir": str(config.transcription.transcript_dir),
            "episode_id": str(episode["id"]),
        }
        args = [arg.format(**context) for arg in config.transcription.args]
        subprocess.run([executable, *args], check=True)
    transcript = clean_text(output_path.read_text(encoding="utf-8"))
    if not transcript:
        raise TranscriptionError(f"transcription command produced an empty file: {output_path}")
    storage.set_transcript(conn, int(episode["id"]), transcript, output_path)
    conn.commit()
    if not config.transcription.keep_audio and audio_path.exists():
        audio_path.unlink()
    return output_path


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


def _audio_suffix(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = pathlib.Path(path).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    return ".mp3"

