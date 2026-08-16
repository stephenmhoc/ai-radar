#!/usr/bin/env python3
"""Derive the homelab config from config.toml.

Feeds, labs, sources, and prompts stay single-sourced in config.toml; this only
rewrites the [transcription] block so the coordinator dispatches to a Mac worker
instead of shelling out to whisper-cli it does not have.

    python3 deploy/make-vm-config.py config.toml deploy/config.vm.toml
"""

from __future__ import annotations

import pathlib
import sys

REMOTE_KEYS = """
# Homelab: no GPU here. Transcription is handed to a Mac worker with Metal
# through the shared broker queue. See the transcribe-broker repo.
mode = "remote"
queue_root = "/opt/transcribe-queue"
worker_ssh = "merimerimeri@100.113.106.26"
# Ignored in practice: the Mac's authorized_keys forces this key to run
# `launchctl kickstart` and nothing else. Kept accurate for documentation.
worker_command = "launchctl kickstart gui/502/com.merimeri.transcribe-agent"
lease_hours = 6
"""


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source = pathlib.Path(argv[1])
    target = pathlib.Path(argv[2])
    lines = source.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    in_transcription = False
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            # Leaving [transcription] for the next section: append before it.
            if in_transcription and not inserted:
                out.append(REMOTE_KEYS.strip())
                out.append("")
                inserted = True
            in_transcription = stripped == "[transcription]"
        out.append(line)

    if in_transcription and not inserted:  # [transcription] was the last section
        out.append(REMOTE_KEYS.strip())
        inserted = True
    if not inserted:
        print("error: no [transcription] section found", file=sys.stderr)
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
